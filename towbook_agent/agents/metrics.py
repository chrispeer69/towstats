"""Deterministic metrics aggregation over the ``requests`` table.

No LLM, no heuristics, no network. Hard constraint #3 puts the only model call
in the Analyst; everything here is plain Python over rows the ingester already
stored, so the same database always produces the same numbers.

What this module guarantees
---------------------------
*Idempotency* (hard constraint #4). Every ``compute_*`` upserts on the window
key, and the JSON document written to ``metrics_daily`` / ``metrics_weekly`` is
byte-identical when the same window is recomputed against the same rows: the
two volatile keys (``computed_at``, ``alerts``) are stripped before storage and
live in their own column / event stream instead.

*Explainability*. ``rules_version`` from config_loader is stamped on every
metrics row. Rates computed under one classification of "light_service" stay
attributable to it after somebody edits rules.yaml.

*Honest rates*. ``accepted / offered``, and ``None`` -- never ``0`` and never a
ZeroDivisionError -- when nothing was offered. Use :func:`format_rate` to render
it, which produces ``"-"`` rather than a misleading ``"0%"``.

    CAVEAT on the stored columns: ``metrics_hourly.rate``,
    ``metrics_hourly.day_running_rate`` and ``client_daily.rate`` are declared
    NOT NULL in core/models.py, so a zero-offer window persists ``0.0`` there.
    The authoritative ``None`` is in the returned dict and in the JSON blobs.
    Any consumer reading those columns directly must treat ``offered == 0`` as
    "no rate" -- :func:`rate_or_none` is the one-liner for it.

Time
----
Rows are stored naive UTC (see core/models.UTCDateTime). Report windows are
defined in *local* time from the ``TZ`` env var (default America/Detroit) and
converted to UTC before querying, via ``zoneinfo``.

An hour window is ``[start, start + 1h)`` measured in *absolute* time: the local
start is converted to UTC first and one hour is added there. That keeps hourly
windows exactly one hour long, contiguous and non-overlapping across a DST
transition, where local arithmetic would otherwise produce a gap or a repeat.

A day window is local midnight to local midnight, so a spring-forward day is
genuinely 23 hours and a fall-back day genuinely 25. ``hourly_distribution``
buckets on the *local* hour, so on a fall-back day the repeated local hour is a
single bucket carrying both passes. That is the intended reading: the buckets
answer "what does 01:00 look like", not "which absolute hour was this".

Configuration
-------------
Weekly thresholds are data, read from ``rules.yaml`` under an optional
``metrics:`` key (see :data:`DEFAULTS` for every key and its default). Nothing
here needs a code change to retune:

    metrics:
      weekly:
        volume_drop_threshold_pct: 40      # adverse volume drop, percent
        volume_drop_min_prior_offers: 10   # floor so tiny clients cannot dominate
        trend_min_offers: 5                # min current-week offers to rank a client
        outlier_limit: 10
        direction_flat_band_pp: 1.0
        capacity_min_offers: 3
      detail:
        max_rows: 200
        max_denial_examples: 3
      alerts:
        enabled: true
        client_window_hours: 24
        row_dedupe: forever                # forever | window
        dedupe_window: 4h                  # default: notifications rate_limit

Dry runs
--------
Every ``compute_*`` takes ``persist`` and ``emit_alerts``. ``emit_alerts``
defaults to ``None``, meaning "follow ``persist``", so ``--dry-run`` (which
passes ``persist=False``) cannot send a real text as a side effect of a
rehearsal. Pass ``emit_alerts=True`` explicitly to override.

Table ownership
---------------
This module writes ``metrics_hourly``, ``metrics_daily``, ``metrics_weekly``,
``metrics_monthly`` and ``client_daily``. It **reads** ``requests`` and
``alerts_fired`` and writes
neither. ``alerts_fired`` belongs to agents/notifier.py, which records every
alert it handles and reads the table back for its ``same_alert_same_entity``
rate limit; a second writer would corrupt that. Metrics reads it only to know
whether a recompute has anything new to say.
"""

from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from datetime import date as _date
from datetime import datetime, time as _time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import companies as _companies
from ..core import events
from ..core.config_loader import get_notifications, rules_version
from ..core.db import get_session
from ..core.models import (
    STATUS_VALUES,
    AlertFired,
    ClientDaily,
    MetricsDaily,
    MetricsHourly,
    MetricsMonthly,
    MetricsWeekly,
    Request,
    client_key_for,
    utcnow,
)

__all__ = [
    # contract
    "compute_hourly",
    "compute_daily",
    "compute_weekly",
    "compute_monthly",
    # helpers other modules and templates need
    "rate_or_none",
    "format_rate",
    "rate_pct",
    "local_timezone",
    "local_timezone_name",
    "to_local",
    "to_utc",
    "parse_local_datetime",
    "parse_local_date",
    "week_start_for",
    "month_start_for",
    "month_end_for",
    "recompute_days",
    "get_stored_hourly",
    "get_stored_daily",
    "get_stored_weekly",
    "get_stored_monthly",
    "DEFAULTS",
    "DEFAULT_TIMEZONE",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Detroit"

#: Every tunable, with the value used when rules.yaml does not override it.
DEFAULTS: dict[str, dict[str, Any]] = {
    "weekly": {
        "volume_drop_threshold_pct": 40.0,
        "volume_drop_min_prior_offers": 10,
        "trend_min_offers": 5,
        "outlier_limit": 10,
        "direction_flat_band_pp": 1.0,
        "capacity_min_offers": 3,
    },
    "detail": {
        "max_rows": 200,
        "max_denial_examples": 3,
    },
    "alerts": {
        "enabled": True,
        "client_window_hours": 24,
        "row_dedupe": "forever",
        "dedupe_window": "",  # empty -> notifications.rate_limit.same_alert_same_entity
    },
}

#: Keys removed from the JSON document before it is stored, so that recomputing
#: an unchanged window writes a byte-identical blob.
_VOLATILE_KEYS: tuple[str, ...] = ("computed_at", "alerts")

_WEEKDAY_LABELS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_UNCLASSIFIED_FALLBACK = "unclassified"

_ZONE_CACHE: dict[str, ZoneInfo] = {}


# ==========================================================================
# Time
# ==========================================================================


def local_timezone_name() -> str:
    """The local timezone: this company's, then ``TZ``, then Detroit.

    A coverage window is a claim about when a human was at a desk, so it is
    only meaningful in that company's own clock. A Texas tenant and an Ohio
    tenant reporting out of one install must not share one zone, and neither
    can be asked to depend on the process-wide ``TZ`` variable. The company is
    the one taking precedence; ``TZ`` remains the answer for a single-company
    install, which declares no zone of its own.
    """
    company = _companies.timezone_for()
    if company:
        return company
    return (os.environ.get("TZ") or "").strip() or DEFAULT_TIMEZONE


def local_timezone() -> ZoneInfo:
    """Return the ZoneInfo for :func:`local_timezone_name`.

    Falls back to UTC with a loud warning rather than crashing the pipeline on
    a machine with a broken tz database -- a report with the wrong offset still
    beats no report, and the warning says which happened.
    """
    name = local_timezone_name()
    cached = _ZONE_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.error("unknown timezone %r; falling back to UTC", name)
        zone = ZoneInfo("UTC")
    _ZONE_CACHE[name] = zone
    return zone


def to_utc(local_naive: datetime) -> datetime:
    """Convert a naive *local* datetime to the naive UTC used for storage."""
    if local_naive.tzinfo is not None:
        return local_naive.astimezone(timezone.utc).replace(tzinfo=None)
    aware = local_naive.replace(tzinfo=local_timezone())
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(utc_naive: datetime) -> datetime:
    """Convert a naive UTC datetime (as stored) to naive local time."""
    if utc_naive.tzinfo is None:
        utc_naive = utc_naive.replace(tzinfo=timezone.utc)
    return utc_naive.astimezone(local_timezone()).replace(tzinfo=None)


_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H",
    "%Y-%m-%d %H",
    "%Y-%m-%d",
)


def parse_local_datetime(value: datetime | _date | str) -> datetime:
    """Coerce CLI / caller input into a naive *local* datetime.

    A naive datetime is taken to already be local. An aware datetime is
    converted into local. A date becomes local midnight. A string is parsed
    with ISO first, then the short forms the CLI accepts (``2026-07-27T14``).
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(local_timezone()).replace(tzinfo=None)
        return value
    if isinstance(value, _date):
        return datetime.combine(value, _time.min)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty datetime string")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in _DATETIME_FORMATS:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"could not parse {value!r} as a datetime") from None
        if parsed.tzinfo is not None:
            return parsed.astimezone(local_timezone()).replace(tzinfo=None)
        return parsed
    raise TypeError(f"expected datetime, date or str, got {type(value).__name__}")


def parse_local_date(value: datetime | _date | str) -> _date:
    """Coerce caller input into a local calendar date."""
    if isinstance(value, datetime):
        return parse_local_datetime(value).date()
    if isinstance(value, _date):
        return value
    if isinstance(value, str):
        return parse_local_datetime(value).date()
    raise TypeError(f"expected datetime, date or str, got {type(value).__name__}")


def week_start_for(value: datetime | _date | str) -> _date:
    """Return the Monday of the local week containing ``value``."""
    day = parse_local_date(value)
    return day - timedelta(days=day.weekday())


def month_start_for(value: datetime | _date | str) -> _date:
    """Return the 1st of the local calendar month containing ``value``.

    Also accepts the ``YYYY-MM`` form the CLI takes, which
    :func:`parse_local_datetime` would otherwise reject.
    """
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 7 and text[4] in "-/" and text[:4].isdigit() and text[5:].isdigit():
            return _date(int(text[:4]), int(text[5:]), 1)
    return parse_local_date(value).replace(day=1)


def month_end_for(month_start: _date) -> _date:
    """The 1st of the FOLLOWING month -- the exclusive end of ``month_start``.

    Reached by adding 32 days and flooring to the 1st, which lands in the next
    month from any day of any month without a table of month lengths and
    without ever overshooting by two.
    """
    return (month_start.replace(day=1) + timedelta(days=32)).replace(day=1)


def _local_midnight_utc(day: _date) -> datetime:
    """Naive UTC instant of local midnight starting ``day``."""
    return to_utc(datetime.combine(day, _time.min))


def _parse_duration(text: str | None, default: timedelta) -> timedelta:
    """Parse ``"4h"`` / ``"90m"`` / ``"3600"`` into a timedelta."""
    if text is None:
        return default
    raw = str(text).strip().lower()
    if not raw:
        return default
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    multiplier = 1
    if raw[-1] in units:
        multiplier = units[raw[-1]]
        raw = raw[:-1].strip()
    try:
        return timedelta(seconds=float(raw) * multiplier)
    except ValueError:
        logger.warning("could not parse duration %r; using %s", text, default)
        return default


# ==========================================================================
# Rates and display
# ==========================================================================


def rate_or_none(accepted: int, offered: int) -> float | None:
    """``accepted / offered``, or ``None`` when nothing was offered.

    The single place this division happens. Zero offers is not a zero rate: it
    is the absence of a rate, and conflating the two turns a quiet hour into a
    fake catastrophe on the dashboard.
    """
    if not offered:
        return None
    return accepted / offered


def _round_half_up(value: float, decimals: int = 0) -> float:
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP))


def rate_pct(rate: float | None, decimals: int = 0, zero_offers: int | float = 0) -> float | int:
    """Rate as a whole-number percentage, rounded half-up. Display only.

    ``zero_offers`` is what a ``None`` rate renders as; it defaults to 0 to
    match ``notifications.yaml -> formatting.zero_offers_rate_pct``, because the
    hourly SMS has to say ``0%`` rather than break its exact shape.
    """
    if rate is None:
        return zero_offers
    value = _round_half_up(rate * 100.0, decimals)
    return int(value) if decimals <= 0 else value


def format_rate(rate: float | None, decimals: int = 0) -> str:
    """Render a rate for humans: ``"73%"``, or ``"-"`` when there is no rate."""
    if rate is None:
        return "-"
    value = _round_half_up(rate * 100.0, decimals)
    if decimals <= 0:
        return f"{int(value)}%"
    return f"{value:.{decimals}f}%"


def _pp(a: float | None, b: float | None) -> float | None:
    """Difference between two rates in percentage points, or None."""
    if a is None or b is None:
        return None
    return _round_half_up((a - b) * 100.0, 2)


# ==========================================================================
# Config access
# ==========================================================================


def _rules(company_id: str | None = None) -> dict[str, Any]:
    """config/rules.yaml with this company's overrides merged over it.

    Global rules.yaml stays the default for everyone; a company entry in
    config/companies.yaml may replace its coverage window, its job values and
    any threshold. See ``core.companies.rules_for`` for the precedence.
    """
    return _companies.rules_for(company_id) or {}


def _metrics_cfg(rules: dict[str, Any], section: str) -> dict[str, Any]:
    """Merge ``rules.yaml -> metrics.<section>`` over :data:`DEFAULTS`."""
    merged = dict(DEFAULTS.get(section, {}))
    block = rules.get("metrics")
    if isinstance(block, dict):
        override = block.get(section)
        if isinstance(override, dict):
            for key, value in override.items():
                if value is not None:
                    merged[key] = value
    return merged


def _notifications_formatting() -> dict[str, Any]:
    """Formatting block from notifications.yaml; never raises."""
    try:
        block = (get_notifications() or {}).get("formatting")
    except Exception as exc:  # pragma: no cover - config level failure
        logger.warning("could not read notifications.yaml formatting: %s", exc)
        return {}
    return block if isinstance(block, dict) else {}


def _rate_limit_window() -> timedelta:
    """The notifier's ``same_alert_same_entity`` window, as a timedelta."""
    try:
        block = (get_notifications() or {}).get("rate_limit")
        if isinstance(block, dict):
            return _parse_duration(block.get("same_alert_same_entity"), timedelta(hours=4))
    except Exception:  # pragma: no cover - config level failure
        pass
    return timedelta(hours=4)


def _service_class_order(rules: dict[str, Any]) -> tuple[list[str], str]:
    """Return ``(configured classes in file order, default class)``.

    Keys beginning with ``_`` are directives, not service classes -- that is the
    documented contract in rules.yaml, and it is why ``_default`` can sit in the
    same mapping as the real classes.
    """
    block = rules.get("service_classes")
    if not isinstance(block, dict):
        return ([], _UNCLASSIFIED_FALLBACK)
    ordered = [str(key) for key in block if not str(key).startswith("_")]
    default = str(block.get("_default") or _UNCLASSIFIED_FALLBACK)
    if default not in ordered:
        ordered.append(default)
    return (ordered, default)


def _policy_classes(rules: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(should_accept classes, should_decline classes)`` from acceptance_policy."""

    def _extract(key: str) -> list[str]:
        block = (rules.get("acceptance_policy") or {}).get(key)
        out: list[str] = []
        if isinstance(block, list):
            for entry in block:
                if isinstance(entry, dict) and entry.get("service_class"):
                    name = str(entry["service_class"])
                    if name not in out:
                        out.append(name)
                elif isinstance(entry, str) and entry not in out:
                    out.append(entry)
        return out

    if not isinstance(rules.get("acceptance_policy"), dict):
        return ([], [])
    return (_extract("should_accept"), _extract("should_decline"))


# ==========================================================================
# Classifier bridge
# ==========================================================================

_classifier_warned = False


def _classifier_module():
    """Import agents.classifier lazily, tolerating its absence.

    Metrics must stay runnable on its own -- during the build, and on a box
    where a bad edit broke the classifier. When it cannot be imported the two
    small pieces metrics needs are reimplemented below using exactly the
    semantics documented in rules.yaml.
    """
    global _classifier_warned
    try:
        from . import classifier  # noqa: PLC0415 - deliberate lazy import

        return classifier
    except Exception as exc:
        if not _classifier_warned:
            logger.warning(
                "agents.classifier unavailable (%s); metrics is using its documented "
                "fallback for classification and alert evaluation",
                exc,
            )
            _classifier_warned = True
        return None


def _fallback_classify(service_type_raw: str | None, rules: dict[str, Any]) -> str:
    """Case-insensitive substring match, file order, first match wins."""
    _, default = _service_class_order(rules)
    text = (service_type_raw or "").casefold()
    if not text:
        return default
    block = rules.get("service_classes")
    if not isinstance(block, dict):
        return default
    for name, spec in block.items():
        if str(name).startswith("_") or not isinstance(spec, dict):
            continue
        for needle in spec.get("match_any") or []:
            if needle and str(needle).casefold() in text:
                return str(name)
    return default


def _fallback_evaluate_alerts(context: dict[str, Any], rules: dict[str, Any]) -> list[dict]:
    """Evaluate every ``alerts[].when`` against ``context`` via safe_eval."""
    from ..core.safe_eval import safe_eval_bool  # noqa: PLC0415 - keeps import cost local

    fired: list[dict] = []
    for entry in rules.get("alerts") or []:
        if not isinstance(entry, dict):
            continue
        alert_id = entry.get("id")
        expression = entry.get("when")
        if not alert_id or not expression:
            continue
        if safe_eval_bool(str(expression), context, default=False):
            fired.append(
                {
                    "id": str(alert_id),
                    "severity": str(entry.get("severity") or "medium"),
                    "when": str(expression),
                }
            )
    return fired


_missed_work_warned = False


def _missed_work_module():
    """Import agents.missed_work lazily, tolerating its absence.

    Imported here rather than at module scope because missed_work imports *this*
    module for its time, rate and persistence helpers. The dependency runs one
    way at import time and is resolved lazily in the other, which keeps both
    modules independently importable.

    A missing or broken missed_work degrades the daily and weekly reports to
    their acceptance-rate content instead of failing the run. That is the right
    trade: a report without the inventory is worth less than a full one and far
    more than no report at all, and the warning says which happened.
    """
    global _missed_work_warned
    try:
        from . import missed_work  # noqa: PLC0415 - deliberate lazy import

        return missed_work
    except Exception as exc:
        if not _missed_work_warned:
            logger.warning(
                "agents.missed_work unavailable (%s); reports will omit the "
                "missed-work inventory",
                exc,
            )
            _missed_work_warned = True
        return None


def _classify(service_type_raw: str | None, rules: dict[str, Any]) -> str:
    module = _classifier_module()
    if module is not None:
        try:
            return str(module.classify_service(service_type_raw or "", rules))
        except Exception as exc:  # pragma: no cover - classifier level failure
            logger.warning("classifier.classify_service failed (%s); using fallback", exc)
    return _fallback_classify(service_type_raw, rules)


def _evaluate_alerts(context: dict[str, Any], rules: dict[str, Any]) -> list[dict]:
    module = _classifier_module()
    if module is not None:
        try:
            result = module.evaluate_alerts(context, rules)
            return list(result) if result else []
        except Exception as exc:  # pragma: no cover - classifier level failure
            logger.warning("classifier.evaluate_alerts failed (%s); using fallback", exc)
    return _fallback_evaluate_alerts(context, rules)


# ==========================================================================
# Denial reasons
# ==========================================================================


def _normalize_denial_reason(raw: str | None, rules: dict[str, Any]) -> str | None:
    """Bucket a free-text denial reason using rules.yaml, or keep it verbatim.

    Same semantics as service classes: case-insensitive substring, list order,
    first match wins. rules.yaml ships this list empty on purpose, so until
    real reasons have been seen the raw text *is* the bucket.
    """
    text = (raw or "").strip()
    if not text:
        return None
    haystack = text.casefold()
    for entry in rules.get("denial_reason_normalization") or []:
        if not isinstance(entry, dict):
            continue
        normalized = entry.get("normalized")
        if not normalized:
            continue
        for needle in entry.get("match_any") or []:
            if needle and str(needle).casefold() in haystack:
                return str(normalized)
    return text


# ==========================================================================
# Row loading and views
# ==========================================================================


def _load_rows(
    session: Session,
    start_utc: datetime,
    end_utc: datetime,
    company_id: str | None = None,
) -> list[Request]:
    """Requests whose ``offered_at`` falls in ``[start_utc, end_utc)``.

    ALWAYS filtered by company. ``company_id=None`` means "the default
    company", never "every company" -- this function is the single gate every
    computation in this module and in agents/missed_work.py loads its rows
    through, so an unfiltered branch here would put one tenant's offers into
    another tenant's report with nothing on screen to show it happened.

    Ordered deterministically so that every list in the JSON output is stable
    across recomputation -- which is what makes the stored blob comparable.
    """
    stmt = (
        select(Request)
        .where(Request.company_id == _companies.resolve_company_id(company_id))
        .where(Request.offered_at.is_not(None))
        .where(Request.offered_at >= start_utc)
        .where(Request.offered_at < end_utc)
        .order_by(Request.offered_at, Request.request_id)
    )
    return list(session.scalars(stmt))


def _row_view(request: Request, rules: dict[str, Any], default_class: str) -> dict[str, Any]:
    """Flatten one ORM row into a plain, JSON-safe dict in local time."""
    offered_utc = request.offered_at
    offered_local = to_local(offered_utc) if offered_utc else None

    status = (request.status or "").strip().casefold() or "pending"

    service_class = (request.service_class or "").strip()
    if not service_class:
        # Not classified yet (ingested before backfill). Derive it for this
        # report without ever touching the stored verbatim string.
        service_class = _classify(request.service_type_raw, rules) or default_class

    name = (request.client_name or "").strip()
    key = (request.client_key or "").strip() or client_key_for(request.client_name)

    amount = float(request.amount) if request.amount is not None else None
    denial_raw = (request.denial_reason or "").strip() or None

    return {
        "request_id": request.request_id,
        "company_id": request.company_id,
        #: Deprecated alias, same value. Kept so that alert expressions and
        #: fixtures written against the single-company build still resolve.
        "account_id": request.company_id,
        "client": name or key or "(unknown)",
        "client_name": name,
        "client_key": key,
        "status": status,
        # The verbatim portal status and its numeric code, carried through
        # untouched. agents/missed_work.py buckets on these because the
        # five-value `status` above cannot tell "Expired" (nobody answered) from
        # "Accept Failed" (we answered and it failed), nor "Rejected" (we said
        # no) from "Rejected By Motor Club" (the client withdrew). Both are NULL
        # on rows ingested before those columns existed.
        "status_raw": (request.status_raw or "").strip(),
        "status_code": request.status_code,
        "is_accepted": status == "accepted",
        "service_class": service_class,
        "service_type_raw": request.service_type_raw or "",
        "denial_reason": denial_raw,
        "denial_reason_normalized": _normalize_denial_reason(denial_raw, rules),
        "pickup_location": request.pickup_location or "",
        "dropoff_location": request.dropoff_location or "",
        "truck_assigned": request.truck_assigned or "",
        "driver_assigned": request.driver_assigned or "",
        "amount": amount,
        "offered_at_utc": offered_utc.isoformat(sep="T") if offered_utc else None,
        "offered_at_local": offered_local.isoformat(sep="T") if offered_local else None,
        "responded_at_utc": request.responded_at.isoformat(sep="T") if request.responded_at else None,
        "local_date": offered_local.date() if offered_local else None,
        "local_hour": offered_local.hour if offered_local else 0,
        "local_weekday": offered_local.weekday() if offered_local else 0,
    }


def _views(
    rows: Iterable[Request], rules: dict[str, Any], default_class: str
) -> list[dict[str, Any]]:
    return [_row_view(row, rules, default_class) for row in rows]


def _reason_for(view: dict[str, Any]) -> str:
    """Human-readable reason a request was not accepted."""
    reason = view.get("denial_reason_normalized") or view.get("denial_reason")
    if reason:
        return str(reason)
    status = view.get("status")
    return {
        "denied": "denied, no reason given",
        "expired": "expired without response",
        "canceled": "canceled",
        "pending": "still pending",
        "accepted": "accepted",
    }.get(str(status), str(status or "unknown"))


# ==========================================================================
# Aggregation primitives
# ==========================================================================


def _status_totals(views: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(view["status"] for view in views)
    offered = len(views)
    accepted = counts.get("accepted", 0)
    known = set(STATUS_VALUES)
    return {
        "offered": offered,
        "accepted": accepted,
        "denied": counts.get("denied", 0),
        "expired": counts.get("expired", 0),
        "canceled": counts.get("canceled", 0),
        "pending": counts.get("pending", 0),
        "other": sum(count for status, count in counts.items() if status not in known),
        "acceptance_rate": rate_or_none(accepted, offered),
    }


def _display_name(group: Sequence[dict[str, Any]], key: str) -> str:
    """Pick the most frequent non-empty client_name in a group."""
    names = Counter(view["client_name"] for view in group if view["client_name"])
    if names:
        # most_common ties are resolved alphabetically so the pick is stable.
        best = max(names.items(), key=lambda item: (item[1], item[0]))
        return best[0]
    return key or "(unknown)"


def _denial_reason_breakdown(
    group: Sequence[dict[str, Any]], max_examples: int
) -> list[dict[str, Any]]:
    """Grouped denial reasons, most frequent first, with verbatim examples."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for view in group:
        if view["is_accepted"]:
            continue
        normalized = view.get("denial_reason_normalized")
        if not normalized:
            continue
        buckets[str(normalized)].append(str(view.get("denial_reason") or ""))

    out: list[dict[str, Any]] = []
    for reason, raws in buckets.items():
        examples = sorted({raw for raw in raws if raw and raw != reason})
        out.append(
            {
                "reason": reason,
                "count": len(raws),
                "examples": examples[: max(0, int(max_examples))],
            }
        )
    out.sort(key=lambda item: (-item["count"], item["reason"]))
    return out


def _by_client(
    views: Sequence[dict[str, Any]], max_examples: int
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for view in views:
        groups[view["client_key"]].append(view)

    out: list[dict[str, Any]] = []
    for key, group in groups.items():
        totals = _status_totals(group)
        out.append(
            {
                "client": _display_name(group, key),
                "client_key": key,
                "offered": totals["offered"],
                "accepted": totals["accepted"],
                "denied": totals["denied"],
                "expired": totals["expired"],
                "canceled": totals["canceled"],
                "pending": totals["pending"],
                "rate": totals["acceptance_rate"],
                "denial_reasons": _denial_reason_breakdown(group, max_examples),
            }
        )
    out.sort(key=lambda item: (-item["offered"], item["client_key"]))
    return out


def _by_service_class(
    views: Sequence[dict[str, Any]], rules: dict[str, Any]
) -> list[dict[str, Any]]:
    """One entry per configured class, in rules.yaml order, plus any strays.

    Configured classes appear even at zero volume so the dashboard and the
    email template have a stable set of rows to render.
    """
    ordered, default_class = _service_class_order(rules)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for view in views:
        groups[view["service_class"]].append(view)

    names = list(ordered)
    for extra in sorted(set(groups) - set(names)):
        names.append(extra)

    out: list[dict[str, Any]] = []
    for name in names:
        group = groups.get(name, [])
        totals = _status_totals(group)
        entry: dict[str, Any] = {
            "service_class": name,
            "offered": totals["offered"],
            "accepted": totals["accepted"],
            "denied": totals["denied"],
            "expired": totals["expired"],
            "canceled": totals["canceled"],
            "pending": totals["pending"],
            "rate": totals["acceptance_rate"],
        }
        if name == default_class:
            # The unclassified bucket carries the evidence needed to fix it:
            # the exact strings that matched nothing in rules.yaml.
            raw_counts = Counter(
                view["service_type_raw"] for view in group if view["service_type_raw"]
            )
            entry["raw_service_types"] = sorted(raw_counts)
            entry["raw_service_type_counts"] = [
                {"service_type_raw": raw, "count": count}
                for raw, count in sorted(raw_counts.items(), key=lambda i: (-i[1], i[0]))
            ]
        out.append(entry)
    return out


def _hourly_distribution(views: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    offered = [0] * 24
    accepted = [0] * 24
    for view in views:
        hour = int(view["local_hour"]) % 24
        offered[hour] += 1
        if view["is_accepted"]:
            accepted[hour] += 1
    return [
        {
            "hour": hour,
            "label": f"{hour:02d}:00",
            "offered": offered[hour],
            "accepted": accepted[hour],
            "rate": rate_or_none(accepted[hour], offered[hour]),
        }
        for hour in range(24)
    ]


def _policy_variance(
    views: Sequence[dict[str, Any]], rules: dict[str, Any], max_rows: int
) -> dict[str, Any]:
    """Where reality diverged from acceptance_policy in rules.yaml."""
    should_accept, should_decline = _policy_classes(rules)
    accept_set = set(should_accept)
    decline_set = set(should_decline)

    def _detail(view: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": view["request_id"],
            "client": view["client"],
            "client_key": view["client_key"],
            "service_class": view["service_class"],
            "service_type_raw": view["service_type_raw"],
            "status": view["status"],
            "reason": _reason_for(view),
            "denial_reason": view["denial_reason"],
            "offered_at_local": view["offered_at_local"],
            "offered_at_utc": view["offered_at_utc"],
            "amount": view["amount"],
            "pickup_location": view["pickup_location"],
            "dropoff_location": view["dropoff_location"],
        }

    missed_should_accept = [
        view for view in views if view["service_class"] in accept_set and not view["is_accepted"]
    ]
    missed_tows = [view for view in missed_should_accept if view["service_class"] == "tow"]
    accepted_light = [
        view for view in views if view["service_class"] in decline_set and view["is_accepted"]
    ]

    limit = max(0, int(max_rows))
    missed_by_client = Counter(view["client_key"] for view in missed_should_accept)
    top_missed: dict[str, Any] | None = None
    if missed_by_client:
        key, count = max(missed_by_client.items(), key=lambda item: (item[1], item[0]))
        top_missed = {
            "client": _display_name(
                [view for view in missed_should_accept if view["client_key"] == key], key
            ),
            "client_key": key,
            "count": count,
        }

    return {
        "policy": {"should_accept": should_accept, "should_decline": should_decline},
        "missed_tows": [_detail(view) for view in missed_tows[:limit]],
        "missed_tows_count": len(missed_tows),
        "missed_tows_truncated": len(missed_tows) > limit,
        "missed_should_accept": [_detail(view) for view in missed_should_accept[:limit]],
        "missed_should_accept_count": len(missed_should_accept),
        "missed_should_accept_truncated": len(missed_should_accept) > limit,
        "missed_by_class": dict(
            sorted(Counter(view["service_class"] for view in missed_should_accept).items())
        ),
        "accepted_light": [_detail(view) for view in accepted_light[:limit]],
        "accepted_light_count": len(accepted_light),
        "accepted_light_truncated": len(accepted_light) > limit,
        "accepted_against_policy_by_class": dict(
            sorted(Counter(view["service_class"] for view in accepted_light).items())
        ),
        "missed_value": _round_half_up(
            sum(view["amount"] or 0.0 for view in missed_should_accept), 2
        ),
        "top_missed_client": top_missed,
    }


# ==========================================================================
# Alerts
# ==========================================================================


def _row_context(
    view: dict[str, Any],
    report_type: str,
    version: str,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-row evaluation context for the row-scoped alerts.

    Carries ``bucket``, ``cause`` and ``remedy_class`` alongside the canonical
    status, because ``tow_unanswered`` has to be able to say "nobody answered"
    rather than "this ended up as expired". Those two are the same canonical
    status and completely different failures: one is a staffing problem, the
    other is a decision. The keys are simply absent when agents/missed_work.py
    is unavailable, and a rule that references an absent name does not fire.
    """
    context: dict[str, Any] = {
        "request_id": view["request_id"],
        "company_id": view.get("company_id") or view.get("account_id"),
        "account_id": view.get("account_id") or view.get("company_id"),
        "client": view["client"],
        "client_key": view["client_key"],
        "status": view["status"],
        "is_accepted": view["is_accepted"],
        "service_class": view["service_class"],
        "service_type_raw": view["service_type_raw"],
        "denial_reason": view["denial_reason"] or "",
        "denial_reason_normalized": view["denial_reason_normalized"] or "",
        "amount": view["amount"] if view["amount"] is not None else 0.0,
        "offered_at": view["offered_at_local"] or "",
        "offered_hour": view["local_hour"],
        "offered_weekday": view["local_weekday"],
        "truck_assigned": view["truck_assigned"],
        "driver_assigned": view["driver_assigned"],
        "report_type": report_type,
        "rules_version": version,
    }

    # The view already carries these when it came through missed_work; derive
    # them here for the plain metrics path so both produce the same context.
    for key in ("bucket", "cause", "remedy_class", "coverage_label"):
        if view.get(key):
            context[key] = view[key]
    if "bucket" not in context or "coverage_label" not in context:
        module = _missed_work_module()
        if module is not None:
            try:
                if "bucket" not in context:
                    context["bucket"] = module.classify_bucket(view, rules)
                    cause, remedy = module.attribute_cause(view, rules)
                    context["cause"] = cause
                    context["remedy_class"] = remedy
                # `coverage_gap` fires on this. Whether an offer arrived inside
                # the staffed window is the dimension the outcome actually
                # splits on -- 5.8% of offers expire inside it and 61.7%
                # outside -- so a row-scoped alert has to be able to see it.
                if "coverage_label" not in context:
                    context["coverage_label"] = module.coverage_label(view, rules)
            except Exception as exc:  # pragma: no cover - missed_work level failure
                logger.warning("missed_work row attribution failed (%s); skipping", exc)

    return context


def _client_contexts(
    session: Session,
    end_utc: datetime,
    rules: dict[str, Any],
    default_class: str,
    report_type: str,
    version: str,
    company_id: str | None,
    window_hours: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Per-client trailing-24h contexts for client_acceptance_drop.

    Deliberately a real trailing window ending at the report window end, not
    "the calendar day": the rule is named ``*_24h`` and must mean 24 hours even
    on a 23 or 25 hour DST day. Scoped to one company, like every other read.
    """
    hours = max(1, int(window_hours))
    start_utc = end_utc - timedelta(hours=hours)
    views = _views(_load_rows(session, start_utc, end_utc, company_id), rules, default_class)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for view in views:
        groups[view["client_key"]].append(view)

    start_local = to_local(start_utc).isoformat(sep="T")
    end_local = to_local(end_utc).isoformat(sep="T")

    out: list[tuple[str, dict[str, Any]]] = []
    for key in sorted(groups):
        group = groups[key]
        totals = _status_totals(group)
        out.append(
            (
                key,
                {
                    "client": _display_name(group, key),
                    "client_key": key,
                    "client_offers_24h": totals["offered"],
                    "client_accepted_24h": totals["accepted"],
                    "client_denied_24h": totals["denied"],
                    "client_expired_24h": totals["expired"],
                    "client_canceled_24h": totals["canceled"],
                    "client_acceptance_rate_24h": totals["acceptance_rate"],
                    "window_hours": hours,
                    "window_start": start_local,
                    "window_end": end_local,
                    "report_type": report_type,
                    "rules_version": version,
                },
            )
        )
    return out


def _alert_detail(context: dict[str, Any], kind: str) -> str:
    """One-line human summary, used verbatim by the ``alert_sms`` template."""
    if kind == "hour":
        return (
            f"{context.get('hour_label', '?')}: "
            f"{context.get('hour_no_response', 0)} of {context.get('hour_offers', 0)} "
            f"offers unanswered ({format_rate(context.get('hour_no_response_rate'))})"
        )
    if kind == "cause":
        return (
            f"{context.get('cause', '?')}: {context.get('cause_count_24h', 0)} in the "
            f"last {context.get('cause_window_hours', 24)}h vs a "
            f"{context.get('cause_baseline_7d', 0)}/day baseline"
        )
    if kind == "service_type":
        return (
            f"{context.get('service_type_raw', '?')}: "
            f"{context.get('service_type_accepted_7d', 0)} of "
            f"{context.get('service_type_offers_7d', 0)} accepted "
            f"({format_rate(context.get('service_type_rate_7d'))}) over "
            f"{context.get('closeoff_window_days', 7)}d"
        )
    if kind == "client":
        offered = context.get("client_offers_24h", 0)
        accepted = context.get("client_accepted_24h", 0)
        rate = context.get("client_acceptance_rate_24h")
        return (
            f"{context.get('client', '?')}: {accepted}/{offered} accepted "
            f"({format_rate(rate)}) in last {context.get('window_hours', 24)}h"
        )
    parts = [
        str(context.get("client") or "unknown client"),
        str(context.get("service_type_raw") or "?"),
        str(context.get("status") or "?"),
    ]
    reason = context.get("denial_reason_normalized") or context.get("denial_reason")
    if reason:
        parts.append(str(reason))
    return " | ".join(parts)


def _already_fired(
    session: Session,
    alert_id: str,
    entity: str,
    kind: str,
    row_dedupe: str,
    window: timedelta,
    company_id: str | None = None,
) -> bool:
    """Has this alert already been handed to the notifier for this entity?

    Scoped to one company. Two tenants both work for Agero, so
    ``("client_acceptance_drop", "agero (swoop)")`` is a different fact for
    each of them and one company's alert must never silence the other's.

    ``alerts_fired`` is written by agents/notifier.py -- it records every alert
    it handled, delivered or suppressed, and its own ``same_alert_same_entity``
    rate limit reads the table back. Metrics only *reads* it, to decide whether
    a recompute has anything new to say. Two writers on one table would corrupt
    the notifier's rate limit.

    Row-scoped alerts describe an immutable historical fact about one request,
    so by default they fire once per request, ever -- which is what makes a
    recompute of yesterday silent rather than a second round of texts.
    Client-scoped alerts recur, so they are deduped on the notifier's own
    rate-limit window instead.

    Unlike the notifier this deliberately counts *suppressed* rows too: quiet
    hours already decided that alert's fate, and recomputing the window is not
    a new reason to reconsider it.
    """
    stmt = select(AlertFired.id).where(
        AlertFired.company_id == _companies.resolve_company_id(company_id),
        AlertFired.alert_id == alert_id,
        AlertFired.entity == entity,
    )
    if kind != "row" or str(row_dedupe).strip().casefold() != "forever":
        stmt = stmt.where(AlertFired.fired_at >= utcnow() - window)
    try:
        return session.scalars(stmt.limit(1)).first() is not None
    except Exception as exc:  # pragma: no cover - DB level failure
        # Fail open, same as the notifier: a dedupe check that cannot run must
        # not become a reason to drop an alert.
        logger.warning("alert dedupe check failed (%s); alerting anyway", exc)
        return False


def _fire_alerts(
    session: Session,
    contexts: Sequence[tuple[str, dict[str, Any]]],
    kind: str,
    rules: dict[str, Any],
    version: str,
    dedupe: bool = True,
    company_id: str | None = None,
) -> list[dict[str, Any]]:
    """Evaluate the rules over each context and return what should be emitted.

    Nothing is written here. The notifier records the outcome in
    ``alerts_fired`` when it handles the event.
    """
    alert_cfg = _metrics_cfg(rules, "alerts")
    if not alert_cfg.get("enabled", True):
        return []

    window = _parse_duration(alert_cfg.get("dedupe_window") or None, _rate_limit_window())
    row_dedupe = str(alert_cfg.get("row_dedupe") or "forever")
    company = _companies.resolve_company_id(company_id)

    fired: list[dict[str, Any]] = []
    for entity, context in contexts:
        for raw in _evaluate_alerts(context, rules):
            if not isinstance(raw, dict):
                continue
            alert_id = str(raw.get("id") or raw.get("alert_id") or "").strip()
            if not alert_id:
                continue
            severity = str(raw.get("severity") or "medium")
            # OUR entity wins. classifier.evaluate_alerts guesses one from the
            # context and prefers client_key, which would collapse every missed
            # tow for a client onto a single entity and silence all but the
            # first. Metrics built the context, so metrics knows its scope.
            entity_key = str(entity or raw.get("entity") or "")

            if dedupe and _already_fired(
                session, alert_id, entity_key, kind, row_dedupe, window, company
            ):
                logger.debug("alert %s for %r already fired; not re-emitting", alert_id, entity_key)
                continue

            fired.append(
                {
                    "alert_id": alert_id,
                    "entity": entity_key,
                    "entity_kind": kind,
                    "severity": severity,
                    "when": raw.get("when") or raw.get("expression") or "",
                    "detail": _alert_detail(context, kind),
                    "rules_version": version,
                    # Rides on the event payload so agents/notifier.py stamps
                    # the alerts_fired row with the company it belongs to, and
                    # so a message body can name which company it is about.
                    "company_id": company,
                    "context": _jsonable(context),
                }
            )
    return fired


def _missed_work_pass(
    session: Session,
    start_local: datetime,
    end_local: datetime,
    period_type: str,
    rules: dict[str, Any],
    version: str,
    company_id: str | None,
    persist: bool,
    emit_alerts: bool,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Run the missed-work model for a report window.

    Returns ``(document, pending alerts)``. The document is embedded in the
    daily / weekly blob so the report can *lead* with the inventory of missed
    work, and is also persisted in full to ``metrics_missed_work`` on its own
    window key by ``compute_missed_work``.

    ``computed_at`` is stripped from the embedded copy. metrics only strips
    volatile keys at the top level of its own document, and a nested timestamp
    would break the guarantee that recomputing an unchanged window writes a
    byte-identical blob.

    Every failure here is caught. The missed-work inventory is the most valuable
    part of the report and the newest code in the system; a bug in it must
    degrade the report, not delete the acceptance numbers that were working
    before it existed.
    """
    module = _missed_work_module()
    if module is None:
        return (None, [])

    try:
        document = module.compute_missed_work(
            session,
            start_local,
            end_local,
            rules,
            period_type=period_type,
            company_id=company_id,
            persist=persist,
        )
    except Exception as exc:
        logger.exception("missed_work computation failed (%s); report degraded", exc)
        return (None, [])

    pending: list[dict[str, Any]] = []
    if emit_alerts:
        try:
            triples = module.alert_contexts(
                session,
                end_local,
                rules,
                company_id=company_id,
                report_type=period_type,
            )
        except Exception as exc:
            logger.exception("missed_work alert contexts failed (%s)", exc)
            triples = []

        by_kind: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for kind, entity, context in triples:
            by_kind[kind].append((entity, context))
        for kind in sorted(by_kind):
            pending.extend(
                _fire_alerts(
                    session,
                    by_kind[kind],
                    kind,
                    rules,
                    version,
                    company_id=company_id,
                )
            )

    embedded = {
        key: value for key, value in document.items() if key not in _VOLATILE_KEYS
    }
    return (embedded, pending)


def _emit(pending: Sequence[dict[str, Any]]) -> None:
    """Hand fired alerts to the notifier through the event bus.

    ``events.emit_event`` never raises and always writes to state/events.jsonl
    first, so a dead notifier cannot lose an alert or break a metrics run.
    """
    for payload in pending:
        context = {
            key: value
            for key, value in payload.items()
            if key not in {"alert_id", "severity", "entity"}
        }
        events.emit_alert(
            payload["alert_id"],
            payload.get("severity", "medium"),
            payload.get("entity", ""),
            **context,
        )


# ==========================================================================
# Persistence helpers
# ==========================================================================


def _jsonable(value: Any) -> Any:
    """Recursively coerce to types the JSON column can store."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep="T")
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [_jsonable(item) for item in items]
    return str(value)


def _persistable(document: dict[str, Any]) -> dict[str, Any]:
    """The document minus the keys that legitimately differ between runs."""
    return _jsonable({k: v for k, v in document.items() if k not in _VOLATILE_KEYS})


def _upsert(session: Session, model: Any, filters: dict[str, Any], values: dict[str, Any]) -> Any:
    """Find-or-create on ``filters``, then apply ``values``.

    ``filters`` must carry ``company_id`` for every table that has one: it is
    part of each metrics unique key, and an upsert that omitted it would find
    the OTHER company's row for the same window and overwrite it. Asserted
    rather than silently defaulted, because the failure is invisible in the
    result -- the number returned is correct, and the row it replaced is gone.
    """
    if "company_id" in model.__table__.c and "company_id" not in filters:
        raise ValueError(
            f"_upsert({model.__name__}) needs company_id in its filters; "
            "without it one company's row overwrites another's"
        )
    stmt = select(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    obj = session.scalars(stmt.limit(1)).first()
    if obj is None:
        obj = model(**filters)
        session.add(obj)
    for column, value in values.items():
        setattr(obj, column, value)
    session.flush()
    return obj


def _persist_client_daily(
    session: Session,
    day: _date,
    by_client: Sequence[dict[str, Any]],
    version: str,
    company_id: str | None = None,
) -> int:
    """Upsert one row per client and drop rows no longer supported by the data.

    Scoped to one company on BOTH halves. The read that finds existing rows is
    filtered, so this company cannot adopt another's Agero row; and the sweep
    that deletes unsupported rows is filtered, so it cannot delete one either.
    The delete is the dangerous half: unscoped, a quiet Tuesday for one tenant
    would wipe every other tenant's Tuesday.
    """
    company = _companies.resolve_company_id(company_id)
    existing = {
        row.client_key: row
        for row in session.scalars(
            select(ClientDaily)
            .where(ClientDaily.company_id == company)
            .where(ClientDaily.date == day)
        )
    }
    seen: set[str] = set()
    stamp = utcnow()

    for entry in by_client:
        key = entry["client_key"]
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = ClientDaily(company_id=company, date=day, client_key=key)
            session.add(row)
        row.client_name = entry["client"]
        row.offered = entry["offered"]
        row.accepted = entry["accepted"]
        row.denied = entry["denied"]
        row.expired = entry["expired"]
        row.canceled = entry["canceled"]
        # NOT NULL column: 0.0 stands in for "no rate". offered == 0 is the flag.
        row.rate = entry["rate"] if entry["rate"] is not None else 0.0
        row.rules_version = version
        row.computed_at = stamp

    for key, row in existing.items():
        if key not in seen:
            session.delete(row)

    session.flush()
    return len(seen)


# ==========================================================================
# Session plumbing
# ==========================================================================


def _company_scope(company_id: str | None, account_id: str | None = None):
    """Activate the company a ``compute_*`` call is about.

    Takes the pair of argument names every public entry point accepts:
    ``company_id`` is the current one, ``account_id`` the name the
    single-company build used. Whichever was given wins; ``company_id`` first
    if somehow both were.

    Activating rather than only resolving matters because the LOCAL TIMEZONE
    is read all over this module -- ``_local_midnight_utc``, ``to_local``,
    ``parse_local_date`` -- and cannot be threaded through every one of them.
    The scope is entered before the window is parsed, so "Tuesday" is that
    company's Tuesday from the first line.
    """
    return _companies.use_company(company_id if company_id is not None else account_id)


def _run(
    session: Session | None,
    persist: bool,
    work: Callable[[Session], tuple[dict[str, Any], list[dict[str, Any]]]],
) -> dict[str, Any]:
    """Execute ``work``, commit if we own the session, then emit its alerts.

    Alerts are emitted only after the data they describe is durable. A notifier
    that texts about a row that was rolled back is worse than a late text.
    """
    if session is not None:
        result, pending = work(session)
        session.flush()
        _emit(pending)
        return result

    with get_session(commit=persist) as owned:
        result, pending = work(owned)

    _emit(pending)
    return result


# ==========================================================================
# compute_hourly
# ==========================================================================


def compute_hourly(
    window_start: datetime | _date | str,
    *,
    session: Session | None = None,
    company_id: str | None = None,
    account_id: str | None = None,
    persist: bool = True,
    emit_alerts: bool | None = None,
) -> dict[str, Any]:
    """Metrics for the clock hour ``[window_start, window_start + 1h)``.

    ``window_start`` is local time (naive is assumed local, aware is converted).
    Persists to ``metrics_hourly``, unique on ``(company_id, window_start)``,
    and returns the document. The running-day figures cover local midnight up
    to the window end, because that is what the hourly SMS reports on the
    second line.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company_scope(company_id, account_id) as company:
        return _compute_hourly(
            window_start,
            session=session,
            company_id=company,
            persist=persist,
            emit_alerts=emit_alerts,
        )


def _compute_hourly(
    window_start: datetime | _date | str,
    *,
    session: Session | None,
    company_id: str,
    persist: bool,
    emit_alerts: bool | None,
) -> dict[str, Any]:
    if isinstance(window_start, datetime) and window_start.tzinfo is not None:
        # An aware input names an exact instant, so use it directly. On the
        # fall-back night local 01:00 happens twice; a naive "01:00" can only
        # ever mean the first pass, but the scheduler hands us an aware value
        # that still knows which one it is.
        aware = window_start.astimezone(local_timezone()).replace(
            minute=0, second=0, microsecond=0
        )
        start_local = aware.replace(tzinfo=None)
        start_utc = aware.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        start_local = parse_local_datetime(window_start).replace(
            minute=0, second=0, microsecond=0
        )
        start_utc = to_utc(start_local)
    end_utc = start_utc + timedelta(hours=1)
    end_local = to_local(end_utc)

    day = start_local.date()
    day_start_utc = _local_midnight_utc(day)

    emit_alerts = persist if emit_alerts is None else emit_alerts
    rules = _rules(company_id)
    version = rules_version()
    _, default_class = _service_class_order(rules)
    detail_cfg = _metrics_cfg(rules, "detail")
    alert_cfg = _metrics_cfg(rules, "alerts")
    time_format = str(_notifications_formatting().get("time_format") or "%H:%M")
    zero_pct = _notifications_formatting().get("zero_offers_rate_pct", 0)

    def work(sess: Session) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        # One query for the whole day so far; the window is a slice of it.
        day_rows = _load_rows(sess, day_start_utc, end_utc, company_id)
        day_views = _views(day_rows, rules, default_class)
        window_views = [
            view
            for view, row in zip(day_views, day_rows)
            if row.offered_at is not None and row.offered_at >= start_utc
        ]

        window_totals = _status_totals(window_views)
        day_totals = _status_totals(day_views)

        # THE HEADLINE, on the hourly path too. The hourly SMS keeps its exact
        # two-line shape and appends ONE line only when there is something to
        # act on -- "!! 3 tows unanswered this hour" -- and it cannot know that
        # without the bucket split (MISSED_WORK_MODEL.md section 7).
        #
        # emit_alerts is hard-wired FALSE here, and that is deliberate rather
        # than an oversight. The three aggregate missed-work alerts each run
        # over their own trailing multi-day window; firing them from an hourly
        # job would re-evaluate a week of rows twenty-four times a day to
        # re-raise the same finding the daily run already raised. The row-scoped
        # `tow_unanswered` is unaffected: it rides on _row_context below, which
        # already carries `bucket`, and it still fires within the hour.
        missed, _ = _missed_work_pass(
            sess,
            start_local,
            end_local,
            "hourly",
            rules,
            version,
            company_id,
            persist,
            False,
        )

        document: dict[str, Any] = {
            "report_type": "hourly",
            "company_id": company_id,
            "timezone": local_timezone_name(),
            "rules_version": version,
            "computed_at": utcnow().isoformat(sep="T"),
            "date": day.isoformat(),
            "window_start": start_local.isoformat(sep="T"),
            "window_end": end_local.isoformat(sep="T"),
            "window_start_utc": start_utc.isoformat(sep="T"),
            "window_end_utc": end_utc.isoformat(sep="T"),
            "hour_start": start_local.strftime(time_format),
            "hour_end": (end_local - timedelta(minutes=1)).strftime(time_format),
            # -- the six figures named in the spec ---------------------------
            "offered_window": window_totals["offered"],
            "accepted_window": window_totals["accepted"],
            "acceptance_rate_window": window_totals["acceptance_rate"],
            "offered_day_running": day_totals["offered"],
            "accepted_day_running": day_totals["accepted"],
            "acceptance_rate_day": day_totals["acceptance_rate"],
            # -- aliases the hourly_short SMS template interpolates -----------
            "offered": window_totals["offered"],
            "accepted": window_totals["accepted"],
            "rate": window_totals["acceptance_rate"],
            "rate_pct": rate_pct(window_totals["acceptance_rate"], zero_offers=zero_pct),
            "rate_display": format_rate(window_totals["acceptance_rate"]),
            "day_offered": day_totals["offered"],
            "day_accepted": day_totals["accepted"],
            "day_rate": day_totals["acceptance_rate"],
            "day_rate_pct": rate_pct(day_totals["acceptance_rate"], zero_offers=zero_pct),
            "day_rate_display": format_rate(day_totals["acceptance_rate"]),
            # -- window detail -----------------------------------------------
            "totals": window_totals,
            "day_totals": day_totals,
            "by_client": _by_client(window_views, int(detail_cfg["max_denial_examples"])),
            "by_service_class": _by_service_class(window_views, rules),
            "policy_variance": _policy_variance(window_views, rules, int(detail_cfg["max_rows"])),
            "counts": {
                "requests": window_totals["offered"],
                "clients": len({view["client_key"] for view in window_views}),
            },
            # LAST for the same reason as in compute_daily: key order is not
            # report order, but it is the order a generic walk over the blob
            # searches, and this is a whole second document with its own
            # `service_class` and `client_key` fields at every level.
            "missed_work": missed,
        }

        if persist:
            _upsert(
                sess,
                MetricsHourly,
                {"company_id": company_id, "window_start": start_utc},
                {
                    "offered": window_totals["offered"],
                    "accepted": window_totals["accepted"],
                    # NOT NULL columns: 0.0 means "no rate" when offered == 0.
                    "rate": window_totals["acceptance_rate"] or 0.0,
                    "day_running_offered": day_totals["offered"],
                    "day_running_accepted": day_totals["accepted"],
                    "day_running_rate": day_totals["acceptance_rate"] or 0.0,
                    "rules_version": version,
                    "computed_at": utcnow(),
                },
            )

        pending: list[dict[str, Any]] = []
        if emit_alerts:
            row_contexts = [
                (view["request_id"], _row_context(view, "hourly", version, rules))
                for view in window_views
            ]
            pending.extend(
                _fire_alerts(
                    sess, row_contexts, "row", rules, version, company_id=company_id
                )
            )
            pending.extend(
                _fire_alerts(
                    sess,
                    _client_contexts(
                        sess,
                        end_utc,
                        rules,
                        default_class,
                        "hourly",
                        version,
                        company_id,
                        int(alert_cfg["client_window_hours"]),
                    ),
                    "client",
                    rules,
                    version,
                    company_id=company_id,
                )
            )
        document["alerts"] = pending

        logger.info(
            "hourly %s: offered %d accepted %d (%s), day %d/%d (%s), %d alert(s)",
            start_local.strftime("%Y-%m-%d %H:%M"),
            window_totals["offered"],
            window_totals["accepted"],
            format_rate(window_totals["acceptance_rate"]),
            day_totals["offered"],
            day_totals["accepted"],
            format_rate(day_totals["acceptance_rate"]),
            len(pending),
        )
        return document, pending

    return _run(session, persist, work)


# ==========================================================================
# compute_daily
# ==========================================================================


def compute_daily(
    day: datetime | _date | str,
    *,
    session: Session | None = None,
    company_id: str | None = None,
    account_id: str | None = None,
    persist: bool = True,
    emit_alerts: bool | None = None,
) -> dict[str, Any]:
    """Metrics for one local calendar day, for one company.

    Persists the JSON document to ``metrics_daily`` (unique on
    ``(company_id, date)``) and one row per client to ``client_daily`` (unique
    on ``company_id`` + ``date`` + ``client_key``). Clients that no longer
    appear for the day are deleted -- for this company only -- so a corrected
    re-ingest cannot leave a stale row behind or remove another tenant's.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company_scope(company_id, account_id) as company:
        return _compute_daily(
            day,
            session=session,
            company_id=company,
            persist=persist,
            emit_alerts=emit_alerts,
        )


def _compute_daily(
    day: datetime | _date | str,
    *,
    session: Session | None,
    company_id: str,
    persist: bool,
    emit_alerts: bool | None,
) -> dict[str, Any]:
    target = parse_local_date(day)
    start_utc = _local_midnight_utc(target)
    end_utc = _local_midnight_utc(target + timedelta(days=1))

    emit_alerts = persist if emit_alerts is None else emit_alerts
    rules = _rules(company_id)
    version = rules_version()
    _, default_class = _service_class_order(rules)
    detail_cfg = _metrics_cfg(rules, "detail")
    alert_cfg = _metrics_cfg(rules, "alerts")
    max_examples = int(detail_cfg["max_denial_examples"])
    max_rows = int(detail_cfg["max_rows"])
    zero_pct = _notifications_formatting().get("zero_offers_rate_pct", 0)

    def work(sess: Session) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        views = _views(_load_rows(sess, start_utc, end_utc, company_id), rules, default_class)
        totals = _status_totals(views)
        by_client = _by_client(views, max_examples)

        # THE HEADLINE. Computed before the document is assembled so the report
        # leads with the inventory of missed work and treats acceptance rate as
        # the supporting context it is (MISSED_WORK_MODEL.md section 7).
        missed, missed_alerts = _missed_work_pass(
            sess,
            datetime.combine(target, _time.min),
            datetime.combine(target + timedelta(days=1), _time.min),
            "daily",
            rules,
            version,
            company_id,
            persist,
            bool(emit_alerts),
        )

        document: dict[str, Any] = {
            "report_type": "daily",
            "company_id": company_id,
            "date": target.isoformat(),
            "weekday": _WEEKDAY_LABELS[target.weekday()],
            "timezone": local_timezone_name(),
            "window_start_utc": start_utc.isoformat(sep="T"),
            "window_end_utc": end_utc.isoformat(sep="T"),
            "window_hours": _round_half_up((end_utc - start_utc).total_seconds() / 3600.0, 2),
            "rules_version": version,
            "computed_at": utcnow().isoformat(sep="T"),
            "totals": totals,
            "rate_pct": rate_pct(totals["acceptance_rate"], zero_offers=zero_pct),
            "rate_display": format_rate(totals["acceptance_rate"]),
            "by_client": by_client,
            "by_service_class": _by_service_class(views, rules),
            "policy_variance": _policy_variance(views, rules, max_rows),
            "hourly_distribution": _hourly_distribution(views),
            "counts": {"requests": totals["offered"], "clients": len(by_client)},
            # THE HEADLINE, and deliberately the LAST key in the document.
            #
            # Key order is not report order -- the template decides what a
            # reader sees first, and it leads with this. What key order does
            # decide is what a generic "find the breakdown for X" walk over the
            # blob hits first, and this is a whole second document with its own
            # `service_class` and `client_key` fields at every level. Sitting
            # ahead of by_client / by_service_class it would shadow them, and a
            # consumer asking for "tow" would get one inventory row instead of
            # the tow totals. The full document, grids and all, also lives in
            # metrics_missed_work under its own window key.
            "missed_work": missed,
        }

        if persist:
            _upsert(
                sess,
                MetricsDaily,
                {"company_id": company_id, "date": target},
                {
                    "metrics": _persistable(document),
                    "rules_version": version,
                    "computed_at": utcnow(),
                },
            )
            _persist_client_daily(sess, target, by_client, version, company_id)

        pending: list[dict[str, Any]] = list(missed_alerts)
        if emit_alerts:
            row_contexts = [
                (view["request_id"], _row_context(view, "daily", version, rules)) for view in views
            ]
            pending.extend(
                _fire_alerts(
                    sess, row_contexts, "row", rules, version, company_id=company_id
                )
            )
            pending.extend(
                _fire_alerts(
                    sess,
                    _client_contexts(
                        sess,
                        end_utc,
                        rules,
                        default_class,
                        "daily",
                        version,
                        company_id,
                        int(alert_cfg["client_window_hours"]),
                    ),
                    "client",
                    rules,
                    version,
                    company_id=company_id,
                )
            )
        document["alerts"] = pending

        logger.info(
            "daily %s: offered %d accepted %d (%s), %d client(s), %d alert(s)",
            target.isoformat(),
            totals["offered"],
            totals["accepted"],
            format_rate(totals["acceptance_rate"]),
            len(by_client),
            len(pending),
        )
        return document, pending

    return _run(session, persist, work)


# ==========================================================================
# compute_weekly
# ==========================================================================


def _client_totals(views: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for view in views:
        groups[view["client_key"]].append(view)
    return {
        key: {**_status_totals(group), "client": _display_name(group, key)}
        for key, group in groups.items()
    }


def _client_trend(
    current: Sequence[dict[str, Any]],
    prior: Sequence[dict[str, Any]],
    flat_band_pp: float,
) -> list[dict[str, Any]]:
    """Week-over-week acceptance movement, one entry per client seen in either week."""
    now = _client_totals(current)
    was = _client_totals(prior)

    out: list[dict[str, Any]] = []
    for key in sorted(set(now) | set(was)):
        cur = now.get(key)
        old = was.get(key)
        offers_wk = cur["offered"] if cur else 0
        accepted_wk = cur["accepted"] if cur else 0
        offers_prior = old["offered"] if old else 0
        accepted_prior = old["accepted"] if old else 0
        rate_wk = rate_or_none(accepted_wk, offers_wk)
        rate_prior = rate_or_none(accepted_prior, offers_prior)
        delta_pp = _pp(rate_wk, rate_prior)

        if offers_prior == 0:
            direction = "new"
        elif offers_wk == 0:
            direction = "lost"
        elif delta_pp is None:
            direction = "flat"
        elif delta_pp > flat_band_pp:
            direction = "up"
        elif delta_pp < -flat_band_pp:
            direction = "down"
        else:
            direction = "flat"

        volume_delta_pct = (
            _round_half_up((offers_wk - offers_prior) / offers_prior * 100.0, 2)
            if offers_prior
            else None
        )

        out.append(
            {
                "client": (cur or old)["client"],
                "client_key": key,
                "offers_wk": offers_wk,
                "accepted_wk": accepted_wk,
                "rate_wk": rate_wk,
                "offers_prior_wk": offers_prior,
                "accepted_prior_wk": accepted_prior,
                "rate_prior_wk": rate_prior,
                "delta_pp": delta_pp,
                "direction": direction,
                "volume_delta": offers_wk - offers_prior,
                "volume_delta_pct": volume_delta_pct,
            }
        )
    out.sort(key=lambda item: (-item["offers_wk"], item["client_key"]))
    return out


def _outliers(trend: Sequence[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """Clients ranked by adverse movement, plus clients whose volume collapsed."""
    threshold_pct = float(cfg["volume_drop_threshold_pct"])
    min_prior = int(cfg["volume_drop_min_prior_offers"])
    min_offers = int(cfg["trend_min_offers"])
    limit = max(0, int(cfg["outlier_limit"]))

    drops = [
        entry
        for entry in trend
        if entry["delta_pp"] is not None
        and entry["delta_pp"] < 0
        and entry["offers_wk"] >= min_offers
    ]
    drops.sort(key=lambda item: (item["delta_pp"], -item["offers_wk"], item["client_key"]))

    volume: list[dict[str, Any]] = []
    for entry in trend:
        prior = entry["offers_prior_wk"]
        # The floor is the whole point: a client that went from 2 offers to 1
        # is a 50% "collapse" and complete noise.
        if prior < min_prior:
            continue
        drop_pct = _round_half_up((prior - entry["offers_wk"]) / prior * 100.0, 2)
        if drop_pct >= threshold_pct:
            volume.append(
                {
                    "client": entry["client"],
                    "client_key": entry["client_key"],
                    "offers_wk": entry["offers_wk"],
                    "offers_prior_wk": prior,
                    "drop_pct": drop_pct,
                    "rate_wk": entry["rate_wk"],
                    "rate_prior_wk": entry["rate_prior_wk"],
                }
            )
    volume.sort(key=lambda item: (-item["drop_pct"], item["client_key"]))

    return {
        "acceptance_drops": drops[:limit],
        "acceptance_drops_count": len(drops),
        "volume_drops": volume[:limit],
        "volume_drops_count": len(volume),
        "thresholds": {
            "volume_drop_threshold_pct": threshold_pct,
            "volume_drop_min_prior_offers": min_prior,
            "trend_min_offers": min_offers,
            "outlier_limit": limit,
        },
    }


def _capacity_signal(
    views: Sequence[dict[str, Any]], week_start: _date, min_offers: int
) -> dict[str, Any]:
    """7x24 hour-of-week heatmap of offered vs accepted, rows from week_start."""
    offered = [[0] * 24 for _ in range(7)]
    accepted = [[0] * 24 for _ in range(7)]

    for view in views:
        local_date = view["local_date"]
        if local_date is None:
            continue
        row = (local_date - week_start).days
        if not 0 <= row < 7:
            continue
        col = int(view["local_hour"]) % 24
        offered[row][col] += 1
        if view["is_accepted"]:
            accepted[row][col] += 1

    rates = [
        [rate_or_none(accepted[r][c], offered[r][c]) for c in range(24)] for r in range(7)
    ]

    peak: dict[str, Any] | None = None
    worst: dict[str, Any] | None = None
    for r in range(7):
        for c in range(24):
            if offered[r][c] == 0:
                continue
            cell = {
                "weekday": _WEEKDAY_LABELS[(week_start + timedelta(days=r)).weekday()],
                "weekday_index": r,
                "date": (week_start + timedelta(days=r)).isoformat(),
                "hour": c,
                "label": f"{c:02d}:00",
                "offered": offered[r][c],
                "accepted": accepted[r][c],
                "rate": rates[r][c],
            }
            if peak is None or cell["offered"] > peak["offered"]:
                peak = cell
            if cell["offered"] >= max(1, min_offers):
                if worst is None or (cell["rate"] or 0.0) < (worst["rate"] or 0.0):
                    worst = cell

    return {
        "rows": 7,
        "cols": 24,
        "week_start": week_start.isoformat(),
        "weekdays": [
            _WEEKDAY_LABELS[(week_start + timedelta(days=r)).weekday()] for r in range(7)
        ],
        "dates": [(week_start + timedelta(days=r)).isoformat() for r in range(7)],
        "hours": list(range(24)),
        "offered": offered,
        "accepted": accepted,
        "rate": rates,
        "peak": peak,
        "worst_rate": worst,
        "min_offers_for_worst": int(min_offers),
    }


def _daily_distribution(
    views: Sequence[dict[str, Any]], week_start: _date, days: int = 7
) -> list[dict[str, Any]]:
    """One row per day from ``week_start``. ``days`` defaults to a week.

    The monthly report passes the length of its own month, which is 28 to 31
    days and not something a caller should have to special-case per February.
    """
    buckets: dict[_date, list[dict[str, Any]]] = defaultdict(list)
    for view in views:
        if view["local_date"] is not None:
            buckets[view["local_date"]].append(view)

    out: list[dict[str, Any]] = []
    for offset in range(max(0, int(days))):
        day = week_start + timedelta(days=offset)
        totals = _status_totals(buckets.get(day, []))
        out.append(
            {
                "date": day.isoformat(),
                "weekday": _WEEKDAY_LABELS[day.weekday()],
                "offered": totals["offered"],
                "accepted": totals["accepted"],
                "denied": totals["denied"],
                "expired": totals["expired"],
                "canceled": totals["canceled"],
                "rate": totals["acceptance_rate"],
            }
        )
    return out


def _missed_work_trend(
    current: dict[str, Any] | None, prior: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Week-over-week movement in the missed-work model.

    Answers the three questions the weekly report exists to ask and the daily
    cannot: is each cause growing or shrinking, are the blind spots moving, and
    did a close-off actually take effect. Direction is stated in plain words
    (``up`` / ``down`` / ``flat`` / ``new`` / ``gone``) so a template does not
    have to decide what a negative number means.
    """
    if not isinstance(current, dict) or not isinstance(prior, dict):
        return None

    def _direction(now: float, was: float) -> str:
        if was == 0 and now == 0:
            return "flat"
        if was == 0:
            return "new"
        if now == 0:
            return "gone"
        if now > was:
            return "up"
        if now < was:
            return "down"
        return "flat"

    now_totals = current.get("totals") or {}
    was_totals = prior.get("totals") or {}

    causes: dict[str, Any] = {}
    now_causes = current.get("by_cause") or {}
    was_causes = prior.get("by_cause") or {}
    for cause in sorted(set(now_causes) | set(was_causes)):
        now_count = int((now_causes.get(cause) or {}).get("missed", 0))
        was_count = int((was_causes.get(cause) or {}).get("missed", 0))
        causes[cause] = {
            "cause": cause,
            "missed": now_count,
            "missed_prior": was_count,
            "delta": now_count - was_count,
            "direction": _direction(now_count, was_count),
        }

    now_spots = {
        (cell["weekday"], cell["hour"])
        for cell in (current.get("blind_spots") or {}).get("blind_spots", [])
    }
    was_spots = {
        (cell["weekday"], cell["hour"])
        for cell in (prior.get("blind_spots") or {}).get("blind_spots", [])
    }

    now_types = {
        entry["service_type_raw"]
        for entry in (current.get("closeoff_candidates") or {}).get("by_service_type", [])
    }
    was_types = {
        entry["service_type_raw"]
        for entry in (prior.get("closeoff_candidates") or {}).get("by_service_type", [])
    }

    def _pair(key: str) -> dict[str, Any]:
        now_value = int(now_totals.get(key, 0) or 0)
        was_value = int(was_totals.get(key, 0) or 0)
        return {
            key: now_value,
            f"{key}_prior": was_value,
            "delta": now_value - was_value,
            "direction": _direction(now_value, was_value),
        }

    return {
        "totals": {
            key: _pair(key)
            for key in ("offers", "accepted", "missed", "recoverable", "withdrew")
        },
        "acceptance_rate_pp": _pp(
            now_totals.get("acceptance_rate"), was_totals.get("acceptance_rate")
        ),
        "missed_rate_pp": _pp(
            now_totals.get("missed_rate"), was_totals.get("missed_rate")
        ),
        "no_response_rate_pp": _pp(
            now_totals.get("no_response_rate"), was_totals.get("no_response_rate")
        ),
        "by_cause": causes,
        "blind_spots": {
            "current": sorted(f"{day} {hour:02d}:00" for day, hour in now_spots),
            "opened": sorted(f"{day} {hour:02d}:00" for day, hour in now_spots - was_spots),
            "closed": sorted(f"{day} {hour:02d}:00" for day, hour in was_spots - now_spots),
            "persisting": sorted(
                f"{day} {hour:02d}:00" for day, hour in now_spots & was_spots
            ),
        },
        "closeoff_candidates": {
            # A service type that WAS a candidate and no longer appears is the
            # signal that a close-off conversation actually took effect.
            "current": sorted(now_types),
            "new": sorted(now_types - was_types),
            "resolved": sorted(was_types - now_types),
            "persisting": sorted(now_types & was_types),
        },
        "ranking_basis": current.get("ranking_basis", "job_count"),
        "revenue_available": bool(current.get("revenue_available")),
    }


def compute_weekly(
    week_start: datetime | _date | str,
    *,
    session: Session | None = None,
    company_id: str | None = None,
    account_id: str | None = None,
    persist: bool = True,
    emit_alerts: bool | None = None,
) -> dict[str, Any]:
    """Metrics for the 7 local days from ``week_start``, against the prior 7.

    ``week_start`` is normalised to the Monday of the week it falls in, so the
    scheduler and a human typing a Wednesday both land on the same row.
    Persists the JSON document to ``metrics_weekly`` (unique on
    ``(company_id, week_start)``).

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company_scope(company_id, account_id) as company:
        return _compute_weekly(
            week_start,
            session=session,
            company_id=company,
            persist=persist,
            emit_alerts=emit_alerts,
        )


def _compute_weekly(
    week_start: datetime | _date | str,
    *,
    session: Session | None,
    company_id: str,
    persist: bool,
    emit_alerts: bool | None,
) -> dict[str, Any]:
    start_day = week_start_for(week_start)
    end_day = start_day + timedelta(days=7)  # exclusive
    prior_start_day = start_day - timedelta(days=7)

    start_utc = _local_midnight_utc(start_day)
    end_utc = _local_midnight_utc(end_day)
    prior_start_utc = _local_midnight_utc(prior_start_day)

    emit_alerts = persist if emit_alerts is None else emit_alerts
    rules = _rules(company_id)
    version = rules_version()
    _, default_class = _service_class_order(rules)
    detail_cfg = _metrics_cfg(rules, "detail")
    weekly_cfg = _metrics_cfg(rules, "weekly")
    alert_cfg = _metrics_cfg(rules, "alerts")
    max_examples = int(detail_cfg["max_denial_examples"])
    max_rows = int(detail_cfg["max_rows"])
    zero_pct = _notifications_formatting().get("zero_offers_rate_pct", 0)

    def work(sess: Session) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        # One 14-day query, split locally: two round trips would be two
        # snapshots, and a request ingested between them would skew the delta.
        rows = _load_rows(sess, prior_start_utc, end_utc, company_id)
        all_views = _views(rows, rules, default_class)
        current = [
            view
            for view, row in zip(all_views, rows)
            if row.offered_at is not None and row.offered_at >= start_utc
        ]
        prior = [
            view
            for view, row in zip(all_views, rows)
            if row.offered_at is not None and row.offered_at < start_utc
        ]

        totals = _status_totals(current)
        prior_totals = _status_totals(prior)
        by_client = _by_client(current, max_examples)
        trend = _client_trend(current, prior, float(weekly_cfg["direction_flat_band_pp"]))

        # THE HEADLINE, plus the same model over the prior week so the weekly
        # report can answer the questions the daily cannot: is each cause
        # growing or shrinking, are the blind spots moving, did a close-off
        # actually take effect (MISSED_WORK_MODEL.md section 7).
        missed, missed_alerts = _missed_work_pass(
            sess,
            datetime.combine(start_day, _time.min),
            datetime.combine(end_day, _time.min),
            "weekly",
            rules,
            version,
            company_id,
            persist,
            bool(emit_alerts),
        )
        prior_missed, _ = _missed_work_pass(
            sess,
            datetime.combine(prior_start_day, _time.min),
            datetime.combine(start_day, _time.min),
            "weekly",
            rules,
            version,
            company_id,
            # The prior week is context for this week's trend, not a report in
            # its own right: it is neither persisted again nor re-alerted, so a
            # weekly run cannot re-text last week.
            False,
            False,
        )

        document: dict[str, Any] = {
            "report_type": "weekly",
            "company_id": company_id,
            "week_start": start_day.isoformat(),
            "week_end": (end_day - timedelta(days=1)).isoformat(),
            "prior_week_start": prior_start_day.isoformat(),
            "prior_week_end": (start_day - timedelta(days=1)).isoformat(),
            "timezone": local_timezone_name(),
            "window_start_utc": start_utc.isoformat(sep="T"),
            "window_end_utc": end_utc.isoformat(sep="T"),
            "prior_window_start_utc": prior_start_utc.isoformat(sep="T"),
            "prior_window_end_utc": start_utc.isoformat(sep="T"),
            "rules_version": version,
            "computed_at": utcnow().isoformat(sep="T"),
            "totals": totals,
            "prior_totals": prior_totals,
            "totals_delta": {
                "offered": totals["offered"] - prior_totals["offered"],
                "accepted": totals["accepted"] - prior_totals["accepted"],
                "denied": totals["denied"] - prior_totals["denied"],
                "expired": totals["expired"] - prior_totals["expired"],
                "canceled": totals["canceled"] - prior_totals["canceled"],
                "acceptance_rate_pp": _pp(
                    totals["acceptance_rate"], prior_totals["acceptance_rate"]
                ),
            },
            "rate_pct": rate_pct(totals["acceptance_rate"], zero_offers=zero_pct),
            "rate_display": format_rate(totals["acceptance_rate"]),
            "prior_rate_display": format_rate(prior_totals["acceptance_rate"]),
            "by_client": by_client,
            "by_service_class": _by_service_class(current, rules),
            "policy_variance": _policy_variance(current, rules, max_rows),
            "daily_distribution": _daily_distribution(current, start_day),
            "hourly_distribution": _hourly_distribution(current),
            "client_trend": trend,
            "outliers": _outliers(trend, weekly_cfg),
            "capacity_signal": _capacity_signal(
                current, start_day, int(weekly_cfg["capacity_min_offers"])
            ),
            "counts": {
                "requests": totals["offered"],
                "prior_requests": prior_totals["offered"],
                "clients": len(by_client),
                "clients_prior": len({view["client_key"] for view in prior}),
            },
            # THE HEADLINE, last for the same reason as in compute_daily: key
            # order is not report order, but it IS the order a generic walk over
            # the blob searches, and these carry their own service_class and
            # client_key fields that would otherwise shadow the breakdowns above.
            "missed_work": missed,
            "prior_missed_work": prior_missed,
            "missed_work_trend": _missed_work_trend(missed, prior_missed),
        }

        if persist:
            _upsert(
                sess,
                MetricsWeekly,
                {"company_id": company_id, "week_start": start_day},
                {
                    "metrics": _persistable(document),
                    "rules_version": version,
                    "computed_at": utcnow(),
                },
            )

        pending: list[dict[str, Any]] = list(missed_alerts)
        if emit_alerts:
            # Row alerts are deduplicated on (alert_id, request_id), so the
            # weekly pass only surfaces requests the hourly and daily runs never
            # saw -- a backfilled week, typically. It does not re-text the week.
            row_contexts = [
                (view["request_id"], _row_context(view, "weekly", version, rules)) for view in current
            ]
            pending.extend(
                _fire_alerts(
                    sess, row_contexts, "row", rules, version, company_id=company_id
                )
            )
            pending.extend(
                _fire_alerts(
                    sess,
                    _client_contexts(
                        sess,
                        end_utc,
                        rules,
                        default_class,
                        "weekly",
                        version,
                        company_id,
                        int(alert_cfg["client_window_hours"]),
                    ),
                    "client",
                    rules,
                    version,
                    company_id=company_id,
                )
            )
        document["alerts"] = pending

        logger.info(
            "weekly %s: offered %d accepted %d (%s) vs prior %d/%d (%s), %d alert(s)",
            start_day.isoformat(),
            totals["offered"],
            totals["accepted"],
            format_rate(totals["acceptance_rate"]),
            prior_totals["offered"],
            prior_totals["accepted"],
            format_rate(prior_totals["acceptance_rate"]),
            len(pending),
        )
        return document, pending

    return _run(session, persist, work)


# ==========================================================================
# compute_monthly
#
# The third cadence. The daily says what was lost yesterday, the weekly says
# what to change, and this says whether what changed is working. A month is the
# shortest window over which cause growth, client trajectory and the effect of
# a close-off are signal rather than weather -- and the coverage finding this
# system exists to serve (offers inside Mon-Fri 06:00-18:00 expire at 5.8%,
# offers outside it at 61.7%) needs a month of evidence to argue about staffing.
# ==========================================================================


def _weekly_distribution(
    views: Sequence[dict[str, Any]], start_day: _date, end_day: _date
) -> list[dict[str, Any]]:
    """One row per calendar week touched by ``[start_day, end_day)``.

    The within-month trajectory: four or five points is enough to see a month
    improving or decaying, which a single month total cannot show. Weeks are
    Monday-anchored and clipped to the month, so the first and last rows are
    usually partial and say so in ``days``.
    """
    buckets: dict[_date, list[dict[str, Any]]] = defaultdict(list)
    for view in views:
        day = view["local_date"]
        if day is not None:
            buckets[day - timedelta(days=day.weekday())].append(view)

    out: list[dict[str, Any]] = []
    week = start_day - timedelta(days=start_day.weekday())
    while week < end_day:
        first = max(week, start_day)
        last = min(week + timedelta(days=7), end_day)  # exclusive
        totals = _status_totals(buckets.get(week, []))
        out.append(
            {
                "week_start": week.isoformat(),
                "first_day": first.isoformat(),
                "last_day": (last - timedelta(days=1)).isoformat(),
                "days": (last - first).days,
                "partial": (last - first).days < 7,
                "offered": totals["offered"],
                "accepted": totals["accepted"],
                "denied": totals["denied"],
                "expired": totals["expired"],
                "canceled": totals["canceled"],
                "rate": totals["acceptance_rate"],
            }
        )
        week += timedelta(days=7)
    return out


def _coverage_trend(
    current: dict[str, Any] | None, prior: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Hour-of-day unanswered rate, this month against last.

    Built from the missed-work document's ``blind_spots.by_hour`` rather than
    recounted here, so the definition of "nobody answered" stays in one place
    (rules.yaml -> missed_work.buckets) and cannot drift between two reports.

    This is the table the staffing conversation is had over: the same hour, two
    months running, with both counts and the movement in percentage points.
    """

    def _hours(document: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
        if not isinstance(document, dict):
            return {}
        block = document.get("blind_spots")
        if not isinstance(block, dict):
            return {}
        rows = block.get("by_hour")
        if not isinstance(rows, list):
            return {}
        return {
            int(row["hour"]): row
            for row in rows
            if isinstance(row, dict) and row.get("hour") is not None
        }

    now, was = _hours(current), _hours(prior)
    if not now and not was:
        return []

    out: list[dict[str, Any]] = []
    for hour in range(24):
        cell = now.get(hour) or {}
        old = was.get(hour) or {}
        rate_now = cell.get("no_response_rate")
        rate_prior = old.get("no_response_rate")
        delta = _pp(rate_now, rate_prior)
        out.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "offers": int(cell.get("offers") or 0),
                "no_response": int(cell.get("no_response") or 0),
                "no_response_rate": rate_now,
                "offers_prior": int(old.get("offers") or 0),
                "no_response_prior": int(old.get("no_response") or 0),
                "no_response_rate_prior": rate_prior,
                # NEGATIVE is good here: fewer offers going unanswered.
                "delta_pp": delta,
                "direction": (
                    "unknown"
                    if delta is None
                    else "worse"
                    if delta > 1.0
                    else "better"
                    if delta < -1.0
                    else "flat"
                ),
                "is_blind_spot": bool(cell.get("is_blind_spot")),
                "was_blind_spot": bool(old.get("is_blind_spot")),
            }
        )
    return out


def _trend_period_aliases(trend: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy ``_client_trend``'s ``*_wk`` keys onto period-neutral names.

    ``_client_trend`` is period-agnostic arithmetic wearing weekly key names.
    Rather than fork it -- two copies of a comparison is how two reports start
    disagreeing -- the monthly document carries both spellings, so its template
    can say ``offers`` and ``offers_prior`` and mean this month and last.
    """
    out: list[dict[str, Any]] = []
    for row in trend:
        entry = dict(row)
        entry["offers"] = row.get("offers_wk")
        entry["accepted"] = row.get("accepted_wk")
        entry["rate"] = row.get("rate_wk")
        entry["offers_prior"] = row.get("offers_prior_wk")
        entry["accepted_prior"] = row.get("accepted_prior_wk")
        entry["rate_prior"] = row.get("rate_prior_wk")
        out.append(entry)
    return out


def compute_monthly(
    month_start: datetime | _date | str,
    *,
    session: Session | None = None,
    company_id: str | None = None,
    account_id: str | None = None,
    persist: bool = True,
    emit_alerts: bool | None = None,
) -> dict[str, Any]:
    """Metrics for one local calendar month, against the month before it.

    ``month_start`` is normalised to the 1st of the month it falls in (and
    accepts the ``YYYY-MM`` form the CLI takes), so the scheduler, a human
    typing ``2026-06`` and a human typing ``2026-06-17`` all land on the same
    row. Persists the JSON document to ``metrics_monthly``, unique on
    ``month_start``, so a re-run of June upserts June.

    Mirrors :func:`compute_weekly` deliberately: same totals, same client trend,
    same missed-work pass over both periods. What differs is the span -- and
    that the comparison is month over month, which is the horizon on which a
    staffing change or a close-off conversation can actually be judged.

    ``emit_alerts`` defaults to following ``persist``, but the row-scoped alerts
    are deduped on (alert_id, request_id) forever, so a monthly run re-raises
    nothing the hourly and daily runs already sent.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company_scope(company_id, account_id) as company:
        return _compute_monthly(
            month_start,
            session=session,
            company_id=company,
            persist=persist,
            emit_alerts=emit_alerts,
        )


def _compute_monthly(
    month_start: datetime | _date | str,
    *,
    session: Session | None,
    company_id: str,
    persist: bool,
    emit_alerts: bool | None,
) -> dict[str, Any]:
    start_day = month_start_for(month_start)
    end_day = month_end_for(start_day)  # exclusive
    prior_start_day = month_start_for(start_day - timedelta(days=1))

    start_utc = _local_midnight_utc(start_day)
    end_utc = _local_midnight_utc(end_day)
    prior_start_utc = _local_midnight_utc(prior_start_day)

    days_in_month = (end_day - start_day).days
    prior_days_in_month = (start_day - prior_start_day).days

    emit_alerts = persist if emit_alerts is None else emit_alerts
    rules = _rules(company_id)
    version = rules_version()
    _, default_class = _service_class_order(rules)
    detail_cfg = _metrics_cfg(rules, "detail")
    weekly_cfg = _metrics_cfg(rules, "weekly")
    alert_cfg = _metrics_cfg(rules, "alerts")
    max_examples = int(detail_cfg["max_denial_examples"])
    max_rows = int(detail_cfg["max_rows"])
    zero_pct = _notifications_formatting().get("zero_offers_rate_pct", 0)

    def work(sess: Session) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        # One two-month query, split locally. Two round trips would be two
        # snapshots, and a row ingested between them would skew the delta.
        rows = _load_rows(sess, prior_start_utc, end_utc, company_id)
        all_views = _views(rows, rules, default_class)
        current = [
            view
            for view, row in zip(all_views, rows)
            if row.offered_at is not None and row.offered_at >= start_utc
        ]
        prior = [
            view
            for view, row in zip(all_views, rows)
            if row.offered_at is not None and row.offered_at < start_utc
        ]

        totals = _status_totals(current)
        prior_totals = _status_totals(prior)
        by_client = _by_client(current, max_examples)
        trend = _client_trend(current, prior, float(weekly_cfg["direction_flat_band_pp"]))

        # THE HEADLINE, plus the same model over the prior month. Everything the
        # monthly exists to say -- which causes grew, whether a blind spot
        # closed, whether a close-off took effect -- is a difference between
        # these two documents.
        missed, missed_alerts = _missed_work_pass(
            sess,
            datetime.combine(start_day, _time.min),
            datetime.combine(end_day, _time.min),
            "monthly",
            rules,
            version,
            company_id,
            persist,
            bool(emit_alerts),
        )
        prior_missed, _ = _missed_work_pass(
            sess,
            datetime.combine(prior_start_day, _time.min),
            datetime.combine(start_day, _time.min),
            "monthly",
            rules,
            version,
            company_id,
            # Context for this month's trend, not a report of its own: neither
            # persisted again nor re-alerted, so a monthly run cannot re-text
            # last month.
            False,
            False,
        )

        # Volume per day, because a 30-day month against a 31-day month is a 3%
        # difference before anything real happened. Stated so nobody reads the
        # raw delta as a trend.
        per_day = _round_half_up(totals["offered"] / days_in_month, 2) if days_in_month else None
        prior_per_day = (
            _round_half_up(prior_totals["offered"] / prior_days_in_month, 2)
            if prior_days_in_month
            else None
        )

        document: dict[str, Any] = {
            "report_type": "monthly",
            "month_start": start_day.isoformat(),
            "month_end": (end_day - timedelta(days=1)).isoformat(),
            "month_label": start_day.strftime("%Y-%m"),
            "month_name": start_day.strftime("%B %Y"),
            "days_in_month": days_in_month,
            "prior_month_start": prior_start_day.isoformat(),
            "prior_month_end": (start_day - timedelta(days=1)).isoformat(),
            "prior_month_label": prior_start_day.strftime("%Y-%m"),
            "prior_month_name": prior_start_day.strftime("%B %Y"),
            "prior_days_in_month": prior_days_in_month,
            "timezone": local_timezone_name(),
            "window_start_utc": start_utc.isoformat(sep="T"),
            "window_end_utc": end_utc.isoformat(sep="T"),
            "prior_window_start_utc": prior_start_utc.isoformat(sep="T"),
            "prior_window_end_utc": start_utc.isoformat(sep="T"),
            "rules_version": version,
            "computed_at": utcnow().isoformat(sep="T"),
            "totals": totals,
            "prior_totals": prior_totals,
            "totals_delta": {
                "offered": totals["offered"] - prior_totals["offered"],
                "accepted": totals["accepted"] - prior_totals["accepted"],
                "denied": totals["denied"] - prior_totals["denied"],
                "expired": totals["expired"] - prior_totals["expired"],
                "canceled": totals["canceled"] - prior_totals["canceled"],
                "acceptance_rate_pp": _pp(
                    totals["acceptance_rate"], prior_totals["acceptance_rate"]
                ),
            },
            # Months are 28 to 31 days long. The per-day figures are what makes
            # "offers are up 40" mean something.
            "offers_per_day": per_day,
            "prior_offers_per_day": prior_per_day,
            "offers_per_day_delta": (
                _round_half_up(per_day - prior_per_day, 2)
                if per_day is not None and prior_per_day is not None
                else None
            ),
            "rate_pct": rate_pct(totals["acceptance_rate"], zero_offers=zero_pct),
            "rate_display": format_rate(totals["acceptance_rate"]),
            "prior_rate_display": format_rate(prior_totals["acceptance_rate"]),
            "by_client": by_client,
            "by_service_class": _by_service_class(current, rules),
            "policy_variance": _policy_variance(current, rules, max_rows),
            "daily_distribution": _daily_distribution(current, start_day, days_in_month),
            "weekly_distribution": _weekly_distribution(current, start_day, end_day),
            "hourly_distribution": _hourly_distribution(current),
            "client_trend": _trend_period_aliases(trend),
            "outliers": _outliers(trend, weekly_cfg),
            # Hour by hour, this month against last -- the coverage argument.
            "coverage_trend": _coverage_trend(missed, prior_missed),
            "counts": {
                "requests": totals["offered"],
                "prior_requests": prior_totals["offered"],
                "clients": len(by_client),
                "clients_prior": len({view["client_key"] for view in prior}),
            },
            # THE HEADLINE, last for the same reason as in compute_daily and
            # compute_weekly: key order is not report order, but it IS the order
            # a generic walk over the blob searches, and these carry their own
            # service_class and client_key fields that would shadow the
            # breakdowns above.
            "missed_work": missed,
            "prior_missed_work": prior_missed,
            "missed_work_trend": _missed_work_trend(missed, prior_missed),
        }

        if persist:
            _upsert(
                sess,
                MetricsMonthly,
                {"month_start": start_day},
                {
                    "metrics": _persistable(document),
                    "rules_version": version,
                    "computed_at": utcnow(),
                },
            )

        pending: list[dict[str, Any]] = list(missed_alerts)
        if emit_alerts:
            # Row alerts dedupe on (alert_id, request_id) forever, so this only
            # surfaces requests the hourly, daily and weekly runs never saw --
            # a backfilled month. It does not re-text the month.
            row_contexts = [
                (view["request_id"], _row_context(view, "monthly", version, rules))
                for view in current
            ]
            pending.extend(
                _fire_alerts(
                    sess, row_contexts, "row", rules, version, company_id=company_id
                )
            )
            pending.extend(
                _fire_alerts(
                    sess,
                    _client_contexts(
                        sess,
                        end_utc,
                        rules,
                        default_class,
                        "monthly",
                        version,
                        company_id,
                        int(alert_cfg["client_window_hours"]),
                    ),
                    "client",
                    rules,
                    version,
                    company_id=company_id,
                )
            )
        document["alerts"] = pending

        logger.info(
            "monthly %s: offered %d accepted %d (%s) vs prior %d/%d (%s), %d alert(s)",
            start_day.strftime("%Y-%m"),
            totals["offered"],
            totals["accepted"],
            format_rate(totals["acceptance_rate"]),
            prior_totals["offered"],
            prior_totals["accepted"],
            format_rate(prior_totals["acceptance_rate"]),
            len(pending),
        )
        return document, pending

    return _run(session, persist, work)


# ==========================================================================
# Convenience readers used by the CLI, the scheduler and the dashboard
# ==========================================================================


def get_stored_hourly(
    window_start: datetime | _date | str,
    *,
    session: Session | None = None,
    company_id: str | None = None,
) -> dict[str, Any] | None:
    """Read back a persisted hourly row for one company as a dict, or None."""
    with _company_scope(company_id) as company:
        start_utc = to_utc(
            parse_local_datetime(window_start).replace(minute=0, second=0, microsecond=0)
        )

    def read(sess: Session) -> dict[str, Any] | None:
        row = sess.scalars(
            select(MetricsHourly)
            .where(MetricsHourly.company_id == company)
            .where(MetricsHourly.window_start == start_utc)
            .limit(1)
        ).first()
        if row is None:
            return None
        return {
            "company_id": row.company_id,
            "window_start": to_local(row.window_start).isoformat(sep="T"),
            "window_start_utc": row.window_start.isoformat(sep="T"),
            "offered_window": row.offered,
            "accepted_window": row.accepted,
            "acceptance_rate_window": rate_or_none(row.accepted, row.offered),
            "offered_day_running": row.day_running_offered,
            "accepted_day_running": row.day_running_accepted,
            "acceptance_rate_day": rate_or_none(
                row.day_running_accepted, row.day_running_offered
            ),
            "rules_version": row.rules_version,
            "computed_at": row.computed_at.isoformat(sep="T") if row.computed_at else None,
        }

    if session is not None:
        return read(session)
    with get_session(commit=False) as owned:
        return read(owned)


def _stored_blob(
    model: Any, column: str, value: _date, session: Session | None, company_id: str
):
    def read(sess: Session) -> dict[str, Any] | None:
        row = sess.scalars(
            select(model)
            .where(model.company_id == company_id)
            .where(getattr(model, column) == value)
            .limit(1)
        ).first()
        if row is None:
            return None
        document = dict(row.metrics or {})
        document["company_id"] = row.company_id
        document["rules_version"] = row.rules_version
        document["computed_at"] = row.computed_at.isoformat(sep="T") if row.computed_at else None
        return document

    if session is not None:
        return read(session)
    with get_session(commit=False) as owned:
        return read(owned)


def get_stored_daily(
    day: datetime | _date | str,
    *,
    session: Session | None = None,
    company_id: str | None = None,
) -> dict[str, Any] | None:
    """Read back a persisted daily document for one company, or None."""
    with _company_scope(company_id) as company:
        return _stored_blob(MetricsDaily, "date", parse_local_date(day), session, company)


def get_stored_weekly(
    week_start: datetime | _date | str,
    *,
    session: Session | None = None,
    company_id: str | None = None,
) -> dict[str, Any] | None:
    """Read back a persisted weekly document for one company, or None."""
    with _company_scope(company_id) as company:
        return _stored_blob(
            MetricsWeekly, "week_start", week_start_for(week_start), session, company
        )


def get_stored_monthly(
    month_start: datetime | _date | str,
    *,
    session: Session | None = None,
    company_id: str | None = None,
) -> dict[str, Any] | None:
    """Read back a persisted monthly document for one company, or None."""
    with _company_scope(company_id) as company:
        return _stored_blob(
            MetricsMonthly, "month_start", month_start_for(month_start), session, company
        )


def recompute_days(
    start: datetime | _date | str,
    end: datetime | _date | str,
    *,
    company_id: str | None = None,
    account_id: str | None = None,
    emit_alerts: bool = False,
) -> list[dict[str, Any]]:
    """Recompute every local day in ``[start, end]`` inclusive, for one company.

    Used by ``seed`` and by backfills. Alerts default OFF here: replaying a
    month of history should not replay a month of text messages.
    """
    with _company_scope(company_id, account_id) as company:
        first = parse_local_date(start)
        last = parse_local_date(end)
        if last < first:
            first, last = last, first

        out: list[dict[str, Any]] = []
        day = first
        while day <= last:
            out.append(
                compute_daily(day, company_id=company, emit_alerts=emit_alerts)
            )
            day += timedelta(days=1)
        return out
