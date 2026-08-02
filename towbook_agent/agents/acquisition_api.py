"""Acquisition agent, JSON API path -- the PRIMARY source.

WHY THIS EXISTS (and why it outranks the Playwright/CSV path)
-------------------------------------------------------------
The UI export has no stable row identity. Towbook's "Export to Excel" actually
writes a CSV whose only id-shaped column is ``Call #`` -- and ``Call #`` is
**blank on exactly the rows this system exists to measure**: every offer that
was never accepted. Using it as the key would give one offer two different ids
across two overlapping exports and double-count it, so ``config/schema.yaml``
has to synthesise a fingerprint from ``Request Date + Provider + Service Needed
+ Expiration Date + Vehicle``. That fingerprint is deterministic, but it is a
guess about which columns are stable, and two genuinely distinct offers that
agree on all five would collapse into one row.

The JSON API returns ``callRequestId``. Verified live on 2026-07-28: 3,079
records over a 30-day window, **3,079 distinct ids, zero collisions, zero
blanks**. There is nothing to synthesise and nothing to guess.

So this path is both more robust *and* more correct:

* more robust -- no browser, no download handling, no DOM, no header drift,
  no w2ui grid, ~1.5 s per 1,000 rows instead of a full Playwright clickpath;
* more correct -- a real primary key on precisely the population (unaccepted
  offers) whose count the acceptance rate and the missed-work inventory depend
  on.

``agents/acquisition.py`` is NOT replaced. It stays as the escape hatch for the
day Towbook changes or revokes this endpoint, and it is still the only way to
run ``discover-selectors`` / ``bootstrap-session``. Choose between them with
``source: api`` / ``source: ui`` in ``config/schedule.yaml``, or ``--source``
on the CLI. ``api`` is the default.

ONE LOGIN, SEVERAL COMPANIES
----------------------------
The endpoint has no ``companyId`` parameter. It returns whatever company the
**session** is currently switched to -- the book icon in the top right of the
portal -- and a fresh login lands on the user's home company.

An account that carries more than one towing company therefore pulls only the
home one unless the session is switched first. On this install that is exactly
what hid the HONK work: it is assigned through **Auto Lyft USA, Inc (254467)**,
while every pull ever made ran on **Roadside Towing and Recovery Inc (61343)**.
Nothing was mis-parsed; the other company was simply never asked for.

:func:`select_company` performs the switch and, critically, CONFIRMS it, and
:func:`_verify_records_company` then confirms the returned rows agree. Both are
fatal on mismatch, because rows filed under the wrong company are permanent and
invisible -- see :class:`CompanyMismatch`.

Public entry points
-------------------
    acquire_api(start_date, end_date, company_id="default") -> Path
        Authenticate, switch the session to that company, page the callrequests
        endpoint, archive the raw JSON verbatim under
        raw/YYYY/MM/DD/run_<ts>.json, return that path.

    select_company(client, towbook_company_id) -> dict
        Switch an authenticated client onto a Towbook company and prove it took.

    current_company_id(client) -> str | None
        Which Towbook company the session is on right now.

    login_api(client) -> None
        Authenticate ``client`` in place. Returns None on success, raises
        LoginFailed / MfaRequired / BotWallDetected with a named outcome
        otherwise.

    login_check_api(account_id="default") -> dict
        The API-path equivalent of ``acquisition.login_check``. Same dict
        contract, same closed ``stage`` vocabulary, no browser required.

Everything verified live against the real account on 2026-07-28
----------------------------------------------------------------
* ``GET /Security/Login`` carries a hidden ``name="RequestVerificationToken"``.
* ``POST /Security/Login`` with Username / Password / RequestVerificationToken
  / ``bSignIn="Log in"`` plus Referer and Origin headers issues the session.
* Success test: a cookie named **``.xtl``** exists AND the final URL is not
  ``/Security/Login``. Towbook does **not** use ``.AspNetCore.Cookies`` --
  testing for it fails on a working login.
* Failure test: still on ``/Security/Login`` and the body carries the portal's
  invalid-credentials banner.
* ``GET /api/digitaldispatch/callrequests?extended=true&page=&pageSize=&
  startDate=&endDate=`` with ``Referer: https://app.towbook.com/requestLog/``
  and ``X-Requested-With: XMLHttpRequest``.
* Total row count arrives in the ``X-Records-Count`` response header.
* ``pageSize`` **maximum is 1000**. 2000 returns HTTP 500 after a ~30 s server
  timeout. The cap is enforced in code, not merely documented.
* 30 days = 3,079 records over 4 pages at pageSize=1000, zero overlap. A page
  past the end returns ``[]``.
* ``endDate`` is inclusive of that calendar day; there is no time-of-day
  filter, so an hourly job pulls the calendar day and metrics trims the hour.

Company switching, verified live 2026-08-01
--------------------------------------------
* ``GET /change?c=<companyId>&returnUrl=%2F`` switches the session. 302, then
  the portal renders as that company. **No browser is needed** -- it works on
  the same cookie jar the login produced.
* Every authenticated page carries ``<body data-current-company-id="61343">``.
  That attribute is the confirmation; a switch is never assumed to have worked.
* The switcher menu lists the companies this login may switch to, as
  ``href="/change?c=254467&returnUrl=%2F"``. ``companies`` on the CLI prints them.
* Same 30-day window, same query, session switched:
      61343 Roadside Towing and Recovery Inc -> 2,984 records, 5 providers
      254467 Auto Lyft USA, Inc              ->   201 records, 100% "Honk"
  Every row's ``companyId`` matched the session in both cases.

Design rules this module obeys
------------------------------
* Hard constraint #1 -- credentials come from TOWBOOK_USER / TOWBOOK_PASS (or
  TOWBOOK_ACCOUNTS) via ``acquisition.credentials_for`` only. The password is
  never logged, never archived, never written to the run envelope.
* Hard constraint #2 -- every URL, header and marker string is read from
  ``config/selectors.yaml`` (section ``api``) with a code fallback, so the
  endpoint moving is a YAML edit.
* Hard constraint #4 -- the archive is content-addressed by window, and
  ingestion upserts on ``callRequestId``, so re-running a window is a no-op
  against the database.
* Hard constraint #5 -- total failure emits ``pipeline_failure`` before it
  raises. A short read (max-pages guard hit) also emits, because a truncated
  pull that looked successful is exactly the silent wrong number this system
  must never produce.

Environment variables (all optional except the credentials)
-----------------------------------------------------------
    TOWBOOK_USER, TOWBOOK_PASS      required
    TOWBOOK_ACCOUNTS                optional JSON list for multi-account
    TOWBOOK_BASE_URL                default https://app.towbook.com
    TOWBOOK_API_PAGE_SIZE           default 1000, hard-capped at 1000
    TOWBOOK_API_MAX_PAGES           default 200 (200,000 rows) -- runaway guard
    TOWBOOK_API_TIMEOUT_S           per-request timeout, default 120
    TOWBOOK_API_USER_AGENT          override the browser-like UA
    TOWBOOK_RETRY_BACKOFF_S         default "30,120,300" (shared with the UI path)
    TZ                              default America/Detroit
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ..core import companies as _companies
from ..core import events
from ..core.config_loader import CONFIG, ConfigError
from ..core.logging_setup import get_logger, redact
from ..core.paths import ENV_FILE, ensure_dirs, raw_path_for

# The UI path is the source of truth for credentials, backoff, timestamps and
# the named login-outcome vocabulary. Reimplementing any of it here would let
# the two paths drift apart, and they must fail in the same language.
from .acquisition import (
    DEFAULT_ACCOUNT_ID,
    LOGIN_OUTCOMES,
    OUTCOME_BAD_CREDENTIALS,
    OUTCOME_CAPTCHA_OR_BOT_WALL,
    OUTCOME_ENVIRONMENT_ERROR,
    OUTCOME_MFA_REQUIRED,
    OUTCOME_NETWORK_ERROR,
    OUTCOME_OK,
    OUTCOME_UNKNOWN_POST_LOGIN,
    AcquisitionError,
    BotWallDetected,
    CredentialsMissing,
    LoginFailed,
    MfaRequired,
    _as_datetime,
    _env_int,
    _env_str,
    _retry_backoff_seconds,
    _utc_stamp,
    base_url,
    credentials_for,
)

if TYPE_CHECKING:  # pragma: no cover - import cost only
    import httpx

__all__ = [
    "acquire_api",
    "login_api",
    "login_check_api",
    "current_company_id",
    "select_company",
    "MAX_PAGE_SIZE",
    "ApiError",
    "ApiUnavailable",
    "CompanyMismatch",
    "LOGIN_OUTCOMES",
]

logger = get_logger(__name__)


# ==========================================================================
# Hard limits
# ==========================================================================

#: VERIFIED 2026-07-28: ``pageSize=1000`` returns in ~1.5 s. ``pageSize=2000``
#: returns **HTTP 500 after a ~30 second server-side timeout** -- the endpoint
#: does not merely clamp the value, it fails. The cap therefore lives in code,
#: not only in a comment in schedule.yaml, so no configuration edit and no
#: caller can ask for a page size that is guaranteed to break the run.
MAX_PAGE_SIZE: int = 1000

#: Runaway guard. 200 pages x 1000 rows = 200,000 records, comfortably above
#: the 156,442 rows this account holds in total, so a legitimate full backfill
#: still fits while a pagination bug cannot loop forever.
DEFAULT_MAX_PAGES: int = 200

#: A browser-like User-Agent. Towbook served no bot wall on 2026-07-28, but a
#: python-httpx UA is the first thing any future WAF rule would match on.
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ApiError(AcquisitionError):
    """The callrequests endpoint did not return a usable payload."""


class ApiUnavailable(AcquisitionError):
    """httpx is not installed in this interpreter."""


class CompanyMismatch(AcquisitionError):
    """The session is not on the company this run is about.

    Raised when the switch could not be made, could not be confirmed, or when
    the rows that came back carry a different ``companyId`` than the one asked
    for. Every one of those means the same thing: the records in hand belong to
    a different tenant than the ``company_id`` they are about to be stamped
    with. Filing them anyway would double-count one company's jobs under
    another's name, permanently and with no error -- which is precisely the
    silently-wrong number this system exists to prevent. So it is fatal, and it
    is deliberately NOT retried.
    """


# ==========================================================================
# Configuration -- selectors.yaml section `api`, with code fallbacks
# ==========================================================================

#: Used when config/selectors.yaml has no ``api`` section at all (a minimal
#: test config, or a config file from before this module existed). Every value
#: is the live-verified one, so the module works with or without the YAML.
_API_DEFAULTS: dict[str, list[str]] = {
    "login_url": ["/Security/Login"],
    "callrequests_url": ["/api/digitaldispatch/callrequests"],
    "request_log_referer": ["/requestLog/"],
    # --- the company switcher (the book icon, top right of the portal) ------
    # VERIFIED 2026-08-01 against the live account: the switcher's menu item is
    # a plain link, `GET /change?c=<towbook company id>&returnUrl=%2F`, and it
    # works on a cookie session with no browser at all.
    "switch_company_url": ["/change"],
    "switch_company_param": ["c"],
    # Every authenticated page carries the active company on the <body> tag:
    #     <body data-current-company-id="61343">
    # That is what makes the switch VERIFIABLE rather than merely requested.
    "current_company_attribute": ["data-current-company-id"],
    "home_url": ["/Index", "/"],
    "antiforgery_field": ["RequestVerificationToken", "__RequestVerificationToken"],
    "submit_field": ["bSignIn"],
    "submit_value": ["Log in"],
    "username_field": ["Username"],
    "password_field": ["Password"],
    "session_cookie": [".xtl"],
    "records_count_header": ["X-Records-Count", "x-records-count"],
    "logged_out_url_marker": ["/security/login", "returnurl="],
    "login_failure_markers": [
        "field-validation-error",
        "username/password you specified is invalid",
        "passwords are case-sensitive",
        "invalid username or password",
        "invalid login attempt",
    ],
    "login_locked_markers": [
        "account has been locked",
        "account is locked",
        "account has been disabled",
    ],
    "mfa_markers": [
        "verification code",
        "two-factor",
        "two factor",
        "authenticator app",
        "one-time code",
        "enter the code",
    ],
    "bot_wall_markers": [
        "are you a robot",
        "unusual traffic",
        "verify you are human",
        "checking your browser",
        "cloudflare ray id",
        "request blocked",
    ],
}


def _api_config(key: str) -> list[str]:
    """Candidate list for ``selectors.yaml -> api.<key>``, or the code default.

    Hard constraint #2 in practice: the endpoint path, the antiforgery field
    name, the session cookie name and every failure marker are DATA. Towbook
    renaming any of them is a YAML edit.
    """
    try:
        values = CONFIG.selector_candidates("api", key)
    except (ConfigError, Exception):  # noqa: BLE001 - a missing section is normal
        values = []
    cleaned = [str(item).strip() for item in values if str(item).strip()]
    return cleaned or list(_API_DEFAULTS.get(key, []))


def _api_first(key: str) -> str:
    values = _api_config(key)
    return values[0] if values else ""


def _abs_url(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return f"{base_url()}/{path_or_url.lstrip('/')}"


def _page_size() -> int:
    """Requested page size, hard-capped at :data:`MAX_PAGE_SIZE`.

    The cap is applied here rather than trusted to the caller because asking
    for 2000 is not a slow request, it is an HTTP 500 after a 30 second stall.
    """
    wanted = _env_int("TOWBOOK_API_PAGE_SIZE", MAX_PAGE_SIZE)
    if wanted > MAX_PAGE_SIZE:
        logger.warning(
            "TOWBOOK_API_PAGE_SIZE=%d exceeds the verified maximum of %d "
            "(pageSize=2000 returns HTTP 500 after a ~30s server timeout); using %d",
            wanted,
            MAX_PAGE_SIZE,
            MAX_PAGE_SIZE,
        )
    return max(1, min(wanted, MAX_PAGE_SIZE))


def _max_pages() -> int:
    return max(1, _env_int("TOWBOOK_API_MAX_PAGES", DEFAULT_MAX_PAGES))


def _timeout_seconds() -> float:
    return float(max(5, _env_int("TOWBOOK_API_TIMEOUT_S", 120)))


def _user_agent() -> str:
    return _env_str("TOWBOOK_API_USER_AGENT", DEFAULT_USER_AGENT)


def _looks_logged_out(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker.lower() in lowered for marker in _api_config("logged_out_url_marker"))


def _hits(body: str, key: str) -> list[str]:
    lowered = body.lower()
    return [marker for marker in _api_config(key) if marker.lower() in lowered]


# ==========================================================================
# The httpx client
# ==========================================================================


def _import_httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - environment problem
        raise ApiUnavailable(
            "httpx is not installed in this interpreter.\n"
            "  Fix:  .venv\\Scripts\\python.exe -m pip install httpx\n"
            "  (or run:  .venv\\Scripts\\python.exe -m pip install -r requirements.txt)"
        ) from exc
    return httpx


def new_client() -> "httpx.Client":
    """A configured, unauthenticated client. Caller owns closing it."""
    httpx = _import_httpx()
    return httpx.Client(
        base_url=base_url(),
        follow_redirects=True,
        timeout=_timeout_seconds(),
        headers={
            "User-Agent": _user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _cookie_names(client: "httpx.Client") -> list[str]:
    """Cookie NAMES only. A cookie value is a live session; it never leaves here."""
    try:
        return sorted({str(cookie.name) for cookie in client.cookies.jar})
    except Exception:  # pragma: no cover - defensive
        return []


def _has_session_cookie(client: "httpx.Client") -> bool:
    wanted = {name.lower() for name in _api_config("session_cookie")}
    return any(name.lower() in wanted for name in _cookie_names(client))


# ==========================================================================
# Antiforgery token
# ==========================================================================

_INPUT_TAG = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")


def _tag_attributes(tag: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for match in _ATTR.finditer(tag):
        name = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attributes[name] = value
    return attributes


def extract_antiforgery_token(html: str) -> tuple[str, str]:
    """Return ``(field_name, token)`` from a login page, or ``("", "")``.

    Parsed attribute-by-attribute rather than with one fixed regex because
    ``value`` can appear either side of ``name`` in the rendered tag, and a
    positional regex that happens to work today is a silent breakage waiting
    for the next markup tweak.
    """
    wanted = [name.lower() for name in _api_config("antiforgery_field")]
    for tag in _INPUT_TAG.findall(html or ""):
        attributes = _tag_attributes(tag)
        name = attributes.get("name", "")
        if name.lower() in wanted and attributes.get("value"):
            return name, attributes["value"]
    return "", ""


# ==========================================================================
# Login
# ==========================================================================


class _Outcome:
    """A named login result. ``outcome`` is always one of LOGIN_OUTCOMES."""

    __slots__ = ("outcome", "message", "final_url", "details")

    def __init__(self, outcome: str, message: str, final_url: str = "", details: dict | None = None) -> None:
        self.outcome = outcome
        self.message = message
        self.final_url = final_url
        self.details: dict[str, Any] = details or {}

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_OK


def _classify_login(client: "httpx.Client", response: Any, token_present: bool) -> _Outcome:
    """Decide, by name, what the login POST produced."""
    final_url = str(response.url)
    body = response.text or ""
    details: dict[str, Any] = {
        "url_after_post": final_url,
        "http_status": response.status_code,
        "antiforgery_token_present": token_present,
        "cookies_seen": _cookie_names(client),
    }

    # 1. A bot wall can imitate every other symptom, so it is tested first.
    bot_hits = _hits(body, "bot_wall_markers")
    if bot_hits:
        details["bot_wall_markers"] = bot_hits
        return _Outcome(
            OUTCOME_CAPTCHA_OR_BOT_WALL,
            "A bot-challenge page was served instead of a session. No challenge existed "
            "when this endpoint was last verified, so this is new. Run "
            "'python -m towbook_agent login-check --source ui' to see it in a browser, "
            "then 'bootstrap-session' to solve it once by hand.",
            final_url,
            details,
        )

    # 2. Second factor.
    mfa_hits = _hits(body, "mfa_markers")
    if mfa_hits and _looks_logged_out(final_url) is False and not _has_session_cookie(client):
        details["mfa_markers"] = mfa_hits
        return _Outcome(
            OUTCOME_MFA_REQUIRED,
            "Towbook is asking for a second factor. The JSON path cannot complete one. "
            "Run 'python -m towbook_agent login-check --source ui' and then "
            "bootstrap-session to establish a browser session.",
            final_url,
            details,
        )

    # 3. VERIFIED success test: the .xtl cookie exists AND we are off the login
    #    page. Both, not either -- the cookie is issued alongside the redirect,
    #    and a login page that happens to set a cookie is still a failure.
    #    Deliberately NOT testing for .AspNetCore.Cookies: Towbook does not use
    #    it, and asserting on it fails a perfectly good login.
    has_cookie = _has_session_cookie(client)
    on_login_page = _looks_logged_out(final_url)
    details["session_cookie_present"] = has_cookie
    if has_cookie and not on_login_page:
        return _Outcome(
            OUTCOME_OK,
            f"Authenticated. Session cookie {_api_first('session_cookie')!r} was issued and "
            f"the response landed on {final_url}.",
            final_url,
            details,
        )

    # 4. VERIFIED failure test: still on /Security/Login with the portal's
    #    validation banner in the body.
    if on_login_page:
        failure_hits = _hits(body, "login_failure_markers")
        locked_hits = _hits(body, "login_locked_markers")
        details["login_failure_markers"] = failure_hits
        details["login_locked_markers"] = locked_hits
        if locked_hits:
            return _Outcome(
                OUTCOME_BAD_CREDENTIALS,
                "The Towbook account appears to be locked or disabled: the login page came "
                f"back carrying {locked_hits[0]!r}. Log in manually at "
                f"{_abs_url(_api_first('login_url'))} to clear it.",
                final_url,
                details,
            )
        if failure_hits:
            return _Outcome(
                OUTCOME_BAD_CREDENTIALS,
                "Towbook rejected the credentials: the POST came back on the login page "
                f"carrying {failure_hits[0]!r}. Check TOWBOOK_USER / TOWBOOK_PASS in "
                f"{ENV_FILE}.",
                final_url,
                details,
            )
        details["confidence"] = "low"
        return _Outcome(
            OUTCOME_BAD_CREDENTIALS,
            "The login form was posted and the response stayed on the login page, which "
            "means it was rejected -- but none of the known validation markers were in the "
            "body, so the page may have changed too. Check TOWBOOK_USER / TOWBOOK_PASS "
            "first, then reconcile api.login_failure_markers in config/selectors.yaml.",
            final_url,
            details,
        )

    # 5. Off the login page but no session cookie: the cookie is not sticking.
    return _Outcome(
        OUTCOME_UNKNOWN_POST_LOGIN,
        f"The login POST left the login page (landed on {final_url}) but no "
        f"{_api_first('session_cookie')!r} cookie was issued, so nothing downstream will "
        "authenticate. Check the system clock and any proxy that strips cookies. "
        f"Cookies received: {', '.join(details['cookies_seen']) or '(none)'}.",
        final_url,
        details,
    )


def _authenticate(client: "httpx.Client", account_id: str = DEFAULT_ACCOUNT_ID) -> _Outcome:
    """Perform the login round-trip and return a named outcome. Never raises for
    a credential problem -- that is the caller's decision to escalate."""
    httpx = _import_httpx()
    creds = credentials_for(account_id)
    login_url = _abs_url(_api_first("login_url"))

    try:
        page = client.get(login_url)
    except httpx.HTTPError as exc:
        return _Outcome(
            OUTCOME_NETWORK_ERROR,
            f"Could not reach {login_url}: {type(exc).__name__}: {exc}. Check connectivity, "
            "DNS, any corporate proxy, and that TOWBOOK_BASE_URL is right.",
            login_url,
            {"raw_error": str(exc)[:2000]},
        )

    field_name, token = extract_antiforgery_token(page.text or "")
    if not token:
        # Not fatal on its own -- post anyway and let the response say what
        # happened -- but it is a portal change worth shouting about.
        logger.warning(
            "no antiforgery token field found on %s; posting anyway, but this is a "
            "portal change worth investigating",
            login_url,
        )
        field_name = _api_first("antiforgery_field")

    form: dict[str, str] = {
        _api_first("username_field") or "Username": creds.username,
        _api_first("password_field") or "Password": creds.password,
        _api_first("submit_field") or "bSignIn": _api_first("submit_value") or "Log in",
    }
    if token:
        form[field_name] = token

    logger.info(
        "POST %s for user %s (password read from TOWBOOK_PASS, never logged; "
        "antiforgery token %s)",
        login_url,
        creds.masked_username,
        "present" if token else "MISSING",
    )

    try:
        response = client.post(
            login_url,
            data=form,
            headers={
                "Referer": login_url,
                "Origin": base_url(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except httpx.HTTPError as exc:
        return _Outcome(
            OUTCOME_NETWORK_ERROR,
            f"The login POST to {login_url} failed at the transport level: "
            f"{type(exc).__name__}: {exc}",
            login_url,
            {"raw_error": str(exc)[:2000]},
        )

    return _classify_login(client, response, bool(token))


def login_api(client: "httpx.Client", account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    """Authenticate ``client`` in place against Towbook.

    On success the client's cookie jar carries the ``.xtl`` session cookie and
    every later request on it is authenticated. Returns None.

    Raises :class:`CredentialsMissing`, :class:`MfaRequired`,
    :class:`BotWallDetected` or :class:`LoginFailed` -- each carrying one of the
    named outcomes in ``LOGIN_OUTCOMES``, so the caller can tell a transient
    network fault (retry) from a rejected password (do not retry).
    """
    outcome = _authenticate(client, account_id)
    if outcome.ok:
        logger.info("api login ok for account '%s'", account_id)
        return None
    if outcome.outcome == OUTCOME_MFA_REQUIRED:
        raise MfaRequired(outcome.message, final_url=outcome.final_url, details=outcome.details)
    if outcome.outcome == OUTCOME_CAPTCHA_OR_BOT_WALL:
        raise BotWallDetected(outcome.message, final_url=outcome.final_url, details=outcome.details)
    raise LoginFailed(
        outcome.outcome, outcome.message, final_url=outcome.final_url, details=outcome.details
    )


# ==========================================================================
# The active company -- the book icon, top right of the portal
#
# Towbook scopes /api/digitaldispatch/callrequests to whatever company the
# SESSION is currently switched to. There is no companyId parameter on the
# endpoint; asking for a date range returns that one company's offers and
# nothing else. A login lands on the user's home company and stays there.
#
# So an account with more than one company -- one login, several towing
# entities, switched with the book icon -- pulls only its home company unless
# something switches it. That is why HONK work assigned through Auto Lyft USA
# (companyId 254467) was absent from every report while Roadside (61343) was
# complete: not a parsing bug, an unasked question.
#
# VERIFIED LIVE 2026-08-01:
#     GET /change?c=254467&returnUrl=%2F   -> 302, session now on 254467
#     the same callrequests query then returns 201 records, 100% providerName
#     "Honk", every row companyId 254467.
# ==========================================================================

_CURRENT_COMPANY = re.compile(
    r"""data-current-company-id\s*=\s*["'](\d+)["']""", re.IGNORECASE
)


def _current_company_pattern() -> "re.Pattern[str]":
    """Regex for the <body> attribute naming the active company."""
    attribute = _api_first("current_company_attribute")
    if not attribute or attribute == "data-current-company-id":
        return _CURRENT_COMPANY
    return re.compile(rf"""{re.escape(attribute)}\s*=\s*["'](\d+)["']""", re.IGNORECASE)


def current_company_id(client: "httpx.Client") -> str | None:
    """The Towbook company id this session is currently on, or None.

    Read from the ``data-current-company-id`` attribute that every
    authenticated page carries on its ``<body>`` tag. None means the attribute
    was not found -- a portal change, or a page served while logged out --
    which is treated as "cannot confirm" rather than "wrong", because the two
    need very different responses.
    """
    pattern = _current_company_pattern()
    for path in _api_config("home_url"):
        try:
            response = client.get(_abs_url(path))
        except Exception as exc:  # noqa: BLE001 - try the next candidate URL
            logger.debug("could not read the active company from %s: %s", path, exc)
            continue
        if _looks_logged_out(str(response.url)):
            continue
        match = pattern.search(response.text or "")
        if match:
            return match.group(1)
    return None


def select_company(client: "httpx.Client", towbook_company_id: str | int | None) -> dict[str, Any]:
    """Switch the authenticated session onto ``towbook_company_id``.

    Returns a diagnostic dict recorded in the run envelope. Raises
    :class:`CompanyMismatch` if the switch did not take, or could not be
    confirmed to have taken.

    ``None`` / blank means the company has no ``towbook_company_id`` in
    companies.yaml. That is the single-company install, and the correct
    behaviour there is to leave the session exactly where the login put it --
    so this reports what it found and changes nothing.

    Confirmation is not optional. A ``/change`` that answers 200 while leaving
    the session where it was would hand the caller another company's rows to
    file under this company's name, and nothing downstream could tell.
    """
    result: dict[str, Any] = {
        "requested": str(towbook_company_id or "").strip() or None,
        "before": None,
        "after": None,
        "switched": False,
        "confirmed": False,
    }

    before = current_company_id(client)
    result["before"] = before

    wanted = result["requested"]
    if not wanted:
        # Nothing to switch to. Report the session's own company so the archive
        # still records which tenant the rows actually came from.
        logger.info(
            "no towbook_company_id configured; leaving the session on company %s",
            before or "(unknown)",
        )
        result["after"] = before
        result["confirmed"] = before is not None
        return result

    if before == wanted:
        logger.info("session is already on Towbook company %s; no switch needed", wanted)
        result["after"] = before
        result["confirmed"] = True
        return result

    url = _abs_url(_api_first("switch_company_url") or "/change")
    param = _api_first("switch_company_param") or "c"
    logger.info("switching session from company %s to %s", before or "(unknown)", wanted)

    try:
        response = client.get(url, params={param: wanted, "returnUrl": "/"})
    except Exception as exc:  # noqa: BLE001 - reported as a mismatch below
        raise CompanyMismatch(
            f"Could not switch the session to Towbook company {wanted}: "
            f"{type(exc).__name__}: {exc}. GET {url}?{param}={wanted} is what the portal's "
            "company switcher (the book icon, top right) does."
        ) from exc

    result["switch_http_status"] = response.status_code
    result["switch_final_url"] = str(response.url)
    result["switched"] = True

    after = current_company_id(client)
    result["after"] = after

    if after is None:
        raise CompanyMismatch(
            f"The session was asked to switch to Towbook company {wanted} (HTTP "
            f"{response.status_code}), but the active company could not be read back from the "
            f"portal, so the switch cannot be confirmed. This run is abandoned rather than "
            f"risk filing another company's jobs under this one. Reconcile "
            f"api.current_company_attribute in config/selectors.yaml -- the portal marks the "
            f"active company on the <body> tag."
        )

    if after != wanted:
        raise CompanyMismatch(
            f"The session was asked to switch to Towbook company {wanted} but is on {after}. "
            f"Either this login has no access to company {wanted} -- open the book icon in the "
            f"portal, top right, and check it is listed -- or towbook_company_id is wrong for "
            f"this company in config/companies.yaml."
        )

    result["confirmed"] = True
    logger.info("session confirmed on Towbook company %s", after)
    return result


def _verify_records_company(
    records: list[dict[str, Any]], towbook_company_id: str | None
) -> dict[str, Any]:
    """Check the rows really are the company we asked for.

    ``config/schema.yaml`` has declared ``account_id: [companyId]`` as the
    source of record for exactly this check since before there was a second
    company to run it against. This is that check.

    Belt and braces over :func:`select_company`: the switch is confirmed from
    the portal's own ``<body>`` attribute, and then the payload is confirmed
    against the rows themselves. A disagreement between the two is a hard stop,
    because the alternative is stamping one tenant's jobs with another's id.
    """
    seen = Counter(
        str(record.get("companyId")) for record in records if record.get("companyId") is not None
    )
    summary: dict[str, Any] = {
        "expected": towbook_company_id,
        "seen": dict(seen),
        "records_without_company_id": sum(1 for r in records if r.get("companyId") is None),
    }
    if not towbook_company_id or not seen:
        return summary

    foreign = {value: count for value, count in seen.items() if value != towbook_company_id}
    summary["foreign"] = foreign
    if foreign:
        detail = ", ".join(f"{value} ({count} row(s))" for value, count in sorted(foreign.items()))
        raise CompanyMismatch(
            f"The pull asked for Towbook company {towbook_company_id} and the session confirmed "
            f"it, but {sum(foreign.values())} of {len(records)} returned row(s) carry a different "
            f"companyId: {detail}. Nothing is ingested -- filing these would mix two companies' "
            f"jobs together. This means the endpoint's scoping has changed and "
            f"agents/acquisition_api.py needs a look before the next run."
        )
    return summary


def login_check_api(account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
    """Prove the credentials work over the JSON path. No browser, no scraping.

    Returns the same dict contract as ``acquisition.login_check`` so the CLI can
    print either one: ``ok``, ``stage`` (one of LOGIN_OUTCOMES), ``message``,
    ``final_url``, ``cookies_seen`` (NAMES only), ``details``.

    On success it also probes the callrequests endpoint for a single row, so a
    pass means "the pipeline can actually fetch data", not merely "a cookie was
    issued".
    """
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": False,
        "stage": OUTCOME_UNKNOWN_POST_LOGIN,
        "outcome": OUTCOME_UNKNOWN_POST_LOGIN,
        "message": "",
        "final_url": "",
        "cookies_seen": [],
        "account_id": account_id,
        "base_url": base_url(),
        "source": "api",
        "username_masked": "",
        "screenshot_path": "",  # no browser on this path; kept for a stable shape
        "details": {},
    }

    def finish() -> dict:
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        level = logger.info if result["ok"] else logger.error
        level(
            "login-check(api) [%s] account=%s -> %s | %s",
            "OK" if result["ok"] else "FAIL",
            account_id,
            result["stage"],
            result["message"],
        )
        return result

    try:
        creds = credentials_for(account_id)
        result["username_masked"] = creds.masked_username
    except CredentialsMissing as exc:
        result["stage"] = result["outcome"] = OUTCOME_BAD_CREDENTIALS
        result["message"] = str(exc)
        result["details"] = {"reason": "credentials_missing"}
        return finish()

    try:
        with new_client() as client:
            outcome = _authenticate(client, account_id)
            result["stage"] = result["outcome"] = outcome.outcome
            result["message"] = outcome.message
            result["final_url"] = outcome.final_url
            result["ok"] = outcome.ok
            result["details"] = dict(outcome.details)
            result["cookies_seen"] = _cookie_names(client)

            if outcome.ok:
                # A cookie is not data, and data from the wrong company is
                # worse than none. Prove the session can reach THIS company
                # before proving the endpoint answers.
                company = _companies.resolve_company(account_id)
                result["details"]["company"] = {
                    "id": company.id,
                    "name": company.name,
                    "towbook_company_id": company.towbook_company_id,
                }
                try:
                    result["details"]["company_selection"] = select_company(
                        client, company.towbook_company_id
                    )
                except CompanyMismatch as exc:
                    result["ok"] = False
                    result["stage"] = result["outcome"] = OUTCOME_UNKNOWN_POST_LOGIN
                    result["message"] = str(exc)
                    result["details"]["company_selection"] = {"ok": False, "error": str(exc)[:500]}
                    return finish()

                today = datetime.now().date()
                try:
                    records, total, _pages = _fetch_page(client, today, today, page=1, page_size=1)
                    result["details"]["callrequests_probe"] = {
                        "ok": True,
                        "records_returned": len(records),
                        "x_records_count": total,
                        "companies_seen": sorted(
                            {str(r.get("companyId")) for r in records if r.get("companyId")}
                        ),
                    }
                except Exception as exc:  # noqa: BLE001 - the probe is diagnostic
                    result["ok"] = False
                    result["stage"] = result["outcome"] = OUTCOME_UNKNOWN_POST_LOGIN
                    result["message"] = (
                        "Login succeeded and the session cookie was issued, but "
                        f"{_abs_url(_api_first('callrequests_url'))} did not return usable "
                        f"JSON: {type(exc).__name__}: {exc}. The endpoint may have moved -- "
                        "reconcile api.callrequests_url in config/selectors.yaml, or fall "
                        "back with --source ui."
                    )
                    result["details"]["callrequests_probe"] = {"ok": False, "error": str(exc)[:500]}
    except ApiUnavailable as exc:
        result["stage"] = result["outcome"] = OUTCOME_ENVIRONMENT_ERROR
        result["message"] = str(exc)
    except LoginFailed as exc:
        result["stage"] = result["outcome"] = exc.outcome
        result["message"] = str(exc)
        result["final_url"] = exc.final_url
        result["details"] = exc.details
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never crash
        result["stage"] = result["outcome"] = OUTCOME_NETWORK_ERROR
        result["message"] = f"{type(exc).__name__}: {exc}"

    return finish()


# ==========================================================================
# Fetching
# ==========================================================================


def _elapsed_seconds(response: Any) -> float | None:
    """Round-trip time for one page, or None.

    ``httpx.Response.elapsed`` raises RuntimeError until the response has been
    read or closed, and is absent entirely on some transports. This is a
    diagnostic recorded in the archive -- it must never be the thing that fails
    an acquisition.
    """
    try:
        return round(response.elapsed.total_seconds(), 3)
    except (RuntimeError, AttributeError, TypeError):
        return None


def _records_count(response: Any) -> int | None:
    for header in _api_config("records_count_header"):
        raw = response.headers.get(header)
        if raw is None:
            continue
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            logger.warning("%s header was %r, which is not a number", header, raw)
    return None


def _fetch_page(
    client: "httpx.Client",
    start_day: date,
    end_day: date,
    *,
    page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int | None, Any]:
    """Fetch one page. Returns ``(records, x_records_count, response)``.

    Raises :class:`ApiError` when the endpoint answers with anything other than
    a JSON array -- which is how an expired session shows up here: the redirect
    to /Security/Login is followed and an HTML page arrives with HTTP 200.
    """
    httpx = _import_httpx()
    url = _abs_url(_api_first("callrequests_url"))
    params = {
        "extended": "true",
        "page": page,
        # Capped again at the call site: the cap is the one thing in this module
        # that must hold no matter which path reached it.
        "pageSize": min(page_size, MAX_PAGE_SIZE),
        # VERIFIED: both YYYY-MM-DD and MM/DD/YYYY are accepted; ISO is used
        # because it is unambiguous.
        "startDate": start_day.isoformat(),
        "endDate": end_day.isoformat(),
    }
    headers = {
        "Referer": _abs_url(_api_first("request_log_referer")),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    try:
        response = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ApiError(f"GET {url} page={page} failed: {type(exc).__name__}: {exc}") from exc

    if response.status_code >= 400:
        raise ApiError(
            f"GET {url} page={page} pageSize={params['pageSize']} returned HTTP "
            f"{response.status_code}. "
            + (
                "pageSize above 1000 is known to return 500 after a ~30s timeout."
                if int(params["pageSize"]) > MAX_PAGE_SIZE
                else "Body: " + (response.text or "")[:300]
            )
        )

    if _looks_logged_out(str(response.url)):
        raise ApiError(
            f"GET {url} page={page} was redirected to the login page -- the session "
            "expired mid-pull. The retry will authenticate again."
        )

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        snippet = (response.text or "")[:200].replace("\n", " ")
        raise ApiError(
            f"GET {url} page={page} did not return JSON ({exc}). "
            f"Content-Type was {response.headers.get('content-type')!r}. "
            f"First 200 bytes: {snippet!r}. An HTML body here means the session was lost."
        ) from exc

    if not isinstance(payload, list):
        raise ApiError(
            f"GET {url} page={page} returned a {type(payload).__name__}, expected a JSON "
            "array of call requests. The endpoint's shape has changed."
        )

    records = [item for item in payload if isinstance(item, dict)]
    if len(records) != len(payload):
        logger.warning(
            "page %d contained %d non-object entries, which were dropped",
            page,
            len(payload) - len(records),
        )
    return records, _records_count(response), response


def _fetch_all(
    client: "httpx.Client",
    start_day: date,
    end_day: date,
    page_size: int,
) -> dict[str, Any]:
    """Page the endpoint until it is exhausted. Returns the archive envelope body.

    Stops on any of: an empty page, ``collected >= X-Records-Count``, a short
    page, or the max-pages guard. Duplicate ``callRequestId`` values across
    pages are counted -- verified zero over 3,079 rows, and a non-zero count
    would mean the endpoint's ordering is not stable under our paging.
    """
    max_pages = _max_pages()
    pages: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    total: int | None = None
    truncated_reason = ""

    for page in range(1, max_pages + 1):
        batch, count_header, response = _fetch_page(
            client, start_day, end_day, page=page, page_size=page_size
        )
        if count_header is not None:
            total = count_header

        if not batch:
            logger.info("page %d returned an empty array; pagination complete", page)
            break

        for item in batch:
            key = str(item.get("callRequestId", ""))
            if key and key in seen:
                duplicates += 1
            elif key:
                seen.add(key)

        pages.append(
            {
                "page": page,
                "page_size": min(page_size, MAX_PAGE_SIZE),
                "count": len(batch),
                "x_records_count": count_header,
                "elapsed_seconds": _elapsed_seconds(response),
                # Verbatim. Nothing here is normalised, renamed or dropped --
                # the archive has to be able to answer a question we have not
                # thought to ask yet.
                "records": batch,
            }
        )
        records.extend(batch)
        logger.info(
            "page %d: %d record(s), running total %d%s",
            page,
            len(batch),
            len(records),
            f" of {total}" if total is not None else "",
        )

        if total is not None and len(records) >= total:
            break
        if len(batch) < min(page_size, MAX_PAGE_SIZE):
            # A short page from an endpoint that fills every page means the end.
            break
        if page == max_pages:
            truncated_reason = (
                f"the max-pages guard ({max_pages}) was reached with "
                f"{len(records)} record(s) collected"
                + (f" of {total} reported" if total is not None else "")
            )

    if total is not None and len(records) < total and not truncated_reason:
        truncated_reason = (
            f"the endpoint reported {total} record(s) but pagination yielded {len(records)}"
        )

    return {
        "records": records,
        "pages": pages,
        "x_records_count": total,
        "duplicate_ids": duplicates,
        "distinct_ids": len(seen),
        "truncated_reason": truncated_reason,
    }


# ==========================================================================
# Archive
# ==========================================================================


def _archive_json(document: dict[str, Any], anchor: datetime, run_id: str) -> Path:
    """Write the run envelope to raw/YYYY/MM/DD/run_<id>.json. Never deletes.

    Mirrors ``acquisition._archive``: the YYYY/MM/DD directory is the *window
    start* date, not the download date, so every pull covering a given day sits
    together and a backfill lands beside the original.

    Unlike the UI path there is no separate ``.meta.json`` sidecar -- the
    payload is already JSON, so the run metadata lives in the same document as
    the records it describes and can never drift away from them.
    """
    target = raw_path_for(anchor, f"run_{run_id}.json")
    counter = 1
    while target.exists():
        target = target.with_name(f"run_{run_id}_{counter}.json")
        counter += 1
    target.write_text(
        redact(json.dumps(document, indent=2, default=str, ensure_ascii=False)),
        encoding="utf-8",
    )
    logger.info("archived raw API payload -> %s (%d bytes)", target, target.stat().st_size)
    return target


def _query_days(start: datetime, end: datetime) -> tuple[date, date]:
    """Convert a half-open local window into the inclusive calendar days to ask for.

    VERIFIED: ``endDate`` is inclusive of that calendar day and there is no
    time-of-day filter. A daily window of ``[07-26 00:00, 07-27 00:00)`` must
    therefore ask for ``07-26 .. 07-26``, and an hourly window of
    ``[14:00, 15:00)`` for that day .. that day. Subtracting one microsecond
    from the exclusive end gets both right without a special case.
    """
    start_day = start.date()
    end_day = (end - timedelta(microseconds=1)).date()
    if end_day < start_day:
        end_day = start_day
    return start_day, end_day


# ==========================================================================
# acquire_api -- the production path
# ==========================================================================


def acquire_api(
    start_date: datetime | date | str,
    end_date: datetime | date | str,
    account_id: str = DEFAULT_ACCOUNT_ID,
    *,
    company_id: str | None = None,
    run_id: str | None = None,
    page_size: int | None = None,
) -> Path:
    """Authenticate, switch to the company, page the endpoint, archive, return the path.

    ``company_id`` names a company in ``config/companies.yaml`` and is the
    current spelling; ``account_id`` is the older name for the same thing and
    is still accepted positionally, exactly as ``ingestion.ingest`` does it.
    Both matter here: ``scheduler._call`` drops keyword arguments a callee does
    not declare, so before this parameter existed the scheduler's
    ``company_id=`` was silently discarded and every run -- whichever company
    it claimed to be for -- pulled whatever company the login happened to land
    on.

    THE SESSION IS SWITCHED TO THE COMPANY BEFORE ANYTHING IS FETCHED, and the
    switch is confirmed twice: once from the portal's own active-company
    attribute, and once against the ``companyId`` on the returned rows. A
    failure of either is fatal and is not retried -- see :class:`CompanyMismatch`.

    ``start_date`` / ``end_date`` accept a datetime, a date or an ISO string.
    ``end_date`` is treated as the **exclusive** end of a half-open window --
    the same convention ``core.scheduler`` uses -- and converted to the
    inclusive ``endDate`` the endpoint expects.

    Retries the whole pull on failure with 30s / 2m / 5m of backoff
    (TOWBOOK_RETRY_BACKOFF_S). A rejected password or a missing dependency is
    not retried, because it will fail identically forever. On total failure it
    emits ``pipeline_failure`` and raises -- silence is never treated as
    success.

    Idempotent by construction: two pulls of the same window write two archive
    files (nothing under raw/ is ever deleted or overwritten) but carry the same
    ``callRequestId`` values, and ingestion upserts on that key, so the second
    ingest inserts nothing.
    """
    ensure_dirs()
    start = _as_datetime(start_date, "start_date")
    end = _as_datetime(end_date, "end_date")
    if end < start:
        raise ValueError(f"end_date ({end.isoformat()}) is before start_date ({start.isoformat()})")

    # `company_id` wins when given; `account_id` is the legacy positional name.
    tenant = company_id if company_id is not None else account_id
    company = _companies.resolve_company(tenant)
    # The merged scope has no login and no Towbook company to switch to -- it
    # is the sum of the others, computed at read time. Pulling "into" it would
    # archive a payload attributed to no company at all.
    _companies.ensure_writable(company.id)
    tenant = company.id
    towbook_company_id = company.towbook_company_id

    start_day, end_day = _query_days(start, end)
    size = _page_size() if page_size is None else max(1, min(int(page_size), MAX_PAGE_SIZE))
    run_identifier = run_id or _utc_stamp()
    backoff = _retry_backoff_seconds()
    attempts = len(backoff) + 1
    failures: list[str] = []

    logger.info(
        "acquire_api: company=%s (towbook %s) window=%s..%s -> query %s..%s pageSize=%d "
        "run_id=%s attempts=%d",
        tenant,
        towbook_company_id or "(not set -- session default)",
        start.isoformat(),
        end.isoformat(),
        start_day.isoformat(),
        end_day.isoformat(),
        size,
        run_identifier,
        attempts,
    )

    if towbook_company_id is None and _companies.is_multi_company():
        # With one company, "wherever the login lands" is right by definition.
        # With several it is a coin toss, and the loser is a tenant whose report
        # quietly fills with another tenant's jobs.
        logger.warning(
            "company %r has no towbook_company_id in config/companies.yaml, but this install "
            "reports on several companies. The session cannot be switched, so this run will "
            "pull whichever company the login lands on. Set towbook_company_id to remove the "
            "ambiguity.",
            tenant,
        )

    for attempt in range(1, attempts + 1):
        started_at = datetime.now(timezone.utc)
        try:
            with new_client() as client:
                login_api(client, tenant)
                # The switch happens BEFORE the first fetch, and a failure to
                # confirm it aborts the run: rows pulled from the wrong company
                # are worse than no rows at all.
                selection = select_company(client, towbook_company_id)
                harvest = _fetch_all(client, start_day, end_day, size)
                company_check = _verify_records_company(harvest["records"], towbook_company_id)
                cookies = _cookie_names(client)

            document: dict[str, Any] = {
                "schema": "towbook.callrequests.v1",
                "source": "api",
                "endpoint": _abs_url(_api_first("callrequests_url")),
                "run_id": run_identifier,
                "company_id": tenant,
                # Retained under its old name too: every archived payload
                # written before this key was renamed carries `account_id`, and
                # a reader of raw/ should not have to know when that happened.
                "account_id": tenant,
                "towbook_company_id": towbook_company_id,
                "company_selection": selection,
                "company_verification": company_check,
                "attempt": attempt,
                "acquired_at_utc": started_at.isoformat(timespec="seconds"),
                "finished_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "window_start_local": start.isoformat(),
                "window_end_local": end.isoformat(),
                "query_start_date": start_day.isoformat(),
                "query_end_date": end_day.isoformat(),
                "timezone": _env_str("TZ", "America/Detroit"),
                "page_size": size,
                "page_count": len(harvest["pages"]),
                "record_count": len(harvest["records"]),
                "distinct_ids": harvest["distinct_ids"],
                "duplicate_ids": harvest["duplicate_ids"],
                "x_records_count": harvest["x_records_count"],
                "cookies_seen": cookies,
                "base_url": base_url(),
                "status": "ok" if not harvest["truncated_reason"] else "truncated",
                # `pages` holds the verbatim payloads; ingestion flattens them.
                "pages": harvest["pages"],
            }

            archived = _archive_json(document, start, run_identifier)

            if harvest["duplicate_ids"]:
                logger.warning(
                    "%d duplicate callRequestId value(s) across pages -- pagination may not "
                    "be stable for this window; ingestion will upsert them, so the count is "
                    "still right, but this is worth knowing",
                    harvest["duplicate_ids"],
                )

            if harvest["truncated_reason"]:
                # Partial data is archived (partial beats none, and ingestion
                # upserts so a later wider run repairs it) but a human MUST be
                # told: a short pull that reported success is precisely the
                # silently-wrong number this system exists to prevent.
                events.emit_event(
                    "pipeline_failure",
                    {
                        "stage": "acquisition",
                        "severity": "high",
                        "source": "api",
                        "company_id": tenant,
                        "account_id": tenant,
                        "run_id": run_identifier,
                        "window_start": start.isoformat(),
                        "window_end": end.isoformat(),
                        "row_count": len(harvest["records"]),
                        "x_records_count": harvest["x_records_count"],
                        "archived_path": str(archived),
                        "error": f"TRUNCATED PULL: {harvest['truncated_reason']}.",
                        "next_step": (
                            "Raise TOWBOOK_API_MAX_PAGES, or narrow the window and re-run -- "
                            "ingestion upserts on callRequestId, so a repeat pull repairs it."
                        ),
                    },
                )

            logger.info(
                "acquire_api OK: %d record(s) (%d distinct) over %d page(s) for company %s "
                "(towbook %s) -> %s",
                len(harvest["records"]),
                harvest["distinct_ids"],
                len(harvest["pages"]),
                tenant,
                selection.get("after") or "(unknown)",
                archived,
            )
            return archived

        except Exception as exc:  # noqa: BLE001 - every failure is retried, then alerted
            label = f"{type(exc).__name__}: {exc}"
            failures.append(f"attempt {attempt}: {label}")
            logger.error("acquire_api attempt %d/%d failed -- %s", attempt, attempts, label)

            # These fail identically forever; retrying only delays the alert.
            if isinstance(exc, (CredentialsMissing, ApiUnavailable, ValueError)):
                logger.error("this failure is not transient; not retrying")
                break
            # A company mismatch is a configuration or portal-behaviour fault,
            # never a blip. Retrying it would just ask the wrong question three
            # more times, and the one outcome that must never happen is a
            # retry that "succeeds" by pulling another tenant's rows.
            if isinstance(exc, CompanyMismatch):
                logger.error(
                    "the session is not on the company this run is about; not retrying, and "
                    "nothing will be ingested"
                )
                break
            if isinstance(exc, LoginFailed) and exc.outcome in {
                OUTCOME_BAD_CREDENTIALS,
                OUTCOME_MFA_REQUIRED,
                OUTCOME_CAPTCHA_OR_BOT_WALL,
            }:
                logger.error("this failure needs a human; not retrying")
                break

            if attempt <= len(backoff):
                delay = backoff[attempt - 1]
                logger.info("backing off %ds before attempt %d", delay, attempt + 1)
                if delay:
                    time.sleep(delay)

    summary = (
        f"Towbook API acquisition failed for company '{tenant}' (towbook "
        f"{towbook_company_id or 'not set'}), window "
        f"{start.isoformat()} .. {end.isoformat()} (run {run_identifier}) after "
        f"{len(failures)} attempt(s).\n  " + "\n  ".join(failures)
    )
    events.emit_event(
        "pipeline_failure",
        {
            "stage": "acquisition",
            "severity": "high",
            "source": "api",
            "company_id": tenant,
            "account_id": tenant,
            "towbook_company_id": towbook_company_id,
            "run_id": run_identifier,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "page_size": size,
            "attempts": len(failures),
            "error": failures[-1] if failures else "unknown",
            "all_errors": failures,
            "next_step": (
                "python -m towbook_agent login-check      (proves the JSON path)\n"
                "then, if the endpoint itself has moved:  --source ui falls back to the "
                "Playwright export."
            ),
        },
    )
    raise AcquisitionError(summary)


# ==========================================================================
# Manual invocation:  python -m towbook_agent.agents.acquisition_api ...
# ==========================================================================


def _cli(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator tool
    import argparse

    from .. import load_env
    from ..core.logging_setup import setup_logging

    parser = argparse.ArgumentParser(
        prog="python -m towbook_agent.agents.acquisition_api",
        description="Towbook JSON API acquisition (the primary source).",
    )
    parser.add_argument("command", choices=("login-check", "acquire", "companies"))
    parser.add_argument("--start", help="window start, YYYY-MM-DD or ISO datetime")
    parser.add_argument("--end", help="window end (EXCLUSIVE), YYYY-MM-DD or ISO datetime")
    parser.add_argument(
        "--company",
        "--account",
        dest="company",
        default=DEFAULT_ACCOUNT_ID,
        help="company id from config/companies.yaml (--account is the old spelling)",
    )
    parser.add_argument("--page-size", type=int, default=None)
    args = parser.parse_args(argv)

    load_env()
    setup_logging(force=True)

    if args.command == "companies":
        # Which companies does this login actually reach? Answers the question
        # "is the roster right" without pulling a single row.
        with new_client() as client:
            login_api(client, args.company)
            active = current_company_id(client)
            html = client.get(_abs_url(_api_first("home_url"))).text or ""
            offered = sorted(set(re.findall(r"/change\?c=(\d+)", html)))
        print(
            json.dumps(
                {
                    "session_company": active,
                    "switchable_to": offered,
                    "roster": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "towbook_company_id": c.towbook_company_id,
                            "enabled": c.enabled,
                        }
                        for c in _companies.all_companies()
                    ],
                },
                indent=2,
            )
        )
        return 0

    if args.command == "login-check":
        result = login_check_api(args.company)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["ok"] else 1

    if not args.start or not args.end:
        parser.error("acquire needs --start and --end")
    path = acquire_api(args.start, args.end, company_id=args.company, page_size=args.page_size)
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_cli(sys.argv[1:]))
