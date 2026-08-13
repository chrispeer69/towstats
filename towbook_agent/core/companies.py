"""The tenant registry: which towing companies this install reports on.

The system began as one owner watching one Towbook account. It is now given or
sold to other US Tow Alliance members, so "which company is this number about"
has to be a first-class question rather than an assumption baked into a single
set of credentials.

    config/companies.yaml   the roster -- one entry per towing company
    requests.company_id     which company every stored row belongs to
    company_id filters      on every query, in every module

WHO MAY SEE WHICH COMPANY
-------------------------
The roster says which companies exist. :func:`use_visible_companies` says which
of them the current *reader* is allowed to see, and while it is set this whole
module behaves as though the roster contained only those:
:func:`enabled_companies`, :func:`company_choices`, :func:`get_company`,
:func:`default_company_id` and the merged scope are all restricted to it, so a
switcher cannot offer a company the reader may not open, and ``/company/<id>``
for somebody else's company resolves to one of their own instead.

It is set once per HTTP request from the signed-in user's record (see
``web/accounts.py`` and ``web/auth.py``) and is NEVER set for the scheduler,
the CLI or the pipeline -- those run unscoped, over every company on the
install, because a nightly run that quietly skipped a tenant is a tenant whose
board silently stops updating.

There is no billing and no self-serve signup: an operator creates the accounts.

AND THE SUM OF THEM
-------------------
Separate numbers, and -- when the operator asks for it by name -- their total.
:data:`MERGED_COMPANY_ID` is a READ SCOPE covering every enabled company at
once, offered in the dashboard switcher as soon as there are two. It exists
because several legal entities are usually one business: on this install
Roadside Towing holds the club accounts and Auto Lyft USA holds HONK's, with
one owner, one dispatch desk and one building between them.

It is not a company. It has no roster entry, no credentials and no Towbook
company to switch a session to; ``enabled_companies()`` excludes it, so the
scheduler never runs a pipeline for it. It stores nothing -- every writer calls
:func:`ensure_writable`, which refuses it -- and is recomputed from the members'
own rows on every read. Where those members disagree about a setting the
numbers depend on, :attr:`Company.conflicts` carries the sentence that says
whose setting was used, and the board prints it above the figures.

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
from typing import Any, Iterable, Iterator, Mapping

from .config_loader import CONFIG, ConfigError

__all__ = [
    "DEFAULT_COMPANY_ID",
    "MERGED_COMPANY_ID",
    "Company",
    "CompanyError",
    "NotWritable",
    "all_companies",
    "enabled_companies",
    "get_company",
    "resolve_company",
    "resolve_company_id",
    "default_company_id",
    "is_multi_company",
    "company_choices",
    "normalise_company_id",
    "merged_company",
    "is_merged_id",
    "company_ids_for",
    "ensure_writable",
    "rules_for",
    "timezone_for",
    "active_company_id",
    "use_company",
    "visible_company_ids",
    "use_visible_companies",
    "is_visible",
    "reload_companies",
]

logger = logging.getLogger(__name__)

#: The company id an install with no companies.yaml uses, and the value every
#: row written before this module existed already carries. Must stay equal to
#: ``core.models.DEFAULT_ACCOUNT_ID`` -- there is a test that asserts it.
DEFAULT_COMPANY_ID: str = "default"

#: The id of the MERGED scope -- every enabled company's work read as one book.
#:
#: One owner, one dispatch desk, two legal entities: Roadside Towing takes the
#: club work and Auto Lyft USA takes HONK's, and the question "how much work did
#: we turn away last week" is about both of them. So the switcher offers the
#: companies AND their sum.
#:
#: IT IS A READ SCOPE AND NOTHING ELSE. No row is ever stamped with it -- see
#: :func:`ensure_writable`, which every writer calls -- because a row filed
#: under ``__all__`` belongs to no company and would be counted twice by any
#: query that later sums the members. The merged view is computed from the
#: members' own rows, every time, and owns not one byte of stored state.
#:
#: Reserved: a roster entry claiming this id is rejected at parse time. The
#: underscores survive :func:`_slug` intact, so it stays recognisable in a URL
#: (``/company/__all__``) and cannot be produced by slugging a company name.
MERGED_COMPANY_ID: str = "__all__"

#: The id of the empty scope: a reader whose account names no company this
#: install actually has. It is not a company and never appears in the roster --
#: it exists so :func:`resolve_company` has something to return that is
#: guaranteed to match no stored row, giving that reader an empty board instead
#: of somebody else's. Reserved at parse time alongside :data:`MERGED_COMPANY_ID`.
_NO_COMPANY_ID: str = "__none__"

#: Ids a roster entry may not claim, because each already means something else.
_RESERVED_IDS: frozenset[str] = frozenset({MERGED_COMPANY_ID, _NO_COMPANY_ID})

#: Prefix for the per-company credential variables. ``credentials_env: ACME``
#: means TOWBOOK_ACME_USER / TOWBOOK_ACME_PASS.
_ENV_PREFIX = "TOWBOOK"

_ID_SAFE = re.compile(r"[^a-z0-9._-]+")
_ENV_SAFE = re.compile(r"[^A-Z0-9_]+")


class CompanyError(RuntimeError):
    """companies.yaml is present but cannot be read as a company roster."""


class NotWritable(CompanyError):
    """Something tried to write rows for the merged scope.

    The merged scope is a way of READING several companies at once. It owns no
    rows, and a row stamped ``__all__`` would belong to no company while being
    counted a second time by anything that sums the members. Every writer --
    acquisition, ingestion, the metrics upserts -- calls
    :func:`ensure_writable` first, so this is raised before anything reaches
    the datastore rather than discovered afterwards in the numbers.
    """


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
    #: Letterhead shown ONLY on the printed page -- address, phone, logo. See
    #: :meth:`letterhead_lines`. Per-company because a printout that goes to a
    #: client has to carry the name of the company whose numbers are on it, and
    #: on a multi-tenant install that is not whoever runs the server.
    letterhead: dict[str, Any] = field(default_factory=dict)
    #: The real company ids this record stands for. EMPTY on a real company --
    #: only the merged scope has members. See :data:`MERGED_COMPANY_ID`.
    members: tuple[str, ...] = ()
    #: Plain sentences naming what the members disagree about (staffed window,
    #: timezone, a client's job value). Empty when they agree, which is the
    #: normal case for one owner's two entities. Rendered on the merged view,
    #: because a merged figure computed over two different staffed windows is
    #: not wrong so much as unanswerable, and the reader has to be told which
    #: company's setting was used.
    conflicts: tuple[str, ...] = ()

    # -- what kind of scope is this ----------------------------------------

    @property
    def is_merged(self) -> bool:
        """True for the merged scope, false for a real towing company."""
        return bool(self.members)

    @property
    def member_ids(self) -> tuple[str, ...]:
        """The company ids to filter rows on. A real company filters on itself.

        THE ONE FUNCTION EVERY TENANT FILTER GOES THROUGH. A real company
        returns its own id and nothing else, so the merged scope cannot widen
        a query that was never meant to be widened.
        """
        return self.members or (self.id,)

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

    # -- letterhead --------------------------------------------------------

    @property
    def letterhead_name(self) -> str:
        """Trading name for the printed header. Falls back to :attr:`label`."""
        return str(self.letterhead.get("name") or "").strip() or self.label

    @property
    def letterhead_logo(self) -> str | None:
        """URL of the logo image, or None to print the name as a wordmark.

        A path, not an upload: the file is dropped into
        ``towbook_agent/web/static/`` and named here. There is no admin screen
        and no upload endpoint, because one image that changes once a year does
        not justify either.
        """
        value = str(self.letterhead.get("logo") or "").strip()
        if not value:
            return None
        return value if value.startswith(("/", "http://", "https://", "data:")) else f"/static/{value}"

    def letterhead_lines(self) -> list[str]:
        """Address block for the printed header, in reading order.

        Every field is optional and blanks are dropped, so a company that has
        supplied only a phone number prints a one-line letterhead rather than a
        block of empty space.
        """
        head = self.letterhead
        parts = [
            head.get("address"),
            head.get("address2"),
            " ".join(
                str(x).strip()
                for x in (head.get("city"), head.get("state"), head.get("zip"))
                if str(x or "").strip()
            ),
            head.get("phone"),
            head.get("email"),
            head.get("website"),
        ]
        return [str(p).strip() for p in parts if str(p or "").strip()]

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
            "is_merged": self.is_merged,
            "members": list(self.members),
            "conflicts": list(self.conflicts),
            # Printed header. Resolved here rather than in the template so the
            # fallbacks (name -> label, blank fields dropped) are decided once.
            "letterhead_name": self.letterhead_name,
            "letterhead_logo": self.letterhead_logo,
            "letterhead_lines": self.letterhead_lines(),
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


def normalise_company_id(value: Any) -> str:
    """The slugging rule for a company id, for callers outside this module.

    ``web/accounts.py`` stores company ids in a user's scope and the roster
    stores them in companies.yaml. Both have to agree about what "Auto Lyft"
    slugs down to or a scope would silently name a company that exists under a
    slightly different id, so there is one implementation and this is it.
    """
    return _slug(value)


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

    if company_id in _RESERVED_IDS:
        # Reserved. `__all__` is the merged scope: a real company holding it
        # would take over the id the switcher uses to mean "all of them", and
        # would be a company whose rows the merged read then counts twice.
        # `__none__` is the empty scope, and a real company holding it would be
        # shown to every reader entitled to no company at all.
        logger.error(
            "config/companies.yaml: entry #%d uses the reserved id %r; skipping it. "
            "Give this company its own id.",
            position + 1,
            company_id,
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
        letterhead=_as_mapping(entry.get("letterhead")) or {},
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
#: ``(stamp, merged)`` -- the assembled merged scope. Keyed on both digests
#: because it is built out of companies.yaml AND rules.yaml.
_cached_merged: tuple[str, Company] | None = None


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
    global _cached_digest, _cached_merged
    with _lock:
        _cached_digest = None
        _cached_rules.clear()
        # The merged scope is assembled from the roster, so a stale one would
        # keep answering for companies that are no longer in it.
        _cached_merged = None
    CONFIG.reload("companies")


# ==========================================================================
# The visibility scope -- which companies the current READER may see
#
# The roster is what this install reports on. The scope is what one signed-in
# person is allowed to look at, and it exists because the two stopped being the
# same thing the moment this system was sold to a company that is not the one
# that owns the server.
#
# UNSET IS UNSCOPED, AND UNSCOPED MEANS EVERYTHING. The scheduler, the CLI, the
# pipeline and the test suite never set it: a nightly run that skipped a tenant
# because of a permission model would be a tenant whose board silently stops
# updating, which is a worse failure than the one this guards against. Only the
# web layer sets it, once per request, from the signed-in user's record.
#
# WHILE IT IS SET, THE ROSTER LOOKS SMALLER THAN IT IS. Every lookup below
# filters through it, so there is no second code path to keep in step and no
# "did that endpoint remember to check" question to answer: a company outside
# the scope cannot be listed, cannot be resolved by id, cannot be reached by
# typing its slug into the URL, and is not a member of the merged view.
# ==========================================================================

#: The ids the current reader may see, or None for "every company". A tuple
#: rather than a set so the order the roster declares is preserved -- it is the
#: order the switcher renders and the order `default_company_id` picks from.
_visible: ContextVar[tuple[str, ...] | None] = ContextVar(
    "towbook_visible_companies", default=None
)


def visible_company_ids() -> tuple[str, ...] | None:
    """The ids the current reader may see, or ``None`` when unscoped."""
    return _visible.get()


@contextmanager
def use_visible_companies(company_ids: Iterable[str] | None) -> Iterator[tuple[str, ...] | None]:
    """Restrict every lookup in this module to ``company_ids`` for the block.

    ``None`` (or the sentinel ``"*"`` anywhere in the iterable) means unscoped
    -- the operator's own account, which sees the whole install. An EMPTY
    iterable is not the same thing and is not treated as one: it scopes the
    reader to nothing, and everything below then resolves to nothing rather
    than helpfully falling back to the roster. A user with no companies is a
    misconfiguration, and it must fail closed at the point of use.

    Always restores the previous value, including on an exception, so one
    request's scope cannot leak into the next request served by the thread.
    """
    if company_ids is None:
        scope: tuple[str, ...] | None = None
    else:
        wanted = [_slug(value) for value in company_ids]
        scope = None if "*" in wanted else tuple(value for value in wanted if value)
    token = _visible.set(scope)
    try:
        yield scope
    finally:
        _visible.reset(token)


def is_visible(company_id: str | None) -> bool:
    """May the current reader see this company?

    True for everything when unscoped. The merged scope is visible whenever the
    reader has more than one company to merge, since it is assembled from their
    own companies and nobody else's.
    """
    scope = _visible.get()
    if scope is None:
        return True
    wanted = _slug(company_id)
    if wanted == MERGED_COMPANY_ID:
        return len(scope) > 1
    return wanted in scope


# ==========================================================================
# Lookup
# ==========================================================================


def all_companies() -> list[Company]:
    """Every company in the roster, enabled or not, in file order.

    Filtered by the visibility scope when one is set -- this is the funnel
    :func:`enabled_companies` and :func:`get_company` both go through, so
    scoping it once is what makes every other lookup in this module obey it.
    """
    companies = list(_roster()[0])
    scope = _visible.get()
    if scope is None:
        return companies
    return [company for company in companies if company.id in scope]


def enabled_companies() -> list[Company]:
    """The companies the scheduler runs and the dashboard offers.

    Never empty WHEN UNSCOPED: if every entry is disabled the roster is treated
    as unconfigured, because a scheduler with nothing to do and a dashboard with
    no company to show are both silent failures.

    Under a visibility scope it CAN be empty, and the fallback company is
    deliberately not substituted: a reader scoped to companies that do not
    exist must see nothing, not the default tenant's numbers.
    """
    companies = [company for company in all_companies() if company.enabled]
    if companies:
        return companies
    if _visible.get() is not None:
        return []
    return [_fallback_company()]


def get_company(company_id: str | None) -> Company | None:
    """The company with this id, or None. Never raises.

    Answers for the merged scope too, but only on a multi-company install:
    with one company there is nothing to merge, and ``/company/__all__`` on a
    single-tenant board should fall back to that tenant rather than invent a
    second way of naming it.
    """
    wanted = _slug(company_id)
    if not wanted:
        return None
    if wanted == MERGED_COMPANY_ID:
        return merged_company() if is_multi_company() else None
    # `all_companies()` is already filtered by the visibility scope, so a
    # company the reader may not see is not findable by id -- typing another
    # tenant's slug into /company/<id> is indistinguishable from typing one
    # that does not exist.
    for company in all_companies():
        if company.id == wanted:
            return company
    return None


def default_company_id() -> str:
    """``default_company:`` from companies.yaml, or the first enabled entry.

    Under a visibility scope the roster default is only honoured when the
    reader can actually see it; otherwise it is their first visible company.
    Falling back to ``default_company`` for a reader not entitled to it is the
    one place this module could hand somebody another company's board by
    accident, so it is decided here rather than at each call site.
    """
    configured = _roster()[1]
    if _visible.get() is None or is_visible(configured):
        return configured
    visible = enabled_companies() or all_companies()
    return visible[0].id if visible else configured


def resolve_company(company_id: str | None = None) -> Company:
    """The company to use for a request that named ``company_id``.

    ``None`` resolves to the ACTIVE company -- the one :func:`use_company` is
    currently scoped to -- and only then to the roster default. That ordering
    is what makes the tenant filter safe by construction: a helper deep inside
    a computation that forgot to take a ``company_id`` argument still filters to
    the company the request is about, instead of quietly reading the default
    tenant's rows into another tenant's report.

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
            "unknown or not-permitted company %r; falling back to %r",
            company_id,
            active_company_id(),
        )
    fallback = get_company(active_company_id())
    if fallback is not None:
        return fallback
    # Nothing resolved. Unscoped, that means an install with no roster, and the
    # single-company fallback is right. SCOPED, it must never be: `default` is
    # a real company on this install and handing it to a reader who is not
    # entitled to it is the exact failure the scope exists to prevent. Their
    # own first company, or -- if they have none -- a company that holds
    # nothing, whose id matches no row and whose board is therefore empty.
    if _visible.get() is None:
        return _fallback_company()
    visible = enabled_companies()
    if visible:
        return visible[0]
    logger.error(
        "a reader is scoped to %r, none of which is an enabled company on this "
        "install; showing an empty board rather than another company's numbers",
        _visible.get(),
    )
    return Company(id=_NO_COMPANY_ID, name="No company", enabled=False, configured=False)


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
    """What the switcher offers -- empty when there is only one company.

    The real companies in roster order, then the merged scope last. Last
    because the tabs open on a single company: merged is the extra question
    ("and both together?"), not the default answer.
    """
    companies = enabled_companies()
    if len(companies) <= 1:
        return []
    return [*companies, merged_company()]


def timezone_for(company_id: str | None = None) -> str | None:
    """This company's IANA zone, or None to mean "use the TZ variable"."""
    company = get_company(company_id or active_company_id())
    return company.timezone if company is not None else None


# ==========================================================================
# The merged scope -- every company's work read as one book
#
# Why it exists: the two entities on this install are one business. Roadside
# Towing and Recovery holds the club accounts, Auto Lyft USA holds HONK's, and
# they share an owner, a dispatch desk and a building. "How much work did we
# turn away overnight" is a question about the pair, and answering it by
# reading two dashboards and adding up in your head is how a number gets
# fumbled.
#
# What it is NOT is a third company. It stores nothing, is never a write
# target, and never appears in `enabled_companies()` -- so the scheduler does
# not try to log into it, and no pipeline run is ever attributed to it. It is
# assembled from the members on every read.
#
# WHERE THE MEMBERS DISAGREE, IT SAYS SO. A staffed window is the headline of
# every report; two members with different windows have no single coverage
# figure between them. Rather than silently picking one, the merged scope uses
# the default company's setting and records a sentence in `conflicts` that the
# board prints above the numbers. On this install the members agree about
# everything except their letterheads, so nothing is printed.
# ==========================================================================


def is_merged_id(company_id: str | None) -> bool:
    """Is this the merged scope rather than a real towing company?"""
    return _slug(company_id) == MERGED_COMPANY_ID


def _merged_letterhead(members: list[Company]) -> dict[str, Any]:
    """Printed header for a document covering several companies.

    Names every entity the numbers belong to -- a printout that goes to a
    client or an insurer must not claim to be about one company when it is
    about two. The address block is carried over only when every member prints
    the same one; two different addresses under one heading is worse than none.
    """
    first = members[0].letterhead_lines()
    shared = first if all(m.letterhead_lines() == first for m in members) else []
    head: dict[str, Any] = {"name": " + ".join(m.letterhead_name for m in members)}
    if shared:
        # Rebuild from the member whose lines they are, so the field-by-field
        # structure (and its blank-dropping) is preserved rather than re-parsed.
        head.update({k: v for k, v in members[0].letterhead.items() if k != "name"})
    return head


def _agree(values: list[Any]) -> bool:
    """Do all the members say the same thing? Compared by value, not identity."""
    return all(value == values[0] for value in values[1:])


#: Coverage keys that describe the window to a READER rather than to the model.
#: Compared, they make two identical shifts look like a disagreement -- which is
#: exactly what happened here: both companies are staffed 06:00-18:00 Mon-Fri and
#: say so, in different words ("Owner's stated staffed window" against "Same
#: staffed window as Roadside"). A conflict banner nobody needs is a conflict
#: banner nobody reads.
_COVERAGE_PROSE = ("note", "description", "label")


def _coverage_shape(coverage: Any) -> Any:
    """A coverage block reduced to what actually decides covered/uncovered."""
    if not isinstance(coverage, Mapping):
        return coverage
    windows = coverage.get("windows")
    if isinstance(windows, (list, tuple)):
        windows = [
            {k: v for k, v in window.items() if k not in _COVERAGE_PROSE}
            if isinstance(window, Mapping)
            else window
            for window in windows
        ]
    return {
        **{k: v for k, v in coverage.items() if k not in (*_COVERAGE_PROSE, "windows")},
        "windows": windows,
    }


def _merged_rules_blocks(
    members: list[Company], default_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """``(coverage, job_value_by_client, conflicts)`` for the merged scope.

    Both are compared as EFFECTIVE values -- what each member actually reports
    on after rules.yaml and its own overrides -- not as the shorthand blocks in
    companies.yaml. A member that declares nothing and one that declares
    exactly the global default do not disagree, and must not be reported as
    disagreeing.
    """
    conflicts: list[str] = []
    effective = [(m, (rules_for(m.id).get("missed_work") or {})) for m in members]
    winner = next((m for m, _ in effective if m.id == default_id), members[0])

    coverages = [block.get("coverage") for _, block in effective]
    if _agree([_coverage_shape(value) for value in coverages]):
        coverage = coverages[0]
    else:
        coverage = (rules_for(winner.id).get("missed_work") or {}).get("coverage")
        conflicts.append(
            f"These companies declare different staffed windows, so there is no single "
            f"covered/uncovered split for them. The coverage figures below use "
            f"{winner.label}'s window."
        )

    # Job values: the union, because the members' clients barely overlap -- the
    # whole reason for the second entity is that HONK dispatches to it and to
    # nothing else. A client priced differently by two members has no one merged
    # value, so the default company's is used and the disagreement is named.
    values: dict[str, Any] = {}
    disputed: list[str] = []
    for member, block in effective:
        for client, value in (block.get("job_value_by_client") or {}).items():
            if client in values and values[client] != value:
                disputed.append(str(client))
                if member.id != winner.id:
                    continue
            values[client] = value
    if disputed:
        conflicts.append(
            f"{winner.label}'s job values were used for "
            f"{', '.join(sorted(set(disputed)))}, which the companies price differently."
        )
    return coverage, (values or None), conflicts


def _build_merged() -> Company:
    """Assemble the merged scope from the enabled roster."""
    members = enabled_companies()
    default_id = default_company_id()
    conflicts: list[str] = []

    zones = [m.timezone for m in members]
    if _agree(zones):
        timezone = zones[0]
    else:
        winner = next((m for m in members if m.id == default_id), members[0])
        timezone = winner.timezone
        conflicts.append(
            f"These companies keep different local times, so a day means a different "
            f"thing to each of them. Days below are {winner.label}'s ({winner.timezone})."
        )

    coverage, job_values, rule_conflicts = _merged_rules_blocks(members, default_id)
    conflicts.extend(rule_conflicts)

    return Company(
        id=MERGED_COMPANY_ID,
        name="All companies (merged)",
        towbook_company_id=None,
        credentials_env=None,
        timezone=timezone,
        enabled=True,
        coverage=coverage,
        job_value_by_client=job_values,
        rules_overrides={},
        configured=True,
        letterhead=_merged_letterhead(members),
        members=tuple(m.id for m in members),
        conflicts=tuple(conflicts),
    )


def merged_company() -> Company:
    """The merged scope, rebuilt whenever companies.yaml or rules.yaml changes.

    THE CACHE KEY CARRIES THE VISIBILITY SCOPE, and must. The merged scope is
    assembled from :func:`enabled_companies`, which is scoped, so two readers
    entitled to different companies have two different merged views. Keyed on
    the config digests alone, whichever of them loaded a page first would have
    served their total to the other -- a cache that hands one towing company a
    figure computed over another's jobs.
    """
    global _cached_merged
    try:
        rules_digest = CONFIG.digest("rules")
    except ConfigError:
        rules_digest = ""
    scope = _visible.get()
    stamp = f"{rules_digest}:{_digest()}:{'*' if scope is None else ','.join(scope)}"
    with _lock:
        if _cached_merged is not None and _cached_merged[0] == stamp:
            return _cached_merged[1]
    # Built outside the lock: it calls rules_for, which takes the same lock.
    built = _build_merged()
    with _lock:
        _cached_merged = (stamp, built)
    return built


def company_ids_for(company_id: str | None = None) -> tuple[str, ...]:
    """The company ids a query must filter on. NEVER empty, never "everything".

    A real company resolves to a one-element tuple holding its own id, so
    switching a filter from ``== resolve_company_id(x)`` to
    ``.in_(company_ids_for(x))`` cannot widen a query by accident: the merged
    scope is the only value that returns more than one id, and it is only ever
    reachable when the operator selected it.
    """
    return resolve_company(company_id).member_ids


def ensure_writable(company_id: str | None = None) -> str:
    """Return the id to stamp on rows, or refuse.

    Called by every writer. The merged scope is a read scope: a stored row, a
    metrics_daily entry or a run attributed to ``__all__`` would belong to no
    company at all, and would then be double-counted by every merged read that
    sums the members.
    """
    company = resolve_company(company_id)
    if company.is_merged:
        raise NotWritable(
            f"{company.label!r} ({MERGED_COMPANY_ID}) is a way of reading several companies "
            f"at once, not a company. Nothing can be pulled, ingested or computed INTO it -- "
            f"run the pipeline for each of {', '.join(company.members)} and the merged view "
            f"adds them up on every read."
        )
    return company.id


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


#: The two shorthand blocks, and where they land in rules.yaml. Both REPLACE
#: the global block outright instead of merging into it -- see :func:`rules_for`.
_REPLACED_BLOCKS: tuple[tuple[str, str, str], ...] = (
    ("coverage", "missed_work", "coverage"),
    ("job_value_by_client", "missed_work", "job_value_by_client"),
)


def _apply_replacements(merged: dict[str, Any], company: Company) -> dict[str, Any]:
    """Set the shorthand blocks wholesale, after everything else has merged.

    ``coverage`` and ``job_value_by_client`` REPLACE rather than merge, and
    that is the whole point of them. rules.yaml ships five clients' job values;
    a company that lists two would otherwise silently inherit the other three
    and price work it has never been offered. A staffed window is worse: merged
    key by key, a company that declares ``start: "12:00"`` would keep the
    global ``days`` and ``end``, and the coverage contrast -- the headline of
    every report -- would be measured against a shift nobody works.
    """
    for attribute, section, key in _REPLACED_BLOCKS:
        value = getattr(company, attribute)
        if value is None:
            continue
        block = dict(merged.get(section) or {})
        block[key] = value
        merged[section] = block
    return merged


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
    has_overrides = bool(
        company.rules_overrides
        or company.coverage is not None
        or company.job_value_by_client is not None
    )
    if not has_overrides:
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
        # Deep merge first, wholesale replacements second, so that a company
        # naming BOTH `coverage:` and a `rules:` block touching missed_work
        # still ends up with its own coverage rather than a half-merged one.
        merged = _deep_merge(base, company.rules_overrides)
        merged = _apply_replacements(merged, company)
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
