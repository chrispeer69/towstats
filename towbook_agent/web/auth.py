"""The gate in front of the dashboard, and the fence around each tenant.

WHAT THIS IS, STATED PLAINLY AND ONCE
------------------------------------
Two modes, and ``web/accounts.py`` decides which by counting rows:

**Shared password.** No accounts exist. One password (``DASHBOARD_PASSWORD``,
default ``1234``), a signed cookie, and access to every company on the install.
This is what this module has always done, it is what was asked for, and it is
still correct for the deployment this system was built on -- one owner, two of
his own legal entities. It is NOT adequate for two towing companies that do not
own each other, and the login page says so on screen while the default password
is still in place.

**Accounts.** At least one row in ``dashboard_users``. Username and password,
a cookie that names the user, and -- the part that matters -- a request scoped
to the companies that user's row lists, enforced in one place rather than at
each endpoint. The shared password stops being accepted entirely; see
``accounts.py`` for why there is no break-glass fallback.

THE FENCE
---------
:class:`PasswordGateMiddleware` wraps every request in
``core.companies.use_visible_companies(principal.company_ids)``. While that is
set, the company registry behaves as though the roster held only that reader's
companies: the switcher cannot list another tenant, ``/company/<their-id>``
resolves to one of the reader's own, ``?company=`` on a JSON endpoint does the
same, and the merged view sums the reader's companies and nobody else's.

That is why it is middleware and not a decorator. The comment above
``add_middleware`` in app.py has it right: a dashboard where forgetting a check
publishes a customer's numbers is a dashboard that will eventually publish a
customer's numbers. There is one check, it is on the outside, and an endpoint
added next year is behind it without its author doing anything.

It is a PURE ASGI middleware rather than a ``BaseHTTPMiddleware`` for exactly
this reason: ``BaseHTTPMiddleware`` runs the downstream application in a
separate anyio task, and a :class:`~contextvars.ContextVar` set around that
call is not reliably the one the endpoint reads. A scope that silently fails to
apply is worse than no scope at all, because the code says it is safe.

DESIGN NOTES
------------
* **No new dependency.** Cookies are signed with :mod:`hmac` and
  :mod:`hashlib`; passwords are PBKDF2 from the same standard library.
* **The signature covers a fingerprint that changes when access should end.**
  In shared-password mode that is a digest of the password, so rotating
  ``DASHBOARD_PASSWORD`` invalidates every outstanding session. In accounts
  mode it is the user's ``password_changed_at``, so changing one person's
  password ends that person's sessions and nobody else's. An iteration-count
  re-hash deliberately does not move it -- see ``accounts.authenticate``.
* **The cookie carries a user id, never a role or a company list.** Both are
  read from the row on every request, so disabling an account or narrowing its
  scope takes effect on the next click rather than in thirty days when the
  cookie expires.
* **``SESSION_SECRET`` may be unset.** One is generated at boot and a warning
  says what that costs. Refusing to boot without it would make the
  out-of-the-box path fail, which is the opposite of what was asked for.
* **``/healthz`` is exempt**, so Railway's health check passes without
  credentials. It already leaks nothing.
* **Timing-safe comparison** on passwords and signatures alike.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any, Final
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..core import companies as _companies
from . import accounts as _accounts

__all__ = [
    "COOKIE_NAME",
    "DEFAULT_PASSWORD",
    "PasswordGateMiddleware",
    "PASSWORD_CHANGE_PATH",
    "clear_session_cookie",
    "current_principal",
    "dashboard_password",
    "is_authenticated",
    "is_exempt",
    "issue_token",
    "issue_user_token",
    "password_is_default",
    "resolve_principal",
    "safe_next",
    "session_max_age",
    "set_session_cookie",
    "set_user_session_cookie",
    "token_is_valid",
    "verify_password",
]

logger = logging.getLogger(__name__)

#: What the password is when nobody has set one, and only while no account
#: exists. Published here, in the README and on the login page: it is a
#: starting value, not a secret.
DEFAULT_PASSWORD: Final[str] = "1234"

COOKIE_NAME: Final[str] = "tbk_session"

#: Shared-password token. Bumped if the payload shape ever changes, so an old
#: cookie is rejected rather than misparsed.
_TOKEN_VERSION: Final[str] = "v1"

#: Account token. A different version tag, so a session minted under the shared
#: password cannot survive the creation of the first account -- the moment this
#: install grows more than one tenant's reader, everybody signs in again.
_USER_TOKEN_VERSION: Final[str] = "v2"

#: How long a login lasts. Long by default because the owner opens this several
#: times a day and a board that logs him out is a board he stops opening.
DEFAULT_SESSION_DAYS: Final[int] = 30

#: Where an account still on its handed-over password is sent, and the only
#: page it may reach.
PASSWORD_CHANGE_PATH: Final[str] = "/account/password"

#: Paths served without a session.
_EXEMPT_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/login", "/favicon.ico"})
_EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("/static/",)

#: Paths a signed-in user may reach while ``must_change_password`` is set.
#: Everything else redirects to the change form -- an account still on the
#: password that was handed to it over the phone is an account whose password
#: is in somebody's call history.
_PASSWORD_CHANGE_ALLOWED: Final[frozenset[str]] = frozenset(
    {PASSWORD_CHANGE_PATH, "/logout"}
)

_boot_secret: bytes | None = None
_warned_about_boot_secret: bool = False


# ==========================================================================
# Configuration
# ==========================================================================


def dashboard_password() -> str:
    """The shared password. ``DASHBOARD_PASSWORD``, or ``1234``."""
    return (os.environ.get("DASHBOARD_PASSWORD") or "").strip() or DEFAULT_PASSWORD


def password_is_default() -> bool:
    """True while the shared password is still the shipped ``1234``.

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
#
# Both forms are `<version>.<...>.<issued_at>.<signature>`, and both sign a
# FINGERPRINT of something that changes when the session should end rather than
# the secret itself. Nothing sent to the browser contains a password, a hash,
# or anything derived from one that is useful without the server's key.
# ==========================================================================


def _password_fingerprint(password: str) -> str:
    """A digest of the shared password, so a rotation invalidates live sessions."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def _signature(issued_at: int, secret: bytes, password: str) -> str:
    message = f"{_TOKEN_VERSION}.{issued_at}.{_password_fingerprint(password)}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def issue_token(now: int | None = None) -> str:
    """A signed shared-password session token."""
    issued_at = int(now if now is not None else time.time())
    signature = _signature(issued_at, session_secret(), dashboard_password())
    return f"{_TOKEN_VERSION}.{issued_at}.{signature}"


def token_is_valid(token: str | None, now: int | None = None) -> bool:
    """Whether ``token`` is a shared-password token we signed, recently enough."""
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
    if not _age_is_acceptable(issued_at, now):
        return False
    expected = _signature(issued_at, session_secret(), dashboard_password())
    return hmac.compare_digest(expected, signature)


def _age_is_acceptable(issued_at: int, now: int | None = None) -> bool:
    """Not expired, and not issued in the future by more than a clock skew.

    A cookie issued in the future is a skewed clock or a forgery; five minutes
    covers the first, and nothing should cover the second.
    """
    age = int(now if now is not None else time.time()) - issued_at
    return -300 <= age <= session_max_age()


def _user_fingerprint(password_changed_at: Any) -> str:
    """A digest of when this user's password last changed.

    The whole session-invalidation rule in one value: change a password and
    every cookie signed against the old stamp stops verifying, for that user
    and for nobody else. It is not derived from the hash, so raising the PBKDF2
    iteration count -- which re-hashes the row without the password changing --
    does not sign anybody out.
    """
    return hashlib.sha256(str(password_changed_at).encode("utf-8")).hexdigest()[:16]


def _user_signature(
    user_id: int, issued_at: int, secret: bytes, password_changed_at: Any
) -> str:
    message = (
        f"{_USER_TOKEN_VERSION}.{user_id}.{issued_at}."
        f"{_user_fingerprint(password_changed_at)}"
    ).encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def issue_user_token(user: Any, now: int | None = None) -> str:
    """A signed session token naming one account.

    Carries the row id and nothing else about the user. Their role and their
    company scope are read from the database on every request, so an operator
    who narrows somebody's access does not have to wait out a thirty-day
    cookie for it to take effect.
    """
    issued_at = int(now if now is not None else time.time())
    signature = _user_signature(
        int(user.id), issued_at, session_secret(), user.password_changed_at
    )
    return f"{_USER_TOKEN_VERSION}.{int(user.id)}.{issued_at}.{signature}"


def _read_user_token(token: str | None, now: int | None = None) -> int | None:
    """The user id in a valid account token, or ``None``.

    The signature cannot be checked without the user's ``password_changed_at``,
    which means a database read, so this only parses and range-checks; the
    caller does the verification once it has the row. Both steps must pass --
    see :func:`resolve_principal`.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _USER_TOKEN_VERSION:
        return None
    try:
        user_id = int(parts[1])
        issued_at = int(parts[2])
    except ValueError:
        return None
    if not _age_is_acceptable(issued_at, now):
        return None
    return user_id


def _user_token_matches(token: str, user: Any) -> bool:
    parts = token.split(".")
    if len(parts) != 4:
        return False
    try:
        issued_at = int(parts[2])
    except ValueError:
        return False
    expected = _user_signature(
        int(user.id), issued_at, session_secret(), user.password_changed_at
    )
    return hmac.compare_digest(expected, parts[3])


def verify_password(candidate: str | None) -> bool:
    """Timing-safe check of a submitted shared password.

    Refuses everything once accounts exist, whatever the variable still says.
    ``DASHBOARD_PASSWORD`` is usually left set on the Railway service after the
    first account is created, and a password that goes on working there would
    be an unnamed extra account with access to every company.
    """
    if _accounts.accounts_are_configured():
        return False
    return hmac.compare_digest((candidate or "").strip(), dashboard_password())


# ==========================================================================
# Who is making this request
# ==========================================================================


def resolve_principal(request: Request) -> _accounts.Principal | None:
    """The signed-in reader, or ``None``.

    Which token is accepted follows the mode, and only ever one of them:

    * accounts exist -> a ``v2`` token whose signature verifies against the
      named user's current ``password_changed_at``, and whose account is still
      enabled. A ``v1`` shared-password cookie is refused, so creating the
      first account signs out every session that predates it.
    * no accounts    -> a ``v1`` token against ``DASHBOARD_PASSWORD``.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None

    if not _accounts.accounts_are_configured():
        return _accounts.shared_password_principal() if token_is_valid(token) else None

    user_id = _read_user_token(token)
    if user_id is None:
        return None
    user = _accounts.get_user(user_id=user_id)
    if user is None or not user.enabled:
        return None
    if not _user_token_matches(token, user):
        return None
    return _accounts.principal_for_user(user)


def current_principal(request: Request) -> _accounts.Principal | None:
    """The principal the middleware already resolved for this request.

    Endpoints read this rather than calling :func:`resolve_principal` again:
    the middleware has done the database read, and doing it twice per page is
    two connections out of a pool of five for one answer.
    """
    return getattr(request.state, "principal", None)


def is_authenticated(request: Request) -> bool:
    """Whether this request carries a usable session."""
    return current_principal(request) is not None or resolve_principal(request) is not None


# ==========================================================================
# Requests and responses
# ==========================================================================


def is_exempt(path: str) -> bool:
    """Whether ``path`` is served without a session."""
    path = path or "/"
    if path in _EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


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


def _set_cookie(response: Response, request: Request, token: str) -> Response:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=session_max_age(),
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )
    return response


def set_session_cookie(response: Response, request: Request) -> Response:
    """Issue a shared-password session."""
    return _set_cookie(response, request, issue_token())


def set_user_session_cookie(response: Response, request: Request, user: Any) -> Response:
    """Issue a session naming ``user``."""
    return _set_cookie(response, request, issue_user_token(user))


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


class PasswordGateMiddleware:
    """Require a session, then fence the request to that reader's companies.

    Three things happen here and nowhere else:

    1. **The gate.** No usable session and the path is not exempt -> the login
       page (or a 401 for ``/api``).
    2. **The fence.** ``core.companies`` is scoped to the reader's companies
       for the whole downstream call, so no endpoint can resolve, list or merge
       a company that reader may not see -- including endpoints written later
       by someone who has never read this file.
    3. **The password-change wall.** An account still carrying the password it
       was handed can reach the change form and sign out, and nothing else.

    A pure ASGI middleware, not a ``BaseHTTPMiddleware``: the fence is a
    :class:`~contextvars.ContextVar`, and ``BaseHTTPMiddleware`` runs the
    application in a separate task where a variable set around the call is not
    reliably visible. Here the downstream app is awaited in this very context,
    so the scope is exactly as wide as it looks.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path

        if is_exempt(path):
            await self.app(scope, receive, send)
            return

        principal = resolve_principal(request)
        if principal is None:
            await login_redirect(request)(scope, receive, send)
            return

        if principal.must_change_password and path not in _PASSWORD_CHANGE_ALLOWED:
            await self._password_change_required(request)(scope, receive, send)
            return

        # Endpoints read the principal from here rather than re-resolving it.
        scope.setdefault("state", {})
        scope["state"]["principal"] = principal

        # THE FENCE. Everything downstream -- every query, every template, the
        # switcher, the merged view -- sees a roster containing only this
        # reader's companies. `None` is the operator and the shared-password
        # session, and means unscoped.
        with _companies.use_visible_companies(principal.company_ids):
            await self.app(scope, receive, send)

    @staticmethod
    def _password_change_required(request: Request) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {
                    "error": "password change required",
                    "change_password": PASSWORD_CHANGE_PATH,
                },
                status_code=403,
            )
        return RedirectResponse(url=PASSWORD_CHANGE_PATH, status_code=303)
