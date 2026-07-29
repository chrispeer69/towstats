"""Acquisition agent -- drives the Towbook portal with Playwright.

This is the load-bearing file of the whole system. If authentication against
app.towbook.com does not work, nothing downstream has any data to work on, so
this module is written to make success or failure *obvious and self-diagnosing*
rather than to be clever.

Three public entry points, plus one manual helper:

    acquire(start, end, page_size, account_id="default") -> Path
        Full clickpath. Authenticate -> Dispatching -> Request Log ->
        Digital Requests tab -> page size -> date range -> run -> Export to
        Excel -> capture the file -> archive it under raw/YYYY/MM/DD/.

    login_check(account_id="default") -> dict
        Authenticates and nothing else. Never scrapes. Returns a dict whose
        ``stage`` is one of a closed vocabulary of named outcomes so that a
        human (or the CLI) can tell *why* it failed at a glance.

    discover_selectors(account_id="default") -> Path
        Authenticates, walks to the Request Log, and dumps screenshots, HTML,
        a JSON inventory of every interactive element and a suggested
        selectors.yaml patch. This is how the operator reconciles the
        post-login DOM -- which is behind auth and has never been observed --
        without anybody guessing.

    bootstrap_session(account_id="default") -> Path
        Opens a HEADED browser, lets the operator log in by hand (covering MFA
        or SSO), then saves the Playwright storage_state so every later
        unattended run reuses the session.

Design rules this module obeys
------------------------------
* Hard constraint #1 -- credentials come from the environment only:
  TOWBOOK_USER / TOWBOOK_PASS, or the per-company pair a company's
  ``credentials_env`` prefix names (TOWBOOK_<PREFIX>_USER / _PASS), or the
  legacy TOWBOOK_ACCOUNTS JSON. They appear in no source file, not in
  config/companies.yaml -- which holds only the variable NAME -- no log line,
  no screenshot, no HTML dump and no discovery inventory. The username is
  masked even in diagnostics.
* Hard constraint #7 -- ZERO hardcoded selectors. Every selector, every URL
  path and even the keyword vocabularies used to classify a failed login come
  from config/selectors.yaml through ``resolve()``. When nothing matches,
  ``SelectorNotFound`` names the config key and every candidate it tried.
* Hard constraint #5 -- pipeline failure must alert. A total acquisition
  failure emits ``pipeline_failure`` before raising, and the CLI turns the
  raise into a non-zero exit.

Environment variables (all optional except the credentials)
-----------------------------------------------------------
    TOWBOOK_USER, TOWBOOK_PASS      required for a single-company install
    TOWBOOK_<PREFIX>_USER / _PASS   per company, named by companies.yaml
                                    -> credentials_env: <PREFIX>
    TOWBOOK_ACCOUNTS                legacy JSON list for multi-account
    TOWBOOK_BASE_URL                default https://app.towbook.com
    HEADLESS                        default true
    PLAYWRIGHT_SLOWMO               ms between actions, default 0
    TZ                              default America/Detroit
    TOWBOOK_BROWSER                 chromium | firefox | webkit  (default chromium)
    TOWBOOK_NAV_TIMEOUT_MS          default 45000
    TOWBOOK_STAGE_TIMEOUT_MS        per-stage hard budget, default 90000
    TOWBOOK_SELECTOR_PROBE_MS       per-candidate wait, default 3000
    TOWBOOK_DOWNLOAD_TIMEOUT_MS     default 180000
    TOWBOOK_RETRY_BACKOFF_S         default "30,120,300"  (3 retries)
    TOWBOOK_BOOTSTRAP_TIMEOUT_S     default 900
    TOWBOOK_REQUIRE_PAGE_SIZE       default false  (warn, do not abort)
    TOWBOOK_REQUIRE_DATE_RANGE      default true   (abort: wrong window is
                                                    worse than no window)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import unicodedata
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Sequence

import yaml

from ..core import events
from ..core import companies as _companies
from ..core.config_loader import CONFIG, ConfigError
from ..core.logging_setup import get_logger, redact
from ..core.paths import (
    DISCOVERY_DIR,
    ENV_FILE,
    STATE_DIR,
    ensure_dirs,
    raw_path_for,
    resolve_under_root,
)

if TYPE_CHECKING:  # pragma: no cover - import cost / optional dependency
    from playwright.sync_api import BrowserContext, Frame, Locator, Page

__all__ = [
    # entry points
    "acquire",
    "login_check",
    "discover_selectors",
    "bootstrap_session",
    # helpers other agents / the CLI may want
    "resolve",
    "resolve_optional",
    "Credentials",
    "credentials_for",
    "storage_state_path",
    "LOGIN_OUTCOMES",
    # exceptions
    "AcquisitionError",
    "CredentialsMissing",
    "SelectorNotFound",
    "LoginFailed",
    "MfaRequired",
    "BotWallDetected",
    "ExportFailed",
    "StageTimeout",
    "PlaywrightUnavailable",
]

logger = get_logger(__name__)


# ==========================================================================
# Named outcomes
# ==========================================================================

OUTCOME_OK = "ok"
OUTCOME_BAD_CREDENTIALS = "bad_credentials"
OUTCOME_MFA_REQUIRED = "mfa_required"
OUTCOME_CAPTCHA_OR_BOT_WALL = "captcha_or_bot_wall"
OUTCOME_SELECTOR_NOT_FOUND = "selector_not_found"
OUTCOME_NETWORK_ERROR = "network_error"
OUTCOME_UNKNOWN_POST_LOGIN = "unknown_post_login_state"
# ASSUMPTION: the seven names above are the vocabulary the spec requires.
# "environment_error" is an eighth, added because "Playwright or its browser is
# not installed" is neither a network fault nor a credential fault, and calling
# it either of those would send the operator down the wrong path. Every one of
# the seven required names is still produced.
OUTCOME_ENVIRONMENT_ERROR = "environment_error"

LOGIN_OUTCOMES: tuple[str, ...] = (
    OUTCOME_OK,
    OUTCOME_BAD_CREDENTIALS,
    OUTCOME_MFA_REQUIRED,
    OUTCOME_CAPTCHA_OR_BOT_WALL,
    OUTCOME_SELECTOR_NOT_FOUND,
    OUTCOME_NETWORK_ERROR,
    OUTCOME_UNKNOWN_POST_LOGIN,
    OUTCOME_ENVIRONMENT_ERROR,
)

DEFAULT_ACCOUNT_ID = "default"
DEFAULT_BASE_URL = "https://app.towbook.com"

#: Where diagnostics land. state/diagnostics/<prefix>_<utc timestamp>/
DIAGNOSTIC_DIR: Path = STATE_DIR / "diagnostics"
#: Per-run isolated download directories. NEVER the shared Downloads folder.
DOWNLOAD_DIR: Path = STATE_DIR / "downloads"

#: XLSX is a zip. Anything that does not start with this is not a spreadsheet.
_ZIP_MAGIC = b"PK\x03\x04"
#: Legacy BIFF (.xls). Accepted with a warning; openpyxl cannot read it.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"


# ==========================================================================
# Exceptions
# ==========================================================================


class AcquisitionError(RuntimeError):
    """Base class for every failure raised by this module."""


class PlaywrightUnavailable(AcquisitionError):
    """Playwright, or the browser binary it drives, is not installed."""


class CredentialsMissing(AcquisitionError):
    """TOWBOOK_USER / TOWBOOK_PASS are not set for the requested account."""


class StageTimeout(AcquisitionError):
    """A single stage blew its wall-clock budget."""


class SelectorNotFound(AcquisitionError):
    """No candidate in config/selectors.yaml resolved to a visible element.

    The message names the config key and lists every candidate that was tried,
    because that list is exactly what the operator has to fix -- and
    ``discover-selectors`` is exactly the tool that tells them what to put
    there instead.
    """

    def __init__(
        self,
        key: str,
        candidates: Sequence[str],
        *,
        frames_searched: int = 1,
        url: str = "",
        hint: str = "",
    ) -> None:
        self.key = key
        self.candidates = list(candidates)
        self.url = url
        listing = "\n".join(f"    {i + 1}. {c}" for i, c in enumerate(self.candidates))
        if not listing:
            listing = "    (the candidate list in selectors.yaml is EMPTY)"
        message = (
            f"selectors.yaml key '{key}' matched no visible element "
            f"(searched the main page and {frames_searched - 1} iframe(s)"
            + (f" at {url}" if url else "")
            + ").\n"
            f"  Candidates tried, in order:\n{listing}\n"
            f"  Fix: run  python -m towbook_agent discover-selectors  and copy the\n"
            f"  suggested candidate for '{key}' into config/selectors.yaml."
        )
        if hint:
            message += f"\n  Hint: {hint}"
        super().__init__(message)


class LoginFailed(AcquisitionError):
    """Authentication did not end in a logged-in state."""

    def __init__(self, outcome: str, message: str, *, final_url: str = "", details: dict | None = None) -> None:
        self.outcome = outcome
        self.final_url = final_url
        self.details = details or {}
        super().__init__(f"[{outcome}] {message}" + (f" (final url: {final_url})" if final_url else ""))


class MfaRequired(LoginFailed):
    """The portal asked for a second factor.

    Recovery is manual and one-off: run ``python -m towbook_agent
    bootstrap-session``, complete the challenge in the headed browser, and the
    saved storage_state carries every later unattended run.
    """

    def __init__(self, message: str, *, final_url: str = "", details: dict | None = None) -> None:
        super().__init__(OUTCOME_MFA_REQUIRED, message, final_url=final_url, details=details)


class BotWallDetected(LoginFailed):
    """A CAPTCHA or bot-challenge page stood between us and the session."""

    def __init__(self, message: str, *, final_url: str = "", details: dict | None = None) -> None:
        super().__init__(OUTCOME_CAPTCHA_OR_BOT_WALL, message, final_url=final_url, details=details)


class ExportFailed(AcquisitionError):
    """The Export -> Excel step produced nothing, or produced something that is not a workbook."""


# ==========================================================================
# Environment helpers
# ==========================================================================


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("%s=%r is not a number, using %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    logger.warning("%s=%r is not a boolean, using %s", name, raw, default)
    return default


def base_url() -> str:
    """The Towbook origin. Relative selector paths are resolved against it."""
    return _env_str("TOWBOOK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _headless(override: bool | None = None) -> bool:
    if override is not None:
        return override
    return _env_bool("HEADLESS", True)


def _slowmo_ms() -> int:
    return max(0, _env_int("PLAYWRIGHT_SLOWMO", 0))


def _nav_timeout_ms() -> int:
    return max(5_000, _env_int("TOWBOOK_NAV_TIMEOUT_MS", 45_000))


def _stage_timeout_ms() -> int:
    return max(5_000, _env_int("TOWBOOK_STAGE_TIMEOUT_MS", 90_000))


def _probe_timeout_ms() -> int:
    return max(250, _env_int("TOWBOOK_SELECTOR_PROBE_MS", 3_000))


def _download_timeout_ms() -> int:
    return max(10_000, _env_int("TOWBOOK_DOWNLOAD_TIMEOUT_MS", 180_000))


def _retry_backoff_seconds() -> tuple[int, ...]:
    """Sleep schedule between attempts.

    Default "30,120,300" means three retries after the first attempt, i.e. four
    attempts total, with 30s / 2m / 5m of backoff -- exactly the schedule the
    spec asks for. Shorten it in tests with TOWBOOK_RETRY_BACKOFF_S=0.
    """
    raw = _env_str("TOWBOOK_RETRY_BACKOFF_S", "30,120,300")
    values: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.append(max(0, int(float(chunk))))
        except ValueError:
            logger.warning("ignoring unparseable backoff entry %r", chunk)
    return tuple(values) if values else (30, 120, 300)


def _local_timezone() -> str:
    return _env_str("TZ", "America/Detroit")


def _utc_stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def _mask_user(username: str) -> str:
    """Mask a username for logs. Never show a credential in full."""
    if not username:
        return "<empty>"
    local, at, domain = username.partition("@")
    if len(local) <= 2:
        masked_local = local[:1] + "*"
    else:
        masked_local = f"{local[:2]}{'*' * max(1, len(local) - 3)}{local[-1]}"
    return f"{masked_local}{at}{domain}" if at else masked_local


def _slug(value: str, limit: int = 60) -> str:
    """Filesystem-safe fragment. Windows-safe: no reserved characters."""
    text = unicodedata.normalize("NFKD", str(value))
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return (text or "x")[:limit]


# ==========================================================================
# Credentials -- environment only, never a source file, never a log line
# ==========================================================================


@dataclass(frozen=True)
class Credentials:
    """A Towbook login. ``password`` is excluded from repr and never logged."""

    account_id: str
    username: str
    password: str = field(repr=False)

    @property
    def masked_username(self) -> str:
        return _mask_user(self.username)

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"Credentials(account_id={self.account_id!r}, username={self.masked_username!r}, password=***)"


def _credentials_error(account_id: str, detail: str) -> CredentialsMissing:
    return CredentialsMissing(
        f"No Towbook credentials for account '{account_id}': {detail}\n"
        f"  Fix: open {ENV_FILE} and set\n"
        f"      TOWBOOK_USER=<your Towbook login>\n"
        f"      TOWBOOK_PASS=<your Towbook password>\n"
        f"  (copy {ENV_FILE.with_name('.env.example')} if the file does not exist yet).\n"
        f"  Credentials are read from the environment ONLY -- never put them in a source file.\n"
        f"  Then re-run:  python -m towbook_agent login-check"
    )


def credentials_for(account_id: str = DEFAULT_ACCOUNT_ID) -> Credentials:
    """Read credentials for one company from the environment.

    Resolution order:

    1. **The company's own pair.** ``config/companies.yaml`` gives each company
       a ``credentials_env`` PREFIX -- never a login -- and that names
       ``TOWBOOK_<PREFIX>_USER`` / ``TOWBOOK_<PREFIX>_PASS``.
    2. ``TOWBOOK_ACCOUNTS``, the legacy JSON list, for installs that already
       used it.
    3. ``TOWBOOK_USER`` / ``TOWBOOK_PASS``, the single-company pair.

    **A company that declares a prefix never falls through to step 3.** Falling
    back would log into the WRONG company's Towbook account and file the
    resulting offers under this one -- a silent, permanent mixing of two
    tenants' data, produced by nothing worse than a typo in a variable name. A
    missing per-company variable is an error with the variable named in it.

    Raises CredentialsMissing with an actionable message naming the .env file.
    """
    company = _companies.get_company(account_id)
    if company is not None and company.credentials_env:
        username = _env_str(company.env_user)
        password = os.environ.get(company.env_pass) or ""
        missing = [
            name
            for name, value in ((company.env_user, username), (company.env_pass, password))
            if not value
        ]
        if missing:
            raise _credentials_error(
                account_id,
                f"{' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set. "
                f"config/companies.yaml gives {company.id!r} the credentials_env "
                f"prefix {company.credentials_env!r}, so its login must live in "
                f"those two variables. It deliberately does NOT fall back to "
                f"TOWBOOK_USER / TOWBOOK_PASS: that would sign in as a different "
                f"company and file its jobs under this one",
            )
        return Credentials(account_id=account_id, username=username, password=password)

    raw_accounts = _env_str("TOWBOOK_ACCOUNTS")
    if raw_accounts:
        try:
            parsed = json.loads(raw_accounts)
        except json.JSONDecodeError as exc:
            raise _credentials_error(
                account_id, f"TOWBOOK_ACCOUNTS is not valid JSON ({exc.msg} at position {exc.pos})"
            ) from None
        entries = parsed if isinstance(parsed, list) else [parsed]
        known: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("account_id") or entry.get("id") or DEFAULT_ACCOUNT_ID)
            known.append(entry_id)
            if entry_id != account_id:
                continue
            username = str(entry.get("user") or entry.get("username") or "").strip()
            password = str(entry.get("pass") or entry.get("password") or "")
            if not username or not password:
                raise _credentials_error(
                    account_id, "the TOWBOOK_ACCOUNTS entry is missing 'user' or 'pass'"
                )
            return Credentials(account_id=account_id, username=username, password=password)
        if account_id != DEFAULT_ACCOUNT_ID:
            raise _credentials_error(
                account_id,
                f"TOWBOOK_ACCOUNTS defines {known or ['(nothing)']} but not '{account_id}'",
            )
        # account_id == default and TOWBOOK_ACCOUNTS did not define it -> fall
        # through to the single-account variables.

    username = _env_str("TOWBOOK_USER")
    password = os.environ.get("TOWBOOK_PASS") or ""
    missing = [n for n, v in (("TOWBOOK_USER", username), ("TOWBOOK_PASS", password)) if not v]
    if missing:
        raise _credentials_error(account_id, f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set")
    return Credentials(account_id=account_id, username=username, password=password)


def storage_state_path(account_id: str = DEFAULT_ACCOUNT_ID) -> Path:
    """Where the Playwright storage_state for ``account_id`` lives.

    Template comes from selectors.yaml session.storage_state_file. The file
    holds live session cookies and is git-ignored via ``*.storage.json``.
    """
    try:
        candidates = CONFIG.selector_candidates("session", "storage_state_file")
    except ConfigError:
        candidates = []
    template = candidates[0] if candidates else "state/session/{account_id}.storage.json"
    return resolve_under_root(template.format(account_id=_slug(account_id)))


# ==========================================================================
# Selector resolution -- the only way this module is allowed to touch the DOM
# ==========================================================================


def _candidates(key: str, fmt: dict[str, Any] | None = None) -> list[str]:
    """Return the candidate list for a dotted ``section.key``."""
    section, _, name = key.partition(".")
    if not name:
        raise ValueError(f"selector key must be 'section.key', got {key!r}")
    values = CONFIG.selector_candidates(section, name)
    if fmt:
        formatted: list[str] = []
        for value in values:
            try:
                formatted.append(value.format(**fmt))
            except (KeyError, IndexError, ValueError):
                # A candidate with no placeholder, or a stray brace. Keep it
                # verbatim rather than dropping a potentially working selector.
                formatted.append(value)
        return formatted
    return values


def _config_words(key: str) -> list[str]:
    """A lowercased keyword vocabulary from selectors.yaml (not a DOM selector)."""
    try:
        return [w.strip().lower() for w in _candidates(key) if w and w.strip()]
    except (ConfigError, ValueError):
        return []


def _search_roots(page: "Page", include_frames: bool = True) -> list[Any]:
    """The page plus every child frame.

    Towbook's grid framework is unobserved; if the Request Log turns out to
    live in an iframe, a selector that "does not exist" on the page would be
    sitting right there in a frame. Searching both costs nothing.
    """
    roots: list[Any] = [page]
    if include_frames:
        with suppress(Exception):
            main = page.main_frame
            for frame in page.frames:
                if frame is not main:
                    roots.append(frame)
    return roots


def _match_now(root: Any, candidate: str, require_visible: bool) -> "Locator | None":
    """Non-blocking check: does this candidate already match right now?"""
    try:
        locator = root.locator(candidate).first
        if require_visible:
            return locator if locator.is_visible() else None
        return locator if locator.count() > 0 else None
    except Exception:
        # Invalid selector syntax, a detached frame, a navigation mid-probe --
        # all of them just mean "this candidate is not the one".
        return None


def _match_wait(root: Any, candidate: str, timeout_ms: int, require_visible: bool) -> "Locator | None":
    """Blocking check: wait up to ``timeout_ms`` for this candidate."""
    try:
        locator = root.locator(candidate).first
        locator.wait_for(state="visible" if require_visible else "attached", timeout=timeout_ms)
        return locator
    except Exception:
        return None


def resolve(
    page: "Page",
    key: str,
    *,
    timeout_ms: int | None = None,
    require_visible: bool = True,
    fmt: dict[str, Any] | None = None,
    include_frames: bool = True,
) -> "Locator":
    """Return the first candidate from selectors.yaml that resolves.

    ``key`` is dotted: ``"login.username_input"``, ``"request_log.export_menu"``.

    Two passes, deliberately:
      1. an instant, non-blocking sweep over every candidate on the page and on
         every iframe -- this is what makes a 12-candidate list cheap;
      2. if nothing matched, a blocking wait per candidate on the main page,
         which covers an element that simply has not rendered yet.

    Raises SelectorNotFound naming the key and every candidate tried.
    """
    candidates = _candidates(key, fmt)
    roots = _search_roots(page, include_frames)

    for root in roots:
        for candidate in candidates:
            found = _match_now(root, candidate, require_visible)
            if found is not None:
                logger.debug("resolved %s -> %s (immediate)", key, candidate)
                return found

    wait_ms = timeout_ms if timeout_ms is not None else _probe_timeout_ms()
    for candidate in candidates:
        found = _match_wait(page, candidate, wait_ms, require_visible)
        if found is not None:
            logger.debug("resolved %s -> %s (after wait)", key, candidate)
            return found

    url = ""
    with suppress(Exception):
        url = page.url
    raise SelectorNotFound(key, candidates, frames_searched=len(roots), url=url)


def resolve_optional(
    page: "Page",
    key: str,
    *,
    timeout_ms: int | None = None,
    require_visible: bool = True,
    fmt: dict[str, Any] | None = None,
    include_frames: bool = True,
) -> "Locator | None":
    """``resolve`` that returns None instead of raising. For genuinely optional UI."""
    try:
        return resolve(
            page,
            key,
            timeout_ms=timeout_ms,
            require_visible=require_visible,
            fmt=fmt,
            include_frames=include_frames,
        )
    except SelectorNotFound:
        return None
    except (ConfigError, ValueError) as exc:
        logger.warning("selector key %s is unusable: %s", key, exc)
        return None


def _resolved_candidate_name(page: "Page", key: str, fmt: dict[str, Any] | None = None) -> str:
    """Which candidate actually matched -- recorded in the run sidecar."""
    for root in _search_roots(page):
        for candidate in _candidates(key, fmt):
            if _match_now(root, candidate, True) is not None:
                return candidate
    return ""


# ==========================================================================
# Wall-clock budgets
# ==========================================================================


class _Deadline:
    """A shrinking wall-clock budget for one stage."""

    def __init__(self, budget_ms: int, stage: str) -> None:
        self.stage = stage
        self.budget_ms = budget_ms
        self._end = time.monotonic() + budget_ms / 1000.0

    def remaining_ms(self) -> int:
        return max(0, int((self._end - time.monotonic()) * 1000))

    def expired(self) -> bool:
        return self.remaining_ms() <= 0

    def slice_ms(self, wanted_ms: int) -> int:
        """Never wait longer than the stage has left, but always wait a little."""
        return max(500, min(wanted_ms, self.remaining_ms() or 500))

    def check(self) -> None:
        if self.expired():
            raise StageTimeout(
                f"stage '{self.stage}' exceeded its hard timeout of {self.budget_ms} ms "
                f"(raise TOWBOOK_STAGE_TIMEOUT_MS if the portal is genuinely this slow)"
            )


@contextmanager
def _stage(name: str, budget_ms: int | None = None) -> Iterator[_Deadline]:
    deadline = _Deadline(budget_ms or _stage_timeout_ms(), name)
    started = time.monotonic()
    logger.info("stage start: %s", name)
    try:
        yield deadline
    except Exception as exc:
        logger.error("stage FAILED: %s after %.1fs -- %s", name, time.monotonic() - started, exc)
        raise
    else:
        logger.info("stage ok: %s in %.1fs", name, time.monotonic() - started)


# ==========================================================================
# Browser session
# ==========================================================================


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment problem
        raise PlaywrightUnavailable(
            "Playwright is not installed in this interpreter.\n"
            "  Fix:  .venv\\Scripts\\python.exe -m pip install playwright\n"
            "        .venv\\Scripts\\python.exe -m playwright install chromium"
        ) from exc
    return sync_playwright


def _playwright_errors() -> tuple[type[BaseException], type[BaseException]]:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout

    return PWError, PWTimeout


@dataclass
class _Session:
    """Everything one browser run needs, in one place."""

    playwright: Any
    browser: Any
    context: "BrowserContext"
    page: "Page"
    account_id: str
    base_url: str
    headless: bool
    downloads_dir: Path
    state_path: Path
    loaded_saved_state: bool
    downloads: list[Any] = field(default_factory=list)

    # -- session persistence ------------------------------------------------

    def save_state(self) -> Path:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.state_path))
        with suppress(OSError):
            # Best effort on POSIX; a no-op on Windows.
            os.chmod(self.state_path, 0o600)
        logger.info("saved session storage_state -> %s", self.state_path)
        return self.state_path

    def drop_state(self) -> None:
        with suppress(OSError):
            if self.state_path.exists():
                self.state_path.unlink()
                logger.info("discarded stale session storage_state %s", self.state_path)

    # -- diagnostics --------------------------------------------------------

    def cookie_names(self) -> list[str]:
        """Cookie NAMES only. Values are session secrets and never leave here."""
        try:
            return sorted({str(cookie.get("name", "")) for cookie in self.context.cookies() if cookie.get("name")})
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not read cookies: %s", exc)
            return []

    def url(self) -> str:
        with suppress(Exception):
            return self.page.url
        return ""


def _abs_url(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return f"{base_url()}/{path_or_url.lstrip('/')}"


@contextmanager
def _launch(
    account_id: str,
    *,
    headless: bool | None = None,
    use_saved_state: bool = True,
    downloads_dir: Path | None = None,
) -> Iterator[_Session]:
    """Start Playwright, open a context, hand back a _Session, always clean up."""
    sync_playwright = _import_playwright()
    ensure_dirs()

    resolved_headless = _headless(headless)
    state_path = storage_state_path(account_id)
    downloads = downloads_dir or (DOWNLOAD_DIR / f"{_utc_stamp()}_{_slug(account_id)}")
    downloads.mkdir(parents=True, exist_ok=True)

    browser_name = _env_str("TOWBOOK_BROWSER", "chromium").lower()
    if browser_name not in {"chromium", "firefox", "webkit"}:
        logger.warning("TOWBOOK_BROWSER=%r is unknown, using chromium", browser_name)
        browser_name = "chromium"

    playwright = sync_playwright().start()
    browser = None
    context = None
    try:
        launcher = getattr(playwright, browser_name)
        try:
            browser = launcher.launch(
                headless=resolved_headless,
                slow_mo=_slowmo_ms(),
                # Downloads are pinned here so pickup is deterministic. We NEVER
                # glob a shared Downloads folder.
                downloads_path=str(downloads),
            )
        except Exception as exc:
            message = str(exc)
            if "executable doesn't exist" in message.lower() or "playwright install" in message.lower():
                raise PlaywrightUnavailable(
                    f"The {browser_name} browser binary is not installed.\n"
                    f"  Fix:  .venv\\Scripts\\python.exe -m playwright install {browser_name}\n"
                    f"  Original error: {message.splitlines()[0]}"
                ) from exc
            raise

        context_kwargs: dict[str, Any] = {
            "accept_downloads": True,
            "base_url": base_url(),
            "viewport": {"width": 1600, "height": 1000},
            "locale": "en-US",
        }
        # ASSUMPTION: the portal renders timestamps in the operator's local zone.
        # Pinning the browser to TZ keeps a headless run and a headed run showing
        # the same clock, which matters because the export is what we ingest.
        with suppress(Exception):
            context_kwargs["timezone_id"] = _local_timezone()

        loaded_saved_state = False
        if use_saved_state and state_path.exists():
            try:
                # Validate before handing it to Playwright; a truncated file
                # would otherwise blow up inside new_context with a vague error.
                json.loads(state_path.read_text(encoding="utf-8"))
                context_kwargs["storage_state"] = str(state_path)
                loaded_saved_state = True
                logger.info("reusing saved session %s", state_path)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("saved session %s is unreadable (%s); logging in fresh", state_path, exc)

        context = browser.new_context(**context_kwargs)
        context.set_default_timeout(_probe_timeout_ms() * 3)
        context.set_default_navigation_timeout(_nav_timeout_ms())

        page = context.new_page()

        session = _Session(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            account_id=account_id,
            base_url=base_url(),
            headless=resolved_headless,
            downloads_dir=downloads,
            state_path=state_path,
            loaded_saved_state=loaded_saved_state,
        )

        # A download can land on a popup rather than on the page we clicked.
        # expect_download stays the primary mechanism; this collector is the
        # safety net so a popup export is still picked up deterministically.
        def _collect(download: Any) -> None:
            session.downloads.append(download)

        page.on("download", _collect)
        context.on("page", lambda popup: popup.on("download", _collect))

        logger.info(
            "browser %s launched (headless=%s, slowmo=%dms, downloads=%s)",
            browser_name,
            resolved_headless,
            _slowmo_ms(),
            downloads,
        )
        yield session
    finally:
        for closer in (
            lambda: context.close() if context else None,
            lambda: browser.close() if browser else None,
            playwright.stop,
        ):
            with suppress(Exception):
                closer()


def _page_text(page: "Page", limit: int = 40_000) -> str:
    """Visible text of the page, lowercased, for keyword classification."""
    text = ""
    with suppress(Exception):
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    if not text:
        with suppress(Exception):
            text = page.content() or ""
    return text[:limit].lower()


def _goto(page: "Page", path_or_url: str, deadline: _Deadline | None = None, wait_until: str = "domcontentloaded") -> Any:
    url = _abs_url(path_or_url)
    timeout = deadline.slice_ms(_nav_timeout_ms()) if deadline else _nav_timeout_ms()
    logger.debug("goto %s", url)
    return page.goto(url, wait_until=wait_until, timeout=timeout)


def _settle(page: "Page", ms: int = 600) -> None:
    """Let the page finish whatever it started. networkidle is best-effort."""
    with suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=min(8_000, _nav_timeout_ms()))
    with suppress(Exception):
        page.wait_for_timeout(ms)


def _screenshot(page: "Page", target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(target), full_page=True)
    except Exception:
        try:
            page.screenshot(path=str(target))  # viewport-only fallback
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not capture screenshot %s: %s", target, exc)
            return ""
    return str(target)


def _dump_html(page: "Page", target: Path) -> str:
    """Write the rendered HTML, with every known secret scrubbed out of it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        html = page.content() or ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not capture HTML %s: %s", target, exc)
        return ""
    try:
        target.write_text(redact(html), encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("could not write HTML %s: %s", target, exc)
        return ""
    return str(target)


def _diag_dir(prefix: str) -> Path:
    directory = DIAGNOSTIC_DIR / f"{_slug(prefix)}_{_utc_stamp()}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ==========================================================================
# Authentication
# ==========================================================================


def _looks_logged_out(url: str) -> bool:
    markers = _config_words("session.logged_out_url_marker")
    lowered = (url or "").lower()
    return any(marker in lowered for marker in markers)


def _on_login_page(url: str) -> bool:
    """Are we sitting on the login form? Derived from selectors.yaml login.url."""
    lowered = (url or "").lower()
    for candidate in _candidates("login.url"):
        path = candidate.split("?")[0].strip().lower()
        if path and path in lowered:
            return True
    return _looks_logged_out(url)


def _probe_logged_in(session: _Session, deadline: _Deadline | None = None) -> bool:
    """Navigate to a protected path and see whether we get bounced to login.

    VERIFIED behaviour: an unauthenticated request to any app.towbook.com path
    redirects to /Security/Login?ReturnUrl=<path>. That redirect is the whole
    test -- it is far more reliable than hunting for a DOM element.
    """
    for path in _candidates("session.logged_in_probe"):
        try:
            response = _goto(session.page, path, deadline)
        except Exception as exc:
            logger.debug("probe %s raised %s", path, exc)
            continue
        final_url = session.url()
        status = getattr(response, "status", None)
        if _looks_logged_out(final_url):
            logger.info("probe %s -> bounced to login (%s)", path, final_url)
            return False
        if status is not None and status >= 400:
            logger.debug("probe %s -> HTTP %s, trying the next probe path", path, status)
            continue
        logger.info("probe %s -> authenticated (%s)", path, final_url)
        return True
    return False


def _has_validation_error(page: "Page", key: str) -> str:
    """Return the text of a NON-EMPTY ASP.NET validation span, or ""."""
    locator = resolve_optional(page, key, require_visible=False, timeout_ms=500)
    if locator is None:
        return ""
    text = ""
    with suppress(Exception):
        text = (locator.inner_text() or "").strip()
    if text:
        return text
    # A span can carry the error in a class while rendering the text via CSS.
    with suppress(Exception):
        classes = (locator.get_attribute("class") or "").lower()
        if "field-validation-error" in classes or "input-validation-error" in classes:
            return "(validation error class present, no text)"
    return ""


@dataclass
class _LoginOutcome:
    outcome: str
    message: str
    final_url: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_OK


def _classify_post_login(session: _Session, deadline: _Deadline) -> _LoginOutcome:
    """Decide, by name, what happened after the login form was submitted."""
    page = session.page
    final_url = session.url()
    body = _page_text(page)
    details: dict[str, Any] = {"url_after_submit": final_url}

    # 1. Bot wall first: a challenge page can imitate every other symptom.
    captcha_locator = resolve_optional(page, "login.captcha_indicator", timeout_ms=500)
    captcha_words = [w for w in _config_words("login.captcha_body_keywords") if w in body]
    if captcha_locator is not None or captcha_words:
        details["captcha_keywords"] = captcha_words
        return _LoginOutcome(
            OUTCOME_CAPTCHA_OR_BOT_WALL,
            "A CAPTCHA or bot-challenge page was served instead of a session. "
            "No CAPTCHA was present when the login page was last verified, so this is new. "
            "Run 'python -m towbook_agent bootstrap-session' to solve it once by hand in a "
            "headed browser; the saved session then carries the unattended runs.",
            final_url,
            details,
        )

    # 2. Second factor.
    mfa_locator = resolve_optional(page, "login.mfa_indicator", timeout_ms=500)
    url_hits = [w for w in _config_words("login.mfa_url_keywords") if w in final_url.lower()]
    body_hits = [w for w in _config_words("login.mfa_body_keywords") if w in body]
    if mfa_locator is not None or url_hits or body_hits:
        details["mfa_url_keywords"] = url_hits
        details["mfa_body_keywords"] = body_hits
        return _LoginOutcome(
            OUTCOME_MFA_REQUIRED,
            "Towbook is asking for a second factor. Unattended login cannot complete it. "
            "Run 'python -m towbook_agent bootstrap-session', finish the challenge in the "
            "headed browser, and the saved storage_state covers later scheduled runs.",
            final_url,
            details,
        )

    # 3. Rejected credentials: still on the login form after the POST.
    if _on_login_page(final_url):
        username_error = _has_validation_error(page, "login.username_validation_message")
        password_error = _has_validation_error(page, "login.password_validation_message")
        summary = resolve_optional(page, "login.failure_indicator", timeout_ms=500)
        summary_text = ""
        if summary is not None:
            with suppress(Exception):
                summary_text = (summary.inner_text() or "").strip()[:300]
        keyword_hits = [w for w in _config_words("login.bad_credential_body_keywords") if w in body]

        details.update(
            {
                "username_validation_message": username_error,
                "password_validation_message": password_error,
                "failure_indicator_text": summary_text,
                "bad_credential_keywords": keyword_hits,
            }
        )

        if username_error or password_error or summary_text or keyword_hits:
            shown = username_error or password_error or summary_text or ", ".join(keyword_hits)
            return _LoginOutcome(
                OUTCOME_BAD_CREDENTIALS,
                f"Towbook rejected the credentials. The page stayed on the login form and "
                f"reported: {shown!r}. Check TOWBOOK_USER / TOWBOOK_PASS in {ENV_FILE}. "
                f"Confirm by logging in manually in a browser with the same values.",
                final_url,
                details,
            )
        details["confidence"] = "low"
        return _LoginOutcome(
            OUTCOME_BAD_CREDENTIALS,
            "The login form was submitted and the browser stayed on the login page, which means "
            "the credentials were rejected -- but the portal's usual error banner "
            "('The username/password you specified is invalid') was NOT found, so the page markup "
            "may have changed too. Check TOWBOOK_USER / TOWBOOK_PASS first, then look at the "
            "screenshot and HTML dump beside this result and reconcile login.failure_indicator "
            "in config/selectors.yaml.",
            final_url,
            details,
        )

    # 4. Off the login page, but where exactly?
    chooser = resolve_optional(page, "login.account_chooser_indicator", timeout_ms=500)
    if chooser is not None:
        return _LoginOutcome(
            OUTCOME_UNKNOWN_POST_LOGIN,
            "Authentication succeeded but the portal stopped on an account/company chooser. "
            "The agent will not pick an account for you. Run 'bootstrap-session', select the "
            "right account by hand, and the saved session will land straight in it afterwards.",
            final_url,
            details,
        )

    success_locator = resolve_optional(page, "login.success_indicator", timeout_ms=1_000)
    details["success_indicator_visible"] = success_locator is not None

    deadline.check()
    if _probe_logged_in(session, deadline):
        details["probe"] = "passed"
        return _LoginOutcome(OUTCOME_OK, "Authenticated. A protected page loaded without redirecting to the login form.", session.url(), details)

    details["probe"] = "failed"
    if success_locator is not None:
        return _LoginOutcome(
            OUTCOME_UNKNOWN_POST_LOGIN,
            "The post-login chrome rendered, but a protected page still redirected back to the "
            "login form -- the session cookie is not sticking. Check the system clock and any "
            "corporate proxy that might be stripping cookies.",
            session.url(),
            details,
        )
    return _LoginOutcome(
        OUTCOME_UNKNOWN_POST_LOGIN,
        "The browser left the login page but landed somewhere unrecognised, and a protected "
        "page did not load. Inspect the screenshot and HTML dump recorded beside this result.",
        session.url(),
        details,
    )


def _perform_login(session: _Session, deadline: _Deadline, diag: Path | None = None) -> _LoginOutcome:
    """Fill and submit the login form, then classify the result. Never logs the password."""
    PWError, PWTimeout = _playwright_errors()
    page = session.page
    creds = credentials_for(session.account_id)

    login_paths = _candidates("login.url")
    if not login_paths:
        raise SelectorNotFound("login.url", [], url=session.url())

    last_error: Exception | None = None
    for path in login_paths:
        try:
            _goto(page, path, deadline)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            logger.warning("could not open login page %s: %s", _abs_url(path), exc)
    if last_error is not None:
        raise _as_network_error(last_error, _abs_url(login_paths[0]))

    _settle(page, 300)
    deadline.check()

    if diag:
        _screenshot(page, diag / "00_login_page.png")
        _dump_html(page, diag / "00_login_page.html")

    # A challenge served in place of the form is not a selector problem.
    if resolve_optional(page, "login.captcha_indicator", timeout_ms=500) is not None:
        return _LoginOutcome(
            OUTCOME_CAPTCHA_OR_BOT_WALL,
            "A CAPTCHA or bot challenge was served on the login page itself, before any "
            "credentials were submitted.",
            session.url(),
            {"phase": "pre_submit"},
        )

    username_input = resolve(page, "login.username_input", timeout_ms=deadline.slice_ms(_probe_timeout_ms() * 2))
    password_input = resolve(page, "login.password_input", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))
    submit_button = resolve(page, "login.submit_button", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))

    antiforgery = resolve_optional(page, "login.antiforgery_input", require_visible=False, timeout_ms=500)
    if antiforgery is None:
        logger.warning(
            "no antiforgery token field found on the login form; the POST may be rejected. "
            "This is a portal change worth investigating."
        )
    else:
        logger.info("antiforgery token field present on the login form")

    logger.info("submitting Towbook login for user %s (password read from TOWBOOK_PASS, never logged)", creds.masked_username)

    try:
        username_input.fill(creds.username)
        password_input.fill(creds.password)
    except PWError as exc:
        raise SelectorNotFound(
            "login.username_input / login.password_input",
            _candidates("login.username_input") + _candidates("login.password_input"),
            url=session.url(),
            hint=f"the inputs resolved but could not be filled: {exc}",
        ) from exc

    deadline.check()
    try:
        with suppress(PWTimeout):
            with page.expect_navigation(
                wait_until="domcontentloaded", timeout=deadline.slice_ms(_nav_timeout_ms())
            ):
                submit_button.click()
    except PWError as exc:
        raise _as_network_error(exc, session.url())

    _settle(page, 800)
    deadline.check()

    if diag:
        _screenshot(page, diag / "01_after_submit.png")
        _dump_html(page, diag / "01_after_submit.html")

    return _classify_post_login(session, deadline)


def _as_network_error(exc: Exception, url: str) -> AcquisitionError:
    """Turn a Playwright transport failure into a named, actionable error."""
    text = str(exc)
    lowered = text.lower()
    transport_markers = (
        "net::",
        "err_",
        "econnrefused",
        "enotfound",
        "timeout",
        "dns",
        "ssl",
        "certificate",
        "socket hang up",
        "connection closed",
    )
    if any(marker in lowered for marker in transport_markers):
        return LoginFailed(
            OUTCOME_NETWORK_ERROR,
            f"Could not reach {url}: {text.splitlines()[0]}. "
            f"Check connectivity, DNS, any corporate proxy, and that TOWBOOK_BASE_URL is right.",
            final_url=url,
            details={"raw_error": text[:2000]},
        )
    return LoginFailed(
        OUTCOME_UNKNOWN_POST_LOGIN,
        f"Unexpected browser error at {url}: {text.splitlines()[0]}",
        final_url=url,
        details={"raw_error": text[:2000]},
    )


def ensure_logged_in(session: _Session, deadline: _Deadline | None = None, diag: Path | None = None) -> str:
    """Guarantee an authenticated session, reusing storage_state when it still works.

    Returns "reused_session" or "fresh_login". Raises LoginFailed / MfaRequired /
    BotWallDetected / SelectorNotFound with a named outcome otherwise.
    """
    deadline = deadline or _Deadline(_stage_timeout_ms(), "login")

    if session.loaded_saved_state:
        if _probe_logged_in(session, deadline):
            logger.info("saved session for account '%s' is still valid", session.account_id)
            return "reused_session"
        logger.info("saved session for account '%s' has expired; authenticating fresh", session.account_id)
        session.drop_state()

    outcome = _perform_login(session, deadline, diag)
    if outcome.outcome == OUTCOME_OK:
        with suppress(Exception):
            session.save_state()
        return "fresh_login"

    if outcome.outcome == OUTCOME_MFA_REQUIRED:
        raise MfaRequired(outcome.message, final_url=outcome.final_url, details=outcome.details)
    if outcome.outcome == OUTCOME_CAPTCHA_OR_BOT_WALL:
        raise BotWallDetected(outcome.message, final_url=outcome.final_url, details=outcome.details)
    raise LoginFailed(outcome.outcome, outcome.message, final_url=outcome.final_url, details=outcome.details)


# ==========================================================================
# login_check -- the command the owner runs first
# ==========================================================================


def login_check(account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
    """Authenticate and report, by name, exactly what happened. No scraping.

    Always ignores any saved storage_state: the point of this command is to
    prove the *credentials* work, not that a cookie from last week survives.
    On success the fresh session is saved, so a passing login-check also arms
    every later unattended run.

    Returns a dict with at least:
        ok               bool
        stage            one of LOGIN_OUTCOMES
        message          a sentence a human can act on
        final_url        where the browser ended up
        screenshot_path  full-page screenshot of the post-submit page
        cookies_seen     cookie NAMES only -- never values
    """
    started = time.monotonic()
    diag = _diag_dir(f"login-check_{account_id}")
    result: dict[str, Any] = {
        "ok": False,
        "stage": OUTCOME_UNKNOWN_POST_LOGIN,
        "outcome": OUTCOME_UNKNOWN_POST_LOGIN,
        "message": "",
        "final_url": "",
        "screenshot_path": "",
        "cookies_seen": [],
        "account_id": account_id,
        "base_url": base_url(),
        "headless": _headless(),
        "diagnostics_dir": str(diag),
        "html_path": "",
        "username_masked": "",
        "storage_state_path": str(storage_state_path(account_id)),
        "storage_state_saved": False,
        "details": {},
    }

    def finish() -> dict:
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        with suppress(OSError):
            (diag / "login_check.json").write_text(
                redact(json.dumps(result, indent=2, default=str)), encoding="utf-8"
            )
        level = logger.info if result["ok"] else logger.error
        level(
            "login-check [%s] account=%s -> %s | %s",
            "OK" if result["ok"] else "FAIL",
            account_id,
            result["stage"],
            result["message"],
        )
        return result

    # -- credentials --------------------------------------------------------
    try:
        creds = credentials_for(account_id)
        result["username_masked"] = creds.masked_username
    except CredentialsMissing as exc:
        result["stage"] = OUTCOME_BAD_CREDENTIALS
        result["outcome"] = OUTCOME_BAD_CREDENTIALS
        result["message"] = str(exc)
        result["details"] = {"reason": "credentials_missing"}
        return finish()

    # -- drive the browser --------------------------------------------------
    try:
        with _launch(account_id, use_saved_state=False, downloads_dir=diag / "downloads") as session:
            with _stage("login", _stage_timeout_ms()) as deadline:
                outcome = _perform_login(session, deadline, diag=diag)

            result["stage"] = outcome.outcome
            result["outcome"] = outcome.outcome
            result["message"] = outcome.message
            result["final_url"] = outcome.final_url or session.url()
            result["ok"] = outcome.ok
            result["details"] = outcome.details
            result["cookies_seen"] = session.cookie_names()
            result["screenshot_path"] = _screenshot(session.page, diag / "02_final_state.png")
            result["html_path"] = _dump_html(session.page, diag / "02_final_state.html")

            expected = _config_words("session.expected_cookie_names")
            seen_lower = [c.lower() for c in result["cookies_seen"]]
            result["details"]["expected_cookies_present"] = [
                name for name in expected if any(name in seen for seen in seen_lower)
            ]

            if outcome.ok:
                with suppress(Exception):
                    session.save_state()
                    result["storage_state_saved"] = True

    except PlaywrightUnavailable as exc:
        result["stage"] = OUTCOME_ENVIRONMENT_ERROR
        result["outcome"] = OUTCOME_ENVIRONMENT_ERROR
        result["message"] = str(exc)
    except SelectorNotFound as exc:
        result["stage"] = OUTCOME_SELECTOR_NOT_FOUND
        result["outcome"] = OUTCOME_SELECTOR_NOT_FOUND
        result["message"] = str(exc)
        result["details"] = {"selector_key": exc.key, "candidates_tried": exc.candidates}
        result["final_url"] = exc.url
    except LoginFailed as exc:
        result["stage"] = exc.outcome
        result["outcome"] = exc.outcome
        result["message"] = str(exc)
        result["final_url"] = exc.final_url
        result["details"] = exc.details
    except StageTimeout as exc:
        result["stage"] = OUTCOME_NETWORK_ERROR
        result["outcome"] = OUTCOME_NETWORK_ERROR
        result["message"] = f"{exc} The portal did not respond inside the stage budget."
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never crash
        named = _as_network_error(exc, base_url())
        result["stage"] = named.outcome if isinstance(named, LoginFailed) else OUTCOME_UNKNOWN_POST_LOGIN
        result["outcome"] = result["stage"]
        result["message"] = str(named)

    return finish()


# ==========================================================================
# The Request Log clickpath
# ==========================================================================


def _on_request_log(page: "Page") -> bool:
    """Are we looking at the Request Log? Any of three independent signals."""
    if _looks_logged_out(page.url):
        return False
    for key in ("navigation.digital_requests_tab", "request_log.results_table", "request_log.no_results_indicator"):
        if resolve_optional(page, key, timeout_ms=800) is not None:
            return True
    return False


def _open_request_log(session: _Session, deadline: _Deadline) -> str:
    """Dispatching -> Request Log. Direct URL first, menu dance as the fallback."""
    page = session.page

    for path in _candidates("navigation.request_log_url"):
        deadline.check()
        try:
            _goto(page, path, deadline)
        except Exception as exc:
            logger.debug("direct request-log url %s failed: %s", path, exc)
            continue
        _settle(page, 400)
        if _looks_logged_out(page.url):
            logger.info("direct url %s bounced to login -- session died mid-run", path)
            raise LoginFailed(
                OUTCOME_UNKNOWN_POST_LOGIN,
                "The session expired between authenticating and opening the Request Log.",
                final_url=page.url,
            )
        if _on_request_log(page):
            logger.info("Request Log reached directly at %s", _abs_url(path))
            return f"direct:{path}"

    logger.info("no direct Request Log URL worked; using the Dispatching menu")
    deadline.check()

    menu = resolve(page, "navigation.dispatching_menu", timeout_ms=deadline.slice_ms(_probe_timeout_ms() * 2))
    # Hover first: an ASP.NET top toolbar usually opens its submenu on hover,
    # and clicking a menu ROOT sometimes navigates away instead of opening it.
    with suppress(Exception):
        menu.hover(timeout=deadline.slice_ms(5_000))
        page.wait_for_timeout(400)

    link = resolve_optional(page, "navigation.request_log_link", timeout_ms=1_500)
    if link is None:
        with suppress(Exception):
            menu.click(timeout=deadline.slice_ms(5_000))
            _settle(page, 500)
        link = resolve(page, "navigation.request_log_link", timeout_ms=deadline.slice_ms(_probe_timeout_ms() * 2))

    with suppress(Exception):
        with page.expect_navigation(wait_until="domcontentloaded", timeout=deadline.slice_ms(_nav_timeout_ms())):
            link.click()
    _settle(page, 600)

    if not _on_request_log(page):
        raise SelectorNotFound(
            "request_log.results_table",
            _candidates("request_log.results_table"),
            url=page.url,
            hint=(
                "Dispatching -> Request Log was clicked but nothing on the resulting page looked "
                "like the grid. Either the menu path changed or the grid selectors are wrong."
            ),
        )
    logger.info("Request Log reached via the Dispatching menu")
    return "menu"


def _select_digital_requests_tab(session: _Session, deadline: _Deadline) -> str:
    """Make sure we are on Digital Requests, not Other Requests."""
    page = session.page
    tab = resolve_optional(page, "navigation.digital_requests_tab", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))
    if tab is None:
        logger.warning(
            "the 'Digital Requests' tab did not resolve (candidates: %s). Continuing on whatever "
            "tab the Request Log opened on -- verify the export really is Digital Requests.",
            ", ".join(_candidates("navigation.digital_requests_tab")),
        )
        return "not_found"

    selected = ""
    with suppress(Exception):
        selected = (tab.get_attribute("aria-selected") or "").lower()
    classes = ""
    with suppress(Exception):
        classes = (tab.get_attribute("class") or "").lower()

    # ASSUMPTION: an already-active tab is marked aria-selected="true" or carries
    # an "active"/"k-state-active"/"selected" class. Clicking an active tab is
    # harmless anyway, so a wrong guess here costs nothing.
    if selected == "true" or any(marker in classes for marker in ("active", "k-state-active", "selected", "current")):
        logger.info("already on the Digital Requests tab")
        return "already_selected"

    with suppress(Exception):
        tab.click(timeout=deadline.slice_ms(10_000))
    _settle(page, 600)
    logger.info("clicked the Digital Requests tab")
    return "clicked"


def _set_page_size(session: _Session, page_size: int | None, deadline: _Deadline) -> dict:
    """Ask the grid for ``page_size`` rows.

    Not fatal by default: an export normally covers the whole result set, so a
    page-size control we cannot find is a loud warning rather than a dead run.
    Set TOWBOOK_REQUIRE_PAGE_SIZE=true to make it fatal.
    """
    report: dict[str, Any] = {"requested": page_size, "applied": None, "method": "skipped"}
    if not page_size:
        return report

    page = session.page
    required = _env_bool("TOWBOOK_REQUIRE_PAGE_SIZE", False)
    control = resolve_optional(page, "request_log.page_size_control", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))
    if control is None:
        message = (
            f"page size control not found (selectors.yaml request_log.page_size_control: "
            f"{', '.join(_candidates('request_log.page_size_control'))})"
        )
        if required:
            raise SelectorNotFound("request_log.page_size_control", _candidates("request_log.page_size_control"), url=page.url)
        logger.warning("%s -- continuing; the export usually covers every row regardless.", message)
        report["method"] = "not_found"
        report["note"] = message
        return report

    tag = ""
    with suppress(Exception):
        tag = (control.evaluate("el => el.tagName ? el.tagName.toLowerCase() : ''") or "").lower()

    if tag == "select":
        options: list[dict[str, str]] = []
        with suppress(Exception):
            options = control.evaluate(
                "el => Array.from(el.options || []).map(o => ({value: String(o.value), label: (o.text || '').trim()}))"
            ) or []
        wanted = str(page_size)
        try:
            control.select_option(value=wanted)
            report.update(applied=page_size, method="select_option_value")
        except Exception:
            try:
                control.select_option(label=wanted)
                report.update(applied=page_size, method="select_option_label")
            except Exception:
                # Pick the largest offered size that does not exceed what we
                # asked for; failing that, the largest on offer.
                numeric = []
                for option in options:
                    digits = re.sub(r"[^0-9]", "", option.get("value") or option.get("label") or "")
                    if digits:
                        numeric.append((int(digits), option.get("value", "")))
                numeric.sort()
                choice = next((v for n, v in reversed(numeric) if n <= page_size), numeric[-1][1] if numeric else "")
                if choice:
                    with suppress(Exception):
                        control.select_option(value=choice)
                    report.update(applied=choice, method="select_option_closest")
                    logger.warning("page size %s is not offered; selected %s instead", page_size, choice)
                else:
                    report.update(method="select_option_failed")
                    logger.warning("could not set the page size on the <select>; options were %s", options)
    else:
        # A styled dropdown: open it, then pick the option by the templated selector.
        with suppress(Exception):
            control.click(timeout=deadline.slice_ms(10_000))
            page.wait_for_timeout(400)
        option = resolve_optional(
            page,
            "request_log.page_size_option_template",
            fmt={"size": page_size},
            timeout_ms=deadline.slice_ms(_probe_timeout_ms()),
        )
        if option is None:
            message = f"page size option '{page_size}' not found in the opened dropdown"
            if required:
                raise SelectorNotFound(
                    "request_log.page_size_option_template",
                    _candidates("request_log.page_size_option_template", {"size": page_size}),
                    url=page.url,
                )
            logger.warning("%s -- continuing.", message)
            report.update(method="option_not_found", note=message)
            with suppress(Exception):
                page.keyboard.press("Escape")
            return report
        with suppress(Exception):
            option.click(timeout=deadline.slice_ms(10_000))
        report.update(applied=page_size, method="dropdown_option_click")

    _settle(page, 800)
    logger.info("page size step: %s", report)
    return report


def _format_for_input(locator: "Locator", moment: datetime, format_key: str) -> str:
    """Render ``moment`` the way this particular input expects it."""
    input_type = ""
    with suppress(Exception):
        input_type = (locator.get_attribute("type") or "").lower()
    if input_type == "date":
        # The HTML specification fixes the *value* of an <input type="date"> at
        # yyyy-mm-dd regardless of the locale the browser displays.
        return moment.strftime("%Y-%m-%d")
    if input_type == "datetime-local":
        return moment.strftime("%Y-%m-%dT%H:%M")
    if input_type == "time":
        return moment.strftime("%H:%M")
    formats = _candidates(format_key)
    return moment.strftime(formats[0]) if formats else moment.strftime("%m/%d/%Y")


def _fill_temporal(page: "Page", locator: "Locator", text: str, deadline: _Deadline) -> None:
    """Type a value into a date/time input and make sure the widget notices.

    VERIFIED: Towbook's date inputs carry the jQuery UI datepicker (class
    "hasDatepicker"), which opens a calendar overlay on focus. The overlay is
    dismissed with an explicit blur() rather than the Escape key, because some
    jQuery UI builds treat Escape as "cancel" and restore the previous value --
    which would silently export the wrong window.
    """
    with suppress(Exception):
        locator.fill("", timeout=deadline.slice_ms(8_000))
    locator.fill(text, timeout=deadline.slice_ms(8_000))
    # fill() already dispatches 'input'; datepickers built on jQuery UI and
    # Kendo listen for 'change'. Firing it explicitly is cheap insurance, and
    # blur() closes the calendar without touching the value.
    with suppress(Exception):
        locator.evaluate(
            "el => { el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " if (el.blur) el.blur(); }"
        )
    with suppress(Exception):
        page.wait_for_timeout(150)


def _read_input_value(page: "Page", key: str) -> str:
    """Read back what a filter input currently holds, or "" if it is gone."""
    locator = resolve_optional(page, key, timeout_ms=1_000)
    if locator is None:
        return ""
    with suppress(Exception):
        return (locator.input_value() or "").strip()
    return ""


def _write_and_verify(page: "Page", key: str, text: str, deadline: _Deadline, attempts: int = 3) -> bool:
    """Type ``text`` into ``key`` and confirm it actually stuck.

    VERIFIED failure mode: w2ui re-renders the whole toolbar when the grid
    reloads (a page-size change fires changePageSize -> refreshDigitalGrid),
    and the re-render replaces the date inputs with EMPTY ones. A value typed
    just before that lands is silently discarded, and the export then covers
    the portal's default window instead of the requested one -- wrong numbers
    that look entirely plausible.

    So every write is read back, and the element is re-resolved on each attempt
    because the previous node may have been detached by the re-render.
    """
    for attempt in range(1, attempts + 1):
        deadline.check()
        locator = resolve_optional(page, key, timeout_ms=deadline.slice_ms(_probe_timeout_ms()))
        if locator is None:
            return False
        with suppress(Exception):
            _fill_temporal(page, locator, text, deadline)
        actual = _read_input_value(page, key)
        if actual == text:
            if attempt > 1:
                logger.info("%s held %r on attempt %d", key, text, attempt)
            return True
        logger.warning(
            "%s did not retain %r (reads %r) on attempt %d/%d -- the toolbar was most likely "
            "re-rendered mid-write; retrying",
            key,
            text,
            actual,
            attempt,
            attempts,
        )
        _settle(page, 900)
    return False


def _set_date_range(session: _Session, start: datetime, end: datetime, deadline: _Deadline) -> dict:
    """Enter the report window and prove it stuck.

    Fatal by default: exporting the wrong window is worse than exporting
    nothing, because wrong numbers look plausible. Override with
    TOWBOOK_REQUIRE_DATE_RANGE=false only while reconciling selectors.
    """
    page = session.page
    required = _env_bool("TOWBOOK_REQUIRE_DATE_RANGE", True)
    report: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_written": "",
        "end_written": "",
        "verified": False,
        "time_inputs": "absent",
    }

    # Let any in-flight grid reload finish first, so the toolbar we are about
    # to type into is the one that will still be there when Filter is clicked.
    _settle(page, 900)

    start_input = resolve_optional(page, "request_log.date_start_input", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))
    end_input = resolve_optional(page, "request_log.date_end_input", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))

    if start_input is None or end_input is None:
        missing = "date_start_input" if start_input is None else "date_end_input"
        if required:
            raise SelectorNotFound(
                f"request_log.{missing}",
                _candidates(f"request_log.{missing}"),
                url=page.url,
                hint=(
                    "Refusing to export without a date range: a full-history or default-window "
                    "export would silently produce wrong metrics. Reconcile selectors.yaml with "
                    "'python -m towbook_agent discover-selectors', or set "
                    "TOWBOOK_REQUIRE_DATE_RANGE=false to accept the grid's default window."
                ),
            )
        logger.warning("date inputs not found; exporting the grid's default window")
        report["note"] = "date inputs not found; default window exported"
        return report

    start_text = _format_for_input(start_input, start, "request_log.date_input_formats")
    end_text = _format_for_input(end_input, end, "request_log.date_input_formats")
    report["start_written"] = start_text
    report["end_written"] = end_text

    ok_start = _write_and_verify(page, "request_log.date_start_input", start_text, deadline)
    ok_end = _write_and_verify(page, "request_log.date_end_input", end_text, deadline)
    report["verified"] = bool(ok_start and ok_end)

    if not report["verified"]:
        message = (
            f"the date range would not stay in the filter inputs "
            f"(start={_read_input_value(page, 'request_log.date_start_input')!r}, "
            f"end={_read_input_value(page, 'request_log.date_end_input')!r}, "
            f"wanted {start_text!r}..{end_text!r})"
        )
        if required:
            raise AcquisitionError(
                f"Refusing to export: {message}. Exporting the portal's default window would "
                f"produce plausible-looking but wrong metrics. Set TOWBOOK_REQUIRE_DATE_RANGE=false "
                f"only if you genuinely want the default window."
            )
        logger.warning("%s -- continuing with the default window", message)

    # Optional separate time-of-day inputs.
    start_time = resolve_optional(page, "request_log.time_start_input", timeout_ms=800)
    end_time = resolve_optional(page, "request_log.time_end_input", timeout_ms=800)
    if start_time is not None and end_time is not None:
        start_time_text = _format_for_input(start_time, start, "request_log.time_input_formats")
        end_time_text = _format_for_input(end_time, end, "request_log.time_input_formats")
        _write_and_verify(page, "request_log.time_start_input", start_time_text, deadline)
        _write_and_verify(page, "request_log.time_end_input", end_time_text, deadline)
        report["time_inputs"] = "filled"
        report["start_time_written"] = start_time_text
        report["end_time_written"] = end_time_text
    else:
        # VERIFIED: the Request Log filters by DATE only. Exporting whole days
        # and letting metrics trim to the exact hour stays correct because
        # ingestion upserts on request_id -- extra rows are simply re-seen.
        logger.info("no separate time inputs; exporting whole days and trimming downstream")

    logger.info("date range step: %s -> %s (verified=%s)", start_text, end_text, report["verified"])
    return report


def _run_report(session: _Session, deadline: _Deadline, date_report: dict | None = None) -> str:
    """Apply the filter. Re-verifies the date range immediately before clicking.

    The re-check is the last line of defence against the w2ui toolbar
    re-rendering between "dates typed" and "Filter clicked".
    """
    page = session.page
    date_report = date_report or {}

    for key, wanted in (
        ("request_log.date_start_input", date_report.get("start_written")),
        ("request_log.date_end_input", date_report.get("end_written")),
    ):
        if not wanted:
            continue
        if _read_input_value(page, key) != wanted:
            logger.warning("%s was cleared before Filter could be clicked; rewriting", key)
            if not _write_and_verify(page, key, wanted, deadline) and _env_bool(
                "TOWBOOK_REQUIRE_DATE_RANGE", True
            ):
                raise AcquisitionError(
                    f"{key} would not hold {wanted!r} immediately before applying the filter; "
                    f"refusing to export the wrong window."
                )

    button = resolve_optional(page, "request_log.run_report_button", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))
    if button is None:
        logger.info("no run/filter button found; assuming the grid filters live")
        _settle(page, 1_200)
        return "live_filtering"
    with suppress(Exception):
        button.click(timeout=deadline.slice_ms(15_000))
    _settle(page, 1_500)
    logger.info("applied the filter")
    return "clicked"


def _wait_for_results(session: _Session, deadline: _Deadline, page_size: int | None = None) -> dict:
    """Wait out the busy overlay, then confirm the grid resolved one way or the other.

    Telling "zero rows in this window" apart from "the page never loaded" is
    the difference between a correct zero and a silent failure, so both states
    are named explicitly here.
    """
    page = session.page

    spinner = resolve_optional(page, "request_log.loading_indicator", timeout_ms=800)
    if spinner is not None:
        with suppress(Exception):
            spinner.wait_for(state="hidden", timeout=deadline.slice_ms(30_000))

    table = resolve_optional(page, "request_log.results_table", timeout_ms=deadline.slice_ms(_probe_timeout_ms() * 2))

    row_count = 0
    for candidate in _candidates("request_log.results_table_row"):
        with suppress(Exception):
            count = page.locator(candidate).count()
            if count:
                row_count = count
                break

    # VERIFIED: the portal renders "<n> Records [Page <p> of <q>]" -- but it is
    # NOT refreshed after a date filter is applied, so it still shows the
    # unfiltered total. It is recorded as advisory context only; the authority
    # on "did we get rows" is the row count above.
    record_label = ""
    reported_records: int | None = None
    label = resolve_optional(page, "request_log.record_count_label", timeout_ms=1_500)
    if label is not None:
        with suppress(Exception):
            record_label = re.sub(r"\s+", " ", label.inner_text() or "").strip()
        match = re.search(r"([\d,]+)\s+Records", record_label, re.I)
        if match:
            with suppress(ValueError):
                reported_records = int(match.group(1).replace(",", ""))

    headers: list[str] = []
    for candidate in _candidates("request_log.results_table_header"):
        with suppress(Exception):
            texts = page.locator(candidate).all_inner_texts()
            cleaned = [re.sub(r"\s+", " ", t).strip() for t in texts if t and t.strip()]
            if cleaned:
                headers = cleaned
                break

    result: dict[str, Any] = {
        "state": "loaded",
        "row_count": row_count,
        "reported_records_stale": reported_records,
        "record_label": record_label,
        "headers": headers,
        "truncated": False,
    }

    if row_count == 0:
        empty = resolve_optional(page, "request_log.no_results_indicator", timeout_ms=1_500)
        if empty is not None or table is not None:
            logger.info("the grid holds no records for this window (a genuine zero, not a failure)")
            result["state"] = "empty"
            return result
        raise SelectorNotFound(
            "request_log.results_table",
            _candidates("request_log.results_table"),
            url=page.url,
            hint="Neither rows nor a 'no records' message appeared, so the grid state is unknown.",
        )

    # VERIFIED: the export writes the CURRENT PAGE, not the whole result set.
    # A full page therefore means rows were almost certainly cut off, and a
    # truncated export is exactly the kind of quiet wrongness this system must
    # never ship. Raise the page size in config/schedule.yaml.
    if page_size and row_count >= page_size:
        result["truncated"] = True
        logger.error(
            "the grid returned %d row(s) for a page size of %d -- the export covers only the "
            "current page, so rows are being LOST. Increase page_size in config/schedule.yaml "
            "(the portal offers up to 10000).",
            row_count,
            page_size,
        )

    logger.info(
        "grid loaded: %d visible row(s), stale portal label %r, headers=%s",
        row_count,
        record_label or "(none)",
        headers or "(none read)",
    )
    return result


# ==========================================================================
# Export -> Excel -> archive
# ==========================================================================


#: How long to wait, after clicking a control that might be a menu OR might be
#: the download itself, before deciding which of the two it was.
_MENU_GRACE_MS = 8_000


def _capture_download(session: _Session, trigger: "Locator", key: str, timeout_ms: int) -> Any:
    """Click ``trigger`` inside expect_download and return the Download, or None."""
    _, PWTimeout = _playwright_errors()
    page = session.page
    try:
        with page.expect_download(timeout=timeout_ms) as download_info:
            trigger.click(timeout=min(20_000, timeout_ms))
        return download_info.value
    except PWTimeout:
        # A download can land on a popup rather than on the page we clicked;
        # the context-wide collector catches that case.
        if session.downloads:
            logger.info("download for %s arrived on a popup page", key)
            return session.downloads[0]
        return None


def _export_excel(session: _Session, deadline: _Deadline) -> Path:
    """Drive Export -> Excel and capture the file into this run's own directory.

    Two shapes are supported without a code change:

    * VERIFIED on Towbook -- Export is a SINGLE toolbar button whose tooltip is
      "Export to Excel". One click, one download, no menu.
    * A dropdown portal -- clicking Export reveals an "Excel" item which is
      then clicked. Detected by the first click producing no download but
      revealing ``request_log.export_excel_option``.

    Clicking Export twice would produce two exports, so the trigger is only
    ever clicked once per shape.
    """
    page = session.page
    session.downloads.clear()
    full_timeout = max(deadline.slice_ms(_download_timeout_ms()), 15_000)

    # A portal that exposes Excel directly, with no menu to open first.
    excel = resolve_optional(page, "request_log.export_excel_option", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))
    if excel is not None:
        logger.info("export: a direct Excel control is present; clicking it once")
        download = _capture_download(session, excel, "request_log.export_excel_option", full_timeout)
        if download is None:
            raise ExportFailed(
                f"Clicked request_log.export_excel_option but no download started within "
                f"{full_timeout} ms. Current url: {page.url}"
            )
        return _save_download(session, download)

    menu = resolve_optional(page, "request_log.export_menu", timeout_ms=deadline.slice_ms(_probe_timeout_ms()))
    if menu is None:
        raise SelectorNotFound(
            "request_log.export_menu / request_log.export_excel_option",
            _candidates("request_log.export_menu") + _candidates("request_log.export_excel_option"),
            url=page.url,
            hint="Neither an Export control nor an Excel option is on the page. Run discover-selectors.",
        )

    # Ambiguous click: it either starts the download or opens a menu. Give it a
    # short grace period, then decide from what actually happened.
    logger.info("export: clicking the Export control (single-button export expected)")
    download = _capture_download(session, menu, "request_log.export_menu", min(_MENU_GRACE_MS, full_timeout))
    if download is not None:
        return _save_download(session, download)

    revealed = resolve_optional(page, "request_log.export_excel_option", timeout_ms=2_000)
    if revealed is not None:
        logger.info("export: the first click opened a menu; clicking the revealed Excel option")
        download = _capture_download(session, revealed, "request_log.export_excel_option", full_timeout)
        if download is not None:
            return _save_download(session, download)
        raise ExportFailed(
            f"An Excel menu item was revealed and clicked, but no download started within "
            f"{full_timeout} ms. Current url: {page.url}"
        )

    # No menu appeared and no download yet -- the server may simply be slow
    # building a large workbook. Keep waiting on the already-clicked export
    # rather than clicking again, which would queue a second export.
    remaining = max(0, full_timeout - _MENU_GRACE_MS)
    if remaining:
        logger.info("export: no menu appeared; waiting a further %d ms for the file to build", remaining)
        deadline_at = time.monotonic() + remaining / 1000.0
        while time.monotonic() < deadline_at:
            if session.downloads:
                return _save_download(session, session.downloads[0])
            with suppress(Exception):
                page.wait_for_timeout(500)

    raise ExportFailed(
        f"Clicked request_log.export_menu but no download started within {full_timeout} ms and no "
        f"Excel menu item appeared.\n"
        f"  Current url: {page.url}\n"
        f"  Either the export is asynchronous (queued or emailed), or the wrong element was clicked. "
        f"  Run 'python -m towbook_agent discover-selectors' and check the suggested candidates for "
        f"request_log.export_menu / request_log.export_excel_option."
    )


def _save_download(session: _Session, download: Any) -> Path:
    """Persist a Download into this run's isolated directory."""
    failure = None
    with suppress(Exception):
        failure = download.failure()
    if failure:
        raise ExportFailed(f"The browser reported a failed download: {failure}")

    suggested = ""
    with suppress(Exception):
        suggested = download.suggested_filename or ""
    filename = _slug(suggested or "towbook_export.xlsx", limit=80)
    if not filename.lower().endswith((".xlsx", ".xls", ".csv")):
        filename += ".xlsx"

    target = session.downloads_dir / f"{_utc_stamp()}_{filename}"
    target.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(str(target))
    size = target.stat().st_size if target.exists() else -1
    logger.info("captured export -> %s (%d bytes, suggested name %r)", target, size, suggested)
    return target


def _validate_export(path: Path) -> str:
    """Refuse anything that is not actually tabular data. Returns the format.

    An expired session commonly "downloads" the login page as HTML. Catching
    that here is what stops a silent zero-row day from reaching the database.

    VERIFIED 2026-07-28: Towbook's "Export to Excel" button actually delivers
    ``export.csv`` -- a CSV, despite the tooltip. The file is CRLF-terminated
    and carries TWO preamble lines before the header row:

        "https://app.towbook.com/RequestLog"
        "07/28/2026 11:11 am"
        "Request Date","Provider","Contractor ID",...

    So CSV is a first-class, expected result here, not a failure. The XLSX and
    XLS branches remain for the day Towbook changes its mind.
    """
    if not path.exists():
        raise ExportFailed(f"the export file was not written: {path}")
    size = path.stat().st_size
    if size == 0:
        raise ExportFailed(f"the export file is empty (0 bytes): {path}")

    with path.open("rb") as handle:
        head = handle.read(8)

    if head.startswith(_ZIP_MAGIC):
        logger.info("export validated as XLSX (%d bytes)", size)
        return "xlsx"
    if head.startswith(_OLE_MAGIC):
        logger.warning(
            "the export is legacy .xls (OLE2). openpyxl cannot read it -- ingestion will need "
            "a different reader. File: %s",
            path,
        )
        return "xls"

    preview = ""
    with suppress(Exception):
        preview = redact(path.read_bytes()[:600].decode("utf-8-sig", errors="replace"))
    flat = preview.replace("\r", " ").replace("\n", " ")

    if "<html" in flat.lower() or "<!doctype" in flat.lower() or flat.lstrip().startswith("<"):
        raise ExportFailed(
            f"the download is HTML, not tabular data -- almost always an expired session being "
            f"redirected to the login page. File: {path}\n  First bytes: {flat[:200]!r}"
        )

    # A CSV must have a delimiter and at least one line break in its first
    # chunk. Requiring both keeps a stray error page from passing as data.
    if ("," in preview or "\t" in preview or ";" in preview) and ("\n" in preview or "\r" in preview):
        logger.info("export validated as CSV (%d bytes) -- the portal's 'Export to Excel' emits CSV", size)
        return "csv"

    raise ExportFailed(
        f"the download is not recognisable as XLSX, XLS or CSV (magic bytes {head[:4]!r}). "
        f"File: {path}\n  First bytes: {flat[:200]!r}"
    )


def _archive(source: Path, anchor: datetime, run_id: str) -> Path:
    """Copy the raw export into raw/YYYY/MM/DD/run_<timestamp>.xlsx. Never deletes.

    ASSUMPTION: the YYYY/MM/DD directory is the *window start* date, not the
    download date, so every export covering a given day sits together and a
    backfill lands beside the original rather than in today's folder.
    """
    suffix = source.suffix.lower() or ".xlsx"
    target = raw_path_for(anchor, f"run_{run_id}{suffix}")
    counter = 1
    while target.exists():
        target = target.with_name(f"run_{run_id}_{counter}{suffix}")
        counter += 1
    shutil.copy2(source, target)
    logger.info("archived raw export -> %s", target)
    return target


# ==========================================================================
# Window normalisation
# ==========================================================================


def _as_datetime(value: datetime | date | str, name: str) -> datetime:
    """Accept a datetime, a date, or an ISO string; return naive local time."""
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{name}={value!r} is not an ISO 8601 datetime: {exc}") from None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    else:
        raise TypeError(f"{name} must be a datetime, date or ISO string, got {type(value).__name__}")

    if moment.tzinfo is not None:
        # The portal's filters are in the operator's local zone; storage is UTC
        # everywhere else, so this is the one place that converts back.
        try:
            from zoneinfo import ZoneInfo

            moment = moment.astimezone(ZoneInfo(_local_timezone()))
        except Exception:  # pragma: no cover - missing tzdata
            moment = moment.astimezone()
        moment = moment.replace(tzinfo=None)
    return moment


# ==========================================================================
# acquire -- the production path
# ==========================================================================


def acquire(
    start_datetime: datetime | date | str,
    end_datetime: datetime | date | str,
    page_size: int | None,
    account_id: str = DEFAULT_ACCOUNT_ID,
    *,
    run_id: str | None = None,
    headless: bool | None = None,
) -> Path:
    """Log in, drive the Request Log, export, archive the file, return its path.

    NOTE ON THE FILE TYPE: the portal's "Export to Excel" button actually
    delivers a **CSV** (verified 2026-07-28), so the returned path is normally
    ``raw/YYYY/MM/DD/run_<timestamp>.csv``. The true extension is preserved
    rather than renamed to .xlsx, because pretending a CSV is a workbook would
    only break the reader downstream. ``meta["export_format"]`` records which
    of xlsx / xls / csv arrived, and an XLSX is handled unchanged if Towbook
    ever switches.

    Retries the whole clickpath on failure with 30s / 2m / 5m of backoff
    (TOWBOOK_RETRY_BACKOFF_S). On total failure it emits ``pipeline_failure``
    and raises -- silence is never treated as success.

    A sidecar ``run_<id>.meta.json`` is written next to the archived file
    recording which selector candidate satisfied each step, whether the date
    range was verified to have stuck, the visible row count and whether the
    export was truncated by the page size. That file is what makes a
    suspicious number traceable months later.
    """
    ensure_dirs()
    start = _as_datetime(start_datetime, "start_datetime")
    end = _as_datetime(end_datetime, "end_datetime")
    if end < start:
        raise ValueError(f"end_datetime ({end.isoformat()}) is before start_datetime ({start.isoformat()})")

    run_identifier = run_id or _utc_stamp()
    backoff = _retry_backoff_seconds()
    attempts = len(backoff) + 1
    failures: list[str] = []

    logger.info(
        "acquire: account=%s window=%s..%s page_size=%s run_id=%s attempts=%d",
        account_id,
        start.isoformat(),
        end.isoformat(),
        page_size,
        run_identifier,
        attempts,
    )

    for attempt in range(1, attempts + 1):
        download_dir = DOWNLOAD_DIR / f"{run_identifier}_a{attempt}_{_slug(account_id)}"
        meta: dict[str, Any] = {
            "run_id": run_identifier,
            "account_id": account_id,
            "attempt": attempt,
            "window_start_local": start.isoformat(),
            "window_end_local": end.isoformat(),
            "timezone": _local_timezone(),
            "page_size_requested": page_size,
            "base_url": base_url(),
            "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            with _launch(account_id, headless=headless, downloads_dir=download_dir) as session:
                with _stage(f"login (attempt {attempt})") as deadline:
                    meta["login"] = ensure_logged_in(session, deadline)

                with _stage(f"open request log (attempt {attempt})") as deadline:
                    meta["navigation"] = _open_request_log(session, deadline)
                    meta["digital_requests_tab"] = _select_digital_requests_tab(session, deadline)

                with _stage(f"filters (attempt {attempt})") as deadline:
                    meta["page_size"] = _set_page_size(session, page_size, deadline)
                    meta["date_range"] = _set_date_range(session, start, end, deadline)

                with _stage(f"run report (attempt {attempt})") as deadline:
                    meta["run_report"] = _run_report(session, deadline, meta.get("date_range"))
                    meta["results"] = _wait_for_results(session, deadline, page_size)

                with _stage(f"export (attempt {attempt})", _download_timeout_ms() + 30_000) as deadline:
                    meta["export_selector"] = _resolved_candidate_name(
                        session.page, "request_log.export_menu"
                    ) or _resolved_candidate_name(session.page, "request_log.export_excel_option")
                    downloaded = _export_excel(session, deadline)

                meta["export_format"] = _validate_export(downloaded)
                meta["downloaded_bytes"] = downloaded.stat().st_size
                meta["downloaded_name"] = downloaded.name
                meta["cookies_seen"] = session.cookie_names()

                # The export covers only the current page. A full page means
                # rows were cut off. The file is still archived (partial data
                # beats none, and ingestion upserts so a later wider run
                # repairs it), but a human MUST be told -- silence is never
                # treated as success.
                if meta.get("results", {}).get("truncated"):
                    events.emit_event(
                        "pipeline_failure",
                        {
                            "stage": "acquisition",
                            "severity": "high",
                            "account_id": account_id,
                            "run_id": run_identifier,
                            "window_start": start.isoformat(),
                            "window_end": end.isoformat(),
                            "page_size": page_size,
                            "row_count": meta["results"].get("row_count"),
                            "error": (
                                f"TRUNCATED EXPORT: the Request Log returned at least "
                                f"{meta['results'].get('row_count')} rows for a page size of "
                                f"{page_size}, and the export only covers the current page, so "
                                f"rows were lost."
                            ),
                            "next_step": (
                                "Raise page_size for this job in config/schedule.yaml. The portal "
                                "offers 25/50/75/100/1000/1500/2000/2500/5000/10000."
                            ),
                        },
                    )

                archived = _archive(downloaded, start, run_identifier)
                meta["archived_path"] = str(archived)
                meta["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                meta["status"] = "ok"

                with suppress(OSError):
                    archived.with_suffix(".meta.json").write_text(
                        redact(json.dumps(meta, indent=2, default=str)), encoding="utf-8"
                    )

                # The isolated download directory has served its purpose; the
                # archive under raw/ is the copy we never delete.
                with suppress(OSError):
                    shutil.rmtree(download_dir, ignore_errors=True)

                logger.info("acquire OK: %s (%d bytes)", archived, meta["downloaded_bytes"])
                return archived

        except Exception as exc:  # noqa: BLE001 - every failure is retried, then alerted
            label = f"{type(exc).__name__}: {exc}"
            failures.append(f"attempt {attempt}: {label}")
            logger.error("acquire attempt %d/%d failed -- %s", attempt, attempts, label)

            # A credential or environment problem will fail identically forever.
            if isinstance(exc, (CredentialsMissing, PlaywrightUnavailable)):
                logger.error("this failure is not transient; not retrying")
                break
            if isinstance(exc, LoginFailed) and exc.outcome in {OUTCOME_BAD_CREDENTIALS, OUTCOME_MFA_REQUIRED}:
                logger.error("this failure needs a human; not retrying")
                break

            if attempt <= len(backoff):
                delay = backoff[attempt - 1]
                logger.info("backing off %ds before attempt %d", delay, attempt + 1)
                if delay:
                    time.sleep(delay)

    summary = (
        f"Towbook acquisition failed for account '{account_id}', window "
        f"{start.isoformat()} .. {end.isoformat()} (run {run_identifier}) after "
        f"{len(failures)} attempt(s).\n  " + "\n  ".join(failures)
    )
    events.emit_event(
        "pipeline_failure",
        {
            "stage": "acquisition",
            "severity": "high",
            "account_id": account_id,
            "run_id": run_identifier,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "page_size": page_size,
            "attempts": len(failures),
            "error": failures[-1] if failures else "unknown",
            "all_errors": failures,
            "next_step": "python -m towbook_agent login-check   then   python -m towbook_agent discover-selectors",
        },
    )
    raise AcquisitionError(summary)


# ==========================================================================
# discover_selectors -- how the operator reconciles the unknown DOM
# ==========================================================================

# Collects every attribute worth knowing about an interactive element.
# Password field values are blanked here, and the whole payload is passed
# through redact() before it is written to disk.
_INVENTORY_JS = """
(selector) => {
  let nodes = [];
  try { nodes = Array.from(document.querySelectorAll(selector)); }
  catch (err) { return [{error: String(err)}]; }
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  for (const el of nodes.slice(0, 1200)) {
    let rect = {width: 0, height: 0};
    try { rect = el.getBoundingClientRect(); } catch (e) {}
    let style = null;
    try { style = window.getComputedStyle(el); } catch (e) {}
    const visible = !!(rect.width || rect.height) &&
      (!style || (style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0'));
    const tag = (el.tagName || '').toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let text = clean(el.innerText || el.textContent || '');
    if (text.length > 160) text = text.slice(0, 160) + '...';
    const data = {};
    try {
      for (const attr of Array.from(el.attributes || [])) {
        if (attr.name.startsWith('data-') || attr.name.startsWith('aria-')) {
          data[attr.name] = clean(attr.value).slice(0, 120);
        }
      }
    } catch (e) {}
    const entry = {
      tag: tag,
      id: el.id || '',
      name: el.getAttribute('name') || '',
      class: (typeof el.className === 'string' ? el.className : '').slice(0, 200),
      type: type,
      role: el.getAttribute('role') || '',
      aria_label: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      title: el.getAttribute('title') || '',
      href: (el.getAttribute('href') || '').slice(0, 300),
      text: text,
      visible: visible,
      disabled: !!el.disabled,
      data_attrs: data
    };
    if (type === 'password' || /pass/i.test(entry.name) || /pass/i.test(entry.id)) {
      entry.value = '';
    } else {
      entry.value = clean(el.getAttribute('value') || '').slice(0, 120);
    }
    if (tag === 'select') {
      try {
        entry.options = Array.from(el.options).slice(0, 60)
          .map(o => ({value: String(o.value), label: clean(o.text)}));
      } catch (e) {}
    }
    out.push(entry);
  }
  return out;
}
"""

_DEFAULT_INTERACTIVE = [
    "button",
    "a",
    "select",
    "input",
    "textarea",
    '[role="tab"]',
    '[role="button"]',
    '[role="menuitem"]',
    '[role="combobox"]',
    '[role="option"]',
]


def _interactive_selector() -> str:
    try:
        parts = _candidates("discovery.interactive_selectors")
    except (ConfigError, ValueError):
        parts = []
    return ", ".join(parts or _DEFAULT_INTERACTIVE)


def _inventory(page: "Page") -> list[dict]:
    """Catalogue every interactive element on the page and in every frame."""
    selector = _interactive_selector()
    entries: list[dict] = []
    for index, root in enumerate(_search_roots(page)):
        frame_url = ""
        with suppress(Exception):
            frame_url = root.url
        try:
            found = root.evaluate(_INVENTORY_JS, selector) or []
        except Exception as exc:
            logger.debug("inventory failed on frame %d: %s", index, exc)
            continue
        for entry in found:
            if not isinstance(entry, dict) or "error" in entry:
                continue
            entry["frame_index"] = index
            entry["frame_url"] = frame_url
            entries.append(entry)
    return entries


def _css_for(element: dict) -> str:
    """A Playwright selector that would find this element again."""
    tag = element.get("tag") or "*"

    def quote(value: str) -> str:
        return value.replace('"', '\\"')

    if element.get("id"):
        return f"#{element['id']}"
    if element.get("name"):
        return f'{tag}[name="{quote(element["name"])}"]'
    if element.get("aria_label"):
        return f'{tag}[aria-label="{quote(element["aria_label"])}"]'
    if element.get("placeholder"):
        return f'{tag}[placeholder="{quote(element["placeholder"])}"]'
    if element.get("href") and tag == "a":
        return f'a[href="{quote(element["href"])}"]'
    text = (element.get("text") or "").strip()
    if text and len(text) <= 40:
        return f'{tag}:has-text("{quote(text)}")'
    classes = [c for c in (element.get("class") or "").split() if c and not c.isdigit()]
    if classes:
        return f"{tag}.{classes[0]}"
    return tag


def _suggestion_config() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    section: dict[str, Any] = {}
    with suppress(Exception):
        section = CONFIG.get_selectors().get("discovery") or {}
    keywords = section.get("suggestion_keywords") or {}
    tags = section.get("suggestion_tags") or {}
    clean_keywords = {
        str(k): [str(w).lower() for w in (v or [])] for k, v in keywords.items() if isinstance(v, (list, tuple))
    }
    clean_tags = {str(k): [str(t).lower() for t in (v or [])] for k, v in tags.items() if isinstance(v, (list, tuple))}
    return clean_keywords, clean_tags


def _suggest_selectors(inventory: list[dict]) -> dict[str, list[str]]:
    """Rank inventory elements against the keyword vocabulary in selectors.yaml."""
    keywords, tag_hints = _suggestion_config()
    suggestions: dict[str, list[str]] = {}

    for key, words in keywords.items():
        allowed_tags = tag_hints.get(key, [])
        scored: list[tuple[int, str, dict]] = []
        for element in inventory:
            tag = element.get("tag", "")
            if allowed_tags and tag not in allowed_tags:
                continue
            strong = " ".join(
                str(element.get(f) or "") for f in ("id", "name", "aria_label", "placeholder", "title")
            ).lower()
            weak = " ".join(
                str(element.get(f) or "") for f in ("class", "text", "href", "type", "role")
            ).lower()
            score = 0
            for word in words:
                if word in strong:
                    score += 5
                elif word in weak:
                    score += 2
            if not score:
                continue
            if element.get("visible"):
                score += 3
            if element.get("id"):
                score += 2
            if element.get("disabled"):
                score -= 2
            scored.append((score, _css_for(element), element))

        seen: set[str] = set()
        ranked: list[str] = []
        for score, css, _element in sorted(scored, key=lambda item: -item[0]):
            if css in seen:
                continue
            seen.add(css)
            ranked.append(css)
            if len(ranked) >= 6:
                break
        suggestions[key] = ranked
    return suggestions


def discover_selectors(account_id: str = DEFAULT_ACCOUNT_ID) -> Path:
    """Log in, walk to the Request Log, and dump everything needed to fix selectors.yaml.

    Writes to ``state/discovery/<utc timestamp>_<account>/``:
        NN_<stage>.png            full-page screenshot at every stage
        NN_<stage>.html           the rendered HTML at that stage (secrets scrubbed)
        inventory.json            every interactive element, per stage, per frame
        selectors.suggested.yaml  the best candidates found for the unknown keys
        summary.json              what happened, and which stages succeeded
        README.txt                what to do with all of it

    Returns the directory. Runs headed automatically when HEADLESS is false, so
    the operator can watch (and complete an MFA challenge) while it works.
    """
    ensure_dirs()
    out_dir = DISCOVERY_DIR / f"{_utc_stamp()}_{_slug(account_id)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("discovery dump -> %s", out_dir)

    stages: list[dict[str, Any]] = []
    inventories: dict[str, list[dict]] = {}
    summary: dict[str, Any] = {
        "account_id": account_id,
        "base_url": base_url(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "headless": _headless(),
        "output_dir": str(out_dir),
        "stages": stages,
        "errors": [],
    }

    def capture(page: "Page", index: int, name: str) -> None:
        prefix = f"{index:02d}_{_slug(name)}"
        entry = {
            "index": index,
            "name": name,
            "url": "",
            "title": "",
            "screenshot": _screenshot(page, out_dir / f"{prefix}.png"),
            "html": _dump_html(page, out_dir / f"{prefix}.html"),
            "frames": [],
        }
        with suppress(Exception):
            entry["url"] = page.url
        with suppress(Exception):
            entry["title"] = page.title()
        with suppress(Exception):
            entry["frames"] = [f.url for f in page.frames]
        elements = _inventory(page)
        inventories[name] = elements
        entry["element_count"] = len(elements)
        stages.append(entry)
        logger.info("captured stage '%s': %d interactive element(s)", name, len(elements))

    try:
        with _launch(account_id, downloads_dir=out_dir / "downloads") as session:
            page = session.page
            with _stage("login") as deadline:
                summary["login"] = ensure_logged_in(session, deadline, diag=out_dir)
            capture(page, 1, "post_login")

            # The Dispatching menu opened, so its submenu markup is captured too.
            with suppress(Exception):
                menu = resolve_optional(page, "navigation.dispatching_menu", timeout_ms=3_000)
                if menu is not None:
                    with suppress(Exception):
                        menu.hover(timeout=5_000)
                        page.wait_for_timeout(700)
                    capture(page, 2, "dispatching_menu_open")

            with _stage("open request log") as deadline:
                summary["navigation"] = _open_request_log(session, deadline)
            capture(page, 3, "request_log")

            with _stage("digital requests tab") as deadline:
                summary["digital_requests_tab"] = _select_digital_requests_tab(session, deadline)
            capture(page, 4, "digital_requests_tab")

            # Click Export so that, on a dropdown-style portal, its menu items
            # appear in the inventory -- they usually do not exist in the DOM
            # until then. On Towbook the same click IS the export, so any
            # download it starts is cancelled rather than kept: discovery must
            # never quietly pull a data file. Which of the two happened is
            # itself a useful finding, so it is recorded.
            with suppress(Exception):
                session.downloads.clear()
                export = resolve_optional(page, "request_log.export_menu", timeout_ms=3_000)
                if export is not None:
                    with suppress(Exception):
                        export.click(timeout=5_000)
                        page.wait_for_timeout(1_200)
                    capture(page, 5, "export_menu_open")
                    if session.downloads:
                        summary["export_is_direct_download"] = True
                        for download in session.downloads:
                            with suppress(Exception):
                                summary.setdefault("export_filename", download.suggested_filename)
                            with suppress(Exception):
                                download.cancel()
                        session.downloads.clear()
                        logger.info(
                            "Export is a direct download (suggested name %r); cancelled it -- "
                            "discovery inventories the DOM, it does not pull data",
                            summary.get("export_filename", "?"),
                        )
                    else:
                        summary["export_is_direct_download"] = False
                        with suppress(Exception):
                            page.keyboard.press("Escape")

            summary["cookies_seen"] = session.cookie_names()

    except Exception as exc:  # noqa: BLE001 - discovery must always leave evidence
        message = f"{type(exc).__name__}: {exc}"
        summary["errors"].append(message)
        logger.error("discovery stopped early -- %s", message)

    combined: list[dict] = []
    for name, elements in inventories.items():
        for element in elements:
            element = dict(element)
            element["stage"] = name
            combined.append(element)

    suggestions = _suggest_selectors(combined)
    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary["total_elements"] = len(combined)

    with suppress(OSError):
        (out_dir / "inventory.json").write_text(
            redact(json.dumps({"stages": inventories}, indent=2, default=str)), encoding="utf-8"
        )
    with suppress(OSError):
        (out_dir / "summary.json").write_text(redact(json.dumps(summary, indent=2, default=str)), encoding="utf-8")

    patch: dict[str, Any] = {
        "_generated_by": "python -m towbook_agent discover-selectors",
        "_generated_at_utc": summary["finished_at_utc"],
        "_how_to_use": (
            "These are SUGGESTIONS ranked by keyword match, not confirmed selectors. "
            "Check each one against the screenshots and inventory.json in this folder, then paste "
            "the good ones at the TOP of the matching candidate list in config/selectors.yaml. "
            "Never replace the whole list -- prepending keeps the existing fallbacks working."
        ),
        "request_log": {key: value for key, value in suggestions.items() if value},
    }
    empty_keys = [key for key, value in suggestions.items() if not value]
    if empty_keys:
        patch["_nothing_found_for"] = empty_keys
    with suppress(OSError):
        (out_dir / "selectors.suggested.yaml").write_text(
            yaml.safe_dump(patch, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
        )

    readme = f"""Towbook selector discovery
==========================
Generated : {summary['finished_at_utc']} UTC
Account   : {account_id}
Base URL  : {summary['base_url']}
Stages    : {', '.join(stage['name'] for stage in stages) or '(none reached)'}
Elements  : {summary['total_elements']}
Errors    : {'; '.join(summary['errors']) or 'none'}

Files
-----
NN_<stage>.png            full-page screenshot of each stage, in order
NN_<stage>.html           the rendered HTML of that stage (secrets scrubbed)
inventory.json            every button/link/select/input/tab found, per stage,
                          including iframes, with id / name / class / role /
                          aria-label / visible text / href / <select> options
selectors.suggested.yaml  the best candidates found for the five unknown keys:
                          page_size_control, date_start_input, date_end_input,
                          run_report_button, export_menu, export_excel_option
summary.json              machine-readable version of this file

What to do
----------
1. Open the screenshots and find the real controls.
2. Look them up in inventory.json to get their exact id / name / class.
3. PREPEND the correct selector to the matching list in config/selectors.yaml.
   Keep the existing entries below it -- they are the fallbacks.
4. Re-run:  python -m towbook_agent discover-selectors
   and confirm the agent now reaches every stage.

No password appears anywhere in this folder. Values of password inputs are
blanked at capture time and every file is passed through the secret redactor.
"""
    with suppress(OSError):
        (out_dir / "README.txt").write_text(readme, encoding="utf-8")

    logger.info("discovery complete: %d element(s) catalogued -> %s", len(combined), out_dir)
    return out_dir


# ==========================================================================
# bootstrap_session -- one manual login that unlocks every unattended run
# ==========================================================================


def bootstrap_session(account_id: str = DEFAULT_ACCOUNT_ID, timeout_seconds: int | None = None) -> Path:
    """Open a HEADED browser, wait for a human to finish logging in, save the session.

    This is the escape hatch for MFA, SSO and an account chooser: anything a
    script cannot complete, a person completes once here, and the resulting
    Playwright storage_state carries every scheduled run afterwards.

    Credentials are pre-filled when TOWBOOK_USER / TOWBOOK_PASS are set, so
    usually the only manual step is the second factor. Returns the path of the
    saved storage_state file.
    """
    ensure_dirs()
    budget = timeout_seconds or _env_int("TOWBOOK_BOOTSTRAP_TIMEOUT_S", 900)
    target = storage_state_path(account_id)

    print("=" * 78, file=sys.stderr)
    print(f"Towbook session bootstrap for account '{account_id}'", file=sys.stderr)
    print(f"  A browser window will open at {base_url()}.", file=sys.stderr)
    print("  Log in there by hand -- including any two-factor code or account choice.", file=sys.stderr)
    print(f"  This process watches for up to {budget}s and saves the session the moment", file=sys.stderr)
    print(f"  it sees you are authenticated:  {target}", file=sys.stderr)
    print("=" * 78, file=sys.stderr)

    with _launch(account_id, headless=False, use_saved_state=False, downloads_dir=DOWNLOAD_DIR / "bootstrap") as session:
        page = session.page
        login_paths = _candidates("login.url") or ["/"]
        with suppress(Exception):
            _goto(page, login_paths[0])
        _settle(page, 400)

        # Pre-fill so the operator only has to deal with the part a script cannot.
        try:
            creds = credentials_for(account_id)
        except CredentialsMissing:
            creds = None
            logger.info("no credentials in the environment; log in entirely by hand")
        if creds is not None:
            username_input = resolve_optional(page, "login.username_input", timeout_ms=4_000)
            password_input = resolve_optional(page, "login.password_input", timeout_ms=2_000)
            if username_input is not None and password_input is not None:
                with suppress(Exception):
                    username_input.fill(creds.username)
                    password_input.fill(creds.password)
                print(
                    f"  Credentials for {creds.masked_username} pre-filled. Click 'Log in' and finish "
                    f"any challenge.",
                    file=sys.stderr,
                )

        deadline = time.monotonic() + budget
        interval = 3.0
        last_report = 0.0
        while time.monotonic() < deadline:
            with suppress(Exception):
                page.wait_for_timeout(int(interval * 1000))
            current = session.url()
            if current and not _on_login_page(current):
                probe = _Deadline(30_000, "bootstrap probe")
                if _probe_logged_in(session, probe):
                    saved = session.save_state()
                    print(f"\n  Authenticated. Session saved to {saved}", file=sys.stderr)
                    print("  Unattended runs will now reuse it until it expires.\n", file=sys.stderr)
                    return saved
            remaining = int(deadline - time.monotonic())
            if remaining and remaining % 30 < interval and remaining != last_report:
                last_report = remaining
                print(f"  ...still waiting for a completed login ({remaining}s left)", file=sys.stderr)

        raise LoginFailed(
            OUTCOME_UNKNOWN_POST_LOGIN,
            f"No completed login was observed within {budget}s. Re-run "
            f"'python -m towbook_agent bootstrap-session' and finish the login in the browser "
            f"window, or raise TOWBOOK_BOOTSTRAP_TIMEOUT_S.",
            final_url=session.url(),
        )


# ==========================================================================
# Standalone CLI -- so this file is runnable on its own before __main__ exists
# ==========================================================================


def _print_login_check(result: dict) -> None:
    banner = "LOGIN OK" if result["ok"] else f"LOGIN FAILED -- {result['stage']}"
    line = "=" * max(40, min(78, len(banner) + 20))
    print(line)
    print(banner)
    print(line)
    print(f"account          : {result['account_id']}")
    print(f"user             : {result.get('username_masked') or '(none)'}")
    print(f"base url         : {result['base_url']}")
    print(f"outcome          : {result['stage']}")
    print(f"final url        : {result['final_url'] or '(none)'}")
    print(f"cookies seen     : {', '.join(result['cookies_seen']) or '(none)'}")
    print(f"screenshot       : {result['screenshot_path'] or '(none)'}")
    print(f"html dump        : {result.get('html_path') or '(none)'}")
    print(f"diagnostics dir  : {result.get('diagnostics_dir')}")
    print(f"session saved    : {result.get('storage_state_saved')}")
    print(f"duration         : {result.get('duration_seconds')}s")
    print("-" * len(line))
    print(result["message"])
    print(line)


def _cli(argv: Sequence[str]) -> int:
    command = argv[0] if argv else "login-check"
    account = argv[1] if len(argv) > 1 else DEFAULT_ACCOUNT_ID

    if command in {"login-check", "login_check", "check"}:
        result = login_check(account)
        _print_login_check(result)
        return 0 if result["ok"] else 1

    if command in {"discover-selectors", "discover"}:
        path = discover_selectors(account)
        print(f"discovery written to: {path}")
        return 0

    if command in {"bootstrap-session", "bootstrap"}:
        try:
            path = bootstrap_session(account)
        except AcquisitionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"session saved to: {path}")
        return 0

    print(
        "usage: python -m towbook_agent.agents.acquisition "
        "[login-check|discover-selectors|bootstrap-session] [account_id]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    from ..core.logging_setup import setup_logging

    setup_logging()
    sys.exit(_cli(sys.argv[1:]))
