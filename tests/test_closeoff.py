"""Close-off candidates -- work we do not want (MISSED_WORK_MODEL.md section 5).

The mirror image of the recoverable inventory. Some service types are refused
almost every time they are offered, and they are not free: they consume
attention inside the same **3-minute** decision window the tow offers are
competing for. On 30 days of the owner's real traffic this surfaces Tire Change
(145 offers, 0 accepted), Battery Jump (77, 3), Lock Out (61, 4), Light Tire
Change (59, 1) and Fuel Delivery (18, 0) -- roughly 450 offers producing 13
jobs.

The output is grouped **by client**, because the action is not a setting to
change, it is a conversation to have: "stop sending us these". Each row carries
the share of that client's total offers it represents so the ask can be sized.

The fixture
-----------
:data:`PLAN` is one day, hand-built so each threshold has a case sitting exactly
on it and one clearly on either side:

==============  ======  ========  =====  =====================================
service type    offers  accepted  rate   why it is here
==============  ======  ========  =====  =====================================
Tire Change         20         0  0.00   THE CANDIDATE, split across 2 clients
Lock Out            20         2  0.10   exactly ON max_rate
Battery Jump        20        15  0.75   plenty of offers, we want this one
Fuel Delivery        3         0  0.00   worst rate, too few offers
Tow                 20         0  0.00   would qualify -- but we WANT tows
==============  ======  ========  =====  =====================================

Shipped thresholds are ``min_offers: 5`` and ``max_rate: 0.10``, and the rate
comparison is ``<=``, so Lock Out must be flagged.

The Tow row is the one that matters most. Without
``exclude_wanted_classes`` a bad week would produce "ask Agero to stop sending
you tows", which is the exact inverse of the point of the entire report.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import yaml

from towbook_agent.core.config_loader import CONFIG, get_rules
from towbook_agent.core.db import get_session
from towbook_agent.core.models import Request, client_key_for

DAY = datetime(2026, 7, 20, 0, 0)
WINDOW_START = DAY
WINDOW_END = DAY + timedelta(days=1)

AGERO = "Agero"
NSD = "NSD"
ALLSTATE = "Allstate"

TIRE = "Tire Change"
LOCKOUT = "Lock Out"
BATTERY = "Battery Jump"
FUEL = "Fuel Delivery"
TOW = "Tow"

LIGHT = "light_service"

#: (client, service_type_raw, service_class, accepted, no_response, declined)
#:
#: service_class is set explicitly rather than left to the classifier so that a
#: rules.yaml edit cannot quietly change what this file is testing. The values
#: are the ones the shipped rules really produce for these strings -- see
#: tests/test_rules_real_data.py, which owns that half of the contract.
PLAN: tuple[tuple[str, str, str, int, int, int], ...] = (
    (AGERO, TIRE, LIGHT, 0, 14, 0),      # 14 offers, none taken
    (NSD, TIRE, LIGHT, 0, 6, 0),         #  6 offers, none taken -> 20 total
    (ALLSTATE, LOCKOUT, LIGHT, 2, 18, 0),  # 20 offers, 2 taken == 10% exactly
    (AGERO, BATTERY, LIGHT, 15, 0, 5),   # 20 offers, 15 taken -- keep sending
    (AGERO, FUEL, LIGHT, 0, 3, 0),       #  3 offers -- under min_offers
    (AGERO, TOW, "tow", 0, 20, 0),       # 20 offers, none taken -- but WANTED
)

#: Counted from the table.
TIRE_OFFERS, TIRE_ACCEPTED = 20, 0
LOCKOUT_OFFERS, LOCKOUT_ACCEPTED = 20, 2
AGERO_OFFERS = 14 + 20 + 3 + 20      # tire + battery + fuel + tow  = 57
NSD_OFFERS = 6
ALLSTATE_OFFERS = 20
TOW_OFFERS = 20

EXPECTED_CANDIDATES = {TIRE, LOCKOUT}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def missed_work():
    from conftest import load_agent

    return load_agent("missed_work")


def build_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    counter = 0
    hour = 8
    for client, service, service_class, accepted, unanswered, declined in PLAN:
        labels = (
            [("Accepted", "accepted", None)] * accepted
            + [("Expired", "expired", None)] * unanswered
            + [("Rejected", "denied", "Equipment Not Available")] * declined
        )
        for index, (label, canonical, reason) in enumerate(labels):
            counter += 1
            rows.append(
                {
                    "request_id": f"CO-{counter:05d}",
                    "account_id": "default",
                    "client_name": client,
                    "client_key": client_key_for(client),
                    "offered_at": DAY.replace(hour=hour) + timedelta(minutes=index),
                    "status": canonical,
                    "status_raw": label,
                    "denial_reason": reason,
                    "service_type_raw": service,
                    "service_class": service_class,
                }
            )
        hour += 1
    return rows


@pytest.fixture
def seeded() -> list[dict[str, Any]]:
    rows = build_rows()
    with get_session() as session:
        for row in rows:
            session.add(Request(**row))
    return rows


def closeoff(missed_work) -> dict[str, Any]:
    with get_session(commit=False) as session:
        return missed_work.closeoff_candidates(session, WINDOW_START, WINDOW_END, None)


def by_type(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["service_type_raw"]: entry for entry in document["by_service_type"]}


def by_client(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["client_key"]: entry for entry in document["clients"]}


def rewrite_rules(write_config, mutate) -> dict[str, Any]:
    data = yaml.safe_load((CONFIG.config_dir / "rules.yaml").read_text(encoding="utf-8"))
    mutate(data)
    write_config("rules.yaml", data)
    return get_rules()


def set_closeoff(write_config, **values: Any) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["missed_work"]["closeoff"].update(values)

    rewrite_rules(write_config, mutate)


# ==========================================================================
# Which service types qualify
# ==========================================================================


def test_the_shipped_thresholds_are_the_documented_ones(missed_work, seeded) -> None:
    assert closeoff(missed_work)["thresholds"] == {
        "min_offers": 5,
        "max_rate": 0.10,
        "exclude_wanted_classes": True,
    }


def test_a_service_type_at_zero_of_twenty_is_a_candidate(missed_work, seeded) -> None:
    """Tire Change: offered twenty times, taken none. The clearest possible case."""
    entry = by_type(closeoff(missed_work))[TIRE]
    assert entry["offers"] == TIRE_OFFERS
    assert entry["accepted"] == TIRE_ACCEPTED
    assert entry["rate"] == 0.0, "zero percent, not None -- twenty offers were made"
    assert entry["service_class"] == LIGHT


def test_a_service_type_at_fifteen_of_twenty_is_not_a_candidate(
    missed_work, seeded
) -> None:
    """Battery Jump at 75%. It is light service, it is outside the acceptance
    policy, and it is still emphatically not something to close off -- the test
    is behaviour, not category."""
    document = closeoff(missed_work)
    assert BATTERY not in by_type(document)
    for client in document["clients"]:
        assert BATTERY not in {row["service_type_raw"] for row in client["service_types"]}


def test_a_service_type_at_zero_of_three_is_not_a_candidate(missed_work, seeded) -> None:
    """Fuel Delivery ties Tire Change on rate and loses on evidence.

    min_offers is what stops a report recommending a client conversation on the
    strength of three offers.
    """
    assert FUEL not in by_type(closeoff(missed_work))


def test_a_rate_exactly_on_max_rate_is_a_candidate(missed_work, seeded) -> None:
    """Lock Out: 2 of 20 is 0.10 exactly, and the comparison is <=.

    An off-by-one here drops the marginal service types, which are the ones
    where the conversation is actually worth having.
    """
    entry = by_type(closeoff(missed_work))[LOCKOUT]
    assert entry["offers"] == LOCKOUT_OFFERS
    assert entry["accepted"] == LOCKOUT_ACCEPTED
    assert entry["rate"] == pytest.approx(0.10)


def test_exactly_the_expected_service_types_qualify(missed_work, seeded) -> None:
    document = closeoff(missed_work)
    assert set(by_type(document)) == EXPECTED_CANDIDATES
    assert document["totals"]["service_types"] == 2
    assert document["totals"]["offers"] == TIRE_OFFERS + LOCKOUT_OFFERS
    assert document["totals"]["accepted"] == TIRE_ACCEPTED + LOCKOUT_ACCEPTED
    assert document["totals"]["rate"] == pytest.approx(2 / 40)


def test_service_types_are_ranked_by_volume(missed_work, seeded) -> None:
    """By job count, descending, with an alphabetical tiebreak so a recompute
    produces a byte-identical list."""
    document = closeoff(missed_work)
    offers = [entry["offers"] for entry in document["by_service_type"]]
    assert offers == sorted(offers, reverse=True)
    # Both are 20, so the tiebreak decides: "Lock Out" before "Tire Change".
    assert [e["service_type_raw"] for e in document["by_service_type"]] == [
        LOCKOUT,
        TIRE,
    ]


# ==========================================================================
# Work we WANT is never a candidate
# ==========================================================================


def test_a_tow_at_zero_of_twenty_is_never_a_candidate(missed_work, seeded) -> None:
    """The single most important assertion in this file.

    Twenty tows offered and none taken is the worst number in the fixture. It
    belongs in the recoverable inventory as a staffing failure -- NOT in a
    report advising the owner to ask a client to stop sending tows.
    """
    document = closeoff(missed_work)
    assert TOW not in by_type(document)
    for client in document["clients"]:
        assert TOW not in {row["service_type_raw"] for row in client["service_types"]}
    assert document["excluded_service_classes"] == ["tow", "winch_out"]


def test_turning_the_exclusion_off_would_recommend_closing_off_tows(
    missed_work, seeded, write_config
) -> None:
    """Proof the filter is what prevents it, and that it is data.

    Reproduces the inverted report deliberately, so ``exclude_wanted_classes``
    is protected by something that fails rather than by a comment.
    """
    assert TOW not in by_type(closeoff(missed_work))

    set_closeoff(write_config, exclude_wanted_classes=False)

    document = closeoff(missed_work)
    assert TOW in by_type(document), "without the filter a bad week closes off tows"
    assert by_type(document)[TOW]["offers"] == TOW_OFFERS
    assert document["excluded_service_classes"] == []


# ==========================================================================
# Grouped by client -- because the action is a conversation
# ==========================================================================


def test_results_are_grouped_by_client(missed_work, seeded) -> None:
    """Three clients are sending candidate work; each gets its own row."""
    document = closeoff(missed_work)
    clients = by_client(document)
    assert set(clients) == {
        client_key_for(AGERO),
        client_key_for(NSD),
        client_key_for(ALLSTATE),
    }
    assert document["totals"]["clients"] == 3
    for entry in document["clients"]:
        assert entry["client"] in {AGERO, NSD, ALLSTATE}


def test_one_service_type_split_across_two_clients_is_two_conversations(
    missed_work, seeded
) -> None:
    """Tire Change is a single 0-of-20 decision, but Agero sends 14 of them and
    NSD 6 -- so it is one finding and two asks, sized differently."""
    clients = by_client(closeoff(missed_work))

    agero = clients[client_key_for(AGERO)]
    nsd = clients[client_key_for(NSD)]

    agero_tire = next(r for r in agero["service_types"] if r["service_type_raw"] == TIRE)
    nsd_tire = next(r for r in nsd["service_types"] if r["service_type_raw"] == TIRE)

    assert agero_tire["offers"] == 14
    assert nsd_tire["offers"] == 6
    assert agero_tire["offers"] + nsd_tire["offers"] == TIRE_OFFERS


def test_each_row_carries_the_share_that_sizes_the_ask(missed_work, seeded) -> None:
    """"14 of your 57 offers" is a conversation. "14" on its own is not."""
    clients = by_client(closeoff(missed_work))

    agero = clients[client_key_for(AGERO)]
    assert agero["client_offers"] == AGERO_OFFERS
    assert agero["candidate_offers"] == 14
    assert agero["candidate_share_of_client_offers"] == pytest.approx(14 / AGERO_OFFERS)
    agero_tire = next(r for r in agero["service_types"] if r["service_type_raw"] == TIRE)
    assert agero_tire["share_of_client_offers"] == pytest.approx(14 / AGERO_OFFERS)

    # NSD sends nothing but Tire Change, so the whole relationship is the ask.
    nsd = clients[client_key_for(NSD)]
    assert nsd["client_offers"] == NSD_OFFERS
    assert nsd["candidate_share_of_client_offers"] == 1.0


def test_the_client_denominator_counts_every_offer_not_just_candidates(
    missed_work, seeded
) -> None:
    """Agero's 57 includes its tows. Sizing the ask against candidates only would
    make every client look like they send nothing but junk."""
    agero = by_client(closeoff(missed_work))[client_key_for(AGERO)]
    assert agero["client_offers"] == AGERO_OFFERS > agero["candidate_offers"]


def test_clients_are_ranked_by_how_much_candidate_work_they_send(
    missed_work, seeded
) -> None:
    document = closeoff(missed_work)
    order = [(e["client_key"], e["candidate_offers"]) for e in document["clients"]]
    assert order == [
        (client_key_for(ALLSTATE), 20),
        (client_key_for(AGERO), 14),
        (client_key_for(NSD), 6),
    ]


# ==========================================================================
# The thresholds are data
# ==========================================================================


def test_lowering_min_offers_admits_the_three_offer_type(
    missed_work, seeded, write_config
) -> None:
    assert FUEL not in by_type(closeoff(missed_work))
    set_closeoff(write_config, min_offers=3)
    assert FUEL in by_type(closeoff(missed_work))


def test_raising_max_rate_admits_the_one_we_actually_take(
    missed_work, seeded, write_config
) -> None:
    assert BATTERY not in by_type(closeoff(missed_work))
    set_closeoff(write_config, max_rate=0.80)
    assert BATTERY in by_type(closeoff(missed_work))


def test_tightening_max_rate_drops_the_boundary_type(
    missed_work, seeded, write_config
) -> None:
    """Lock Out sits exactly on 0.10; at 0.09 it must fall out while the 0.00
    type stays, so this is a threshold move and not the detector failing."""
    set_closeoff(write_config, max_rate=0.09)
    types = by_type(closeoff(missed_work))
    assert LOCKOUT not in types
    assert TIRE in types


# ==========================================================================
# The before/after claim
# ==========================================================================


def test_the_baseline_it_expects_to_improve_is_recorded(missed_work, seeded) -> None:
    """Closing these off is claimed to IMPROVE the response rate on work we want,
    by removing competing offers from the same 3-minute window. A claim nobody
    can check later is a slogan, so the report records the rate at the moment
    the recommendation was made."""
    document = closeoff(missed_work)
    baseline = document["wanted_baseline"]
    assert baseline["service_classes"] == ["tow", "winch_out"]
    assert baseline["offers"] == TOW_OFFERS
    assert baseline["no_response"] == 20
    assert baseline["no_response_rate"] == 1.0
    assert "3-minute" in document["rationale"]


def test_nothing_here_implies_money(missed_work, seeded) -> None:
    """offerAmount is empty on 100% of real records. This report ranks by job
    count and says so."""
    document = closeoff(missed_work)
    assert document["ranking_basis"] == "job_count"
    for entry in document["by_service_type"]:
        assert "estimated_value" not in entry
        assert "amount" not in entry


# ==========================================================================
# Edges
# ==========================================================================


def test_an_empty_window_produces_no_candidates_rather_than_a_crash(
    missed_work,
) -> None:
    """Divide-by-zero yields None, never 0, and never an exception."""
    document = closeoff(missed_work)
    assert document["by_service_type"] == []
    assert document["clients"] == []
    assert document["totals"]["service_types"] == 0
    assert document["totals"]["rate"] is None
    assert document["wanted_baseline"]["no_response_rate"] is None


def test_a_row_with_no_service_type_is_skipped_not_grouped_under_blank(
    missed_work, seeded
) -> None:
    """An empty service_type_raw must not become a candidate called "" that the
    owner is then advised to ask a client to stop sending."""
    with get_session() as session:
        for index in range(10):
            session.add(
                Request(
                    request_id=f"CO-BLANK-{index}",
                    account_id="default",
                    client_name=AGERO,
                    client_key=client_key_for(AGERO),
                    offered_at=DAY.replace(hour=20) + timedelta(minutes=index),
                    status="expired",
                    status_raw="Expired",
                    service_type_raw="",
                    service_class=LIGHT,
                )
            )
    assert set(by_type(closeoff(missed_work))) == EXPECTED_CANDIDATES


def test_offers_outside_the_window_are_not_counted(missed_work, seeded) -> None:
    with get_session() as session:
        for index in range(10):
            session.add(
                Request(
                    request_id=f"CO-NEXTDAY-{index}",
                    account_id="default",
                    client_name=AGERO,
                    client_key=client_key_for(AGERO),
                    offered_at=WINDOW_END + timedelta(minutes=index),
                    status="expired",
                    status_raw="Expired",
                    service_type_raw=TIRE,
                    service_class=LIGHT,
                )
            )
    assert by_type(closeoff(missed_work))[TIRE]["offers"] == TIRE_OFFERS


def test_the_document_is_identical_when_recomputed(missed_work, seeded) -> None:
    """Determinism -- what makes the stored missed-work blob comparable."""
    assert closeoff(missed_work) == closeoff(missed_work)


def test_it_matches_the_copy_embedded_in_the_document(missed_work, seeded) -> None:
    """The dashboard reads one and the daily report the other; they must agree."""
    standalone = closeoff(missed_work)
    with get_session(commit=False) as session:
        embedded = missed_work.compute_missed_work(
            session, WINDOW_START, WINDOW_END, None, persist=False
        )["closeoff_candidates"]

    assert set(standalone) - set(embedded) == {"window"}
    assert {k: v for k, v in standalone.items() if k != "window"} == embedded
