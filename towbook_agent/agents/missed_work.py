"""The inventory of work we did NOT get -- the headline of every report.

The build document framed this system around acceptance rate. The owner's actual
question is narrower and more useful:

    What are we NOT accepting? Do we want it? If yes, what do we need to do to
    accept it? If no, how do we stop it being offered at all?

Acceptance rate is a *symptom*. The deliverable is an **inventory of missed
work, attributed to a cause, with an action attached**. This module computes
that inventory. It is the implementation of MISSED_WORK_MODEL.md sections 1-6,
and every threshold, bucket, cause and remedy in it is DATA in
``config/rules.yaml -> missed_work``. There is not one status code and not one
denial-reason string in this file.

The seven outputs
-----------------
:func:`classify_bucket`      every offer lands in exactly one bucket (section 1)
:func:`attribute_cause`      bucket + denial reason -> cause + remedy (section 2)
:func:`compute_missed_work`  the whole document (section 3)
:func:`blind_spots`          7 x 24 hour-of-week grid of unanswered offers (4)
:func:`closeoff_candidates`  work we do not want, grouped by client (5)
:func:`client_comparison`    the same trucks behaving differently per client (6)
:func:`coverage_analysis`    inside vs outside the staffed window (7)

COVERAGE IS THE DIMENSION, NOT HOUR-OF-DAY
------------------------------------------
The 7 x 24 grid finds *which* hours leak. It cannot state the finding, because
the finding is not about clock hours at all -- it is about whether a human was
watching. Measured on 30 days of real Agero traffic against the owner's stated
staffed window (Mon-Fri 06:00-18:00, ``missed_work.coverage``):

    INSIDE   989 offers,  57 expired  ( 5.8%)
    OUTSIDE 1017 offers, 627 expired  (61.7%)

Near-identical volume. Same trucks, which sat idle at 19:00 exactly as they sat
idle at 11:00. 99.1% of accepted rows carry a named human responder, so every
job needs a person and outside the window there is no person. **This is a
coverage problem, not a truck problem**, and the headline recoverable figure is
therefore the OUTSIDE number -- with the INSIDE number kept beside it, because
the contrast is the business case and a single number cannot carry it.

Windows are a list, so a Saturday shift is a YAML edit; the label a row lands in
is on every row view as ``coverage_label`` and on the row-level alert context,
which is what ``coverage_gap`` fires on.

RANKING IS BY JOB COUNT, NOT MONEY
----------------------------------
``offerAmount`` is empty on **100%** of the records this account produces --
3,079 of 3,079 over 30 days. No dollar figure is derivable *from the feed* and
none is invented from it. Every document carries ``ranking_basis: "job_count"``
and ``revenue_available`` so that every consumer -- the dashboard, the email
template, the Analyst prompt -- can state which it is rather than leaving a
reader to assume the numbers are revenue.

``missed_work.job_value_by_client`` is the only route to a dollar figure: real
per-client average job values, supplied by a human from invoice data this feed
does not carry. When it is populated, each inventory row gains an
``estimated_value`` and ``revenue_available`` flips true. Ranking still happens
on job count. A row contributes only when its client has a configured value AND
its service class is one those averages describe
(``job_value_applies_to``, default ``should_accept``); everything else
contributes **nothing** and is counted in ``unpriced_missed``. A partial price
list therefore understates visibly. No default value is ever invented for a
client, and no value is stretched to cover a service it does not describe.

Why buckets are not the canonical status
----------------------------------------
``requests.status`` is a five-value controlled vocabulary and the portal emits
fourteen statuses. Collapsing them destroys exactly the distinctions this model
exists to make: "Expired" (nobody answered) and "Accept Failed" (we answered and
the accept failed) both become ``expired``; "Rejected" (we said no) and "Rejected
By Motor Club" (the client pulled it) are three words apart and belong in
different buckets. So bucketing resolves in this order, and each row records
which route it took in ``bucket_sources``:

1. ``requests.status_code``  -> ``missed_work.buckets``                (exact)
2. ``requests.status_raw``   -> ``missed_work.bucket_by_status_name``  (exact)
3. ``requests.status``       -> ``missed_work.bucket_by_canonical_status`` (lossy)
4. ``missed_work.bucket_default`` -- a visible "unknown status", never a guess

Route 3 is lossy in one place and says so: canonical ``expired`` covers Expired,
Another Provider Responded *and* Accept Failed, so it must merge the last into
the first. It catches history ingested before ``status_raw`` existed, and it
rescues a source label the bucket maps have not been taught yet -- schema.yaml's
``status_vocabulary`` is a separate and richer table, so it often knows one this
block does not. Any row that reached route 3 or 4 while carrying a label is
named in ``unknown_statuses`` with the bucket it was given, because a new status
silently absorbed into a plausible bucket is how a report starts being
confidently incorrect.

Determinism
-----------
No LLM, no network, no heuristics (hard constraint #3). Every list is sorted on
an explicit total order, so recomputing an unchanged window writes a
byte-identical JSON blob and the upsert on
``(window_start, window_end, period_type)`` is genuinely idempotent (#4).

Rates
-----
``None`` when the denominator is zero -- never ``0``, never a ZeroDivisionError.
``None`` is persisted as ``null`` and rendered ``"-"`` by
:func:`agents.metrics.format_rate`. A zero-offer hour is the *absence* of a
rate, and calling it 0% turns a quiet Tuesday into a fake catastrophe.

Time
----
Rows are stored naive UTC. Windows are given in *local* time (``TZ``, default
America/Detroit) and converted through ``zoneinfo`` before querying, reusing
metrics' helpers so the two modules can never disagree about what "Tuesday"
means. ``requestDate`` from the portal is already company-local and ingestion
localises it once; nothing here converts a second time.

Table ownership
---------------
This module writes ``metrics_missed_work`` and nothing else. It reads
``requests``. Alerts are *described* here -- :func:`alert_contexts` builds the
evaluation contexts -- but they are evaluated, deduplicated and emitted by
agents/metrics.py, which owns that machinery and the ``alerts_fired`` read path.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from ..core import companies as _companies
from ..core.config_loader import rules_version
from ..core.db import get_session
from ..core.models import MetricsMissedWork, utcnow
from . import metrics as _metrics

__all__ = [
    # contract
    "classify_bucket",
    "attribute_cause",
    "compute_missed_work",
    "blind_spots",
    "closeoff_candidates",
    "client_comparison",
    "coverage_label",
    "coverage_analysis",
    "territory_band",
    "in_territory",
    # wiring
    "alert_contexts",
    "get_stored_missed_work",
    # introspection
    "DEFAULTS",
    "UNKNOWN_BUCKET",
    "UNATTRIBUTED_CAUSE",
    "UNATTRIBUTED_REMEDY",
    "UNCOVERED_LABEL",
    "OUTSIDE_TERRITORY_LABEL",
    "UNKNOWN_TERRITORY_LABEL",
]

logger = logging.getLogger(__name__)

#: Where a row goes when rules.yaml declares no ``bucket_default``. Not a portal
#: status and not a business category -- a visible "we could not read this",
#: exactly like ``classifier.UNCLASSIFIED``.
UNKNOWN_BUCKET: str = "unknown_status"

#: Same idea for causes. Used only when rules.yaml has nothing to say about a
#: row, so an unattributed line in a report is a prompt to extend the config.
UNATTRIBUTED_CAUSE: str = "unattributed"
UNATTRIBUTED_REMEDY: str = "review"

#: The label a row gets when it falls inside NO configured coverage window --
#: i.e. nobody was rostered. Overridden by
#: ``missed_work.coverage.default_label``; named here so the fallback is
#: greppable and so the shipped ``coverage_gap`` alert has something to match
#: even if the block is deleted from rules.yaml.
UNCOVERED_LABEL: str = "uncovered"

#: The label for an offer whose pickup ZIP is in no configured band -- work
#: this company does not travel for. Overridden by
#: ``missed_work.territory.default_label``.
OUTSIDE_TERRITORY_LABEL: str = "outside"

#: The label for an offer we cannot place: no ZIP on the row and none parseable
#: out of the address, or no territory configured at all. DELIBERATELY NOT the
#: outside label -- see territory_band(). An unplaceable offer is counted as
#: ours, because the alternative is silently shrinking the missed-work
#: inventory, and this model exists to stop work going unseen.
UNKNOWN_TERRITORY_LABEL: str = "territory_unknown"

#: Trailing ZIP in a free-text address, for CSV rows that carry no ZIP field.
_ZIP_IN_TEXT = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

#: Every tunable, with the value used when rules.yaml does not override it.
#: Mirrors :data:`agents.metrics.DEFAULTS` in shape and intent.
DEFAULTS: dict[str, dict[str, Any]] = {
    "blind_spot": {
        "min_offers": 5,
        "threshold": 0.35,
    },
    "closeoff": {
        "min_offers": 5,
        "max_rate": 0.10,
        "exclude_wanted_classes": True,
    },
    "inventory": {
        "rank_by": "job_count",
        "restrict_to_should_accept": True,
        "top_n": 10,
        "top_clients": 5,
        "top_service_types": 5,
    },
    # THE JOB LIST. Every other section of this document is an aggregate --
    # which is right for deciding what to change, and useless for the other
    # thing the owner does with these reports: pull one job up in Towbook and
    # ask why that one went the way it did. That needs the reference Towbook
    # searches on, per job, which is what this section carries.
    "job_list": {
        # Rows per report before it says "and N more". A day misses ~60 offers
        # on this account, so 100 shows a daily window whole; a weekly or
        # monthly one truncates, says so, and points at the dashboard, which
        # pages the full set.
        "max_rows": 100,
        # False on purpose. The inventory restricts itself to should_accept
        # classes because it is about work we want. This list is a lookup
        # table, and "why did we turn down that lockout" is a question the
        # owner is entitled to ask about a class the policy declines.
        "restrict_to_should_accept": False,
    },
    "client_comparison": {
        "min_offers": 20,
        "gap_pp": 20.0,
    },
    # The owner's stated staffed window, verified in the data (see the module
    # docstring). Mirrors config/rules.yaml -> missed_work.coverage so the code
    # and the config describe the same shift, and so a rules.yaml with the block
    # deleted still labels rows rather than silently calling everything covered.
    "coverage": {
        "windows": [
            {
                "name": "covered",
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "06:00",
                "end": "18:00",
            }
        ],
        "default_label": UNCOVERED_LABEL,
    },
    "alerts": {
        # Trailing windows for the three aggregate missed-work alerts. Named in
        # the rule expressions themselves (`cause_count_24h`, `*_7d`), so these
        # exist to keep the code and the rule text describing the same window.
        "blind_spot_window_days": 7,
        "cause_window_hours": 24,
        "cause_baseline_days": 7,
        "closeoff_window_days": 7,
    },
}

#: Removed from the document before storage so a recompute of an unchanged
#: window writes a byte-identical blob.
_VOLATILE_KEYS: tuple[str, ...] = ("computed_at", "alerts")

_WEEKDAY_LABELS: tuple[str, ...] = _metrics._WEEKDAY_LABELS

_rate = _metrics.rate_or_none


# ==========================================================================
# Config access
# ==========================================================================


def _rules(
    rules: Mapping[str, Any] | None = None, company_id: str | None = None
) -> Mapping[str, Any]:
    """The rules to use, re-reading rules.yaml when none was given.

    Same hot-reload contract as agents/classifier.py: the default path asks the
    config loader every call, so saving rules.yaml takes effect on the next run
    with no restart. Callers that need one consistent snapshot across a batch
    pass ``rules`` explicitly -- which is what :func:`compute_missed_work` does.

    The rules returned are THIS COMPANY's: global rules.yaml with the company's
    coverage window, job values and thresholds merged over it. That matters
    most here, because the coverage block is the one every headline in this
    module rests on and it is the override a new tenant sets first.
    ``company_id=None`` means the company currently active
    (``core.companies.use_company``), which the entry points below set.
    """
    if rules is None:
        return _companies.rules_for(company_id) or {}
    if not isinstance(rules, Mapping):
        raise TypeError(f"rules must be a mapping, got {type(rules).__name__}")
    return rules


def _company(company_id: str | None, account_id: str | None = None):
    """Activate the company this call is about, accepting either argument name.

    ``company_id`` is the current name, ``account_id`` the one the
    single-company build used; whichever was given wins. Activating rather than
    only resolving is what makes ``_metrics.to_local`` -- and therefore every
    ``local_weekday`` / ``local_hour`` the coverage windows and the 7 x 24 grid
    are cut on -- use this company's own clock.
    """
    return _companies.use_company(company_id if company_id is not None else account_id)


def _block(rules: Mapping[str, Any]) -> Mapping[str, Any]:
    block = rules.get("missed_work")
    return block if isinstance(block, Mapping) else {}


def _cfg(rules: Mapping[str, Any], section: str) -> dict[str, Any]:
    """Merge ``rules.yaml -> missed_work.<section>`` over :data:`DEFAULTS`."""
    merged = dict(DEFAULTS.get(section, {}))
    override = _block(rules).get(section)
    if isinstance(override, Mapping):
        for key, value in override.items():
            if value is not None:
                merged[key] = value
    return merged


def _mapping(rules: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = _block(rules).get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _name_set(rules: Mapping[str, Any], key: str) -> frozenset[str]:
    value = _block(rules).get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(item).strip() for item in value if item is not None)


def _fold(value: Any) -> str:
    """Casefold + collapse whitespace, for matching only. Never stored."""
    if value is None:
        return ""
    text = str(value).replace(" ", " ").replace(" ", " ")
    return " ".join(text.split()).strip().casefold()


def _field(row: Any, *names: str) -> Any:
    """Read a field from a view dict or an ORM row, whichever we were handed."""
    for name in names:
        if isinstance(row, Mapping):
            if name in row and row[name] not in (None, ""):
                return row[name]
        else:
            value = getattr(row, name, None)
            if value not in (None, ""):
                return value
    return None


# ==========================================================================
# Section 1 -- buckets
# ==========================================================================


def _bucket_with_source(row: Any, rules: Mapping[str, Any]) -> tuple[str, str]:
    """``(bucket, which map answered)``. See the module docstring for the order."""
    block = _block(rules)
    default = str(block.get("bucket_default") or UNKNOWN_BUCKET)

    # 1. numeric code -- exact, lossless, and what the JSON API carries.
    code = _field(row, "status_code")
    if code is not None:
        try:
            key = str(int(code))
        except (TypeError, ValueError):
            key = None
        if key is not None:
            by_code = _mapping(rules, "buckets")
            for candidate, bucket in by_code.items():
                if str(candidate).strip() == key and bucket:
                    return (str(bucket), "status_code")

    # 2. verbatim source label -- EXACT match, deliberately not a substring test.
    #    "rejected" (we declined) and "rejected by motor club" (the client
    #    withdrew) differ by three words; a substring test would merge 723 client
    #    withdrawals into our own decline count.
    label = _fold(_field(row, "status_raw", "status_name", "statusName"))
    if label:
        for candidate, bucket in _mapping(rules, "bucket_by_status_name").items():
            if _fold(candidate) == label and bucket:
                return (str(bucket), "status_name")

    # 3. the five-value controlled vocabulary -- coarse, and only for history
    #    ingested before status_raw existed, or for a label these maps have not
    #    been taught yet. schema.yaml's status_vocabulary is a separate and
    #    richer table, so it may well know a label this block does not; letting
    #    it answer is better than throwing the row away. When it answers for a
    #    row that DID have a label, the label is still reported under
    #    `unknown_statuses` so somebody extends missed_work.buckets.
    canonical = _fold(_field(row, "status"))
    if canonical:
        for candidate, bucket in _mapping(rules, "bucket_by_canonical_status").items():
            if _fold(candidate) == canonical and bucket:
                return (str(bucket), "canonical_status")

    return (default, "default")


def classify_bucket(row: Any, rules: Mapping[str, Any] | None = None) -> str:
    """Put one offer in exactly one bucket (MISSED_WORK_MODEL.md section 1).

    Returns one of the bucket names declared in
    ``rules.yaml -> missed_work`` -- shipped as ``won``, ``in_flight``,
    ``declined``, ``no_response``, ``accept_failed``, ``client_withdrew`` -- or
    the configured ``bucket_default`` for a status no map knows.

    ``row`` may be a view dict or an ORM ``Request``; both are read through the
    same field names. Pure: same row and same rules, same bucket, always.
    """
    return _bucket_with_source(row, _rules(rules))[0]


# ==========================================================================
# Section 2 -- cause and remedy
# ==========================================================================


def _cause_defaults(rules: Mapping[str, Any]) -> tuple[str, str]:
    block = _block(rules)
    cause = str(block.get("cause_default") or UNATTRIBUTED_CAUSE)
    remedy = str(block.get("remedy_default") or UNATTRIBUTED_REMEDY)
    return (cause, remedy)


def _remedy_entry(row: Any, rules: Mapping[str, Any], bucket: str) -> dict[str, Any]:
    """The full ``remedies`` / ``bucket_remedies`` entry that explains this row."""
    # Buckets that carry no denial_reason by definition take their cause from
    # the bucket. `no_response` is the one that matters: its cause is always
    # attention and its remedy is always alerting/coverage in a specific window,
    # which is exactly what blind_spots() quantifies.
    bucket_remedies = _mapping(rules, "bucket_remedies")
    for candidate, entry in bucket_remedies.items():
        if str(candidate).strip() == bucket and isinstance(entry, Mapping):
            return dict(entry)

    # Otherwise the row carries a reason. Match the RAW text first, then the
    # normalized bucket, because the two carry different information: "Not
    # Enough Information" normalizes to `other` (correctly -- that is the
    # verified grouping) yet deserves its own cause, since it is the one decline
    # with a cheap process fix.
    raw = _fold(_field(row, "denial_reason"))
    normalized = _fold(_field(row, "denial_reason_normalized"))

    remedies = _mapping(rules, "remedies")
    if not raw:
        for entry in remedies.values():
            if isinstance(entry, Mapping) and entry.get("match_blank"):
                return dict(entry)
    else:
        for haystack in (raw, normalized):
            if not haystack:
                continue
            for entry in remedies.values():
                if not isinstance(entry, Mapping) or entry.get("match_blank"):
                    continue
                terms = entry.get("match_any") or []
                if isinstance(terms, str):
                    terms = [terms]
                for term in terms:
                    if term and _fold(term) in haystack:
                        return dict(entry)

    return {}


def attribute_cause(
    row: Any, rules: Mapping[str, Any] | None = None
) -> tuple[str, str]:
    """``(cause, remedy_class)`` for one offer (MISSED_WORK_MODEL.md section 2).

    The pair is what turns a number into an action. ``cause`` says *why* the job
    was not taken -- equipment, staffing, coverage, information, attention --
    and ``remedy_class`` says *what kind of fix it is*: capital, scheduling,
    territory, process, alerting. Both come from ``rules.yaml``; reassigning a
    reason to a different remedy is a YAML edit.

    Falls back to ``(cause_default, remedy_default)`` -- shipped as
    ``("unattributed", "review")`` -- when rules.yaml has nothing to say, so an
    unexplained line in a report is a prompt to extend the config rather than a
    silently miscategorised number.
    """
    resolved = _rules(rules)
    bucket = classify_bucket(row, resolved)
    entry = _remedy_entry(row, resolved, bucket)
    cause_default, remedy_default = _cause_defaults(resolved)
    cause = str(entry.get("cause") or cause_default)
    remedy = str(entry.get("remedy") or remedy_default)
    return (cause, remedy)


def _remedy_question(cause: str, rules: Mapping[str, Any]) -> str:
    """The sentence a report prints beside the number, from rules.yaml."""
    for source in ("remedies", "bucket_remedies"):
        for entry in _mapping(rules, source).values():
            if isinstance(entry, Mapping) and str(entry.get("cause") or "") == cause:
                question = entry.get("question")
                if question:
                    return str(question)
    return ""


# ==========================================================================
# Section 7 -- the operating window, as a first-class dimension
#
# Hour-of-day is the wrong cut. INSIDE vs OUTSIDE the hours somebody is
# rostered is the right one, and it is the cut the numbers actually split on:
# 5.8% of Agero offers expire inside Mon-Fri 06:00-18:00, 61.7% outside it, on
# near-identical volume. Everything below is driven by
# ``rules.yaml -> missed_work.coverage``; there is no clock time in this code.
# ==========================================================================


#: Weekday token -> Python weekday index (Monday is 0, matching
#: ``datetime.weekday()`` and ``_WEEKDAY_LABELS``). Full names, three-letter
#: abbreviations and the bare index all resolve, because a config file that
#: rejects "monday" for not being "mon" is a config file that wastes an evening.
_WEEKDAY_TOKENS: dict[str, int] = {}
for _index, _name in enumerate(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
):
    _WEEKDAY_TOKENS[_name] = _index
    _WEEKDAY_TOKENS[_name[:3]] = _index
    _WEEKDAY_TOKENS[str(_index)] = _index

#: Tokens meaning "every day", for a 24/7 window.
_ALL_DAYS: frozenset[str] = frozenset({"all", "any", "daily", "everyday", "every day"})

_MINUTES_PER_DAY = 24 * 60


def _parse_days(value: Any) -> frozenset[int]:
    """``[mon, tue]`` / ``all`` / ``[0, 5]`` -> the set of weekday indexes."""
    if value is None:
        return frozenset(range(7))
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    days: set[int] = set()
    for item in value:
        token = _fold(item)
        if token in _ALL_DAYS:
            return frozenset(range(7))
        index = _WEEKDAY_TOKENS.get(token)
        if index is None:
            logger.warning(
                "missed_work.coverage: %r is not a weekday; ignoring it", item
            )
            continue
        days.add(index)
    return frozenset(days)


def _parse_clock(value: Any, fallback: int) -> int:
    """``"06:00"`` -> minutes since local midnight. ``"24:00"`` is 1440.

    Accepts ``6``, ``"6"``, ``"06:00"``, ``"6:30"`` and a ``datetime.time``.
    Anything unreadable warns and falls back rather than silently becoming
    midnight -- a typo'd shift start that quietly became 00:00 would relabel a
    month of overnight misses as covered.
    """
    if value is None:
        return fallback
    hour = getattr(value, "hour", None)
    if hour is not None and not isinstance(value, (str, bytes)):
        return int(hour) * 60 + int(getattr(value, "minute", 0) or 0)
    text = str(value).strip()
    if not text:
        return fallback
    parts = text.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        logger.warning("missed_work.coverage: %r is not a HH:MM time; ignoring", value)
        return fallback
    total = hours * 60 + minutes
    if not 0 <= total <= _MINUTES_PER_DAY:
        logger.warning("missed_work.coverage: %r is out of range; ignoring", value)
        return fallback
    return total


def _coverage_cfg(rules: Mapping[str, Any]) -> dict[str, Any]:
    return _cfg(rules, "coverage")


def _coverage_default_label(rules: Mapping[str, Any]) -> str:
    return str(_coverage_cfg(rules).get("default_label") or UNCOVERED_LABEL).strip()


def _clock_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _days_text(days: frozenset[int]) -> str:
    """``{0,1,2,3,4}`` -> ``"Mon-Fri"``; a broken run stays comma-separated."""
    if not days:
        return "never"
    if len(days) == 7:
        return "every day"
    ordered = sorted(days)
    if ordered == list(range(ordered[0], ordered[-1] + 1)) and len(ordered) > 2:
        return f"{_WEEKDAY_LABELS[ordered[0]]}-{_WEEKDAY_LABELS[ordered[-1]]}"
    return ", ".join(_WEEKDAY_LABELS[day] for day in ordered)


def _coverage_windows(rules: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The configured windows, parsed once, IN FILE ORDER.

    List order is evaluation order and the first window a row falls inside wins,
    so a narrower exception can be listed above a broader shift. Multiple
    windows are the point: adding a Saturday day shift is appending an entry
    here, never a code change.
    """
    raw = _coverage_cfg(rules).get("windows")
    if isinstance(raw, Mapping):
        # Tolerate `windows: {covered: {...}}` as well as a list.
        raw = [
            {**entry, "name": entry.get("name", name)}
            for name, entry in raw.items()
            if isinstance(entry, Mapping)
        ]
    if not isinstance(raw, (list, tuple)):
        return []

    default_label = _coverage_default_label(rules)
    out: list[dict[str, Any]] = []
    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or f"window_{position + 1}").strip()
        if name == default_label:
            # Would make the default label unreachable and the contrast
            # meaningless. Say so rather than producing a report where every
            # row is "covered".
            logger.warning(
                "missed_work.coverage: window %r uses the default_label; renaming "
                "it to %r so the two stay distinguishable",
                name,
                f"{name}_window",
            )
            name = f"{name}_window"
        days = _parse_days(entry.get("days"))
        start = _parse_clock(entry.get("start"), 0)
        end = _parse_clock(entry.get("end"), _MINUTES_PER_DAY)
        if not days:
            logger.warning(
                "missed_work.coverage: window %r names no valid days; it will "
                "never match",
                name,
            )
        out.append(
            {
                "name": name,
                "days": days,
                "day_labels": [_WEEKDAY_LABELS[d] for d in sorted(days)],
                "start": _clock_text(start),
                "end": _clock_text(end),
                "start_minute": start,
                "end_minute": end,
                # end <= start is an OVERNIGHT shift, and wraps onto the next
                # calendar day. A Fri 22:00-06:00 window covers Saturday 05:00.
                "overnight": end <= start,
                "note": str(entry.get("note") or "").strip(),
                "hours_per_week": round(
                    len(days)
                    * (
                        (end - start)
                        if end > start
                        else (_MINUTES_PER_DAY - start + end)
                    )
                    / 60.0,
                    2,
                ),
                "label": (
                    f"{_days_text(days)} {_clock_text(start)}-{_clock_text(end)}"
                ),
            }
        )
    return out


def _in_window(window: Mapping[str, Any], weekday: int, minute: int) -> bool:
    days = window["days"]
    if not days:
        return False
    start = int(window["start_minute"])
    end = int(window["end_minute"])
    if not window["overnight"]:
        # Half-open [start, end): two windows meeting at 18:00 neither overlap
        # nor leave a gap.
        return weekday in days and start <= minute < end
    # Overnight: the tail of the named day, plus the morning of the day after.
    if weekday in days and minute >= start:
        return True
    return (weekday - 1) % 7 in days and minute < end


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _local_moment(row: Any) -> tuple[int, int] | None:
    """``(weekday, minutes since local midnight)`` for one offer, or None.

    Reads ``offered_at_local`` first -- a row view already carries it, in local
    time, converted once by ingestion and metrics. An ORM row carries only naive
    UTC in ``offered_at``, so that is converted here through the same helper
    metrics uses; two modules must never disagree about what "Tuesday" means.
    """
    local = _as_datetime(_field(row, "offered_at_local"))
    if local is None:
        stored = _as_datetime(_field(row, "offered_at", "offered_at_utc"))
        if stored is not None:
            local = _metrics.to_local(stored.replace(tzinfo=None))
    if local is not None:
        return (local.weekday(), local.hour * 60 + local.minute)

    # Hand-built fixtures and older views may carry only the derived parts.
    weekday = _field(row, "local_weekday")
    hour = _field(row, "local_hour")
    if weekday is None and hour is None:
        return None
    try:
        return (int(weekday or 0) % 7, (int(hour or 0) % 24) * 60)
    except (TypeError, ValueError):
        return None


# ==========================================================================
# Territory -- "was this even our job to take?"
#
# The second half of the same question coverage asks. Coverage asks whether
# anybody was at a desk; territory asks whether the job was inside the area
# this company works at all. An offer that fails either test is not a miss the
# owner should be judged on, and counting it as one is what makes an honest
# acceptance rate look bad.
#
# Keyed on ZIP, from ``rules.yaml -> missed_work.territory``; there is no
# geography in this code. Bands are ordered nearest-first and are reported
# separately, because "we take 40% of the core and 9% of the edge" is a real
# operating fact and flattening the two hides it.
# ==========================================================================


def _zip_key(value: Any) -> str:
    """Normalise anything ZIP-shaped to a comparable 5-character key.

    YAML turns an unquoted 43201 into an int and an unquoted 01234 into 1234
    (or, worse, into octal on some loaders), and the portal writes ZIP+4 as
    "43201-1234". All of those have to land on the same key, or a tenant in
    Massachusetts silently has no territory at all.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("-", 1)[0].strip()
    if not text.isdigit():
        return ""
    return text.zfill(5)[:5]


def _territory_config(rules: Mapping[str, Any]) -> Mapping[str, Any]:
    block = (rules.get("missed_work") or {}).get("territory")
    return block if isinstance(block, Mapping) else {}


def _territory_default_label(rules: Mapping[str, Any]) -> str:
    return str(_territory_config(rules).get("default_label") or OUTSIDE_TERRITORY_LABEL)


def _territory_bands(rules: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``[{name, zips: frozenset}]``, nearest band first.

    A ZIP listed in more than one band belongs to the FIRST that names it, so
    the ordering in config is the tie-break and moving a ZIP inward is a
    one-line edit.
    """
    raw = _territory_config(rules).get("bands")
    if isinstance(raw, Mapping):
        raw = [{**entry, "name": name} for name, entry in raw.items()
               if isinstance(entry, Mapping)]
    if not isinstance(raw, (list, tuple)):
        return []

    default_label = _territory_default_label(rules)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or f"band_{position + 1}").strip()
        if name == default_label:
            logger.warning(
                "missed_work.territory: band %r uses the default_label; renaming it "
                "to %r so in and out stay distinguishable",
                name,
                f"{name}_band",
            )
            name = f"{name}_band"
        zips: set[str] = set()
        for item in entry.get("zips") or ():
            key = _zip_key(item)
            if not key:
                logger.warning("missed_work.territory: %r is not a ZIP; ignoring", item)
                continue
            if key in seen:
                continue  # first band wins
            seen.add(key)
            zips.add(key)
        if not zips:
            logger.warning(
                "missed_work.territory: band %r lists no usable ZIP; it will never match",
                name,
            )
        out.append({"name": name, "zips": frozenset(zips)})
    return out


def territory_band(row: Any, rules: Mapping[str, Any] | None = None) -> str:
    """Which service-area band this offer's pickup falls in, or the outside label.

    Reads ``pickup_zip`` -- the ZIP the API states as its own field -- and falls
    back to the trailing ZIP in ``pickup_location`` for CSV-ingested rows, which
    carry no ZIP field at all.

    A row whose ZIP cannot be read returns the IN-TERRITORY default rather than
    the outside label. That direction is deliberate: calling an unreadable row
    "out of territory" would quietly delete it from the missed-work inventory,
    and hiding work is the one failure this model must not have. Unknown
    territory is reported as unknown, and counted.
    """
    resolved = _rules(rules)
    bands = _territory_bands(resolved)
    if not bands:
        # No territory configured -- every offer is in territory, which is the
        # behaviour every install had before this block existed.
        return UNKNOWN_TERRITORY_LABEL

    key = _zip_key(_field(row, "pickup_zip"))
    if not key:
        text = str(_field(row, "pickup_location") or "")
        found = _ZIP_IN_TEXT.findall(text)
        key = _zip_key(found[-1]) if found else ""
    if not key:
        return UNKNOWN_TERRITORY_LABEL

    for band in bands:
        if key in band["zips"]:
            return str(band["name"])
    return _territory_default_label(resolved)


def in_territory(row: Any, rules: Mapping[str, Any] | None = None) -> bool:
    """Was this job inside the area this company works?

    Unknown counts as IN, for the reason in :func:`territory_band`: an offer we
    cannot place must not vanish from the inventory.
    """
    band = territory_band(row, rules)
    return band != _territory_default_label(_rules(rules))


def coverage_label(row: Any, rules: Mapping[str, Any] | None = None) -> str:
    """Which operating window this offer arrived in, or the uncovered label.

    Returns the ``name`` of the first window in
    ``rules.yaml -> missed_work.coverage.windows`` that contains the offer's
    LOCAL arrival time, or ``coverage.default_label`` (shipped as ``uncovered``)
    when it falls in none -- which is to say, when nobody was rostered to see it.

    ``row`` may be a view dict or an ORM ``Request``. Pure: same row and same
    rules, same label, always. A row with no readable timestamp gets the default
    label rather than being dropped, because an offer whose arrival time we
    cannot read is certainly not one we can claim was staffed.
    """
    resolved = _rules(rules)
    default = _coverage_default_label(resolved)
    moment = _local_moment(row)
    if moment is None:
        return default
    weekday, minute = moment
    for window in _coverage_windows(resolved):
        if _in_window(window, weekday, minute):
            return str(window["name"])
    return default


# ==========================================================================
# Row views
# ==========================================================================


def _view(row: Any, rules: Mapping[str, Any], default_class: str) -> dict[str, Any]:
    """metrics' row view, plus the bucket / cause / remedy this module adds."""
    view = _metrics._row_view(row, rules, default_class)

    # metrics renders a NULL status as "pending" -- a sensible DISPLAY default,
    # and a dangerous one to bucket on. schema.yaml's `unknown_status_action:
    # ignore` stores NULL precisely for a status it could not read, and bucketing
    # that as in_flight would quietly report an unreadable row as "still
    # deciding" forever. Restore the stored value so a NULL falls through to
    # bucket_default and is reported.
    view["status"] = (getattr(row, "status", None) or "").strip().casefold()

    bucket, source = _bucket_with_source(view, rules)
    cause, remedy = attribute_cause(view, rules)
    view["bucket"] = bucket
    view["bucket_source"] = source
    view["cause"] = cause
    view["remedy_class"] = remedy
    # EVERY row-level classification carries the operating window it arrived
    # in. It is the dimension the outcome actually splits on, so it belongs
    # beside the bucket rather than being re-derived by each consumer -- and it
    # is what the `coverage_gap` alert fires on.
    view["coverage_label"] = coverage_label(view, rules)
    return view


def _load_views(
    session: Session,
    start_utc: datetime,
    end_utc: datetime,
    rules: Mapping[str, Any],
    company_id: str | None,
) -> list[dict[str, Any]]:
    """Every row this company was offered in the window, as bucketed views.

    The company filter lives in ``metrics._load_rows`` and is not optional
    there: ``company_id=None`` resolves to the default company, never to "all
    companies". Every public function in this module loads through here, so
    there is exactly one place a tenant filter could go missing and it does
    not.
    """
    _, default_class = _metrics._service_class_order(rules)
    rows = _metrics._load_rows(session, start_utc, end_utc, company_id)
    return [_view(row, rules, default_class) for row in rows]


def _window(
    window_start: Any, window_end: Any, period_type: str | None
) -> dict[str, Any]:
    """Normalise a local window and describe it in both clocks."""
    start_local = _metrics.parse_local_datetime(window_start)
    end_local = _metrics.parse_local_datetime(window_end)
    if end_local < start_local:
        start_local, end_local = end_local, start_local

    start_utc = _metrics.to_utc(start_local)
    end_utc = _metrics.to_utc(end_local)
    hours = (end_utc - start_utc).total_seconds() / 3600.0

    if not period_type:
        # Derive from the span, so an ad-hoc call still lands in a sensible
        # partition instead of colliding with the scheduled runs.
        if hours <= 1.01:
            period_type = "hourly"
        elif 22.0 <= hours <= 26.0:
            period_type = "daily"
        elif 6.9 * 24 <= hours <= 7.1 * 24:
            period_type = "weekly"
        else:
            period_type = "custom"

    return {
        "period_type": period_type,
        "timezone": _metrics.local_timezone_name(),
        "start": start_local.isoformat(sep="T"),
        "end": end_local.isoformat(sep="T"),
        "start_utc": start_utc.isoformat(sep="T"),
        "end_utc": end_utc.isoformat(sep="T"),
        "hours": round(hours, 2),
        "days": round(hours / 24.0, 3),
        "_start_local": start_local,
        "_end_local": end_local,
        "_start_utc": start_utc,
        "_end_utc": end_utc,
    }


def _public_window(window: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in window.items() if not key.startswith("_")}


# ==========================================================================
# Pricing (off until a human fills in job_value_by_client)
# ==========================================================================


def _price_book(rules: Mapping[str, Any]) -> dict[str, float]:
    """``client_key -> average job value``, empty unless a human filled it in.

    offerAmount arrives empty on every record, so there is no way to derive
    this from the feed. Anything that cannot be parsed as a positive number is
    dropped with a warning rather than coerced -- a typo'd price silently
    becoming 0 would understate the very number it was added to produce.
    """
    book: dict[str, float] = {}
    for key, value in _mapping(rules, "job_value_by_client").items():
        try:
            amount = float(value)
        except (TypeError, ValueError):
            logger.warning(
                "missed_work.job_value_by_client[%r] is not a number (%r); ignoring",
                key,
                value,
            )
            continue
        if amount <= 0:
            logger.warning(
                "missed_work.job_value_by_client[%r] is %s; ignoring", key, amount
            )
            continue
        book[_fold(key)] = amount
    return book


def _priced_classes(rules: Mapping[str, Any]) -> frozenset[str] | None:
    """Service classes the price book may be applied to, or None for all.

    ``job_value_by_client`` holds average TOW values. Applying them to a missed
    Tire Change would price a job the owner never wanted at a tow's worth and
    inflate the one figure a reader is most likely to quote out loud, so the
    default restricts them to ``acceptance_policy.should_accept``.
    """
    setting = str(_block(rules).get("job_value_applies_to") or "should_accept").strip()
    if _fold(setting) == "all":
        return None
    wanted, _ = _metrics._policy_classes(rules)
    return frozenset(wanted)


def _valuation(
    views: Sequence[Mapping[str, Any]],
    book: Mapping[str, float],
    priced_classes: frozenset[str] | None,
) -> dict[str, Any]:
    """Estimated value of a set of missed rows, and how much of it is priced.

    A row contributes only when its client has a configured value AND its
    service class is one the price book applies to. Everything else contributes
    NOTHING and is counted in ``unpriced_missed``, so a partial price list
    understates visibly instead of inventing a per-job default.
    """
    total = 0.0
    priced = 0
    for view in views:
        if priced_classes is not None and view.get("service_class") not in priced_classes:
            continue
        value = book.get(_fold(view.get("client_key")))
        if value is None:
            continue
        total += value
        priced += 1
    return {
        "estimated_value": round(total, 2),
        "priced_missed": priced,
        "unpriced_missed": len(views) - priced,
        "value_basis": "gross average job value per client, from "
        "missed_work.job_value_by_client -- not margin",
    }


# ==========================================================================
# Section 4 -- blind spots
# ==========================================================================


def _blind_spots_from(
    views: Sequence[Mapping[str, Any]], rules: Mapping[str, Any]
) -> dict[str, Any]:
    cfg = _cfg(rules, "blind_spot")
    min_offers = max(0, int(cfg["min_offers"]))
    threshold = float(cfg["threshold"])

    offers = [[0] * 24 for _ in range(7)]
    accepted = [[0] * 24 for _ in range(7)]
    unanswered = [[0] * 24 for _ in range(7)]

    for view in views:
        weekday = int(view.get("local_weekday") or 0) % 7
        hour = int(view.get("local_hour") or 0) % 24
        offers[weekday][hour] += 1
        if view.get("bucket") == "won":
            accepted[weekday][hour] += 1
        elif view.get("bucket") == "no_response":
            unanswered[weekday][hour] += 1

    rates = [
        [_rate(unanswered[d][h], offers[d][h]) for h in range(24)] for d in range(7)
    ]

    cells: list[dict[str, Any]] = []
    for day in range(7):
        for hour in range(24):
            if not offers[day][hour]:
                continue
            rate = rates[day][hour]
            cells.append(
                {
                    "weekday_index": day,
                    "weekday": _WEEKDAY_LABELS[day],
                    "hour": hour,
                    "label": f"{_WEEKDAY_LABELS[day]} {hour:02d}:00",
                    "offers": offers[day][hour],
                    "accepted": accepted[day][hour],
                    "no_response": unanswered[day][hour],
                    "no_response_rate": rate,
                    "is_blind_spot": bool(
                        offers[day][hour] >= min_offers
                        and rate is not None
                        and rate >= threshold
                    ),
                }
            )

    flagged = [cell for cell in cells if cell["is_blind_spot"]]
    # Worst first: most jobs actually lost, then the rate, then a stable
    # tiebreak so a recompute writes the same list.
    flagged.sort(
        key=lambda c: (
            -c["no_response"],
            -(c["no_response_rate"] or 0.0),
            c["weekday_index"],
            c["hour"],
        )
    )

    by_hour: list[dict[str, Any]] = []
    for hour in range(24):
        hour_offers = sum(offers[day][hour] for day in range(7))
        hour_unanswered = sum(unanswered[day][hour] for day in range(7))
        hour_rate = _rate(hour_unanswered, hour_offers)
        by_hour.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "offers": hour_offers,
                "accepted": sum(accepted[day][hour] for day in range(7)),
                "no_response": hour_unanswered,
                "no_response_rate": hour_rate,
                "is_blind_spot": bool(
                    hour_offers >= min_offers
                    and hour_rate is not None
                    and hour_rate >= threshold
                ),
            }
        )

    total_offers = sum(sum(row) for row in offers)
    total_unanswered = sum(sum(row) for row in unanswered)

    return {
        "rows": 7,
        "cols": 24,
        "weekdays": list(_WEEKDAY_LABELS),
        "hours": list(range(24)),
        "offers": offers,
        "accepted": accepted,
        "no_response": unanswered,
        "no_response_rate": rates,
        "cells": cells,
        "blind_spots": flagged,
        "blind_spot_count": len(flagged),
        "blind_spot_offers": sum(cell["offers"] for cell in flagged),
        "blind_spot_no_response": sum(cell["no_response"] for cell in flagged),
        "by_hour": by_hour,
        "worst_hours": [entry for entry in by_hour if entry["is_blind_spot"]],
        "totals": {
            "offers": total_offers,
            "no_response": total_unanswered,
            "no_response_rate": _rate(total_unanswered, total_offers),
        },
        "thresholds": {"min_offers": min_offers, "threshold": threshold},
        # Context that makes the grid urgent rather than interesting. Verified
        # live: median 3 minutes, mean 4, max 15.
        "response_window_note": (
            "The median response window Towbook allows is 3 minutes. A missed "
            "notification is a lost job almost immediately."
        ),
    }


def blind_spots(
    session: Session | None,
    window_start: Any,
    window_end: Any,
    rules: Mapping[str, Any] | None = None,
    *,
    company_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """The 7 x 24 hour-of-week grid of when offers go unanswered (section 4).

    A cell is a blind spot when ``offers >= min_offers`` **and**
    ``no_response_rate >= threshold``, both from
    ``rules.yaml -> missed_work.blind_spot``. ``min_offers`` is what stops a
    1-of-2 hour on a Tuesday night reading as a crisis.

    This is the highest-value output in the system: it turns "we are missing
    work" into "nobody is covering 17:00-21:00 and 01:00-04:00", which is a
    staffing conversation with evidence attached.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company(company_id, account_id) as company:
        resolved = _rules(rules, company)
        window = _window(window_start, window_end, None)

        def work(sess: Session) -> dict[str, Any]:
            views = _load_views(
                sess, window["_start_utc"], window["_end_utc"], resolved, company
            )
            document = _blind_spots_from(views, resolved)
            # NOTE no company_id here. This is a FRAGMENT of the missed-work
            # document, and compute_missed_work() embeds an identical copy of
            # it. Two tests assert the standalone and the embedded copies are
            # the same object; a key on one and not the other would make the
            # dashboard and the report disagree in a way nobody would notice.
            # The company is on the enclosing document, and on the request.
            document["window"] = _public_window(window)
            return document

        if session is not None:
            return work(session)
        with get_session(commit=False) as owned:
            return work(owned)


# ==========================================================================
# Section 5 -- close-off candidates
# ==========================================================================


def _closeoff_from(
    views: Sequence[Mapping[str, Any]], rules: Mapping[str, Any]
) -> dict[str, Any]:
    cfg = _cfg(rules, "closeoff")
    min_offers = max(1, int(cfg["min_offers"]))
    max_rate = float(cfg["max_rate"])
    exclude_wanted = bool(cfg["exclude_wanted_classes"])

    wanted, _ = _metrics._policy_classes(rules)
    wanted_set = set(wanted) if exclude_wanted else set()

    considered = [
        view for view in views if view.get("service_class") not in wanted_set
    ]

    # A candidate is a SERVICE TYPE, measured across the whole window, not per
    # client -- an 0-of-3 from one client and an 0-of-140 from another are the
    # same service type and the same decision.
    totals: dict[str, dict[str, Any]] = {}
    for view in considered:
        raw = str(view.get("service_type_raw") or "").strip()
        if not raw:
            continue
        entry = totals.setdefault(
            raw,
            {
                "service_type_raw": raw,
                "service_class": view.get("service_class"),
                "offers": 0,
                "accepted": 0,
                "no_response": 0,
            },
        )
        entry["offers"] += 1
        if view.get("bucket") == "won":
            entry["accepted"] += 1
        elif view.get("bucket") == "no_response":
            entry["no_response"] += 1

    candidates = {
        raw: entry
        for raw, entry in totals.items()
        if entry["offers"] >= min_offers
        and (_rate(entry["accepted"], entry["offers"]) or 0.0) <= max_rate
    }
    for entry in candidates.values():
        entry["rate"] = _rate(entry["accepted"], entry["offers"])

    # Group BY CLIENT, because the action is a conversation with that client:
    # "stop sending us these".
    client_offers: Counter[str] = Counter()
    client_names: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for view in views:
        key = str(view.get("client_key") or "")
        client_offers[key] += 1
        client_names[key].append(view)

    per_client: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for view in considered:
        raw = str(view.get("service_type_raw") or "").strip()
        if raw not in candidates:
            continue
        key = str(view.get("client_key") or "")
        entry = per_client[key].setdefault(
            raw,
            {
                "service_type_raw": raw,
                "service_class": view.get("service_class"),
                "offers": 0,
                "accepted": 0,
                "no_response": 0,
            },
        )
        entry["offers"] += 1
        if view.get("bucket") == "won":
            entry["accepted"] += 1
        elif view.get("bucket") == "no_response":
            entry["no_response"] += 1

    clients: list[dict[str, Any]] = []
    for key, by_type in per_client.items():
        rows = sorted(
            by_type.values(),
            key=lambda item: (-item["offers"], item["service_type_raw"]),
        )
        offered_here = client_offers.get(key, 0)
        for row in rows:
            row["rate"] = _rate(row["accepted"], row["offers"])
            row["share_of_client_offers"] = _rate(row["offers"], offered_here)
        candidate_offers = sum(row["offers"] for row in rows)
        candidate_accepted = sum(row["accepted"] for row in rows)
        clients.append(
            {
                "client": _metrics._display_name(client_names.get(key, []), key),
                "client_key": key,
                "client_offers": offered_here,
                "candidate_offers": candidate_offers,
                "candidate_accepted": candidate_accepted,
                "candidate_rate": _rate(candidate_accepted, candidate_offers),
                "candidate_share_of_client_offers": _rate(
                    candidate_offers, offered_here
                ),
                "service_types": rows,
            }
        )
    clients.sort(key=lambda item: (-item["candidate_offers"], item["client_key"]))

    by_service_type = sorted(
        candidates.values(),
        key=lambda item: (-item["offers"], item["service_type_raw"]),
    )

    total_offers = sum(entry["offers"] for entry in by_service_type)
    total_accepted = sum(entry["accepted"] for entry in by_service_type)

    # The before/after number. Closing these off is expected to IMPROVE the tow
    # response rate by removing competing offers from the same 3-minute decision
    # window, so the report records what that rate was when the recommendation
    # was made and the next period can be compared against it.
    wanted_views = [view for view in views if view.get("service_class") in set(wanted)]
    wanted_unanswered = sum(
        1 for view in wanted_views if view.get("bucket") == "no_response"
    )

    return {
        "thresholds": {
            "min_offers": min_offers,
            "max_rate": max_rate,
            "exclude_wanted_classes": exclude_wanted,
        },
        "excluded_service_classes": sorted(wanted_set),
        "clients": clients,
        "by_service_type": by_service_type,
        "totals": {
            "offers": total_offers,
            "accepted": total_accepted,
            "rate": _rate(total_accepted, total_offers),
            "service_types": len(by_service_type),
            "clients": len(clients),
        },
        "wanted_baseline": {
            "service_classes": sorted(set(wanted)),
            "offers": len(wanted_views),
            "no_response": wanted_unanswered,
            "no_response_rate": _rate(wanted_unanswered, len(wanted_views)),
        },
        "rationale": (
            "These service types are refused almost every time they are offered, "
            "yet they still consume attention inside the same 3-minute decision "
            "window the tow offers are competing for. Closing them off is not "
            "only noise reduction: it is expected to improve the response rate "
            "on work we do want. wanted_baseline records that rate now so the "
            "next period can show whether it held."
        ),
        "ranking_basis": "job_count",
    }


def closeoff_candidates(
    session: Session | None,
    window_start: Any,
    window_end: Any,
    rules: Mapping[str, Any] | None = None,
    *,
    company_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Work we do not want, being offered anyway -- grouped by client (section 5).

    A service type qualifies when ``offers >= min_offers`` and
    ``acceptance_rate <= max_rate`` over the window. Service classes named in
    ``acceptance_policy.should_accept`` are excluded, because the inventory is
    about work we want and this is its mirror image -- without that filter the
    report would end up advising the owner to ask a client to stop sending tows.

    Output is grouped by client because the action is a conversation with that
    client, and each row carries the share of that client's total offers it
    represents so the ask can be sized.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company(company_id, account_id) as company:
        resolved = _rules(rules, company)
        window = _window(window_start, window_end, None)

        def work(sess: Session) -> dict[str, Any]:
            views = _load_views(
                sess, window["_start_utc"], window["_end_utc"], resolved, company
            )
            document = _closeoff_from(views, resolved)
            # NOTE no company_id here. This is a FRAGMENT of the missed-work
            # document, and compute_missed_work() embeds an identical copy of
            # it. Two tests assert the standalone and the embedded copies are
            # the same object; a key on one and not the other would make the
            # dashboard and the report disagree in a way nobody would notice.
            # The company is on the enclosing document, and on the request.
            document["window"] = _public_window(window)
            return document

        if session is not None:
            return work(session)
        with get_session(commit=False) as owned:
            return work(owned)


# ==========================================================================
# Section 6 -- client comparison
# ==========================================================================


def _client_comparison_from(
    views: Sequence[Mapping[str, Any]], rules: Mapping[str, Any]
) -> dict[str, Any]:
    cfg = _cfg(rules, "client_comparison")
    min_offers = max(1, int(cfg["min_offers"]))
    gap_pp = float(cfg["gap_pp"])

    missed_buckets = _name_set(rules, "missed_buckets")
    recoverable_buckets = _name_set(rules, "recoverable_buckets")

    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for view in views:
        groups[str(view.get("client_key") or "")].append(view)

    clients: list[dict[str, Any]] = []
    for key, group in groups.items():
        counts = Counter(str(view.get("bucket") or "") for view in group)
        offers = len(group)
        accepted = counts.get("won", 0)
        no_response = counts.get("no_response", 0)
        missed = sum(counts.get(bucket, 0) for bucket in missed_buckets)
        recoverable = sum(counts.get(bucket, 0) for bucket in recoverable_buckets)
        clients.append(
            {
                "client": _metrics._display_name(group, key),
                "client_key": key,
                "offers": offers,
                "accepted": accepted,
                "rate": _rate(accepted, offers),
                "declined": counts.get("declined", 0),
                "no_response": no_response,
                "withdrew": counts.get("client_withdrew", 0),
                "in_flight": counts.get("in_flight", 0),
                "accept_failed": counts.get("accept_failed", 0),
                "missed": missed,
                "recoverable": recoverable,
                "no_response_rate": _rate(no_response, offers),
                "decline_rate": _rate(counts.get("declined", 0), offers),
                "missed_rate": _rate(missed, offers),
            }
        )
    clients.sort(key=lambda item: (-item["offers"], item["client_key"]))

    # THE FINDING. The point of this table is not the table -- it is the gap
    # between clients that should behave alike. Same trucks, same staff, same
    # three-minute window: a 34-point spread in how often offers go unanswered
    # is a process or integration difference, and it is stated outright rather
    # than left for a reader to notice.
    findings: list[dict[str, Any]] = []
    eligible = [
        entry
        for entry in clients
        if entry["offers"] >= min_offers and entry["no_response_rate"] is not None
    ]
    if len(eligible) >= 2:

        def _side(entry: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "client": entry["client"],
                "client_key": entry["client_key"],
                "offers": entry["offers"],
                "no_response": entry["no_response"],
                "no_response_rate": entry["no_response_rate"],
            }

        best = min(eligible, key=lambda e: (e["no_response_rate"], -e["offers"]))

        # TWO findings, because "worst" has two honest meanings and picking one
        # buries the other. Ranking by JOBS LOST names the client costing the
        # most work; ranking by RATE names the client whose process is most
        # broken. On real data those are different clients and both are worth
        # saying out loud.
        candidates = [
            (
                "no_response_gap",
                "high",
                max(eligible, key=lambda e: (e["no_response"], e["no_response_rate"])),
                "jobs lost",
            ),
            (
                "no_response_rate_outlier",
                "medium",
                max(eligible, key=lambda e: (e["no_response_rate"], e["no_response"])),
                "miss rate",
            ),
        ]

        seen: set[str] = set()
        for kind, severity, worst, ranked_by in candidates:
            if worst["client_key"] == best["client_key"]:
                continue
            if worst["client_key"] in seen:
                continue
            spread = (worst["no_response_rate"] - best["no_response_rate"]) * 100.0
            if spread < gap_pp:
                continue
            seen.add(worst["client_key"])
            findings.append(
                {
                    "kind": kind,
                    "severity": severity,
                    "ranked_by": ranked_by,
                    "spread_pp": round(spread, 2),
                    "worst": _side(worst),
                    "best": _side(best),
                    "summary": (
                        f"{worst['client']} offers go unanswered "
                        f"{_metrics.format_rate(worst['no_response_rate'], 1)} of the "
                        f"time ({worst['no_response']} of {worst['offers']}) while "
                        f"{best['client']} offers go unanswered "
                        f"{_metrics.format_rate(best['no_response_rate'], 1)} "
                        f"({best['no_response']} of {best['offers']}). Same trucks, "
                        f"same staff, same three-minute window. A gap that large is a "
                        f"process or integration difference, not a capacity one."
                    ),
                }
            )

    return {
        "clients": clients,
        "findings": findings,
        "thresholds": {"min_offers": min_offers, "gap_pp": gap_pp},
        "ranking_basis": "job_count",
    }


def client_comparison(
    session: Session | None,
    window_start: Any,
    window_end: Any,
    rules: Mapping[str, Any] | None = None,
    *,
    company_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Per-client buckets, and the gap between clients that should match (section 6).

    Returns one row per client -- offers, accepted, rate, declined, no_response,
    withdrew, no_response_rate -- plus ``findings``: an explicit statement when
    the spread in ``no_response_rate`` between two clients big enough to matter
    exceeds ``client_comparison.gap_pp``.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company(company_id, account_id) as company:
        resolved = _rules(rules, company)
        window = _window(window_start, window_end, None)

        def work(sess: Session) -> dict[str, Any]:
            views = _load_views(
                sess, window["_start_utc"], window["_end_utc"], resolved, company
            )
            document = _client_comparison_from(views, resolved)
            # NOTE no company_id here. This is a FRAGMENT of the missed-work
            # document, and compute_missed_work() embeds an identical copy of
            # it. Two tests assert the standalone and the embedded copies are
            # the same object; a key on one and not the other would make the
            # dashboard and the report disagree in a way nobody would notice.
            # The company is on the enclosing document, and on the request.
            document["window"] = _public_window(window)
            return document

        if session is not None:
            return work(session)
        with get_session(commit=False) as owned:
            return work(owned)


# ==========================================================================
# Section 7 -- coverage analysis
# ==========================================================================


def _coverage_entry(
    label: str,
    group: Sequence[Mapping[str, Any]],
    rules: Mapping[str, Any],
    book: Mapping[str, float],
    priced_classes: frozenset[str] | None,
    *,
    is_covered: bool,
    definition: Mapping[str, Any] | None,
    total_offers: int,
    total_missed: int,
) -> dict[str, Any]:
    """One window's row: offers, answered, lost, and what the loss was worth."""
    missed_buckets = _name_set(rules, "missed_buckets")
    recoverable_buckets = _name_set(rules, "recoverable_buckets")
    if _block(rules).get("count_client_withdrew_as_recoverable"):
        recoverable_buckets = recoverable_buckets | {"client_withdrew"}

    counts = Counter(str(view.get("bucket") or "") for view in group)
    offers = len(group)
    accepted = counts.get("won", 0)
    declined = counts.get("declined", 0)
    no_response = counts.get("no_response", 0)
    missed_views = [view for view in group if view.get("bucket") in missed_buckets]
    recoverable_views = [
        view for view in group if view.get("bucket") in recoverable_buckets
    ]

    entry: dict[str, Any] = {
        "label": label,
        "is_covered": is_covered,
        "definition": dict(definition) if definition else None,
        "offers": offers,
        "accepted": accepted,
        "declined": declined,
        "no_response": no_response,
        "no_response_rate": _rate(no_response, offers),
        # ANSWERED is "a human got to it": everything that did not expire
        # unanswered and is not still in flight. It is the number the whole
        # coverage argument turns on, because 99.1% of accepted rows carry a
        # named responder -- no person, no answer.
        "answered": offers - no_response - counts.get("in_flight", 0),
        "in_flight": counts.get("in_flight", 0),
        "accept_failed": counts.get("accept_failed", 0),
        "client_withdrew": counts.get("client_withdrew", 0),
        "missed": len(missed_views),
        "recoverable": len(recoverable_views),
        "acceptance_rate": _rate(accepted, offers),
        "share_of_offers": _rate(offers, total_offers),
        "share_of_missed": _rate(len(missed_views), total_missed),
    }
    entry["answer_rate"] = _rate(entry["answered"], offers)

    # missed_value is the money question: what was the work in this window
    # worth. It is 0.0 and flagged unavailable until a human supplies per-client
    # job values -- offerAmount is empty on 100% of records, so nothing here is
    # ever derived from the feed.
    if book:
        valuation = _valuation(missed_views, book, priced_classes)
        entry["missed_value"] = valuation["estimated_value"]
        entry["priced_missed"] = valuation["priced_missed"]
        entry["unpriced_missed"] = valuation["unpriced_missed"]
        entry["value_basis"] = valuation["value_basis"]
        entry["recoverable_value"] = _valuation(
            recoverable_views, book, priced_classes
        )["estimated_value"]
        entry["no_response_value"] = _valuation(
            [v for v in group if v.get("bucket") == "no_response"],
            book,
            priced_classes,
        )["estimated_value"]
        entry["value_available"] = True
    else:
        entry["missed_value"] = 0.0
        entry["recoverable_value"] = 0.0
        entry["no_response_value"] = 0.0
        entry["priced_missed"] = 0
        entry["unpriced_missed"] = len(missed_views)
        entry["value_basis"] = (
            "No per-client job values are configured, so no value is claimed."
        )
        entry["value_available"] = False
    return entry


def _coverage_from(
    views: Sequence[Mapping[str, Any]],
    rules: Mapping[str, Any],
    book: Mapping[str, float] | None = None,
    priced_classes: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Inside vs outside the staffed window, and the contrast between them.

    The uncovered row is the headline because that is where essentially all of
    the recoverable work sits. The covered row stays beside it because a single
    number cannot make the argument -- 61.7% only means something next to 5.8%.
    """
    book = book or {}
    windows = _coverage_windows(rules)
    default_label = _coverage_default_label(rules)
    missed_buckets = _name_set(rules, "missed_buckets")

    definitions: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for window in windows:
        name = str(window["name"])
        if name not in definitions:
            definitions[name] = dict(window)
            order.append(name)
    if default_label not in order:
        order.append(default_label)

    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for view in views:
        label = str(view.get("coverage_label") or "").strip()
        if not label:
            label = coverage_label(view, rules)
        groups[label].append(view)

    # Any label the config no longer declares still gets a row -- a window
    # renamed mid-window would otherwise drop its offers off the report.
    for label in sorted(groups):
        if label not in order:
            order.append(label)

    total_offers = len(views)
    total_missed = sum(1 for v in views if v.get("bucket") in missed_buckets)

    entries = [
        _coverage_entry(
            label,
            groups.get(label, []),
            rules,
            book,
            priced_classes,
            is_covered=label != default_label,
            definition=definitions.get(label),
            total_offers=total_offers,
            total_missed=total_missed,
        )
        for label in order
    ]
    by_label = {entry["label"]: entry for entry in entries}

    outside = by_label.get(default_label) or _coverage_entry(
        default_label,
        [],
        rules,
        book,
        priced_classes,
        is_covered=False,
        definition=None,
        total_offers=total_offers,
        total_missed=total_missed,
    )
    covered_entries = [entry for entry in entries if entry["is_covered"]]

    def _sum(key: str) -> Any:
        values = [entry[key] for entry in covered_entries]
        if not values:
            return 0
        return round(sum(values), 2) if isinstance(values[0], float) else sum(values)

    inside = {
        "labels": [entry["label"] for entry in covered_entries],
        "offers": _sum("offers"),
        "accepted": _sum("accepted"),
        "declined": _sum("declined"),
        "no_response": _sum("no_response"),
        "answered": _sum("answered"),
        "missed": _sum("missed"),
        "recoverable": _sum("recoverable"),
        "missed_value": _sum("missed_value"),
        "recoverable_value": _sum("recoverable_value"),
        "no_response_value": _sum("no_response_value"),
    }
    inside["no_response_rate"] = _rate(inside["no_response"], inside["offers"])
    inside["acceptance_rate"] = _rate(inside["accepted"], inside["offers"])
    inside["answer_rate"] = _rate(inside["answered"], inside["offers"])

    # How many times worse the uncovered window is. None rather than a
    # divide-by-zero or a fake 0 when either side has no offers.
    multiple: float | None = None
    if inside["no_response_rate"] and outside["no_response_rate"] is not None:
        multiple = round(outside["no_response_rate"] / inside["no_response_rate"], 2)
    spread_pp: float | None = None
    if inside["no_response_rate"] is not None and outside["no_response_rate"] is not None:
        spread_pp = round(
            (outside["no_response_rate"] - inside["no_response_rate"]) * 100.0, 2
        )

    summary = (
        f"{outside['no_response']:,} of {outside['offers']:,} offers outside "
        f"{default_label.replace('_', ' ')} hours went unanswered "
        f"({_metrics.format_rate(outside['no_response_rate'], 1)}), against "
        f"{inside['no_response']:,} of {inside['offers']:,} inside them "
        f"({_metrics.format_rate(inside['no_response_rate'], 1)})."
        if entries
        else "No offers in this window."
    )

    return {
        "default_label": default_label,
        "labels": [entry["label"] for entry in entries],
        "windows": entries,
        "by_label": by_label,
        "definitions": [definitions[name] for name in order if name in definitions],
        # THE HEADLINE. Outside-hours missed work is the recoverable figure the
        # reports lead with, because that is where essentially all of it sits.
        "headline": {
            "label": default_label,
            "offers": outside["offers"],
            "answered": outside["answered"],
            "lost": outside["no_response"],
            "no_response_rate": outside["no_response_rate"],
            "missed": outside["missed"],
            "missed_value": outside["missed_value"],
            "recoverable": outside["recoverable"],
            "recoverable_value": outside["recoverable_value"],
            "value_available": outside["value_available"],
        },
        "outside": outside,
        "inside": inside,
        "contrast": {
            "spread_pp": spread_pp,
            "multiple": multiple,
            "summary": summary,
        },
        "finding": (
            "This is a coverage problem, not a truck problem. The same trucks "
            "were idle in both windows and 99.1% of accepted offers carry a "
            "named human responder, so an offer that arrives when nobody is "
            "rostered is lost by default. The fix is a person on the queue in "
            "the uncovered window, not another truck."
        ),
        "note": (
            "Windows come from rules.yaml -> missed_work.coverage: the owner's "
            "stated staffed hours, verified against real traffic. Adding a "
            "Saturday shift is an edit to that block."
        ),
        "ranking_basis": "job_count",
    }


def coverage_analysis(
    session: Session | None,
    window_start: Any,
    window_end: Any,
    rules: Mapping[str, Any] | None = None,
    *,
    company_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Offers, answers and losses per operating window (section 7).

    One row per window declared in ``rules.yaml -> missed_work.coverage.windows``
    plus one for ``default_label`` -- offers, accepted, declined, no_response,
    no_response_rate and missed_value -- with ``headline`` set to the uncovered
    row and ``inside`` carrying the covered aggregate beside it.

    The contrast is the deliverable. On real data the same trucks and the same
    volume produce a 5.8% miss rate inside the staffed window and 61.7% outside
    it; quoting either number alone loses the argument.

    The windows are THIS COMPANY's -- ``companies.yaml -> coverage`` when the
    entry declares one, ``rules.yaml -> missed_work.coverage`` otherwise. A
    company in Texas is staffed different hours than one in Ohio, and a shared
    window would report both of them against somebody else's shift.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company(company_id, account_id) as company:
        resolved = _rules(rules, company)
        window = _window(window_start, window_end, None)
        book = _price_book(resolved)
        priced_classes = _priced_classes(resolved)

        def work(sess: Session) -> dict[str, Any]:
            views = _load_views(
                sess, window["_start_utc"], window["_end_utc"], resolved, company
            )
            document = _coverage_from(views, resolved, book, priced_classes)
            # NOTE no company_id here. This is a FRAGMENT of the missed-work
            # document, and compute_missed_work() embeds an identical copy of
            # it. Two tests assert the standalone and the embedded copies are
            # the same object; a key on one and not the other would make the
            # dashboard and the report disagree in a way nobody would notice.
            # The company is on the enclosing document, and on the request.
            document["window"] = _public_window(window)
            return document

        if session is not None:
            return work(session)
        with get_session(commit=False) as owned:
            return work(owned)


# ==========================================================================
# Section 3 -- the recoverable inventory, and the whole document
# ==========================================================================


def _top(
    views: Sequence[Mapping[str, Any]], field: str, label: str, limit: int
) -> list[dict[str, Any]]:
    """Most frequent values of ``field`` among ``views``, with a stable order."""
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for view in views:
        key = str(view.get(field) or "").strip()
        if not key:
            continue
        counts[key] += 1
        if field == "client_key":
            display.setdefault(key, str(view.get("client") or key))
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    out: list[dict[str, Any]] = []
    for key, count in ordered[: max(0, limit)]:
        entry: dict[str, Any] = {
            label: display.get(key, key),
            "missed": count,
            "share": _rate(count, total),
        }
        if field == "client_key":
            entry["client_key"] = key
        out.append(entry)
    return out


def _inventory_from(
    views: Sequence[Mapping[str, Any]],
    rules: Mapping[str, Any],
    book: Mapping[str, float],
    priced_classes: frozenset[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(inventory rows, metadata)`` for MISSED_WORK_MODEL.md section 3."""
    cfg = _cfg(rules, "inventory")
    restrict = bool(cfg["restrict_to_should_accept"])
    top_clients = int(cfg["top_clients"])
    top_service_types = int(cfg["top_service_types"])

    wanted, _ = _metrics._policy_classes(rules)
    wanted_set = set(wanted)

    missed_buckets = _name_set(rules, "missed_buckets")
    recoverable_buckets = _name_set(rules, "recoverable_buckets")
    if _block(rules).get("count_client_withdrew_as_recoverable"):
        recoverable_buckets = recoverable_buckets | {"client_withdrew"}

    missed_views = [view for view in views if view.get("bucket") in missed_buckets]
    total_missed = len(missed_views)

    # Per-class denominators: offers and accepted for the class as a whole, so
    # each inventory row carries the context needed to read its own number.
    class_offers: Counter[str] = Counter()
    class_accepted: Counter[str] = Counter()
    for view in views:
        service_class = str(view.get("service_class") or "")
        class_offers[service_class] += 1
        if view.get("bucket") == "won":
            class_accepted[service_class] += 1

    pairs: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for view in missed_views:
        service_class = str(view.get("service_class") or "")
        if restrict and service_class not in wanted_set:
            continue
        pairs[(service_class, str(view.get("cause") or ""))].append(view)

    inventory: list[dict[str, Any]] = []
    for (service_class, cause), group in pairs.items():
        remedy = str(group[0].get("remedy_class") or "")
        offers = class_offers.get(service_class, 0)
        missed = len(group)
        recoverable = sum(
            1 for view in group if view.get("bucket") in recoverable_buckets
        )
        entry: dict[str, Any] = {
            "service_class": service_class,
            "cause": cause,
            "remedy": remedy,
            "question": _remedy_question(cause, rules),
            "offers": offers,
            "accepted": class_accepted.get(service_class, 0),
            "missed": missed,
            # Share of EVERYTHING we missed in the window -- so the list reads
            # as "this pair is 21% of the problem", which is what ranks it.
            "missed_share": _rate(missed, total_missed),
            # Share of this class's own offers lost to this cause -- the "how
            # bad is it for this kind of work" number. Different question,
            # different denominator, both named.
            "class_miss_rate": _rate(missed, offers),
            "recoverable": recoverable,
            "buckets": dict(
                sorted(Counter(str(v.get("bucket") or "") for v in group).items())
            ),
            "top_clients": _top(group, "client_key", "client", top_clients),
            "top_service_types": _top(
                group, "service_type_raw", "service_type_raw", top_service_types
            ),
        }
        if book:
            entry.update(_valuation(group, book, priced_classes))
        inventory.append(entry)

    # Ranked by JOB COUNT, descending. The tiebreak is alphabetical so a
    # recompute of an unchanged window produces a byte-identical list.
    inventory.sort(key=lambda item: (-item["missed"], item["service_class"], item["cause"]))

    meta = {
        "restricted_to_should_accept": restrict,
        "service_classes": sorted(wanted_set) if restrict else [],
        "rank_by": str(cfg["rank_by"]),
        "top_n": int(cfg["top_n"]),
        "pairs": len(inventory),
        "missed_in_inventory": sum(entry["missed"] for entry in inventory),
        "missed_in_window": total_missed,
    }
    return inventory, meta


def _display_moment(value: Any) -> str:
    """A local ISO timestamp as ``Jul 28  2:14 PM``, or "" if it cannot be read.

    Presentation, and deliberately lossy -- the machine-readable value travels
    beside it as ``offered_at_local``. An unparseable value returns empty
    rather than raising: a report that renders every other column is worth more
    than one that fails whole over a formatting detail.
    """
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return text
    # %-d / %-I are not portable to Windows, which is where this also runs, so
    # the zero is stripped by hand.
    return moment.strftime("%b %d, %I:%M %p").replace(" 0", " ")


def _job_list_from(
    views: Sequence[Mapping[str, Any]],
    rules: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(one row per job we did not get, metadata)`` -- the lookup table.

    WHY THIS EXISTS. Everything else in this document is an aggregate, and an
    aggregate cannot answer the question the owner actually asks at the end of
    a day: *that one -- why did we not take that one?* Answering it means
    opening the offer in Towbook, and opening it means having the number
    Towbook searches on.

    Each row therefore leads with ``towbook_ref``: the Towbook job / call
    number when the offer ever became a job, and the Digital Request id when it
    did not -- which is the usual case here, because Towbook does not issue a
    call number for an offer nobody accepted (0 of 817 Expired rows in the
    archived data carry one). ``towbook_ref_kind`` says which of the two it is,
    since they are searched in two different Towbook screens.

    Chronological, not ranked. This is a log to scan alongside a memory of the
    shift, so it reads in the order the offers arrived; the ranked views of the
    same rows are ``inventory`` and ``by_cause`` above.
    """
    cfg = _cfg(rules, "job_list")
    max_rows = max(0, int(cfg["max_rows"]))
    restrict = bool(cfg["restrict_to_should_accept"])

    wanted, _ = _metrics._policy_classes(rules)
    wanted_set = set(wanted)

    missed_buckets = _name_set(rules, "missed_buckets")
    recoverable_buckets = _name_set(rules, "recoverable_buckets")
    if _block(rules).get("count_client_withdrew_as_recoverable"):
        recoverable_buckets = recoverable_buckets | {"client_withdrew"}

    missed_views = [view for view in views if view.get("bucket") in missed_buckets]
    if restrict:
        missed_views = [
            view
            for view in missed_views
            if str(view.get("service_class") or "") in wanted_set
        ]

    # Offer time, then request id: the same deterministic order every list in
    # this system uses, so a recompute of an unchanged window is byte
    # identical and the stored blob stays comparable.
    missed_views.sort(
        key=lambda view: (
            str(view.get("offered_at_local") or ""),
            str(view.get("request_id") or ""),
        )
    )

    rows: list[dict[str, Any]] = []
    for view in missed_views[:max_rows]:
        rows.append(
            {
                # THE LOOKUP KEY, first because it is what the row is for.
                "towbook_ref": view.get("towbook_ref") or view.get("request_id") or "",
                "towbook_ref_kind": view.get("towbook_ref_kind") or "request",
                "towbook_ref_label": view.get("towbook_ref_label") or "",
                # Both halves as well as the resolved reference, so a consumer
                # that wants to say "no job number was ever issued" can.
                "job_number": view.get("job_number") or None,
                "request_id": view.get("request_id") or "",
                "offered_at_local": view.get("offered_at_local"),
                # Formatted here rather than in each template. The emailed
                # reports have no date filter -- their Jinja environment
                # carries `num` and `pct` and nothing else -- and three
                # templates each parsing an ISO string is three chances to
                # print a different clock.
                "offered_display": _display_moment(view.get("offered_at_local")),
                "client": view.get("client") or "",
                "client_key": view.get("client_key") or "",
                "service_type_raw": view.get("service_type_raw") or "",
                "service_class": view.get("service_class") or "",
                # The VERBATIM portal status, not the five-value canonical one:
                # "Another Provider Responded" and "Expired" are the same
                # canonical `expired` and completely different explanations.
                "status_raw": view.get("status_raw") or "",
                "status": view.get("status") or "",
                "bucket": view.get("bucket") or "",
                "cause": view.get("cause") or "",
                "denial_reason": view.get("denial_reason") or None,
                "coverage_label": view.get("coverage_label") or "",
                "pickup_location": view.get("pickup_location") or "",
                "recoverable": view.get("bucket") in recoverable_buckets,
            }
        )

    meta = {
        "max_rows": max_rows,
        "restricted_to_should_accept": restrict,
        "service_classes": sorted(wanted_set) if restrict else [],
        "shown": len(rows),
        "missed_in_window": len(missed_views),
        "truncated": len(missed_views) > len(rows),
        # How many of the shown rows can be looked up by a real Towbook job
        # number. Stated rather than left to be noticed, because it is low by
        # nature and a reader who does not know that reads the blanks as a bug.
        "with_job_number": sum(1 for row in rows if row["job_number"]),
        "job_number_note": (
            "Towbook issues a job (call) number when an offer becomes a job, so "
            "most offers we did not accept never got one. Those rows are "
            "referenced by their Digital Request id, which the Request Log "
            "searches."
        ),
    }
    return rows, meta


def _by_cause_from(
    views: Sequence[Mapping[str, Any]],
    rules: Mapping[str, Any],
    book: Mapping[str, float],
    priced_classes: frozenset[str] | None,
) -> dict[str, Any]:
    missed_buckets = _name_set(rules, "missed_buckets")
    recoverable_buckets = _name_set(rules, "recoverable_buckets")
    if _block(rules).get("count_client_withdrew_as_recoverable"):
        recoverable_buckets = recoverable_buckets | {"client_withdrew"}

    missed_views = [view for view in views if view.get("bucket") in missed_buckets]
    total = len(missed_views)

    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for view in missed_views:
        groups[str(view.get("cause") or "")].append(view)

    out: dict[str, Any] = {}
    for cause in sorted(groups, key=lambda c: (-len(groups[c]), c)):
        group = groups[cause]
        entry: dict[str, Any] = {
            "cause": cause,
            "remedy": str(group[0].get("remedy_class") or ""),
            "question": _remedy_question(cause, rules),
            "missed": len(group),
            "share": _rate(len(group), total),
            "recoverable": sum(
                1 for view in group if view.get("bucket") in recoverable_buckets
            ),
            "buckets": dict(
                sorted(Counter(str(v.get("bucket") or "") for v in group).items())
            ),
            "service_classes": dict(
                sorted(Counter(str(v.get("service_class") or "") for v in group).items())
            ),
            "top_clients": _top(group, "client_key", "client", 5),
        }
        if book:
            entry.update(_valuation(group, book, priced_classes))
        out[cause] = entry
    return out


def _totals_from(
    views: Sequence[Mapping[str, Any]], rules: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    """``(totals, by_bucket, bucket_sources)``."""
    counts = Counter(str(view.get("bucket") or "") for view in views)
    sources = Counter(str(view.get("bucket_source") or "") for view in views)

    missed_buckets = _name_set(rules, "missed_buckets")
    recoverable_buckets = _name_set(rules, "recoverable_buckets")
    fold_withdrew = bool(_block(rules).get("count_client_withdrew_as_recoverable"))

    declared = list(_mapping(rules, "bucket_by_canonical_status").values())
    for value in _mapping(rules, "buckets").values():
        declared.append(value)
    for value in _mapping(rules, "bucket_by_status_name").values():
        declared.append(value)
    declared.append(str(_block(rules).get("bucket_default") or UNKNOWN_BUCKET))

    names: list[str] = []
    for value in declared:
        name = str(value).strip()
        if name and name not in names:
            names.append(name)
    for name in sorted(counts):
        if name and name not in names:
            names.append(name)

    offers = len(views)
    by_bucket: dict[str, Any] = {}
    for name in names:
        count = counts.get(name, 0)
        by_bucket[name] = {
            "bucket": name,
            "count": count,
            "share": _rate(count, offers),
            "is_missed": name in missed_buckets,
            "is_recoverable": name in recoverable_buckets
            or (fold_withdrew and name == "client_withdrew"),
        }

    accepted = counts.get("won", 0)
    withdrew = counts.get("client_withdrew", 0)
    missed = sum(counts.get(name, 0) for name in missed_buckets)
    recoverable = sum(counts.get(name, 0) for name in recoverable_buckets)
    if fold_withdrew:
        recoverable += withdrew

    totals = {
        "offers": offers,
        "accepted": accepted,
        "missed": missed,
        # BOTH numbers appear whatever count_client_withdrew_as_recoverable
        # says, so the setting can never hide one (MISSED_WORK_MODEL.md s1).
        "recoverable": recoverable,
        "recoverable_excluding_withdrew": sum(
            counts.get(name, 0) for name in recoverable_buckets
        ),
        "recoverable_including_withdrew": sum(
            counts.get(name, 0) for name in recoverable_buckets
        )
        + withdrew,
        "withdrew": withdrew,
        "in_flight": counts.get("in_flight", 0),
        "declined": counts.get("declined", 0),
        "no_response": counts.get("no_response", 0),
        "accept_failed": counts.get("accept_failed", 0),
        "unknown_status": counts.get(
            str(_block(rules).get("bucket_default") or UNKNOWN_BUCKET), 0
        ),
        "acceptance_rate": _rate(accepted, offers),
        "missed_rate": _rate(missed, offers),
        "recoverable_rate": _rate(recoverable, offers),
        "no_response_rate": _rate(counts.get("no_response", 0), offers),
        "count_client_withdrew_as_recoverable": fold_withdrew,
    }
    return totals, by_bucket, dict(sorted(sources.items()))


def _unknown_status_examples(
    views: Sequence[Mapping[str, Any]], default: str
) -> list[dict[str, Any]]:
    """The exact status values the bucket maps did not recognise.

    Two populations, and both are evidence somebody has to act on:

    * rows that ended in the ``bucket_default`` because nothing knew them at all;
    * rows that carried a source LABEL the bucket maps did not know and were
      rescued by the coarse canonical-status map. Those get a plausible bucket,
      so they never look wrong -- which is exactly why they have to be named
      here. Silently absorbing a new Towbook status is how a report starts
      being confidently incorrect.
    """
    counts: Counter[tuple[str, str, str]] = Counter()
    for view in views:
        bucket = str(view.get("bucket") or "")
        source = str(view.get("bucket_source") or "")
        label = str(view.get("status_raw") or "").strip()
        unmapped_label = bool(label) and source in {"canonical_status", "default"}
        if bucket != default and not unmapped_label:
            continue
        counts[(label, str(view.get("status") or ""), bucket)] += 1
    return [
        {
            "status_raw": label,
            "status": canonical,
            "bucketed_as": bucket,
            "count": count,
        }
        for (label, canonical, bucket), count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def compute_missed_work(
    session: Session | None = None,
    window_start: Any = None,
    window_end: Any = None,
    rules: Mapping[str, Any] | None = None,
    *,
    period_type: str | None = None,
    company_id: str | None = None,
    account_id: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """The inventory of missed work for ``[window_start, window_end)``.

    ``window_start`` / ``window_end`` are **local** times (naive is taken as
    local, aware is converted, a date becomes local midnight). The window is
    half-open, so consecutive windows neither overlap nor leave a gap.

    Returns the document described in MISSED_WORK_MODEL.md section 3 and, when
    ``persist``, upserts it into ``metrics_missed_work`` on
    ``(window_start, window_end, period_type)``. Recomputing an unchanged window
    writes the identical blob -- ``computed_at`` is stripped before storage and
    lives in its own column.

    One snapshot of ``rules.yaml`` is taken for the whole computation, so a
    human saving the file mid-run cannot produce a document whose halves were
    classified under different rules. It is THIS COMPANY's snapshot: global
    rules.yaml with the company's coverage window, job values and thresholds
    merged over it.

    The upsert key is ``(company_id, window_start, window_end, period_type)``.
    Without the company in it, the second tenant to compute Tuesday would
    overwrite the first one's Tuesday with its own numbers and nothing on the
    dashboard would look wrong.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    with _company(company_id, account_id) as company:
        return _compute_missed_work(
            session,
            window_start,
            window_end,
            rules,
            period_type=period_type,
            company_id=company,
            persist=persist,
        )


def _compute_missed_work(
    session: Session | None,
    window_start: Any,
    window_end: Any,
    rules: Mapping[str, Any] | None,
    *,
    period_type: str | None,
    company_id: str,
    persist: bool,
) -> dict[str, Any]:
    resolved = dict(_rules(rules, company_id))
    version = rules_version()
    window = _window(window_start, window_end, period_type)
    book = _price_book(resolved)
    priced_classes = _priced_classes(resolved)
    bucket_default = str(_block(resolved).get("bucket_default") or UNKNOWN_BUCKET)

    def work(sess: Session) -> dict[str, Any]:
        views = _load_views(
            sess, window["_start_utc"], window["_end_utc"], resolved, company_id
        )

        totals, by_bucket, bucket_sources = _totals_from(views, resolved)
        inventory, inventory_meta = _inventory_from(
            views, resolved, book, priced_classes
        )
        missed_jobs, missed_jobs_meta = _job_list_from(views, resolved)
        coverage = _coverage_from(views, resolved, book, priced_classes)

        # THE HEADLINE RECOVERABLE FIGURE IS THE OUTSIDE-HOURS ONE, promoted
        # into totals so every consumer leads with it without having to know
        # the coverage document's shape. The inside figure is promoted with it,
        # never without it: the contrast IS the business case, and either
        # number quoted alone is a different and weaker claim.
        totals["coverage_default_label"] = coverage["default_label"]
        totals["recoverable_uncovered"] = coverage["outside"]["recoverable"]
        totals["recoverable_covered"] = coverage["inside"]["recoverable"]
        totals["no_response_uncovered"] = coverage["outside"]["no_response"]
        totals["no_response_covered"] = coverage["inside"]["no_response"]
        totals["no_response_rate_uncovered"] = coverage["outside"]["no_response_rate"]
        totals["no_response_rate_covered"] = coverage["inside"]["no_response_rate"]

        if book:
            missed_buckets = _name_set(resolved, "missed_buckets")
            recoverable_buckets = _name_set(resolved, "recoverable_buckets")
            if _block(resolved).get("count_client_withdrew_as_recoverable"):
                recoverable_buckets = recoverable_buckets | {"client_withdrew"}
            totals.update(
                _valuation(
                    [v for v in views if v.get("bucket") in missed_buckets],
                    book,
                    priced_classes,
                )
            )
            # The defensible number, alongside the biggest one. Quoting only
            # estimated_value would price every client withdrawal as work we
            # could have had, which is exactly the claim
            # count_client_withdrew_as_recoverable declines to make.
            totals["recoverable_estimated_value"] = _valuation(
                [v for v in views if v.get("bucket") in recoverable_buckets],
                book,
                priced_classes,
            )["estimated_value"]

        document: dict[str, Any] = {
            "report_type": "missed_work",
            "company_id": company_id,
            "window": _public_window(window),
            "period_type": window["period_type"],
            "timezone": window["timezone"],
            "rules_version": version,
            "computed_at": utcnow().isoformat(sep="T"),
            "totals": totals,
            "by_bucket": by_bucket,
            "by_cause": _by_cause_from(views, resolved, book, priced_classes),
            "inventory": inventory,
            "inventory_meta": inventory_meta,
            # THE ONLY ROW-LEVEL SECTION IN THIS DOCUMENT, and the one the
            # owner opens Towbook with: every job we did not get, each carrying
            # the reference Towbook searches on. Every other section here
            # answers "what should we change"; this answers "why that one".
            #
            # It is row level on purpose, which means agents/analyst.py drops it
            # before any LLM call -- see ROW_LEVEL_KEYS there. That guard is
            # correct and this section is not an exception to it.
            "missed_jobs": missed_jobs,
            "missed_jobs_meta": missed_jobs_meta,
            # THE OPERATING WINDOW, as a first-class dimension. Sits above the
            # 7 x 24 grid deliberately: the grid finds which hours leak, this
            # says whether anybody was rostered, and only the second one is an
            # instruction.
            "coverage": coverage,
            "blind_spots": _blind_spots_from(views, resolved),
            "closeoff_candidates": _closeoff_from(views, resolved),
            "client_comparison": _client_comparison_from(views, resolved),
            # HOW TO READ EVERY NUMBER ABOVE. offerAmount is empty on 100% of
            # the records this account produces, so these are job counts. No
            # consumer may present them as money while revenue_available is
            # false, and none may infer a value where none is configured.
            "ranking_basis": str(_cfg(resolved, "inventory")["rank_by"]),
            "revenue_available": bool(book),
            "revenue_note": (
                "Ranked by job count. offerAmount is empty on 100% of records in "
                "this account, so no dollar figure is derivable from this feed."
                if not book
                else (
                    "estimated_value uses the per-client average job values in "
                    "missed_work.job_value_by_client -- GROSS, not margin, and not "
                    "from the feed, which carries no amounts at all. It is applied "
                    "only to "
                    + (
                        "every service class"
                        if priced_classes is None
                        else ", ".join(sorted(priced_classes))
                    )
                    + " (missed_work.job_value_applies_to). Rows outside that set, "
                    "and clients with no configured value, contribute nothing and "
                    "are counted in unpriced_missed. Ranking is still by job count."
                )
            ),
            "priced_service_classes": (
                None if priced_classes is None else sorted(priced_classes)
            ),
            # Which of the three status maps answered, per row. A document
            # resting on the coarse canonical-status map says so out loud.
            "bucket_sources": bucket_sources,
            "unknown_statuses": _unknown_status_examples(views, bucket_default),
            "counts": {
                "requests": len(views),
                "clients": len({str(v.get("client_key") or "") for v in views}),
            },
        }

        if persist:
            _metrics._upsert(
                sess,
                MetricsMissedWork,
                {
                    "company_id": company_id,
                    "window_start": window["_start_utc"],
                    "window_end": window["_end_utc"],
                    "period_type": window["period_type"],
                },
                {
                    "metrics": _persistable(document),
                    "rules_version": version,
                    "computed_at": utcnow(),
                },
            )

        logger.info(
            "missed_work %s %s -> %s: %d offers, %d accepted, %d missed, "
            "%d recoverable (%d withdrew), %d blind spot(s), %d close-off type(s); "
            "coverage: %d recoverable outside vs %d inside the staffed window",
            window["period_type"],
            window["start"],
            window["end"],
            totals["offers"],
            totals["accepted"],
            totals["missed"],
            totals["recoverable"],
            totals["withdrew"],
            document["blind_spots"]["blind_spot_count"],
            document["closeoff_candidates"]["totals"]["service_types"],
            totals["recoverable_uncovered"],
            totals["recoverable_covered"],
        )
        return document

    if session is not None:
        result = work(session)
        session.flush()
        return result
    with get_session(commit=persist) as owned:
        return work(owned)


def _persistable(document: Mapping[str, Any]) -> dict[str, Any]:
    """The document minus the keys that legitimately differ between runs."""
    return _metrics._jsonable(
        {key: value for key, value in document.items() if key not in _VOLATILE_KEYS}
    )


def get_stored_missed_work(
    window_start: Any,
    window_end: Any,
    period_type: str | None = None,
    *,
    session: Session | None = None,
    company_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any] | None:
    """Read back one company's persisted missed-work document, or None."""
    from sqlalchemy import select

    with _company(company_id, account_id) as company:
        window = _window(window_start, window_end, period_type)

    def read(sess: Session) -> dict[str, Any] | None:
        row = sess.scalars(
            select(MetricsMissedWork)
            .where(MetricsMissedWork.company_id == company)
            .where(MetricsMissedWork.window_start == window["_start_utc"])
            .where(MetricsMissedWork.window_end == window["_end_utc"])
            .where(MetricsMissedWork.period_type == window["period_type"])
            .limit(1)
        ).first()
        if row is None:
            return None
        document = dict(row.metrics or {})
        document["company_id"] = row.company_id
        document["rules_version"] = row.rules_version
        document["computed_at"] = (
            row.computed_at.isoformat(sep="T") if row.computed_at else None
        )
        return document

    if session is not None:
        return read(session)
    with get_session(commit=False) as owned:
        return read(owned)


# ==========================================================================
# Alert contexts (MISSED_WORK_MODEL.md section 8)
# ==========================================================================


def alert_contexts(
    session: Session,
    window_end: Any,
    rules: Mapping[str, Any] | None = None,
    *,
    company_id: str | None = None,
    account_id: str | None = None,
    report_type: str = "missed_work",
) -> list[tuple[str, str, dict[str, Any]]]:
    """Build the evaluation contexts for the three aggregate missed-work alerts.

    Returns ``(entity_kind, entity, context)`` triples for
    ``blind_spot_forming``, ``decline_cause_spike`` and ``closeoff_candidate``.
    The fourth, ``tow_unanswered``, is row-scoped and rides on the per-row
    context agents/metrics.py already builds -- which is why metrics puts
    ``bucket`` on it.

    Every context is computed over its **own trailing window**, ending at
    ``window_end``, rather than over whatever window the caller happened to
    report on. Two reasons. The rule text names the window
    (``cause_count_24h``, ``service_type_offers_7d``) and the code must mean the
    same thing it says. And an alert must not change its mind depending on
    whether the daily or the weekly job happened to run it.

    ``window_end`` rather than "now", so that an alert raised by a recompute of
    last Tuesday describes last Tuesday.

    Nothing is evaluated or emitted here. metrics owns ``_fire_alerts``, the
    ``alerts_fired`` dedupe read and the event bus, and two writers on that path
    would corrupt the notifier's rate limit.

    Every trailing window below is loaded for one company only, and the dedupe
    that metrics applies to the result is scoped to the same one.

    ``account_id`` is the old name for ``company_id`` and still works.
    """
    company = _companies.resolve_company_id(
        company_id if company_id is not None else account_id
    )
    with _companies.use_company(company):
        return _alert_contexts(session, window_end, rules, company, report_type)


def _alert_contexts(
    session: Session,
    window_end: Any,
    rules: Mapping[str, Any] | None,
    company_id: str,
    report_type: str,
) -> list[tuple[str, str, dict[str, Any]]]:
    resolved = _rules(rules, company_id)
    cfg = _cfg(resolved, "alerts")
    end_local = _metrics.parse_local_datetime(window_end)
    end_utc = _metrics.to_utc(end_local)
    version = rules_version()

    out: list[tuple[str, str, dict[str, Any]]] = []

    # ---- blind_spot_forming ------------------------------------------------
    # HOUR OF DAY, not hour of week, and over a trailing multi-day window.
    # A single day's 18:00 cell holds four or five offers, which is below the
    # min_offers floor by design -- so a per-day hour-of-week cell would almost
    # never fire and the alert would be decoration. Aggregating the same clock
    # hour across the trailing week gives ~30 offers a cell: enough to tell a
    # forming blind spot from a bad evening. The 7 x 24 grid still exists, in
    # the report, where the extra resolution is worth having.
    spot_days = max(1, int(cfg["blind_spot_window_days"]))
    spot_views = _load_views(
        session, end_utc - timedelta(days=spot_days), end_utc, resolved, company_id
    )
    for entry in _blind_spots_from(spot_views, resolved)["by_hour"]:
        if not entry["offers"]:
            continue
        out.append(
            (
                "hour",
                f"hour:{int(entry['hour']):02d}",
                {
                    "hour": int(entry["hour"]),
                    "hour_label": entry["label"],
                    "hour_offers": int(entry["offers"]),
                    "hour_accepted": int(entry["accepted"]),
                    "hour_no_response": int(entry["no_response"]),
                    "hour_no_response_rate": entry["no_response_rate"],
                    "blind_spot_window_days": spot_days,
                    "report_type": report_type,
                    "rules_version": version,
                },
            )
        )

    # ---- decline_cause_spike ----------------------------------------------
    hours = max(1, int(cfg["cause_window_hours"]))
    baseline_days = max(1, int(cfg["cause_baseline_days"]))
    recent_start = end_utc - timedelta(hours=hours)
    baseline_start = recent_start - timedelta(days=baseline_days)

    recent = _load_views(session, recent_start, end_utc, resolved, company_id)
    baseline = _load_views(session, baseline_start, recent_start, resolved, company_id)

    missed_buckets = _name_set(resolved, "missed_buckets")
    recent_causes = Counter(
        str(view.get("cause") or "")
        for view in recent
        if view.get("bucket") in missed_buckets
    )
    baseline_causes = Counter(
        str(view.get("cause") or "")
        for view in baseline
        if view.get("bucket") in missed_buckets
    )

    for cause in sorted(set(recent_causes) | set(baseline_causes)):
        # The baseline is a per-window average, not a total, so a 24h count is
        # compared against a comparable 24h figure rather than a 7x bigger one.
        baseline_rate = baseline_causes.get(cause, 0) / baseline_days
        out.append(
            (
                "cause",
                f"cause:{cause}",
                {
                    "cause": cause,
                    "cause_count_24h": recent_causes.get(cause, 0),
                    "cause_baseline_7d": round(baseline_rate, 4),
                    "cause_baseline_total": baseline_causes.get(cause, 0),
                    "cause_window_hours": hours,
                    "cause_baseline_days": baseline_days,
                    "report_type": report_type,
                    "rules_version": version,
                },
            )
        )

    # ---- closeoff_candidate ------------------------------------------------
    closeoff_days = max(1, int(cfg["closeoff_window_days"]))
    closeoff_start = end_utc - timedelta(days=closeoff_days)
    trailing = _load_views(session, closeoff_start, end_utc, resolved, company_id)

    closeoff_cfg = _cfg(resolved, "closeoff")
    wanted, _ = _metrics._policy_classes(resolved)
    wanted_set = set(wanted) if bool(closeoff_cfg["exclude_wanted_classes"]) else set()

    per_type: dict[str, dict[str, Any]] = {}
    for view in trailing:
        if view.get("service_class") in wanted_set:
            continue
        raw = str(view.get("service_type_raw") or "").strip()
        if not raw:
            continue
        entry = per_type.setdefault(
            raw, {"offers": 0, "accepted": 0, "service_class": view.get("service_class")}
        )
        entry["offers"] += 1
        if view.get("bucket") == "won":
            entry["accepted"] += 1

    for raw in sorted(per_type):
        entry = per_type[raw]
        out.append(
            (
                "service_type",
                f"service_type:{raw}",
                {
                    "service_type_raw": raw,
                    # DELIBERATELY NOT named `service_class`. The row-scoped
                    # `unclassified_service_type` rule tests that exact name, and
                    # an aggregate context carrying it would fire that alert a
                    # second time under a service-type entity -- one finding,
                    # two texts, two different entities in alerts_fired.
                    "service_type_class": entry["service_class"],
                    "service_type_offers_7d": entry["offers"],
                    "service_type_accepted_7d": entry["accepted"],
                    "service_type_rate_7d": _rate(entry["accepted"], entry["offers"]),
                    "closeoff_window_days": closeoff_days,
                    "report_type": report_type,
                    "rules_version": version,
                },
            )
        )

    return out
