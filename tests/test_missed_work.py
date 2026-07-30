"""The missed-work model, against numbers counted by hand.

Every expected value below was worked out from :data:`PLAN` by counting, not by
running the code and pasting the answer back in. That is the only way a metrics
test means anything.

Four properties matter as much as the arithmetic, because they are the ones the
owner's question rests on:

* **The buckets are DATA.** Re-cutting ``missed_work.buckets`` in rules.yaml
  changes the answer with no code change. There is not one status code in
  agents/missed_work.py, and :func:`test_rewriting_the_buckets_rewrites_the_answer`
  proves it by moving a status into a different bucket at runtime.
* **The client_withdrew setting can never hide a number.** Whatever
  ``count_client_withdrew_as_recoverable`` says, both the with- and
  without-withdrawals totals appear in the document.
* **Nothing implies revenue.** ``offerAmount`` is empty on 100% of real records,
  so ``revenue_available`` is false and ``ranking_basis`` is ``job_count``
  until a human supplies per-client job values -- and even then no default is
  invented for a client that has none.
* **Recomputation is idempotent** (hard constraint #4). The unique key on
  ``(window_start, window_end, period_type)`` is what turns a re-run into an
  upsert.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

import pytest
import yaml
from sqlalchemy import select

from conftest import count_rows
from towbook_agent.core.config_loader import get_rules
from towbook_agent.core.db import get_session
from towbook_agent.core.models import MetricsMissedWork, Request, client_key_for

# --------------------------------------------------------------------------
# The hand-counted dataset
#
# 2026-07-20 is a Monday. TZ is UTC in the suite, so these local hours are also
# the stored UTC hours and the arithmetic below is direct.
#
# Statuses are the VERBATIM portal labels, because that is what the portal
# writes and what ingestion now stores in `status_raw`. The point of the whole
# model is that "Expired" and "Accept Failed" are different failures even though
# both collapse to canonical `expired`.
#
#   Agero, hour 09  (a good hour: somebody was watching)
#       3 Accepted, 1 Rejected/Equipment Not Available, 1 Cancelled          = 5
#   Agero, hour 18  (the blind spot: nobody answered)
#       1 Accepted, 6 Expired, 1 Rejected/No Drivers Available               = 8
#   Agero, hour 19  (light service nobody wants, all refused)
#       6 Expired (Tire Change)                                              = 6
#   Allstate, hour 09
#       4 Accepted, 1 Accept Failed, 2 Rejected By Motor Club                = 7
#   Allstate, hour 18
#       3 Accepted, 1 Accepting                                              = 4
#
#   TOTAL 30 offers
#     won              3 + 1 + 4 + 3          = 11
#     no_response      6 + 6                  = 12
#     declined         1 + 1                  =  2
#     accept_failed                           =  1
#     client_withdrew  1 (Cancelled) + 2 (RBMC) = 3
#     in_flight        1 (Accepting)          =  1
#     missed  = 12 + 2 + 1 + 3               = 18
#     recoverable (default, withdrew excluded) = 15
#     recoverable (withdrew folded in)         = 18
# --------------------------------------------------------------------------

DAY = date(2026, 7, 20)
WINDOW_START = datetime(2026, 7, 20, 0, 0)
WINDOW_END = datetime(2026, 7, 21, 0, 0)

AGERO = "Agero"
ALLSTATE = "Allstate"

TOW = "Light Duty Tow"
TIRE = "Tire Change"

#: (hour, client, status label, service type, denial reason, count)
PLAN: tuple[tuple[int, str, str, str, str, int], ...] = (
    (9, AGERO, "Accepted", TOW, "", 3),
    (9, AGERO, "Rejected", TOW, "Equipment Not Available", 1),
    (9, AGERO, "Cancelled", TOW, "", 1),
    (18, AGERO, "Accepted", TOW, "", 1),
    (18, AGERO, "Expired", TOW, "", 6),
    (18, AGERO, "Rejected", TOW, "No Drivers Available", 1),
    (19, AGERO, "Expired", TIRE, "", 6),
    (9, ALLSTATE, "Accepted", TOW, "", 4),
    (9, ALLSTATE, "Accept Failed", TOW, "", 1),
    (9, ALLSTATE, "Rejected By Motor Club", TOW, "", 2),
    (18, ALLSTATE, "Accepted", TOW, "", 3),
    (18, ALLSTATE, "Accepting", TOW, "", 1),
)

OFFERS = 30
WON = 11
NO_RESPONSE = 12
DECLINED = 2
ACCEPT_FAILED = 1
CLIENT_WITHDREW = 3
IN_FLIGHT = 1
MISSED = 18
RECOVERABLE_DEFAULT = 15

#: Canonical status per portal label, so the seeded rows look exactly like rows
#: that came through ingestion. Mirrors config/schema.yaml status_vocabulary.
CANONICAL = {
    "Accepted": "accepted",
    "Rejected": "denied",
    "Cancelled": "canceled",
    "Expired": "expired",
    "Accept Failed": "expired",
    "Rejected By Motor Club": "canceled",
    "Accepting": "pending",
}

#: Numeric codes, so the code-first resolution path can be exercised too.
CODES = {
    "Accepted": 1,
    "Rejected": 2,
    "Cancelled": 4,
    "Expired": 5,
    "Accept Failed": 21,
    "Rejected By Motor Club": 40,
    "Accepting": 6,
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def missed_work():
    from conftest import load_agent

    return load_agent("missed_work")


def build_rows(*, with_status_raw: bool = True, with_code: bool = False) -> list[dict[str, Any]]:
    """Seeded Request rows.

    ``with_status_raw`` / ``with_code`` control which of the three resolution
    routes a row can take, so each one can be tested on its own.
    """
    rows: list[dict[str, Any]] = []
    counter = 0
    for hour, client, label, service, reason, count in PLAN:
        for index in range(count):
            counter += 1
            rows.append(
                {
                    "request_id": f"MW-{counter:04d}",
                    "account_id": "default",
                    "client_name": client,
                    "client_key": client_key_for(client),
                    "offered_at": datetime(2026, 7, 20, hour, (index * 3) % 60),
                    "status": CANONICAL[label],
                    "status_raw": label if with_status_raw else None,
                    "status_code": CODES[label] if with_code else None,
                    "denial_reason": reason or None,
                    "service_type_raw": service,
                    "service_class": "tow" if service == TOW else "light_service",
                }
            )
    return rows


def add_requests(rows: Iterable[dict[str, Any]]) -> None:
    """Insert Request rows directly.

    The model is tested without going through ingestion on purpose: a
    missed-work failure must not be maskable by an ingestion bug, or vice versa.
    """
    with get_session() as session:
        for row in rows:
            session.add(Request(**row))


@pytest.fixture
def seeded() -> list[dict[str, Any]]:
    rows = build_rows()
    add_requests(rows)
    return rows


def compute(missed_work, **kwargs):
    with get_session() as session:
        return missed_work.compute_missed_work(
            session, WINDOW_START, WINDOW_END, None, **kwargs
        )


def rewrite_rules(write_config, mutate) -> dict[str, Any]:
    """Edit the sandbox rules.yaml through the hot-reloading loader."""
    from towbook_agent.core.config_loader import CONFIG

    data = yaml.safe_load((CONFIG.config_dir / "rules.yaml").read_text(encoding="utf-8"))
    mutate(data)
    write_config("rules.yaml", data)
    return get_rules()


# ==========================================================================
# Section 1 -- buckets
# ==========================================================================


def test_every_offer_lands_in_exactly_one_bucket(missed_work, seeded) -> None:
    document = compute(missed_work, persist=False)
    counts = {name: entry["count"] for name, entry in document["by_bucket"].items()}

    assert counts["won"] == WON
    assert counts["no_response"] == NO_RESPONSE
    assert counts["declined"] == DECLINED
    assert counts["accept_failed"] == ACCEPT_FAILED
    assert counts["client_withdrew"] == CLIENT_WITHDREW
    assert counts["in_flight"] == IN_FLIGHT
    # Exactly one: the buckets partition the offers with nothing left over and
    # nothing counted twice.
    assert sum(counts.values()) == OFFERS == document["totals"]["offers"]


def test_the_status_label_separates_what_the_canonical_status_merges(
    missed_work, seeded
) -> None:
    """This is the reason status_raw exists.

    Canonical `expired` covers Expired (nobody answered) AND Accept Failed (we
    answered and the accept failed) -- one is a staffing problem, the other is a
    technical one, and they have different remedies. Canonical `denied` and
    `canceled` likewise merge "we said no" with "the client pulled it".
    """
    with get_session(commit=False) as session:
        rows = {row.request_id: row for row in session.scalars(select(Request))}

    expired = [r for r in rows.values() if r.status == "expired"]
    assert len(expired) == NO_RESPONSE + ACCEPT_FAILED, "the canonical status merges them"

    buckets = {missed_work.classify_bucket(row) for row in expired}
    assert buckets == {"no_response", "accept_failed"}, "the model must separate them"


def test_the_numeric_code_wins_over_every_other_route(missed_work) -> None:
    """Resolution order: code, then label, then canonical status."""
    # A row whose three fields disagree. The code says Accepted.
    row = {"status_code": 1, "status_raw": "Expired", "status": "denied"}
    assert missed_work.classify_bucket(row) == "won"

    # No code: the verbatim label wins over the canonical status.
    assert missed_work.classify_bucket({"status_raw": "Expired", "status": "denied"}) == (
        "no_response"
    )

    # Neither: fall back to the canonical status, which is coarser and says so.
    assert missed_work.classify_bucket({"status": "denied"}) == "declined"


def test_a_status_nothing_recognises_is_visible_not_guessed(missed_work) -> None:
    """A status no map knows must never be quietly filed under `won`."""
    assert missed_work.classify_bucket({"status_raw": "Teleported To Mars"}) == (
        "unknown_status"
    )

    add_requests(
        [
            {
                "request_id": "MW-ALIEN",
                "account_id": "default",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 10, 0),
                "status": None,
                "status_raw": "Teleported To Mars",
                "service_type_raw": TOW,
                "service_class": "tow",
            }
        ]
    )
    document = compute(missed_work, persist=False)
    assert document["by_bucket"]["unknown_status"]["count"] == 1
    assert document["unknown_statuses"][0]["status_raw"] == "Teleported To Mars"
    # It is NOT counted as missed work. An unreadable status is a data defect to
    # fix, not a job to claim we lost.
    assert document["totals"]["missed"] == 0


def test_a_label_rescued_by_the_coarse_map_is_still_reported(missed_work) -> None:
    """The dangerous case is not the status nothing knows -- it is the one the
    coarse canonical map can absorb. It gets a plausible bucket and so never
    looks wrong, which is exactly how a report starts being confidently
    incorrect. It has to be named."""
    add_requests(
        [
            {
                "request_id": "MW-NEWSTATUS",
                "account_id": "default",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 10, 0),
                # schema.yaml knows this label; missed_work.buckets does not.
                "status": "accepted",
                "status_raw": "Tow Complete",
                "service_type_raw": TOW,
                "service_class": "tow",
            }
        ]
    )
    document = compute(missed_work, persist=False)
    # It is bucketed sensibly rather than thrown away...
    assert document["by_bucket"]["won"]["count"] == 1
    assert document["bucket_sources"] == {"canonical_status": 1}
    # ...and it is named, with the bucket it was given.
    assert document["unknown_statuses"] == [
        {
            "status_raw": "Tow Complete",
            "status": "accepted",
            "bucketed_as": "won",
            "count": 1,
        }
    ]


def test_the_document_says_which_route_bucketed_each_row(missed_work) -> None:
    """A report resting on the coarse canonical map has to say so."""
    add_requests(build_rows(with_status_raw=False))
    document = compute(missed_work, persist=False)
    assert document["bucket_sources"] == {"canonical_status": OFFERS}

    # And on that coarse route the one Accept Failed row is unavoidably folded
    # into no_response, because canonical `expired` cannot tell them apart.
    counts = {name: entry["count"] for name, entry in document["by_bucket"].items()}
    assert counts["no_response"] == NO_RESPONSE + ACCEPT_FAILED
    assert counts["accept_failed"] == 0


def test_rewriting_the_buckets_rewrites_the_answer(
    missed_work, seeded, write_config
) -> None:
    """Hard constraint #2, proved: the buckets are data, not code.

    Move "Expired" from no_response into declined in rules.yaml and the same
    rows produce a different answer with no code change.
    """
    before = compute(missed_work, persist=False)
    assert before["by_bucket"]["no_response"]["count"] == NO_RESPONSE

    def mutate(data: dict[str, Any]) -> None:
        data["missed_work"]["bucket_by_status_name"]["expired"] = "declined"

    rewrite_rules(write_config, mutate)

    after = compute(missed_work, persist=False)
    assert after["by_bucket"]["no_response"]["count"] == 0
    assert after["by_bucket"]["declined"]["count"] == DECLINED + NO_RESPONSE
    # The partition still holds.
    assert sum(e["count"] for e in after["by_bucket"].values()) == OFFERS


# ==========================================================================
# Section 2 -- cause and remedy
# ==========================================================================


def test_cause_and_remedy_come_from_the_reason(missed_work) -> None:
    equipment = {"status_raw": "Rejected", "denial_reason": "Equipment Not Available"}
    assert missed_work.attribute_cause(equipment) == ("equipment", "capital")

    staffing = {"status_raw": "Rejected", "denial_reason": "No Drivers Available"}
    assert missed_work.attribute_cause(staffing) == ("staffing", "scheduling")

    coverage = {"status_raw": "Rejected", "denial_reason": "Out Of Coverage Area"}
    assert missed_work.attribute_cause(coverage) == ("coverage", "territory")

    # The cheap fix. Deliberately its own cause even though it normalizes into
    # the `other` denial-reason bucket.
    info = {"status_raw": "Rejected", "denial_reason": "Not Enough Information"}
    assert missed_work.attribute_cause(info) == ("information", "process")


def test_a_decline_with_no_reason_is_unrecorded_not_invented(missed_work) -> None:
    cause, remedy = missed_work.attribute_cause(
        {"status_raw": "Rejected", "denial_reason": ""}
    )
    assert (cause, remedy) == ("unrecorded", "data")


def test_no_response_always_means_attention(missed_work) -> None:
    """It carries no reason field by definition, so its cause is the bucket."""
    cause, remedy = missed_work.attribute_cause({"status_raw": "Expired"})
    assert cause == "attention"
    assert remedy == "alerting"


def test_an_accepted_job_is_not_labelled_a_failure(missed_work) -> None:
    """A won row carries no denial reason, and must not therefore be filed under
    "we declined without recording why"."""
    assert missed_work.attribute_cause({"status_raw": "Accepted"}) == ("none", "none")


def test_causes_are_counted_and_carry_their_remedy(missed_work, seeded) -> None:
    document = compute(missed_work, persist=False)
    by_cause = document["by_cause"]

    assert by_cause["attention"]["missed"] == NO_RESPONSE
    assert by_cause["attention"]["remedy"] == "alerting"
    assert by_cause["equipment"]["missed"] == 1
    assert by_cause["equipment"]["remedy"] == "capital"
    assert by_cause["staffing"]["missed"] == 1
    assert by_cause["client"]["missed"] == CLIENT_WITHDREW
    assert by_cause["technical"]["missed"] == ACCEPT_FAILED

    # Every cause carries the question a report prints beside the number. That
    # is what turns a count into an action.
    assert by_cause["equipment"]["question"]
    assert sum(entry["missed"] for entry in by_cause.values()) == MISSED


# ==========================================================================
# Section 3 -- totals and the inventory
# ==========================================================================


def test_the_withdrew_setting_can_never_hide_a_number(
    missed_work, seeded, write_config
) -> None:
    """Default false, and BOTH totals present either way."""
    document = compute(missed_work, persist=False)
    totals = document["totals"]

    assert totals["missed"] == MISSED
    assert totals["withdrew"] == CLIENT_WITHDREW
    assert totals["recoverable"] == RECOVERABLE_DEFAULT
    assert totals["count_client_withdrew_as_recoverable"] is False
    # The point: the other number is right there too.
    assert totals["recoverable_excluding_withdrew"] == RECOVERABLE_DEFAULT
    assert totals["recoverable_including_withdrew"] == MISSED

    def mutate(data: dict[str, Any]) -> None:
        data["missed_work"]["count_client_withdrew_as_recoverable"] = True

    rewrite_rules(write_config, mutate)

    flipped = compute(missed_work, persist=False)["totals"]
    assert flipped["recoverable"] == MISSED
    assert flipped["count_client_withdrew_as_recoverable"] is True
    # Still both.
    assert flipped["recoverable_excluding_withdrew"] == RECOVERABLE_DEFAULT
    assert flipped["recoverable_including_withdrew"] == MISSED


def test_the_inventory_is_only_work_we_want(missed_work, seeded) -> None:
    """Filtered to acceptance_policy.should_accept -- the inventory answers "what
    would it take to accept this", which is meaningless for work we refuse."""
    document = compute(missed_work, persist=False)
    classes = {entry["service_class"] for entry in document["inventory"]}
    assert classes <= {"tow", "winch_out"}
    assert "light_service" not in classes

    # The six unanswered Tire Change offers are missed work, and they are
    # counted as such -- they just belong in close-off, not in the inventory.
    assert document["totals"]["missed"] == MISSED
    assert document["inventory_meta"]["missed_in_inventory"] == MISSED - 6


def test_inventory_rows_carry_their_context_and_are_ranked_by_job_count(
    missed_work, seeded
) -> None:
    document = compute(missed_work, persist=False)
    inventory = document["inventory"]
    assert inventory, "there is missed tow work; the inventory must not be empty"

    top = inventory[0]
    assert top["service_class"] == "tow"
    assert top["cause"] == "attention"
    assert top["missed"] == 6  # the 18:00 Agero tows
    assert top["remedy"] == "alerting"
    # offers/accepted are the class's own denominators, so the row can be read
    # on its own.
    assert top["offers"] == OFFERS - 6  # every row except the Tire Changes
    assert top["top_clients"][0]["client"] == AGERO
    assert top["top_service_types"][0]["service_type_raw"] == TOW

    counts = [entry["missed"] for entry in inventory]
    assert counts == sorted(counts, reverse=True), "ranked by job count, descending"


def test_ranking_is_by_job_count_and_says_so(missed_work, seeded, write_config) -> None:
    """offerAmount is empty on 100% of real records. Nothing may imply revenue."""

    def clear_prices(data: dict[str, Any]) -> None:
        data["missed_work"]["job_value_by_client"] = {}

    rewrite_rules(write_config, clear_prices)

    document = compute(missed_work, persist=False)
    assert document["ranking_basis"] == "job_count"
    assert document["revenue_available"] is False
    assert "estimated_value" not in document["totals"]
    for entry in document["inventory"]:
        assert "estimated_value" not in entry


def test_job_values_switch_pricing_on_without_inventing_one(
    missed_work, seeded, write_config
) -> None:
    """A price for Agero and none for Allstate. Allstate's missed jobs must
    contribute nothing at all rather than borrow Agero's number."""

    def price_one_client(data: dict[str, Any]) -> None:
        data["missed_work"]["job_value_by_client"] = {client_key_for(AGERO): 100}

    rewrite_rules(write_config, price_one_client)

    document = compute(missed_work, persist=False)
    assert document["revenue_available"] is True
    # Ranking is still by job count: a price list does not change what the
    # inventory is sorted on.
    assert document["ranking_basis"] == "job_count"

    totals = document["totals"]
    # Agero's missed TOWS only: 6 unanswered + 1 equipment + 1 staffing + 1
    # cancelled = 9. The six Tire Changes are light_service, which the tow
    # averages do not describe, and Allstate has no price at all.
    assert totals["priced_missed"] == 9
    assert totals["estimated_value"] == pytest.approx(900.0)
    assert totals["unpriced_missed"] == MISSED - 9
    # The defensible number is offered alongside the biggest one.
    assert totals["recoverable_estimated_value"] == pytest.approx(800.0)


def test_a_price_that_is_not_a_number_is_ignored_not_coerced(
    missed_work, seeded, write_config
) -> None:
    """A typo'd price silently becoming 0 would understate the very number it
    was added to produce."""

    def bad_price(data: dict[str, Any]) -> None:
        data["missed_work"]["job_value_by_client"] = {client_key_for(AGERO): "ninety"}

    rewrite_rules(write_config, bad_price)

    document = compute(missed_work, persist=False)
    assert document["revenue_available"] is False


# ==========================================================================
# Rates
# ==========================================================================


def test_no_offers_is_no_rate_not_a_zero_rate(missed_work) -> None:
    """Zero percent means "we turned everything down". There is no rate at all
    when nothing was offered, and conflating the two invents a catastrophe."""
    document = compute(missed_work, persist=False)
    totals = document["totals"]
    assert totals["offers"] == 0
    assert totals["acceptance_rate"] is None
    assert totals["missed_rate"] is None
    assert totals["no_response_rate"] is None

    from towbook_agent.agents.metrics import format_rate

    assert format_rate(totals["acceptance_rate"]) == "-"


def test_an_hour_with_no_offers_has_a_null_rate(missed_work, seeded) -> None:
    grid = compute(missed_work, persist=False)["blind_spots"]
    # 03:00 on a Monday carried nothing.
    assert grid["offers"][0][3] == 0
    assert grid["no_response_rate"][0][3] is None
    assert grid["by_hour"][3]["no_response_rate"] is None
    assert grid["by_hour"][3]["is_blind_spot"] is False


# ==========================================================================
# Section 4 -- blind spots
# ==========================================================================


def test_blind_spots_are_a_7_by_24_hour_of_week_grid(missed_work, seeded) -> None:
    grid = compute(missed_work, persist=False)["blind_spots"]
    assert (grid["rows"], grid["cols"]) == (7, 24)
    assert len(grid["offers"]) == 7
    assert all(len(row) == 24 for row in grid["offers"])
    # 2026-07-20 is a Monday -> weekday index 0.
    assert grid["offers"][0][9] == 12  # 5 Agero + 7 Allstate
    assert grid["offers"][0][18] == 12  # 8 Agero + 4 Allstate
    assert grid["no_response"][0][18] == 6


def test_a_cell_is_a_blind_spot_only_above_both_thresholds(
    missed_work, seeded
) -> None:
    grid = compute(missed_work, persist=False)["blind_spots"]
    assert grid["thresholds"] == {"min_offers": 5, "threshold": 0.35}

    flagged = {(cell["weekday"], cell["hour"]) for cell in grid["blind_spots"]}
    # 18:00: 12 offers, 6 unanswered = 50%. Over both thresholds.
    assert ("Mon", 18) in flagged
    # 19:00: 6 offers, 6 unanswered = 100%. Over both thresholds.
    assert ("Mon", 19) in flagged
    # 09:00: 12 offers, 0 unanswered. Volume but no misses.
    assert ("Mon", 9) not in flagged


def test_a_busy_hour_is_needed_before_a_cell_can_be_a_blind_spot(
    missed_work, write_config
) -> None:
    """min_offers is what stops a 1-of-2 hour on a Tuesday night reading as a
    crisis."""
    add_requests(
        [
            {
                "request_id": "MW-QUIET-1",
                "account_id": "default",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 2, 0),
                "status": "expired",
                "status_raw": "Expired",
                "service_type_raw": TOW,
                "service_class": "tow",
            }
        ]
    )
    grid = compute(missed_work, persist=False)["blind_spots"]
    cell = next(c for c in grid["cells"] if c["hour"] == 2)
    assert cell["no_response_rate"] == 1.0, "100% missed"
    assert cell["is_blind_spot"] is False, "but one offer is not evidence"

    def lower_floor(data: dict[str, Any]) -> None:
        data["missed_work"]["blind_spot"]["min_offers"] = 1

    rewrite_rules(write_config, lower_floor)

    grid = compute(missed_work, persist=False)["blind_spots"]
    cell = next(c for c in grid["cells"] if c["hour"] == 2)
    assert cell["is_blind_spot"] is True, "thresholds are data"


def test_blind_spots_is_callable_on_its_own(missed_work, seeded) -> None:
    with get_session(commit=False) as session:
        standalone = missed_work.blind_spots(session, WINDOW_START, WINDOW_END, None)
    embedded = compute(missed_work, persist=False)["blind_spots"]
    assert standalone["blind_spot_count"] == embedded["blind_spot_count"]
    assert standalone["offers"] == embedded["offers"]


# ==========================================================================
# Section 5 -- close-off candidates
# ==========================================================================


def test_closeoff_candidates_are_grouped_by_client(missed_work, seeded) -> None:
    """The action is a conversation with a client, so that is how it is cut."""
    document = compute(missed_work, persist=False)["closeoff_candidates"]

    assert document["totals"]["service_types"] == 1
    assert document["by_service_type"][0]["service_type_raw"] == TIRE
    assert document["by_service_type"][0]["offers"] == 6
    assert document["by_service_type"][0]["accepted"] == 0

    assert len(document["clients"]) == 1
    client = document["clients"][0]
    assert client["client"] == AGERO
    assert client["candidate_offers"] == 6
    assert client["client_offers"] == 19  # every Agero row
    # The share is what sizes the ask.
    assert client["candidate_share_of_client_offers"] == pytest.approx(6 / 19)


def test_closeoff_never_recommends_stopping_work_we_want(missed_work) -> None:
    """Without the should_accept filter a bad week for tows would produce
    "ask this client to stop sending us tows", which is the opposite of the
    point of the whole report."""
    add_requests(
        [
            {
                "request_id": f"MW-BADTOW-{index}",
                "account_id": "default",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                "offered_at": datetime(2026, 7, 20, 11, index),
                "status": "expired",
                "status_raw": "Expired",
                "service_type_raw": TOW,
                "service_class": "tow",
            }
            for index in range(10)
        ]
    )
    document = compute(missed_work, persist=False)["closeoff_candidates"]
    types = {entry["service_type_raw"] for entry in document["by_service_type"]}
    assert TOW not in types
    assert document["excluded_service_classes"] == ["tow", "winch_out"]


def test_closeoff_records_the_baseline_it_expects_to_improve(
    missed_work, seeded
) -> None:
    """Closing these off should raise the response rate on work we want, because
    it removes competing offers from the same 3-minute window. The report has to
    record what that rate was so the claim can be checked next period."""
    document = compute(missed_work, persist=False)["closeoff_candidates"]
    baseline = document["wanted_baseline"]
    assert baseline["service_classes"] == ["tow", "winch_out"]
    assert baseline["offers"] == OFFERS - 6
    assert baseline["no_response"] == 6
    assert baseline["no_response_rate"] == pytest.approx(6 / 24)
    assert document["rationale"]


# ==========================================================================
# Section 6 -- client comparison
# ==========================================================================


def test_client_comparison_counts_every_bucket_per_client(missed_work, seeded) -> None:
    clients = {
        entry["client_key"]: entry
        for entry in compute(missed_work, persist=False)["client_comparison"]["clients"]
    }

    agero = clients[client_key_for(AGERO)]
    assert agero["offers"] == 19
    assert agero["accepted"] == 4
    assert agero["declined"] == 2
    assert agero["no_response"] == 12
    assert agero["withdrew"] == 1
    assert agero["no_response_rate"] == pytest.approx(12 / 19)

    allstate = clients[client_key_for(ALLSTATE)]
    assert allstate["offers"] == 11
    assert allstate["accepted"] == 7
    assert allstate["no_response"] == 0
    assert allstate["withdrew"] == 2
    assert allstate["accept_failed"] == 1
    assert allstate["no_response_rate"] == 0.0


def _lower_client_floor(write_config, gap_pp: float = 20.0) -> None:
    """The shipped floor is 20 offers; this dataset is smaller than that."""

    def mutate(data: dict[str, Any]) -> None:
        data["missed_work"]["client_comparison"]["min_offers"] = 5
        data["missed_work"]["client_comparison"]["gap_pp"] = gap_pp

    rewrite_rules(write_config, mutate)


def test_a_gap_between_clients_is_stated_not_left_to_be_spotted(
    missed_work, seeded, write_config
) -> None:
    """Same trucks, same staff: Agero's offers go unanswered 63% of the time and
    Allstate's never. That is a process difference and the report says so."""
    _lower_client_floor(write_config)

    findings = compute(missed_work, persist=False)["client_comparison"]["findings"]
    assert findings, "a 63-point gap must produce a finding"

    finding = findings[0]
    assert finding["kind"] == "no_response_gap"
    assert finding["worst"]["client"] == AGERO
    assert finding["best"]["client"] == ALLSTATE
    assert finding["spread_pp"] == pytest.approx(12 / 19 * 100, abs=0.01)
    assert AGERO in finding["summary"] and ALLSTATE in finding["summary"]


def test_a_narrow_gap_is_not_a_finding(missed_work, seeded, write_config) -> None:
    _lower_client_floor(write_config, gap_pp=95.0)
    assert compute(missed_work, persist=False)["client_comparison"]["findings"] == []


def test_a_small_client_cannot_become_the_headline(
    missed_work, seeded, write_config
) -> None:
    """min_offers keeps a client with four offers out of a finding."""

    def raise_floor(data: dict[str, Any]) -> None:
        data["missed_work"]["client_comparison"]["min_offers"] = 500

    rewrite_rules(write_config, raise_floor)
    assert compute(missed_work, persist=False)["client_comparison"]["findings"] == []


# ==========================================================================
# Persistence and idempotency
# ==========================================================================


def test_the_document_is_persisted_on_its_window_key(missed_work, seeded) -> None:
    compute(missed_work, period_type="daily")

    with get_session(commit=False) as session:
        row = session.scalars(select(MetricsMissedWork)).one()
    assert row.period_type == "daily"
    assert row.window_start == WINDOW_START
    assert row.window_end == WINDOW_END
    assert row.metrics["totals"]["missed"] == MISSED
    assert row.rules_version


def test_recomputing_a_window_upserts_and_produces_the_same_numbers(
    missed_work, seeded
) -> None:
    """Hard constraint #4."""
    first = compute(missed_work, period_type="daily")
    second = compute(missed_work, period_type="daily")

    assert count_rows(MetricsMissedWork) == 1
    assert missed_work._persistable(first) == missed_work._persistable(second)


def test_a_different_period_type_is_a_different_row(missed_work, seeded) -> None:
    """The daily run for Monday and an ad-hoc look at the same Monday must not
    overwrite one another."""
    compute(missed_work, period_type="daily")
    compute(missed_work, period_type="custom")
    assert count_rows(MetricsMissedWork) == 2


def test_the_period_type_is_derived_from_the_span_when_not_given(
    missed_work, seeded
) -> None:
    assert compute(missed_work, persist=False)["period_type"] == "daily"

    with get_session(commit=False) as session:
        week = missed_work.compute_missed_work(
            session,
            WINDOW_START,
            WINDOW_START + timedelta(days=7),
            None,
            persist=False,
        )
    assert week["period_type"] == "weekly"


def test_a_stored_document_can_be_read_back(missed_work, seeded) -> None:
    compute(missed_work, period_type="daily")
    stored = missed_work.get_stored_missed_work(WINDOW_START, WINDOW_END, "daily")
    assert stored is not None
    assert stored["totals"]["missed"] == MISSED
    assert missed_work.get_stored_missed_work(WINDOW_START, WINDOW_END, "weekly") is None


# ==========================================================================
# Time
# ==========================================================================


def test_windows_are_local_and_rows_are_utc(missed_work, monkeypatch) -> None:
    """Rows are stored UTC; the window is given in local time and converted.

    America/Detroit is UTC-4 in July, so a row stored at 02:00 UTC belongs to the
    previous local day and must NOT appear in the local 20th.
    """
    monkeypatch.setenv("TZ", "America/Detroit")
    add_requests(
        [
            {
                "request_id": "MW-TZ-1",
                "account_id": "default",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                # 02:00 UTC on the 20th == 22:00 local on the 19th.
                "offered_at": datetime(2026, 7, 20, 2, 0),
                "status": "expired",
                "status_raw": "Expired",
                "service_type_raw": TOW,
                "service_class": "tow",
            },
            {
                "request_id": "MW-TZ-2",
                "account_id": "default",
                "client_name": AGERO,
                "client_key": client_key_for(AGERO),
                # 16:00 UTC == 12:00 local on the 20th.
                "offered_at": datetime(2026, 7, 20, 16, 0),
                "status": "expired",
                "status_raw": "Expired",
                "service_type_raw": TOW,
                "service_class": "tow",
            },
        ]
    )

    document = compute(missed_work, persist=False)
    assert document["totals"]["offers"] == 1
    assert document["window"]["timezone"] == "America/Detroit"
    assert document["window"]["start_utc"] == "2026-07-20T04:00:00"
    # And it is bucketed into the LOCAL hour, which is what a staffing
    # conversation is about.
    assert document["blind_spots"]["by_hour"][12]["offers"] == 1


# ==========================================================================
# Section 8 -- alerts
# ==========================================================================


def test_the_four_alerts_evaluate_through_the_sandbox(missed_work, seeded) -> None:
    """Every missed-work alert goes through classifier.evaluate_alerts and
    core/safe_eval.py, the same as every other rule. No bare eval anywhere."""
    from towbook_agent.agents import classifier, metrics

    rules = get_rules()

    with get_session(commit=False) as session:
        triples = missed_work.alert_contexts(session, WINDOW_END, rules)

    fired: dict[str, list[str]] = {}
    for _kind, entity, context in triples:
        for alert in classifier.evaluate_alerts(context, rules):
            fired.setdefault(alert["id"], []).append(entity)

    # 18:00 and 19:00 both clear "no_response_rate >= 0.35 and offers >= 5".
    assert "blind_spot_forming" in fired
    assert "hour:18" in fired["blind_spot_forming"]

    # tow_unanswered is row-scoped and rides on the context metrics builds.
    with get_session(commit=False) as session:
        views = missed_work._load_views(
            session,
            metrics.to_utc(WINDOW_START),
            metrics.to_utc(WINDOW_END),
            rules,
            None,
        )
    unanswered_tows = 0
    for view in views:
        context = metrics._row_context(view, "daily", "test", rules)
        assert "bucket" in context, "the row context must carry the bucket"
        if any(a["id"] == "tow_unanswered" for a in classifier.evaluate_alerts(context, rules)):
            unanswered_tows += 1
    # The six Agero tows at 18:00. The six Tire Changes are not tows.
    assert unanswered_tows == 6


def test_an_aggregate_context_does_not_masquerade_as_a_row(
    missed_work, seeded
) -> None:
    """The service-type contexts deliberately avoid the name `service_class`, so
    the row-scoped `unclassified_service_type` rule cannot fire a second time
    under a service-type entity."""
    from towbook_agent.agents import classifier

    rules = get_rules()
    with get_session(commit=False) as session:
        triples = missed_work.alert_contexts(session, WINDOW_END, rules)

    for kind, entity, context in triples:
        assert "service_class" not in context, (kind, entity)
        ids = {alert["id"] for alert in classifier.evaluate_alerts(context, rules)}
        assert "unclassified_service_type" not in ids
        assert "missed_tow" not in ids


# ==========================================================================
# Wiring into the reports
# ==========================================================================


def test_the_daily_report_carries_the_inventory(missed_work, seeded) -> None:
    from towbook_agent.agents import metrics

    document = metrics.compute_daily(DAY, emit_alerts=False)
    inventory = document["missed_work"]
    assert inventory is not None
    assert inventory["totals"]["missed"] == MISSED
    assert inventory["ranking_basis"] == "job_count"
    # and it was persisted under its own window key as well.
    assert count_rows(MetricsMissedWork) == 1


def test_the_weekly_report_carries_the_trend(missed_work, seeded) -> None:
    from towbook_agent.agents import metrics

    document = metrics.compute_weekly(DAY, emit_alerts=False)
    assert document["missed_work"]["totals"]["missed"] == MISSED
    trend = document["missed_work_trend"]
    # The prior week is empty, so every cause in this week is new.
    assert trend["by_cause"]["attention"]["direction"] == "new"
    assert trend["totals"]["missed"]["delta"] == MISSED
    assert "Mon 18:00" in trend["blind_spots"]["opened"]


def test_the_embedded_copy_does_not_break_idempotency(missed_work, seeded) -> None:
    """metrics strips volatile keys only at the top level of its own document, so
    a nested computed_at would make two identical runs differ."""
    from towbook_agent.agents import metrics
    from towbook_agent.core.models import MetricsDaily

    metrics.compute_daily(DAY, emit_alerts=False)
    with get_session(commit=False) as session:
        first = session.scalars(select(MetricsDaily)).one().metrics

    metrics.compute_daily(DAY, emit_alerts=False)
    with get_session(commit=False) as session:
        second = session.scalars(select(MetricsDaily)).one().metrics

    assert first == second
    assert "computed_at" not in first["missed_work"]


def test_a_broken_missed_work_degrades_the_report_it_does_not_delete_it(
    missed_work, seeded, monkeypatch
) -> None:
    """The inventory is the newest code in the system. A bug in it must not take
    the acceptance numbers that were working before it existed down with it."""
    from towbook_agent.agents import metrics

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(missed_work, "compute_missed_work", explode)

    document = metrics.compute_daily(DAY, emit_alerts=False)
    assert document["missed_work"] is None
    assert document["totals"]["offered"] == OFFERS
    assert document["totals"]["accepted"] == WON


# ==========================================================================
# The complete real vocabularies
#
# The tests above use a representative subset, chosen so the hand-counted
# arithmetic stays checkable. These two blocks are the exhaustive version:
# every status code and every denial reason the portal is known to emit,
# transcribed from TOWBOOK_PORTAL_FACTS.md sections 5 and 7.
#
# They are worth having separately because the failure they catch is a
# *silent* one. A status code nobody wrote a case for does not crash; it lands
# in `unknown_status` and quietly stops being counted as missed work, and the
# headline number gets smaller for a reason no one can see.
# ==========================================================================

#: All 14 codes, with the statusName the portal pairs with each and the bucket
#: MISSED_WORK_MODEL.md section 1 assigns it. Cross-checked against the live
#: 2-day API archive in raw/2026/07/26/, which contained codes 1, 2, 4, 5, 6,
#: 22, 40, 71, 80 and 90 with exactly these labels.
REAL_STATUS_CODES: tuple[tuple[int, str, str], ...] = (
    (1, "Accepted", "won"),
    (10, "Accept Sent", "won"),
    (6, "Accepting", "in_flight"),
    (7, "Rejecting", "in_flight"),
    (2, "Rejected", "declined"),
    (22, "Reject Failed", "declined"),
    (5, "Expired", "no_response"),
    (80, "Another Provider Responded", "no_response"),
    (21, "Accept Failed", "accept_failed"),
    (4, "Cancelled", "client_withdrew"),
    (40, "Rejected By Motor Club", "client_withdrew"),
    (41, "Service No Longer Needed", "client_withdrew"),
    (71, "Goa Approved By Motor Club", "client_withdrew"),
    (90, "Service Failure Confirmed", "client_withdrew"),
)


@pytest.mark.parametrize(
    ("code", "label", "bucket"),
    REAL_STATUS_CODES,
    ids=[f"{code}-{label}" for code, label, _ in REAL_STATUS_CODES],
)
def test_every_real_status_code_buckets_correctly(
    missed_work, code: int, label: str, bucket: str
) -> None:
    """All 14 codes the portal emits, by numeric code -- the lossless route."""
    assert missed_work.classify_bucket({"status_code": code}) == bucket


@pytest.mark.parametrize(
    ("code", "label", "bucket"),
    REAL_STATUS_CODES,
    ids=[f"{code}-{label}" for code, label, _ in REAL_STATUS_CODES],
)
def test_every_real_status_label_buckets_the_same_way_as_its_code(
    missed_work, code: int, label: str, bucket: str
) -> None:
    """The same 14 statuses by verbatim label -- the CSV route.

    The two maps are maintained by hand in the same YAML block and are trivially
    easy to let drift apart. They must not: whether a job is counted as missed
    work cannot depend on which acquisition path fetched it.
    """
    assert missed_work.classify_bucket({"status_raw": label}) == bucket


def test_the_fourteen_codes_are_the_whole_vocabulary(missed_work) -> None:
    """Both maps cover all 14 and neither has grown a stray entry.

    A code present in one map and absent from the other is the drift the test
    above cannot see, because it only looks at codes this file knows about.
    """
    rules = get_rules()["missed_work"]
    assert {int(key) for key in rules["buckets"]} == {
        code for code, _, _ in REAL_STATUS_CODES
    }

    configured = {label.casefold() for label in rules["bucket_by_status_name"]}
    expected = {label.casefold() for _, label, _ in REAL_STATUS_CODES}
    assert expected <= configured, f"labels with no bucket: {expected - configured}"

    # One deliberate extra. The portal spells it "Cancelled"; the canonical
    # five-value vocabulary spells it "canceled". Carrying both is a spelling
    # alias, not an unmapped fifteenth status -- and both go to the same bucket,
    # which is what makes it safe.
    assert configured - expected == {"canceled"}
    assert rules["bucket_by_status_name"]["canceled"] == (
        rules["bucket_by_status_name"]["cancelled"]
    )


def test_the_two_codes_that_are_lost_work_never_inflate_the_decline_rate(
    missed_work,
) -> None:
    """TOWBOOK_PORTAL_FACTS.md section 5 calls these out by name.

    21 (Accept Failed) and 80 (Another Provider Responded) are work we LOST, not
    work we refused. Folding them into `declined` would tell the owner they are
    turning down jobs they in fact tried to take.
    """
    assert missed_work.classify_bucket({"status_code": 21}) == "accept_failed"
    assert missed_work.classify_bucket({"status_code": 80}) == "no_response"
    assert missed_work.classify_bucket({"status_code": 40}) == "client_withdrew"
    # ...and only the two genuine ones are declines.
    declines = {
        code for code, _, bucket in REAL_STATUS_CODES if bucket == "declined"
    }
    assert declines == {2, 22}


#: All nine real denial-reason strings (TOWBOOK_PORTAL_FACTS.md section 7) plus
#: the blank tenth, with the cause and remedy class MISSED_WORK_MODEL.md
#: section 2 assigns each. Counts are the 30-day totals, for weight only.
REAL_DENIAL_REASONS: tuple[tuple[str, str, str, int], ...] = (
    ("Equipment Not Available", "equipment", "capital", 355),
    ("Equipment Availability", "equipment", "capital", 7),
    ("No Drivers Available", "staffing", "scheduling", 68),
    ("Out of Service Area", "coverage", "territory", 18),
    ("Out Of Coverage Area", "coverage", "territory", 8),
    ("Out of Area", "coverage", "territory", 1),
    ("Not Enough Information", "information", "process", 5),
    ("Other", "review", "review", 27),
    ("Refuse", "review", "review", 23),
    # The tenth: we declined and recorded no reason at all. Its own cause,
    # because "we do not know why we said no" is a fixable process failure and
    # not a category of work.
    ("", "unrecorded", "data", 0),
)


@pytest.mark.parametrize(
    ("reason", "cause", "remedy", "_count"),
    REAL_DENIAL_REASONS,
    ids=[reason or "(blank)" for reason, _, _, _ in REAL_DENIAL_REASONS],
)
def test_every_real_denial_reason_attributes_to_a_cause_and_remedy(
    missed_work, reason: str, cause: str, remedy: str, _count: int
) -> None:
    """Cause says WHY the job was lost; remedy says what kind of fix it is.

    The pair is what turns a count into an action -- `capital` buys a truck,
    `scheduling` moves a shift, `territory` redraws a boundary. Both come from
    rules.yaml, so reassigning one is a YAML edit.
    """
    row = {"status_raw": "Rejected", "denial_reason": reason}
    assert missed_work.attribute_cause(row) == (cause, remedy)


def test_a_null_denial_reason_is_treated_like_a_blank_one(missed_work) -> None:
    """The live API sends `responseReasonName: null`, not "" -- verified in the
    2-day archive, where 344 of 361 records carried null. Both spellings of
    "nothing was recorded" have to reach the same cause."""
    assert missed_work.attribute_cause(
        {"status_raw": "Rejected", "denial_reason": None}
    ) == ("unrecorded", "data")


def test_the_duplicate_reasons_land_on_one_cause(missed_work) -> None:
    """The whole point of normalizing. "Equipment Not Available" (355) and
    "Equipment Availability" (7) are one cause typed two ways; counting them
    apart splits the largest decline reason in the account and buries it."""
    causes = {
        missed_work.attribute_cause(
            {"status_raw": "Rejected", "denial_reason": reason}
        )[0]
        for reason in ("Equipment Not Available", "Equipment Availability")
    }
    assert causes == {"equipment"}

    coverage = {
        missed_work.attribute_cause(
            {"status_raw": "Rejected", "denial_reason": reason}
        )[0]
        for reason in ("Out of Service Area", "Out Of Coverage Area", "Out of Area")
    }
    assert coverage == {"coverage"}


def test_not_enough_information_keeps_its_own_cause(missed_work) -> None:
    """It normalizes into the `other` DENIAL-REASON bucket -- that is the
    verified grouping -- and is still broken back out as its own CAUSE, because
    it is the one decline with a cheap process fix: ask before declining.

    If this ever collapses into `review` the report loses the only recommendation
    it can make that costs nothing.
    """
    from towbook_agent.agents import classifier as classifier_module

    assert classifier_module.normalize_denial_reason("Not Enough Information") == "other"
    assert missed_work.attribute_cause(
        {"status_raw": "Rejected", "denial_reason": "Not Enough Information"}
    ) == ("information", "process")
    assert missed_work.attribute_cause(
        {"status_raw": "Rejected", "denial_reason": "Other"}
    ) == ("review", "review")


def test_an_unseen_denial_reason_surfaces_rather_than_being_guessed(
    missed_work,
) -> None:
    """A new reason must not be quietly absorbed into an existing remedy -- a
    wrong remedy is worse than a visible gap, because somebody acts on it."""
    cause, remedy = missed_work.attribute_cause(
        {"status_raw": "Rejected", "denial_reason": "Truck Caught Fire"}
    )
    assert (cause, remedy) not in {
        ("equipment", "capital"),
        ("staffing", "scheduling"),
        ("coverage", "territory"),
        ("information", "process"),
    }


def test_every_real_reason_carries_the_question_a_report_prints(
    missed_work, seeded
) -> None:
    """Each cause has to arrive with the question it answers attached, or the
    inventory is a list of nouns rather than a list of decisions."""
    rules = get_rules()
    for reason, cause, _, _ in REAL_DENIAL_REASONS:
        assert missed_work._remedy_question(cause, rules), (
            f"{reason or '(blank)'} -> {cause} has no question configured"
        )


def test_no_duplicate_rows_when_the_same_window_is_computed_repeatedly(
    missed_work, seeded
) -> None:
    """Idempotency, stated as the database sees it (hard constraint #4).

    The upsert key is (window_start, window_end, period_type). Three identical
    runs must leave one row whose payload never changed -- not three rows, and
    not one row that drifted.
    """
    documents = [compute(missed_work, period_type="daily") for _ in range(3)]

    assert count_rows(MetricsMissedWork) == 1

    persistable = [missed_work._persistable(document) for document in documents]
    assert persistable[0] == persistable[1] == persistable[2]

    with get_session(commit=False) as session:
        stored = session.scalars(select(MetricsMissedWork)).one().metrics
    assert stored == persistable[0]


# ==========================================================================
# Section 7 -- coverage, the dimension the outcome actually splits on
#
# Hand-counted from PLAN. 2026-07-20 is a Monday and the suite runs in UTC, so
# the local hours in PLAN are the stored hours, and the shipped window
# (Mon-Fri 06:00-18:00) cuts the day cleanly in two:
#
#   COVERED    hour 09 only           Agero 5 + Allstate 7            = 12
#       won 3+4 = 7 | declined 1 | no_response 0 | accept_failed 1
#       client_withdrew 1+2 = 3 | missed 5 | recoverable 2
#
#   UNCOVERED  hours 18 and 19        Agero 8 + Agero 6 + Allstate 4  = 18
#       won 1+3 = 4 | declined 1 | no_response 6+6 = 12 | in_flight 1
#       missed 13 | recoverable 13 | answered 18 - 12 - 1 = 5
#
# 12 + 18 = 30 offers, 5 + 13 = 18 missed, 2 + 13 = 15 recoverable -- the same
# totals the rest of this file counts, re-partitioned on the one dimension the
# outcome actually splits on.
# ==========================================================================

COVERED_OFFERS = 12
UNCOVERED_OFFERS = 18
COVERED_NO_RESPONSE = 0
UNCOVERED_NO_RESPONSE = 12
COVERED_RECOVERABLE = 2
UNCOVERED_RECOVERABLE = 13


def coverage_of(document: dict[str, Any]) -> dict[str, Any]:
    return document["coverage"]["by_label"]


def test_the_window_boundary_is_half_open_and_local(missed_work) -> None:
    """06:00 is inside, 18:00 is not, and Saturday is not.

    Half-open matters: two shifts meeting at 18:00 must neither overlap nor
    leave an hour in which an offer belongs to nobody.
    """
    label = missed_work.coverage_label

    assert label({"offered_at_local": "2026-07-20T06:00"}) == "covered"
    assert label({"offered_at_local": "2026-07-20T05:59"}) == "uncovered"
    assert label({"offered_at_local": "2026-07-20T17:59"}) == "covered"
    assert label({"offered_at_local": "2026-07-20T18:00"}) == "uncovered"
    # 2026-07-25 is a Saturday: inside the clock hours, outside the roster.
    assert label({"offered_at_local": "2026-07-25T10:00"}) == "uncovered"


def test_a_row_with_no_readable_time_is_never_claimed_as_covered(
    missed_work,
) -> None:
    """An offer whose arrival time cannot be read is certainly not one we can
    claim somebody was rostered for."""
    assert missed_work.coverage_label({"request_id": "X"}) == "uncovered"


def test_every_row_view_carries_its_coverage_label(missed_work, seeded) -> None:
    document = compute(missed_work, persist=False)
    labels = {entry["label"] for entry in document["coverage"]["windows"]}
    assert labels == {"covered", "uncovered"}
    # The partition is total: every offer lands in exactly one window.
    assert sum(entry["offers"] for entry in document["coverage"]["windows"]) == OFFERS


def test_coverage_splits_the_window_by_hand_counted_numbers(
    missed_work, seeded
) -> None:
    by_label = coverage_of(compute(missed_work, persist=False))

    covered = by_label["covered"]
    assert covered["offers"] == COVERED_OFFERS
    assert covered["accepted"] == 7
    assert covered["declined"] == 1
    assert covered["no_response"] == COVERED_NO_RESPONSE
    assert covered["no_response_rate"] == 0.0
    assert covered["recoverable"] == COVERED_RECOVERABLE

    uncovered = by_label["uncovered"]
    assert uncovered["offers"] == UNCOVERED_OFFERS
    assert uncovered["accepted"] == 4
    assert uncovered["declined"] == 1
    assert uncovered["no_response"] == UNCOVERED_NO_RESPONSE
    assert uncovered["no_response_rate"] == pytest.approx(12 / 18)
    assert uncovered["recoverable"] == UNCOVERED_RECOVERABLE
    # Answered excludes the offer still in flight -- nobody has answered it yet.
    assert uncovered["answered"] == 5


def test_the_headline_is_the_uncovered_figure_and_the_covered_one_sits_beside_it(
    missed_work, seeded
) -> None:
    """The contrast IS the business case.

    Either number alone is a weaker and different claim, so the document must
    never carry one without the other.
    """
    document = compute(missed_work, persist=False)
    coverage = document["coverage"]

    assert coverage["headline"]["label"] == "uncovered"
    assert coverage["headline"]["lost"] == UNCOVERED_NO_RESPONSE
    assert coverage["headline"]["recoverable"] == UNCOVERED_RECOVERABLE

    assert coverage["inside"]["no_response"] == COVERED_NO_RESPONSE
    assert coverage["outside"]["no_response"] == UNCOVERED_NO_RESPONSE

    totals = document["totals"]
    assert totals["recoverable_uncovered"] == UNCOVERED_RECOVERABLE
    assert totals["recoverable_covered"] == COVERED_RECOVERABLE
    assert (
        totals["recoverable_uncovered"] + totals["recoverable_covered"]
        == RECOVERABLE_DEFAULT
    )


def test_missed_value_is_reported_per_window(
    missed_work, seeded, write_config
) -> None:
    """Each window carries what its missed work was worth.

    A price for Agero and none for Allstate, exactly as elsewhere in this file:
    an unpriced client must contribute nothing rather than borrow a number, and
    only should_accept classes are priced -- so the six Tire Change expiries in
    the uncovered window are counted as unpriced instead of being handed a tow's
    value.
    """

    def price_one_client(data: dict[str, Any]) -> None:
        data["missed_work"]["job_value_by_client"] = {client_key_for(AGERO): 100}

    rules = rewrite_rules(write_config, price_one_client)
    with get_session() as session:
        document = missed_work.compute_missed_work(
            session, WINDOW_START, WINDOW_END, rules, persist=False
        )
    by_label = document["coverage"]["by_label"]

    # Uncovered: Agero's 6 unanswered tows + 1 staffing decline are priced; the
    # 6 Tire Changes are light_service and are not.
    uncovered = by_label["uncovered"]
    assert uncovered["value_available"] is True
    assert uncovered["priced_missed"] == 7
    assert uncovered["unpriced_missed"] == 6
    assert uncovered["missed_value"] == pytest.approx(700.0)
    assert uncovered["recoverable_value"] == pytest.approx(700.0)

    # Covered: Agero's 1 equipment decline + 1 cancellation. Allstate's three
    # missed rows have no configured value and contribute nothing.
    covered = by_label["covered"]
    assert covered["priced_missed"] == 2
    assert covered["unpriced_missed"] == 3
    assert covered["missed_value"] == pytest.approx(200.0)

    # The whole point, in one line: the money sits outside the staffed window.
    assert uncovered["missed_value"] > covered["missed_value"]


def test_adding_a_saturday_shift_is_a_yaml_edit(missed_work, write_config) -> None:
    """Multiple windows are supported, and re-cutting them re-cuts the answer
    with no code change -- the same property the buckets have."""
    add_requests(build_rows())

    before = coverage_of(compute(missed_work, persist=False))
    assert before["uncovered"]["offers"] == UNCOVERED_OFFERS

    def widen(data: dict[str, Any]) -> None:
        data["missed_work"]["coverage"]["windows"] = [
            {
                "name": "covered",
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "06:00",
                "end": "18:00",
            },
            {"name": "evening", "days": ["mon"], "start": "18:00", "end": "20:00"},
        ]

    rules = rewrite_rules(write_config, widen)
    with get_session() as session:
        document = missed_work.compute_missed_work(
            session, WINDOW_START, WINDOW_END, rules, persist=False
        )

    by_label = document["coverage"]["by_label"]
    # Hours 18 and 19 move out of `uncovered` and into the new shift.
    assert by_label["evening"]["offers"] == UNCOVERED_OFFERS
    assert by_label["uncovered"]["offers"] == 0
    assert by_label["covered"]["offers"] == COVERED_OFFERS


def test_an_overnight_shift_wraps_past_midnight(missed_work, write_config) -> None:
    """An `end` earlier than `start` is a night shift, not a broken config."""

    def overnight(data: dict[str, Any]) -> None:
        data["missed_work"]["coverage"]["windows"] = [
            {"name": "nights", "days": ["sun"], "start": "22:00", "end": "06:00"}
        ]

    rules = rewrite_rules(write_config, overnight)
    label = missed_work.coverage_label

    # 2026-07-19 is a Sunday; the shift runs into Monday morning.
    assert label({"offered_at_local": "2026-07-19T23:00"}, rules) == "nights"
    assert label({"offered_at_local": "2026-07-20T05:00"}, rules) == "nights"
    assert label({"offered_at_local": "2026-07-20T07:00"}, rules) == "uncovered"


def test_the_coverage_gap_alert_parses_and_fires_on_exactly_its_row(
    missed_work, seeded
) -> None:
    """The shipped rule has to be evaluable by the sandbox, and has to fire on
    exactly the row it describes -- an unanswered tow outside the roster."""
    from towbook_agent.core.safe_eval import safe_eval_bool, validate_expression

    rule = next(
        entry for entry in get_rules()["alerts"] if entry["id"] == "coverage_gap"
    )
    validate_expression(rule["when"])
    assert rule["severity"] == "high"

    fires = {
        "coverage_label": "uncovered",
        "service_class": "tow",
        "bucket": "no_response",
    }
    assert safe_eval_bool(rule["when"], fires) is True
    assert safe_eval_bool(rule["when"], {**fires, "coverage_label": "covered"}) is False
    assert safe_eval_bool(rule["when"], {**fires, "bucket": "declined"}) is False
    assert (
        safe_eval_bool(rule["when"], {**fires, "service_class": "light_service"})
        is False
    )


def test_the_row_alert_context_carries_the_coverage_label(missed_work, seeded) -> None:
    """`coverage_gap` is row-scoped, so metrics' per-row context must see it."""
    from towbook_agent.agents import metrics

    rules = get_rules()
    _, default_class = metrics._service_class_order(rules)
    with get_session(commit=False) as session:
        rows = metrics._load_rows(session, WINDOW_START, WINDOW_END, None)
        views = metrics._views(rows, rules, default_class)

    contexts = [metrics._row_context(view, "daily", "test", rules) for view in views]
    assert all("coverage_label" in context for context in contexts)
    assert {context["coverage_label"] for context in contexts} == {
        "covered",
        "uncovered",
    }


# ==========================================================================
# Territory -- "was this even our job to take?"
# ==========================================================================


def test_a_zip_in_no_band_is_outside_the_service_area(missed_work) -> None:
    """Chillicothe: 26 offers and no jobs in 30 days, and the clubs know it."""
    assert missed_work.territory_band({"pickup_zip": "45601"}) == "outside"
    assert missed_work.in_territory({"pickup_zip": "45601"}) is False


def test_the_bands_are_reported_apart(missed_work) -> None:
    """core 39.6% accepted against edge 9.1% is a real operating fact.

    Flattening the two into one "in territory" answer would hide that the long
    runs are barely worked.
    """
    assert missed_work.territory_band({"pickup_zip": "43201"}) == "core"
    assert missed_work.territory_band({"pickup_zip": "43040"}) == "edge"
    assert missed_work.territory_band({"pickup_zip": "43025"}) == "ext"
    for zip_code in ("43201", "43040", "43025"):
        assert missed_work.in_territory({"pickup_zip": zip_code}) is True


def test_an_unplaceable_offer_counts_as_ours(missed_work) -> None:
    """THE DIRECTION THAT MATTERS.

    A row we cannot place must not be called out-of-territory, because that
    would quietly delete it from the missed-work inventory. Hiding work is the
    one failure this model must not have, so unknown is counted as ours and
    reported as unknown.
    """
    assert missed_work.territory_band({"pickup_zip": None}) == "territory_unknown"
    assert missed_work.territory_band({"pickup_zip": "not a zip"}) == "territory_unknown"
    assert missed_work.in_territory({"pickup_zip": None}) is True


def test_the_zip_is_read_from_the_address_when_the_row_has_no_zip_field(
    missed_work,
) -> None:
    """CSV-ingested rows carry no ZIP field at all -- only the address text."""
    row = {"pickup_location": "4650 Kenny Rd, Columbus, OH 43220"}
    assert missed_work.territory_band(row) == "core"
    # ZIP+4 and a trailing country have to land on the same key.
    assert missed_work.territory_band(
        {"pickup_location": "1 Main St, Columbus, OH 43220-1234, USA"}
    ) == "core"


def test_a_zip_the_config_wrote_as_a_number_still_matches(
    missed_work, write_config
) -> None:
    """YAML turns an unquoted 43201 into an int, and 01234 into 1234.

    The loader has to normalise either way or a New England tenant silently has
    no territory at all.
    """

    def renumber(data: dict[str, Any]) -> None:
        data["missed_work"]["territory"]["bands"] = [
            {"name": "core", "zips": [43201, 1234]}
        ]

    rules = rewrite_rules(write_config, renumber)
    assert missed_work.territory_band({"pickup_zip": "43201"}, rules) == "core"
    assert missed_work.territory_band({"pickup_zip": "01234"}, rules) == "core"


def test_redrawing_the_service_area_is_a_yaml_edit(missed_work, write_config) -> None:
    """Same property coverage and the buckets have: config, not code."""
    assert missed_work.territory_band({"pickup_zip": "45601"}) == "outside"

    def widen(data: dict[str, Any]) -> None:
        data["missed_work"]["territory"]["bands"].append(
            {"name": "expansion", "zips": ["45601"]}
        )

    rules = rewrite_rules(write_config, widen)
    assert missed_work.territory_band({"pickup_zip": "45601"}, rules) == "expansion"


def test_a_zip_in_two_bands_belongs_to_the_nearer_one(
    missed_work, write_config
) -> None:
    """Bands are nearest-first, so config order is the tie-break."""

    def overlap(data: dict[str, Any]) -> None:
        data["missed_work"]["territory"]["bands"] = [
            {"name": "core", "zips": ["43201"]},
            {"name": "edge", "zips": ["43201", "43040"]},
        ]

    rules = rewrite_rules(write_config, overlap)
    assert missed_work.territory_band({"pickup_zip": "43201"}, rules) == "core"
    assert missed_work.territory_band({"pickup_zip": "43040"}, rules) == "edge"


def test_no_territory_configured_means_every_offer_is_ours(
    missed_work, write_config
) -> None:
    """The behaviour every install had before the block existed.

    Deleting the block must not silently reclassify a whole roster as
    out-of-area.
    """

    def strip(data: dict[str, Any]) -> None:
        data["missed_work"].pop("territory", None)

    rules = rewrite_rules(write_config, strip)
    assert missed_work.territory_band({"pickup_zip": "45601"}, rules) == "territory_unknown"
    assert missed_work.in_territory({"pickup_zip": "45601"}, rules) is True


# ==========================================================================
# Section 10 -- the job list
#
# Every other section of the document is an aggregate. This one is the lookup
# table: one row per job we did not get, each carrying the reference that finds
# it in Towbook. The thing worth pinning is not the formatting, it is that a
# row is NEVER left without a reference -- because on this list the Towbook job
# number is usually absent, and a blank cell here means the report cannot be
# acted on.
# ==========================================================================


def test_the_job_list_holds_every_job_we_did_not_get(missed_work, seeded) -> None:
    document = compute(missed_work, persist=False)

    rows = document["missed_jobs"]
    meta = document["missed_jobs_meta"]
    assert len(rows) == MISSED
    assert meta["missed_in_window"] == MISSED
    assert meta["shown"] == MISSED
    assert meta["truncated"] is False
    # Won and in-flight offers are not missed work and are not in the list.
    assert all(row["bucket"] not in {"won", "in_flight"} for row in rows)


def test_every_job_carries_a_reference_even_with_no_job_number(
    missed_work, seeded
) -> None:
    """The point of the whole feature.

    Towbook does not issue a job number until an offer becomes a job, so on
    this list -- offers we did NOT take -- there is usually none. A row with no
    reference at all is a row the owner cannot look up, which would make the
    list decorative.
    """
    document = compute(missed_work, persist=False)

    for row in document["missed_jobs"]:
        assert row["towbook_ref"], f"{row['request_id']} has no reference at all"
        assert row["towbook_ref_kind"] in {"job", "request"}
        # These rows were inserted with no job_number, which is exactly the
        # production shape for unaccepted work.
        assert row["job_number"] is None
        assert row["towbook_ref_kind"] == "request"
        assert row["towbook_ref"] == row["request_id"]
        assert row["towbook_ref_label"] == f"Req {row['request_id']}"

    assert document["missed_jobs_meta"]["with_job_number"] == 0


def test_a_job_number_is_preferred_over_the_request_id(missed_work) -> None:
    """When Towbook did issue one -- accepted first, lost afterwards -- it wins."""
    add_requests(
        [
            {
                "request_id": "MW-JOB-1",
                "account_id": "default",
                "client_name": "Agero",
                "client_key": client_key_for("Agero"),
                "offered_at": datetime(2026, 7, 20, 9, 0),
                "job_number": "125169",
                "status": "canceled",
                "status_raw": "Goa Approved By Motor Club",
                "status_code": 71,
                "service_type_raw": TOW,
                "service_class": "tow",
            }
        ]
    )

    document = compute(missed_work, persist=False)
    row = next(r for r in document["missed_jobs"] if r["request_id"] == "MW-JOB-1")

    assert row["job_number"] == "125169"
    assert row["towbook_ref"] == "125169"
    assert row["towbook_ref_kind"] == "job"
    assert row["towbook_ref_label"] == "Job #125169"
    assert document["missed_jobs_meta"]["with_job_number"] == 1


def test_the_job_list_is_capped_and_says_so(missed_work, seeded, write_config) -> None:
    """A month misses more jobs than any email should carry.

    Truncation is fine; silent truncation is not -- a list that stops at the
    cap without saying so reads as "that was all of them".
    """
    rewrite_rules(
        write_config,
        lambda data: data["missed_work"].__setitem__("job_list", {"max_rows": 5}),
    )

    document = compute(missed_work, persist=False)
    meta = document["missed_jobs_meta"]

    assert len(document["missed_jobs"]) == 5
    assert meta["shown"] == 5
    assert meta["missed_in_window"] == MISSED
    assert meta["truncated"] is True


def test_the_job_list_is_chronological_and_stable(missed_work, seeded) -> None:
    """Same window, same list, same order -- so the stored blob is comparable."""
    first = compute(missed_work, persist=False)["missed_jobs"]
    second = compute(missed_work, persist=False)["missed_jobs"]

    assert [row["request_id"] for row in first] == [row["request_id"] for row in second]
    offered = [row["offered_at_local"] for row in first]
    assert offered == sorted(offered)
