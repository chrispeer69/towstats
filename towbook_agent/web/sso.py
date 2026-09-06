"""Roadside SSO sign-in for the dashboard (OpenID Connect, code + PKCE).

Two routes, both outside the password gate (see ``auth._EXEMPT_PREFIXES``):

    GET /sso/login     -> send the browser to Roadside SSO
    GET /sso/callback  -> exchange the code, look the person up, mint the
                          same session cookie a password sign-in would

A person Roadside vouches for is created here on first sign-in: Roadside
owners and admins become operators (every company on the install), everyone
else a member scoped to every company currently on the roster. Their
password is random and unknown; they sign in through Roadside.

Configuration (environment):
    ROADSIDE_SSO_ISSUER         e.g. https://roadside-sso-production.up.railway.app
    ROADSIDE_SSO_CLIENT_ID      default ``ustowstats``
    ROADSIDE_SSO_CLIENT_SECRET  from Roadside -> Platform -> App catalog -> Secret
    ROADSIDE_SSO_REDIRECT_URI   optional; defaults to <this host>/sso/callback
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
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from ..core import companies as _companies
from ..core.models import ROLE_MEMBER, ROLE_OPERATOR
from . import accounts, auth

logger = logging.getLogger(__name__)

STATE_COOKIE = "tbk_sso"
STATE_TTL_SECONDS = 600
ADMIN_ROLES = frozenset({"owner", "admin"})


def config() -> dict[str, str]:
    return {
        "issuer": (os.environ.get("ROADSIDE_SSO_ISSUER") or "").strip().rstrip("/"),
        "client_id": (os.environ.get("ROADSIDE_SSO_CLIENT_ID") or "ustowstats").strip(),
        "client_secret": (os.environ.get("ROADSIDE_SSO_CLIENT_SECRET") or "").strip(),
        "redirect_uri": (os.environ.get("ROADSIDE_SSO_REDIRECT_URI") or "").strip(),
    }


def enabled() -> bool:
    cfg = config()
    return bool(cfg["issuer"] and cfg["client_secret"])


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _redirect_uri(request: Request) -> str:
    cfg = config()
    if cfg["redirect_uri"]:
        return cfg["redirect_uri"]
    scheme = "https" if auth.request_is_https(request) else request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/sso/callback"


# -- signed, short-lived state cookie (there is no server-side session) -------
def _sign(payload: str) -> str:
    return _b64url(hmac.new(auth.session_secret(), payload.encode(), hashlib.sha256).digest())


def _pack_state(data: dict[str, Any]) -> str:
    payload = _b64url(json.dumps(data, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def _unpack_state(raw: str | None) -> dict[str, Any] | None:
    if not raw or "." not in raw:
        return None
    payload, signature = raw.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode())
    except Exception:
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


def _jwt_payload(token: str) -> dict[str, Any]:
    """Unverified decode, used only for the nonce check on a token that came
    straight from the issuer over HTTPS."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode())
    except Exception:
        return {}


def _company_ids() -> list[str]:
    ids: list[str] = []
    for company in _companies.all_companies():
        value = getattr(company, "company_id", None) or getattr(company, "id", None) or getattr(company, "slug", None)
        slug = _companies.normalise_company_id(value) if value else ""
        if slug and slug not in ids:
            ids.append(slug)
    return ids


def _fail(request: Request, message: str) -> Response:
    logger.warning("roadside sso: %s", message)
    from urllib.parse import quote
    return RedirectResponse(url=f"/login?sso_error={quote(message)}", status_code=303)


# -- routes ------------------------------------------------------------------
def start(request: Request) -> Response:
    cfg = config()
    if not enabled():
        return _fail(request, "Single sign-on is not configured on this dashboard.")
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(16)
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid profile email roadside",
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    hint = request.query_params.get("login_hint")
    if hint:
        params["login_hint"] = hint
    from urllib.parse import urlencode
    response = RedirectResponse(url=f"{cfg['issuer']}/oauth/authorize?{urlencode(params)}", status_code=303)
    response.set_cookie(
        STATE_COOKIE,
        _pack_state({
            "s": state, "n": nonce, "v": verifier,
            "next": auth.safe_next(request.query_params.get("next")),
            "exp": int(time.time()) + STATE_TTL_SECONDS,
        }),
        max_age=STATE_TTL_SECONDS, httponly=True, samesite="lax", secure=auth.request_is_https(request), path="/",
    )
    return response


def callback(request: Request) -> Response:
    cfg = config()
    saved = _unpack_state(request.cookies.get(STATE_COOKIE))
    if not saved or not hmac.compare_digest(str(request.query_params.get("state", "")), str(saved.get("s", ""))):
        return _fail(request, "Your sign-in session expired. Please try again.")
    if request.query_params.get("error"):
        return _fail(request, request.query_params.get("error_description") or "Sign-in was refused by Roadside SSO.")
    code = request.query_params.get("code")
    if not code:
        return _fail(request, "Roadside SSO did not return a sign-in code.")

    try:
        with httpx.Client(timeout=15) as client:
            token_response = client.post(
                f"{cfg['issuer']}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _redirect_uri(request),
                    "code_verifier": saved["v"],
                },
                auth=(cfg["client_id"], cfg["client_secret"]),
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
            tokens = token_response.json()
            if _jwt_payload(tokens.get("id_token", "")).get("nonce") != saved.get("n"):
                return _fail(request, "Sign-in could not be verified. Please try again.")
            info = client.get(
                f"{cfg['issuer']}/oauth/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json"},
            )
            info.raise_for_status()
            claims = info.json()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return _fail(request, f"Could not complete sign-in with Roadside SSO ({exc.__class__.__name__}).")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        return _fail(request, "Roadside SSO did not provide an email address.")
    name = (claims.get("name") or email.split("@")[0]).strip()
    roles = {str(r).lower() for r in (claims.get("roles") or [])}
    role = accounts.ROLE_OPERATOR if roles & ADMIN_ROLES else accounts.ROLE_MEMBER

    user = accounts.get_user(username=email)
    if user is None:
        try:
            user = accounts.create_user(
                email,
                secrets.token_urlsafe(24) + "Aa1!",
                role=role,
                company_ids=_company_ids(),
                display_name=name,
                email=email,
                must_change_password=False,
            )
        except accounts.AccountError as exc:
            return _fail(request, str(exc))
        logger.info("roadside sso: created dashboard account %r (%s)", email, role)
    elif not getattr(user, "enabled", True):
        return _fail(request, "This dashboard account is disabled. Contact your administrator.")

    target = saved.get("next") or "/"
    if getattr(user, "must_change_password", False):
        target = auth.PASSWORD_CHANGE_PATH
    response = RedirectResponse(url=target, status_code=303)
    response.delete_cookie(STATE_COOKIE, path="/")
    logger.info("roadside sso: dashboard sign-in %r", user.username)
    return auth.set_user_session_cookie(response, request, user)
