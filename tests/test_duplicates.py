"""One job offered twice is counted once -- and nothing else is.

A dedupe rule is the most dangerous kind of reporting code there is, because
both of its failure modes are silent. Collapse too little and the report
inflates every count; collapse too much and real work disappears with nothing
on screen to say so. So the tests here are as interested in what must NOT
collapse as in what must:

* two different cars, two different clubs, or an offer outside the window are
  never one job;
* a row with no vehicle is never a duplicate of anything, however many other
  rows also have no vehicle;
* two offers that both became real Towbook jobs are two pieces of work;
* and every collapse is reported, because a total that quietly shrank by 8% is
  a total nobody can reconcile against the portal.

The window arithmetic is checked at the boundary, in both directions, because
"within 60 minutes" is exactly the kind of rule that ships with a `<` where it
needed a `<=`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import count_rows, get_request, ingest_file, make_row
from fixture_generator import write_rows_xlsx
from towbook_agent.agents import duplicates
from towbook_agent.core.models import Request

BASE = datetime(2026, 7, 20, 9, 0, 0)


@pytest.fixture
def missed_work():
    """The agent, loaded the way every other suite loads it."""
    from conftest import load_agent

    return load_agent("missed_work")


def offer(
    request_id: str,
    *,
    minutes: int = 0,
    vehicle: str = "2015 Honda Accord Black",
    client: str = "agero",
    status: str = "expired",
    job_number: str | None = None,
) -> dict[str, Any]:
    """One row in the shape the collapse function is given by its callers."""
    return {
        "request_id": request_id,
        "towbook_ref": request_id,
        "client_key": client,
        "vehicle": vehicle,
        "status": status,
        "job_number": job_number,
        "offered_at_utc": (BASE + timedelta(minutes=minutes)).isoformat(),
    }


def collapse(rows, **overrides):
    config = duplicates.config_for(None)
    config.update(overrides)
    return duplicates.collapse(rows, config=config)


# --------------------------------------------------------------------------
# What is one job
# --------------------------------------------------------------------------


def test_the_same_car_offered_twice_by_one_club_is_one_job() -> None:
    kept, report = collapse([offer("A", minutes=0), offer("B", minutes=16)])

    assert len(kept) == 1
    assert report["suppressed"] == 1
    assert report["clusters"] == 1
    assert kept[0]["duplicate_count"] == 2
    # The survivor names what it stands for, so the collapse can be audited
    # against the portal without re-running anything.
    assert [item["ref"] for item in kept[0]["duplicate_of"]] == ["B"]


def test_a_different_car_is_a_different_job() -> None:
    kept, report = collapse(
        [offer("A", minutes=0), offer("B", minutes=5, vehicle="2019 Ford F-150 White")]
    )

    assert len(kept) == 2
    assert report["suppressed"] == 0


def test_two_clubs_asking_for_the_same_car_are_two_offers() -> None:
    """The insurer and the roadside app both looking for a truck.

    Both really were offered to us, and collapsing across clients would corrupt
    the per-client acceptance rates the close-off decisions are argued from.
    """
    kept, report = collapse(
        [offer("A", minutes=0, client="agero"), offer("B", minutes=5, client="allstate")]
    )

    assert len(kept) == 2
    assert report["suppressed"] == 0


def test_punctuation_and_case_do_not_make_a_second_job() -> None:
    """The clubs are inconsistent about exactly the things that do not matter."""
    kept, _ = collapse(
        [
            offer("A", minutes=0, vehicle="2018 HONDA CR-V TOU silver"),
            offer("B", minutes=10, vehicle="2018 Honda CR V  TOU Silver"),
        ]
    )

    assert len(kept) == 1


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


def test_an_offer_on_the_window_boundary_is_still_the_same_job() -> None:
    kept, _ = collapse([offer("A", minutes=0), offer("B", minutes=60)])
    assert len(kept) == 1, "60 minutes must be inside a 60 minute window"


def test_an_offer_past_the_window_is_a_new_job() -> None:
    kept, report = collapse([offer("A", minutes=0), offer("B", minutes=61)])
    assert len(kept) == 2
    assert report["suppressed"] == 0


def test_the_window_is_anchored_not_chained() -> None:
    """Four offers 40 minutes apart are not one job spanning two hours.

    Chaining ("within 60 minutes of the previous one") would collapse the whole
    run. The window is measured from the first offer of each cluster, so this
    is two jobs: 0/40 and 80/120.
    """
    kept, report = collapse(
        [
            offer("A", minutes=0),
            offer("B", minutes=40),
            offer("C", minutes=80),
            offer("D", minutes=120),
        ]
    )

    assert len(kept) == 2
    assert report["clusters"] == 2
    assert [row["request_id"] for row in kept] == ["A", "C"]


# --------------------------------------------------------------------------
# Which offer survives
# --------------------------------------------------------------------------


def test_the_accepted_offer_represents_the_job() -> None:
    """Asked twice, taken the second time: that job was won, not missed."""
    kept, _ = collapse(
        [offer("A", minutes=0, status="expired"), offer("B", minutes=20, status="accepted")]
    )

    assert len(kept) == 1
    assert kept[0]["request_id"] == "B"
    assert kept[0]["status"] == "accepted"


def test_a_decline_beats_a_no_response() -> None:
    """We were asked once and we said no.

    Recording this as no-response would send the blind-spot analysis after a
    staffing gap that was never there.
    """
    kept, _ = collapse(
        [offer("A", minutes=0, status="expired"), offer("B", minutes=20, status="denied")]
    )

    assert kept[0]["status"] == "denied"


def test_the_earliest_offer_wins_a_tie() -> None:
    kept, _ = collapse(
        [offer("A", minutes=0, status="expired"), offer("B", minutes=20, status="expired")]
    )

    assert kept[0]["request_id"] == "A"


def test_the_precedence_order_is_configuration_not_code() -> None:
    """Re-rank the outcomes in rules.yaml and the same rows resolve differently."""
    rows = [offer("A", minutes=0, status="expired"), offer("B", minutes=20, status="denied")]

    default, _ = collapse(rows)
    reversed_order, _ = collapse(
        rows, outcome_precedence=["expired", "denied", "canceled", "accepted", "pending"]
    )

    assert default[0]["status"] == "denied"
    assert reversed_order[0]["status"] == "expired"


# --------------------------------------------------------------------------
# What must never collapse
# --------------------------------------------------------------------------


def test_a_row_with_no_vehicle_is_never_a_duplicate() -> None:
    """THE SAFETY PROPERTY.

    Blank keys are how a dedupe rule quietly eats unrelated records: without
    the guard these three rows all key identically and become one. They are
    passed through untouched and counted as unkeyed.
    """
    kept, report = collapse(
        [
            offer("A", minutes=0, vehicle=""),
            offer("B", minutes=5, vehicle=""),
            offer("C", minutes=10, vehicle=None),
        ]
    )

    assert len(kept) == 3
    assert report["suppressed"] == 0
    assert report["unkeyed"] == 3


def test_two_offers_that_both_became_jobs_are_two_jobs() -> None:
    """Towbook issues a job number when work is opened, so two numbers is two
    pieces of work. Collapsing them would understate what the company did."""
    kept, report = collapse(
        [
            offer("A", minutes=0, status="accepted", job_number="125169"),
            offer("B", minutes=30, status="accepted", job_number="125172"),
        ]
    )

    assert len(kept) == 2
    assert report["suppressed"] == 0
    assert report["kept_distinct_accepted"] == 1


def test_the_undecided_offers_around_two_real_jobs_still_collapse() -> None:
    kept, report = collapse(
        [
            offer("A", minutes=0, status="expired"),
            offer("B", minutes=10, status="accepted", job_number="125169"),
            offer("C", minutes=30, status="accepted", job_number="125172"),
        ]
    )

    assert sorted(row["request_id"] for row in kept) == ["B", "C"]
    assert report["suppressed"] == 1


def test_an_accepted_offer_that_never_got_a_number_is_not_a_second_job() -> None:
    """No job number means Towbook never opened the work."""
    kept, _ = collapse(
        [
            offer("A", minutes=0, status="accepted", job_number="125169"),
            offer("B", minutes=30, status="accepted", job_number=None),
        ]
    )

    assert len(kept) == 1
    assert kept[0]["request_id"] == "A"


# --------------------------------------------------------------------------
# The rule is configuration
# --------------------------------------------------------------------------


def test_the_rule_can_be_turned_off() -> None:
    kept, report = collapse([offer("A", minutes=0), offer("B", minutes=16)], enabled=False)

    assert len(kept) == 2
    assert report["suppressed"] == 0
    assert report["enabled"] is False
    assert report["reason"] == "disabled"
    # Even switched off, every row carries the key its consumers read.
    assert all(row["duplicate_count"] == 1 for row in kept)


def test_a_shorter_window_splits_what_a_longer_one_joined() -> None:
    rows = [offer("A", minutes=0), offer("B", minutes=16)]

    assert len(collapse(rows, window_minutes=60)[0]) == 1
    assert len(collapse(rows, window_minutes=10)[0]) == 2


def test_a_partial_config_block_is_completed_not_rejected() -> None:
    """Editing one number in rules.yaml must not empty the rest of the rule."""
    config = duplicates.config_for({"duplicate_offers": {"window_minutes": 30}})

    assert config["window_minutes"] == 30
    assert config["match_fields"] == ["client_key", "vehicle"]
    assert config["enabled"] is True


def test_a_rule_with_no_match_fields_turns_itself_off() -> None:
    """Every row would otherwise share the empty key and become one job."""
    config = duplicates.config_for({"duplicate_offers": {"match_fields": []}})

    assert config["enabled"] is False


def test_the_shipped_rules_file_configures_the_rule() -> None:
    from towbook_agent.core.config_loader import get_rules

    config = duplicates.config_for(get_rules())

    assert config["enabled"] is True
    assert config["window_minutes"] == 60
    assert "vehicle" in config["match_fields"]
    assert "vehicle" in config["require_fields"]


# --------------------------------------------------------------------------
# Reporting: nothing is silent
# --------------------------------------------------------------------------


def test_the_collapse_is_reported_by_outcome() -> None:
    """"12 collapsed" is a curiosity; "9 of them declines" is why the decline
    count in this report is lower than the one in the portal."""
    kept, report = collapse(
        [
            offer("A", minutes=0, status="denied"),
            offer("B", minutes=10, status="denied"),
            offer("C", minutes=20, status="expired"),
        ]
    )

    assert len(kept) == 1
    assert report["by_status"] == {"denied": 1, "expired": 1}
    assert report["offers_before"] == 3
    assert report["offers_after"] == 1


def test_the_report_can_be_rebuilt_from_the_surviving_rows() -> None:
    """Six call sites see only the survivors; they still have to state the
    collapse, so it has to be reconstructable from the rows alone."""
    kept, report = collapse(
        [offer("A", minutes=0, status="denied"), offer("B", minutes=10, status="expired")]
    )

    rebuilt = duplicates.summarize(kept)

    assert rebuilt["suppressed"] == report["suppressed"] == 1
    assert rebuilt["clusters"] == report["clusters"] == 1
    assert rebuilt["by_status"] == report["by_status"]
    assert rebuilt["offers_before"] == 2


def test_an_ordinary_window_is_left_exactly_as_it_arrived() -> None:
    rows = [offer(f"R{index}", minutes=index * 90) for index in range(5)]

    kept, report = collapse(rows)

    assert [row["request_id"] for row in kept] == [row["request_id"] for row in rows]
    assert report["suppressed"] == 0
    assert report["reason"] == "no_duplicates"


# --------------------------------------------------------------------------
# End to end: through ingestion and into the numbers
# --------------------------------------------------------------------------


@pytest.fixture
def repeated_export(tmp_path: Path) -> Path:
    """A club that asked three times for one car, plus two unrelated offers."""
    car = "2015 Honda Accord Black"
    rows = [
        make_row("DR-1", status="Expired", offered_at=BASE, **{"Vehicle": car}),
        make_row(
            "DR-2",
            status="Expired",
            offered_at=BASE + timedelta(minutes=18),
            **{"Vehicle": car},
        ),
        make_row(
            "DR-3",
            status="Rejected",
            denial_reason="No Drivers Available",
            offered_at=BASE + timedelta(minutes=35),
            **{"Vehicle": car},
        ),
        make_row(
            "DR-4",
            status="Accepted",
            offered_at=BASE + timedelta(minutes=40),
            **{"Vehicle": "2019 Ford F-150 White"},
        ),
        make_row(
            "DR-5",
            status="Accepted",
            offered_at=BASE + timedelta(minutes=50),
            **{"Vehicle": "2021 Ram 2500 Red"},
        ),
    ]
    return write_rows_xlsx(tmp_path / "repeats.xlsx", rows)


def test_every_offer_is_still_stored(ingestion, repeated_export: Path) -> None:
    """NOTHING IS DELETED. The collapse is a read-time rule, and the raw record
    stays the truth -- which is what lets the window be re-cut in rules.yaml
    without re-pulling anything."""
    _, error = ingest_file(ingestion, repeated_export, run_id="run-dupes")

    assert error is None
    assert count_rows(Request) == 5
    assert get_request("DR-2") is not None


def test_the_daily_numbers_count_the_repeated_job_once(
    metrics, ingestion, repeated_export: Path
) -> None:
    _, error = ingest_file(ingestion, repeated_export, run_id="run-dupes")
    assert error is None

    result = metrics.compute_daily(BASE.date(), persist=False)
    totals = result["totals"]

    # Five offers in the log, three jobs: the repeated one, and the two others.
    assert totals["offered"] == 3
    assert totals["accepted"] == 2
    # The repeated job ended in a decline, which beats the two no-responses.
    assert totals["denied"] == 1
    assert totals["expired"] == 0

    duplicates_block = result["duplicates"]
    assert duplicates_block["suppressed"] == 2
    assert duplicates_block["clusters"] == 1
    assert duplicates_block["by_status"] == {"expired": 2}


def test_the_missed_work_document_counts_it_once_too(
    missed_work, ingestion, repeated_export: Path
) -> None:
    _, error = ingest_file(ingestion, repeated_export, run_id="run-dupes")
    assert error is None

    document = missed_work.compute_missed_work(
        None,
        datetime(2026, 7, 20),
        datetime(2026, 7, 21),
        period_type="daily",
        persist=False,
    )

    assert document["totals"]["offers"] == 3
    assert document["totals"]["missed"] == 1
    assert document["duplicates"]["suppressed"] == 2

    jobs = document["missed_jobs"]
    assert len(jobs) == 1
    # The job list says the club asked three times, so a reader who remembers
    # three pages for one car can see the report knows it was one job.
    assert jobs[0]["duplicate_count"] == 3


def test_the_dashboard_and_the_agents_agree(
    ingestion, repeated_export: Path
) -> None:
    """If one of them counted the repeat and the other did not, the board and
    the emailed report would disagree about how many jobs a day held."""
    from towbook_agent.web import queries as q

    _, error = ingest_file(ingestion, repeated_export, run_id="run-dupes")
    assert error is None

    rows = q.fetch_requests(*q.local_day_bounds(BASE.date()))

    assert len(rows) == 3
    repeated = next(row for row in rows if row["duplicate_count"] > 1)
    assert repeated["duplicate_count"] == 3
    assert duplicates.summarize(rows)["suppressed"] == 2
