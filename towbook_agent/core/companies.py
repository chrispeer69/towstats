"""The tenant registry: which towing companies this install reports on.

The system began as one owner watching one Towbook account. It is now given or
sold to other US Tow Alliance members, so "which company is this number about"
has to be a first-class question rather than an assumption baked into a single
set of credentials.

    config/companies.yaml   the roster -- one entry per towing company
    requests.company_id     which company every stored row belongs to
    company_id filters      on every query, in every module

WHAT THIS IS NOT
----------------
Not a SaaS. There is no billing, no signup, no user management, no per-user
permission model. It is multi-company *reporting*: one operator, running one
install, producing separate numbers for several towing companies.

CREDENTIALS ARE NEVER IN THIS FILE
----------------------------------
``companies.yaml`` is committed. It therefore holds only the *name of the
environment variable prefix*, never a login:

    credentials_env: ROADSIDE   ->  TOWBOOK_ROADSIDE_USER / TOWBOOK_ROADSIDE_PASS

An entry with no ``credentials_env`` falls back to the plain
``TOWBOOK_USER`` / ``TOWBOOK_PASS`` pair, which is what makes an existing
single-company install keep working with no edit at all. Hard constraint #1 is
unchanged: credentials come from the environment only, and the logging filter
scrubs them from every log record.

THE SINGLE-COMPANY FALLBACK
---------------------------
No ``companies.yaml``, an empty one, or one with no enabled entries all resolve
to exactly one company whose id is :data:`DEFAULT_COMPANY_ID` -- the same
``"default"`` that every existing row already carries. So the file is optional,
and adding it later does not orphan a single stored request.

CONFIGURATION PRECEDENCE
------------------------
``config/rules.yaml`` is the default for every company. A company entry may
override parts of it, and :func:`rules_for` merges them in this order, later
winning:

    1. config/rules.yaml                     the global default
    2. company.coverage         ->  rules[missed_work][coverage]
    3. company.job_value_by_client
                                ->  rules[missed_work][job_value_by_client]
    4. company.rules            ->  deep-merged over everything above

Steps 2 and 3 are shorthands for the two overrides every tenant actually needs
-- a Texas company is staffed different hours than an Ohio one, and its clients
pay different money -- and both REPLACE the global block outright rather than
merging into it, because a half-inherited staffed window or a half-inherited
price list is a number nobody can defend. Step 4 is the escape hatch for
anything else (alert thresholds, blind-spot cut-offs, service classes) and
merges mapping-by-mapping; a list or a scalar replaces.

THE ACTIVE COMPANY
------------------
Time is the one dimension that cannot be passed as a parameter without
threading it through every date helper in the system, and a coverage window is
meaningless in the wrong timezone. So the company currently being computed is
held in a :class:`~contextvars.ContextVar`, set by :func:`use_company` around
each pipeline run, each metrics computation and each dashboard request.
``agents.metrics.local_timezone_name`` and ``web.queries.local_tz`` consult it,
falling back to the ``TZ`` environment variable when the company names no zone.
Nothing else reads it: every other company-dependent value is an explicit
argument, because an implicit filter is how rows leak between tenants.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from .config_loader import CONFIG, ConfigError

__all__ = [
    "DEFAULT_COMPANY_ID",
    "Company",
    "CompanyError",
    "all_companies",
    "enabled_companies",
    "get_company",
    "resolve_company",
    "resolve_company_id",
    "default_company_id",
    "is_multi_company",
    "company_choices",
    "rules_for",
    "timezone_for",
    "active_company_id",
    "use_company",
    "reload_companies",
]

logger = logging.getLogger(__name__)

#: The company id an install with no companies.yaml uses, and the value every
#: row written before this module existed already carries. Must stay equal to
#: ``core.models.DEFAULT_ACCOUNT_ID`` -- there is a test that asserts it.
DEFAULT_COMPANY_ID: str = "default"

#: Prefix for the per-company credential variables. ``credentials_env: ACME``
#: means TOWBOOK_ACME_USER / TOWBOOK_ACME_PASS.
_ENV_PREFIX = "TOWBOOK"

_ID_SAFE = re.compile(r"[^a-z0-9._-]+")
_ENV_SAFE = re.compile(r"[^A-Z0-9_]+")


class CompanyError(RuntimeError):
    """companies.yaml is present but cannot be read as a company roster."""


# ==========================================================================
# The record
# ==========================================================================


@dataclass(frozen=True)
class Company:
    """One towing company this install reports on.

    ``id`` is the value stored in ``company_id`` on every row, so it is the one
    field that must never change once data exists for it. Everything else is
    presentation or configuration and can be edited freely.
    """

    id: str
    name: str
    #: Towbook's own numeric company id (61343 for Roadside). Informational --
    #: the system keys on ``id``, not on this -- but it is what an operator
    #: recognises when reconciling against the portal.
    towbook_company_id: str | None = None
    #: Environment variable PREFIX for this company's login. Never the login.
    credentials_env: str | None = None
    #: IANA zone. None means "use the TZ environment variable".
    timezone: str | None = None
    enabled: bool = True
    #: Shorthand override for rules.yaml -> missed_work.coverage
    coverage: dict[str, Any] | None = None
    #: Shorthand override for rules.yaml -> missed_work.job_value_by_client
    job_value_by_client: dict[str, Any] | None = None
    #: Free-form deep merge over rules.yaml -- thresholds, alerts, anything.
    rules_overrides: dict[str, Any] = field(default_factory=dict)
    #: Whether this record came from companies.yaml or from the fallback.
    configured: bool = True

    # -- credentials -------------------------------------------------------

    @property
    def env_user(self) -> str:
        """Name of the environment variable holding this company's username."""
        return f"{_ENV_PREFIX}_{self.credentials_env}_USER" if self.credentials_env else f"{_ENV_PREFIX}_USER"

    @property
    def env_pass(self) -> str:
        """Name of the environment variable holding this company's password."""
        return f"{_ENV_PREFIX}_{self.credentials_env}_PASS" if self.credentials_env else f"{_ENV_PREFIX}_PASS"

    @property
    def label(self) -> str:
        """What the dashboard shows in the switcher."""
        return self.name or self.id

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe summary. Deliberately carries no credential of any kind."""
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "towbook_company_id": self.towbook_company_id,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "credentials_env": self.credentials_env,
            "env_user": self.env_user,
            "env_pass": self.env_pass,
            "configured": self.configured,
        }


def _fallback_company() -> Company:
    """The one company a single-company install has, with no config file.

    Its id is ``default`` because that is what every row already stored on the
    owner's machine carries. Change that and his history disappears.
    """
    return Company(
        id=DEFAULT_COMPANY_ID,
        name="Default company",
        credentials_env=None,
        timezone=None,
        enabled=True,
        configured=False,
    )


# ==========================================================================
# Parsing
# ==========================================================================


def _slug(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = _ID_SAFE.sub("-", text).strip("-")
    return text


def _env_prefix(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    cleaned = _ENV_SAFE.sub("_", text).strip("_")
    return cleaned or None


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "y"}


def _as_mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _parse_company(entry: Mapping[str, Any], position: int) -> Company | None:
    raw_id = entry.get("id") or entry.get("company_id") or entry.get("account_id")
    company_id = _slug(raw_id)
    if not company_id:
        logger.error(
            "config/companies.yaml: entry #%d has no usable 'id'; skipping it",
            position + 1,
        )
        return None

    towbook_id = entry.get("towbook_company_id")
    return Company(
        id=company_id,
        name=str(entry.get("name") or company_id).strip() or company_id,
        towbook_company_id=str(towbook_id).strip() if towbook_id not in (None, "") else None,
        credentials_env=_env_prefix(entry.get("credentials_env")),
        timezone=(str(entry.get("timezone")).strip() or None) if entry.get("timezone") else None,
        enabled=_as_bool(entry.get("enabled"), True),
        coverage=_as_mapping(entry.get("coverage")),
        job_value_by_client=_as_mapping(entry.get("job_value_by_client")),
        rules_overrides=_as_mapping(entry.get("rules")) or {},
        configured=True,
    )


def _read_roster() -> tuple[list[Company], str]:
    """``(companies, default id)`` from companies.yaml, or the fallback.

    A malformed file degrades to the single-company fallback with a loud error
    rather than taking the dashboard and the scheduler down: reporting on one
    company is strictly better than reporting on none.
    """
    try:
        document = CONFIG.get("companies") or {}
    except ConfigError as exc:
        logger.error("config/companies.yaml is unreadable (%s); using the single-company fallback", exc)
        return ([_fallback_company()], DEFAULT_COMPANY_ID)

    raw = document.get("companies")
    if isinstance(raw, Mapping):
        # Tolerate `companies: {acme: {...}}` as well as a list of entries.
        raw = [{**value, "id": value.get("id", key)} for key, value in raw.items() if isinstance(value, Mapping)]
    if not isinstance(raw, (list, tuple)) or not raw:
        return ([_fallback_company()], DEFAULT_COMPANY_ID)

    companies: list[Company] = []
    seen: set[str] = set()
    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            logger.error(
                "config/companies.yaml: entry #%d is a %s, not a mapping; skipping it",
                position + 1,
                type(entry).__name__,
            )
            continue
        company = _parse_company(entry, position)
        if company is None:
            continue
        if company.id in seen:
            # Two entries with one id would silently merge two tenants'
            # numbers, which is the exact failure this module exists to stop.
            logger.error(
                "config/companies.yaml: duplicate company id %r; keeping the first entry only",
                company.id,
            )
            continue
        seen.add(company.id)
        companies.append(company)

    if not companies:
        logger.error(
            "config/companies.yaml declares companies but none are usable; "
            "using the single-company fallback"
        )
        return ([_fallback_company()], DEFAULT_COMPANY_ID)

    configured_default = _slug(document.get("default_company"))
    if configured_default and configured_default in seen:
        default_id = configured_default
    else:
        if configured_default:
            logger.error(
                "config/companies.yaml: default_company %r is not in the roster; "
                "using the first enabled entry instead",
                configured_default,
            )
        enabled = [company for company in companies if company.enabled]
        default_id = (enabled or companies)[0].id

    return (companies, default_id)


# ==========================================================================
# Cache
#
# Keyed on the digest of companies.yaml, so an edit is picked up on the next
# access exactly like every other config file (hard constraint #2, zero
# redeploy) without re-parsing the roster on every query.
# ==========================================================================

_lock = threading.RLock()
_cached_digest: str | None = None
_cached_roster: tuple[list[Company], str] = ([], DEFAULT_COMPANY_ID)
_cached_rules: dict[str, tuple[str, dict[str, Any]]] = {}


def _digest() -> str:
    try:
        return CONFIG.digest("companies")
    except ConfigError:
        return ""


def _roster() -> tuple[list[Company], str]:
    global _cached_digest, _cached_roster
    digest = _digest()
    with _lock:
        if digest != _cached_digest or not _cached_roster[0]:
            _cached_roster = _read_roster()
            _cached_digest = digest
            _cached_rules.clear()
        return _cached_roster


def reload_companies() -> None:
    """Drop the cached roster. The next access re-reads companies.yaml."""
    global _cached_digest
    with _lock:
        _cached_digest = None
        _cached_rules.clear()
    CONFIG.reload("companies")


# ==========================================================================
# Lookup
# ==========================================================================


def all_companies() -> list[Company]:
    """Every company in the roster, enabled or not, in file order."""
    return list(_roster()[0])


def enabled_companies() -> list[Company]:
    """The companies the scheduler runs and the dashboard offers.

    Never empty: if every entry is disabled the roster is treated as
    unconfigured, because a scheduler with nothing to do and a dashboard with
    no company to show are both silent failures.
    """
    companies = [company for company in all_companies() if company.enabled]
    return companies or [_fallback_company()]


def get_company(company_id: str | None) -> Company | None:
    """The company with this id, or None. Never raises."""
    wanted = _slug(company_id)
    if not wanted:
        return None
    for company in all_companies():
        if company.id == wanted:
            return company
    return None


def default_company_id() -> str:
    """``default_company:`` from companies.yaml, or the first enabled entry."""
    return _roster()[1]


def resolve_company(company_id: str | None = None) -> Company:
    """The company to use for a request that named ``company_id``, or the default.

    An unknown id resolves to the default rather than raising -- a stale
    bookmark or a company removed from the roster must not 500 the dashboard --
    but it is logged, because silently showing somebody another company's
    numbers is the worst possible outcome and the log is how it gets noticed.
    """
    if company_id:
        found = get_company(company_id)
        if found is not None:
            return found
        logger.warning(
            "unknown company %r; falling back to %r", company_id, default_company_id()
        )
    fallback = get_company(default_company_id())
    return fallback if fallback is not None else _fallback_company()


def resolve_company_id(company_id: str | None = None) -> str:
    """The id every query must filter on. Never None, never empty."""
    return resolve_company(company_id).id


def is_multi_company() -> bool:
    """True once more than one company is enabled.

    Drives the dashboard switcher: with a single tenant there is nothing to
    switch between, so the control stays off screen entirely.
    """
    return len(enabled_companies()) > 1


def company_choices() -> list[Company]:
    """Companies to offer in the switcher -- empty when there is only one."""
    companies = enabled_companies()
    return companies if len(companies) > 1 else []


def timezone_for(company_id: str | None = None) -> str | None:
    """This company's IANA zone, or None to mean "use the TZ variable"."""
    company = get_company(company_id) if company_id else get_company(active_company_id())
    return company.timezone if company is not None else None


# ==========================================================================
# Per-company rules
# ==========================================================================


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive mapping merge. Lists and scalars replace; mappings merge.

    A list replaces rather than concatenates on purpose: ``match_any`` and
    ``coverage.windows`` are ordered decision tables, and appending a tenant's
    entries to the global ones would change which rule fires first.
    """
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _company_rule_patch(company: Company) -> dict[str, Any]:
    """The company's overrides, expressed as a rules.yaml-shaped fragment."""
    patch: dict[str, Any] = {}
    missed: dict[str, Any] = {}
    if company.coverage is not None:
        missed["coverage"] = company.coverage
    if company.job_value_by_client is not None:
        missed["job_value_by_client"] = company.job_value_by_client
    if missed:
        patch["missed_work"] = missed
    if company.rules_overrides:
        patch = _deep_merge(patch, company.rules_overrides)
    return patch


def rules_for(company_id: str | None = None) -> dict[str, Any]:
    """config/rules.yaml with this company's overrides applied.

    See the module docstring for the precedence. Returns the global rules
    unchanged -- the same object shape ``get_rules()`` returns -- when the
    company declares no overrides, so a single-company install pays nothing for
    this and the two paths cannot diverge.
    """
    try:
        base = CONFIG.get_rules() or {}
    except ConfigError:
        base = {}

    company = resolve_company(company_id)
    patch = _company_rule_patch(company)
    if not patch:
        return base

    try:
        rules_digest = CONFIG.digest("rules")
    except ConfigError:
        rules_digest = ""
    stamp = f"{rules_digest}:{_digest()}"

    with _lock:
        cached = _cached_rules.get(company.id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        merged = _deep_merge(base, patch)
        _cached_rules[company.id] = (stamp, merged)
        return merged


# ==========================================================================
# The active company
# ==========================================================================

_active: ContextVar[str] = ContextVar("towbook_active_company", default="")


def active_company_id() -> str:
    """The company the current computation is about, or the default."""
    return _active.get() or default_company_id()


@contextmanager
def use_company(company_id: str | None) -> Iterator[str]:
    """Make ``company_id`` the active company for the duration of the block.

    Set around every pipeline run, every metrics computation and every
    dashboard request so that ``local_timezone_name()`` resolves this company's
    zone. Always restores the previous value, including on an exception, so one
    tenant's failure cannot leave the next one computing in the wrong timezone.
    """
    resolved = resolve_company_id(company_id)
    token = _active.set(resolved)
    try:
        yield resolved
    finally:
        _active.reset(token)
