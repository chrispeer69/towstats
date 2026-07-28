"""The 7 x 24 blind-spot grid (MISSED_WORK_MODEL.md section 4).

This is the highest-value output in the system. It turns "we are missing work"
into "nobody is covering 17:00-21:00", which is a staffing conversation with
evidence attached rather than an argument. On 30 days of the owner's real
traffic it isolates 17:00-21:00 (47/46/40/55/56% unanswered) and 01:00-04:00
(49/49/59/50%) while 08:00-13:00 sits at 9-15%.

What makes it urgent: **the median response window Towbook allows is 3 minutes**
(mean 4, max 15). A missed notification is a lost job almost immediately, so an
unmanned hour is not a slow hour -- it is a zero hour.

The fixture
-----------
:data:`GRID` is a synthetic week, hand-built so that every interesting position
relative to the two thresholds is occupied by exactly one cell:

===============  ======  ===========  =====  ===========================
cell             offers  no_response  rate   why it is here
===============  ======  ===========  =====  ===========================
Mon 10:00            20            0  0.00   busy and healthy
Wed 19:00            20           12  0.60   THE HOT CELL
Thu 03:00             4            4  1.00   worst rate, too few offers
Fri 09:00            20            6  0.30   plenty of offers, under threshold
Sat 12:00            20            7  0.35   exactly ON the threshold
Sun 22:00             5            5  1.00   exactly ON min_offers
===============  ======  ===========  =====  ===========================

Shipped thresholds are ``min_offers: 5`` and ``threshold: 0.35``, and both
comparisons are ``>=``, so Sat 12:00 and Sun 22:00 must be flagged. A test that
only used comfortably-inside values would pass just as well against ``>``,
which is a real off-by-one in a report somebody will staff against.

Every expected number below was counted from the table, not read back out of
the code.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import yaml

from towbook_agent.core.config_loader import CONFIG, get_rules
from towbook_agent.core.db import get_session
from towbook_agent.core.models import Request, client_key_for

# --------------------------------------------------------------------------
# The week under test. 2026-07-20 is a MONDAY, so weekday index 0 == Mon and
# the day offsets below are also the weekday indexes. TZ is UTC in the suite,
# so these local hours are the stored UTC hours too.
# --------------------------------------------------------------------------

MONDAY = datetime(2026, 7, 20, 0, 0)
WINDOW_START = MONDAY
WINDOW_END = MONDAY + timedelta(days=7)

AGERO = "Agero"
TOW = "Light Duty Tow"

#: (weekday index, hour, offers, no_response, accepted)
#: The remainder of each cell -- offers - no_response - accepted -- is seeded as
#: a decline, so the cell totals are exactly the "offers" column.
GRID: tuple[tuple[int, int, int, int, int], ...] = (
    (0, 10, 20, 0, 20),   # Mon 10:00 -- busy, nothing missed
    (2, 19, 20, 12, 8),   # Wed 19:00 -- THE HOT CELL, 60%
    (3, 3, 4, 4, 0),      # Thu 03:00 -- 100% but only four offers
    (4, 9, 20, 6, 14),    # Fri 09:00 -- 30%, under the threshold
    (5, 12, 20, 7, 13),   # Sat 12:00 -- exactly 35%
    (6, 22, 5, 5, 0),     # Sun 22:00 -- exactly five offers
)

HOT = (2, 19)
TOO_QUIET = (3, 3)
UNDER_THRESHOLD = (4, 9)
ON_THRESHOLD = (5, 12)
ON_MIN_OFFERS = (6, 22)

#: Counted from the table above.
TOTAL_OFFERS = 20 + 20 + 4 + 20 + 20 + 5      # 89
TOTAL_NO_RESPONSE = 0 + 12 + 4 + 6 + 7 + 5    # 34
POPULATED_CELLS = len(GRID)                   # 6

#: Flagged at the shipped thresholds, worst first: ranked by jobs actually
#: lost, then by rate. 12 > 7 > 5.
EXPECTED_BLIND_SPOTS = (HOT, ON_THRESHOLD, ON_MIN_OFFERS)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def missed_work():
    from conftest import load_agent

    return load_agent("missed_work")


def _row(counter: int, when: datetime, label: str, canonical: str) -> dict[str, Any]:
    return {
        "request_id": f"BS-{counter:05d}",
        "account_id": "default",
        "client_name": AGERO,
        "client_key": client_key_for(AGERO),
        "offered_at": when,
        "status": canonical,
        "status_raw": label,
        "service_type_raw": TOW,
        "service_class": "tow",
        "denial_reason": "Equipment Not Available" if label == "Rejected" else None,
    }


def build_week() -> list[dict[str, Any]]:
    """Seed rows for :data:`GRID`, spread across the minutes of their hour."""
    rows: list[dict[str, Any]] = []
    counter = 0
    for day, hour, offers, unanswered, accepted in GRID:
        declined = offers - unanswered - accepted
        assert declined >= 0, "the fixture must not describe a cell it cannot seed"
        labels = (
            [("Expired", "expired")] * unanswered
            + [("Accepted", "accepted")] * accepted
            + [("Rejected", "denied")] * declined
        )
        for index, (label, canonical) in enumerate(labels):
            counter += 1
            when = MONDAY + timedelta(days=day, hours=hour, minutes=index % 60)
            rows.append(_row(counter, when, label, canonical))
    return rows


@pytest.fixture
def week() -> list[dict[str, Any]]:
    rows = build_week()
    with get_session() as session:
        for row in rows:
            session.add(Request(**row))
    return rows


def grid_for(missed_work) -> dict[str, Any]:
    with get_session(commit=False) as session:
        return missed_work.blind_spots(session, WINDOW_START, WINDOW_END, None)


def cell(grid: dict[str, Any], day: int, hour: int) -> dict[str, Any]:
    for entry in grid["cells"]:
        if entry["weekday_index"] == day and entry["hour"] == hour:
            return entry
    raise AssertionError(f"no cell for weekday {day} hour {hour}")


def flagged(grid: dict[str, Any]) -> set[tuple[int, int]]:
    return {(c["weekday_index"], c["hour"]) for c in grid["blind_spots"]}


def rewrite_rules(write_config, mutate) -> dict[str, Any]:
    data = yaml.safe_load((CONFIG.config_dir / "rules.yaml").read_text(encoding="utf-8"))
    mutate(data)
    write_config("rules.yaml", data)
    return get_rules()


def set_blind_spot(write_config, **values: Any) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["missed_work"]["blind_spot"].update(values)

    rewrite_rules(write_config, mutate)


# ==========================================================================
# Shape
# ==========================================================================


def test_the_grid_is_seven_days_by_twenty_four_hours(missed_work, week) -> None:
    grid = grid_for(missed_work)
    assert (grid["rows"], grid["cols"]) == (7, 24)
    assert grid["weekdays"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert grid["hours"] == list(range(24))
    for plane in ("offers", "accepted", "no_response", "no_response_rate"):
        assert len(grid[plane]) == 7, plane
        assert all(len(row) == 24 for row in grid[plane]), plane


def test_every_seeded_cell_lands_where_the_fixture_put_it(missed_work, week) -> None:
    """2026-07-20 is a Monday, so day offset and weekday index coincide."""
    grid = grid_for(missed_work)
    for day, hour, offers, unanswered, accepted in GRID:
        assert grid["offers"][day][hour] == offers, (day, hour)
        assert grid["no_response"][day][hour] == unanswered, (day, hour)
        assert grid["accepted"][day][hour] == accepted, (day, hour)


def test_only_populated_cells_are_listed(missed_work, week) -> None:
    """168 cells, six of them with traffic. An empty cell is not a data point."""
    grid = grid_for(missed_work)
    assert len(grid["cells"]) == POPULATED_CELLS
    assert grid["totals"]["offers"] == TOTAL_OFFERS
    assert grid["totals"]["no_response"] == TOTAL_NO_RESPONSE
    assert grid["totals"]["no_response_rate"] == pytest.approx(
        TOTAL_NO_RESPONSE / TOTAL_OFFERS
    )


# ==========================================================================
# Detection at the thresholds
# ==========================================================================


def test_the_shipped_thresholds_are_the_documented_ones(missed_work, week) -> None:
    assert grid_for(missed_work)["thresholds"] == {"min_offers": 5, "threshold": 0.35}


def test_the_hot_cell_is_detected(missed_work, week) -> None:
    """Wed 19:00: 20 offers, 12 unanswered, 60%. Over both thresholds."""
    grid = grid_for(missed_work)
    hot = cell(grid, *HOT)
    assert hot["offers"] == 20
    assert hot["no_response"] == 12
    assert hot["no_response_rate"] == pytest.approx(0.60)
    assert hot["is_blind_spot"] is True
    assert hot["label"] == "Wed 19:00"
    assert HOT in flagged(grid)


def test_a_cell_exactly_on_the_threshold_is_a_blind_spot(missed_work, week) -> None:
    """Sat 12:00: 7 of 20 is 0.35 exactly. The comparison is >=, not >.

    An off-by-one here silently drops the marginal hours -- which are precisely
    the ones a staffing decision is about.
    """
    spot = cell(grid_for(missed_work), *ON_THRESHOLD)
    assert spot["no_response_rate"] == pytest.approx(0.35)
    assert spot["is_blind_spot"] is True


def test_a_cell_exactly_on_min_offers_is_a_blind_spot(missed_work, week) -> None:
    """Sun 22:00: five offers is the floor, not one above it."""
    spot = cell(grid_for(missed_work), *ON_MIN_OFFERS)
    assert spot["offers"] == 5
    assert spot["is_blind_spot"] is True


def test_a_cell_below_min_offers_is_not_detected(missed_work, week) -> None:
    """Thu 03:00 misses 100% of four offers and is still not evidence.

    This is the whole job of min_offers: without it the worst-looking cell in
    every report is whichever quiet hour happened to get one offer and drop it,
    and the report becomes noise.
    """
    quiet = cell(grid_for(missed_work), *TOO_QUIET)
    assert quiet["offers"] == 4
    assert quiet["no_response_rate"] == 1.0, "the worst rate in the grid"
    assert quiet["is_blind_spot"] is False
    assert TOO_QUIET not in flagged(grid_for(missed_work))


def test_a_cell_below_the_rate_threshold_is_not_detected(missed_work, week) -> None:
    """Fri 09:00: 20 offers is plenty, 30% is not enough."""
    busy = cell(grid_for(missed_work), *UNDER_THRESHOLD)
    assert busy["no_response_rate"] == pytest.approx(0.30)
    assert busy["is_blind_spot"] is False


def test_volume_alone_never_flags_a_cell(missed_work, week) -> None:
    """Mon 10:00 is the busiest healthy cell in the grid."""
    healthy = cell(grid_for(missed_work), 0, 10)
    assert healthy["offers"] == 20
    assert healthy["no_response_rate"] == 0.0
    assert healthy["is_blind_spot"] is False


def test_exactly_the_expected_cells_are_flagged(missed_work, week) -> None:
    assert flagged(grid_for(missed_work)) == set(EXPECTED_BLIND_SPOTS)
    assert grid_for(missed_work)["blind_spot_count"] == len(EXPECTED_BLIND_SPOTS)


def test_blind_spots_are_ranked_by_jobs_lost_not_by_rate(missed_work, week) -> None:
    """Thu 03:00 has the highest rate in the grid and is not even in the list;
    of the three that are, the one that lost twelve jobs leads.

    Ranking by rate would put a 5-of-5 cell above a 12-of-20 cell, which is the
    wrong staffing decision.
    """
    grid = grid_for(missed_work)
    order = [(c["weekday_index"], c["hour"]) for c in grid["blind_spots"]]
    assert order == list(EXPECTED_BLIND_SPOTS)
    assert [c["no_response"] for c in grid["blind_spots"]] == [12, 7, 5]

    assert grid["blind_spot_no_response"] == 12 + 7 + 5
    assert grid["blind_spot_offers"] == 20 + 20 + 5


# ==========================================================================
# The thresholds are data
# ==========================================================================


def test_lowering_min_offers_admits_the_quiet_cell(missed_work, week, write_config) -> None:
    assert TOO_QUIET not in flagged(grid_for(missed_work))

    set_blind_spot(write_config, min_offers=4)

    assert TOO_QUIET in flagged(grid_for(missed_work))


def test_raising_the_rate_threshold_drops_the_hot_cell(
    missed_work, week, write_config
) -> None:
    assert HOT in flagged(grid_for(missed_work))

    set_blind_spot(write_config, threshold=0.61)

    spots = flagged(grid_for(missed_work))
    assert HOT not in spots
    # Sun 22:00 is at 100% and survives, so this is a threshold move rather than
    # the detector having been switched off.
    assert ON_MIN_OFFERS in spots


def test_a_threshold_of_zero_flags_every_busy_cell_including_clean_ones(
    missed_work, week, write_config
) -> None:
    """Characterisation, not endorsement: DO NOT SET ``threshold: 0.0``.

    The rule is ``no_response_rate >= threshold`` and ``0.0 >= 0.0`` is true, so
    a threshold of zero flags Mon 10:00 -- 20 offers, every one of them answered
    -- as a blind spot. That is the documented formula applied faithfully, not a
    defect in the detector, but it is a foot-gun: the one cell in the grid that
    represents the desired behaviour gets reported as unmanned.

    Pinned here so the behaviour is known rather than discovered on a dashboard.
    If this is ever judged wrong, the fix is a ``no_response > 0`` guard in
    ``_blind_spots_from`` and this test inverts.
    """
    set_blind_spot(write_config, threshold=0.0, min_offers=1)
    healthy = cell(grid_for(missed_work), 0, 10)
    assert healthy["no_response"] == 0
    assert healthy["is_blind_spot"] is True

    # The shipped threshold is 0.35 and does the right thing, which is what
    # actually ships and what test_volume_alone_never_flags_a_cell asserts.
    set_blind_spot(write_config, threshold=0.35, min_offers=5)
    assert cell(grid_for(missed_work), 0, 10)["is_blind_spot"] is False


# ==========================================================================
# Rates
# ==========================================================================


def test_an_empty_cell_has_no_rate_rather_than_a_zero_rate(missed_work, week) -> None:
    """Divide-by-zero yields None, never 0.

    162 of the 168 cells are empty. Calling them 0% would render a quiet Tuesday
    as a perfect score and a real 0-of-0 hour as indistinguishable from an hour
    that answered everything.
    """
    grid = grid_for(missed_work)
    populated = {(day, hour) for day, hour, *_ in GRID}
    for day in range(7):
        for hour in range(24):
            if (day, hour) in populated:
                continue
            assert grid["offers"][day][hour] == 0
            assert grid["no_response_rate"][day][hour] is None, (day, hour)


def test_an_empty_hour_column_has_no_rate(missed_work, week) -> None:
    grid = grid_for(missed_work)
    quiet_hour = grid["by_hour"][0]
    assert quiet_hour["offers"] == 0
    assert quiet_hour["no_response_rate"] is None
    assert quiet_hour["is_blind_spot"] is False


def test_no_rate_in_the_grid_is_ever_a_bare_zero_for_an_empty_cell(
    missed_work, week
) -> None:
    """The stronger form: every None is an empty cell and every empty cell is a
    None. Nothing in between."""
    grid = grid_for(missed_work)
    for day in range(7):
        for hour in range(24):
            has_offers = grid["offers"][day][hour] > 0
            has_rate = grid["no_response_rate"][day][hour] is not None
            assert has_offers == has_rate, (day, hour)


# ==========================================================================
# The hour-of-day roll-up
# ==========================================================================


def test_the_hour_columns_sum_the_week(missed_work, week) -> None:
    """by_hour is the same data collapsed across days -- the cut the
    blind_spot_forming alert uses, because a single day's 19:00 cell is below
    min_offers by design."""
    grid = grid_for(missed_work)
    assert len(grid["by_hour"]) == 24

    hour19 = grid["by_hour"][19]
    assert hour19["offers"] == 20
    assert hour19["no_response"] == 12
    assert hour19["no_response_rate"] == pytest.approx(0.60)
    assert hour19["is_blind_spot"] is True

    assert sum(entry["offers"] for entry in grid["by_hour"]) == TOTAL_OFFERS
    assert sum(entry["no_response"] for entry in grid["by_hour"]) == TOTAL_NO_RESPONSE


def test_worst_hours_is_the_flagged_subset_of_by_hour(missed_work, week) -> None:
    grid = grid_for(missed_work)
    assert [entry["hour"] for entry in grid["worst_hours"]] == [12, 19, 22]


# ==========================================================================
# Windowing and determinism
# ==========================================================================


def test_offers_outside_the_window_are_not_counted(missed_work, week) -> None:
    """The window is half-open, so the first instant of the next week belongs to
    the next week."""
    with get_session() as session:
        session.add(
            Request(
                **_row(90001, WINDOW_END, "Expired", "expired")
            )
        )
    grid = grid_for(missed_work)
    assert grid["totals"]["offers"] == TOTAL_OFFERS


def test_the_grid_is_identical_when_recomputed(missed_work, week) -> None:
    """Determinism: no LLM, no heuristics, no dict-ordering surprises. This is
    what makes the stored missed-work blob byte-comparable."""
    first = grid_for(missed_work)
    second = grid_for(missed_work)
    assert first == second


def test_the_grid_matches_the_copy_embedded_in_the_document(missed_work, week) -> None:
    """blind_spots() standalone and compute_missed_work()['blind_spots'] must not
    be allowed to drift apart -- the dashboard reads one and the report the
    other."""
    standalone = grid_for(missed_work)
    with get_session(commit=False) as session:
        embedded = missed_work.compute_missed_work(
            session, WINDOW_START, WINDOW_END, None, persist=False
        )["blind_spots"]

    # blind_spots() stamps the window it was asked for; the embedded copy takes
    # the document's. That one key aside, they must be the same grid.
    assert set(standalone) - set(embedded) == {"window"}
    assert {k: v for k, v in standalone.items() if k != "window"} == embedded
    assert standalone["window"]["start"] == "2026-07-20T00:00:00"


def test_the_report_carries_the_three_minute_context(missed_work, week) -> None:
    """The grid without that fact reads as "some offers were slow". With it, an
    unmanned hour is a lost hour."""
    note = grid_for(missed_work)["response_window_note"]
    assert "3 minutes" in note
