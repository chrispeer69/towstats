"""Metric computation, against numbers worked out by hand.

Every expected value below was counted by hand from :data:`DAY_PLAN`, not
produced by running the code and pasting the answer. That is the only way a
metrics test means anything.

Two properties matter as much as the arithmetic:

* a window with no offers has an acceptance rate of **None**, not 0. Zero
  percent means "we turned everything down"; there is no rate at all when
  nothing was offered, and the difference is visible on the dashboard.
* recomputation is idempotent -- hard constraint #4. The metrics tables carry a
  unique key on their window column precisely so a re-run upserts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

import pytest
from sqlalchemy import select

from conftest import count_rows, dig
from towbook_agent.core.config_loader import rules_version
from towbook_agent.core.db import get_session
from towbook_agent.core.models import (
    ClientDaily,
    MetricsDaily,
    MetricsHourly,
    MetricsWeekly,
    Request,
    client_key_for,
)

# --------------------------------------------------------------------------
# The hand-counted dataset
#
# 2026-07-20 is a Monday. TZ is UTC in the suite, so these local hours are also
# the stored UTC hours and the arithmetic below is direct.
#
#   hour | Agero                        | Quest Roadside
#   -----+------------------------------+------------------------------
#   08   | 4 acc, 1 den, 1 exp   (6)    | 2 acc, 1 den, 1 can   (4)
#   12   | 3 acc, 1 den, 1 exp   (5)    | 2 acc, 1 den          (3)
#   14   | 5 acc, 1 den, 1 can   (7)    | 4 acc, 1 den          (5)
#   16   | 1 acc, 2 den, 1 exp   (4)    | 0 acc, 1 den, 1 exp   (2)
#
#   hour 08 -> 10 offered,  6 accepted
#   hour 12 ->  8 offered,  5 accepted
#   hour 14 -> 12 offered,  9 accepted   <- the window under test
#   hour 16 ->  6 offered,  1 accepted   <- after it; must not leak in
#   day     -> 36 offered, 21 accepted
#
#   Agero  day: 22 offered, 13 accepted, 5 denied, 3 expired, 1 canceled
#   Quest  day: 14 offered,  8 accepted, 4 denied, 1 expired, 1 canceled
# --------------------------------------------------------------------------

DAY = date(2026, 7, 20)
WEEK_START = date(2026, 7, 20)  # Monday
AGERO = "Agero"
QUEST = "Quest Roadside"

DAY_PLAN: tuple[tuple[int, str, dict[str, int]], ...] = (
    (8, AGERO, {"accepted": 4, "denied": 1, "expired": 1}),
    (8, QUEST, {"accepted": 2, "denied": 1, "canceled": 1}),
    (12, AGERO, {"accepted": 3, "denied": 1, "expired": 1}),
    (12, QUEST, {"accepted": 2, "denied": 1}),
    (14, AGERO, {"accepted": 5, "denied": 1, "canceled": 1}),
    (14, QUEST, {"accepted": 4, "denied": 1}),
    (16, AGERO, {"accepted": 1, "denied": 2, "expired": 1}),
    (16, QUEST, {"denied": 1, "expired": 1}),
)

HOUR_14 = datetime(2026, 7, 20, 14, 0, 0)
EMPTY_HOUR = datetime(2026, 7, 20, 3, 0, 0)

DAY_OFFERED = 36
DAY_ACCEPTED = 21
HOUR_14_OFFERED = 12
HOUR_14_ACCEPTED = 9
RUNNING_OFFERED = 30  # hours 08 + 12 + 14
RUNNING_ACCEPTED = 20  # 6 + 5 + 9


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _service_class_for(index: int) -> str:
    """Deterministic spread over the service classes, so the daily breakdown
    has something to break down. Position in the plan decides it."""
    return ("tow", "tow", "light_service", "winch_out", "unclassified")[index % 5]


def add_requests(rows: Iterable[dict[str, Any]]) -> None:
    """Insert Request rows directly.

    Metrics are tested without going through ingestion on purpose: a metrics
    failure should not be maskable by an ingestion bug, or vice versa.
    """
    with get_session() as session:
        for row in rows:
            session.add(Request(**row))


def build_day_rows(
    plan: tuple[tuple[int, str, dict[str, int]], ...] = DAY_PLAN,
    day: date = DAY,
    prefix: str = "R",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    counter = 0
    for hour, client, statuses in plan:
        minute = 0
        for status, count in statuses.items():
            for _ in range(count):
                counter += 1
                minute = (minute + 7) % 60
                offered_at = datetime(day.year, day.month, day.day, hour, minute, 0)
                rows.append(
                    {
                        "request_id": f"{prefix}-{day:%Y%m%d}-{counter:04d}",
                        "account_id": "default",
                        "client_name": client,
                        "client_key": client_key_for(client),
                        "offered_at": offered_at,
                        "responded_at": offered_at + timedelta(minutes=2),
                        "status": status,
                        "service_type_raw": "Flatbed Tow",
                        "service_class": _service_class_for(counter),
                        "source_run_id": "seed",
                    }
                )
    return rows


@pytest.fixture
def seeded_day() -> list[dict[str, Any]]:
    rows = build_day_rows()
    add_requests(rows)
    return rows


def _hourly_row(window_start: datetime) -> MetricsHourly | None:
    with get_session(commit=False) as session:
        return session.execute(
            select(MetricsHourly).where(MetricsHourly.window_start == window_start)
        ).scalar_one_or_none()


def _daily_row(day: date) -> MetricsDaily | None:
    with get_session(commit=False) as session:
        return session.execute(
            select(MetricsDaily).where(MetricsDaily.date == day)
        ).scalar_one_or_none()


def _weekly_row(week_start: date) -> MetricsWeekly | None:
    with get_session(commit=False) as session:
        return session.execute(
            select(MetricsWeekly).where(MetricsWeekly.week_start == week_start)
        ).scalar_one_or_none()


def _client_daily(day: date) -> dict[str, ClientDaily]:
    with get_session(commit=False) as session:
        rows = session.execute(
            select(ClientDaily).where(ClientDaily.date == day)
        ).scalars()
        return {row.client_key: row for row in rows}


# --------------------------------------------------------------------------
# Sanity: the plan says what I think it says
# --------------------------------------------------------------------------


def test_the_seeded_day_matches_the_hand_count(seeded_day) -> None:
    assert len(seeded_day) == DAY_OFFERED
    assert sum(1 for row in seeded_day if row["status"] == "accepted") == DAY_ACCEPTED
    assert count_rows(Request) == DAY_OFFERED


# --------------------------------------------------------------------------
# Hourly
# --------------------------------------------------------------------------


def test_compute_hourly_counts_only_its_own_hour(metrics, seeded_day) -> None:
    result = metrics.compute_hourly(HOUR_14)

    assert dig(result, "offered") == HOUR_14_OFFERED
    assert dig(result, "accepted") == HOUR_14_ACCEPTED
    assert dig(result, "rate") == pytest.approx(0.75)


def test_compute_hourly_persists_the_row(metrics, seeded_day) -> None:
    metrics.compute_hourly(HOUR_14)

    row = _hourly_row(HOUR_14)
    assert row is not None, "compute_hourly must persist as well as return"
    assert row.offered == HOUR_14_OFFERED
    assert row.accepted == HOUR_14_ACCEPTED
    assert float(row.rate) == pytest.approx(0.75)
    assert row.rules_version == rules_version()
    assert row.computed_at is not None


def test_hourly_carries_the_running_day_totals(metrics, seeded_day) -> None:
    """The SMS reads "Day: 84 / 61 (73%)" -- running to the end of this hour,
    not the whole day, so the 16:00 rows must not be included."""
    metrics.compute_hourly(HOUR_14)

    row = _hourly_row(HOUR_14)
    assert row.day_running_offered == RUNNING_OFFERED
    assert row.day_running_accepted == RUNNING_ACCEPTED
    assert float(row.day_running_rate) == pytest.approx(RUNNING_ACCEPTED / RUNNING_OFFERED, abs=1e-3)


def test_hour_boundaries_are_half_open(metrics) -> None:
    """[14:00:00, 15:00:00). One second either side decides the bucket."""
    add_requests(
        [
            {
                "request_id": "EDGE-BEFORE",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 13, 59, 59),
                "status": "accepted",
                "service_class": "tow",
                "service_type_raw": "Tow",
            },
            {
                "request_id": "EDGE-START",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 14, 0, 0),
                "status": "accepted",
                "service_class": "tow",
                "service_type_raw": "Tow",
            },
            {
                "request_id": "EDGE-END",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 14, 59, 59),
                "status": "denied",
                "service_class": "tow",
                "service_type_raw": "Tow",
            },
            {
                "request_id": "EDGE-AFTER",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 15, 0, 0),
                "status": "accepted",
                "service_class": "tow",
                "service_type_raw": "Tow",
            },
        ]
    )

    metrics.compute_hourly(HOUR_14)
    row = _hourly_row(HOUR_14)
    assert row.offered == 2
    assert row.accepted == 1


def test_zero_offer_hour_has_no_rate_at_all(metrics, seeded_day) -> None:
    """None, not 0. Nothing was offered, so there is nothing to be a rate of."""
    result = metrics.compute_hourly(EMPTY_HOUR)

    assert dig(result, "offered") == 0
    assert dig(result, "accepted") == 0
    assert dig(result, "rate") is None, "an empty window must report rate None, not 0"


def test_zero_offer_hour_still_persists_a_row(metrics, seeded_day) -> None:
    """The dashboard needs the gap to exist, and the column is NOT NULL."""
    metrics.compute_hourly(EMPTY_HOUR)

    row = _hourly_row(EMPTY_HOUR)
    assert row is not None
    assert row.offered == 0
    assert row.accepted == 0
    assert float(row.rate) == 0.0


def test_a_pending_offer_counts_as_offered_but_not_accepted(metrics) -> None:
    """An export taken mid-hour always contains live offers.

    They were offered -- they belong in the denominator. They have not been
    accepted, so they are not in the numerator. Dropping them entirely would
    quietly flatter the acceptance rate of the most recent hour, which is the
    number the owner looks at most.
    """
    add_requests(
        [
            {
                "request_id": f"PEND-{index}",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 14, index * 10, 0),
                "status": status,
                "service_class": "tow",
                "service_type_raw": "Tow",
            }
            for index, status in enumerate(["accepted", "accepted", "pending", "denied"])
        ]
    )

    result = metrics.compute_hourly(HOUR_14)

    assert dig(result, "offered") == 4
    assert dig(result, "accepted") == 2
    assert dig(result, "rate") == pytest.approx(0.5)


def test_compute_hourly_is_idempotent(metrics, seeded_day) -> None:
    first = metrics.compute_hourly(HOUR_14)
    row_after_first = _hourly_row(HOUR_14)
    snapshot = (
        row_after_first.offered,
        row_after_first.accepted,
        float(row_after_first.rate),
        row_after_first.day_running_offered,
        row_after_first.day_running_accepted,
    )

    second = metrics.compute_hourly(HOUR_14)
    row_after_second = _hourly_row(HOUR_14)

    assert count_rows(MetricsHourly) == 1, "recomputing inserted a duplicate row"
    assert (
        row_after_second.offered,
        row_after_second.accepted,
        float(row_after_second.rate),
        row_after_second.day_running_offered,
        row_after_second.day_running_accepted,
    ) == snapshot
    assert dig(first, "offered") == dig(second, "offered")
    assert dig(first, "accepted") == dig(second, "accepted")


def test_recomputing_after_late_data_updates_in_place(metrics, seeded_day) -> None:
    """Towbook backfills. The second run must correct the row, not add one."""
    metrics.compute_hourly(HOUR_14)
    assert _hourly_row(HOUR_14).offered == HOUR_14_OFFERED

    add_requests(
        [
            {
                "request_id": "LATE-1",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 14, 45, 0),
                "status": "accepted",
                "service_class": "tow",
                "service_type_raw": "Tow",
            }
        ]
    )
    metrics.compute_hourly(HOUR_14)

    assert count_rows(MetricsHourly) == 1
    row = _hourly_row(HOUR_14)
    assert row.offered == HOUR_14_OFFERED + 1
    assert row.accepted == HOUR_14_ACCEPTED + 1


# --------------------------------------------------------------------------
# Daily
# --------------------------------------------------------------------------


def test_compute_daily_totals(metrics, seeded_day) -> None:
    result = metrics.compute_daily(DAY)

    assert dig(result, "offered") == DAY_OFFERED
    assert dig(result, "accepted") == DAY_ACCEPTED
    assert dig(result, "rate") == pytest.approx(DAY_ACCEPTED / DAY_OFFERED, abs=1e-4)


def test_compute_daily_persists_a_blob_with_its_rules_version(metrics, seeded_day) -> None:
    metrics.compute_daily(DAY)

    row = _daily_row(DAY)
    assert row is not None
    assert isinstance(row.metrics, dict) and row.metrics, "metrics JSON is empty"
    assert row.rules_version == rules_version(), (
        "every stored number must be traceable to the rules that produced it"
    )
    assert dig(row.metrics, "offered") == DAY_OFFERED
    assert dig(row.metrics, "accepted") == DAY_ACCEPTED


def test_compute_daily_ignores_the_neighbouring_days(metrics, seeded_day) -> None:
    add_requests(build_day_rows(day=DAY - timedelta(days=1), prefix="PREV"))
    add_requests(build_day_rows(day=DAY + timedelta(days=1), prefix="NEXT"))

    result = metrics.compute_daily(DAY)
    assert dig(result, "offered") == DAY_OFFERED


def test_compute_daily_is_idempotent(metrics, seeded_day) -> None:
    first = metrics.compute_daily(DAY)
    blob_after_first = dict(_daily_row(DAY).metrics)

    second = metrics.compute_daily(DAY)

    assert count_rows(MetricsDaily) == 1
    assert dict(_daily_row(DAY).metrics) == blob_after_first
    assert dig(first, "offered") == dig(second, "offered")


def test_empty_day_has_no_rate(metrics) -> None:
    result = metrics.compute_daily(date(2026, 1, 1))

    assert dig(result, "offered") == 0
    assert dig(result, "rate") is None


def test_daily_breaks_down_by_service_class(metrics, seeded_day) -> None:
    """The daily report answers "how many tows did we turn down", so the blob
    has to carry a per service class view somewhere inside it."""
    metrics.compute_daily(DAY)
    blob = _daily_row(DAY).metrics

    entry = _find_breakdown_entry(blob, "tow")
    assert entry is not None, (
        f"no per service class breakdown found in the daily metrics: {sorted(blob)}"
    )
    expected = sum(1 for row in seeded_day if row["service_class"] == "tow")
    assert _count_of(entry) == expected


def test_daily_breaks_down_by_client(metrics, seeded_day) -> None:
    metrics.compute_daily(DAY)
    blob = _daily_row(DAY).metrics

    for key in (client_key_for(AGERO), AGERO):
        entry = _find_breakdown_entry(blob, key)
        if entry is not None:
            assert _count_of(entry) == 22
            return
    raise AssertionError(f"no per client breakdown found in the daily metrics: {sorted(blob)}")


# --------------------------------------------------------------------------
# client_daily
# --------------------------------------------------------------------------


def test_client_daily_rows_are_written(metrics, seeded_day) -> None:
    metrics.compute_daily(DAY)

    rows = _client_daily(DAY)
    assert set(rows) == {client_key_for(AGERO), client_key_for(QUEST)}

    agero = rows[client_key_for(AGERO)]
    assert (agero.offered, agero.accepted, agero.denied, agero.expired, agero.canceled) == (
        22,
        13,
        5,
        3,
        1,
    )
    assert float(agero.rate) == pytest.approx(13 / 22, abs=1e-3)

    quest = rows[client_key_for(QUEST)]
    assert (quest.offered, quest.accepted, quest.denied, quest.expired, quest.canceled) == (
        14,
        8,
        4,
        1,
        1,
    )
    assert float(quest.rate) == pytest.approx(8 / 14, abs=1e-3)


def test_client_daily_is_idempotent(metrics, seeded_day) -> None:
    metrics.compute_daily(DAY)
    metrics.compute_daily(DAY)

    assert count_rows(ClientDaily) == 2, "recomputing duplicated the per client rows"


# --------------------------------------------------------------------------
# Weekly
# --------------------------------------------------------------------------


def test_compute_weekly_spans_monday_to_sunday(metrics) -> None:
    # Monday of the week under test, its Sunday, and one day either side.
    add_requests(build_day_rows(day=date(2026, 7, 20), prefix="MON"))
    add_requests(build_day_rows(day=date(2026, 7, 26), prefix="SUN"))
    add_requests(build_day_rows(day=date(2026, 7, 19), prefix="PREV"))  # Sunday before
    add_requests(build_day_rows(day=date(2026, 7, 27), prefix="NEXT"))  # Monday after

    result = metrics.compute_weekly(WEEK_START)

    assert dig(result, "offered") == DAY_OFFERED * 2
    assert dig(result, "accepted") == DAY_ACCEPTED * 2


def test_compute_weekly_persists_and_is_idempotent(metrics, seeded_day) -> None:
    metrics.compute_weekly(WEEK_START)
    row = _weekly_row(WEEK_START)
    assert row is not None
    assert row.rules_version == rules_version()
    blob = dict(row.metrics)

    metrics.compute_weekly(WEEK_START)

    assert count_rows(MetricsWeekly) == 1
    assert dict(_weekly_row(WEEK_START).metrics) == blob


def test_empty_week_has_no_rate(metrics) -> None:
    result = metrics.compute_weekly(date(2026, 1, 5))  # a Monday with no data

    assert dig(result, "offered") == 0
    assert dig(result, "rate") is None


# --------------------------------------------------------------------------
# Helpers for the loosely specified JSON blobs
# --------------------------------------------------------------------------


#: Fields a breakdown record might identify itself by.
_LABEL_FIELDS = ("service_class", "client_key", "client", "name", "key", "id", "label")


def _find_breakdown_entry(blob: Any, label: str, depth: int = 0) -> Any:
    """Find the entry for ``label`` in a breakdown, wherever it lives.

    The daily/weekly ``metrics`` column is deliberately a JSON document so a new
    report dimension does not need a migration, which means its exact shape is
    not part of the contract -- a breakdown may be a mapping keyed by name or a
    list of records that name themselves. The *presence* of the breakdown is
    what is being asserted, and the count inside it.
    """
    if depth > 6:
        return None

    if isinstance(blob, dict):
        if label in blob and not isinstance(blob.get(label), (type(None),)):
            return blob[label]
        for field in _LABEL_FIELDS:
            if blob.get(field) == label:
                return blob
        for value in blob.values():
            found = _find_breakdown_entry(value, label, depth + 1)
            if found is not None:
                return found
        return None

    if isinstance(blob, list):
        for item in blob:
            found = _find_breakdown_entry(item, label, depth + 1)
            if found is not None:
                return found
    return None


def _count_of(value: Any) -> int:
    """A breakdown entry is either a bare count or a small dict of counters."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for name in ("offered", "count", "total", "n"):
            if name in value:
                return int(value[name])
    raise AssertionError(f"cannot read a count out of {value!r}")
