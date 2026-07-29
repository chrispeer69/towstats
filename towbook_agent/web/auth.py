"""The password gate in front of the dashboard.

The board is now the delivery mechanism -- it is opened in a browser several
times a day instead of arriving as a text message -- so it is reachable from
the public internet and something has to stand in front of it.

WHAT THIS IS, STATED PLAINLY AND ONCE
-------------------------------------
This is a **single shared password** with a signed session cookie. It was asked
for explicitly, it is implemented exactly as asked, and the default is ``1234``
so a fresh deployment works before anybody has configured anything.

``1234`` is not adequate protection for real customer data, and it is a great
deal less adequate once this repository is running for several towing companies
whose client names, volumes and acceptance rates would all sit behind the same
four digits. There is one shared password, no per-user accounts, no audit of
who looked at what, and no lockout after repeated guesses. Anybody who learns
the password has everything. That sentence is repeated in the README and is
printed on the login page itself whenever the password is still the default, so
the person deploying cannot miss it. Set ``DASHBOARD_PASSWORD`` to something
long and random before the board holds data anybody would mind losing.

DESIGN NOTES
------------
* **No new dependency.** The cookie is signed with :mod:`hmac` and
  :mod:`hashlib` from the standard library. ``itsdangerous`` would have done the
  same job; it is not worth a line in requirements.txt for sixty lines of code.
* **The signature covers a fingerprint of the password**, so changing
  ``DASHBOARD_PASSWORD`` invalidates every outstanding session immediately. A
  password rotation that left old sessions logged in would not be a rotation.
* **``SESSION_SECRET`` may be unset.** One is generated at boot and a warning is
  logged saying what that costs: sessions do not survive a restart, and two
  replicas would not accept each other's cookies. Refusing to boot without it
  would make the out-of-the-box path fail, which is the opposite of what was
  asked for.
* **``/healthz`` is exempt**, so Railway's health check passes without
  credentials. It already leaks nothing -- see :func:`towbook_agent.web.app.liveness`.
* **Timing-safe comparison** on both the password and the signature
  (:func:`hmac.compare_digest`), because both are compared against attacker-
  supplied bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Final
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

__all__ = [
    "COOKIE_NAME",
    "DEFAULT_PASSWORD",
    "PasswordGateMiddleware",
    "clear_session_cookie",
    "dashboard_password",
    "is_authenticated",
    "is_exempt",
    "password_is_default",
    "safe_next",
    "session_max_age",
    "set_session_cookie",
    "verify_password",
]

logger = logging.getLogger(__name__)

#: What the password is when nobody has set one. Published here, in the README
#: and on the login page: it is a starting value, not a secret.
DEFAULT_PASSWORD: Final[str] = "1234"

COOKIE_NAME: Final[str] = "tbk_session"

#: Bumped if the token payload ever changes shape, so an old cookie is rejected
#: rather than misparsed.
_TOKEN_VERSION: Final[str] = "v1"

#: How long a login lasts. Long by default because the owner opens this several
#: times a day and a board that logs him out is a board he stops opening.
DEFAULT_SESSION_DAYS: Final[int] = 30

#: Paths served without a session.
#:
#: ``/healthz`` is here so Railway's health check passes -- an unauthenticated
#: 303 would be read as a healthy 3xx by some probes and a failure by others,
#: and neither is a liveness signal. ``/login`` obviously cannot require a
#: login. ``/static`` carries the stylesheet and the two vendored libraries and
#: no data at all.
_EXEMPT_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/login", "/favicon.ico"})
_EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("/static/",)

#: Generated once per process when SESSION_SECRET is unset. Module level rather
#: than per-request so every cookie in a process is signed with the same key.
_boot_secret: bytes | None = None
_warned_about_boot_secret: bool = False


# ==========================================================================
# Configuration
# ==========================================================================


def dashboard_password() -> str:
    """The shared password. ``DASHBOARD_PASSWORD``, or ``1234``."""
    return (os.environ.get("DASHBOARD_PASSWORD") or "").strip() or DEFAULT_PASSWORD


def password_is_default() -> bool:
    """True while the password is still the shipped ``1234``.

    Drives the banner on the login page. Deliberately not "is the password
    weak" -- judging that would be guesswork, whereas "you have not changed it
    from the value in the README" is a fact.
    """
    return hmac.compare_digest(dashboard_password(), DEFAULT_PASSWORD)


def session_max_age() -> int:
    """Session lifetime in seconds, from ``DASHBOARD_SESSION_DAYS``."""
    raw = (os.environ.get("DASHBOARD_SESSION_DAYS") or "").strip()
    try:
        days = int(raw) if raw else DEFAULT_SESSION_DAYS
    except ValueError:
        days = DEFAULT_SESSION_DAYS
    return max(1, min(days, 365)) * 86400


def session_secret() -> bytes:
    """The HMAC key. ``SESSION_SECRET``, or one generated at boot.

    Generating one is a supported path, not an error, because the alternative
    is a deployment that will not start until somebody reads the documentation.
    It costs exactly two things and the warning names both: every session dies
    when the process restarts, and a second replica will not accept the first
    one's cookies.
    """
    global _boot_secret, _warned_about_boot_secret

    configured = (os.environ.get("SESSION_SECRET") or "").strip()
    if configured:
        return configured.encode("utf-8")

    if _boot_secret is None:
        _boot_secret = secrets.token_bytes(32)
    if not _warned_about_boot_secret:
        _warned_about_boot_secret = True
        logger.warning(
            "SESSION_SECRET is not set, so a random one was generated at boot. "
            "Sessions will NOT survive a restart or redeploy -- everyone is "
            "logged out -- and two instances of this app would reject each "
            "other's cookies. Set SESSION_SECRET to a long random string "
            "(Railway: Variables) to fix both."
        )
    return _boot_secret


# ==========================================================================
# Tokens
# ==========================================================================


def _password_fingerprint(password: str) -> str:
    """A digest of the password, so a rotation invalidates live sessions.

    The password itself never enters the cookie or the signed payload -- only
    this digest, and only inside the HMAC input, never in anything sent to the
    browser.
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def _signature(issued_at: int, secret: bytes, password: str) -> str:
    message = f"{_TOKEN_VERSION}.{issued_at}.{_password_fingerprint(password)}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def issue_token(now: int | None = None) -> str:
    """A signed session token for the current password and secret."""
    issued_at = int(now if now is not None else time.time())
    signature = _signature(issued_at, session_secret(), dashboard_password())
    return f"{_TOKEN_VERSION}.{issued_at}.{signature}"


def token_is_valid(token: str | None, now: int | None = None) -> bool:
    """Whether ``token`` was signed by us, for this password, recently enough."""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    version, raw_issued, signature = parts
    if version != _TOKEN_VERSION:
        return False
    try:
        issued_at = int(raw_issued)
    except ValueError:
        return False

    moment = int(now if now is not None else time.time())
    age = moment - issued_at
    # A cookie issued in the future is a clock skew or a forgery; a small
    # tolerance covers the first, and nothing should cover the second.
    if age < -300 or age > session_max_age():
        return False

    expected = _signature(issued_at, session_secret(), dashboard_password())
    return hmac.compare_digest(expected, signature)


def verify_password(candidate: str | None) -> bool:
    """Timing-safe check of a submitted password."""
    return hmac.compare_digest((candidate or "").strip(), dashboard_password())


# ==========================================================================
# Requests and responses
# ==========================================================================


def is_exempt(path: str) -> bool:
    """Whether ``path`` is served without a session."""
    path = path or "/"
    if path in _EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def is_authenticated(request: Request) -> bool:
    return token_is_valid(request.cookies.get(COOKIE_NAME))


def request_is_https(request: Request) -> bool:
    """Whether the *browser* is on https, honouring the proxy header.

    Railway terminates TLS in front of the app, so the ASGI scheme is ``http``
    on a connection the user made over ``https``. Trusting
    ``X-Forwarded-Proto`` is what lets the cookie carry ``Secure`` there. On a
    local ``http://127.0.0.1`` run there is no such header and the flag is
    omitted, because a ``Secure`` cookie over plain http is simply never stored
    and the login would silently loop.
    """
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if forwarded:
        return forwarded.casefold() == "https"
    return request.url.scheme == "https"


def set_session_cookie(response: Response, request: Request) -> Response:
    response.set_cookie(
        COOKIE_NAME,
        issue_token(),
        max_age=session_max_age(),
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )
    return response


def clear_session_cookie(response: Response, request: Request) -> Response:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
    )
    return response


def safe_next(raw: str | None, fallback: str = "/") -> str:
    """Sanitise a ``?next=`` target down to a path on this site.

    An open redirect on a login form is the classic way to turn "log in to see
    your numbers" into a credential-phishing link that looks legitimate, so
    anything that is not an ordinary same-site path is discarded rather than
    repaired.
    """
    value = (raw or "").strip()
    if not value or not value.startswith("/"):
        return fallback
    if value.startswith("//") or value.startswith("/\\"):
        return fallback
    if any(character in value for character in ("\r", "\n", "\t")):
        return fallback
    return value


def login_redirect(request: Request) -> Response:
    """Where an unauthenticated request goes.

    JSON for ``/api``, because a chart fetch that follows a redirect and parses
    a login page as JSON fails in a way nobody can read. HTML pages get a 303
    so that an unauthenticated POST becomes a GET of the login form instead of
    re-posting into it.
    """
    path = request.url.path
    if path.startswith("/api/"):
        return JSONResponse(
            {"error": "authentication required", "login": "/login"}, status_code=401
        )

    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    if request.method != "GET":
        target = "/"
    return RedirectResponse(url=f"/login?next={quote(target, safe='')}", status_code=303)


class PasswordGateMiddleware(BaseHTTPMiddleware):
    """Require a valid session cookie on everything except :data:`_EXEMPT_PATHS`."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if is_exempt(request.url.path) or is_authenticated(request):
            return await call_next(request)
        return login_redirect(request)
