"""The Analyst -- the only LLM in the system, and the only place it can lie.

Everything else here is deterministic: the same rows always produce the same
numbers. The Analyst adds prose and suggestions on top of numbers that were
already final before it ran, which means the failure modes are different in kind
from the rest of the system. A metrics bug produces a wrong number that somebody
eventually notices. An Analyst failure produces a *plausible* number that nobody
can check, in a report the owner is about to act on.

So these tests are almost entirely about what the Analyst is NOT allowed to do:

* it must not send row-level data to a third party;
* it must not make a claim without the number behind it;
* it must not mention money, because ``offerAmount`` is empty on 100% of this
  account's records and any dollar figure can therefore only be invented;
* it must not write ``config/rules.yaml``, ever, by any path;
* and it must not be able to stop a report going out by failing.

No test here makes a network call. ``no_network`` in conftest makes a real
connection raise, and the model call itself is replaced with a canned reply, so
what is under test is the filtering -- which is the part that has to hold when
the model is having an off day.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from conftest import load_agent


@pytest.fixture
def analyst():
    return load_agent("analyst")


# --------------------------------------------------------------------------
# Input: an aggregate metrics blob with a missed-work document in it
# --------------------------------------------------------------------------


def metrics_blob(*, revenue_available: bool = False) -> dict[str, Any]:
    return {
        "report_type": "daily",
        "date": "2026-07-20",
        "offered": 120,
        "accepted": 80,
        "totals": {"offered": 120, "accepted": 80, "denied": 12, "acceptance_rate": 0.667},
        "by_service_class": {
            "tow": {"offered": 100, "accepted": 75},
            "light_service": {"offered": 20, "accepted": 5},
        },
        "by_client": [
            {"client_key": "agero", "client_name": "Agero", "offered": 120, "accepted": 80},
        ],
        "missed_work": {
            "totals": {
                "offers": 120,
                "accepted": 80,
                "missed": 40,
                "recoverable": 30,
                "withdrew": 10,
                "declined": 12,
                "no_response": 18,
                "acceptance_rate": 0.667,
                "missed_rate": 0.333,
            },
            "by_cause": {
                "attention": {
                    "cause": "attention",
                    "remedy": "alerting",
                    "question": "Which hour-of-week window is unmanned?",
                    "missed": 18,
                    "share": 0.45,
                    "recoverable": 18,
                    "buckets": {"no_response": 18},
                    "service_classes": {"tow": 15, "light_service": 3},
                    "top_clients": [{"client": "Agero", "missed": 15, "share": 0.83}],
                },
                "equipment": {
                    "cause": "equipment",
                    "remedy": "capital",
                    "question": "Which truck class is missing?",
                    "missed": 12,
                    "share": 0.30,
                    "recoverable": 12,
                    "buckets": {"declined": 12},
                    "service_classes": {"tow": 12},
                    "top_clients": [{"client": "Agero", "missed": 12, "share": 1.0}],
                },
            },
            "inventory": [
                {
                    "service_class": "tow",
                    "cause": "attention",
                    "remedy": "alerting",
                    "question": "Which hour-of-week window is unmanned?",
                    "offers": 100,
                    "accepted": 75,
                    "missed": 15,
                    "missed_share": 0.375,
                    "recoverable": 15,
                    "buckets": {"no_response": 15},
                    "top_clients": [{"client": "Agero", "missed": 15, "share": 1.0}],
                    "top_service_types": [{"service_type_raw": "Tow", "missed": 15, "share": 1.0}],
                }
            ],
            "inventory_meta": {
                "restricted_to_should_accept": True,
                "service_classes": ["tow", "winch_out"],
                "rank_by": "job_count",
                "top_n": 10,
                "missed_in_inventory": 15,
                "missed_in_window": 40,
            },
            "blind_spots": {
                "rows": 7,
                "cols": 24,
                # The dense arrays compact_for_prompt exists to remove.
                "offers": [[1] * 24 for _ in range(7)],
                "accepted": [[0] * 24 for _ in range(7)],
                "no_response": [[1] * 24 for _ in range(7)],
                "no_response_rate": [[1.0] * 24 for _ in range(7)],
                "cells": [{"label": f"cell-{index}"} for index in range(168)],
                "blind_spots": [
                    {
                        "weekday": "Sun",
                        "hour": 20,
                        "label": "Sun 20:00",
                        "offers": 19,
                        "accepted": 5,
                        "no_response": 11,
                        "no_response_rate": 0.58,
                    }
                ],
                "by_hour": [{"hour": 20, "label": "20:00", "offers": 19, "no_response": 11}],
            },
            "closeoff_candidates": {
                "clients": [
                    {
                        "client": "Agero",
                        "client_key": "agero",
                        "client_offers": 120,
                        "service_types": [
                            {
                                "service_type_raw": "Tire Change",
                                "offers": 145,
                                "accepted": 0,
                                "rate": 0.0,
                                "share_of_client_offers": 0.2,
                            }
                        ],
                    }
                ],
                "by_service_type": [
                    {"service_type_raw": "Tire Change", "offers": 145, "accepted": 0}
                ],
                "wanted_baseline": {"service_classes": ["tow", "winch_out"], "offers": 120},
            },
            "client_comparison": {
                "clients": [],
                "findings": [
                    {
                        "kind": "no_response_gap",
                        "severity": "high",
                        "ranked_by": "jobs lost",
                        "spread_pp": 34.0,
                        "worst": {"client": "Agero", "offers": 2006, "no_response": 685},
                        "best": {"client": "Allstate", "offers": 682, "no_response": 1},
                        "summary": "Agero offers go unanswered far more often than Allstate's.",
                    }
                ],
            },
            "ranking_basis": "job_count",
            "revenue_available": revenue_available,
        },
    }


def canned_reply(**overrides: Any) -> dict[str, Any]:
    """A well-formed model reply, with every list populated and sourced."""
    reply: dict[str, Any] = {
        "narrative": "We missed 40 of 120 offers; 30 were recoverable.",
        "missed_work_ranked": [
            {
                "work": "tow",
                "missed": 15,
                "cause": "attention",
                "supporting_number": "15 of 100 tow offers",
            }
        ],
        "cause_assessments": [
            {
                "cause": "attention",
                "want_this_work": "yes",
                "what_it_would_take": "Cover Sun 20:00.",
                "supporting_number": "18 missed jobs",
            }
        ],
        "close_off_requests": [
            {
                "client": "Agero",
                "service_type": "Tire Change",
                "supporting_number": "145 offers, 0 accepted",
            }
        ],
        "clients_needing_attention": [
            {
                "client": "Agero",
                "reason": "offers go unanswered",
                "supporting_numbers": "685 of 2006",
            }
        ],
        "trend_statements": [
            {"statement": "Unanswered offers grew", "supporting_number": "18 against 9"}
        ],
        "proposed_rules": [],
    }
    reply.update(overrides)
    return reply


@pytest.fixture
def with_model(analyst, monkeypatch: pytest.MonkeyPatch):
    """Run the LLM path with a canned reply. No network, no SDK, no key."""

    def _install(reply: dict[str, Any]):
        monkeypatch.setattr(analyst, "_api_key_present", lambda: True)
        monkeypatch.setattr(
            analyst, "_call_model", lambda system, user: (dict(reply), "canned-model")
        )
        return reply

    return _install


# ==========================================================================
# Guard rail 1 -- aggregates only
# ==========================================================================


def test_row_level_data_never_leaves_the_process(analyst) -> None:
    metrics = metrics_blob()
    metrics["requests"] = [{"request_id": "DR1", "pickup_location": "12 Main St"}]
    metrics["sample"] = {"request_id": "DR2", "driver_assigned": "Bob"}

    clean, removed = analyst.sanitize_metrics(metrics)

    assert "requests" not in clean
    assert "sample" not in clean
    assert removed, "the guard must report what it stripped"
    serialised = repr(clean)
    assert "12 Main St" not in serialised and "Bob" not in serialised


def test_the_guard_still_runs_on_the_llm_path(analyst, with_model, monkeypatch) -> None:
    """Sanitising must happen before the prompt is built, not after."""
    seen: dict[str, str] = {}

    monkeypatch.setattr(analyst, "_api_key_present", lambda: True)

    def capture(system: str, user: str):
        seen["user"] = user
        return canned_reply(), "canned-model"

    monkeypatch.setattr(analyst, "_call_model", capture)

    metrics = metrics_blob()
    metrics["requests"] = [{"request_id": "DR1", "pickup_location": "12 Main St"}]
    analyst.analyze(metrics, "daily", persist_proposals=False)

    assert "12 Main St" not in seen["user"]


# ==========================================================================
# Guard rail 2 -- every claim carries its number
# ==========================================================================


@pytest.mark.parametrize(
    "key, unsourced",
    [
        ("missed_work_ranked", {"work": "tow", "missed": 9, "cause": "attention"}),
        (
            "cause_assessments",
            {"cause": "staffing", "want_this_work": "yes", "what_it_would_take": "hire"},
        ),
        ("close_off_requests", {"client": "NSD", "service_type": "Lock Out"}),
        ("clients_needing_attention", {"client": "NSD", "reason": "slow"}),
        ("trend_statements", {"statement": "things got worse"}),
    ],
)
def test_an_unsourced_claim_is_dropped(analyst, with_model, key, unsourced) -> None:
    """Not "flagged". Dropped. An unsourced claim reads exactly like a sourced one."""
    reply = canned_reply()
    reply[key] = list(reply[key]) + [unsourced]
    with_model(reply)

    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)

    assert len(result[key]) == 1, f"{key} kept an entry with no supporting number: {result[key]!r}"
    assert result["dropped_unsourced_statements"] >= 1


def test_a_polite_evasion_counts_as_no_number(analyst, with_model) -> None:
    """"N/A" is not a supporting number; it is the absence of one, spelled out."""
    reply = canned_reply(
        trend_statements=[{"statement": "no change", "supporting_number": "N/A"}]
    )
    with_model(reply)

    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)
    assert result["trend_statements"] == []


# ==========================================================================
# Guard rail 3 -- NO DOLLARS
# ==========================================================================


@pytest.mark.parametrize(
    "key, claim",
    [
        (
            "missed_work_ranked",
            {
                "work": "tow",
                "missed": 15,
                "cause": "attention",
                "supporting_number": "15 tows worth about $4,500",
            },
        ),
        (
            "cause_assessments",
            {
                "cause": "equipment",
                "want_this_work": "yes",
                "what_it_would_take": "A rotator would recover this lost revenue.",
                "supporting_number": "12 missed jobs",
            },
        ),
        (
            "close_off_requests",
            {
                "client": "Agero",
                "service_type": "Tire Change",
                "supporting_number": "145 offers, roughly 8000 dollars",
            },
        ),
        (
            "trend_statements",
            {
                "statement": "Unanswered offers cost us more than last week",
                "supporting_number": "18 against 9",
            },
        ),
    ],
)
def test_a_dollar_claim_is_dropped(analyst, with_model, key, claim) -> None:
    """offerAmount is empty on 100% of records, so any figure was invented.

    The owner acts on the biggest number in the report. A hallucinated dollar
    total is a number he cannot check and would be entirely reasonable to
    believe, which makes it the most dangerous output this system can produce.
    """
    reply = canned_reply()
    reply[key] = [claim]
    with_model(reply)

    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)

    assert result[key] == [], f"{key} kept a money claim: {result[key]!r}"


def test_a_money_sentence_is_cut_out_of_the_narrative(analyst, with_model) -> None:
    """One stray sentence must not delete a paragraph of correct analysis."""
    with_model(
        canned_reply(
            narrative=(
                "We missed 40 of 120 offers. That cost us thousands in lost revenue. "
                "Most of it was nobody answering."
            )
        )
    )

    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)

    assert "We missed 40 of 120 offers." in result["narrative"]
    assert "Most of it was nobody answering." in result["narrative"]
    assert "revenue" not in result["narrative"].lower()
    assert result["dropped_money_claims"] == 1


def test_dollars_are_dropped_even_when_job_values_are_configured(
    analyst, with_model
) -> None:
    """The guard does NOT lift when missed_work.job_value_by_client is populated.

    The owner has since supplied real per-client average job values, so
    agents/missed_work.py can compute an estimated_value -- a traceable number,
    reproducible from a table anyone can read, which the report shows on its
    own. None of that makes a sentence a model WROTE checkable: it can apply a
    tow average to a missed tire change, quote the value config flags as an
    assumption without flagging it, or multiply wrong, and each arrives looking
    exactly like the traceable figure.

    So the flag is reported and the money still goes.
    """
    with_model(
        canned_reply(
            narrative="We missed 40 offers, an estimated $6,000 of gross job value.",
            trend_statements=[
                {
                    "statement": "Missed work is worth more than last week",
                    "supporting_number": "$6,000 estimated against $4,000",
                }
            ],
        )
    )

    result = analyst.analyze(
        metrics_blob(revenue_available=True), "daily", persist_proposals=False
    )

    assert result["revenue_available"] is True, "the flag must still be reported"
    assert "$6,000" not in result["narrative"]
    assert result["trend_statements"] == []


def test_the_money_guard_has_no_opt_out(analyst) -> None:
    """Structural: no caller can pass a flag that turns the rule off."""
    import inspect

    parameters = inspect.signature(analyst._keep_sourced).parameters
    assert "allow_money" not in parameters, (
        "_keep_sourced grew an opt-out; the money rule is unconditional so that "
        "a config edit cannot silently switch it off mid-build"
    )


def test_the_prompt_tells_the_model_there_are_no_dollar_figures(analyst, monkeypatch) -> None:
    """The filter is the backstop. The instruction is the first line of defence."""
    seen: dict[str, str] = {}
    monkeypatch.setattr(analyst, "_api_key_present", lambda: True)

    def capture(system: str, user: str):
        seen["system"], seen["user"] = system, user
        return canned_reply(), "canned-model"

    monkeypatch.setattr(analyst, "_call_model", capture)
    analyst.analyze(metrics_blob(), "daily", persist_proposals=False)

    combined = f"{seen['system']}\n{seen['user']}".lower()
    assert "you do not write dollar figures" in combined
    assert "100% of records" in combined
    for forbidden in ("estimate", "revenue", "job value"):
        assert forbidden in combined, f"the prompt never mentions {forbidden}"


def test_the_prompt_forbids_money_even_when_values_are_configured(
    analyst, monkeypatch
) -> None:
    """One money rule, not two. A softer variant would switch itself on."""
    seen: dict[str, str] = {}
    monkeypatch.setattr(analyst, "_api_key_present", lambda: True)

    def capture(system: str, user: str):
        seen["user"] = user
        return canned_reply(), "canned-model"

    monkeypatch.setattr(analyst, "_call_model", capture)
    analyst.analyze(metrics_blob(revenue_available=True), "daily", persist_proposals=False)

    user = seen["user"].lower()
    assert "no dollar figure can be derived from it" in user
    assert "not yours to quote" in user


def test_the_prompt_asks_the_five_missed_work_questions_in_order(
    analyst, monkeypatch
) -> None:
    seen: dict[str, str] = {}
    monkeypatch.setattr(analyst, "_api_key_present", lambda: True)

    def capture(system: str, user: str):
        seen["user"] = user
        return canned_reply(), "canned-model"

    monkeypatch.setattr(analyst, "_call_model", capture)
    analyst.analyze(metrics_blob(), "daily", persist_proposals=False)

    user = seen["user"]
    positions = [
        user.index("missed_work_ranked"),
        user.index("cause_assessments"),
        user.index("close_off_requests"),
        user.index("trend_statements"),
    ]
    assert positions == sorted(positions), (
        "the five questions must be asked in the order MISSED_WORK_MODEL.md s9 "
        "sets out: what did we not get, do we want it, what would it take, who "
        "should stop sending it, what changed"
    )
    assert "which truck class" in user.lower()
    assert "which hours" in user.lower()
    assert "which territory" in user.lower()


def test_the_grid_is_left_out_of_the_prompt_but_the_findings_are_not(analyst) -> None:
    """A weekly prompt carries two of these documents. Grids blow the budget."""
    clean, _ = analyst.sanitize_metrics(metrics_blob())
    compact = analyst.compact_for_prompt(clean)

    spots = compact["missed_work"]["blind_spots"]
    assert "cells" not in spots and "no_response_rate" not in spots
    assert "grid_omitted" in spots
    # Everything a question refers to survives.
    assert spots["blind_spots"][0]["label"] == "Sun 20:00"
    assert spots["by_hour"][0]["no_response"] == 11
    assert compact["missed_work"]["totals"]["missed"] == 40
    assert compact["missed_work"]["inventory"][0]["missed"] == 15


def test_the_prior_week_document_is_dropped_but_its_trend_is_kept(analyst) -> None:
    """compute_weekly embeds last week's whole document. The trend restates it.

    Measured on real traffic: 88,000 chars sanitised, 49,700 after this. Without
    it a weekly prompt overruns MAX_PROMPT_CHARS and the JSON is cut mid-object,
    which is not a shorter document -- it is a broken one.
    """
    weekly = {
        "report_type": "weekly",
        "missed_work": metrics_blob()["missed_work"],
        "prior_missed_work": metrics_blob()["missed_work"],
        "missed_work_trend": {
            "by_cause": {
                "attention": {"cause": "attention", "missed": 18, "missed_prior": 9},
            },
            "totals": {"missed": {"missed": 40, "missed_prior": 25, "direction": "up"}},
        },
    }
    clean, _ = analyst.sanitize_metrics(weekly)
    compact = analyst.compact_for_prompt(clean)

    assert "prior_missed_work" not in compact
    # Question 5 is still answerable: the deltas are all in the trend.
    assert compact["missed_work_trend"]["by_cause"]["attention"]["missed_prior"] == 9
    assert compact["missed_work_trend"]["totals"]["missed"]["direction"] == "up"
    assert compact["missed_work"]["totals"]["missed"] == 40


def test_a_real_weekly_prompt_fits(analyst, metrics) -> None:
    """The end the truncation guard exists for, measured rather than assumed."""
    import json

    from towbook_agent.core.db import get_session
    from towbook_agent.core.models import Request

    from datetime import datetime, timedelta

    with get_session() as session:
        for index in range(400):
            moment = datetime(2026, 7, 20) + timedelta(minutes=25 * index)
            session.add(
                Request(
                    request_id=f"WK-{index:04d}",
                    account_id="default",
                    client_name="Agero" if index % 3 else "Allstate",
                    client_key="agero" if index % 3 else "allstate",
                    offered_at=moment,
                    status=["accepted", "expired", "denied", "canceled"][index % 4],
                    status_raw=["Accepted", "Expired", "Rejected", "Cancelled"][index % 4],
                    denial_reason="Equipment Not Available" if index % 4 == 2 else None,
                    service_type_raw=["Tow", "Tire Change", "Light Tow"][index % 3],
                    service_class=["tow", "light_service", "tow"][index % 3],
                )
            )

    document = metrics.compute_weekly("2026-07-20", persist=False, emit_alerts=False)
    clean, _ = analyst.sanitize_metrics(document)
    serialised = json.dumps(
        analyst.compact_for_prompt(clean), indent=2, sort_keys=True, default=str
    )

    assert len(serialised) < analyst.MAX_PROMPT_CHARS, (
        f"a weekly prompt is {len(serialised):,} chars against a "
        f"{analyst.MAX_PROMPT_CHARS:,} limit; it would be cut mid-object"
    )
    json.loads(serialised)  # and it is still valid JSON


# ==========================================================================
# Guard rail 4 -- proposals go to rules.proposed.yaml and nowhere else
# ==========================================================================


def test_proposals_are_written_only_to_rules_proposed_yaml(analyst, config_dir) -> None:
    rules_path = config_dir / "rules.yaml"
    before = rules_path.read_bytes()

    written = analyst.record_proposals(
        [
            {
                "id": "light-lockout",
                "target": "service_classes",
                "rationale": "Light Lock-out matched nothing 25 times.",
                "service_type_raw_samples": ["Light Lock-out"],
                "occurrence_count": 25,
                "patch_yaml": "service_classes:\n  light_service:\n    match_any: [lock-out]\n",
            }
        ],
        "daily",
    )

    assert written == 1
    assert rules_path.read_bytes() == before, "the Analyst modified config/rules.yaml"

    proposed = yaml.safe_load((config_dir / "rules.proposed.yaml").read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in proposed["proposals"]]
    assert "light-lockout" in ids
    entry = next(item for item in proposed["proposals"] if item["id"] == "light-lockout")
    assert entry["status"] == "pending", "a proposal must not arrive pre-approved"
    # Hard constraint #6: the verbatim strings that motivated it.
    assert entry["service_type_raw_samples"] == ["Light Lock-out"]


def test_the_write_helper_cannot_be_pointed_at_rules_yaml(analyst, monkeypatch) -> None:
    """Structural, not conventional: there is no path argument to get wrong."""
    import inspect

    signature = inspect.signature(analyst.record_proposals)
    assert "path" not in signature.parameters

    monkeypatch.setattr(analyst, "config_path", lambda name: __import__("pathlib").Path("rules.yaml"))
    with pytest.raises(analyst.AnalystError):
        analyst._proposals_path()


def test_a_human_decision_is_never_reopened(analyst, config_dir) -> None:
    analyst.record_proposals(
        [
            {
                "id": "seen-it",
                "target": "service_classes",
                "rationale": "first sighting",
                "service_type_raw_samples": ["Start"],
                "occurrence_count": 6,
            }
        ],
        "daily",
    )
    path = config_dir / "rules.proposed.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in document["proposals"]:
        if entry["id"] == "seen-it":
            entry["status"] = "rejected"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    analyst.record_proposals(
        [
            {
                "id": "seen-it",
                "target": "service_classes",
                "rationale": "second sighting",
                "service_type_raw_samples": ["Start"],
                "occurrence_count": 99,
            }
        ],
        "daily",
    )

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = next(item for item in document["proposals"] if item["id"] == "seen-it")
    assert entry["status"] == "rejected"
    assert entry["rationale"] == "first sighting"
    assert entry["occurrence_count"] == 6


# ==========================================================================
# Guard rail 5 -- the commentary can never stop a report
# ==========================================================================


def test_without_an_api_key_the_analyst_still_answers_the_five_questions(analyst) -> None:
    """Degradation, not failure -- and degradation into the same shape.

    A fallback that quietly reverted to leading with acceptance rate would be
    reporting the wrong thing on exactly the days the system is least healthy.
    """
    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)

    assert result["llm"] == "unavailable"
    assert result["llm_reason"] == "no_api_key"

    # 1. what did we not get
    assert result["missed_work_ranked"], result
    assert result["missed_work_ranked"][0]["work"] == "tow"
    # 2 + 3. do we want it, and what would it take
    causes = {item["cause"]: item for item in result["cause_assessments"]}
    assert causes["attention"]["want_this_work"] == "yes"
    assert "unmanned" in causes["attention"]["what_it_would_take"]
    # 4. who should stop sending it
    assert result["close_off_requests"][0]["service_type"] == "Tire Change"
    assert "145 offers" in result["close_off_requests"][0]["supporting_number"]
    # the narrative leads with the miss, not the rate
    assert result["narrative"].index("40") < result["narrative"].index("Acceptance rate")
    # and it says what unit it is counting in
    assert "job counts" in result["narrative"]


def test_the_fallback_carries_the_client_gap_finding(analyst) -> None:
    """The strongest finding the system produces must survive an LLM outage."""
    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)
    clients = [item["client"] for item in result["clients_needing_attention"]]
    assert "Agero" in clients
    entry = next(item for item in result["clients_needing_attention"] if item["client"] == "Agero")
    assert "685 of 2006" in entry["supporting_numbers"]


def test_the_fallback_never_mentions_money(analyst) -> None:
    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)
    blob = repr(result)
    assert not analyst.MONEY_PATTERN.search(result["narrative"])
    assert "$" not in blob


@pytest.mark.parametrize(
    "failure",
    [
        lambda: (_ for _ in ()).throw(RuntimeError("api exploded")),
        lambda: (_ for _ in ()).throw(ImportError("no anthropic sdk")),
    ],
)
def test_a_broken_model_call_degrades_instead_of_raising(
    analyst, monkeypatch, failure
) -> None:
    monkeypatch.setattr(analyst, "_api_key_present", lambda: True)
    monkeypatch.setattr(analyst, "_call_model", lambda system, user: failure())

    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)

    assert result["llm"] == "unavailable"
    assert result["narrative"], "a degraded analysis must still say something"
    assert result["missed_work_ranked"], "and still answer question 1"


def test_a_model_returning_junk_degrades_instead_of_raising(analyst, monkeypatch) -> None:
    monkeypatch.setattr(analyst, "_api_key_present", lambda: True)
    monkeypatch.setattr(
        analyst,
        "_call_model",
        lambda system, user: (_ for _ in ()).throw(
            analyst.AnalystError("model response contained no JSON object")
        ),
    )

    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)
    assert result["llm"] == "unavailable"
    assert "no JSON object" in result["llm_reason"]


def test_analyze_reports_how_it_ranked(analyst) -> None:
    result = analyst.analyze(metrics_blob(), "daily", persist_proposals=False)
    assert result["ranking_basis"] == "job_count"
    assert result["revenue_available"] is False


def test_the_missed_work_job_list_never_reaches_the_model(analyst) -> None:
    """The one row-level section of the missed-work document.

    It exists so the owner can look a job up in Towbook, which means it carries
    a reference, a client, a pickup address and a decline reason for named
    individual jobs. None of that is aggregate and none of it goes to an API.
    """
    metrics = metrics_blob()
    metrics["missed_work"] = {
        "totals": {"offers": 30, "missed": 18},
        "missed_jobs": [
            {
                "towbook_ref": "125169",
                "towbook_ref_kind": "job",
                "job_number": "125169",
                "request_id": "324417205",
                "client": "Agero (Swoop)",
                "pickup_location": "COOPER RD, COLUMBUS OH 43231",
                "denial_reason": "Equipment Not Available",
            }
        ],
        "missed_jobs_meta": {"shown": 1, "with_job_number": 1},
    }

    clean, removed = analyst.sanitize_metrics(metrics)

    assert "missed_jobs" not in clean["missed_work"]
    assert any("missed_jobs" in path for path in removed)
    serialised = repr(clean)
    assert "125169" not in serialised
    assert "COOPER RD" not in serialised
    # The aggregate half of the same document is untouched.
    assert clean["missed_work"]["totals"]["missed"] == 18
