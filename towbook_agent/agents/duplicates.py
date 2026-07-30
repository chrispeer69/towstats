"""One job offered twice is one job.

THE PROBLEM
-----------
A motor club that does not get an answer asks again. Same car, same address,
same service, ten to twenty minutes later, a new ``callRequestId``. Counted as
written, that job appears in the report twice: twice in ``offered``, and -- if
we turned it down or never saw it -- twice in the decline or no-response
count. The busier the club is at re-asking, the worse the account looks.

Measured over the 3,124 archived records for this account: **228 clusters,
261 suppressed offers, 8.4% of everything offered in 30 days.** The shape is
unmistakable -- the outcome simply repeats:

    Cancelled -> Cancelled      56 clusters
    Expired   -> Expired        44
    Rejected  -> Rejected       29
    Rejected  -> Cancelled       9
    Expired   -> Rejected        8
    Expired   -> Accepted        5   <- asked again, and the second time we took it
    Cancelled -> Accepted        5

Median gap between consecutive offers in a cluster: 16 minutes. Longest: 59.

WHAT COUNTS AS THE SAME JOB
---------------------------
``(client, vehicle)`` inside a window, and the choice of key is evidence-led:

* **There is no customer name in this feed.** All 30 keys the API returns were
  enumerated; the club is there, the address is there, the person is not. The
  car is the only thing that identifies whose job it is.
* **The address is not usable as a key.** Of the 18 pairs where the same club
  offered the same car twice inside an hour from a *different-looking*
  address, all 18 were one job written two ways ("I-270 N, Dublin, OH, 43017"
  vs "I-270, Dublin, OH, USA 43017"). Matching on it would split them back
  apart. It is available as a match field for an operator who wants it, and it
  is not on by default.
* **The club stays in the key.** Two different clubs offering the same car is
  the customer's insurer and their roadside app both looking for a truck --
  two real offers to us, and collapsing them across clients would corrupt the
  per-client acceptance rates the close-off decisions rest on.

THE WINDOW IS ANCHORED, NOT CHAINED
-----------------------------------
A cluster runs from its FIRST offer, so it can never be longer than the window
however many offers arrive. Chaining ("within 60 minutes of the previous one")
would let six offers 59 minutes apart collapse a six-hour span into one job.

WHICH ROW SURVIVES
------------------
The one whose outcome is the most decisive, by ``outcome_precedence`` in
rules.yaml -- accepted, then denied, then canceled, then expired, then
pending. Ties break on the earliest offer.

That order is a claim about what actually happened, and it is the honest one.
If a club offered twice, we ignored the first and rejected the second, we were
asked once and we said no: that is a decline, not a no-response. Recording it
as no-response would send the blind-spot analysis after a staffing gap that
was not there.

WHAT IS NEVER COLLAPSED
-----------------------
Two offers that BOTH became real Towbook jobs, with different job numbers.
Towbook issues a job number when work is opened, so two of them is two pieces
of work -- collapsing those would understate what the company actually did.
There is one such cluster in the 30-day sample. Governed by
``keep_distinct_accepted``.

A row with no vehicle is never a duplicate of anything. Blank keys are the
classic way a dedupe rule quietly eats unrelated records; here they are
counted in ``unkeyed`` and passed through untouched.

NOTHING IS DELETED, AND NOTHING IS SILENT
-----------------------------------------
This runs at READ time, over the rows a window loaded. ``requests`` keeps
every offer Towbook ever made -- the raw record is the truth, and re-cutting
the rule in rules.yaml re-cuts every report with no migration and no re-pull.
Every collapse is reported: the surviving row carries ``duplicate_count`` and
the references it stands for, and every document carries a ``duplicates``
block saying how many offers were suppressed and why.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULTS",
    "config_for",
    "normalize_value",
    "duplicate_key",
    "collapse",
    "summarize",
    "empty_report",
]

logger = logging.getLogger(__name__)

#: Used when rules.yaml carries no ``duplicate_offers`` block, and merged under
#: whatever it does carry, so a partial block is completed rather than rejected.
#:
#: ``enabled`` defaults TRUE. The counts without it are wrong by 8.4% on real
#: traffic, in the direction that makes the company look worse than it is.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    #: How long after the first offer a repeat still counts as the same job.
    #: 60 covers the whole observed distribution (longest real gap: 59.5 min).
    "window_minutes": 60,
    #: Row fields that must all match. `client_key` keeps clubs apart;
    #: `vehicle` is the customer's job. Add `pickup_zip` for a stricter rule,
    #: but NOT `pickup_location` -- see the module docstring.
    "match_fields": ["client_key", "vehicle"],
    #: A row missing any of these can never be a duplicate. Without this a
    #: hundred rows with no vehicle would all key alike and collapse into one.
    "require_fields": ["vehicle"],
    #: Which outcome represents the cluster. First match wins.
    "outcome_precedence": ["accepted", "denied", "canceled", "expired", "pending"],
    #: Two offers that both became real Towbook jobs are two pieces of work.
    "keep_distinct_accepted": True,
}

#: Everything except letters, digits and spaces is dropped before matching, so
#: "2018 HONDA CR-V TOU silver" and "2018 Honda CR V TOU Silver" are one car.
_PUNCTUATION = re.compile(r"[^0-9a-z ]+")
_WHITESPACE = re.compile(r"\s+")


def empty_report(reason: str = "disabled") -> dict[str, Any]:
    """The shape every consumer reads, for a run where nothing was collapsed.

    Returned rather than ``None`` so a template or a metrics document never has
    to distinguish "the rule did not run" from "the rule found nothing" by
    checking for a missing key -- ``reason`` says which.
    """
    return {
        "enabled": reason != "disabled",
        "reason": reason,
        "suppressed": 0,
        "clusters": 0,
        "offers_before": 0,
        "offers_after": 0,
        "unkeyed": 0,
        "kept_distinct_accepted": 0,
        "by_status": {},
        "by_client": {},
        "window_minutes": int(DEFAULTS["window_minutes"]),
        "match_fields": list(DEFAULTS["match_fields"]),
    }


def config_for(rules: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``duplicate_offers`` block, completed from :data:`DEFAULTS`.

    A block that sets only ``window_minutes`` still gets every other key, so
    editing one number in rules.yaml cannot accidentally turn the rule off or
    empty its match fields.
    """
    config = dict(DEFAULTS)
    block = (rules or {}).get("duplicate_offers")
    if isinstance(block, Mapping):
        for key, value in block.items():
            if key in config and value is not None:
                config[key] = value

    config["enabled"] = bool(config["enabled"])
    config["keep_distinct_accepted"] = bool(config["keep_distinct_accepted"])
    try:
        config["window_minutes"] = max(0, int(config["window_minutes"]))
    except (TypeError, ValueError):
        logger.warning(
            "duplicate_offers.window_minutes is not a number (%r); using %s",
            config["window_minutes"],
            DEFAULTS["window_minutes"],
        )
        config["window_minutes"] = int(DEFAULTS["window_minutes"])

    for key in ("match_fields", "require_fields", "outcome_precedence"):
        value = config[key]
        config[key] = [str(item) for item in value] if isinstance(value, (list, tuple)) else []

    if not config["match_fields"]:
        # A rule with no match fields would make every row the same job. That
        # is never what somebody meant, so it turns the rule off instead.
        logger.warning("duplicate_offers.match_fields is empty; the rule is disabled")
        config["enabled"] = False
    return config


def normalize_value(value: Any) -> str:
    """Fold a match value: casefold, drop punctuation, collapse whitespace.

    The clubs are inconsistent about exactly the things that do not matter --
    "CR-V" and "CR V", double spaces, a trailing comma. Folding here rather
    than at ingest keeps the stored string verbatim, which is what lets the
    matching rule change without a re-pull.
    """
    if value is None:
        return ""
    text = str(value).strip().casefold()
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def duplicate_key(row: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The identity two offers must share to be the same job, or None.

    None means "this row can never be a duplicate": a required field is blank,
    so there is nothing to recognise it by. Returning None rather than an empty
    key is the whole safety property -- otherwise every row missing a vehicle
    would key identically and the rule would collapse unrelated jobs.
    """
    for name in config["require_fields"]:
        if not normalize_value(row.get(name)):
            return None
    return tuple(normalize_value(row.get(name)) for name in config["match_fields"])


def _moment(row: Mapping[str, Any]) -> datetime | None:
    """The offer time, from whichever key this caller's rows use.

    metrics' row views carry ISO strings; the dashboard's rows carry real
    datetimes. Both are accepted so there is ONE implementation of the rule
    rather than one per caller.
    """
    for name in ("offered_at_utc", "offered_at", "offered_utc"):
        value = row.get(name)
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                continue
    return None


def _status_of(row: Mapping[str, Any]) -> str:
    """The canonical status, preferring the stored value over the display one.

    ``status_stored`` / the raw column is empty when the ingester could not
    read a status. That is not "pending", and precedence must not treat it as
    a decisive outcome.
    """
    for name in ("status_stored", "status"):
        value = str(row.get(name) or "").strip().casefold()
        if value:
            return value
    return ""


def _job_number_of(row: Mapping[str, Any]) -> str:
    return str(row.get("job_number") or "").strip()


def _rank(status: str, precedence: Sequence[str]) -> int:
    try:
        return precedence.index(status)
    except ValueError:
        # An outcome nobody ranked sorts last, so it can only win a cluster
        # that holds nothing else. Never silently promoted.
        return len(precedence)


def collapse(
    rows: Iterable[Mapping[str, Any]],
    rules: Mapping[str, Any] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(rows with duplicate offers collapsed, what was collapsed)``.

    Each surviving row is a copy carrying three added keys:

    ``duplicate_count``   how many offers the club made for this one job (1 for
                          the overwhelming majority).
    ``duplicate_of``      the references of the offers it now stands for, so a
                          reader can go and look at every one of them.
    ``duplicate_window``  the minutes those offers were spread over.

    Input order is preserved for the survivors, so a caller that sorted its
    rows does not have to sort them again.
    """
    settings = dict(config) if config is not None else config_for(rules)
    materialised = [dict(row) for row in rows]

    report = empty_report("disabled" if not settings["enabled"] else "no_duplicates")
    report["window_minutes"] = int(settings["window_minutes"])
    report["match_fields"] = list(settings["match_fields"])
    report["offers_before"] = len(materialised)
    report["offers_after"] = len(materialised)

    if not settings["enabled"] or not materialised:
        for row in materialised:
            row.setdefault("duplicate_count", 1)
            row.setdefault("duplicate_of", [])
            row.setdefault("duplicate_window", 0)
        return materialised, report

    window = timedelta(minutes=int(settings["window_minutes"]))
    precedence = list(settings["outcome_precedence"])
    keep_accepted = bool(settings["keep_distinct_accepted"])
    accepted_status = precedence[0] if precedence else "accepted"

    # position -> row, so survivors come back in the order they arrived.
    groups: dict[tuple[str, ...], list[tuple[datetime, int, dict[str, Any]]]] = defaultdict(list)
    passthrough: list[int] = []

    for index, row in enumerate(materialised):
        key = duplicate_key(row, settings)
        moment = _moment(row)
        if key is None or moment is None:
            # Unkeyable, or undateable. Either way it cannot be matched to
            # anything, and it is COUNTED so the gap is visible.
            passthrough.append(index)
            continue
        groups[key].append((moment, index, row))

    report["unkeyed"] = len(passthrough)

    survivors: dict[int, dict[str, Any]] = {index: materialised[index] for index in passthrough}
    for row in survivors.values():
        row["duplicate_count"] = 1
        row["duplicate_of"] = []
        row["duplicate_window"] = 0

    suppressed_by_status: Counter[str] = Counter()
    suppressed_by_client: Counter[str] = Counter()
    clusters = 0
    kept_distinct = 0

    for entries in groups.values():
        entries.sort(key=lambda item: (item[0], item[1]))
        for cluster in _clusters(entries, window):
            if len(cluster) == 1:
                moment, index, row = cluster[0]
                row["duplicate_count"] = 1
                row["duplicate_of"] = []
                row["duplicate_window"] = 0
                survivors[index] = row
                continue

            clusters += 1
            keepers, dropped = _split(cluster, precedence, keep_accepted, accepted_status)
            kept_distinct += max(0, len(keepers) - 1)

            span = cluster[-1][0] - cluster[0][0]
            for position, (_, index, row) in enumerate(keepers):
                # Every dropped offer is attributed to the FIRST keeper, so the
                # count is never double-reported across two survivors of the
                # same cluster.
                mine = dropped if position == 0 else []
                row["duplicate_count"] = 1 + len(mine)
                row["duplicate_of"] = [_reference(other) for _, _, other in mine]
                row["duplicate_window"] = int(span.total_seconds() // 60) if mine else 0
                survivors[index] = row

            for _, _, row in dropped:
                suppressed_by_status[_status_of(row) or "unknown"] += 1
                suppressed_by_client[str(row.get("client_key") or "")] += 1

    kept = [survivors[index] for index in sorted(survivors)]

    report["clusters"] = clusters
    report["suppressed"] = sum(suppressed_by_status.values())
    report["offers_after"] = len(kept)
    report["kept_distinct_accepted"] = kept_distinct
    report["by_status"] = dict(sorted(suppressed_by_status.items()))
    report["by_client"] = dict(sorted(suppressed_by_client.items()))
    report["enabled"] = True
    report["reason"] = "collapsed" if report["suppressed"] else "no_duplicates"

    if report["suppressed"]:
        logger.info(
            "duplicate offers: %d of %d suppressed across %d cluster(s) "
            "within %d minutes on %s",
            report["suppressed"],
            report["offers_before"],
            clusters,
            report["window_minutes"],
            " + ".join(report["match_fields"]),
        )
    return kept, report


def _clusters(
    entries: Sequence[tuple[datetime, int, dict[str, Any]]], window: timedelta
) -> list[list[tuple[datetime, int, dict[str, Any]]]]:
    """Split time-ordered entries into runs anchored on the first offer.

    ANCHORED, not chained: the anchor only moves when an offer falls outside
    the window from it, so a cluster can never span more than ``window`` no
    matter how many offers arrive inside it.
    """
    out: list[list[tuple[datetime, int, dict[str, Any]]]] = []
    current: list[tuple[datetime, int, dict[str, Any]]] = [entries[0]]
    anchor = entries[0][0]
    for entry in entries[1:]:
        if entry[0] - anchor <= window:
            current.append(entry)
        else:
            out.append(current)
            current = [entry]
            anchor = entry[0]
    out.append(current)
    return out


def _split(
    cluster: Sequence[tuple[datetime, int, dict[str, Any]]],
    precedence: Sequence[str],
    keep_distinct_accepted: bool,
    accepted_status: str,
) -> tuple[list[tuple[datetime, int, dict[str, Any]]], list[tuple[datetime, int, dict[str, Any]]]]:
    """``(rows to keep, rows to suppress)`` for one cluster.

    Normally one row survives: the most decisive outcome, earliest wins a tie.

    The exception is real work. Two offers that BOTH became Towbook jobs, with
    two different job numbers, are two jobs -- Towbook does not issue a number
    until work is opened. Collapsing those would understate what the company
    did, so each is kept and the undecided offers around them are what
    collapse.
    """
    ordered = sorted(cluster, key=lambda item: (_rank(_status_of(item[2]), precedence), item[0]))

    if keep_distinct_accepted:
        seen: set[str] = set()
        keepers: list[tuple[datetime, int, dict[str, Any]]] = []
        for entry in ordered:
            if _status_of(entry[2]) != accepted_status:
                continue
            number = _job_number_of(entry[2])
            if number and number not in seen:
                seen.add(number)
                keepers.append(entry)
        if len(keepers) > 1:
            keepers.sort(key=lambda item: item[0])
            keeper_ids = {id(entry[2]) for entry in keepers}
            dropped = [entry for entry in cluster if id(entry[2]) not in keeper_ids]
            return keepers, dropped

    winner = ordered[0]
    dropped = [entry for entry in cluster if entry[1] != winner[1]]
    return [winner], dropped


def _reference(row: Mapping[str, Any]) -> dict[str, str]:
    """How a suppressed offer is named in the survivor's ``duplicate_of``.

    The Towbook reference, so a reader can pull the suppressed offer up in the
    portal and see for themselves that it was the same job -- and its outcome,
    because "collapsed 2 more, both Expired" is the sentence that makes the
    rule auditable from the report alone.
    """
    return {
        "ref": str(row.get("towbook_ref") or row.get("request_id") or ""),
        "status": _status_of(row) or "unknown",
    }


def summarize(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Rebuild the collapse report from rows that have already been collapsed.

    Every consumer of :func:`collapse` -- the daily metrics, the missed-work
    document, four dashboard views -- needs to state what was suppressed, and
    they are reached through half a dozen call sites that only ever see the
    surviving rows. Rather than thread the report through all of them, the
    survivors carry enough to reconstruct it: ``duplicate_count`` and the
    outcome of every offer each one stands for.

    Counts SUPPRESSED offers, so ``offers_before`` is the number Towbook
    actually made and ``offers_after`` is the number of real jobs.
    """
    materialised = list(rows)
    suppressed_by_status: Counter[str] = Counter()
    clusters = 0
    suppressed = 0

    for row in materialised:
        others = row.get("duplicate_of") or []
        if not others:
            continue
        clusters += 1
        suppressed += len(others)
        for other in others:
            if isinstance(other, Mapping):
                suppressed_by_status[str(other.get("status") or "unknown")] += 1
            else:
                suppressed_by_status["unknown"] += 1

    report = empty_report("collapsed" if suppressed else "no_duplicates")
    report["suppressed"] = suppressed
    report["clusters"] = clusters
    report["offers_after"] = len(materialised)
    report["offers_before"] = len(materialised) + suppressed
    report["by_status"] = dict(sorted(suppressed_by_status.items()))
    return report
