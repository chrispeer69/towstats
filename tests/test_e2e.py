"""The whole pipeline, offline: XLSX -> ingest -> classify -> metrics.

Nothing here touches the portal. The fixture generator stands in for
acquisition, which is the entire reason it exists -- everything downstream of
the download can be proved correct without a login.

Expected values are not hardcoded. They come from
:func:`fixture_generator.summarize_rows`, which counts the generated rows
directly and never imports any pipeline code, so the assertions compare two
independent implementations rather than the code against itself.

The acceptance criteria checked end to end:

* every exported row is stored, once, with its verbatim service type;
* classification covers tow / winch_out / light_service and falls back to
  unclassified for strings no rule matches;
* hourly, daily and weekly numbers agree with the source data and with each
  other;
* the client whose acceptance collapsed is visible in ``client_daily`` and
  trips ``client_acceptance_drop``;
* tows that were declined and light service that was accepted are both
  countable, because those are the findings the reports are for;
* re-running an identical window changes nothing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from conftest import count_rows, dig, ingest_file
from fixture_generator import (
    DEGRADING_CLIENT,
    MIN_LIGHT_SERVICE_ACCEPTED,
    MIN_MISSED_TOWS,
    MIN_UNCLASSIFIED,
    generate_fixture_xlsx,
    generate_rows,
    summarize_rows,
)
from towbook_agent.core.config_loader import get_rules
from towbook_agent.core.db import get_session
from towbook_agent.core.models import (
    ClientDaily,
    MetricsDaily,
    MetricsHourly,
    MetricsWeekly,
    Request,
    client_key_for,
)

#: A fixed window so every number below is reproducible.
#: 2026-07-13 is a Monday, 2026-07-26 the Sunday two weeks later.
END_DATE = date(2026, 7, 26)
DAYS = 14
SEED = 42
WEEK_START = date(2026, 7, 20)  # the Monday of the final full week
LAST_DAY = END_DATE


@pytest.fixture(scope="module")
def fixture_rows() -> list[dict]:
    return generate_rows(days=DAYS, seed=SEED, end_date=END_DATE)


@pytest.fixture(scope="module")
def expected(fixture_rows) -> dict:
    return summarize_rows(fixture_rows)


@pytest.fixture
def ingested(ingestion, classifier, tmp_path: Path) -> Path:
    """The full offline pipeline up to the point where metrics can run."""
    path = generate_fixture_xlsx(
        tmp_path / "digital_requests.xlsx", days=DAYS, seed=SEED, end_date=END_DATE
    )
    _, error = ingest_file(ingestion, path, run_id="e2e-run-1")
    assert error is None, f"the fixture export failed to ingest: {error!r}"

    with get_session() as session:
        classifier.backfill(session)
    return path


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


def test_every_exported_row_is_stored_once(ingested, expected) -> None:
    assert count_rows(Request) == expected["offered"]


def test_stored_statuses_match_the_source(ingested, expected) -> None:
    with get_session(commit=False) as session:
        counts = dict(
            session.execute(select(Request.status, func.count()).group_by(Request.status)).all()
        )
    assert counts == expected["by_status"]


def test_the_verbatim_service_type_survived(ingested, fixture_rows) -> None:
    """Hard constraint #6, checked against the strings that were written."""
    source = {row["request_id"]: row["service_type_raw"] for row in fixture_rows}
    with get_session(commit=False) as session:
        stored = dict(
            session.execute(select(Request.request_id, Request.service_type_raw)).all()
        )
    assert stored == source


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_classification_matches_the_rules(ingested, expected) -> None:
    with get_session(commit=False) as session:
        counts = dict(
            session.execute(
                select(Request.service_class, func.count()).group_by(Request.service_class)
            ).all()
        )
    assert counts == expected["by_service_class"]


def test_unclassified_is_exercised_by_more_than_one_string(ingested, expected) -> None:
    """The mandatory fallback is not a theoretical branch: real exports carry
    service types nobody has written a rule for yet."""
    with get_session(commit=False) as session:
        raw_types = set(
            session.execute(
                select(Request.service_type_raw).where(Request.service_class == "unclassified")
            ).scalars()
        )
    assert len(raw_types) >= 2
    assert sorted(raw_types) == expected["unclassified_service_types"]
    assert count_unclassified() >= MIN_UNCLASSIFIED


def count_unclassified() -> int:
    with get_session(commit=False) as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(Request)
                .where(Request.service_class == "unclassified")
            ).scalar_one()
        )


def test_declined_tows_are_countable(ingested, expected) -> None:
    """missed_tow is the alert that pays for this whole system."""
    with get_session(commit=False) as session:
        missed = int(
            session.execute(
                select(func.count())
                .select_from(Request)
                .where(Request.service_class == "tow")
                .where(Request.status.in_(["denied", "expired"]))
            ).scalar_one()
        )
    assert missed == expected["missed_tows"]
    assert missed >= MIN_MISSED_TOWS


def test_light_service_accepted_against_policy_is_countable(ingested, expected) -> None:
    with get_session(commit=False) as session:
        violations = int(
            session.execute(
                select(func.count())
                .select_from(Request)
                .where(Request.service_class == "light_service")
                .where(Request.status == "accepted")
            ).scalar_one()
        )
    assert violations == expected["light_service_accepted"]
    assert violations >= MIN_LIGHT_SERVICE_ACCEPTED


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _busiest_hour(expected: dict) -> datetime:
    return max(expected["by_hour"], key=lambda key: expected["by_hour"][key]["offered"])


def test_hourly_metrics_match_the_source(metrics, ingested, expected) -> None:
    hour = _busiest_hour(expected)
    counts = expected["by_hour"][hour]

    result = metrics.compute_hourly(hour)

    assert dig(result, "offered") == counts["offered"]
    assert dig(result, "accepted") == counts["accepted"]
    assert dig(result, "rate") == pytest.approx(counts["accepted"] / counts["offered"], abs=1e-4)


def test_hourly_running_totals_match_the_day_so_far(metrics, ingested, expected) -> None:
    hour = _busiest_hour(expected)
    day_so_far = [
        counts
        for bucket, counts in expected["by_hour"].items()
        if bucket.date() == hour.date() and bucket <= hour
    ]
    running_offered = sum(counts["offered"] for counts in day_so_far)
    running_accepted = sum(counts["accepted"] for counts in day_so_far)

    metrics.compute_hourly(hour)

    with get_session(commit=False) as session:
        row = session.execute(
            select(MetricsHourly).where(MetricsHourly.window_start == hour)
        ).scalar_one()
    assert row.day_running_offered == running_offered
    assert row.day_running_accepted == running_accepted


def test_daily_metrics_match_the_source(metrics, ingested, expected) -> None:
    counts = expected["by_day"][LAST_DAY]

    result = metrics.compute_daily(LAST_DAY)

    assert dig(result, "offered") == counts["offered"]
    assert dig(result, "accepted") == counts["accepted"]
    assert dig(result, "rate") == pytest.approx(counts["accepted"] / counts["offered"], abs=1e-4)


def test_weekly_metrics_match_the_seven_days_they_cover(metrics, ingested, expected) -> None:
    week = [WEEK_START + timedelta(days=offset) for offset in range(7)]
    offered = sum(expected["by_day"].get(day, {}).get("offered", 0) for day in week)
    accepted = sum(expected["by_day"].get(day, {}).get("accepted", 0) for day in week)
    assert offered > 0, "the fixture does not cover the week under test"

    result = metrics.compute_weekly(WEEK_START)

    assert dig(result, "offered") == offered
    assert dig(result, "accepted") == accepted


def test_the_daily_numbers_add_up_to_the_weekly_numbers(metrics, ingested) -> None:
    """Internal consistency: two code paths, one answer."""
    daily_total = 0
    for offset in range(7):
        daily_total += dig(metrics.compute_daily(WEEK_START + timedelta(days=offset)), "offered")

    weekly = metrics.compute_weekly(WEEK_START)
    assert dig(weekly, "offered") == daily_total


def test_the_hourly_numbers_add_up_to_the_daily_numbers(metrics, ingested, expected) -> None:
    hourly_total = 0
    for hour in range(24):
        window = datetime(LAST_DAY.year, LAST_DAY.month, LAST_DAY.day, hour)
        hourly_total += dig(metrics.compute_hourly(window), "offered")

    assert hourly_total == dig(metrics.compute_daily(LAST_DAY), "offered")


# --------------------------------------------------------------------------
# The degrading client -- the story the fixture was built to tell
# --------------------------------------------------------------------------


def test_the_degrading_client_is_visible_in_client_daily(metrics, ingested, expected) -> None:
    metrics.compute_daily(LAST_DAY)
    key = client_key_for(DEGRADING_CLIENT)
    source = expected["by_client_day"][(LAST_DAY, key)]

    with get_session(commit=False) as session:
        row = session.execute(
            select(ClientDaily)
            .where(ClientDaily.date == LAST_DAY)
            .where(ClientDaily.client_key == key)
        ).scalar_one()

    assert row.offered == source["offered"]
    assert row.accepted == source["accepted"]
    assert float(row.rate) == pytest.approx(source["accepted"] / source["offered"], abs=1e-3)


def test_the_degrading_client_trips_client_acceptance_drop(
    classifier, metrics, ingested, expected
) -> None:
    """>= 10 offers in the last 24h and an acceptance rate under 0.60."""
    metrics.compute_daily(LAST_DAY)
    key = client_key_for(DEGRADING_CLIENT)
    source = expected["by_client_day"][(LAST_DAY, key)]

    assert source["offered"] >= 10, "the fixture must give this client enough volume to alert"
    rate = source["accepted"] / source["offered"]
    assert rate < 0.60, "the fixture must degrade this client's rate below the threshold"

    fired = classifier.evaluate_alerts(
        {
            "client_key": key,
            "client_acceptance_rate_24h": rate,
            "client_offers_24h": source["offered"],
        },
        get_rules(),
    )
    ids = [
        alert.get("alert_id", alert.get("id")) if isinstance(alert, dict) else alert
        for alert in fired
    ]
    assert "client_acceptance_drop" in ids


def test_a_healthy_client_does_not_trip_the_alert(classifier, ingested, expected) -> None:
    healthy = max(
        (
            (key, counts)
            for key, counts in expected["by_client"].items()
            if key != client_key_for(DEGRADING_CLIENT) and counts["offered"] >= 10
        ),
        key=lambda item: item[1]["accepted"] / item[1]["offered"],
    )
    key, counts = healthy
    rate = counts["accepted"] / counts["offered"]
    assert rate >= 0.60, "the fixture should contain at least one healthy client"

    fired = classifier.evaluate_alerts(
        {"client_key": key, "client_acceptance_rate_24h": rate, "client_offers_24h": counts["offered"]},
        get_rules(),
    )
    ids = [
        alert.get("alert_id", alert.get("id")) if isinstance(alert, dict) else alert
        for alert in fired
    ]
    assert "client_acceptance_drop" not in ids


def test_the_recent_week_is_worse_than_the_first_week_for_that_client(
    ingested, expected
) -> None:
    """The weekly report exists to surface exactly this swing."""
    key = client_key_for(DEGRADING_CLIENT)
    days = sorted({day for day, client in expected["by_client_day"] if client == key})
    first_week, recent_week = days[:7], days[-7:]

    def rate_over(window: list[date]) -> float:
        offered = sum(expected["by_client_day"][(day, key)]["offered"] for day in window)
        accepted = sum(expected["by_client_day"][(day, key)]["accepted"] for day in window)
        return accepted / offered

    assert rate_over(first_week) - rate_over(recent_week) > 0.30, (
        "the degradation is not sharp enough to be worth alerting on"
    )


# --------------------------------------------------------------------------
# Idempotency, end to end
# --------------------------------------------------------------------------


def _snapshot() -> dict:
    with get_session(commit=False) as session:
        hourly = [
            (row.window_start, row.offered, row.accepted, float(row.rate))
            for row in session.execute(
                select(MetricsHourly).order_by(MetricsHourly.window_start)
            ).scalars()
        ]
        daily = [
            (row.date, dict(row.metrics))
            for row in session.execute(select(MetricsDaily).order_by(MetricsDaily.date)).scalars()
        ]
        weekly = [
            (row.week_start, dict(row.metrics))
            for row in session.execute(
                select(MetricsWeekly).order_by(MetricsWeekly.week_start)
            ).scalars()
        ]
        clients = [
            (row.date, row.client_key, row.offered, row.accepted, float(row.rate))
            for row in session.execute(
                select(ClientDaily).order_by(ClientDaily.date, ClientDaily.client_key)
            ).scalars()
        ]
    return {"hourly": hourly, "daily": daily, "weekly": weekly, "clients": clients}


def _run_all_metrics(metrics) -> None:
    for hour in range(24):
        metrics.compute_hourly(datetime(LAST_DAY.year, LAST_DAY.month, LAST_DAY.day, hour))
    metrics.compute_daily(LAST_DAY)
    metrics.compute_weekly(WEEK_START)


def test_rerunning_an_identical_window_is_identical(
    metrics, ingestion, classifier, ingested, expected
) -> None:
    """Hard constraint #4, proved on the whole pipeline rather than one function.

    The scheduler re-runs overlapping windows constantly, and a catch-up run
    after an outage re-runs a window that already produced numbers. Both must
    land on the same answer.
    """
    _run_all_metrics(metrics)
    before = _snapshot()
    request_count = count_rows(Request)

    # The same export, ingested again under a new run id, then everything
    # recomputed -- exactly what a catch-up run does.
    _, error = ingest_file(ingestion, ingested, run_id="e2e-run-2")
    assert error is None
    with get_session() as session:
        classifier.backfill(session)
    _run_all_metrics(metrics)

    assert count_rows(Request) == request_count, "re-ingesting duplicated requests"
    assert _snapshot() == before, "recomputation changed the stored numbers"


def test_recomputation_does_not_duplicate_metric_rows(metrics, ingested) -> None:
    _run_all_metrics(metrics)
    counts = (count_rows(MetricsHourly), count_rows(MetricsDaily), count_rows(MetricsWeekly))

    _run_all_metrics(metrics)

    assert (count_rows(MetricsHourly), count_rows(MetricsDaily), count_rows(MetricsWeekly)) == counts
    assert count_rows(MetricsDaily) == 1
    assert count_rows(MetricsWeekly) == 1


# --------------------------------------------------------------------------
# The fixture itself
# --------------------------------------------------------------------------


def test_the_fixture_is_deterministic() -> None:
    """If this fails, every number above becomes unreproducible."""
    first = generate_rows(days=DAYS, seed=SEED, end_date=END_DATE)
    second = generate_rows(days=DAYS, seed=SEED, end_date=END_DATE)
    assert first == second

    different = generate_rows(days=DAYS, seed=SEED + 1, end_date=END_DATE)
    assert different != first


def test_the_fixture_covers_the_signals_the_reports_need(expected) -> None:
    assert expected["offered"] > 500, "too small to be interesting"
    assert set(expected["by_service_class"]) >= {
        "tow",
        "winch_out",
        "light_service",
        "unclassified",
    }
    assert set(expected["by_status"]) >= {"accepted", "denied", "expired", "canceled"}
    assert len(expected["by_client"]) >= 4, "several clients with different rates"
    rates = sorted(
        counts["accepted"] / counts["offered"] for counts in expected["by_client"].values()
    )
    assert rates[-1] - rates[0] > 0.25, "the clients are too alike to be a useful fixture"
