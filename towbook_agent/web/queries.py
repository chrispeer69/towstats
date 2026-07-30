"""Read-only query layer for the dashboard.

Everything the web views render comes from here. Three deliberate choices:

1. **The dashboard aggregates from ``requests``, not from the metrics tables.**
   ``metrics_hourly`` / ``metrics_daily`` / ``client_daily`` are the *reporting*
   artefacts -- they are what the SMS quoted at 14:00 and must never be
   retro-edited. The dashboard is the place an operator goes when a number looks
   wrong, so it recomputes from the source rows every time. Where a stored
   metric exists it is shown *alongside* the recomputation (see
   :func:`health_view`), which is exactly how you spot a stale aggregate.

2. **Aggregation happens in Python, not SQL.** Windows are bounded (a day, 30
   days, 8 weeks) so the row counts are small, and grouping by *local* hour or
   *local* calendar day is not expressible portably in SQL when the storage is
   naive UTC and the local zone has daylight saving. No pandas, no numpy --
   plain dicts and Counters, per the project constraints.

3. **A rate over zero offers is ``None``, never ``0.0``.** "We were offered
   nothing" and "we accepted nothing we were offered" are different facts. The
   templates render ``None`` as an em-dash.

Nothing in this module writes. The single write path in the whole web package
lives in :mod:`towbook_agent.web.rules_admin`.
"""

from __future__ import annotations

import logging
import os
import textwrap
from collections import Counter, defaultdict
from functools import wraps
from datetime import date as _date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ..agents import duplicates as _duplicates
from ..core import companies as _companies
from ..core import db as core_db
from ..core.config_loader import CONFIG, ConfigError
from ..core.logging_setup import redact
from ..core.models import (
    AlertFired,
    ClientDaily,
    MetricsDaily,
    MetricsHourly,
    MetricsWeekly,
    Request,
    Run,
    towbook_ref,
    towbook_ref_kind,
    towbook_ref_label,
)

__all__ = [
    "DEFAULT_TIMEZONE",
    "EMPTY_CLIENT_KEY",
    "local_tz",
    "now_local",
    "today_local",
    "to_local",
    "local_day_bounds",
    "local_span_bounds",
    "parse_date",
    "ratio",
    "rate_band",
    "rate_thresholds",
    "has_any_data",
    # -- multi-company ----------------------------------------------------
    "company_options",
    "current_company",
    "for_company",
    # -- the four board tabs (HOURLY / WEEKLY / MONTHLY / TRENDS) ---------
    "hourly_snapshot",
    "period_snapshot",
    "period_bounds",
    "PERIOD_KINDS",
    "basis_note",
    "bucket_of",
    "coverage_of",
    "rules_snapshot",
    "uncovered_label",
    "live_snapshot",
    "daily_snapshot",
    "clients_overview",
    "client_detail",
    "trends_snapshot",
    "rules_view",
    "health_view",
    "sparkline_points",
    # -- the board IS the delivery mechanism; this is the alarm --------------
    "pipeline_banner",
    "last_recovery",
    "PIPELINE_FAILURE_BANNER_HOURS",
    # -- the missed-work model (MISSED_WORK_MODEL.md) --------------------
    "missed_work_snapshot",
    "blind_spots_snapshot",
    "closeoff_snapshot",
    "client_no_response",
    "coverage_rows",
    "missed_work_window",
    "miss_band",
    "RANKING_NOTE",
    "RESPONSE_WINDOW",
    "HEADLINE_BUCKETS",
    "MISSED_WORK_WINDOW_DAYS",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Detroit"


# ==========================================================================
# Multi-company
# ==========================================================================
#
# EVERY read in this module is scoped to one towing company. Two mechanisms,
# and they are belt and braces on purpose because a missing tenant filter is
# not a wrong number on a screen -- it is one company's customers, volumes and
# refusal reasons shown to another company.
#
#   1. :func:`for_company` decorates every public entry point. It resolves the
#      `company_id` / `account_id` argument pair and ACTIVATES that company for
#      the duration of the call.
#   2. Every statement that touches the datastore adds an explicit
#      `company_id ==` clause, defaulting to the active company. There is no
#      code path that reads rows for "all companies": `company_id=None` means
#      the active one, never everything.
#
# Activating rather than only passing the id also fixes the timezone: a Texas
# tenant's "today" is not an Ohio tenant's, and `local_tz()` below reads the
# active company's zone.


def for_company(func):
    """Scope a public query to one company, accepting either argument name.

    ``company_id`` is the current name and ``account_id`` the one the
    single-company build used; whichever was given wins, and the resolved id is
    passed on so the wrapped function can still filter explicitly.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        legacy = kwargs.pop("account_id", None)
        if kwargs.get("company_id") is None and legacy is not None:
            kwargs["company_id"] = legacy
        resolved = _companies.resolve_company_id(kwargs.get("company_id"))
        kwargs["company_id"] = resolved
        with _companies.use_company(resolved):
            return func(*args, **kwargs)

    return wrapper


def current_company() -> dict[str, Any]:
    """The company the current request is about, as template-safe data."""
    return _companies.resolve_company(None).as_dict()


def company_options() -> list[dict[str, Any]]:
    """Companies to offer in the dashboard switcher.

    EMPTY when only one company is configured. A switcher with a single option
    is noise on every page of a single-company install, so it is not rendered
    at all until there is a second tenant to switch to.
    """
    return [company.as_dict() for company in _companies.company_choices()]


def company_rules() -> dict[str, Any]:
    """config/rules.yaml with the active company's overrides applied."""
    try:
        return dict(_companies.rules_for(None) or {})
    except Exception:
        return {}

#: Sentinel used in URLs for a request whose client_name was blank. A real
#: client_key is a casefolded name, so it can never collide with this.
EMPTY_CLIENT_KEY = "_unnamed"

#: Fallback thresholds for the good / warn / bad rate colouring. Overridable
#: from config/rules.yaml without a code change:
#:
#:     dashboard:
#:       rate_thresholds: {good: 0.80, warn: 0.65}
DEFAULT_RATE_THRESHOLDS = {"good": 0.75, "warn": 0.60}

#: Canonical status vocabulary, in the order the dashboard displays it.
STATUS_ORDER = ("accepted", "denied", "expired", "canceled", "pending")

_UTC = timezone.utc


# ==========================================================================
# Time
# ==========================================================================


def local_tz() -> ZoneInfo:
    """The presentation timezone: the active company's, then TZ, then Detroit.

    Per company, because a coverage window is a claim about when somebody was
    at a desk and an Ohio shift read in a Texas clock is a different claim.

    Storage is naive UTC; every date, hour bucket and calendar day the operator
    sees is local. Falls back to UTC if the configured zone is unknown so a bad
    TZ value degrades instead of taking the dashboard down.
    """
    name = (
        (_companies.timezone_for() or "").strip()
        or (os.environ.get("TZ") or "").strip()
        or DEFAULT_TIMEZONE
    )
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - depends on the tzdata install
        try:
            return ZoneInfo(DEFAULT_TIMEZONE)
        except Exception:
            return ZoneInfo("UTC")


def to_local(value: datetime | None) -> datetime | None:
    """Naive-UTC storage value -> aware local datetime."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(local_tz())


def now_local() -> datetime:
    return datetime.now(_UTC).astimezone(local_tz())


def today_local() -> _date:
    return now_local().date()


def local_day_bounds(day: _date) -> tuple[datetime, datetime]:
    """Return the naive-UTC half-open ``[start, end)`` covering a local day.

    DST-correct: a 23 or 25 hour local day produces a 23 or 25 hour UTC window,
    because both endpoints are converted independently.
    """
    return local_span_bounds(day, day)


def local_span_bounds(first_day: _date, last_day: _date) -> tuple[datetime, datetime]:
    """Naive-UTC window covering local days ``first_day`` .. ``last_day`` inclusive."""
    tz = local_tz()
    start_local = datetime.combine(first_day, time.min, tzinfo=tz)
    end_local = datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=tz)
    return (
        start_local.astimezone(_UTC).replace(tzinfo=None),
        end_local.astimezone(_UTC).replace(tzinfo=None),
    )


def parse_date(raw: str | None, default: _date | None = None) -> _date:
    """Parse ``YYYY-MM-DD`` from a query string, falling back on anything odd."""
    if raw:
        try:
            return _date.fromisoformat(raw.strip()[:10])
        except ValueError:
            pass
    return default if default is not None else today_local()


def week_start_for(day: _date) -> _date:
    """Monday of the week containing ``day``."""
    return day - timedelta(days=day.weekday())


# ==========================================================================
# Rates
# ==========================================================================


def ratio(numerator: int, denominator: int) -> float | None:
    """Acceptance rate, or ``None`` when nothing was offered.

    Hard rule for the whole dashboard: no offers means no rate. Rendering that
    as 0% would tell the owner he declined work he was never sent.
    """
    if not denominator:
        return None
    return numerator / denominator


def rate_thresholds() -> dict[str, float]:
    """Good / warn cut-offs, from this company's rules if it overrides them.

    A different market has a different idea of a good acceptance rate, so
    ``dashboard.rate_thresholds`` is one of the blocks a company entry in
    config/companies.yaml may override.
    """
    thresholds = dict(DEFAULT_RATE_THRESHOLDS)
    try:
        configured = (company_rules().get("dashboard") or {}).get("rate_thresholds") or {}
    except Exception:
        configured = {}
    for key in ("good", "warn"):
        value = configured.get(key)
        if isinstance(value, (int, float)):
            thresholds[key] = float(value)
    return thresholds


def rate_band(value: float | None) -> str:
    """Map a rate to a CSS band: ``good`` / ``warn`` / ``bad`` / ``none``."""
    if value is None:
        return "none"
    thresholds = rate_thresholds()
    if value >= thresholds["good"]:
        return "good"
    if value >= thresholds["warn"]:
        return "warn"
    return "bad"


def _delta(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None:
        return None
    return current - baseline


# ==========================================================================
# Row access
# ==========================================================================

_REQUEST_COLUMNS = (
    Request.request_id,
    Request.company_id,
    # The Towbook call number, when the offer ever became a job. Selected on
    # every row read because "which job was that?" is the first question asked
    # of any line in these tables -- and because it is NULL on most of the
    # not-accepted rows, which is why :func:`_row_to_dict` pairs it with the
    # request id rather than showing an empty cell.
    Request.job_number,
    Request.client_name,
    Request.client_key,
    Request.offered_at,
    Request.responded_at,
    Request.status,
    # The two columns the missed-work model buckets on. `status` is a
    # five-value controlled vocabulary and collapses "Expired" onto "Accept
    # Failed"; the raw label and the numeric code keep them apart. Selected
    # here so the dashboard can bucket a row exactly the way the agent does
    # instead of approximating it -- see :func:`bucket_of`.
    Request.status_raw,
    Request.status_code,
    Request.denial_reason,
    Request.service_type_raw,
    Request.service_class,
    # The customer's car. Selected on every row because it is the key the
    # duplicate rule matches a re-broadcast offer on -- this feed carries no
    # customer name -- and because a collapsed row has to be able to say which
    # car it was.
    Request.vehicle,
    Request.pickup_location,
    # The ZIP the API states as its own field. Territory is decided on it -- see
    # :func:`territory_of` -- with pickup_location as the fallback for
    # CSV-ingested rows, which carry no ZIP.
    Request.pickup_zip,
    Request.dropoff_location,
    Request.truck_assigned,
    Request.driver_assigned,
    Request.amount,
    Request.source_run_id,
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    amount = data.get("amount")
    if isinstance(amount, Decimal):
        data["amount"] = float(amount)
    # The stored value, before the display default is applied. A NULL status is
    # a status the ingester could not read, and bucketing that as "pending"
    # would report an unreadable row as "still deciding" forever -- exactly the
    # trap agents/missed_work.py documents. Bucketing reads this key; every
    # display path reads `status` below.
    data["status_stored"] = (data.get("status") or "").strip().casefold()
    data["status"] = (data.get("status") or "pending").strip().casefold() or "pending"
    data["service_class"] = (data.get("service_class") or "unclassified").strip() or "unclassified"
    data["client_key"] = (data.get("client_key") or "").strip()
    # The reference the owner quotes for this offer. The job number when Towbook
    # issued one, the Digital Request id when it did not -- which is the usual
    # case for anything we did not accept. See core/models.towbook_ref.
    job_number = (data.get("job_number") or "").strip() or None
    data["job_number"] = job_number
    data["towbook_ref"] = towbook_ref(job_number, data.get("request_id"))
    data["towbook_ref_kind"] = towbook_ref_kind(job_number)
    data["towbook_ref_label"] = towbook_ref_label(job_number, data.get("request_id"))
    data["offered_local"] = to_local(data.get("offered_at"))
    data["responded_local"] = to_local(data.get("responded_at"))
    return data


def fetch_requests(
    start_utc: datetime,
    end_utc: datetime,
    company_id: str | None = None,
    client_key: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """One company's requests offered in ``[start_utc, end_utc)``, newest last.

    THE TENANT FILTER IS NOT OPTIONAL. ``company_id=None`` means the active
    company (see :func:`for_company`), never "every company". Every view in
    this module reads its rows through here, so this is the one place the
    filter could go missing -- and it cannot.

    DUPLICATE OFFERS ARE COLLAPSED, exactly as they are for the agents: one
    job a club asked for three times is one row, carrying ``duplicate_count``
    and the references of the offers it stands for. Every dashboard view reads
    its rows through here, so the board and the emailed report cannot disagree
    about how many jobs a day held -- which they would the moment one of them
    counted a re-broadcast twice. :func:`duplicates.summarize` rebuilds the
    "N collapsed" line from the returned rows.

    Rows with a NULL ``offered_at`` cannot be placed on a timeline and are
    excluded here; :func:`health_view` counts them so they are never invisible.
    """
    statement = (
        select(*_REQUEST_COLUMNS)
        .where(Request.company_id == _companies.resolve_company_id(company_id))
        .where(Request.offered_at.is_not(None))
        .where(Request.offered_at >= start_utc)
        .where(Request.offered_at < end_utc)
        .order_by(Request.offered_at.asc())
    )
    if client_key is not None:
        statement = statement.where(Request.client_key == client_key)
    if limit:
        statement = statement.limit(limit)

    with core_db.get_session(commit=False) as session:
        rows = [_row_to_dict(row) for row in session.execute(statement).mappings()]

    collapsed, _ = _duplicates.collapse(rows, company_rules())
    return collapsed


def has_any_data(company_id: str | None = None) -> bool:
    """True once THIS COMPANY has a single request row. Drives the empty state.

    Per company, so a newly added tenant sees "run the pipeline" rather than a
    page of zeros that looks like a bad day.
    """
    try:
        with core_db.get_session(commit=False) as session:
            return bool(
                session.execute(
                    select(func.count())
                    .select_from(Request)
                    .where(
                        Request.company_id == _companies.resolve_company_id(company_id)
                    )
                ).scalar_one()
            )
    except Exception:
        return False


def companies_with_data() -> list[str]:
    """Company ids that actually have rows. Diagnostics only, never a filter.

    Used by /health to show an operator that a company in the roster has never
    produced a request -- usually a credential prefix that was never set.
    """
    try:
        with core_db.get_session(commit=False) as session:
            return [
                row[0]
                for row in session.execute(
                    select(Request.company_id).distinct().order_by(Request.company_id)
                )
                if row[0]
            ]
    except Exception:
        return []


# ==========================================================================
# Bucketing helpers
# ==========================================================================


class Bucket:
    """A counted set of requests. The unit every view is assembled from."""

    __slots__ = ("offered", "accepted", "denied", "expired", "canceled", "pending", "amount")

    def __init__(self) -> None:
        self.offered = 0
        self.accepted = 0
        self.denied = 0
        self.expired = 0
        self.canceled = 0
        self.pending = 0
        self.amount = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.offered += 1
        status = row["status"]
        if status in STATUS_ORDER:
            setattr(self, status, getattr(self, status) + 1)
        else:  # a status outside the vocabulary still counts as an offer
            self.pending += 1
        amount = row.get("amount")
        if isinstance(amount, (int, float)):
            self.amount += float(amount)

    @property
    def decided(self) -> int:
        return self.offered - self.pending

    @property
    def rate(self) -> float | None:
        return ratio(self.accepted, self.offered)

    def as_dict(self) -> dict[str, Any]:
        return {
            "offered": self.offered,
            "accepted": self.accepted,
            "denied": self.denied,
            "expired": self.expired,
            "canceled": self.canceled,
            "pending": self.pending,
            "decided": self.decided,
            "rate": self.rate,
            "band": rate_band(self.rate),
            "amount": round(self.amount, 2),
        }


def _bucket_by(rows: Iterable[dict[str, Any]], key_fn) -> dict[Any, Bucket]:
    buckets: dict[Any, Bucket] = defaultdict(Bucket)
    for row in rows:
        buckets[key_fn(row)].add(row)
    return buckets


def _totals(rows: Iterable[dict[str, Any]]) -> Bucket:
    bucket = Bucket()
    for row in rows:
        bucket.add(row)
    return bucket


# ==========================================================================
# Denial reasons
# ==========================================================================


def _normalization_rules() -> list[dict[str, Any]]:
    try:
        raw = company_rules().get("denial_reason_normalization") or []
    except Exception:
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def normalize_denial_reason(raw_reason: str | None) -> str:
    """Bucket a free-text denial reason at read time.

    Semantics match ``service_classes``: case-insensitive substring, list order
    is evaluation order, first match wins. ``denial_reason_normalization``
    ships empty on purpose, and while it is empty every distinct raw string is
    its own bucket -- which is precisely how an operator discovers what the
    buckets should be. Once rules exist, anything unmatched falls into
    ``other``.
    """
    reason = (raw_reason or "").strip()
    if not reason:
        return "(none given)"
    rules = _normalization_rules()
    if not rules:
        return reason
    haystack = reason.casefold()
    for entry in rules:
        for needle in entry.get("match_any") or []:
            if str(needle).strip().casefold() in haystack:
                return str(entry.get("normalized") or "other")
    return "other"


def denial_mix(rows: Iterable[dict[str, Any]], top: int = 5) -> list[dict[str, Any]]:
    """Top denial reasons among denied rows, with a share of the total."""
    counter: Counter[str] = Counter()
    for row in rows:
        if row["status"] == "denied":
            counter[normalize_denial_reason(row.get("denial_reason"))] += 1
    total = sum(counter.values())
    mix = []
    for reason, count in counter.most_common(top):
        mix.append(
            {
                "reason": reason,
                "count": count,
                "share": ratio(count, total),
            }
        )
    return mix


# ==========================================================================
# Acceptance policy
# ==========================================================================


def policy_map() -> dict[str, str]:
    """``{service_class: "accept"|"decline"}`` derived from rules.yaml.

    Two data sources are merged because rules.yaml expresses the same intent
    twice: an ``accept:`` flag on a service class, and the explicit
    ``acceptance_policy.should_accept`` / ``should_decline`` lists. Either one
    is enough; the explicit lists win on conflict.
    """
    mapping: dict[str, str] = {}
    try:
        rules = company_rules()
    except Exception:
        return mapping

    for name, body in (rules.get("service_classes") or {}).items():
        if str(name).startswith("_") or not isinstance(body, dict):
            continue
        if "accept" in body:
            mapping[str(name)] = "accept" if bool(body["accept"]) else "decline"

    policy = rules.get("acceptance_policy") or {}
    for verdict, key in (("accept", "should_accept"), ("decline", "should_decline")):
        for entry in policy.get(key) or []:
            if isinstance(entry, dict) and entry.get("service_class"):
                mapping[str(entry["service_class"])] = verdict
    return mapping


def policy_variance(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Where reality disagreed with the written acceptance policy.

    ``missed``  -- a class the policy says take, that was denied or expired.
    ``against`` -- a class the policy says decline, that was accepted.
    """
    mapping = policy_map()
    missed: list[dict[str, Any]] = []
    against: list[dict[str, Any]] = []
    covered = 0
    for row in rows:
        verdict = mapping.get(row["service_class"])
        if verdict is None:
            continue
        covered += 1
        if verdict == "accept" and row["status"] in ("denied", "expired"):
            missed.append(row)
        elif verdict == "decline" and row["status"] == "accepted":
            against.append(row)
    return {
        "policy": mapping,
        "missed": missed,
        "against": against,
        "missed_count": len(missed),
        "against_count": len(against),
        "covered": covered,
        "has_policy": bool(mapping),
    }


# ==========================================================================
# Sparklines
# ==========================================================================


def sparkline_points(values: Sequence[float], width: float = 112.0, height: float = 26.0) -> str:
    """SVG ``points`` attribute for a volume sparkline.

    Returned as a string so the template owns the markup. An all-zero or
    single-point series renders as a flat baseline rather than dividing by zero.
    """
    if not values:
        return ""
    peak = max(values)
    span = max(len(values) - 1, 1)
    pad = 2.0
    usable = max(height - (pad * 2), 1.0)
    coords = []
    for index, value in enumerate(values):
        x = (index / span) * width if len(values) > 1 else width / 2
        fraction = (value / peak) if peak else 0.0
        y = pad + (1.0 - fraction) * usable
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords)


# ==========================================================================
# View 0 -- Missed work (the primary view)
# ==========================================================================
#
# The owner's question is not "what did we accept" but "what did we NOT
# accept, do we want it, and what would it take to get it". So the dashboard
# leads with the inventory of missed work and treats acceptance rate as
# supporting context. MISSED_WORK_MODEL.md is the spec.
#
# NOTHING HERE REIMPLEMENTS THE MODEL. Bucketing, cause attribution, the
# inventory, the blind-spot grid and the close-off candidates are computed by
# agents/missed_work.py -- the same code, over the same rules.yaml, that
# produces the daily and weekly reports. The dashboard calls it with
# ``persist=False`` and reshapes the result for the templates. If the two ever
# disagreed the operator would have no way to tell which was lying, so they are
# not allowed to be two implementations.
#
# Three consequences worth knowing:
#
#   * it is recomputed on every page load, from ``requests``, exactly like the
#     rest of this module (see the module docstring) -- not read back from
#     ``metrics_missed_work``;
#   * ``persist=False`` keeps the read-only guarantee: the session is opened
#     with ``commit=False`` inside the agent and nothing is written;
#   * if agents/missed_work.py is missing or raises, every one of these
#     functions returns an envelope with ``available: False`` and an error
#     string. The views render an explanation instead of a 500.

#: The window every missed-work view reports on. Thirty local days ending
#: today, matching :func:`clients_overview` so the two pages agree.
MISSED_WORK_WINDOW_DAYS = 30

#: The disclaimer that must appear on every ranked list in the UI.
#: ``offerAmount`` is empty on 100% of the records this account produces
#: (3,079 of 3,079 over 30 days -- TOWBOOK_PORTAL_FACTS.md section 4), so every
#: figure the dashboard shows is a job count. Nothing here is money.
RANKING_NOTE = (
    "Ranked by job count - offer amounts are not populated by Towbook."
)

#: How long Towbook actually gives us to answer, measured over 30 days of this
#: account's real offers. It is NOT recomputed from the table below it: the
#: JSON feed carries no response timestamp at all (``responded_at`` has no
#: source field -- TOWBOOK_PORTAL_FACTS.md section 4), so this is a measured
#: constant and every screen that shows it says so.
RESPONSE_WINDOW = {
    "median_minutes": 3,
    "mean_minutes": 4,
    "max_minutes": 15,
    "note": (
        "Measured across 30 days of this account's offers. Towbook publishes no "
        "response timestamp, so this is not recomputed from the rows below."
    ),
}

#: The four numbers the primary view leads with: bucket key in the missed-work
#: document, the label, the key it appears under in ``totals``, and the sentence
#: that says what it means. Order is the reading order on screen.
HEADLINE_BUCKETS: tuple[tuple[str, str, str, str], ...] = (
    ("won", "Accepted", "accepted", "We took the job."),
    ("no_response", "Never responded", "no_response", "Nobody answered in time."),
    ("declined", "Declined", "declined", "We said no, with a reason."),
    ("client_withdrew", "Client withdrew", "withdrew", "The client pulled it."),
)

#: Buckets that are neither a win nor one of the four headlines, shown small.
SECONDARY_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ("in_flight", "Still deciding", "in_flight"),
    ("accept_failed", "Accept failed", "accept_failed"),
    ("unknown_status", "Unreadable status", "unknown_status"),
)


def miss_band(value: float | None, threshold: float = 0.35) -> str:
    """CSS band for a *miss* rate, where high is bad.

    The inverse of :func:`rate_band`, and deliberately a separate function: a
    35% acceptance rate and a 35% no-response rate are opposite news, and one
    colouring function cannot mean both. ``None`` is still ``none``, never a
    zero -- an hour with no offers has no miss rate.
    """
    if value is None:
        return "none"
    try:
        value = float(value)
        threshold = float(threshold)
    except (TypeError, ValueError):
        return "none"
    if value >= threshold:
        return "bad"
    if value >= threshold * 0.6:
        return "warn"
    return "good"


def _missed_work_module():
    """Import agents.missed_work, or ``None`` if it is not available.

    Mirrors ``agents.metrics._missed_work_module``: the inventory is the most
    valuable thing on the dashboard and also the newest code in the system. A
    failure to import it must degrade this page, not take the whole dashboard
    down with it.
    """
    try:
        from ..agents import missed_work as module
    except Exception as exc:  # pragma: no cover - import failure is environmental
        logger.warning("agents.missed_work unavailable: %s", exc)
        return None
    return module


def _notifier_module():
    """Import agents.notifier, or ``None``. Same contract as above."""
    try:
        from ..agents import notifier as module
    except Exception as exc:  # pragma: no cover - import failure is environmental
        logger.warning("agents.notifier unavailable: %s", exc)
        return None
    return module


def basis_note(revenue_available: bool = False) -> str:
    """What unit the numbers on a screen are in, in one sentence.

    Delegates to ``agents/notifier.py -> ranking_note()`` so the board and the
    reports cannot end up making different claims about the same figures. That
    helper returns one of two true sentences: "these are jobs, not dollars"
    while no average job values are configured, and "these dollars are
    estimates from rules.yaml, gross not margin" once they are.

    Falls back to :data:`RANKING_NOTE` if the notifier cannot be imported --
    a caption is never allowed to be the reason a page fails to render, but a
    ranked list with no stated unit is never shipped either.
    """
    module = _notifier_module()
    if module is None or not hasattr(module, "ranking_note"):
        return RANKING_NOTE
    try:
        return str(module.ranking_note(bool(revenue_available)))
    except Exception:  # pragma: no cover - defensive
        return RANKING_NOTE


def rules_snapshot() -> dict[str, Any]:
    """One read of rules.yaml, to be reused across a whole page render.

    ``CONFIG`` re-stats the file on every access, which is the right behaviour
    for a long-lived scheduler and the wrong one inside a loop over three
    thousand rows. Callers that classify rows take a snapshot once and pass it
    down, exactly as ``compute_missed_work`` does for the same reason.
    """
    return company_rules()


def bucket_of(row: dict[str, Any], rules: dict[str, Any] | None = None) -> str:
    """The missed-work bucket for one fetched request row.

    Calls ``agents.missed_work.classify_bucket`` rather than re-deriving
    anything: the dashboard and the reports must never disagree about whether
    an offer was declined or nobody answered it. Returns ``""`` when the model
    is unavailable, and every caller renders that as "unknown", not as zero.
    """
    module = _missed_work_module()
    if module is None:
        return ""
    try:
        return str(
            module.classify_bucket(
                {
                    "status_code": row.get("status_code"),
                    "status_raw": row.get("status_raw"),
                    "status": row.get("status_stored") or row.get("status"),
                },
                rules if rules is not None else None,
            )
        )
    except Exception:  # pragma: no cover - defensive
        return ""


def coverage_of(row: dict[str, Any], rules: dict[str, Any] | None = None) -> str:
    """Which operating window an offer arrived in, or the uncovered label."""
    module = _missed_work_module()
    if module is None:
        return ""
    try:
        return str(
            module.coverage_label(
                {"offered_at_local": row.get("offered_local")},
                rules if rules is not None else None,
            )
        )
    except Exception:  # pragma: no cover - defensive
        return ""


def territory_of(row: dict[str, Any], rules: dict[str, Any] | None = None) -> str:
    """Which service-area band an offer's pickup falls in, or the outside label.

    Same contract as :func:`coverage_of`: delegates to the missed-work model so
    the board and the reports can never disagree about whether a job was even
    in this company's area.
    """
    module = _missed_work_module()
    if module is None:
        return ""
    try:
        return str(
            module.territory_band(
                {
                    "pickup_zip": row.get("pickup_zip"),
                    "pickup_location": row.get("pickup_location"),
                },
                rules if rules is not None else None,
            )
        )
    except Exception:  # pragma: no cover - defensive
        return ""


def is_addressable(row: dict[str, Any], rules: dict[str, Any] | None = None) -> bool:
    """Was this offer ours to take at all?

    Two independent reasons an offer is not, and neither is a failure the owner
    should be judged on:

    * **Out of territory.** Outside the service area in
      ``missed_work.territory``. The clubs send them anyway.
    * **Declined by written policy.** A service class this company has decided
      it does not do -- ``acceptance_policy.should_decline``, which is light
      service here. 431 offers a month producing 13 jobs.

    Together those are 18.8% of everything this company is offered, and folding
    them into one acceptance rate is what makes an honest operation read as a
    bad one.

    Anything we cannot place counts as addressable. Same direction as
    ``territory_band``: the cost of wrongly excluding real work is far higher
    than the cost of including a little noise.
    """
    if str(row.get("service_class") or "") in _declined_classes():
        return False
    band = territory_of(row, rules)
    return band != outside_territory_label()


def _declined_classes() -> set[str]:
    """Service classes the written policy says we do NOT want. Config, not code."""
    return {name for name, verdict in policy_map().items() if verdict == "decline"}


def outside_territory_label(rules: dict[str, Any] | None = None) -> str:
    """The label for "not in our area". Config, never hardcoded."""
    module = _missed_work_module()
    default = "outside"
    if module is None:
        return default
    try:
        return str(getattr(module, "OUTSIDE_TERRITORY_LABEL", default) or default)
    except Exception:  # pragma: no cover - defensive
        return default


def uncovered_label(rules: dict[str, Any] | None = None) -> str:
    """The label used for "nobody was rostered". Config, never hardcoded."""
    module = _missed_work_module()
    default = "uncovered"
    if module is None:
        return default
    try:
        return str(getattr(module, "UNCOVERED_LABEL", default) or default)
    except Exception:  # pragma: no cover - defensive
        return default


def missed_work_window(days: int = MISSED_WORK_WINDOW_DAYS) -> dict[str, Any]:
    """The local, half-open window every missed-work view reports on.

    ``[first day 00:00, tomorrow 00:00)`` in local time -- the agent takes local
    naive datetimes and does its own conversion, so no UTC arithmetic happens
    here and the two modules cannot drift about what "Tuesday" means.
    """
    days = max(1, min(int(days or MISSED_WORK_WINDOW_DAYS), 365))
    today = today_local()
    first = today - timedelta(days=days - 1)
    return {
        "days": days,
        "first_day": first,
        "last_day": today,
        "start_local": datetime.combine(first, time.min),
        "end_local": datetime.combine(today + timedelta(days=1), time.min),
    }


def _missed_work_call(
    name: str, days: int, company_id: str | None
) -> dict[str, Any]:
    """Run one public agents.missed_work entry point over the standard window.

    Returns an envelope -- ``available`` / ``error`` / ``document`` -- rather
    than raising or returning ``None``, so every template can render the same
    "why is this empty" block without knowing which failure happened.
    """
    window = missed_work_window(days)
    envelope: dict[str, Any] = {
        "available": False,
        "error": None,
        "document": {},
        "days": window["days"],
        "first_day": window["first_day"],
        "last_day": window["last_day"],
        "ranking_note": RANKING_NOTE,
        "response_window": RESPONSE_WINDOW,
    }

    module = _missed_work_module()
    if module is None:
        envelope["error"] = (
            "agents/missed_work.py could not be imported, so the missed-work "
            "model cannot be computed."
        )
        return envelope

    function = getattr(module, name, None)
    if function is None:  # pragma: no cover - defensive
        envelope["error"] = f"agents.missed_work has no {name}()"
        return envelope

    kwargs: dict[str, Any] = {"company_id": _companies.resolve_company_id(company_id)}
    if name == "compute_missed_work":
        # persist=False is what keeps this module read-only. The agent opens its
        # own session with commit=False, so nothing reaches the datastore.
        kwargs["persist"] = False
        kwargs["period_type"] = "custom"

    try:
        envelope["document"] = function(
            None, window["start_local"], window["end_local"], None, **kwargs
        )
        envelope["available"] = True
    except Exception as exc:
        logger.exception("missed_work.%s failed", name)
        envelope["error"] = redact(f"{type(exc).__name__}: {exc}")
    return envelope


def _slug_for(client_key: Any) -> str:
    return str(client_key or "").strip() or EMPTY_CLIENT_KEY


def _linkable_clients(entries: Any) -> list[dict[str, Any]]:
    """Add a URL slug to the agent's ``top_clients`` lists."""
    out: list[dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        row["slug"] = _slug_for(row.get("client_key"))
        out.append(row)
    return out


def coverage_rows(coverage: dict[str, Any]) -> dict[str, Any]:
    """The covered-vs-uncovered comparison, in the shape the template renders.

    Two rows and one contrast sentence. The uncovered row is the headline
    because that is where essentially all of the recoverable work sits; the
    covered row is not optional decoration beside it, it is the control -- same
    trucks, comparable volume, 5.8% missed against 61.7%. Showing one without
    the other turns evidence into an assertion.

    The uncovered row is emitted FIRST so the reading order matches the
    argument, which is the reverse of the config's declaration order.
    """
    if not coverage:
        return {"available": False, "rows": [], "contrast": {}, "headline": {}}

    default_label = str(coverage.get("default_label") or "uncovered")
    by_label = coverage.get("by_label") or {}
    ordered = list(coverage.get("windows") or [])

    rows: list[dict[str, Any]] = []
    for entry in sorted(ordered, key=lambda e: (bool(e.get("is_covered")), str(e.get("label")))):
        row = dict(entry)
        definition = row.get("definition") or {}
        row["definition_label"] = str(definition.get("label") or "")
        row["definition_note"] = str(definition.get("note") or "")
        row["is_headline"] = row.get("label") == default_label
        row["band"] = miss_band(row.get("no_response_rate"))
        row["title"] = (
            "Outside covered hours" if row["is_headline"] else "Inside covered hours"
        )
        rows.append(row)

    return {
        "available": True,
        "default_label": default_label,
        "rows": rows,
        "headline": coverage.get("headline") or {},
        "outside": coverage.get("outside") or by_label.get(default_label) or {},
        "inside": coverage.get("inside") or {},
        "contrast": coverage.get("contrast") or {},
        "finding": coverage.get("finding") or "",
        "note": coverage.get("note") or "",
        "definitions": coverage.get("definitions") or [],
        "value_available": bool((coverage.get("outside") or {}).get("value_available")),
    }


@for_company
def missed_work_snapshot(
    days: int = MISSED_WORK_WINDOW_DAYS, company_id: str | None = None
) -> dict[str, Any]:
    """The inventory of work we did not get -- the primary view.

    Leads with the recoverable count and the four buckets, then the
    ``(service_class, cause)`` inventory with a remedy and the clients behind
    it. Blind spots and close-off candidates appear here as summaries with a
    link to their own page.
    """
    envelope = _missed_work_call("compute_missed_work", days, company_id)
    document = envelope["document"] or {}
    totals = dict(document.get("totals") or {})
    by_bucket = document.get("by_bucket") or {}

    def _bucket_entry(bucket: str, label: str, totals_key: str, note: str) -> dict[str, Any]:
        stored = by_bucket.get(bucket) or {}
        count = totals.get(totals_key)
        if count is None:
            count = stored.get("count", 0)
        return {
            "bucket": bucket,
            "label": label,
            "count": int(count or 0),
            "share": stored.get("share", ratio(int(count or 0), totals.get("offers", 0) or 0)),
            "note": note,
            "is_recoverable": bool(stored.get("is_recoverable")),
            "is_missed": bool(stored.get("is_missed")),
        }

    headline = [_bucket_entry(*row) for row in HEADLINE_BUCKETS]
    secondary = [
        _bucket_entry(bucket, label, key, "")
        for bucket, label, key in SECONDARY_BUCKETS
    ]

    inventory: list[dict[str, Any]] = []
    for row in document.get("inventory") or []:
        entry = dict(row)
        entry["top_clients"] = _linkable_clients(entry.get("top_clients"))
        entry["miss_band"] = miss_band(entry.get("class_miss_rate"))
        inventory.append(entry)

    causes = []
    for row in (document.get("by_cause") or {}).values():
        entry = dict(row)
        entry["top_clients"] = _linkable_clients(entry.get("top_clients"))
        causes.append(entry)
    causes.sort(key=lambda e: (-(e.get("missed") or 0), str(e.get("cause") or "")))

    spots = document.get("blind_spots") or {}
    closeoff = document.get("closeoff_candidates") or {}
    comparison = document.get("client_comparison") or {}

    envelope.update(
        {
            "coverage": coverage_rows(document.get("coverage") or {}),
            "totals": totals,
            "by_bucket": by_bucket,
            "headline_buckets": headline,
            "secondary_buckets": secondary,
            "inventory": inventory,
            "inventory_meta": document.get("inventory_meta") or {},
            # One row per job we did not get, each carrying the reference that
            # finds it in Towbook. The inventory above says what to change;
            # this is what the owner reads with the portal open, and it is the
            # only row-level section on the page.
            "missed_jobs": document.get("missed_jobs") or [],
            "missed_jobs_meta": document.get("missed_jobs_meta") or {},
            # How many repeat offers this window collapsed. Every count on the
            # page is smaller for it, so the page says so.
            "duplicates": document.get("duplicates") or {},
            "causes": causes,
            "blind_spots": {
                "count": spots.get("blind_spot_count", 0),
                "offers": spots.get("blind_spot_offers", 0),
                "no_response": spots.get("blind_spot_no_response", 0),
                "worst": (spots.get("blind_spots") or [])[:6],
                "thresholds": spots.get("thresholds") or {},
                "totals": spots.get("totals") or {},
            },
            "closeoff": {
                "totals": closeoff.get("totals") or {},
                "clients": (closeoff.get("clients") or [])[:5],
                "by_service_type": (closeoff.get("by_service_type") or [])[:8],
                "rationale": closeoff.get("rationale") or "",
            },
            "findings": comparison.get("findings") or [],
            "bucket_sources": document.get("bucket_sources") or {},
            "unknown_statuses": document.get("unknown_statuses") or [],
            "ranking_basis": document.get("ranking_basis") or "job_count",
            "revenue_available": bool(document.get("revenue_available")),
            "revenue_note": document.get("revenue_note") or "",
            "rules_version": document.get("rules_version"),
            "has_data": bool(totals.get("offers")),
        }
    )
    return envelope


@for_company
def blind_spots_snapshot(
    days: int = MISSED_WORK_WINDOW_DAYS, company_id: str | None = None
) -> dict[str, Any]:
    """The 7 x 24 hour-of-week grid of when offers go unanswered.

    This is the staffing-decision view. The agent returns the counts as
    matrices; the only thing added here is the presentation shape the template
    needs -- a colour step per cell, a hatch flag for a cell too small to read
    as a trend, and the hover text.
    """
    envelope = _missed_work_call("blind_spots", days, company_id)
    document = envelope["document"] or {}

    offers = document.get("offers") or [[0] * 24 for _ in range(7)]
    accepted = document.get("accepted") or [[0] * 24 for _ in range(7)]
    unanswered = document.get("no_response") or [[0] * 24 for _ in range(7)]
    rates = document.get("no_response_rate") or [[None] * 24 for _ in range(7)]
    weekdays = document.get("weekdays") or list(WEEKDAY_LABELS)
    thresholds = document.get("thresholds") or {}
    threshold = float(thresholds.get("threshold") or 0.35)
    min_offers = int(thresholds.get("min_offers") or 5)

    flagged = {
        (int(cell.get("weekday_index", -1)), int(cell.get("hour", -1)))
        for cell in document.get("blind_spots") or []
    }

    grid: list[dict[str, Any]] = []
    for day in range(7):
        cells: list[dict[str, Any]] = []
        for hour in range(24):
            day_offers = int((offers[day] or [0] * 24)[hour] or 0)
            rate = (rates[day] or [None] * 24)[hour]
            label = weekdays[day] if day < len(weekdays) else str(day)
            cells.append(
                {
                    "weekday": day,
                    "weekday_label": label,
                    "hour": hour,
                    "offers": day_offers,
                    "accepted": int((accepted[day] or [0] * 24)[hour] or 0),
                    "no_response": int((unanswered[day] or [0] * 24)[hour] or 0),
                    "no_response_rate": rate,
                    # 0..6 into the sequential ramp; -1 means "no offers", which
                    # is drawn as an outline, never as a zero.
                    "step": -1 if rate is None else min(6, int(float(rate) * 7)),
                    "low_sample": 0 < day_offers < min_offers,
                    "flag": (day, hour) in flagged,
                }
            )
        grid.append({"weekday": day, "label": weekdays[day] if day < len(weekdays) else str(day), "cells": cells})

    by_hour = []
    for entry in document.get("by_hour") or []:
        row = dict(entry)
        row["band"] = miss_band(row.get("no_response_rate"), threshold)
        by_hour.append(row)

    worst_cells = list(document.get("blind_spots") or [])

    envelope.update(
        {
            "grid": grid,
            "hours": list(range(24)),
            "weekdays": weekdays,
            "by_hour": by_hour,
            "worst_hours": document.get("worst_hours") or [],
            "worst_cells": worst_cells,
            "blind_spot_count": document.get("blind_spot_count", 0),
            "blind_spot_offers": document.get("blind_spot_offers", 0),
            "blind_spot_no_response": document.get("blind_spot_no_response", 0),
            "totals": document.get("totals") or {},
            "thresholds": {"threshold": threshold, "min_offers": min_offers},
            "response_window_note": document.get("response_window_note") or "",
            "has_data": bool((document.get("totals") or {}).get("offers")),
        }
    )
    return envelope


#: Column width the close-off note is wrapped to. 72 is the width a plain-text
#: email has been wrapped to since before Towbook existed, and it fits the card
#: it is displayed in without the browser reflowing it into something ragged.
_EMAIL_WIDTH = 72


def _closeoff_email(client: dict[str, Any], days: int) -> str:
    """A plain-text note the owner can paste into an email to that client.

    Deliberately plain ASCII and deliberately free of dollar figures: it is
    going to leave the building, and the one claim it must never make is that
    these offers were worth a specific amount of money.
    """
    name = str(client.get("client") or "there").strip() or "there"
    rows = client.get("service_types") or []
    total_offers = int(client.get("client_offers") or 0)
    candidate_offers = int(client.get("candidate_offers") or 0)
    candidate_accepted = int(client.get("candidate_accepted") or 0)
    share = client.get("candidate_share_of_client_offers")
    share_text = f" ({share * 100:.0f}% of everything you sent us)" if share is not None else ""

    # Hard-wrapped rather than left to the browser, because the destination is
    # somebody's email client and a paragraph that reflows to the width of a
    # dashboard column arrives looking like a machine wrote it.
    def wrap(text: str) -> list[str]:
        return textwrap.wrap(text, width=_EMAIL_WIDTH) or [""]

    width = max((len(str(row.get("service_type_raw") or "")) for row in rows), default=0)
    width = min(max(width, 12), 34)

    lines = [f"Hi {name},", ""]
    lines += wrap(
        f"Over the last {days} days you sent us {total_offers:,} offers. "
        f"{candidate_offers:,} of them{share_text} were for service types we are "
        f"not set up to cover, and we were able to take {candidate_accepted:,} "
        "of those:"
    )
    lines.append("")
    for row in rows:
        service = str(row.get("service_type_raw") or "")[:width].ljust(width)
        lines.append(
            f"  {service}  {int(row.get('offers') or 0):>4} offered, "
            f"{int(row.get('accepted') or 0):>3} accepted"
        )
    lines.append("")
    lines += wrap(
        "Could you stop routing these to us? Every one of them arrives in the "
        "same short decision window as your tow offers, so taking them out of "
        "the queue should make us faster on the work we do cover for you."
    )
    lines.append("")
    lines += wrap(
        "These are job counts, not dollar figures - Towbook does not publish an "
        "offer amount, so we are not quoting one."
    )
    lines += ["", "Thanks,", ""]
    return "\n".join(lines)


@for_company
def closeoff_snapshot(
    days: int = MISSED_WORK_WINDOW_DAYS, company_id: str | None = None
) -> dict[str, Any]:
    """Work we do not want, being offered anyway -- grouped by client.

    Grouped by client because the action is a conversation with that client,
    and each group carries a paste-ready summary for exactly that conversation.
    """
    envelope = _missed_work_call("closeoff_candidates", days, company_id)
    document = envelope["document"] or {}

    clients: list[dict[str, Any]] = []
    for index, entry in enumerate(document.get("clients") or []):
        row = dict(entry)
        row["slug"] = _slug_for(row.get("client_key"))
        row["service_types"] = [dict(item) for item in row.get("service_types") or []]
        row["email"] = _closeoff_email(row, envelope["days"])
        # Positional rather than derived from the client name: an id has to be
        # a valid CSS selector for the copy button to find it, and the agent
        # already returns this list in a stable, documented order.
        row["dom_id"] = f"closeoff-note-{index}"
        clients.append(row)

    envelope.update(
        {
            "clients": clients,
            "by_service_type": document.get("by_service_type") or [],
            "totals": document.get("totals") or {},
            "thresholds": document.get("thresholds") or {},
            "excluded_service_classes": document.get("excluded_service_classes") or [],
            "wanted_baseline": document.get("wanted_baseline") or {},
            "rationale": document.get("rationale") or "",
            "has_data": bool(clients),
        }
    )
    return envelope


@for_company
def client_no_response(
    days: int = MISSED_WORK_WINDOW_DAYS, company_id: str | None = None
) -> dict[str, Any]:
    """Per-client no-response counts, plus which of them are outliers.

    Backs the first-class ``no_response_rate`` column on /clients. An outlier is
    a client with enough volume to judge whose offers go unanswered at least
    ``client_comparison.gap_pp`` points more often than the best client on the
    same trucks and the same staff.
    """
    envelope = _missed_work_call("client_comparison", days, company_id)
    document = envelope["document"] or {}
    thresholds = document.get("thresholds") or {}
    min_offers = int(thresholds.get("min_offers") or 20)
    gap_pp = float(thresholds.get("gap_pp") or 20.0)

    by_key: dict[str, dict[str, Any]] = {}
    eligible: list[dict[str, Any]] = []
    for entry in document.get("clients") or []:
        row = dict(entry)
        key = str(row.get("client_key") or "")
        row["is_eligible"] = bool(
            (row.get("offers") or 0) >= min_offers and row.get("no_response_rate") is not None
        )
        by_key[key] = row
        if row["is_eligible"]:
            eligible.append(row)

    best_rate: float | None = None
    best_key: str | None = None
    if eligible:
        best = min(eligible, key=lambda e: (e["no_response_rate"], -(e.get("offers") or 0)))
        best_rate = float(best["no_response_rate"])
        best_key = str(best.get("client_key") or "")

    for key, row in by_key.items():
        row["is_best"] = bool(best_key is not None and key == best_key)
        row["gap_pp"] = (
            round((float(row["no_response_rate"]) - best_rate) * 100.0, 2)
            if row["is_eligible"] and best_rate is not None
            else None
        )
        row["is_outlier"] = bool(
            row["is_eligible"] and row["gap_pp"] is not None and row["gap_pp"] >= gap_pp
        )

    envelope.update(
        {
            "by_client_key": by_key,
            "clients": list(by_key.values()),
            "findings": document.get("findings") or [],
            "thresholds": {"min_offers": min_offers, "gap_pp": gap_pp},
            "best_rate": best_rate,
            "best_client_key": best_key,
            "outlier_count": sum(1 for row in by_key.values() if row["is_outlier"]),
        }
    )
    return envelope


# ==========================================================================
# Tab 1 -- HOURLY. The board that replaced the hourly text message.
# ==========================================================================
#
# THIS TAB IS THE SMS NOW. Nothing is texted any more, so everything the text
# carried has to be on this screen or it is simply gone:
#
#     14:00-14:59 | Offered 12 / Accepted 9 (75%)
#     Day: 84 / 61 (73%)
#     !! 3 tows unanswered this hour
#
# All three lines are reproduced verbatim in `text_block` -- built by the same
# agents/notifier.py helpers that built the message, not by a second
# implementation -- and then expanded into the hour-by-hour table around them.
# If the two ever disagreed there would be no way to tell which was lying.
#
# The one number the SMS did NOT carry, and the board does, is `unanswered`
# per hour. On this account 795 of 2,996 offers over 30 days were never
# answered at all, so an hour's acceptance rate without its unanswered count
# beside it hides the thing most worth acting on.


def _hour_range_label(hour: int) -> str:
    """``"14:00-14:59"`` -- the exact shape the hourly SMS used."""
    return f"{hour:02d}:00-{hour:02d}:59"


def _notification_formatting() -> dict[str, Any]:
    try:
        return dict((CONFIG.get_notifications().get("formatting") or {}))
    except Exception:
        return {}


def _missed_work_line(counts: dict[str, int], report_type: str) -> str:
    """The ``!! 3 tows unanswered this hour`` line, or ``""``.

    Built by ``agents/notifier.py -> missed_work_line`` so the board's wording
    is the message's wording. Empty when the count is zero: a warning that
    appears every hour is one the owner stops reading, and that was true of the
    text message for the same reason.
    """
    module = _notifier_module()
    if module is None or not hasattr(module, "missed_work_line"):
        return ""
    try:
        return str(module.missed_work_line(counts, report_type, _notification_formatting()))
    except Exception:  # pragma: no cover - defensive
        return ""


def _wanted_classes() -> set[str]:
    """Service classes the written policy says we want. Config, not code."""
    return {name for name, verdict in policy_map().items() if verdict == "accept"}


@for_company
def hourly_snapshot(company_id: str | None = None) -> dict[str, Any]:
    """Today, hour by hour: offers, accepted, unanswered, running day total.

    The current hour is returned separately as ``current`` as well as inside
    ``hours``, because it is the number the owner actually opened the page for
    and it should not have to be found in a table of 24 rows.

    Hours later than the current one are marked ``is_future`` and carry a
    running rate of ``None``. They are not zeros -- nothing has been offered in
    them yet -- and the template draws them as em-dashes.
    """
    today = today_local()
    now = now_local()
    rules = rules_snapshot()
    wanted = _wanted_classes()

    day_start, day_end = local_day_bounds(today)
    rows = fetch_requests(day_start, day_end, company_id=company_id)

    # 28 days ending yesterday: four of each weekday, so the same-hour baseline
    # is not one freak Tuesday. Today is excluded on purpose -- a baseline that
    # includes the hour being measured moves as that hour fills up.
    base_start, base_end = local_span_bounds(today - timedelta(days=28), today - timedelta(days=1))
    baseline_rows = fetch_requests(base_start, base_end, company_id=company_id)

    outside = outside_territory_label()
    declined_classes = _declined_classes()
    for row in rows:
        row["bucket"] = bucket_of(row, rules)
        # Why an offer was never ours, kept as two separate reasons rather than
        # one "not addressable" flag: "they keep sending us tire changes" and
        # "they keep sending us Chillicothe" are different conversations with
        # the client, and the report has to be able to name which one.
        row["territory"] = territory_of(row, rules)
        row["out_of_territory"] = row["territory"] == outside
        row["policy_declined"] = row["service_class"] in declined_classes
        row["addressable"] = not (row["out_of_territory"] or row["policy_declined"])

    day_counts: Counter[str] = Counter()
    per_hour: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per_hour[row["offered_local"].hour].append(row)
        day_counts[row["bucket"] or "unknown"] += 1

    # The same-hour-of-day baseline, as an average per day observed.
    baseline_offered: Counter[int] = Counter()
    baseline_accepted: Counter[int] = Counter()
    baseline_days: set[_date] = set()
    for row in baseline_rows:
        moment = row["offered_local"]
        baseline_offered[moment.hour] += 1
        baseline_days.add(moment.date())
        if row["status"] == "accepted":
            baseline_accepted[moment.hour] += 1
    observed_days = max(len(baseline_days), 1)

    hours: list[dict[str, Any]] = []
    running_offered = running_accepted = running_unanswered = 0
    for hour in range(24):
        hour_rows = per_hour.get(hour) or []
        counts: Counter[str] = Counter(row["bucket"] or "unknown" for row in hour_rows)
        offered = len(hour_rows)
        accepted = counts.get("won", 0)
        unanswered = counts.get("no_response", 0)

        is_future = hour > now.hour
        running_offered += offered
        running_accepted += accepted
        running_unanswered += unanswered

        wanted_unanswered = Counter(
            row["service_class"]
            for row in hour_rows
            if row["bucket"] == "no_response" and row["service_class"] in wanted
        )

        # The same hour counted only over work this company would actually take.
        # Shown BESIDE the raw rate, never instead of it: the raw number is what
        # the club sees and what the contract is judged on, and quietly
        # replacing it would be marking our own homework.
        addressable_rows = [r for r in hour_rows if r["addressable"]]
        addressable_offered = len(addressable_rows)
        addressable_accepted = sum(
            1 for r in addressable_rows if r["bucket"] == "won"
        )

        hours.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "range_label": _hour_range_label(hour),
                "offered": offered,
                "accepted": accepted,
                "addressable_offered": addressable_offered,
                "addressable_accepted": addressable_accepted,
                "addressable_rate": ratio(addressable_accepted, addressable_offered),
                "out_of_territory": sum(1 for r in hour_rows if r["out_of_territory"]),
                "policy_declined": sum(1 for r in hour_rows if r["policy_declined"]),
                "unanswered": unanswered,
                "declined": counts.get("declined", 0),
                "withdrew": counts.get("client_withdrew", 0),
                "in_flight": counts.get("in_flight", 0),
                "rate": ratio(accepted, offered),
                "band": rate_band(ratio(accepted, offered)),
                "unanswered_rate": ratio(unanswered, offered),
                "unanswered_band": miss_band(ratio(unanswered, offered)),
                "running_offered": running_offered,
                "running_accepted": running_accepted,
                "running_unanswered": running_unanswered,
                "running_rate": None if is_future else ratio(running_accepted, running_offered),
                "is_future": is_future,
                "is_current": hour == now.hour,
                "is_past": hour < now.hour,
                "baseline_offered": round(baseline_offered.get(hour, 0) / observed_days, 1),
                "baseline_rate": ratio(baseline_accepted.get(hour, 0), baseline_offered.get(hour, 0)),
                "alert_line": _missed_work_line(dict(wanted_unanswered), "hourly"),
                "wanted_unanswered": dict(wanted_unanswered),
                "top_clients": _top_clients(hour_rows, limit=4),
            }
        )

    current = hours[min(now.hour, 23)]

    day_offered = len(rows)
    day_accepted = day_counts.get("won", 0)
    day_unanswered = day_counts.get("no_response", 0)
    day_rate = ratio(day_accepted, day_offered)

    day_addressable_rows = [r for r in rows if r["addressable"]]
    day_addressable = len(day_addressable_rows)
    day_addressable_accepted = sum(
        1 for r in day_addressable_rows if r["bucket"] == "won"
    )
    day_addressable_rate = ratio(day_addressable_accepted, day_addressable)
    day_out_of_territory = sum(1 for r in rows if r["out_of_territory"])
    day_policy_declined = sum(1 for r in rows if r["policy_declined"])

    day_wanted_unanswered = Counter(
        row["service_class"]
        for row in rows
        if row["bucket"] == "no_response" and row["service_class"] in wanted
    )

    # The message, exactly as it read. Percentages are rendered here rather than
    # by a template filter so the block is a faithful copy and not a re-format.
    def _sms_pct(value: float | None) -> str:
        return "0" if value is None else f"{value * 100:.0f}"

    text_lines = [
        f"{current['range_label']} | Offered {current['offered']} / "
        f"Accepted {current['accepted']} ({_sms_pct(current['rate'])}%)",
        f"Day: {day_offered} / {day_accepted} ({_sms_pct(day_rate)}%)",
    ]
    if current["alert_line"]:
        text_lines.append(current["alert_line"])
    # Appended, never substituted. The two lines above are the message this
    # system replaced a person typing, and MISSED_WORK_MODEL.md fixes their
    # shape; this adds a line only when some of the day's offers were never
    # ours, which is the only time it says anything.
    day_not_ours = day_offered - day_addressable
    if day_not_ours:
        reasons = []
        if day_policy_declined:
            reasons.append(f"{day_policy_declined} light service")
        if day_out_of_territory:
            reasons.append(f"{day_out_of_territory} out of area")
        text_lines.append(
            f"Of ours: {day_addressable} / {day_addressable_accepted} "
            f"({_sms_pct(day_addressable_rate)}%) - excludes {', '.join(reasons)}"
        )

    return {
        "date": today,
        "generated_at": now,
        "current_hour": now.hour,
        "current": current,
        "hours": hours,
        "totals": {
            "offered": day_offered,
            "accepted": day_accepted,
            "unanswered": day_unanswered,
            "declined": day_counts.get("declined", 0),
            "withdrew": day_counts.get("client_withdrew", 0),
            "in_flight": day_counts.get("in_flight", 0),
            "unknown": day_counts.get("unknown_status", 0) + day_counts.get("unknown", 0),
            "rate": day_rate,
            "band": rate_band(day_rate),
            "unanswered_rate": ratio(day_unanswered, day_offered),
            "unanswered_band": miss_band(ratio(day_unanswered, day_offered)),
            # Work this company would actually take, and the two reasons the
            # rest was never theirs. Over 30 days these are 18.8% of all offers.
            "addressable": day_addressable,
            "addressable_accepted": day_addressable_accepted,
            "addressable_rate": day_addressable_rate,
            "addressable_band": rate_band(day_addressable_rate),
            "out_of_territory": day_out_of_territory,
            "policy_declined": day_policy_declined,
            "not_ours": day_not_ours,
        },
        "day_alert_line": _missed_work_line(dict(day_wanted_unanswered), "daily"),
        "text_block": "\n".join(text_lines),
        "top_clients": _top_clients(rows, limit=6),
        "alerts": recent_alerts(limit=6),
        "last_run": last_run_summary(),
        "basis_note": basis_note(),
        "baseline_days": observed_days,
        "model_available": bool(_missed_work_module()),
        "has_data": bool(rows) or bool(baseline_rows),
        "has_today": bool(rows),
    }


# ==========================================================================
# Tabs 2 and 3 -- WEEKLY and MONTHLY, each against the period before it
# ==========================================================================
#
# ONE FUNCTION SERVES BOTH TABS. A week and a month differ only in where their
# boundaries fall; every comparison, every cause delta and every recommendation
# below is identical, and writing them twice would guarantee the two tabs
# eventually disagreed.
#
# THE COMPARISON IS LIKE-FOR-LIKE BY DEFAULT. On a Tuesday, "this week" is two
# days long. Setting that against a full seven-day last week would report a 70%
# collapse in volume every Monday morning, so the headline comparison is
# against the SAME NUMBER OF ELAPSED DAYS of the previous period, and the full
# previous period is shown beside it, labelled, never instead of it.

#: The two period kinds the board offers, and how each one is described.
PERIOD_KINDS: tuple[str, ...] = ("week", "month")


def _month_start(day: _date) -> _date:
    return day.replace(day=1)


def _previous_month_start(day: _date) -> _date:
    first = _month_start(day)
    return _month_start(first - timedelta(days=1))


def _month_end(first_of_month: _date) -> _date:
    """Last calendar day of the month ``first_of_month`` opens."""
    if first_of_month.month == 12:
        following = first_of_month.replace(year=first_of_month.year + 1, month=1)
    else:
        following = first_of_month.replace(month=first_of_month.month + 1)
    return following - timedelta(days=1)


def period_bounds(kind: str, today: _date | None = None) -> dict[str, Any]:
    """The three windows a period tab compares.

    ``current``            this week / month so far, up to and including today
    ``previous_to_date``   the same number of elapsed days of the period before
    ``previous_full``      that whole period, however long it ran

    All three are ``(first_day, last_day)`` pairs of local calendar days,
    inclusive at both ends, because that is how a human reads "last month".
    """
    today = today or today_local()
    kind = kind if kind in PERIOD_KINDS else "week"

    if kind == "week":
        current_first = week_start_for(today)
        previous_first = current_first - timedelta(days=7)
        previous_last = current_first - timedelta(days=1)
        length = 7
    else:
        current_first = _month_start(today)
        previous_first = _previous_month_start(today)
        previous_last = _month_end(previous_first)
        length = (previous_last - previous_first).days + 1

    elapsed = (today - current_first).days + 1
    # A short previous month (February against March) cannot supply more days
    # than it has, so the like-for-like window is clamped and the page says so.
    comparable = min(elapsed, length)
    previous_to_date_last = previous_first + timedelta(days=comparable - 1)

    return {
        "kind": kind,
        "label": "week" if kind == "week" else "month",
        "elapsed_days": elapsed,
        "previous_length_days": length,
        "comparable_days": comparable,
        "is_partial": elapsed < length,
        "current": (current_first, today),
        "previous_to_date": (previous_first, previous_to_date_last),
        "previous_full": (previous_first, previous_last),
    }


def _missed_work_for_days(
    first_day: _date, last_day: _date, company_id: str | None
) -> dict[str, Any]:
    """``compute_missed_work`` over an inclusive span of local days.

    ``persist=False`` -- this is the dashboard, and the dashboard never writes.
    Returns an envelope with ``available`` / ``error`` rather than raising, so a
    period tab degrades to an explanation instead of a 500.
    """
    envelope: dict[str, Any] = {
        "available": False,
        "error": None,
        "document": {},
        "first_day": first_day,
        "last_day": last_day,
        "days": (last_day - first_day).days + 1,
    }
    module = _missed_work_module()
    if module is None:
        envelope["error"] = (
            "agents/missed_work.py could not be imported, so the missed-work "
            "model cannot be computed."
        )
        return envelope
    try:
        envelope["document"] = module.compute_missed_work(
            None,
            datetime.combine(first_day, time.min),
            datetime.combine(last_day + timedelta(days=1), time.min),
            None,
            period_type="custom",
            company_id=_companies.resolve_company_id(company_id),
            persist=False,
        )
        envelope["available"] = True
    except Exception as exc:
        logger.exception("compute_missed_work failed for %s..%s", first_day, last_day)
        envelope["error"] = redact(f"{type(exc).__name__}: {exc}")
    return envelope


def _count_delta(current: Any, previous: Any) -> dict[str, Any]:
    """A count against its comparison, with the direction already decided.

    ``change`` is ``None`` rather than 100% when the previous period had none of
    something: "we went from nothing to three" has no percentage, and inventing
    one is how a report ends up claiming an infinite increase.
    """
    current_value = int(current or 0)
    previous_value = int(previous or 0)
    difference = current_value - previous_value
    change = (difference / previous_value) if previous_value else None
    if difference > 0:
        direction = "up"
    elif difference < 0:
        direction = "down"
    else:
        direction = "flat"
    return {
        "current": current_value,
        "previous": previous_value,
        "difference": difference,
        "change": change,
        "direction": direction,
    }


def _rate_delta(current: float | None, previous: float | None) -> dict[str, Any]:
    points = None if (current is None or previous is None) else (current - previous)
    if points is None:
        direction = "unknown"
    elif points > 0.005:
        direction = "up"
    elif points < -0.005:
        direction = "down"
    else:
        direction = "flat"
    return {
        "current": current,
        "previous": previous,
        "points": points,
        "direction": direction,
    }


def _coverage_comparison(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Covered vs uncovered, this period against last."""
    rows = []
    for key, title in (("outside", "Outside covered hours"), ("inside", "Inside covered hours")):
        now_side = (current or {}).get(key) or {}
        was_side = (previous or {}).get(key) or {}
        rows.append(
            {
                "key": key,
                "title": title,
                "is_headline": key == "outside",
                "offers": _count_delta(now_side.get("offers"), was_side.get("offers")),
                "no_response": _count_delta(now_side.get("no_response"), was_side.get("no_response")),
                "recoverable": _count_delta(now_side.get("recoverable"), was_side.get("recoverable")),
                "no_response_rate": _rate_delta(
                    now_side.get("no_response_rate"), was_side.get("no_response_rate")
                ),
                "band": miss_band(now_side.get("no_response_rate")),
            }
        )
    return {
        "available": bool(current),
        "rows": rows,
        "contrast": (current or {}).get("contrast") or {},
        "finding": (current or {}).get("finding") or "",
        "definitions": (current or {}).get("definitions") or [],
        "default_label": (current or {}).get("default_label") or uncovered_label(),
    }


def _cause_comparison(current: dict[str, Any], previous: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-cause movement, worst first. Every cause in either period appears."""
    now_causes = (current or {}).get("by_cause") or {}
    was_causes = (previous or {}).get("by_cause") or {}
    rows: list[dict[str, Any]] = []
    for cause in sorted(set(now_causes) | set(was_causes)):
        now_entry = now_causes.get(cause) or {}
        was_entry = was_causes.get(cause) or {}
        source = now_entry or was_entry
        rows.append(
            {
                "cause": cause,
                "remedy": source.get("remedy") or "",
                "question": source.get("question") or "",
                "missed": _count_delta(now_entry.get("missed"), was_entry.get("missed")),
                "recoverable": _count_delta(
                    now_entry.get("recoverable"), was_entry.get("recoverable")
                ),
                "share": now_entry.get("share"),
                "top_clients": _linkable_clients(now_entry.get("top_clients")),
                "service_classes": now_entry.get("service_classes") or {},
            }
        )
    rows.sort(key=lambda row: (-(row["missed"]["current"]), row["cause"]))
    return rows


def _client_comparison(current: dict[str, Any], previous: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-client trajectory: volume, acceptance and unanswered, then vs then."""
    def _by_key(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        entries = ((document or {}).get("client_comparison") or {}).get("clients") or []
        return {str(entry.get("client_key") or ""): entry for entry in entries if isinstance(entry, dict)}

    now_clients = _by_key(current)
    was_clients = _by_key(previous)

    rows: list[dict[str, Any]] = []
    for key in set(now_clients) | set(was_clients):
        now_entry = now_clients.get(key) or {}
        was_entry = was_clients.get(key) or {}
        source = now_entry or was_entry
        rows.append(
            {
                "client_key": key,
                "slug": _slug_for(key),
                "client": source.get("client") or "(no client name)",
                "offers": _count_delta(now_entry.get("offers"), was_entry.get("offers")),
                "accepted": _count_delta(now_entry.get("accepted"), was_entry.get("accepted")),
                "no_response": _count_delta(
                    now_entry.get("no_response"), was_entry.get("no_response")
                ),
                "rate": _rate_delta(now_entry.get("rate"), was_entry.get("rate")),
                "no_response_rate": _rate_delta(
                    now_entry.get("no_response_rate"), was_entry.get("no_response_rate")
                ),
                "band": rate_band(now_entry.get("rate")),
                "miss_band": miss_band(now_entry.get("no_response_rate")),
                "is_new": not was_entry and bool(now_entry),
                "is_gone": bool(was_entry) and not now_entry,
            }
        )
    rows.sort(key=lambda row: (-(row["offers"]["current"]), row["client"].casefold()))
    return rows


def _closeoff_effect(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Did the close-offs work?

    For every service type flagged as a close-off candidate in EITHER period,
    the offer count then and now. A type that was flagged last period and has
    fallen away is the close-off having taken effect; one that is still arriving
    at the same volume is a conversation that did not happen or did not land.

    MISSED_WORK_MODEL.md section 5 claims closing these off should also improve
    the tow response rate, because they compete for the same three-minute
    decision window. That claim is tracked here rather than asserted: the
    no-response rate for both periods sits beside the volumes.
    """
    def _types(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        rows = ((document or {}).get("closeoff_candidates") or {}).get("by_service_type") or []
        return {
            str(row.get("service_type_raw") or ""): row
            for row in rows
            if isinstance(row, dict)
        }

    now_types = _types(current)
    was_types = _types(previous)

    rows: list[dict[str, Any]] = []
    for name in set(now_types) | set(was_types):
        now_entry = now_types.get(name) or {}
        was_entry = was_types.get(name) or {}
        offers = _count_delta(now_entry.get("offers"), was_entry.get("offers"))
        rows.append(
            {
                "service_type_raw": name,
                "service_class": (now_entry or was_entry).get("service_class"),
                "offers": offers,
                "accepted": _count_delta(now_entry.get("accepted"), was_entry.get("accepted")),
                "still_flagged": name in now_types,
                "was_flagged": name in was_types,
                # "Stopped" only when it was flagged before and no offer of that
                # type arrived at all. A drop from 145 to 12 is progress, not a
                # close-off that landed, and calling it one would overstate it.
                "stopped": name in was_types and offers["current"] == 0,
            }
        )
    rows.sort(key=lambda row: (-(row["offers"]["previous"] + row["offers"]["current"]), row["service_type_raw"]))

    return {
        "rows": rows,
        "stopped": sum(1 for row in rows if row["stopped"]),
        "still_arriving": sum(1 for row in rows if row["still_flagged"]),
        "no_response_rate": _rate_delta(
            ((current or {}).get("totals") or {}).get("no_response_rate"),
            ((previous or {}).get("totals") or {}).get("no_response_rate"),
        ),
    }


def _recommendations(
    document: dict[str, Any], previous: dict[str, Any], kind: str
) -> list[dict[str, Any]]:
    """What to actually do, ranked, each carrying the number that justifies it.

    Deterministic and sourced. Nothing here is generated text and nothing here
    is a guess: every entry is a threshold in ``rules.yaml`` having been crossed
    by a count from the window, and every entry states that count. An
    unsupported sentence is not added -- MISSED_WORK_MODEL.md section 9 applies
    to the board as much as to the Analyst.
    """
    if not document:
        return []

    out: list[dict[str, Any]] = []
    totals = document.get("totals") or {}
    coverage = document.get("coverage") or {}
    outside = coverage.get("outside") or {}
    inside = coverage.get("inside") or {}
    contrast = coverage.get("contrast") or {}
    window = "this week" if kind == "week" else "this month"

    # 1. Coverage. On this owner's real data the largest single lever by a wide
    #    margin -- but the recommendation FIRES ONLY WHEN THE UNCOVERED WINDOW
    #    IS ACTUALLY WORSE. "Staff the uncovered hours" is a claim about a gap,
    #    and in a week where there is no gap it is an instruction with no
    #    evidence behind it. The coverage table on the page still shows both
    #    rows either way, so nothing is hidden by declining to advise.
    outside_rate = outside.get("no_response_rate")
    inside_rate = inside.get("no_response_rate")
    coverage_is_worse = outside_rate is not None and (
        inside_rate is None or outside_rate > inside_rate
    )
    if outside.get("no_response") and coverage_is_worse:
        multiple = contrast.get("multiple")
        detail = (
            f"{outside['no_response']:,} of {outside.get('offers', 0):,} offers that arrived "
            f"outside the staffed window went unanswered "
            f"({(outside_rate or 0) * 100:.1f}%), against "
            f"{inside.get('no_response', 0):,} of {inside.get('offers', 0):,} inside it "
            f"({(inside_rate or 0) * 100:.1f}%)."
        )
        if multiple and multiple > 1:
            detail += f" That is {multiple}x as often, on the same trucks."
        out.append(
            {
                "priority": "high",
                "action": "Put a person on the queue outside the staffed window",
                "detail": detail,
                "evidence": f"{outside.get('recoverable', 0):,} recoverable jobs {window}",
                "where": "rules.yaml -> missed_work -> coverage",
            }
        )

    # 2. Named hours, so the coverage point becomes a rota change.
    for cell in (document.get("blind_spots") or {}).get("blind_spots", [])[:3]:
        if not isinstance(cell, dict):
            continue
        rate = cell.get("no_response_rate")
        out.append(
            {
                "priority": "high",
                "action": (
                    f"Cover {cell.get('weekday', '')} "
                    f"{int(cell.get('hour', 0)):02d}:00-{int(cell.get('hour', 0)):02d}:59"
                ),
                "detail": (
                    f"{cell.get('offers', 0):,} offers in that slot {window}, "
                    f"{cell.get('no_response', 0):,} never answered"
                    + (f" ({rate * 100:.0f}%)." if rate is not None else ".")
                ),
                "evidence": "blind spot: over the configured offers and miss-rate thresholds",
                "where": "rules.yaml -> missed_work -> blind_spot",
            }
        )

    # 3. Causes we can actually act on, biggest first, with the remedy attached.
    for entry in sorted(
        (document.get("by_cause") or {}).values(),
        key=lambda row: -(row.get("missed") or 0),
    )[:3]:
        if not isinstance(entry, dict) or not entry.get("missed"):
            continue
        clients = ", ".join(
            str(client.get("client") or "")
            for client in (entry.get("top_clients") or [])[:3]
            if isinstance(client, dict)
        )
        out.append(
            {
                "priority": "medium",
                "action": f"{str(entry.get('cause') or 'unattributed').replace('_', ' ').capitalize()}"
                f" -- {str(entry.get('remedy') or 'review').replace('_', ' ')}",
                "detail": (
                    f"{entry.get('missed', 0):,} missed {window}, "
                    f"{entry.get('recoverable', 0):,} of them recoverable."
                    + (f" Mostly {clients}." if clients else "")
                ),
                "evidence": entry.get("question") or "",
                "where": "rules.yaml -> missed_work -> remedies",
            }
        )

    # 4. Work we do not want, and who to say so to.
    for client in ((document.get("closeoff_candidates") or {}).get("clients") or [])[:3]:
        if not isinstance(client, dict):
            continue
        share = client.get("candidate_share_of_client_offers")
        out.append(
            {
                "priority": "medium",
                "action": f"Ask {client.get('client', 'this client')} to stop sending work we do not cover",
                "detail": (
                    f"{client.get('candidate_offers', 0):,} offers {window} produced "
                    f"{client.get('candidate_accepted', 0):,} jobs"
                    + (f" -- {share * 100:.0f}% of everything they sent." if share is not None else ".")
                    + " Each one lands in the same three-minute window as their tows."
                ),
                "evidence": "close-off candidate: over the configured offers threshold, under the rate threshold",
                "where": "/close-off has a paste-ready note",
            }
        )

    # 5. A client whose offers go unanswered far more often than the best one.
    for finding in ((document.get("client_comparison") or {}).get("findings") or [])[:2]:
        if not isinstance(finding, dict):
            continue
        out.append(
            {
                "priority": "medium",
                "action": (
                    f"Find the process difference between "
                    f"{(finding.get('worst') or {}).get('client', 'these clients')} and "
                    f"{(finding.get('best') or {}).get('client', 'the best one')}"
                ),
                "detail": str(finding.get("summary") or ""),
                "evidence": f"{finding.get('spread_pp', 0)} point gap, same trucks and same staff",
                "where": "/clients",
            }
        )

    # 6. Anything the status maps did not recognise. Small, and never hidden:
    #    silently absorbing a new Towbook status is how a report starts being
    #    confidently incorrect.
    unknown = int(totals.get("unknown_status") or 0)
    if unknown:
        out.append(
            {
                "priority": "low",
                "action": "Teach the bucket map the statuses it does not know",
                "detail": f"{unknown:,} offers {window} carried a status no map recognised.",
                "evidence": "listed on the Missed work tab under unreadable status",
                "where": "rules.yaml -> missed_work -> buckets",
            }
        )

    return out


@for_company
def period_snapshot(kind: str = "week", company_id: str | None = None) -> dict[str, Any]:
    """A whole period tab: this week/month against the one before it.

    Three computations of the missed-work model -- current, the like-for-like
    slice of the previous period, and the whole previous period -- reshaped
    into the comparisons the tab renders. Nothing is recomputed a second way.
    """
    bounds = period_bounds(kind)
    current = _missed_work_for_days(*bounds["current"], company_id)
    previous = _missed_work_for_days(*bounds["previous_to_date"], company_id)
    previous_full = _missed_work_for_days(*bounds["previous_full"], company_id)

    document = current["document"] or {}
    prior = previous["document"] or {}
    prior_full = previous_full["document"] or {}

    totals = document.get("totals") or {}
    prior_totals = prior.get("totals") or {}
    prior_full_totals = prior_full.get("totals") or {}

    buckets = [
        {
            "bucket": bucket,
            "label": label,
            "note": note,
            "counts": _count_delta(totals.get(totals_key), prior_totals.get(totals_key)),
            "previous_full": int(prior_full_totals.get(totals_key) or 0),
        }
        for bucket, label, totals_key, note in HEADLINE_BUCKETS
    ]

    return {
        "kind": bounds["kind"],
        "label": bounds["label"],
        "available": current["available"],
        "error": current["error"] or previous["error"],
        "bounds": bounds,
        "current_first": bounds["current"][0],
        "current_last": bounds["current"][1],
        "previous_first": bounds["previous_to_date"][0],
        "previous_last": bounds["previous_to_date"][1],
        "previous_full_first": bounds["previous_full"][0],
        "previous_full_last": bounds["previous_full"][1],
        "elapsed_days": bounds["elapsed_days"],
        "comparable_days": bounds["comparable_days"],
        "previous_length_days": bounds["previous_length_days"],
        "is_partial": bounds["is_partial"],
        "totals": totals,
        "prior_totals": prior_totals,
        "prior_full_totals": prior_full_totals,
        "headline_buckets": buckets,
        "offers": _count_delta(totals.get("offers"), prior_totals.get("offers")),
        "recoverable": _count_delta(totals.get("recoverable"), prior_totals.get("recoverable")),
        "missed": _count_delta(totals.get("missed"), prior_totals.get("missed")),
        "acceptance_rate": _rate_delta(
            totals.get("acceptance_rate"), prior_totals.get("acceptance_rate")
        ),
        "no_response_rate": _rate_delta(
            totals.get("no_response_rate"), prior_totals.get("no_response_rate")
        ),
        "coverage": _coverage_comparison(
            document.get("coverage") or {}, prior.get("coverage") or {}
        ),
        "causes": _cause_comparison(document, prior),
        "clients": _client_comparison(document, prior),
        "closeoff": _closeoff_effect(document, prior),
        "blind_spots": {
            "count": (document.get("blind_spots") or {}).get("blind_spot_count", 0),
            "offers": (document.get("blind_spots") or {}).get("blind_spot_offers", 0),
            "no_response": (document.get("blind_spots") or {}).get("blind_spot_no_response", 0),
            "worst": ((document.get("blind_spots") or {}).get("blind_spots") or [])[:6],
            "previous_count": (prior.get("blind_spots") or {}).get("blind_spot_count", 0),
        },
        "inventory": [
            dict(row, top_clients=_linkable_clients(row.get("top_clients")))
            for row in (document.get("inventory") or [])
        ],
        "findings": (document.get("client_comparison") or {}).get("findings") or [],
        "recommendations": _recommendations(document, prior, bounds["kind"]),
        "revenue_available": bool(document.get("revenue_available")),
        # FALSE ON PURPOSE, even when job values ARE configured.
        #
        # ``ranking_note`` captions what the reader is actually looking at, not
        # what the system could compute: its "dollar figures are estimates"
        # variant is only true of a report that contains dollar figures. No
        # screen on this board does -- every column here is a job count -- so
        # printing that sentence would tell the owner these numbers are money.
        # The reports carry the priced version; the board does not.
        "basis_note": basis_note(False),
        "rules_version": document.get("rules_version"),
        "has_data": bool(totals.get("offers")) or bool(prior_totals.get("offers")),
    }


# ==========================================================================
# View 1 -- Live
# ==========================================================================


@for_company
def live_snapshot(company_id: str | None = None) -> dict[str, Any]:
    """Today so far: running totals, hour buckets, and the comparison baselines."""
    today = today_local()
    now = now_local()

    day_start, day_end = local_day_bounds(today)
    today_rows = fetch_requests(day_start, day_end, company_id=company_id)

    # 30 days ending yesterday -- the baselines must not include today, or the
    # line the operator is being measured against moves as the day fills up.
    base_start, base_end = local_span_bounds(today - timedelta(days=30), today - timedelta(days=1))
    baseline_rows = fetch_requests(base_start, base_end, company_id=company_id)

    by_day: dict[_date, Bucket] = _bucket_by(baseline_rows, lambda r: r["offered_local"].date())

    def _window_rate(days: int) -> dict[str, Any]:
        first = today - timedelta(days=days)
        offered = accepted = 0
        for day, bucket in by_day.items():
            if first <= day <= today - timedelta(days=1):
                offered += bucket.offered
                accepted += bucket.accepted
        value = ratio(accepted, offered)
        return {"days": days, "offered": offered, "accepted": accepted, "rate": value}

    seven = _window_rate(7)
    thirty = _window_rate(30)

    totals = _totals(today_rows)
    by_hour = _bucket_by(today_rows, lambda r: r["offered_local"].hour)

    hours: list[dict[str, Any]] = []
    running_offered = running_accepted = 0
    for hour in range(24):
        bucket = by_hour.get(hour)
        entry: dict[str, Any] = {
            "hour": hour,
            "label": f"{hour:02d}:00",
            "offered": bucket.offered if bucket else 0,
            "accepted": bucket.accepted if bucket else 0,
            "rate": bucket.rate if bucket else None,
            "is_future": hour > now.hour,
        }
        running_offered += entry["offered"]
        running_accepted += entry["accepted"]
        entry["running_rate"] = ratio(running_accepted, running_offered) if not entry["is_future"] else None
        entry["band"] = rate_band(entry["rate"])
        hours.append(entry)

    yesterday = by_day.get(today - timedelta(days=1))

    return {
        "date": today,
        "generated_at": now,
        "totals": totals.as_dict(),
        "hours": hours,
        "status_mix": [
            {"status": status, "count": getattr(totals, status)} for status in STATUS_ORDER
        ],
        "baseline_7d": seven,
        "baseline_30d": thirty,
        "yesterday": yesterday.as_dict() if yesterday else None,
        "delta_7d": _delta(totals.rate, seven["rate"]),
        "delta_30d": _delta(totals.rate, thirty["rate"]),
        "top_clients": _top_clients(today_rows, limit=6),
        "alerts": recent_alerts(limit=8),
        "last_run": last_run_summary(),
        "has_data": bool(today_rows) or bool(baseline_rows),
        "has_today": bool(today_rows),
    }


def _top_clients(rows: Iterable[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    buckets = _bucket_by(rows, lambda r: (r["client_key"], r["client_name"] or ""))
    entries = []
    for (key, name), bucket in buckets.items():
        entry = bucket.as_dict()
        entry["client_key"] = key
        entry["client_name"] = name or "(no client name)"
        entry["slug"] = key or EMPTY_CLIENT_KEY
        entries.append(entry)
    entries.sort(key=lambda e: (-e["offered"], e["client_name"]))
    return entries[:limit]


# ==========================================================================
# View 2 -- Daily
# ==========================================================================

CLIENT_SORTS = {
    "volume": lambda e: (-e["offered"], e["client_name"]),
    "rate": lambda e: (e["rate"] is None, -(e["rate"] or 0.0), e["client_name"]),
    "delta": lambda e: (e["delta"] is None, e["delta"] if e["delta"] is not None else 0.0, e["client_name"]),
    "name": lambda e: (e["client_name"].casefold(),),
    "accepted": lambda e: (-e["accepted"], e["client_name"]),
    "denied": lambda e: (-e["denied"], e["client_name"]),
    # The missed-work columns. `.get` throughout because the same sorter runs
    # over the daily client table, whose rows do not carry these keys -- a sort
    # request for a column a table does not have must degrade, never raise.
    "no_response": lambda e: (-(e.get("no_response") or 0), e["client_name"]),
    "miss": lambda e: (
        e.get("no_response_rate") is None,
        -(e.get("no_response_rate") or 0.0),
        e["client_name"],
    ),
}


def sort_clients(entries: list[dict[str, Any]], sort: str, direction: str) -> list[dict[str, Any]]:
    """Sort a client table. Unknown keys fall back to volume, never raise."""
    key_fn = CLIENT_SORTS.get(sort, CLIENT_SORTS["volume"])
    ordered = sorted(entries, key=key_fn)
    if direction == "asc":
        ordered.reverse()
    return ordered


@for_company
def daily_snapshot(
    day: _date,
    company_id: str | None = None,
    sort: str = "volume",
    direction: str = "desc",
) -> dict[str, Any]:
    """Full breakdown for one local calendar day."""
    day_start, day_end = local_day_bounds(day)
    rows = fetch_requests(day_start, day_end, company_id=company_id)

    prior_start, prior_end = local_span_bounds(day - timedelta(days=7), day - timedelta(days=1))
    prior_rows = fetch_requests(prior_start, prior_end, company_id=company_id)

    totals = _totals(rows)
    prior_by_day = _bucket_by(prior_rows, lambda r: r["offered_local"].date())

    yesterday_bucket = prior_by_day.get(day - timedelta(days=1))
    trailing = Bucket()
    for bucket in prior_by_day.values():
        trailing.offered += bucket.offered
        trailing.accepted += bucket.accepted

    # ---- clients -------------------------------------------------------
    day_clients = _bucket_by(rows, lambda r: r["client_key"])
    prior_clients = _bucket_by(prior_rows, lambda r: r["client_key"])
    names: dict[str, str] = {}
    for row in rows:
        if row["client_name"]:
            names.setdefault(row["client_key"], row["client_name"])
    for row in prior_rows:
        if row["client_name"]:
            names.setdefault(row["client_key"], row["client_name"])

    rows_by_client: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_client[row["client_key"]].append(row)

    clients: list[dict[str, Any]] = []
    for key, bucket in day_clients.items():
        entry = bucket.as_dict()
        baseline = prior_clients.get(key)
        baseline_rate = baseline.rate if baseline else None
        entry.update(
            {
                "client_key": key,
                "slug": key or EMPTY_CLIENT_KEY,
                "client_name": names.get(key) or "(no client name)",
                "baseline_rate": baseline_rate,
                "baseline_offered": baseline.offered if baseline else 0,
                "delta": _delta(bucket.rate, baseline_rate),
                "denial_mix": denial_mix(rows_by_client[key], top=3),
            }
        )
        clients.append(entry)
    clients = sort_clients(clients, sort, direction)

    # ---- service classes ----------------------------------------------
    mapping = policy_map()
    service_rows = []
    for name, bucket in _bucket_by(rows, lambda r: r["service_class"]).items():
        entry = bucket.as_dict()
        entry["service_class"] = name
        entry["policy"] = mapping.get(name)
        service_rows.append(entry)
    service_rows.sort(key=lambda e: (-e["offered"], e["service_class"]))

    # ---- hour profile --------------------------------------------------
    by_hour = _bucket_by(rows, lambda r: r["offered_local"].hour)
    hours = []
    for hour in range(24):
        bucket = by_hour.get(hour)
        hours.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "offered": bucket.offered if bucket else 0,
                "accepted": bucket.accepted if bucket else 0,
                "rate": bucket.rate if bucket else None,
            }
        )

    variance = policy_variance(rows)

    return {
        "date": day,
        "prev_date": day - timedelta(days=1),
        "next_date": day + timedelta(days=1),
        "is_today": day == today_local(),
        "totals": totals.as_dict(),
        "yesterday": yesterday_bucket.as_dict() if yesterday_bucket else None,
        "trailing_7d": {
            "offered": trailing.offered,
            "accepted": trailing.accepted,
            "rate": ratio(trailing.accepted, trailing.offered),
        },
        "delta_yesterday": _delta(totals.rate, yesterday_bucket.rate if yesterday_bucket else None),
        "delta_7d": _delta(totals.rate, ratio(trailing.accepted, trailing.offered)),
        "clients": clients,
        "sort": sort if sort in CLIENT_SORTS else "volume",
        "direction": "asc" if direction == "asc" else "desc",
        "service_classes": service_rows,
        "hours": hours,
        "denial_mix": denial_mix(rows, top=8),
        "variance": variance,
        "stored_metric": stored_daily_metric(day, company_id),
        "has_data": bool(rows),
    }


def stored_daily_metric(day: _date, company_id: str | None = None) -> dict[str, Any] | None:
    """This company's metrics_daily row for ``day``, if one was computed.

    Shown next to the recomputation so a divergence is visible rather than
    silently resolved in favour of one of them. Unfiltered it would show
    whichever company's row happened to be first, which is a divergence
    invented by the query.
    """
    try:
        with core_db.get_session(commit=False) as session:
            row = session.execute(
                select(MetricsDaily)
                .where(MetricsDaily.company_id == _companies.resolve_company_id(company_id))
                .where(MetricsDaily.date == day)
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "date": row.date,
                "metrics": row.metrics or {},
                "rules_version": row.rules_version,
                "computed_at": to_local(row.computed_at),
            }
    except Exception:
        return None


# ==========================================================================
# View 3 -- Clients
# ==========================================================================


@for_company
def clients_overview(
    company_id: str | None = None,
    sort: str = "volume",
    direction: str = "desc",
    sparkline_days: int = 14,
) -> dict[str, Any]:
    """One row per client with 24h / 7d / 30d rates, a volume sparkline and a
    denial reason mix."""
    today = today_local()
    now = now_local()

    start, end = local_span_bounds(today - timedelta(days=29), today)
    rows = fetch_requests(start, end, company_id=company_id)

    cutoff_24h = now - timedelta(hours=24)
    first_7d = today - timedelta(days=6)
    spark_first = today - timedelta(days=sparkline_days - 1)

    names: dict[str, str] = {}
    b24: dict[str, Bucket] = defaultdict(Bucket)
    b7: dict[str, Bucket] = defaultdict(Bucket)
    b30: dict[str, Bucket] = defaultdict(Bucket)
    spark: dict[str, dict[_date, int]] = defaultdict(dict)
    per_client_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    last_seen: dict[str, datetime] = {}

    for row in rows:
        key = row["client_key"]
        moment = row["offered_local"]
        day = moment.date()
        if row["client_name"]:
            names.setdefault(key, row["client_name"])
        b30[key].add(row)
        if day >= first_7d:
            b7[key].add(row)
        if moment >= cutoff_24h:
            b24[key].add(row)
        if day >= spark_first:
            spark[key][day] = spark[key].get(day, 0) + 1
        per_client_rows[key].append(row)
        if key not in last_seen or moment > last_seen[key]:
            last_seen[key] = moment

    spark_days = [spark_first + timedelta(days=offset) for offset in range(sparkline_days)]

    entries: list[dict[str, Any]] = []
    for key, bucket in b30.items():
        counts = [spark[key].get(day, 0) for day in spark_days]
        rate_24h = b24[key].rate if key in b24 else None
        rate_7d = b7[key].rate if key in b7 else None
        entries.append(
            {
                "client_key": key,
                "slug": key or EMPTY_CLIENT_KEY,
                "client_name": names.get(key) or "(no client name)",
                "offered": bucket.offered,
                "accepted": bucket.accepted,
                "denied": bucket.denied,
                "expired": bucket.expired,
                "canceled": bucket.canceled,
                "pending": bucket.pending,
                "rate": bucket.rate,
                "band": rate_band(bucket.rate),
                "offered_24h": b24[key].offered if key in b24 else 0,
                "rate_24h": rate_24h,
                "band_24h": rate_band(rate_24h),
                "offered_7d": b7[key].offered if key in b7 else 0,
                "rate_7d": rate_7d,
                "band_7d": rate_band(rate_7d),
                "offered_30d": bucket.offered,
                "rate_30d": bucket.rate,
                "band_30d": rate_band(bucket.rate),
                "delta": _delta(rate_7d, bucket.rate),
                "sparkline": counts,
                "sparkline_points": sparkline_points(counts),
                "sparkline_peak": max(counts) if counts else 0,
                "denial_mix": denial_mix(per_client_rows[key], top=3),
                "last_seen": last_seen.get(key),
            }
        )

    # ---- no-response, as a first-class column --------------------------
    #
    # Merged in from the missed-work model rather than derived from the
    # canonical `expired` status, because canonical `expired` covers three
    # different portal statuses -- Expired, Another Provider Responded and
    # Accept Failed -- and only the first two are "nobody answered". The agent
    # buckets on the numeric status code, which can tell them apart.
    #
    # Merge is by client_key. A row whose client_key the model did not produce
    # keeps `no_response_rate: None` and renders as an em-dash, which is the
    # correct reading of "we do not know", not "zero".
    misses = client_no_response(days=30, company_id=company_id)
    by_key = misses["by_client_key"]
    for entry in entries:
        stats = by_key.get(entry["client_key"]) or {}
        entry["no_response"] = stats.get("no_response")
        entry["no_response_rate"] = stats.get("no_response_rate")
        entry["no_response_band"] = miss_band(stats.get("no_response_rate"))
        entry["no_response_gap_pp"] = stats.get("gap_pp")
        entry["no_response_outlier"] = bool(stats.get("is_outlier"))
        entry["no_response_best"] = bool(stats.get("is_best"))
        entry["missed"] = stats.get("missed")
        entry["recoverable"] = stats.get("recoverable")

    entries = sort_clients(entries, sort, direction)
    overall = _totals(rows)

    return {
        "clients": entries,
        "totals": overall.as_dict(),
        "client_count": len(entries),
        "window_days": 30,
        "missed_available": misses["available"],
        "missed_error": misses["error"],
        "no_response_findings": misses["findings"],
        "no_response_thresholds": misses["thresholds"],
        "no_response_best_rate": misses["best_rate"],
        "no_response_outliers": misses["outlier_count"],
        "ranking_note": RANKING_NOTE,
        "sparkline_days": sparkline_days,
        "sparkline_first": spark_first,
        "sparkline_last": today,
        "sort": sort if sort in CLIENT_SORTS else "volume",
        "direction": "asc" if direction == "asc" else "desc",
        "has_data": bool(rows),
    }


@for_company
def client_detail(
    slug: str,
    days: int = 30,
    limit: int = 400,
    company_id: str | None = None,
) -> dict[str, Any]:
    """Request history and rate trajectory for a single client."""
    client_key = "" if slug == EMPTY_CLIENT_KEY else slug
    today = today_local()
    start, end = local_span_bounds(today - timedelta(days=days - 1), today)
    rows = fetch_requests(start, end, company_id=company_id, client_key=client_key)
    rows.sort(key=lambda r: r["offered_at"], reverse=True)

    name = ""
    for row in rows:
        if row["client_name"]:
            name = row["client_name"]
            break

    totals = _totals(rows)
    by_day = _bucket_by(rows, lambda r: r["offered_local"].date())
    trajectory = []
    for offset in range(days):
        day = today - timedelta(days=days - 1 - offset)
        bucket = by_day.get(day)
        trajectory.append(
            {
                "date": day.isoformat(),
                "offered": bucket.offered if bucket else 0,
                "accepted": bucket.accepted if bucket else 0,
                "rate": bucket.rate if bucket else None,
            }
        )

    services = []
    for service, bucket in _bucket_by(rows, lambda r: r["service_class"]).items():
        entry = bucket.as_dict()
        entry["service_class"] = service
        services.append(entry)
    services.sort(key=lambda e: -e["offered"])

    return {
        "client_key": client_key,
        "slug": slug,
        "client_name": name or ("(no client name)" if not client_key else client_key),
        "days": days,
        "totals": totals.as_dict(),
        "trajectory": trajectory,
        "services": services,
        "denial_mix": denial_mix(rows, top=10),
        "requests": rows[:limit],
        "truncated": len(rows) > limit,
        "row_count": len(rows),
        "has_data": bool(rows),
    }


# ==========================================================================
# View 4 -- Trends
# ==========================================================================

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@for_company
def trends_snapshot(
    weeks: int = 8,
    company_id: str | None = None,
    max_series: int = 6,
    min_cell_sample: int = 3,
) -> dict[str, Any]:
    """Hour-of-week heatmap, client rate trajectories and offer volume."""
    weeks = max(1, min(int(weeks or 8), 26))
    today = today_local()
    days = weeks * 7
    first_day = today - timedelta(days=days - 1)
    start, end = local_span_bounds(first_day, today)
    rows = fetch_requests(start, end, company_id=company_id)

    # ---- hour of week heatmap -----------------------------------------
    cells: dict[tuple[int, int], Bucket] = defaultdict(Bucket)
    for row in rows:
        moment = row["offered_local"]
        cells[(moment.weekday(), moment.hour)].add(row)

    peak_offered = max((bucket.offered for bucket in cells.values()), default=0)
    heatmap = []
    for weekday in range(7):
        cells_row = []
        for hour in range(24):
            bucket = cells.get((weekday, hour))
            offered = bucket.offered if bucket else 0
            value = bucket.rate if bucket else None
            cells_row.append(
                {
                    "weekday": weekday,
                    "hour": hour,
                    "offered": offered,
                    "accepted": bucket.accepted if bucket else 0,
                    "rate": value,
                    "band": rate_band(value),
                    # 0..6 index into the sequential blue ramp; -1 means no data
                    "step": -1 if value is None else min(6, int(value * 7)),
                    "low_sample": 0 < offered < min_cell_sample,
                    "share": ratio(offered, peak_offered) if peak_offered else None,
                }
            )
        heatmap.append({"weekday": weekday, "label": WEEKDAY_LABELS[weekday], "cells": cells_row})

    # ---- daily volume --------------------------------------------------
    by_day = _bucket_by(rows, lambda r: r["offered_local"].date())
    volume = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        bucket = by_day.get(day)
        volume.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%b %d"),
                "offered": bucket.offered if bucket else 0,
                "accepted": bucket.accepted if bucket else 0,
                "rate": bucket.rate if bucket else None,
            }
        )

    # ---- weekly client trajectories ------------------------------------
    volumes: Counter[str] = Counter()
    names: dict[str, str] = {}
    for row in rows:
        volumes[row["client_key"]] += 1
        if row["client_name"]:
            names.setdefault(row["client_key"], row["client_name"])

    top_keys = [key for key, _ in volumes.most_common(max_series)]
    week_starts = sorted({week_start_for(first_day + timedelta(days=offset)) for offset in range(days)})

    weekly: dict[tuple[str, _date], Bucket] = defaultdict(Bucket)
    for row in rows:
        key = row["client_key"]
        if key in top_keys:
            weekly[(key, week_start_for(row["offered_local"].date()))].add(row)

    trajectories = []
    for key in top_keys:
        points = []
        for week in week_starts:
            bucket = weekly.get((key, week))
            points.append(
                {
                    "week": week.isoformat(),
                    "rate": bucket.rate if bucket else None,
                    "offered": bucket.offered if bucket else 0,
                }
            )
        trajectories.append(
            {
                "client_key": key,
                "slug": key or EMPTY_CLIENT_KEY,
                "client_name": names.get(key) or "(no client name)",
                "offered": volumes[key],
                "points": points,
            }
        )

    overall_weekly = []
    weekly_all = _bucket_by(rows, lambda r: week_start_for(r["offered_local"].date()))
    for week in week_starts:
        bucket = weekly_all.get(week)
        overall_weekly.append(
            {
                "week": week.isoformat(),
                "label": week.strftime("%b %d"),
                "offered": bucket.offered if bucket else 0,
                "accepted": bucket.accepted if bucket else 0,
                "rate": bucket.rate if bucket else None,
            }
        )

    # ---- coverage trend --------------------------------------------------
    #
    # Is the coverage gap closing? The single most important line on this tab,
    # because it is the one the owner can move. Computed here, in Python, over
    # the rows already in hand -- calling coverage_analysis() once per week
    # would re-read and re-classify the same rows N times for the same answer.
    coverage_weekly = _coverage_weekly(rows, week_starts)

    return {
        "weeks": weeks,
        "days": days,
        "first_day": first_day,
        "last_day": today,
        "heatmap": heatmap,
        "hours": list(range(24)),
        "min_cell_sample": min_cell_sample,
        "peak_offered": peak_offered,
        "volume": volume,
        "week_labels": [week.isoformat() for week in week_starts],
        "trajectories": trajectories,
        "overall_weekly": overall_weekly,
        "coverage_weekly": coverage_weekly,
        "totals": _totals(rows).as_dict(),
        "basis_note": basis_note(),
        "has_data": bool(rows),
    }


def _coverage_weekly(
    rows: Sequence[dict[str, Any]], week_starts: Sequence[_date]
) -> list[dict[str, Any]]:
    """Per week: unanswered inside the staffed window versus outside it.

    Both series always, never one. A falling uncovered miss rate means nothing
    on its own -- it could be a quiet fortnight -- and only reads as progress
    next to the covered line that did not move.
    """
    rules = rules_snapshot()
    default_label = uncovered_label(rules)
    try:
        module = _missed_work_module()
        if module is not None:
            default_label = str(
                ((rules.get("missed_work") or {}).get("coverage") or {}).get("default_label")
                or default_label
            )
    except Exception:  # pragma: no cover - defensive
        pass

    tally: dict[tuple[_date, bool], dict[str, int]] = defaultdict(
        lambda: {"offers": 0, "no_response": 0, "accepted": 0}
    )
    for row in rows:
        week = week_start_for(row["offered_local"].date())
        covered = coverage_of(row, rules) != default_label
        cell = tally[(week, covered)]
        cell["offers"] += 1
        bucket = bucket_of(row, rules)
        if bucket == "no_response":
            cell["no_response"] += 1
        elif bucket == "won":
            cell["accepted"] += 1

    out: list[dict[str, Any]] = []
    for week in week_starts:
        entry: dict[str, Any] = {"week": week.isoformat(), "label": week.strftime("%b %d")}
        for covered, prefix in ((False, "uncovered"), (True, "covered")):
            cell = tally.get((week, covered)) or {"offers": 0, "no_response": 0, "accepted": 0}
            rate = ratio(cell["no_response"], cell["offers"])
            entry[f"{prefix}_offers"] = cell["offers"]
            entry[f"{prefix}_no_response"] = cell["no_response"]
            entry[f"{prefix}_rate"] = rate
            entry[f"{prefix}_band"] = miss_band(rate)
        out.append(entry)
    return out


# ==========================================================================
# View 5 -- Rules
# ==========================================================================


def unclassified_service_types(
    days: int = 60, limit: int = 100, company_id: str | None = None
) -> list[dict[str, Any]]:
    """Distinct verbatim service strings the classifier could not place.

    ``service_type_raw`` is immutable, so this list *is* the backlog: map a
    string in rules.yaml, run ``backfill``, and the row leaves the list without
    the original data ever having been touched.
    """
    today = today_local()
    start, end = local_span_bounds(today - timedelta(days=days - 1), today)
    counter: Counter[str] = Counter()
    last_seen: dict[str, datetime] = {}
    for row in fetch_requests(start, end, company_id=company_id):
        if row["service_class"] not in ("unclassified", ""):
            continue
        raw = (row.get("service_type_raw") or "").strip()
        label = raw or "(blank)"
        counter[label] += 1
        moment = row["offered_local"]
        if label not in last_seen or moment > last_seen[label]:
            last_seen[label] = moment
    return [
        {"service_type_raw": label, "count": count, "last_seen": last_seen.get(label)}
        for label, count in counter.most_common(limit)
    ]


@for_company
def rules_view(company_id: str | None = None) -> dict[str, Any]:
    """Current rules, pending proposals and this company's unclassified backlog.

    The rules TEXT shown is the global config/rules.yaml, because that is the
    file the accept-a-proposal button writes to. ``rules`` is the EFFECTIVE
    document -- global plus this company's overrides -- because that is what
    actually classified the rows on every other tab. The page names both.
    """
    result: dict[str, Any] = {
        "rules_text": "",
        "rules_path": None,
        "rules_error": None,
        "rules": {},
        "rules_version": None,
        "proposed_path": None,
        "proposals": [],
        "proposal_counts": {"pending": 0, "accepted": 0, "rejected": 0},
        "proposals_error": None,
        "unclassified": [],
        "service_classes": [],
        "alerts": [],
        "config_digests": {},
    }

    try:
        path = CONFIG.path_for("rules")
        result["rules_path"] = str(path)
        result["rules_text"] = path.read_text(encoding="utf-8")
        # The EFFECTIVE rules -- global plus this company's overrides -- because
        # they are what classified every row on every other tab. The text above
        # is the global file, which is what /rules writes to.
        result["rules"] = company_rules()
        result["rules_version"] = CONFIG.rules_version()
        result["company"] = current_company()
        result["company_overrides"] = bool(result["rules"] != CONFIG.get_rules())
    except (ConfigError, OSError) as exc:
        result["rules_error"] = redact(f"{type(exc).__name__}: {exc}")

    rules = result["rules"] or {}
    mapping = policy_map()
    for name, body in (rules.get("service_classes") or {}).items():
        if str(name).startswith("_"):
            continue
        body = body if isinstance(body, dict) else {}
        result["service_classes"].append(
            {
                "name": str(name),
                "match_any": [str(item) for item in (body.get("match_any") or [])],
                "policy": mapping.get(str(name)),
            }
        )
    result["default_class"] = (rules.get("service_classes") or {}).get("_default", "unclassified")
    for alert in rules.get("alerts") or []:
        if isinstance(alert, dict):
            result["alerts"].append(
                {
                    "id": str(alert.get("id") or "?"),
                    "when": str(alert.get("when") or ""),
                    "severity": str(alert.get("severity") or ""),
                }
            )

    try:
        result["proposed_path"] = str(CONFIG.path_for("rules_proposed"))
        proposed = CONFIG.get_proposed_rules() or {}
        proposals = []
        for entry in proposed.get("proposals") or []:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status") or "pending").strip().casefold()
            if status not in result["proposal_counts"]:
                status = "pending"
            result["proposal_counts"][status] += 1
            proposals.append(
                {
                    "id": str(entry.get("id") or ""),
                    "status": status,
                    "proposed_at": entry.get("proposed_at"),
                    "source_report": entry.get("source_report"),
                    "target": entry.get("target"),
                    "rationale": (entry.get("rationale") or "").strip()
                    if isinstance(entry.get("rationale"), str)
                    else entry.get("rationale"),
                    "evidence": entry.get("evidence") or {},
                    "patch": entry.get("patch") or {},
                    "patch_yaml": _dump_yaml(entry.get("patch") or {}),
                    "reviewed_by": entry.get("reviewed_by"),
                    "reviewed_at": entry.get("reviewed_at"),
                }
            )
        proposals.sort(key=lambda p: (p["status"] != "pending", str(p.get("proposed_at") or "")))
        result["proposals"] = proposals
    except (ConfigError, OSError) as exc:
        result["proposals_error"] = redact(f"{type(exc).__name__}: {exc}")

    try:
        result["unclassified"] = unclassified_service_types(company_id=company_id)
    except Exception as exc:
        result["unclassified_error"] = redact(f"{type(exc).__name__}: {exc}")

    try:
        result["config_digests"] = CONFIG.snapshot()
    except Exception:
        result["config_digests"] = {}

    result["has_data"] = has_any_data(company_id)
    return result


def _dump_yaml(value: Any) -> str:
    """Render a fragment back to YAML for display. Never raises."""
    try:
        import yaml

        return yaml.safe_dump(value, sort_keys=False, default_flow_style=False, allow_unicode=True).rstrip()
    except Exception:
        return repr(value)


# ==========================================================================
# View 6 -- Health
# ==========================================================================


def last_run_summary(company_id: str | None = None) -> dict[str, Any] | None:
    """This company's most recent run, for the staleness banner.

    Per company, because "the pipeline ran" is not a fact about the install --
    it is a fact about one tenant. Another company's healthy 06:00 run must not
    clear the banner for a company whose pull has been failing for two days.
    """
    try:
        with core_db.get_session(commit=False) as session:
            run = session.execute(
                select(Run)
                .where(Run.company_id == _companies.resolve_company_id(company_id))
                .order_by(Run.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                return None
            started = to_local(run.started_at)
            age = None
            if started is not None:
                age = (now_local() - started).total_seconds() / 3600.0
            return {
                "run_id": run.run_id,
                "report_type": run.report_type,
                "status": run.status,
                "row_count": run.row_count,
                "started_at": started,
                "finished_at": to_local(run.finished_at),
                "age_hours": age,
                "error_message": redact(run.error_message) if run.error_message else None,
            }
    except Exception:
        return None


def last_recovery(company_id: str | None = None) -> datetime | None:
    """When this company's pipeline last produced a good run, local-aware.

    The clock that tells a recorded failure it is history. ``partial`` counts,
    for the same reason the watchdog counts it: the numbers were produced.
    Returns ``None`` when nothing has ever succeeded, which reads as "no
    recovery", so a failure on a pipeline that has never worked stays up.
    """
    try:
        with core_db.get_session(commit=False) as session:
            run = session.execute(
                select(Run)
                .where(
                    Run.company_id == _companies.resolve_company_id(company_id),
                    Run.status.in_(("succeeded", "partial")),
                )
                .order_by(Run.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                return None
            return to_local(run.finished_at or run.started_at)
    except Exception:
        return None


#: How long a recorded pipeline_failure keeps the banner up WHEN NOTHING HAS
#: SUCCEEDED SINCE. Long enough that a failure overnight is still on the board
#: when the owner opens it in the morning. A later successful run retires it
#: sooner -- see the recovery check in :func:`pipeline_banner` -- because this
#: timer alone cannot tell "still broken" from "fixed an hour ago", and a red
#: banner over a pipeline that is running again is a false alarm that teaches
#: the owner to ignore the real one.
PIPELINE_FAILURE_BANNER_HOURS: float = 24.0


@for_company
def pipeline_banner(company_id: str | None = None) -> dict[str, Any] | None:
    """The one thing on this board that replaces the text message.

    THERE IS NO SMS AND NO EMAIL. config/notifications.yaml ships with every
    route disabled, because the dashboard is the delivery mechanism. That is a
    reasonable trade for a report -- a report is something you go and look at --
    but it is not a reasonable trade for a *failure*, because the whole point of
    a failure notification is that it finds you. So the failure has to find the
    owner here instead: a persistent banner rendered by ``base.html`` on EVERY
    tab, not a line on the Health page that nobody opens when everything looks
    normal, which is exactly when the pipeline is broken.

    Three conditions raise it, in descending severity, and the worst one wins:

    1. **The last scheduled run failed.** ``runs.status == 'failed'``.
    2. **A report is overdue.** The scheduler's own watchdog logic, asked
       read-only (``core.scheduler.overdue_reports``) so a page view does not
       fire an alert. This is the condition a try/except cannot catch: the
       container was redeployed and the scheduler never came back, the cron
       entry was removed, RUN_SCHEDULER was left false on the service.
    3. **A pipeline_failure was recorded recently** and delivery was disabled,
       so nothing was sent about it anywhere else -- and *no run has succeeded
       since it fired*. Conditions 1 and 2 read live state and clear themselves
       the moment the pipeline produces again; this one reads a row in a log
       that nothing ever deletes, so it needs to be told when it is over.

    Returns ``None`` when the pipeline is healthy -- a banner that is always
    there is furniture, and gets read as furniture. Never raises: the banner is
    on every page, and a broken banner must not be able to take the board down.
    """
    try:
        candidates: list[dict[str, Any]] = []

        last_run = last_run_summary(company_id)
        if last_run and str(last_run.get("status") or "").strip().lower() == "failed":
            candidates.append(
                {
                    "level": "error",
                    "rank": 3,
                    "title": "The last pipeline run failed.",
                    "detail": (
                        last_run.get("error_message")
                        or "No error message was recorded."
                    ),
                    "hint": (
                        f"The {last_run.get('report_type') or 'last'} run started "
                        f"{last_run.get('age_hours'):.1f}h ago. Numbers below are "
                        "incomplete until it succeeds."
                    )
                    if isinstance(last_run.get("age_hours"), (int, float))
                    else "Numbers below are incomplete until it succeeds.",
                }
            )

        for finding in _overdue_reports(company_id):
            hours = float(finding.get("overdue_seconds") or 0) / 3600.0
            last = finding.get("last_success")
            candidates.append(
                {
                    "level": "error",
                    "rank": 4,
                    "title": f"The {finding.get('report_type')} report is overdue.",
                    "detail": (
                        f"Nothing has succeeded for {hours:.1f}h. Last success: "
                        + (
                            to_local(last).strftime("%b %d, %H:%M")
                            if isinstance(last, datetime)
                            else "never"
                        )
                        + "."
                    ),
                    "hint": (
                        "The scheduler is not running, or its jobs are failing. Nothing is "
                        "sent by SMS or email, so this banner is the only warning."
                    ),
                }
            )

        # A failure that a later run has already superseded is history, and
        # history belongs on Health, not on a red banner on every tab. Nothing
        # in this system ever sets alerts_fired.acknowledged, so without this
        # the 24h timer is the ONLY way down: the pipeline could be fixed and
        # running hourly and the board would still be shouting about the run
        # that broke at 22:00. That trains the owner to read the banner as
        # decoration, which costs us the one alarm we have.
        recovered_at = last_recovery(company_id)

        for alert in recent_alerts(limit=25, company_id=company_id):
            if alert.get("alert_id") != "pipeline_failure":
                continue
            fired = alert.get("fired_at")
            if not isinstance(fired, datetime):
                continue
            age_hours = (now_local() - fired).total_seconds() / 3600.0
            if age_hours > PIPELINE_FAILURE_BANNER_HOURS or age_hours < 0:
                continue
            if alert.get("acknowledged"):
                continue
            if recovered_at is not None and recovered_at > fired:
                continue
            payload = alert.get("payload") or {}
            candidates.append(
                {
                    "level": "error",
                    "rank": 2,
                    "title": "A pipeline failure was recorded.",
                    "detail": redact(
                        str(payload.get("error") or payload.get("stage") or alert.get("entity") or "")
                    )[:400]
                    or "See Health for the details.",
                    "hint": (
                        f"{age_hours:.1f}h ago"
                        + (
                            " - notification routes are disabled, so this banner is the "
                            "only warning."
                            if alert.get("suppressed_reason") == "delivery_disabled"
                            else "."
                        )
                    ),
                }
            )
            break

        if not candidates:
            return None

        best = max(candidates, key=lambda item: item["rank"])
        best["others"] = len(candidates) - 1
        return best
    except Exception:  # pragma: no cover - the banner must never break a page
        logger.exception("could not build the pipeline banner")
        return None


def _overdue_reports(company_id: str | None = None) -> list[dict[str, Any]]:
    """``core.scheduler.overdue_reports`` without importing it at module scope.

    Deferred because ``core.scheduler`` pulls in APScheduler, and the query layer
    is imported by things that have no business starting a scheduler.

    ALWAYS SCOPED. This feeds the banner and the Health page, which are the
    whole alarm system now that no report is texted or emailed. Asked
    roster-wide it answers with whichever company ran most recently, so a tenant
    that has been silent for a week reads its neighbour's healthy run and shows
    itself green.
    """
    try:
        from ..core.scheduler import overdue_reports

        return overdue_reports(company_id=_companies.resolve_company_id(company_id))
    except Exception:
        logger.debug("could not evaluate overdue reports", exc_info=True)
        return []


def recent_alerts(limit: int = 20, company_id: str | None = None) -> list[dict[str, Any]]:
    """This company's most recent alerts, newest first."""
    try:
        with core_db.get_session(commit=False) as session:
            alerts = (
                session.execute(
                    select(AlertFired)
                    .where(
                        AlertFired.company_id
                        == _companies.resolve_company_id(company_id)
                    )
                    .order_by(AlertFired.fired_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [
                {
                    "id": alert.id,
                    "alert_id": alert.alert_id,
                    "entity": alert.entity,
                    "severity": (alert.severity or "").strip().casefold(),
                    "fired_at": to_local(alert.fired_at),
                    "payload": alert.payload or {},
                    "acknowledged": bool(alert.acknowledged),
                    "suppressed_reason": alert.suppressed_reason,
                }
                for alert in alerts
            ]
    except Exception:
        return []


@for_company
def health_view(run_limit: int = 60, company_id: str | None = None) -> dict[str, Any]:
    """Run history, last success per report type, failures and row counts.

    The first place to look when a number seems wrong, so it deliberately shows
    the boring things: which runs happened, how many rows each pulled, how that
    compares with the run before it, and what the pipeline last complained about.
    """
    result: dict[str, Any] = {
        "database": {},
        "runs": [],
        "last_success": {},
        "failures": [],
        "counts": {},
        "coverage": [],
        "company": current_company(),
        "alerts": recent_alerts(limit=20, company_id=company_id),
        "config_digests": {},
        "rules_version": None,
        # Is anything in THIS container actually scheduled? With the board as the
        # only delivery mechanism, "the web service is up but nothing refreshes
        # it" is the failure that looks most like everything being fine, and it
        # is otherwise unanswerable from a browser.
        "scheduler": {},
        "overdue": [],
        "storage_warning": None,
        "timezone": str(local_tz()),
        "generated_at": now_local(),
        "error": None,
    }

    try:
        result["database"] = core_db.healthcheck()
        result["storage_warning"] = core_db.warn_if_ephemeral_sqlite()
    except Exception as exc:  # pragma: no cover - healthcheck swallows its own
        result["database"] = {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}

    try:
        from ..core.scheduler import background_scheduler_status

        result["scheduler"] = background_scheduler_status()
    except Exception as exc:  # pragma: no cover - defensive
        result["scheduler"] = {"error": redact(f"{type(exc).__name__}: {exc}")}

    result["overdue"] = _overdue_reports(company_id)

    try:
        result["config_digests"] = CONFIG.snapshot()
        result["rules_version"] = CONFIG.rules_version()
    except Exception as exc:
        result["config_error"] = redact(f"{type(exc).__name__}: {exc}")

    try:
        with core_db.get_session(commit=False) as session:
            runs = (
                session.execute(
                    select(Run)
                    .where(Run.company_id == company_id)
                    .order_by(Run.started_at.desc())
                    .limit(run_limit)
                )
                .scalars()
                .all()
            )

            # Previous run of the same report type, for the row-count diff.
            previous: dict[str, int | None] = {}
            entries: list[dict[str, Any]] = []
            for run in reversed(runs):  # oldest first so "previous" is meaningful
                report_type = run.report_type or "unknown"
                prior = previous.get(report_type)
                delta = None
                if prior is not None and run.row_count is not None:
                    delta = run.row_count - prior
                entries.append(
                    {
                        "run_id": run.run_id,
                        "company_id": run.company_id,
                        "report_type": report_type,
                        "status": (run.status or "").strip().casefold(),
                        "window_start": to_local(run.window_start),
                        "window_end": to_local(run.window_end),
                        "page_size": run.page_size,
                        "row_count": run.row_count,
                        "row_delta": delta,
                        "rules_version": run.rules_version,
                        "started_at": to_local(run.started_at),
                        "finished_at": to_local(run.finished_at),
                        "duration_s": (
                            (run.finished_at - run.started_at).total_seconds()
                            if run.finished_at and run.started_at
                            else None
                        ),
                        "error_message": redact(run.error_message) if run.error_message else None,
                        "source_file": run.source_file,
                    }
                )
                if run.row_count is not None:
                    previous[report_type] = run.row_count
            entries.reverse()  # newest first for display
            result["runs"] = entries
            result["failures"] = [e for e in entries if e["status"] in ("failed", "partial")]

            for report_type in sorted({e["report_type"] for e in entries}):
                success = next(
                    (
                        e
                        for e in entries
                        if e["report_type"] == report_type and e["status"] == "succeeded"
                    ),
                    None,
                )
                last_any = next((e for e in entries if e["report_type"] == report_type), None)
                age = None
                if success and success["started_at"]:
                    age = (now_local() - success["started_at"]).total_seconds() / 3600.0
                result["last_success"][report_type] = {
                    "run": success,
                    "last_attempt": last_any,
                    "age_hours": age,
                }

            # EVERY count is this company's. A roster-wide row count on a page
            # headed with one company's name is the most quietly misleading
            # number the dashboard could show: it looks like the answer to
            # "how much data do I have" and is the answer to somebody else's.
            def _count(model: Any, *clauses: Any) -> int:
                statement = (
                    select(func.count())
                    .select_from(model)
                    .where(model.company_id == company_id)
                )
                for clause in clauses:
                    statement = statement.where(clause)
                return int(session.execute(statement).scalar_one())

            result["counts"] = {
                "requests": _count(Request),
                "requests_without_offered_at": _count(
                    Request, Request.offered_at.is_(None)
                ),
                "requests_unclassified": _count(
                    Request,
                    (Request.service_class.is_(None))
                    | (Request.service_class == "unclassified"),
                ),
                "runs": _count(Run),
                "metrics_hourly": _count(MetricsHourly),
                "metrics_daily": _count(MetricsDaily),
                "metrics_weekly": _count(MetricsWeekly),
                "client_daily": _count(ClientDaily),
                "alerts_fired": _count(AlertFired),
            }
            result["companies_with_data"] = companies_with_data()

            earliest, latest = session.execute(
                select(func.min(Request.offered_at), func.max(Request.offered_at)).where(
                    Request.company_id == company_id
                )
            ).one()
            result["earliest_request"] = to_local(earliest)
            result["latest_request"] = to_local(latest)
    except Exception as exc:
        result["error"] = redact(f"{type(exc).__name__}: {exc}")

    # Recompute-vs-stored coverage for the last 14 local days.
    try:
        today = today_local()
        start, end = local_span_bounds(today - timedelta(days=13), today)
        rows = fetch_requests(start, end, company_id=company_id)
        by_day = _bucket_by(rows, lambda r: r["offered_local"].date())
        with core_db.get_session(commit=False) as session:
            stored = {
                row.date: row
                for row in session.execute(
                    select(MetricsDaily)
                    .where(MetricsDaily.company_id == company_id)
                    .where(MetricsDaily.date >= today - timedelta(days=13))
                ).scalars()
            }
        coverage = []
        for offset in range(14):
            day = today - timedelta(days=13 - offset)
            bucket = by_day.get(day)
            stored_row = stored.get(day)
            stored_offered = None
            if stored_row is not None and isinstance(stored_row.metrics, dict):
                stored_offered = stored_row.metrics.get("offered")
            live_offered = bucket.offered if bucket else 0
            coverage.append(
                {
                    "date": day,
                    "live_offered": live_offered,
                    "live_accepted": bucket.accepted if bucket else 0,
                    "live_rate": bucket.rate if bucket else None,
                    "stored_offered": stored_offered,
                    "has_stored": stored_row is not None,
                    "matches": (
                        stored_offered is None or int(stored_offered) == live_offered
                        if stored_row is not None
                        else None
                    ),
                    "computed_at": to_local(stored_row.computed_at) if stored_row else None,
                }
            )
        result["coverage"] = coverage
    except Exception:
        result["coverage"] = []

    result["has_data"] = bool(result.get("counts", {}).get("requests"))
    return result


# ==========================================================================
# JSON shaping for the /api endpoints
# ==========================================================================


def jsonable(value: Any) -> Any:
    """Recursively convert dates, datetimes, Decimals and sets for JSON.

    Sets are sorted before being emitted as a list. ``missed_work``'s coverage
    definitions carry the covered weekdays as a ``frozenset``, and a JSON feed
    whose day list came out in a different order on every request would be
    impossible to diff.
    """
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        try:
            return [jsonable(item) for item in sorted(value)]
        except TypeError:  # mixed types have no total order; fall back to text
            return [jsonable(item) for item in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
