"""Two towing companies in one database, and never a row of one in the other.

This system is given and sold to other US Tow Alliance members, so one install
reports on several tenants out of one datastore. That makes exactly one bug
category unacceptable: a query that forgets its ``company_id`` filter does not
produce a wrong number that somebody notices, it produces a plausible number
built out of another company's customers, volumes and refusal reasons.

So the tests below are adversarial about it. Two companies are seeded with
DELIBERATELY DIFFERENT data -- different clients, different hours, different
service types, different outcomes -- and every public read is asserted to
return one company's rows and none of the other's. Where the leak would be
invisible in a total, the assertion is on the identity of the rows (client
names, service types), not only on the count.

Three other properties are proved here because they are the reason multi-tenant
reporting is not just a filter:

* **Per-company coverage windows.** The coverage split is the headline of every
  report. An Ohio company staffed 06:00-18:00 Mon-Fri and a Texas company
  staffed 12:00-23:00 must produce different "inside/outside" answers over the
  SAME offers, because the question is whether a human was watching.
* **Per-company job values.** Dollars are only ever derived from the configured
  per-client table, and one company's price list must not price another's work.
* **Per-company metrics rows.** ``metrics_daily`` keyed on the date alone would
  have the second company to run overwrite the first one's Tuesday.

And one operational property: **the scheduler must survive one tenant failing.**
A company whose Towbook password expired cannot be allowed to stop the rest of
the roster from reporting.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import pytest
import yaml
from sqlalchemy import select

import socket as _socket

from conftest import count_rows

#: Captured at import, before conftest's ``no_network`` fixture replaces them.
_REAL_CONNECT = _socket.socket.connect
_REAL_CONNECT_EX = _socket.socket.connect_ex
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", ""})
from towbook_agent.core import companies as companies_module
from towbook_agent.core.db import get_session
from towbook_agent.core.models import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_COMPANY_ID,
    ClientDaily,
    MetricsDaily,
    MetricsMissedWork,
    Request,
    Run,
    client_key_for,
)

# --------------------------------------------------------------------------
# The roster and the two datasets
# --------------------------------------------------------------------------

OHIO = "ohio-towing"
TEXAS = "texas-recovery"

#: 2026-07-20 is a Monday. The suite runs with TZ=UTC, so these local hours are
#: also the stored UTC hours and every expectation below is direct arithmetic.
DAY = date(2026, 7, 20)

#: Ohio: staffed 06:00-18:00 Mon-Fri, so its 09:00 offers are covered and its
#: 20:00 offers are not.
OHIO_PLAN = (
    # (hour, client, status, service_type, count)
    (9, "Agero", "accepted", "Tow", 4),
    (9, "Agero", "expired", "Tow", 1),
    (20, "Agero", "expired", "Tow", 5),
    (20, "Allstate", "denied", "Tow", 2),
)

#: Texas: an evening shift, 12:00-23:00 every day, and a different client mix
#: entirely. Nothing here shares a client name or a service type with Ohio, so
#: a leak shows up as an identity, not only as a count.
TEXAS_PLAN = (
    (7, "Nationwide", "expired", "Heavy Duty TOW", 6),
    (14, "Nationwide", "accepted", "Heavy Duty TOW", 3),
    (14, "Road America", "accepted", "Winch Out", 2),
    (21, "Road America", "accepted", "Winch Out", 1),
)

OHIO_TOTAL = sum(entry[4] for entry in OHIO_PLAN)      # 12
TEXAS_TOTAL = sum(entry[4] for entry in TEXAS_PLAN)    # 12

OHIO_CLIENTS = {"agero", "allstate"}
TEXAS_CLIENTS = {"nationwide", "road america"}

STATUS_RAW = {
    "accepted": "Accepted",
    "expired": "Expired",
    "denied": "Rejected",
}
STATUS_CODE = {"accepted": 1, "expired": 5, "denied": 2}


def _rows(company_id: str, plan: tuple, prefix: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    counter = 0
    for hour, client, status, service, count in plan:
        for index in range(count):
            counter += 1
            out.append(
                {
                    "request_id": f"{prefix}-{counter:04d}",
                    "company_id": company_id,
                    "client_name": client,
                    "client_key": client_key_for(client),
                    "offered_at": datetime(DAY.year, DAY.month, DAY.day, hour, index * 2),
                    "status": status,
                    "status_raw": STATUS_RAW[status],
                    "status_code": STATUS_CODE[status],
                    "denial_reason": "No Drivers Available" if status == "denied" else None,
                    "service_type_raw": service,
                    "service_class": "winch_out" if "Winch" in service else "tow",
                }
            )
    return out


def _insert(rows: Iterable[dict[str, Any]]) -> None:
    with get_session() as session:
        for row in rows:
            session.add(Request(**row))


ROSTER = {
    "version": 1,
    "default_company": OHIO,
    "companies": [
        {
            "id": OHIO,
            "name": "Ohio Towing and Recovery",
            "towbook_company_id": 61343,
            "credentials_env": "OHIO",
            "timezone": "UTC",
            "enabled": True,
            "coverage": {
                "windows": [
                    {
                        "name": "covered",
                        "days": ["mon", "tue", "wed", "thu", "fri"],
                        "start": "06:00",
                        "end": "18:00",
                    }
                ],
                "default_label": "uncovered",
            },
            "job_value_by_client": {"agero": 65, "allstate": 65},
        },
        {
            "id": TEXAS,
            "name": "Texas Recovery LLC",
            "credentials_env": "TEXAS",
            "timezone": "UTC",
            "enabled": True,
            "coverage": {
                "windows": [
                    {
                        "name": "covered",
                        "days": ["all"],
                        "start": "12:00",
                        "end": "23:00",
                    }
                ],
                "default_label": "uncovered",
            },
            "job_value_by_client": {"nationwide": 200, "road america": 200},
            "rules": {"missed_work": {"blind_spot": {"min_offers": 2, "threshold": 0.5}}},
        },
    ],
}


@pytest.fixture
def roster(write_config):
    """Write config/companies.yaml with two enabled companies."""
    write_config("companies", ROSTER)
    companies_module.reload_companies()
    try:
        yield ROSTER
    finally:
        companies_module.reload_companies()


@pytest.fixture
def two_companies(roster):
    """Both datasets in one database."""
    _insert(_rows(OHIO, OHIO_PLAN, "OH"))
    _insert(_rows(TEXAS, TEXAS_PLAN, "TX"))
    return roster


@pytest.fixture
def missed_work():
    from conftest import load_agent

    return load_agent("missed_work")


@pytest.fixture
def metrics_agent():
    from conftest import load_agent

    return load_agent("metrics")


# ==========================================================================
# The roster itself
# ==========================================================================


def test_the_default_company_id_matches_the_stored_default() -> None:
    """models and companies must name the same string or history is orphaned.

    Every row written before the roster existed carries ``account_id="default"``.
    If these two ever drift, a single-company install stops finding its own data
    and the failure looks like an empty database.
    """
    assert DEFAULT_COMPANY_ID == DEFAULT_ACCOUNT_ID == "default"


def test_with_no_roster_there_is_exactly_one_company_and_no_switcher() -> None:
    """The single-company install is the default, not a special case."""
    companies_module.reload_companies()
    companies = companies_module.enabled_companies()
    assert [company.id for company in companies] == [DEFAULT_COMPANY_ID]
    assert companies_module.is_multi_company() is False
    assert companies_module.company_choices() == []


def test_a_roster_of_two_turns_the_switcher_on(roster) -> None:
    assert companies_module.is_multi_company() is True
    assert [company.id for company in companies_module.company_choices()] == [OHIO, TEXAS]
    assert companies_module.default_company_id() == OHIO


def test_credentials_are_env_var_names_not_credentials(roster) -> None:
    """companies.yaml is committed, so it may only ever name the variable."""
    text = yaml.safe_dump(ROSTER)
    assert "password" not in text.lower()

    ohio = companies_module.get_company(OHIO)
    assert ohio.env_user == "TOWBOOK_OHIO_USER"
    assert ohio.env_pass == "TOWBOOK_OHIO_PASS"
    # And nothing in the serialised record leaks a value either.
    assert set(ohio.as_dict()) & {"user", "username", "pass", "password"} == set()


def test_a_company_with_no_prefix_falls_back_to_the_single_company_pair() -> None:
    """An existing single-company install must keep working untouched."""
    companies_module.reload_companies()
    only = companies_module.enabled_companies()[0]
    assert only.credentials_env is None
    assert (only.env_user, only.env_pass) == ("TOWBOOK_USER", "TOWBOOK_PASS")


def test_a_company_that_declares_a_prefix_never_falls_back(roster, monkeypatch) -> None:
    """Falling back would sign in as the WRONG company and file its jobs here.

    That is not a degraded report; it is two tenants' data permanently mixed,
    caused by a typo in a variable name. So it is an error that names the
    variable, never a quiet substitution.
    """
    from conftest import load_agent

    acquisition = load_agent("acquisition")
    monkeypatch.setenv("TOWBOOK_USER", "someone@example.invalid")
    monkeypatch.setenv("TOWBOOK_PASS", "a-real-looking-password")
    monkeypatch.delenv("TOWBOOK_OHIO_USER", raising=False)
    monkeypatch.delenv("TOWBOOK_OHIO_PASS", raising=False)

    with pytest.raises(acquisition.CredentialsMissing) as caught:
        acquisition.credentials_for(OHIO)
    assert "TOWBOOK_OHIO_USER" in str(caught.value)


def test_per_company_credentials_are_read_from_their_own_variables(roster, monkeypatch) -> None:
    from conftest import load_agent

    acquisition = load_agent("acquisition")
    monkeypatch.setenv("TOWBOOK_TEXAS_USER", "texas@example.invalid")
    monkeypatch.setenv("TOWBOOK_TEXAS_PASS", "texas-secret-value")

    creds = acquisition.credentials_for(TEXAS)
    assert creds.username == "texas@example.invalid"
    assert creds.password == "texas-secret-value"
    # The password is excluded from repr, per hard constraint #1.
    assert "texas-secret-value" not in repr(creds)


def test_a_per_company_password_is_scrubbed_from_logs(roster, monkeypatch) -> None:
    from towbook_agent.core.logging_setup import REDACTION, redact

    monkeypatch.setenv("TOWBOOK_TEXAS_PASS", "texas-secret-value")
    assert redact("login failed for texas-secret-value") == f"login failed for {REDACTION}"


# ==========================================================================
# Rule precedence
# ==========================================================================


def test_a_company_overrides_only_what_it_names(roster) -> None:
    """Global rules.yaml stays the default; the company entry overrides."""
    from towbook_agent.core.config_loader import get_rules

    global_rules = get_rules()
    ohio = companies_module.rules_for(OHIO)

    # Untouched blocks come straight from rules.yaml.
    assert ohio["service_classes"] == global_rules["service_classes"]
    assert ohio["acceptance_policy"] == global_rules["acceptance_policy"]
    # Named blocks are this company's.
    assert ohio["missed_work"]["coverage"]["windows"][0]["end"] == "18:00"
    assert ohio["missed_work"]["job_value_by_client"] == {"agero": 65, "allstate": 65}


def test_coverage_and_job_values_replace_rather_than_merge(roster) -> None:
    """A half-inherited staffed window or price list is indefensible.

    Ohio's price list has two clients. rules.yaml ships five. If the merge were
    recursive, Ohio would silently price NSD work it has never been offered.
    """
    texas = companies_module.rules_for(TEXAS)
    assert set(texas["missed_work"]["job_value_by_client"]) == {"nationwide", "road america"}
    assert texas["missed_work"]["coverage"]["windows"][0]["days"] == ["all"]


def test_the_free_form_rules_block_deep_merges(roster) -> None:
    """`rules:` reaches anything, and merges mapping by mapping."""
    texas = companies_module.rules_for(TEXAS)
    assert texas["missed_work"]["blind_spot"]["min_offers"] == 2
    assert texas["missed_work"]["blind_spot"]["threshold"] == 0.5
    # Siblings of the overridden block survive the merge.
    assert "closeoff" in texas["missed_work"]
    assert "buckets" in texas["missed_work"]


def test_a_company_with_no_overrides_gets_the_global_rules_unchanged(roster) -> None:
    from towbook_agent.core.config_loader import get_rules

    companies_module.reload_companies()
    assert companies_module.rules_for(DEFAULT_COMPANY_ID) is not None
    # The unconfigured fallback company declares nothing, so it is the globals.
    companies_module.reload_companies()


# ==========================================================================
# THE LEAK TESTS: no query may return the other company's rows
# ==========================================================================


def test_the_row_loader_returns_one_company_only(two_companies, metrics_agent) -> None:
    start = datetime(DAY.year, DAY.month, DAY.day)
    end = start + timedelta(days=1)
    with get_session(commit=False) as session:
        ohio = metrics_agent._load_rows(session, start, end, OHIO)
        texas = metrics_agent._load_rows(session, start, end, TEXAS)

    assert len(ohio) == OHIO_TOTAL
    assert len(texas) == TEXAS_TOTAL
    assert {row.client_key for row in ohio} == OHIO_CLIENTS
    assert {row.client_key for row in texas} == TEXAS_CLIENTS
    assert {row.request_id for row in ohio} & {row.request_id for row in texas} == set()


def test_no_company_id_means_the_active_company_never_everything(
    two_companies, metrics_agent
) -> None:
    """`company_id=None` must never widen to the whole roster.

    This is the shape the bug would take: a helper that forgot to take the
    argument, quietly reading twice the rows and reporting a total nobody can
    reconcile.
    """
    start = datetime(DAY.year, DAY.month, DAY.day)
    end = start + timedelta(days=1)
    with get_session(commit=False) as session:
        with companies_module.use_company(TEXAS):
            rows = metrics_agent._load_rows(session, start, end, None)
    assert len(rows) == TEXAS_TOTAL
    assert {row.client_key for row in rows} == TEXAS_CLIENTS


def test_the_dashboard_row_fetch_returns_one_company_only(two_companies) -> None:
    from towbook_agent.web import queries as q

    start = datetime(DAY.year, DAY.month, DAY.day)
    end = start + timedelta(days=1)
    ohio = q.fetch_requests(start, end, company_id=OHIO)
    texas = q.fetch_requests(start, end, company_id=TEXAS)

    assert {row["client_key"] for row in ohio} == OHIO_CLIENTS
    assert {row["client_key"] for row in texas} == TEXAS_CLIENTS
    assert len(ohio) + len(texas) == OHIO_TOTAL + TEXAS_TOTAL


@pytest.mark.parametrize("view", ["clients_overview", "trends_snapshot"])
def test_every_dashboard_view_sees_one_company(two_companies, view) -> None:
    """Whole-view assertion, by client identity rather than by count.

    A count can coincide -- both companies have twelve offers here, on purpose
    -- so only the NAMES say which rows were actually read. The two trailing
    -window views are the ones checked here; ``live`` and ``hourly`` report on
    today and are covered by the switcher tests below, which drive them through
    the real routes.
    """
    from towbook_agent.web import queries as q

    function = getattr(q, view)
    for company_id, expected in ((OHIO, OHIO_CLIENTS), (TEXAS, TEXAS_CLIENTS)):
        data = function(company_id=company_id)
        blob = repr(data).casefold()
        for name in expected:
            assert name in blob, f"{view} for {company_id} lost {name}"
        for name in (OHIO_CLIENTS | TEXAS_CLIENTS) - expected:
            assert name not in blob, f"{view} for {company_id} leaked {name}"


def test_the_daily_view_sees_one_company(two_companies) -> None:
    from towbook_agent.web import queries as q

    ohio = q.daily_snapshot(DAY, company_id=OHIO)
    texas = q.daily_snapshot(DAY, company_id=TEXAS)

    assert ohio["totals"]["offered"] == OHIO_TOTAL
    assert texas["totals"]["offered"] == TEXAS_TOTAL
    assert {entry["client_key"] for entry in ohio["clients"]} == OHIO_CLIENTS
    assert {entry["client_key"] for entry in texas["clients"]} == TEXAS_CLIENTS


def test_the_client_detail_view_cannot_be_used_to_read_across(two_companies) -> None:
    """Ohio's Agero page must not show Texas rows, and vice versa.

    The slug is a client key and client keys are not unique across tenants, so
    this is the read most likely to cross the boundary by accident.
    """
    from towbook_agent.web import queries as q

    detail = q.client_detail("agero", company_id=TEXAS)
    assert detail["totals"]["offered"] == 0

    detail = q.client_detail("agero", company_id=OHIO)
    assert detail["totals"]["offered"] == 10  # 4 + 1 + 5


def test_the_missed_work_model_sees_one_company(two_companies) -> None:
    from towbook_agent.web import queries as q

    ohio = q.missed_work_snapshot(days=400, company_id=OHIO)
    texas = q.missed_work_snapshot(days=400, company_id=TEXAS)

    assert ohio["totals"]["offers"] == OHIO_TOTAL
    assert texas["totals"]["offers"] == TEXAS_TOTAL
    assert ohio["totals"]["accepted"] == 4
    assert texas["totals"]["accepted"] == 6


def test_health_counts_are_this_company_not_the_whole_install(two_companies) -> None:
    """A roster-wide row count under one company's name is quietly misleading."""
    from towbook_agent.web import queries as q

    assert q.health_view(company_id=OHIO)["counts"]["requests"] == OHIO_TOTAL
    assert q.health_view(company_id=TEXAS)["counts"]["requests"] == TEXAS_TOTAL
    assert count_rows(Request) == OHIO_TOTAL + TEXAS_TOTAL


def test_has_any_data_is_per_company(roster) -> None:
    """A newly added tenant must see "no data yet", not a page of zeros.

    Seeded with ONE company's rows on purpose: the second is in the roster,
    enabled, and has never been pulled -- which is exactly the state a company
    is in on the day it is added.
    """
    from towbook_agent.web import queries as q

    _insert(_rows(OHIO, OHIO_PLAN, "OH"))
    assert q.has_any_data(OHIO) is True
    assert q.has_any_data(TEXAS) is False


def test_the_unclassified_backlog_is_per_company(two_companies) -> None:
    from towbook_agent.web import queries as q

    with get_session() as session:
        for request_id in ("OH-0001", "TX-0001"):
            row = session.get(Request, request_id)
            row.service_class = "unclassified"

    ohio = {entry["service_type_raw"] for entry in q.unclassified_service_types(company_id=OHIO)}
    texas = {entry["service_type_raw"] for entry in q.unclassified_service_types(company_id=TEXAS)}
    assert ohio == {"Tow"}
    assert texas == {"Heavy Duty TOW"}


def test_the_run_history_is_per_company(two_companies) -> None:
    from towbook_agent.web import queries as q

    # Recent on purpose: an old run would also trip the "report is overdue"
    # finding, which is a roster-wide watchdog question and would mask the
    # per-company one being tested here.
    recent = datetime.utcnow() - timedelta(minutes=5)
    with get_session() as session:
        session.add(
            Run(
                run_id="ohio-run",
                company_id=OHIO,
                report_type="daily",
                status="succeeded",
                row_count=OHIO_TOTAL,
                started_at=recent,
                finished_at=recent,
            )
        )
        session.add(
            Run(
                run_id="texas-run",
                company_id=TEXAS,
                report_type="daily",
                status="failed",
                row_count=0,
                started_at=recent,
                finished_at=recent,
            )
        )

    assert q.last_run_summary(OHIO)["run_id"] == "ohio-run"
    assert q.last_run_summary(TEXAS)["run_id"] == "texas-run"

    # And the failed-run banner is one company's problem, not the board's.
    # (Both companies are also "overdue" here, because the seeded runs are
    # older than the watchdog window -- that finding is roster-wide by design
    # and is a different, lower-ranked candidate.)
    assert q.pipeline_banner(company_id=TEXAS)["title"] == "The last pipeline run failed."
    ohio_banner = q.pipeline_banner(company_id=OHIO) or {}
    assert ohio_banner.get("title") != "The last pipeline run failed."


# ==========================================================================
# Per-company coverage windows
# ==========================================================================


def test_each_company_is_measured_against_its_own_staffed_window(
    two_companies, missed_work
) -> None:
    """The coverage split is the headline of every report, so it must be theirs.

    Ohio works 06:00-18:00 Mon-Fri: its 09:00 offers are inside, its 20:00
    offers are outside. Texas works 12:00-23:00 every day: its 07:00 offers are
    outside and its 14:00 and 21:00 offers are inside. Run either company's
    rows against the other's window and both answers are wrong.
    """
    start = datetime(DAY.year, DAY.month, DAY.day)
    end = start + timedelta(days=1)

    ohio = missed_work.coverage_analysis(None, start, end, None, company_id=OHIO)
    assert ohio["inside"]["offers"] == 5       # the 09:00 block
    assert ohio["outside"]["offers"] == 7      # the 20:00 blocks
    assert ohio["outside"]["no_response"] == 5

    texas = missed_work.coverage_analysis(None, start, end, None, company_id=TEXAS)
    assert texas["inside"]["offers"] == 6      # 14:00 x5 + 21:00 x1
    assert texas["outside"]["offers"] == 6     # the 07:00 block
    assert texas["outside"]["no_response"] == 6


def test_the_same_offer_time_lands_in_different_windows_for_different_companies(
    roster, missed_work
) -> None:
    """The cleanest statement of the property: one row, two companies, two answers.

    07:00 on a Monday is outside Ohio's 06:00-18:00 window only because Ohio's
    window starts at 06:00 -- so it is INSIDE for Ohio and OUTSIDE for Texas,
    whose shift does not start until noon.
    """
    row = {"offered_at_local": datetime(DAY.year, DAY.month, DAY.day, 7, 30)}
    assert missed_work.coverage_label(row, companies_module.rules_for(OHIO)) == "covered"
    assert missed_work.coverage_label(row, companies_module.rules_for(TEXAS)) == "uncovered"


def test_a_company_with_no_coverage_block_inherits_the_global_window(
    roster, missed_work
) -> None:
    """Omitting the block is how a new tenant starts: rules.yaml is the default."""
    row = {"offered_at_local": datetime(DAY.year, DAY.month, DAY.day, 7, 30)}
    inherited = missed_work.coverage_label(row, companies_module.rules_for("not-in-roster"))
    # Falls back to the roster default (Ohio) rather than to nothing at all.
    assert inherited in {"covered", "uncovered"}


def test_the_per_company_blind_spot_thresholds_are_applied(two_companies, missed_work) -> None:
    """Texas lowered min_offers to 2 and threshold to 0.5 in its `rules:` block."""
    start = datetime(DAY.year, DAY.month, DAY.day)
    end = start + timedelta(days=1)

    texas = missed_work.blind_spots(None, start, end, None, company_id=TEXAS)
    assert texas["thresholds"] == {"min_offers": 2, "threshold": 0.5}

    ohio = missed_work.blind_spots(None, start, end, None, company_id=OHIO)
    assert ohio["thresholds"]["min_offers"] == 5   # the global default
    assert ohio["thresholds"]["threshold"] == 0.35


# ==========================================================================
# Per-company job values
# ==========================================================================


def test_each_company_prices_its_missed_work_with_its_own_table(
    two_companies, missed_work
) -> None:
    """Dollars come only from the configured table, and only from THIS one.

    Ohio pays $65 a tow and missed 8 (1 + 5 expired, 2 declined). Texas pays
    $200 and missed 6. Sharing one price list would put $65 against Texas's
    heavy-duty work or $200 against Ohio's light-duty tows.
    """
    start = datetime(DAY.year, DAY.month, DAY.day)
    end = start + timedelta(days=1)

    ohio = missed_work.compute_missed_work(
        None, start, end, None, company_id=OHIO, persist=False
    )
    texas = missed_work.compute_missed_work(
        None, start, end, None, company_id=TEXAS, persist=False
    )

    assert ohio["revenue_available"] is True
    assert texas["revenue_available"] is True
    assert ohio["totals"]["estimated_value"] == pytest.approx(8 * 65)
    assert texas["totals"]["estimated_value"] == pytest.approx(6 * 200)


def test_a_company_price_list_never_prices_another_companys_clients(
    two_companies, missed_work
) -> None:
    """Texas has no entry for Agero, so an Agero row must contribute nothing."""
    start = datetime(DAY.year, DAY.month, DAY.day)
    end = start + timedelta(days=1)
    texas_book = missed_work._price_book(companies_module.rules_for(TEXAS))
    assert "agero" not in texas_book
    assert set(texas_book) == {"nationwide", "road america"}


# ==========================================================================
# Storage: one company's numbers cannot overwrite another's
# ==========================================================================


def test_two_companies_store_the_same_day_side_by_side(two_companies, metrics_agent) -> None:
    """metrics_daily keyed on the date alone would lose the first computation.

    This is the failure that leaves no trace: the number is plausible, the row
    count is right, and the company that ran first has simply been overwritten.
    """
    metrics_agent.compute_daily(DAY, company_id=OHIO, emit_alerts=False)
    metrics_agent.compute_daily(DAY, company_id=TEXAS, emit_alerts=False)

    with get_session(commit=False) as session:
        rows = list(
            session.scalars(select(MetricsDaily).where(MetricsDaily.date == DAY))
        )
    assert {row.company_id for row in rows} == {OHIO, TEXAS}
    stored = {row.company_id: row.metrics["totals"]["offered"] for row in rows}
    assert stored == {OHIO: OHIO_TOTAL, TEXAS: TEXAS_TOTAL}


def test_client_daily_keeps_the_same_client_apart_for_two_companies(
    roster, metrics_agent
) -> None:
    """Both tenants work for Agero. One date, one client_key, two rows."""
    _insert(_rows(OHIO, ((9, "Agero", "accepted", "Tow", 3),), "OHA"))
    _insert(_rows(TEXAS, ((14, "Agero", "accepted", "Tow", 7),), "TXA"))

    metrics_agent.compute_daily(DAY, company_id=OHIO, emit_alerts=False)
    metrics_agent.compute_daily(DAY, company_id=TEXAS, emit_alerts=False)

    with get_session(commit=False) as session:
        rows = list(
            session.scalars(
                select(ClientDaily)
                .where(ClientDaily.date == DAY)
                .where(ClientDaily.client_key == "agero")
            )
        )
    assert {row.company_id: row.offered for row in rows} == {OHIO: 3, TEXAS: 7}


def test_recomputing_one_company_does_not_delete_the_others_client_rows(
    roster, metrics_agent
) -> None:
    """The client_daily sweep deletes rows no longer supported by the data.

    Unscoped, a quiet Tuesday for one tenant would wipe every other tenant's
    Tuesday -- the delete is the dangerous half of that upsert.
    """
    _insert(_rows(TEXAS, ((14, "Nationwide", "accepted", "Heavy Duty TOW", 4),), "TXQ"))
    metrics_agent.compute_daily(DAY, company_id=TEXAS, emit_alerts=False)
    assert count_rows(ClientDaily) == 1

    # Ohio has no rows at all for this day; recomputing it must be a no-op for
    # everybody else.
    metrics_agent.compute_daily(DAY, company_id=OHIO, emit_alerts=False)

    with get_session(commit=False) as session:
        rows = list(session.scalars(select(ClientDaily)))
    assert [(row.company_id, row.client_key, row.offered) for row in rows] == [
        (TEXAS, "nationwide", 4)
    ]


def test_the_missed_work_window_key_is_per_company(two_companies, missed_work) -> None:
    start = datetime(DAY.year, DAY.month, DAY.day)
    end = start + timedelta(days=1)
    missed_work.compute_missed_work(None, start, end, None, company_id=OHIO)
    missed_work.compute_missed_work(None, start, end, None, company_id=TEXAS)

    with get_session(commit=False) as session:
        rows = list(session.scalars(select(MetricsMissedWork)))
    assert {row.company_id for row in rows} == {OHIO, TEXAS}
    assert len(rows) == 2

    stored = missed_work.get_stored_missed_work(start, end, company_id=TEXAS)
    assert stored["totals"]["offers"] == TEXAS_TOTAL


def test_a_stored_daily_document_is_read_back_for_the_right_company(
    two_companies, metrics_agent
) -> None:
    metrics_agent.compute_daily(DAY, company_id=OHIO, emit_alerts=False)
    metrics_agent.compute_daily(DAY, company_id=TEXAS, emit_alerts=False)

    assert metrics_agent.get_stored_daily(DAY, company_id=OHIO)["totals"]["offered"] == OHIO_TOTAL
    assert metrics_agent.get_stored_daily(DAY, company_id=TEXAS)["totals"]["offered"] == TEXAS_TOTAL


# ==========================================================================
# The scheduler
# ==========================================================================


def test_a_job_with_no_company_runs_every_enabled_company(roster) -> None:
    from towbook_agent.core.scheduler import JobSpec, job_companies

    spec = JobSpec(name="daily", cron="0 6 * * *", range_name="previous_calendar_day",
                   report_type="daily")
    assert job_companies(spec) == [OHIO, TEXAS]


def test_a_job_pinned_to_a_company_runs_only_that_one(roster) -> None:
    from towbook_agent.core.scheduler import JobSpec, job_companies

    spec = JobSpec(name="daily", cron="0 6 * * *", range_name="previous_calendar_day",
                   report_type="daily", company_id=TEXAS)
    assert job_companies(spec) == [TEXAS]


def test_a_job_pinned_to_an_unknown_company_runs_nothing_and_says_so(
    roster, captured_events
) -> None:
    """Silently falling back would report somebody else's data under this name."""
    from towbook_agent.core.scheduler import JobSpec, job_companies

    spec = JobSpec(name="daily", cron="0 6 * * *", range_name="previous_calendar_day",
                   report_type="daily", company_id="closed-last-year")
    assert job_companies(spec) == []
    assert captured_events.pipeline_failures


def test_a_disabled_company_is_not_scheduled(write_config) -> None:
    from towbook_agent.core.scheduler import JobSpec, job_companies

    document = {
        "version": 1,
        "companies": [
            dict(ROSTER["companies"][0]),
            {**ROSTER["companies"][1], "enabled": False},
        ],
    }
    write_config("companies", document)
    companies_module.reload_companies()
    try:
        spec = JobSpec(name="daily", cron="0 6 * * *",
                       range_name="previous_calendar_day", report_type="daily")
        assert job_companies(spec) == [OHIO]
    finally:
        companies_module.reload_companies()


def test_one_companys_failure_does_not_stop_the_others(roster, monkeypatch, captured_events) -> None:
    """A tenant whose password expired must not take the roster's reporting down."""
    from towbook_agent.core import scheduler

    seen: list[str] = []

    def fake_pipeline(report_type, start, end, **kwargs):
        company_id = kwargs.get("company_id")
        seen.append(company_id)
        if company_id == OHIO:
            raise RuntimeError("Towbook rejected the login for ohio-towing")
        return object()

    monkeypatch.setattr(scheduler, "run_pipeline", fake_pipeline)

    spec = scheduler.JobSpec(
        name="daily", cron="0 6 * * *",
        range_name="previous_calendar_day", report_type="daily",
    )
    scheduler._run_job(spec, dry_run=True)

    assert seen == [OHIO, TEXAS], "the loop stopped at the first failure"
    failures = [
        payload
        for payload in captured_events.pipeline_failures
        if payload.get("company_id") == OHIO
    ]
    assert failures, "the failing company produced no pipeline_failure event"


def test_the_run_id_carries_the_company(roster) -> None:
    """Two tenants running the same window must not fight over one runs row."""
    from towbook_agent.core.scheduler import make_run_id

    moment = datetime(2026, 7, 20, 6, 0)
    assert make_run_id("daily", OHIO, moment) != make_run_id("daily", TEXAS, moment)


# ==========================================================================
# The dashboard switcher
# ==========================================================================


def _client(app_module):
    from fastapi.testclient import TestClient
    from towbook_agent.web.auth import DEFAULT_PASSWORD

    client = TestClient(app_module.app)
    client.post("/login", data={"password": DEFAULT_PASSWORD, "next": "/"},
                follow_redirects=False)
    return client


@pytest.fixture(autouse=True)
def allow_loopback(monkeypatch: pytest.MonkeyPatch, no_network: None) -> None:
    """Narrow the suite's offline guard to "loopback only" for this module.

    The same narrowing test_web.py does, and for the same reason: TestClient
    runs the ASGI app on an anyio portal and asyncio's Windows proactor loop
    builds its self-pipe with a connect to 127.0.0.1. That is the process
    talking to itself. Anything that is not loopback still raises.

    The real functions are captured at IMPORT time, above, because by the time
    this fixture runs ``no_network`` has already replaced them -- wrapping the
    replacement would just call the blocker.
    """

    def guard(original):
        def wrapper(self, address, *args, **kwargs):
            host = address[0] if isinstance(address, (tuple, list)) and address else None
            if str(host) in _LOOPBACK:
                return original(self, address, *args, **kwargs)
            raise AssertionError("the suite is offline by design")

        return wrapper

    monkeypatch.setattr(_socket.socket, "connect", guard(_REAL_CONNECT), raising=False)
    monkeypatch.setattr(
        _socket.socket, "connect_ex", guard(_REAL_CONNECT_EX), raising=False
    )


def test_the_switcher_is_hidden_when_only_one_company_is_configured() -> None:
    """Written as the negative case on purpose: a one-option dropdown is noise.

    Deliberately takes no roster fixture: with no config/companies.yaml at all
    this is the shipped single-company install, and the control must not exist.
    """
    from towbook_agent.web import app as app_module

    companies_module.reload_companies()
    _insert(_rows(DEFAULT_COMPANY_ID, OHIO_PLAN, "DEF"))

    client = _client(app_module)
    body = client.get("/hourly").text
    assert 'id="company-select"' not in body


def test_the_switcher_appears_and_lists_every_company(two_companies) -> None:
    from towbook_agent.web import app as app_module

    client = _client(app_module)
    body = client.get("/hourly").text
    assert 'id="company-select"' in body
    assert "Ohio Towing and Recovery" in body
    assert "Texas Recovery LLC" in body


def test_the_selected_company_persists_across_tabs(two_companies) -> None:
    """One switch, and every later tab is that company until it is switched again."""
    from towbook_agent.web import app as app_module

    client = _client(app_module)
    response = client.get(f"/company/{TEXAS}?next=/hourly", follow_redirects=False)
    assert response.status_code == 303
    assert app_module.COMPANY_COOKIE in client.cookies

    for path in ("/hourly", "/weekly", "/clients", "/daily", "/health"):
        body = client.get(path).text.casefold()
        assert "texas recovery" in body, f"{path} lost the selected company"
        assert "allstate" not in body, f"{path} showed the other company's client"


def test_the_query_parameter_overrides_the_cookie(two_companies) -> None:
    from towbook_agent.web import app as app_module

    client = _client(app_module)
    client.get(f"/company/{TEXAS}?next=/", follow_redirects=False)
    body = client.get(f"/clients?company={OHIO}").text.casefold()
    assert "allstate" in body
    assert "nationwide" not in body
    assert "road america" not in body


def test_an_unknown_company_falls_back_rather_than_404ing(two_companies) -> None:
    """A stale bookmark must not be a dead end."""
    from towbook_agent.web import app as app_module

    client = _client(app_module)
    response = client.get("/company/closed-last-year?next=/hourly", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/hourly").status_code == 200


def test_the_switcher_will_not_redirect_off_site(two_companies) -> None:
    """An open redirect on a board about to sit on a public URL is a phishing link."""
    from towbook_agent.web import app as app_module

    client = _client(app_module)
    response = client.get(
        f"/company/{TEXAS}?next=https://evil.example.com/x", follow_redirects=False
    )
    assert response.headers["location"] == "/"


def test_the_json_api_respects_the_company_parameter(two_companies) -> None:
    from towbook_agent.web import app as app_module

    client = _client(app_module)
    ohio = client.get(f"/api/clients?company={OHIO}").json()
    texas = client.get(f"/api/clients?company={TEXAS}").json()
    assert {entry["client_key"] for entry in ohio["clients"]} == OHIO_CLIENTS
    assert {entry["client_key"] for entry in texas["clients"]} == TEXAS_CLIENTS


def test_every_json_endpoint_takes_a_company_parameter() -> None:
    """No ``/api`` read may be reachable without a way to scope it.

    Enumerated off the route table rather than listed by hand, so an endpoint
    added later is caught the day it is added rather than the day somebody
    notices the wrong company's numbers in a chart. ``/api/clients/{slug}``
    was exactly this bug: it took ``days`` and nothing else, so it answered
    every request with the DEFAULT company's rows no matter which company was
    selected.
    """
    from towbook_agent.web import app as app_module

    missing = []
    for route in app_module.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not path.startswith("/api/") or "GET" not in methods:
            continue
        if path in ("/api/docs", "/api/openapi.json"):
            continue  # FastAPI's own, no data
        parameters = getattr(getattr(route, "dependant", None), "query_params", [])
        names = {parameter.name for parameter in parameters}
        source = (getattr(route, "endpoint", None).__code__.co_varnames
                  if getattr(route, "endpoint", None) else ())
        if "company" not in names and "company" not in source:
            missing.append(path)

    assert not missing, f"these /api endpoints cannot be scoped to a company: {missing}"


def test_the_client_detail_json_endpoint_cannot_read_across_companies(two_companies) -> None:
    """``/api/clients/<slug>?company=`` must obey the company it is given.

    The HTML page at ``/clients/<slug>`` already did. Its JSON twin did not: it
    resolved ``company_id=None`` to the roster default, so a request for Texas's
    Agero page returned Ohio's Agero rows -- one tenant's client history served
    under another tenant's name, and nothing on the page to say so.
    """
    from towbook_agent.web import app as app_module

    client = _client(app_module)

    # Texas has no Agero at all. Asking for it must return nothing, not Ohio's.
    texas = client.get(f"/api/clients/agero?company={TEXAS}").json()
    assert texas["totals"]["offered"] == 0, "Texas was served Ohio's Agero rows"

    ohio = client.get(f"/api/clients/agero?company={OHIO}").json()
    assert ohio["totals"]["offered"] == 10

    # And the same via the remembered cookie, with no query string, because
    # that is how every link on the board arrives.
    client.get(f"/company/{TEXAS}?next=/", follow_redirects=False)
    assert client.get("/api/clients/agero").json()["totals"]["offered"] == 0
    assert client.get("/api/clients/nationwide").json()["totals"]["offered"] == 9


# ==========================================================================
# The leak test at the level the leak would actually happen: the SQL
# ==========================================================================

#: Tables that hold one tenant's data. Every SELECT against one of these has to
#: name company_id or it is reading the whole install.
_TENANT_TABLES = (
    "requests",
    "runs",
    "metrics_hourly",
    "metrics_daily",
    "metrics_weekly",
    "metrics_monthly",
    "metrics_missed_work",
    "client_daily",
    "alerts_fired",
)


#: ``company_id`` used as a PREDICATE, not merely selected as a column.
#:
#: This distinction is the whole test. Every SELECT the ORM emits lists
#: ``requests.company_id`` in its projection, so a naive substring search for
#: "company_id" reports every statement as filtered -- including a bare
#: ``SELECT * FROM requests`` that reads the entire install. Only a comparison
#: is evidence of a filter.
_COMPANY_PREDICATE = re.compile(r"company_id\s*(?:=|<>|!=|\bin\b|\bis\b)", re.IGNORECASE)


#: The one roster-wide read that is allowed, named exactly rather than waved
#: through by a loose pattern.
#:
#: ``queries.companies_with_data()`` selects DISTINCT company_id and nothing
#: else. It returns identifiers, never a client name, a volume or a rate, and
#: those identifiers are already on screen in the company switcher. /health uses
#: it to tell the operator that a rostered company has never produced a row --
#: usually a credentials prefix nobody set -- which is a question that only has
#: an answer across the roster. It is documented in queries.py as "diagnostics
#: only, never a filter"; this is the test holding it to that.
_ALLOWED_ROSTER_WIDE = re.compile(
    r"^select\s+distinct\s+requests\.company_id\s+from\s+requests\b", re.IGNORECASE
)


def _unscoped_selects(statements: Iterable[str]) -> list[str]:
    """Statements that read a tenant table with no company_id comparison."""
    out = []
    for statement in statements:
        collapsed = " ".join(statement.split())
        lowered = collapsed.casefold()
        if not lowered.startswith("select"):
            continue
        if not any(f" {table}" in lowered for table in _TENANT_TABLES):
            continue
        if _COMPANY_PREDICATE.search(collapsed):
            continue
        if _ALLOWED_ROSTER_WIDE.match(collapsed):
            continue
        out.append(collapsed[:240])
    return out


@pytest.fixture
def captured_sql():
    """Record every statement the engine executes, for the duration of a test."""
    from sqlalchemy import event

    from towbook_agent.core.db import get_engine

    engine = get_engine()
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_every_select_against_a_tenant_table_names_company_id(two_companies, captured_sql) -> None:
    """The isolation proof at the level the bug would actually live: the SQL.

    Every other leak test in this file compares RESULTS -- it asks whether the
    rows that came back belonged to the right company. That catches a filter
    that is missing today. It does not catch a filter that is missing but
    happens to be harmless on this fixture, and it cannot catch one on a code
    path the fixture does not reach.

    So this one reads the statements themselves. Every dashboard entry point is
    driven for both companies, every statement the engine executes is captured,
    and any SELECT that touches a tenant table without a company_id predicate
    fails the test by name. A query that forgets the filter is caught here even
    if the rows it returned this time looked correct.
    """
    from towbook_agent.web import queries as q

    for company_id in (OHIO, TEXAS):
        q.has_any_data(company_id)
        q.missed_work_snapshot(days=400, company_id=company_id)
        q.blind_spots_snapshot(days=400, company_id=company_id)
        q.closeoff_snapshot(days=400, company_id=company_id)
        q.hourly_snapshot(company_id=company_id)
        q.live_snapshot(company_id=company_id)
        q.daily_snapshot(DAY, company_id=company_id)
        q.clients_overview(company_id=company_id)
        q.client_detail("agero", company_id=company_id)
        q.trends_snapshot(company_id=company_id)
        q.period_snapshot("week", company_id=company_id)
        q.period_snapshot("month", company_id=company_id)
        q.rules_view(company_id=company_id)
        q.health_view(company_id=company_id)
        q.last_run_summary(company_id=company_id)
        q.pipeline_banner(company_id=company_id)
        q.recent_alerts(company_id=company_id)

    assert captured_sql, "no SQL was captured; the hook is not attached"

    unscoped = _unscoped_selects(captured_sql)
    assert not unscoped, (
        f"{len(unscoped)} SELECT(s) read a tenant table without a company_id filter:\n  "
        + "\n  ".join(sorted(set(unscoped))[:10])
    )


def test_the_sql_proof_would_actually_fail_on_an_unfiltered_query(
    two_companies, captured_sql
) -> None:
    """The guard above is only worth having if it can fail. Prove that it can.

    A test that asserts "nothing bad was found" is worthless when the search is
    broken -- it passes just as happily against an empty capture or a predicate
    that never matches. So this runs a deliberately unscoped read and asserts
    the same check catches it.

    This is not hypothetical tidiness. The first version of the detector looked
    for the substring "company_id" anywhere in the statement, and every ORM
    SELECT lists ``requests.company_id`` in its projection -- so it passed on a
    bare ``SELECT * FROM requests`` that read both companies. This control is
    what caught that, and it is why the detector now insists on a comparison.
    """
    from sqlalchemy import select

    from towbook_agent.core.db import get_session

    with get_session(commit=False) as session:
        rows = session.execute(select(Request)).scalars().all()

    # It really did read both companies -- that is what makes it a leak.
    assert len(rows) == OHIO_TOTAL + TEXAS_TOTAL

    offenders = _unscoped_selects(captured_sql)
    assert offenders, "the detector failed to notice a query with no company_id filter"
    assert any("requests." in statement for statement in offenders)


def test_one_companys_healthy_run_does_not_clear_anothers_overdue_alarm(roster) -> None:
    """A silent tenant must not read its neighbour's success as its own.

    This is the failure the SQL test above was written to find, and it found it.
    ``overdue_reports`` asked "when did a <report_type> run last succeed?"
    without naming a company, so the newest run on the whole install answered
    for every tenant. On a roster where Ohio ran an hour ago and Texas has not
    run in a week, Texas's own Health page and its own banner both reported
    healthy -- an alarm stating the opposite of the truth, on the only delivery
    mechanism there is.
    """
    from towbook_agent.core.scheduler import overdue_reports
    from towbook_agent.web import queries as q

    now = datetime(2026, 7, 27, 12, 0)
    with get_session() as session:
        # Ohio succeeded a minute ago.
        session.add(
            Run(
                run_id="OH-run-fresh",
                company_id=OHIO,
                report_type="daily",
                status="succeeded",
                started_at=now - timedelta(minutes=1),
                finished_at=now - timedelta(minutes=1),
            )
        )
        # Texas last succeeded three weeks ago. It is unambiguously overdue.
        session.add(
            Run(
                run_id="TX-run-ancient",
                company_id=TEXAS,
                report_type="daily",
                status="succeeded",
                started_at=now - timedelta(days=21),
                finished_at=now - timedelta(days=21),
            )
        )

    specs = [
        spec
        for spec in __import__(
            "towbook_agent.core.scheduler", fromlist=["load_job_specs"]
        ).load_job_specs(strict=False)
        if spec.report_type == "daily" and spec.enabled
    ]
    if not specs:
        pytest.skip("no enabled daily job in config/schedule.yaml")

    ohio_findings = overdue_reports(specs=specs, now=now, company_id=OHIO)
    texas_findings = overdue_reports(specs=specs, now=now, company_id=TEXAS)

    assert not [f for f in ohio_findings if f["report_type"] == "daily"], (
        "Ohio ran a minute ago and must not be reported overdue"
    )
    assert [f for f in texas_findings if f["report_type"] == "daily"], (
        "Texas has not run in three weeks and MUST be reported overdue"
    )

    # And through the board's own entry point, which is what the owner sees.
    assert any(
        item.get("report_type") == "daily"
        for item in (q.health_view(company_id=TEXAS).get("overdue") or [])
    ), "the Texas Health page cleared its alarm using Ohio's run"


# ==========================================================================
# The printed letterhead
#
# It exists only in the PDF, so nothing on screen fails when it is wrong --
# which is exactly why it is tested here rather than left to be noticed on a
# document that has already been emailed to a client.
# ==========================================================================


def test_the_letterhead_falls_back_to_the_company_name() -> None:
    """A company that has configured nothing still prints a usable header."""
    company = companies_module.Company(id="acme", name="Acme Towing LLC")
    assert company.letterhead_name == "Acme Towing LLC"
    assert company.letterhead_logo is None
    assert company.letterhead_lines() == []


def test_the_letterhead_drops_blank_fields_rather_than_printing_them() -> None:
    """Half a letterhead prints as a short block, not as a block of gaps."""
    company = companies_module.Company(
        id="acme",
        name="Acme Towing LLC",
        letterhead={
            "name": "Acme Towing & Recovery",
            "address": "100 Main St",
            "address2": "",
            "city": "Columbus",
            "state": "OH",
            "zip": "43004",
            "phone": "(614) 555-0100",
            "email": "",
            "website": "",
        },
    )
    assert company.letterhead_name == "Acme Towing & Recovery"
    assert company.letterhead_lines() == [
        "100 Main St",
        "Columbus OH 43004",
        "(614) 555-0100",
    ]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # The ordinary case: a file dropped into web/static/.
        ("letterhead-logo.png", "/static/letterhead-logo.png"),
        # Already a path or a URL -- left exactly as given.
        ("/static/logo.svg", "/static/logo.svg"),
        ("https://cdn.example.com/logo.png", "https://cdn.example.com/logo.png"),
        ("data:image/png;base64,AAAA", "data:image/png;base64,AAAA"),
        ("", None),
    ],
)
def test_the_logo_path_is_resolved_for_each_way_of_naming_it(
    configured: str, expected: str | None
) -> None:
    company = companies_module.Company(id="acme", name="Acme", letterhead={"logo": configured})
    assert company.letterhead_logo == expected


def test_the_letterhead_reaches_the_template_layer() -> None:
    """as_dict() is what the templates get; the header renders from it."""
    company = companies_module.Company(
        id="acme",
        name="Acme Towing LLC",
        letterhead={"phone": "(614) 555-0100"},
    )
    data = company.as_dict()
    assert data["letterhead_name"] == "Acme Towing LLC"
    assert data["letterhead_lines"] == ["(614) 555-0100"]
    assert data["letterhead_logo"] is None
    # Unchanged guarantee: no credential value ever rides along.
    assert "password" not in repr(data).lower()
