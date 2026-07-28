"""Reclassifying history after a rules change.

This is why ``service_type_raw`` is immutable (hard constraint #6). The verbatim
string the portal sent is kept forever, so when someone finally adds a rule for
"Motorcycle Transport" every row that ever carried it can be reclassified --
without re-scraping, without a migration, and without the original text ever
having been altered.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from towbook_agent.core.config_loader import get_rules
from towbook_agent.core.db import get_session
from towbook_agent.core.models import Request, client_key_for

RAW_TYPES = [
    ("BF-1", "Flatbed Tow", "tow"),
    ("BF-2", "Motorcycle Transport", "unclassified"),
    ("BF-3", "Equipment Haul", "unclassified"),
    ("BF-4", "Jump Start", "light_service"),
    ("BF-5", "Winch Out", "winch_out"),
    ("BF-6", "MOTORCYCLE TRANSPORT - no keys", "unclassified"),
]


@pytest.fixture
def historical_rows() -> list[tuple[str, str, str]]:
    """Rows classified under the rules as they ship today."""
    base = datetime(2026, 7, 20, 9, 0, 0)
    with get_session() as session:
        for index, (request_id, raw, service_class) in enumerate(RAW_TYPES):
            session.add(
                Request(
                    request_id=request_id,
                    client_name="Agero",
                    client_key=client_key_for("Agero"),
                    offered_at=base + timedelta(minutes=index * 5),
                    status="accepted",
                    service_type_raw=raw,
                    service_class=service_class,
                    source_run_id="seed",
                )
            )
    return RAW_TYPES


def _stored() -> dict[str, Request]:
    with get_session(commit=False) as session:
        rows = session.execute(select(Request)).scalars()
        return {row.request_id: row for row in rows}


def _add_moto_rule(write_config) -> None:
    base = get_rules()
    write_config(
        "rules",
        {
            "version": 7,
            "service_classes": {
                "moto_transport": {"match_any": ["motorcycle"], "accept": True},
                **{
                    name: spec
                    for name, spec in base["service_classes"].items()
                    if name != "_default"
                },
                "_default": "unclassified",
            },
            "acceptance_policy": base.get("acceptance_policy", {}),
            "alerts": base.get("alerts", []),
        },
    )


# --------------------------------------------------------------------------


def test_backfill_reclassifies_after_a_rules_change(
    classifier, historical_rows, write_config
) -> None:
    before = _stored()
    assert before["BF-2"].service_class == "unclassified"
    assert before["BF-6"].service_class == "unclassified"

    _add_moto_rule(write_config)

    with get_session() as session:
        changed = classifier.backfill(session)

    after = _stored()
    assert after["BF-2"].service_class == "moto_transport"
    # Case-insensitive substring, so the messy variant is caught too.
    assert after["BF-6"].service_class == "moto_transport"
    # Untouched by the new rule.
    assert after["BF-1"].service_class == "tow"
    assert after["BF-3"].service_class == "unclassified"
    assert after["BF-4"].service_class == "light_service"
    assert after["BF-5"].service_class == "winch_out"

    assert changed == 2, f"backfill should report the 2 rows it changed, reported {changed}"


def test_backfill_never_touches_the_verbatim_service_type(
    classifier, historical_rows, write_config
) -> None:
    _add_moto_rule(write_config)

    with get_session() as session:
        classifier.backfill(session)

    after = _stored()
    for request_id, raw, _ in RAW_TYPES:
        assert after[request_id].service_type_raw == raw, (
            "service_type_raw is immutable -- backfill re-derives from it, "
            "it does not rewrite it"
        )


def test_backfill_is_idempotent(classifier, historical_rows, write_config) -> None:
    _add_moto_rule(write_config)

    with get_session() as session:
        first = classifier.backfill(session)
    with get_session() as session:
        second = classifier.backfill(session)

    assert first == 2
    assert second == 0, "a second backfill with unchanged rules must change nothing"


def test_backfill_with_unchanged_rules_changes_nothing(classifier, historical_rows) -> None:
    with get_session() as session:
        changed = classifier.backfill(session)

    assert changed == 0
    after = _stored()
    for request_id, _, expected in RAW_TYPES:
        assert after[request_id].service_class == expected


def test_backfill_narrows_a_class_when_a_rule_is_removed(
    classifier, historical_rows, write_config
) -> None:
    """Deleting a rule must also take effect -- reclassification is not
    one-directional."""
    write_config(
        "rules",
        {
            "version": 8,
            "service_classes": {
                "tow": {"match_any": ["tow", "recovery", "accident", "flatbed"]},
                "_default": "unclassified",
            },
            "acceptance_policy": {},
            "alerts": [],
        },
    )

    with get_session() as session:
        changed = classifier.backfill(session)

    after = _stored()
    assert after["BF-4"].service_class == "unclassified"  # was light_service
    assert after["BF-5"].service_class == "unclassified"  # was winch_out
    assert after["BF-1"].service_class == "tow"
    assert changed == 2


def test_backfill_fixes_rows_that_were_never_classified(classifier) -> None:
    """A row ingested before the classifier ran, or by an older version."""
    with get_session() as session:
        session.add(
            Request(
                request_id="BF-NULL",
                client_name="Agero",
                client_key=client_key_for("Agero"),
                offered_at=datetime(2026, 7, 20, 10, 0, 0),
                status="denied",
                service_type_raw="Heavy Duty Tow - Accident",
                service_class=None,
                source_run_id="seed",
            )
        )

    with get_session() as session:
        changed = classifier.backfill(session)

    assert changed == 1
    assert _stored()["BF-NULL"].service_class == "tow"


def test_backfill_handles_an_empty_service_type(classifier) -> None:
    with get_session() as session:
        session.add(
            Request(
                request_id="BF-EMPTY",
                client_name="Agero",
                client_key=client_key_for("Agero"),
                offered_at=datetime(2026, 7, 20, 10, 0, 0),
                status="denied",
                service_type_raw=None,
                service_class=None,
                source_run_id="seed",
            )
        )

    with get_session() as session:
        classifier.backfill(session)

    assert _stored()["BF-EMPTY"].service_class == "unclassified"
