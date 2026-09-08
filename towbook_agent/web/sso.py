"""Sign in with US Tow -- OpenID Connect against the central US Tow SSO.

THE FLOW, END TO END
--------------------
1. ``GET /auth/login`` mints a ``state``, a ``nonce`` and a PKCE verifier,
   stores the three (plus the ``?next=`` target) in a short-lived signed cookie
   and redirects the browser to the SSO's authorization endpoint, asking for
   ``openid profile email ustow``.
2. The SSO sends the browser back to ``GET /auth/callback?code=...&state=...``.
   The state must match the cookie. authlib's ``OAuth2Client`` exchanges the
   code at the token endpoint (``client_secret_basic`` plus the PKCE verifier).
3. The ID token is verified with authlib's JOSE against the SSO's JWKS: RS256
   signature, ``iss``, ``aud`` (our client id), ``exp`` and the ``nonce``.
4. The ``apps`` claim must list our client id, otherwise the person is refused
   with "This app is not on your US Tow dashboard".
5. A session cookie of our own (``tbk_sso``) is issued: a small signed JSON of
   the identity, HMAC'd with the same ``SESSION_SECRET`` the password gate uses.
   There is no users table in this project, so the cookie *is* the session.
   :func:`towbook_agent.web.auth.is_authenticated` accepts either cookie, which
   is what lets the existing password login keep working alongside this one.
6. Logout clears both cookies and, when the person came in through SSO, sends
   the browser to the SSO's end-session endpoint with
   ``post_logout_redirect_uri=<site root>`` and ``id_token_hint``.

Endpoints are read from the discovery document (``/.well-known/openid-configuration``)
and cached in the process, as are the JWKS; an unknown ``kid`` refetches the
keys once so a key rotation at the SSO does not lock everybody out.

CONFIGURATION (environment)
---------------------------
``SSO_ISSUER``        defaults to the production SSO; ``SSO_CLIENT_ID`` defaults
to ``ustowstats``; ``SSO_CLIENT_SECRET`` is the only strictly required value --
when it is unset the button is hidden and ``/auth/login`` explains why.
``SSO_REDIRECT_URI`` defaults to ``<scheme>://<host>/auth/callback`` of the
incoming request, which on the live site is the registered
``https://www.ustowstats.com/auth/callback``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import warnings
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from . import auth

with warnings.catch_warnings():
    # authlib 1.8 announces that ``authlib.jose`` will move to ``joserfc`` before
    # 2.0. The pin in requirements.txt is ``<2``; silence the notice so it does
    # not land in the deploy log on every boot. authlib installs its own
    # "always" filter when ``authlib.deprecate`` is first imported, so ours has
    # to be added after that import to take precedence.
    import authlib.deprecate

    warnings.simplefilter("ignore", authlib.deprecate.AuthlibDeprecationWarning)
    from authlib.integrations.httpx_client import OAuth2Client
    from authlib.jose import JsonWebKey, JsonWebToken
    from authlib.oidc.core import CodeIDToken

# authlib's OAuth2Client is an httpx client built on the successor package
# ``httpx2`` whenever that is installed (it arrives with ``anthropic``), and on
# ``httpx`` otherwise. Discovery, the JWKS fetch and the test transport must be
# the same flavour, so this module picks the way authlib does.
try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover - only on an install without httpx2
    import httpx

__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEFAULT_ISSUER",
    "SCOPE",
    "SESSION_COOKIE",
    "SSOError",
    "clear_session_cookies",
    "client_id",
    "end_session_url",
    "finish_login",
    "is_configured",
    "issuer",
    "session_user",
    "start_login",
    "verify_id_token",
]

logger = logging.getLogger(__name__)

DEFAULT_ISSUER: Final[str] = "https://us-tow-sso-production.up.railway.app"
DEFAULT_CLIENT_ID: Final[str] = "ustowstats"
SCOPE: Final[str] = "openid profile email ustow"

#: The identity, signed. Present on every request from an SSO session.
SESSION_COOKIE: Final[str] = "tbk_sso"
#: The raw ID token, kept only so logout can pass it as ``id_token_hint``.
ID_TOKEN_COOKIE: Final[str] = "tbk_sso_idt"
#: state + nonce + PKCE verifier between /auth/login and /auth/callback.
FLOW_COOKIE: Final[str] = "tbk_sso_flow"
FLOW_MAX_AGE: Final[int] = 10 * 60

#: How long the cached discovery document and key set are trusted.
_CACHE_SECONDS: Final[int] = 6 * 3600

#: Tests point this at an ``httpx.MockTransport``; production leaves it None.
_transport: httpx.BaseTransport | None = None

_metadata: dict[str, Any] | None = None
_metadata_at: float = 0.0
_keys: Any = None
_keys_at: float = 0.0


class SSOError(Exception):
    """A sign-in that cannot proceed. ``status_code`` is the HTTP status to answer with."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


# ==========================================================================
# Configuration
# ==========================================================================


def issuer() -> str:
    return ((os.environ.get("SSO_ISSUER") or "").strip() or DEFAULT_ISSUER).rstrip("/")


def client_id() -> str:
    return (os.environ.get("SSO_CLIENT_ID") or "").strip() or DEFAULT_CLIENT_ID


def client_secret() -> str:
    return (os.environ.get("SSO_CLIENT_SECRET") or "").strip()


def is_configured() -> bool:
    """Whether the button should be shown. Only the secret is strictly required."""
    return bool(client_secret())


def redirect_uri(request: Request) -> str:
    configured = (os.environ.get("SSO_REDIRECT_URI") or "").strip()
    if configured:
        return configured
    scheme = "https" if auth.request_is_https(request) else request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/auth/callback"


def post_logout_redirect_uri(request: Request) -> str:
    """The registered post-logout URI: the site root, derived from the redirect URI."""
    parts = urlsplit(redirect_uri(request))
    return f"{parts.scheme}://{parts.netloc}/"


# ==========================================================================
# Discovery and keys
# ==========================================================================


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0, transport=_transport)


def metadata() -> dict[str, Any]:
    """The discovery document, fetched once and cached."""
    global _metadata, _metadata_at
    if _metadata is None or time.time() - _metadata_at > _CACHE_SECONDS:
        url = f"{issuer()}/.well-known/openid-configuration"
        with _http() as http:
            response = http.get(url)
            response.raise_for_status()
            _metadata = response.json()
        _metadata_at = time.time()
    return _metadata


def key_set(refresh: bool = False) -> Any:
    """The SSO's JWKS as an authlib ``KeySet``."""
    global _keys, _keys_at
    if refresh or _keys is None or time.time() - _keys_at > _CACHE_SECONDS:
        with _http() as http:
            response = http.get(metadata()["jwks_uri"])
            response.raise_for_status()
            _keys = JsonWebKey.import_key_set(response.json())
        _keys_at = time.time()
    return _keys


def _client(request: Request) -> OAuth2Client:
    return OAuth2Client(
        client_id(),
        client_secret(),
        scope=SCOPE,
        redirect_uri=redirect_uri(request),
        code_challenge_method="S256",
        timeout=15.0,
        transport=_transport,
    )


# ==========================================================================
# Signed cookies
# ==========================================================================


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(auth.session_secret(), b"sso." + body.encode("ascii"), hashlib.sha256)
    return f"{body}.{signature.hexdigest()}"


def _read(token: str | None, max_age: int) -> dict[str, Any] | None:
    """The payload of a cookie we signed, or None if it is forged, stale or malformed."""
    if not token or token.count(".") != 1:
        return None
    body, signature = token.split(".")
    expected = hmac.new(auth.session_secret(), b"sso." + body.encode("ascii"), hashlib.sha256)
    if not hmac.compare_digest(expected.hexdigest(), signature):
        return None
    try:
        payload = json.loads(_unb64(body))
        issued_at = int(payload["iat"])
    except (ValueError, KeyError, TypeError):
        return None
    age = time.time() - issued_at
    if age < -300 or age > max_age:
        return None
    return payload


def _cookie_options(request: Request) -> dict[str, Any]:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": auth.request_is_https(request),
        "path": "/",
    }


def session_user(request: Request) -> dict[str, Any] | None:
    """The signed-in US Tow identity, or None when this is not an SSO session."""
    return _read(request.cookies.get(SESSION_COOKIE), auth.session_max_age())


def clear_session_cookies(response: Response, request: Request) -> Response:
    for name in (SESSION_COOKIE, ID_TOKEN_COOKIE, FLOW_COOKIE):
        response.delete_cookie(name, **_cookie_options(request))
    return response


# ==========================================================================
# The flow
# ==========================================================================


def start_login(request: Request, next_path: str) -> Response:
    """Redirect to the SSO authorization endpoint, remembering state/nonce/verifier."""
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(16)
    url, state = _client(request).create_authorization_url(
        metadata()["authorization_endpoint"], code_verifier=verifier, nonce=nonce
    )
    response = RedirectResponse(url=url, status_code=303)
    flow = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "next": auth.safe_next(next_path),
        "iat": int(time.time()),
    }
    response.set_cookie(FLOW_COOKIE, _sign(flow), max_age=FLOW_MAX_AGE, **_cookie_options(request))
    return response


def verify_id_token(id_token: str, nonce: str | None) -> dict[str, Any]:
    """Signature, ``iss``, ``aud``, ``exp`` and ``nonce`` -- or :class:`SSOError`."""
    jwt = JsonWebToken(["RS256"])
    options = {
        "iss": {"essential": True, "value": issuer()},
        "aud": {"essential": True, "value": client_id()},
    }

    def decode(keys: Any) -> Any:
        return jwt.decode(
            id_token,
            keys,
            claims_cls=CodeIDToken,
            claims_options=options,
            claims_params={"nonce": nonce} if nonce else None,
        )

    try:
        try:
            claims = decode(key_set())
        except ValueError:
            # Unknown ``kid``: the SSO may have rotated its key. Fetch once more.
            claims = decode(key_set(refresh=True))
        claims.validate(leeway=60)
    except Exception as exc:  # noqa: BLE001 - every JOSE error means "not signed in"
        logger.warning("US Tow ID token rejected: %s: %s", type(exc).__name__, exc)
        raise SSOError(401, "The sign-in token from US Tow could not be verified.") from exc
    return dict(claims)


def _identity(claims: dict[str, Any]) -> dict[str, Any]:
    """The subset of claims worth carrying in the cookie."""
    roles = [str(role) for role in (claims.get("roles") or [])]
    return {
        "sub": claims.get("sub"),
        "email": (claims.get("email") or "").strip().casefold(),
        "name": claims.get("name") or "",
        "org_id": claims.get("org_id"),
        "org_slug": claims.get("org_slug"),
        "org_name": claims.get("org_name") or "",
        "roles": roles,
        # The site has no role model of its own; owner/admin (or a platform
        # admin) is recorded so a future admin-only view has something to read.
        "admin": bool(claims.get("platform_admin")) or bool({"owner", "admin"} & set(roles)),
        "sid": claims.get("sid"),
        "iat": int(time.time()),
    }


def finish_login(request: Request) -> Response:
    """Handle the callback: exchange the code, verify, authorise, set the session."""
    flow = _read(request.cookies.get(FLOW_COOKIE), FLOW_MAX_AGE)
    if flow is None:
        raise SSOError(400, "That sign-in attempt has expired or was not started here. Please try again.")

    params = request.query_params
    if params.get("error"):
        raise SSOError(403, f"Sign-in refused by US Tow: {params.get('error_description') or params['error']}")
    if not params.get("state") or not hmac.compare_digest(params["state"], flow["state"]):
        raise SSOError(400, "Sign-in state mismatch. Please try again.")
    code = params.get("code")
    if not code:
        raise SSOError(400, "US Tow did not return a sign-in code.")

    try:
        token = _client(request).fetch_token(
            metadata()["token_endpoint"],
            grant_type="authorization_code",
            code=code,
            code_verifier=flow["verifier"],
        )
    except Exception as exc:  # noqa: BLE001 - network, 4xx, malformed body: all one outcome
        logger.warning("US Tow token exchange failed: %s: %s", type(exc).__name__, exc)
        raise SSOError(502, "US Tow SSO did not accept the sign-in code. Please try again.") from exc

    id_token = token.get("id_token")
    if not id_token:
        raise SSOError(502, "US Tow SSO returned no ID token.")
    claims = verify_id_token(id_token, flow.get("nonce"))

    if client_id() not in (claims.get("apps") or []):
        logger.info("US Tow user %s is not licensed for %s", claims.get("email"), client_id())
        raise SSOError(403, "This app is not on your US Tow dashboard.")

    identity = _identity(claims)
    logger.info("US Tow sign-in: %s (%s)", identity["email"], identity.get("org_slug"))
    response = RedirectResponse(url=auth.safe_next(flow.get("next")), status_code=303)
    options = _cookie_options(request)
    response.delete_cookie(FLOW_COOKIE, **options)
    response.set_cookie(SESSION_COOKIE, _sign(identity), max_age=auth.session_max_age(), **options)
    response.set_cookie(ID_TOKEN_COOKIE, id_token, max_age=auth.session_max_age(), **options)
    return response


def end_session_url(request: Request) -> str:
    """Where an SSO logout sends the browser after our cookies are cleared."""
    try:
        endpoint = metadata().get("end_session_endpoint") or f"{issuer()}/oauth/logout"
    except Exception as exc:  # noqa: BLE001 - the SSO being down must not block a logout
        logger.warning("could not read SSO discovery for logout: %s", exc)
        endpoint = f"{issuer()}/oauth/logout"
    query = {"post_logout_redirect_uri": post_logout_redirect_uri(request)}
    hint = request.cookies.get(ID_TOKEN_COOKIE)
    if hint:
        query["id_token_hint"] = hint
    return f"{endpoint}?{urlencode(query)}"
