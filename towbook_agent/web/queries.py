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
from datetime import date as _date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

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
    "live_snapshot",
    "daily_snapshot",
    "clients_overview",
    "client_detail",
    "trends_snapshot",
    "rules_view",
    "health_view",
    "sparkline_points",
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
    """The presentation timezone (TZ env var, default America/Detroit).

    Storage is naive UTC; every date, hour bucket and calendar day the operator
    sees is local. Falls back to UTC if the configured zone is unknown so a bad
    TZ value degrades instead of taking the dashboard down.
    """
    name = (os.environ.get("TZ") or "").strip() or DEFAULT_TIMEZONE
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
    """Good / warn cut-offs, from rules.yaml if present."""
    thresholds = dict(DEFAULT_RATE_THRESHOLDS)
    try:
        configured = (CONFIG.get_rules().get("dashboard") or {}).get("rate_thresholds") or {}
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
    Request.account_id,
    Request.client_name,
    Request.client_key,
    Request.offered_at,
    Request.responded_at,
    Request.status,
    Request.denial_reason,
    Request.service_type_raw,
    Request.service_class,
    Request.pickup_location,
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
    data["status"] = (data.get("status") or "pending").strip().casefold() or "pending"
    data["service_class"] = (data.get("service_class") or "unclassified").strip() or "unclassified"
    data["client_key"] = (data.get("client_key") or "").strip()
    data["offered_local"] = to_local(data.get("offered_at"))
    data["responded_local"] = to_local(data.get("responded_at"))
    return data


def fetch_requests(
    start_utc: datetime,
    end_utc: datetime,
    account_id: str | None = None,
    client_key: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Requests offered in ``[start_utc, end_utc)``, newest last.

    Rows with a NULL ``offered_at`` cannot be placed on a timeline and are
    excluded here; :func:`health_view` counts them so they are never invisible.
    """
    statement = (
        select(*_REQUEST_COLUMNS)
        .where(Request.offered_at.is_not(None))
        .where(Request.offered_at >= start_utc)
        .where(Request.offered_at < end_utc)
        .order_by(Request.offered_at.asc())
    )
    if account_id:
        statement = statement.where(Request.account_id == account_id)
    if client_key is not None:
        statement = statement.where(Request.client_key == client_key)
    if limit:
        statement = statement.limit(limit)

    with core_db.get_session(commit=False) as session:
        return [_row_to_dict(row) for row in session.execute(statement).mappings()]


def has_any_data() -> bool:
    """True once a single request row exists. Drives the empty state."""
    try:
        with core_db.get_session(commit=False) as session:
            return bool(session.execute(select(func.count()).select_from(Request)).scalar_one())
    except Exception:
        return False


def available_accounts() -> list[str]:
    """Distinct account ids present in the data.

    Returns an empty list for the single-account case so the account selector
    stays off screen until multi-account is actually in use.
    """
    try:
        with core_db.get_session(commit=False) as session:
            accounts = [
                row[0]
                for row in session.execute(
                    select(Request.account_id).distinct().order_by(Request.account_id)
                )
                if row[0]
            ]
    except Exception:
        return []
    return accounts if len(accounts) > 1 else []


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
        raw = CONFIG.get_rules().get("denial_reason_normalization") or []
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
        rules = CONFIG.get_rules()
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
    name: str, days: int, account_id: str | None
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

    kwargs: dict[str, Any] = {"account_id": account_id or None}
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


def missed_work_snapshot(
    days: int = MISSED_WORK_WINDOW_DAYS, account_id: str | None = None
) -> dict[str, Any]:
    """The inventory of work we did not get -- the primary view.

    Leads with the recoverable count and the four buckets, then the
    ``(service_class, cause)`` inventory with a remedy and the clients behind
    it. Blind spots and close-off candidates appear here as summaries with a
    link to their own page.
    """
    envelope = _missed_work_call("compute_missed_work", days, account_id)
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


def blind_spots_snapshot(
    days: int = MISSED_WORK_WINDOW_DAYS, account_id: str | None = None
) -> dict[str, Any]:
    """The 7 x 24 hour-of-week grid of when offers go unanswered.

    This is the staffing-decision view. The agent returns the counts as
    matrices; the only thing added here is the presentation shape the template
    needs -- a colour step per cell, a hatch flag for a cell too small to read
    as a trend, and the hover text.
    """
    envelope = _missed_work_call("blind_spots", days, account_id)
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


def closeoff_snapshot(
    days: int = MISSED_WORK_WINDOW_DAYS, account_id: str | None = None
) -> dict[str, Any]:
    """Work we do not want, being offered anyway -- grouped by client.

    Grouped by client because the action is a conversation with that client,
    and each group carries a paste-ready summary for exactly that conversation.
    """
    envelope = _missed_work_call("closeoff_candidates", days, account_id)
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


def client_no_response(
    days: int = MISSED_WORK_WINDOW_DAYS, account_id: str | None = None
) -> dict[str, Any]:
    """Per-client no-response counts, plus which of them are outliers.

    Backs the first-class ``no_response_rate`` column on /clients. An outlier is
    a client with enough volume to judge whose offers go unanswered at least
    ``client_comparison.gap_pp`` points more often than the best client on the
    same trucks and the same staff.
    """
    envelope = _missed_work_call("client_comparison", days, account_id)
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
# View 1 -- Live
# ==========================================================================


def live_snapshot(account_id: str | None = None) -> dict[str, Any]:
    """Today so far: running totals, hour buckets, and the comparison baselines."""
    today = today_local()
    now = now_local()

    day_start, day_end = local_day_bounds(today)
    today_rows = fetch_requests(day_start, day_end, account_id=account_id)

    # 30 days ending yesterday -- the baselines must not include today, or the
    # line the operator is being measured against moves as the day fills up.
    base_start, base_end = local_span_bounds(today - timedelta(days=30), today - timedelta(days=1))
    baseline_rows = fetch_requests(base_start, base_end, account_id=account_id)

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


def daily_snapshot(
    day: _date,
    account_id: str | None = None,
    sort: str = "volume",
    direction: str = "desc",
) -> dict[str, Any]:
    """Full breakdown for one local calendar day."""
    day_start, day_end = local_day_bounds(day)
    rows = fetch_requests(day_start, day_end, account_id=account_id)

    prior_start, prior_end = local_span_bounds(day - timedelta(days=7), day - timedelta(days=1))
    prior_rows = fetch_requests(prior_start, prior_end, account_id=account_id)

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
        "stored_metric": stored_daily_metric(day),
        "has_data": bool(rows),
    }


def stored_daily_metric(day: _date) -> dict[str, Any] | None:
    """The metrics_daily row for ``day``, if the pipeline has computed one.

    Shown next to the recomputation so a divergence is visible rather than
    silently resolved in favour of one of them.
    """
    try:
        with core_db.get_session(commit=False) as session:
            row = session.execute(
                select(MetricsDaily).where(MetricsDaily.date == day)
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


def clients_overview(
    account_id: str | None = None,
    sort: str = "volume",
    direction: str = "desc",
    sparkline_days: int = 14,
) -> dict[str, Any]:
    """One row per client with 24h / 7d / 30d rates, a volume sparkline and a
    denial reason mix."""
    today = today_local()
    now = now_local()

    start, end = local_span_bounds(today - timedelta(days=29), today)
    rows = fetch_requests(start, end, account_id=account_id)

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
    misses = client_no_response(days=30, account_id=account_id)
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


def client_detail(
    slug: str,
    days: int = 30,
    limit: int = 400,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Request history and rate trajectory for a single client."""
    client_key = "" if slug == EMPTY_CLIENT_KEY else slug
    today = today_local()
    start, end = local_span_bounds(today - timedelta(days=days - 1), today)
    rows = fetch_requests(start, end, account_id=account_id, client_key=client_key)
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


def trends_snapshot(
    weeks: int = 8,
    account_id: str | None = None,
    max_series: int = 6,
    min_cell_sample: int = 3,
) -> dict[str, Any]:
    """Hour-of-week heatmap, client rate trajectories and offer volume."""
    weeks = max(1, min(int(weeks or 8), 26))
    today = today_local()
    days = weeks * 7
    first_day = today - timedelta(days=days - 1)
    start, end = local_span_bounds(first_day, today)
    rows = fetch_requests(start, end, account_id=account_id)

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
        "totals": _totals(rows).as_dict(),
        "has_data": bool(rows),
    }


# ==========================================================================
# View 5 -- Rules
# ==========================================================================


def unclassified_service_types(days: int = 60, limit: int = 100) -> list[dict[str, Any]]:
    """Distinct verbatim service strings the classifier could not place.

    ``service_type_raw`` is immutable, so this list *is* the backlog: map a
    string in rules.yaml, run ``backfill``, and the row leaves the list without
    the original data ever having been touched.
    """
    today = today_local()
    start, end = local_span_bounds(today - timedelta(days=days - 1), today)
    counter: Counter[str] = Counter()
    last_seen: dict[str, datetime] = {}
    for row in fetch_requests(start, end):
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


def rules_view() -> dict[str, Any]:
    """Current rules, pending proposals and the unclassified backlog."""
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
        result["rules"] = CONFIG.get_rules()
        result["rules_version"] = CONFIG.rules_version()
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
        result["unclassified"] = unclassified_service_types()
    except Exception as exc:
        result["unclassified_error"] = redact(f"{type(exc).__name__}: {exc}")

    try:
        result["config_digests"] = CONFIG.snapshot()
    except Exception:
        result["config_digests"] = {}

    result["has_data"] = has_any_data()
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


def last_run_summary() -> dict[str, Any] | None:
    """The most recent run of any type, for the staleness banner."""
    try:
        with core_db.get_session(commit=False) as session:
            run = session.execute(
                select(Run).order_by(Run.started_at.desc()).limit(1)
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


def recent_alerts(limit: int = 20) -> list[dict[str, Any]]:
    try:
        with core_db.get_session(commit=False) as session:
            alerts = (
                session.execute(select(AlertFired).order_by(AlertFired.fired_at.desc()).limit(limit))
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


def health_view(run_limit: int = 60) -> dict[str, Any]:
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
        "alerts": recent_alerts(limit=20),
        "config_digests": {},
        "rules_version": None,
        "timezone": str(local_tz()),
        "generated_at": now_local(),
        "error": None,
    }

    try:
        result["database"] = core_db.healthcheck()
    except Exception as exc:  # pragma: no cover - healthcheck swallows its own
        result["database"] = {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}

    try:
        result["config_digests"] = CONFIG.snapshot()
        result["rules_version"] = CONFIG.rules_version()
    except Exception as exc:
        result["config_error"] = redact(f"{type(exc).__name__}: {exc}")

    try:
        with core_db.get_session(commit=False) as session:
            runs = (
                session.execute(select(Run).order_by(Run.started_at.desc()).limit(run_limit))
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
                        "account_id": run.account_id,
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

            result["counts"] = {
                "requests": session.execute(select(func.count()).select_from(Request)).scalar_one(),
                "requests_without_offered_at": session.execute(
                    select(func.count()).select_from(Request).where(Request.offered_at.is_(None))
                ).scalar_one(),
                "requests_unclassified": session.execute(
                    select(func.count())
                    .select_from(Request)
                    .where(
                        (Request.service_class.is_(None)) | (Request.service_class == "unclassified")
                    )
                ).scalar_one(),
                "runs": session.execute(select(func.count()).select_from(Run)).scalar_one(),
                "metrics_hourly": session.execute(
                    select(func.count()).select_from(MetricsHourly)
                ).scalar_one(),
                "metrics_daily": session.execute(
                    select(func.count()).select_from(MetricsDaily)
                ).scalar_one(),
                "metrics_weekly": session.execute(
                    select(func.count()).select_from(MetricsWeekly)
                ).scalar_one(),
                "client_daily": session.execute(
                    select(func.count()).select_from(ClientDaily)
                ).scalar_one(),
                "alerts_fired": session.execute(
                    select(func.count()).select_from(AlertFired)
                ).scalar_one(),
            }

            earliest, latest = session.execute(
                select(func.min(Request.offered_at), func.max(Request.offered_at))
            ).one()
            result["earliest_request"] = to_local(earliest)
            result["latest_request"] = to_local(latest)
    except Exception as exc:
        result["error"] = redact(f"{type(exc).__name__}: {exc}")

    # Recompute-vs-stored coverage for the last 14 local days.
    try:
        today = today_local()
        start, end = local_span_bounds(today - timedelta(days=13), today)
        rows = fetch_requests(start, end)
        by_day = _bucket_by(rows, lambda r: r["offered_local"].date())
        with core_db.get_session(commit=False) as session:
            stored = {
                row.date: row
                for row in session.execute(
                    select(MetricsDaily).where(MetricsDaily.date >= today - timedelta(days=13))
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
    """Recursively convert dates, datetimes and Decimals for JSON responses."""
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
