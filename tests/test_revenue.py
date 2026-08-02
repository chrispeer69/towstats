"""The lost-revenue view: what it counts, what it refuses to count, and when
it admits it is looking at part of a period.

This is the most quotable page in the system -- its figures are the ones that
end up in a conversation with a client or a bank -- so these tests are mostly
about the ways a dollar figure can be quietly wrong:

* a job whose client has no configured price must contribute NOTHING, and must
  be counted somewhere visible rather than dropped;
* a tow average must never be applied to light service, and vice versa;
* a week or month only partly inside the window must say so;
* the running tiles must compare like with like -- the same offset into the
  previous period, not a part-week against a whole one;
* the merged scope must be the sum of its members, not a double count.

Rows are inserted relative to ``today_local()`` rather than at fixed dates,
because the running tiles are defined against the real current day and a
hard-coded anchor would make this module fail on a calendar boundary.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from towbook_agent.core.db import get_session
from towbook_agent.core.models import Request, client_key_for
from towbook_agent.web import queries as q


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _utc_for(local_day, hour: int) -> datetime:
    """A naive-UTC storage value that lands on ``local_day`` at ``hour`` local.

    Storage is naive UTC and every view converts on read, so a test that wants
    a job "at 3am local" has to do this conversion itself -- writing 03:00 into
    the column would put the job at 11pm the previous day in Detroit.
    """
    aware_local = datetime.combine(local_day, time(hour=hour), tzinfo=q.local_tz())
    return aware_local.astimezone(timezone.utc).replace(tzinfo=None)


_COUNTER = {"n": 0}


def _add(
    *,
    day,
    hour: int = 12,
    client: str = "Agero (Swoop)",
    service_class: str = "tow",
    service_type: str = "Standard Tow",
    status: str = "expired",
    status_code: int | None = 5,
    status_raw: str = "Expired",
    company_id: str = "default",
) -> None:
    """Insert one offer. Defaults to a no-response tow, i.e. recoverable."""
    _COUNTER["n"] += 1
    with get_session() as session:
        session.add(
            Request(
                request_id=f"rev-{_COUNTER['n']}",
                company_id=company_id,
                client_name=client,
                client_key=client_key_for(client),
                offered_at=_utc_for(day, hour),
                status=status,
                status_raw=status_raw,
                status_code=status_code,
                service_type_raw=service_type,
                service_class=service_class,
                pickup_location="100 High St, Columbus OH",
            )
        )


def _patch_rules(config_dir, write_config, **missed_work):
    """Overlay keys onto the SHIPPED rules.yaml rather than replacing it.

    ``write_config`` writes a whole file. Handing it a two-key mapping would
    delete the bucket and status tables every one of these tests depends on, and
    the symptom -- every figure coming back zero -- looks exactly like the view
    being broken. So the real file is read, patched and written back.
    """
    import yaml

    path = config_dir / "rules.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules.setdefault("missed_work", {}).update(missed_work)
    return write_config("rules.yaml", rules)


@pytest.fixture
def priced(config_dir, write_config):
    """A price book with exactly one client in it, so unpriced work is visible."""
    _patch_rules(
        config_dir,
        write_config,
        job_value_by_client={"agero (swoop)": 100},
        light_service_value=35,
    )


# --------------------------------------------------------------------------
# What it counts
# --------------------------------------------------------------------------


def test_recoverable_tows_are_valued_at_the_client_average(priced):
    today = q.today_local()
    for _ in range(3):
        _add(day=today, hour=9)

    snap = q.revenue_snapshot(days=30, company_id="default")

    assert snap["totals"]["value"] == 300.0
    assert snap["totals"]["jobs"] == 3
    assert snap["totals"]["tow_jobs"] == 3
    assert snap["running"]["today"]["current"]["value"] == 300.0


def test_won_and_in_flight_work_is_not_lost_revenue(priced):
    today = q.today_local()
    _add(day=today, hour=9)  # no_response -> recoverable
    _add(day=today, hour=9, status="accepted", status_code=1, status_raw="Accepted")
    _add(day=today, hour=9, status="pending", status_code=6, status_raw="Pending")

    snap = q.revenue_snapshot(days=30, company_id="default")

    # Only the missed one is money. An accepted job is revenue we HAVE.
    assert snap["totals"]["jobs"] == 1
    assert snap["totals"]["value"] == 100.0


def test_client_withdrew_is_excluded_unless_configured(priced):
    today = q.today_local()
    _add(day=today, hour=9, status="cancelled", status_code=4, status_raw="Cancelled")

    snap = q.revenue_snapshot(days=30, company_id="default")

    # The default judgement: a client pulling a job is not work we turned away.
    assert snap["totals"]["jobs"] == 0
    assert snap["totals"]["value"] == 0.0


# --------------------------------------------------------------------------
# What it refuses to count -- the understatement rules
# --------------------------------------------------------------------------


def test_an_unpriced_client_contributes_nothing_and_is_named(priced):
    today = q.today_local()
    _add(day=today, hour=9)  # Agero, priced
    _add(day=today, hour=9, client="Someone New")  # no configured value

    snap = q.revenue_snapshot(days=30, company_id="default")

    assert snap["totals"]["value"] == 100.0, "an unpriced job must not be guessed at"
    assert snap["totals"]["jobs"] == 2, "but it is still a missed job"
    assert snap["totals"]["unpriced_jobs"] == 1
    named = [row["client"] for row in snap["pricing"]["unpriced_clients"]]
    assert named == ["Someone New"], "the reader has to be told which price is missing"


def test_a_tow_average_is_never_applied_to_light_service(priced):
    today = q.today_local()
    _add(
        day=today,
        hour=9,
        service_class="light_service",
        service_type="Tire Change",
        status="declined",
        status_code=2,
        status_raw="Declined",
    )

    snap = q.revenue_snapshot(days=30, company_id="default")

    # The flat light-service rate, not Agero's $100 tow average.
    assert snap["totals"]["light_value"] == 35.0
    assert snap["totals"]["tow_value"] == 0.0
    assert snap["totals"]["value"] == 35.0


def test_no_price_book_means_no_dollars_not_zero_dollars(config_dir, write_config):
    _patch_rules(config_dir, write_config, job_value_by_client={})
    _add(day=q.today_local(), hour=9)

    snap = q.revenue_snapshot(days=30, company_id="default")

    # `configured` is what the page keys its empty state off. A total of $0
    # would render as "you lost nothing", which is the opposite of the truth.
    assert snap["pricing"]["configured"] is False


# --------------------------------------------------------------------------
# The hours -- the whole point of the view
# --------------------------------------------------------------------------


def test_hours_are_ranked_by_dollars_not_by_job_count(priced):
    today = q.today_local()
    # 3am: two tows at $100. 2pm: four light-service jobs at $35.
    for _ in range(2):
        _add(day=today, hour=3)
    for _ in range(4):
        _add(
            day=today,
            hour=14,
            service_class="light_service",
            service_type="Tire Change",
            status="declined",
            status_code=2,
            status_raw="Declined",
        )

    snap = q.revenue_snapshot(days=30, company_id="default")
    worst = snap["worst_hours"][0]

    # 2pm has twice the jobs; 3am has $200 against $140. This view exists to
    # rank the second way -- it is the one a staffing decision turns on.
    assert worst["hour"] == 3
    assert worst["value"] == 200.0
    assert snap["hours"][14]["jobs"] == 4
    assert snap["hours"][14]["value"] == 140.0


def test_coverage_is_measured_per_job_not_guessed_from_the_hour(priced):
    """An hour is staffed on some weekdays and not others, so the flag has to
    come from the jobs themselves.

    The first cut of this view asked "is 09:00 inside the staffed window?" of a
    synthetic datetime built on today's date. Run on a Sunday, that answered
    "no" for all 24 hours and the page marked the entire day unstaffed.
    """
    # A weekday and a weekend day at the same hour, inside a Mon-Fri window.
    today = q.today_local()
    weekday = today - timedelta(days=(today.weekday() - 2) % 7)  # a Wednesday
    saturday = today - timedelta(days=(today.weekday() - 5) % 7)
    if weekday == saturday:  # pragma: no cover - impossible, guards a silent pass
        pytest.skip("could not find distinct weekday/weekend days")

    _add(day=weekday, hour=9)
    _add(day=saturday, hour=9)

    snap = q.revenue_snapshot(days=30, company_id="default")
    nine = snap["hours"][9]

    assert nine["jobs"] == 2
    # Exactly one of the two fell outside the staffed window.
    assert nine["uncovered_jobs"] == 1
    assert nine["uncovered_share"] == 0.5
    assert nine["uncovered_value"] == 100.0
    # An hour that is half covered must not be reported as simply "unstaffed".
    assert nine["mostly_uncovered"] is True  # >= 0.5 is the documented boundary

    # And an hour nobody is ever rostered for is unambiguous.
    _add(day=weekday, hour=3)
    snap = q.revenue_snapshot(days=30, company_id="default")
    assert snap["hours"][3]["uncovered_share"] == 1.0


def test_every_hour_of_the_day_is_present_even_when_empty(priced):
    _add(day=q.today_local(), hour=9)
    snap = q.revenue_snapshot(days=30, company_id="default")

    assert [h["hour"] for h in snap["hours"]] == list(range(24))
    # An hour with nothing in it is a real zero here, not a gap.
    assert snap["hours"][2]["value"] == 0.0


# --------------------------------------------------------------------------
# Periods -- the part-week / part-month honesty rules
# --------------------------------------------------------------------------


def test_a_month_only_partly_inside_the_window_is_flagged(priced):
    today = q.today_local()
    _add(day=today, hour=9)
    _add(day=today - timedelta(days=20), hour=9)

    # Seven days cannot span a whole month, so whichever months appear are cut.
    snap = q.revenue_snapshot(days=7, company_id="default")

    assert snap["monthly"], "the roll-up must still be produced"
    assert any(row["partial"] for row in snap["monthly"]), (
        "a clipped month must say so -- otherwise its total reads as the month's"
    )


def test_a_full_week_inside_the_window_is_not_flagged_partial(priced):
    today = q.today_local()
    _add(day=today - timedelta(days=40), hour=9)
    _add(day=today, hour=9)

    snap = q.revenue_snapshot(days=60, company_id="default")
    complete = [w for w in snap["weekly"] if not w["partial"]]

    assert complete, "a 60-day window must contain at least one whole week"
    assert all(w["days_covered"] == 7 for w in complete)


def test_running_tiles_compare_the_same_offset_into_the_previous_period(priced):
    today = q.today_local()
    week_start = q.week_start_for(today)
    offset = (today - week_start).days

    # This week: one job so far.
    _add(day=today, hour=9)
    # Last week: one job at the same offset, and one LATER in that week that the
    # comparison must not reach -- otherwise a Monday is judged against a whole
    # week and every Monday looks like an improvement.
    _add(day=week_start - timedelta(days=7) + timedelta(days=offset), hour=9)
    if offset < 6:
        _add(day=week_start - timedelta(days=1), hour=9)

    snap = q.revenue_snapshot(days=30, company_id="default")
    week = snap["running"]["week"]

    assert week["current"]["value"] == 100.0
    assert week["previous"]["value"] == 100.0, "the tail of last week must be excluded"
    assert week["direction"] == "flat"


def test_no_percentage_delta_against_a_zero_baseline(priced):
    _add(day=q.today_local(), hour=9)
    snap = q.revenue_snapshot(days=30, company_id="default")

    # Yesterday holds nothing, so there is no percentage to state.
    assert snap["running"]["today"]["previous"]["value"] == 0.0
    assert snap["running"]["today"]["delta_pct"] is None
    assert snap["running"]["today"]["delta"] == 100.0


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------


def test_the_view_is_scoped_to_one_company(priced):
    today = q.today_local()
    _add(day=today, hour=9, company_id="default")
    _add(day=today, hour=9, company_id="other-co")

    snap = q.revenue_snapshot(days=30, company_id="default")

    assert snap["totals"]["value"] == 100.0, "another company's loss is not ours"
    assert snap["totals"]["jobs"] == 1


def test_totals_are_internally_consistent(priced):
    today = q.today_local()
    for hour in (3, 9, 21):
        _add(day=today, hour=hour)
    _add(
        day=today,
        hour=9,
        service_class="light_service",
        service_type="Tire Change",
        status="declined",
        status_code=2,
        status_raw="Declined",
    )
    _add(day=today, hour=9, client="Someone New")

    snap = q.revenue_snapshot(days=30, company_id="default")
    totals = snap["totals"]

    assert totals["value"] == totals["tow_value"] + totals["light_value"]
    assert totals["jobs"] == totals["tow_jobs"] + totals["light_jobs"] + totals["unpriced_jobs"]
    assert sum(h["value"] for h in snap["hours"]) == pytest.approx(totals["value"])
    assert sum(c["value"] for c in snap["clients"]) == pytest.approx(totals["value"])
    assert sum(c["value"] for c in snap["causes"]) == pytest.approx(totals["value"])
    assert sum(d["value"] for d in snap["daily"]) == pytest.approx(totals["value"])
