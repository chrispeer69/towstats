"""Re-ingesting the same window must update, never duplicate.

Hard constraint #4. The hourly job re-reads an overlapping window every time it
runs, and the same request appears in several of those exports as its status
changes: offered, then accepted, then completed. If that produced a new row
each time, every acceptance number in the system would be inflated.

``requests`` is keyed on ``request_id`` and upserted. These tests hold that line.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from conftest import count_rows, get_request, ingest_file, make_row
from fixture_generator import write_rows_xlsx
from towbook_agent.core.models import Request


def _ingest(ingestion, tmp_path: Path, name: str, rows: list[dict], run_id: str) -> None:
    path = write_rows_xlsx(tmp_path / name, rows)
    _, error = ingest_file(ingestion, path, run_id=run_id)
    assert error is None, f"{name} failed to ingest: {error!r}"


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------


def test_reingesting_the_same_file_does_not_duplicate(ingestion, tmp_path: Path) -> None:
    rows = [make_row(f"DR-{index}") for index in range(5)]

    _ingest(ingestion, tmp_path, "first.xlsx", rows, "run-1")
    assert count_rows(Request) == 5

    _ingest(ingestion, tmp_path, "second.xlsx", rows, "run-2")
    assert count_rows(Request) == 5, "re-ingesting the same rows duplicated them"


def test_overlapping_windows_merge_on_request_id(ingestion, tmp_path: Path) -> None:
    """Hour 14 and hour 15 exports share rows. The union is what we keep."""
    first = [make_row(f"DR-{index}") for index in range(1, 8)]
    second = [make_row(f"DR-{index}") for index in range(5, 13)]

    _ingest(ingestion, tmp_path, "w1.xlsx", first, "run-w1")
    _ingest(ingestion, tmp_path, "w2.xlsx", second, "run-w2")

    assert count_rows(Request) == 12


# --------------------------------------------------------------------------
# Status mutation
# --------------------------------------------------------------------------


def test_pending_becomes_accepted_on_the_next_pass(ingestion, tmp_path: Path) -> None:
    offered = datetime(2026, 7, 20, 14, 0, 0)

    _ingest(
        ingestion,
        tmp_path,
        "offered.xlsx",
        [make_row("DR-LIVE", status="Pending", responded_at="", amount=None)],
        "run-a",
    )
    first = get_request("DR-LIVE")
    assert first is not None
    assert first.status == "pending"

    _ingest(
        ingestion,
        tmp_path,
        "accepted.xlsx",
        [
            make_row(
                "DR-LIVE",
                status="Accepted",
                offered_at=offered,
                responded_at=offered + timedelta(minutes=3),
                amount=325.00,
            )
        ],
        "run-b",
    )

    assert count_rows(Request) == 1, "the status change created a second row"
    updated = get_request("DR-LIVE")
    assert updated is not None
    assert updated.status == "accepted"
    assert updated.responded_at == datetime(2026, 7, 20, 14, 3, 0)
    assert Decimal(str(updated.amount)) == Decimal("325.00")
    assert updated.source_run_id == "run-b", "the newest run should own the row"


def test_pending_becomes_expired_on_the_next_pass(ingestion, tmp_path: Path) -> None:
    _ingest(
        ingestion,
        tmp_path,
        "live.xlsx",
        [make_row("DR-TIMEOUT", status="Pending", responded_at="")],
        "run-a",
    )
    assert get_request("DR-TIMEOUT").status == "pending"

    _ingest(
        ingestion,
        tmp_path,
        "expired.xlsx",
        [make_row("DR-TIMEOUT", status="Expired", denial_reason="")],
        "run-b",
    )

    assert count_rows(Request) == 1
    assert get_request("DR-TIMEOUT").status == "expired"


def test_a_denial_reason_arriving_late_is_stored(ingestion, tmp_path: Path) -> None:
    _ingest(
        ingestion,
        tmp_path,
        "denied-bare.xlsx",
        [make_row("DR-LATE", status="Denied", denial_reason="")],
        "run-a",
    )
    _ingest(
        ingestion,
        tmp_path,
        "denied-reason.xlsx",
        [make_row("DR-LATE", status="Denied", denial_reason="All units out on calls")],
        "run-b",
    )

    assert count_rows(Request) == 1
    assert get_request("DR-LATE").denial_reason == "All units out on calls"


# --------------------------------------------------------------------------
# What an upsert must NOT do
# --------------------------------------------------------------------------


def test_service_type_raw_is_never_rewritten_by_an_upsert(ingestion, tmp_path: Path) -> None:
    """Hard constraint #6. The verbatim string is the audit trail; a later
    export tidying it up must not erase what was actually sent."""
    _ingest(
        ingestion,
        tmp_path,
        "raw-first.xlsx",
        [make_row("DR-RAW", service="HEAVY DUTY TOW - ACCIDENT")],
        "run-a",
    )
    assert get_request("DR-RAW").service_type_raw == "HEAVY DUTY TOW - ACCIDENT"

    _ingest(
        ingestion,
        tmp_path,
        "raw-second.xlsx",
        [make_row("DR-RAW", service="HEAVY DUTY TOW - ACCIDENT", status="Completed")],
        "run-b",
    )

    stored = get_request("DR-RAW")
    assert stored.service_type_raw == "HEAVY DUTY TOW - ACCIDENT"
    assert stored.status == "accepted"


def test_ingested_at_moves_but_the_offer_time_does_not(ingestion, tmp_path: Path) -> None:
    offered = datetime(2026, 7, 20, 9, 15, 0)
    row = make_row("DR-STAMP", offered_at=offered, status="Pending", responded_at="")

    _ingest(ingestion, tmp_path, "stamp-a.xlsx", [row], "run-a")
    first = get_request("DR-STAMP")
    first_ingested = first.ingested_at

    _ingest(
        ingestion,
        tmp_path,
        "stamp-b.xlsx",
        [make_row("DR-STAMP", offered_at=offered, status="Accepted")],
        "run-b",
    )
    second = get_request("DR-STAMP")

    assert second.offered_at == offered
    assert second.ingested_at >= first_ingested


def test_two_clients_can_share_nothing_but_still_coexist(ingestion, tmp_path: Path) -> None:
    """A sanity check that the primary key is the request, not the client."""
    rows = [
        make_row("DR-A", client="Agero"),
        make_row("DR-B", client="AGERO"),
        make_row("DR-C", client="Quest Roadside"),
    ]
    _ingest(ingestion, tmp_path, "clients.xlsx", rows, "run-clients")

    assert count_rows(Request) == 3
    # client_key is trimmed + casefolded, so the first two are the same client.
    assert get_request("DR-A").client_key == get_request("DR-B").client_key == "agero"
    assert get_request("DR-C").client_key == "quest roadside"


def test_upsert_survives_a_row_appearing_three_times_in_one_file(
    ingestion, tmp_path: Path
) -> None:
    """Exports do occasionally repeat a row. Last one wins, still one row."""
    rows = [
        make_row("DR-DUP", status="Pending", responded_at=""),
        make_row("DR-DUP", status="Accepted"),
        make_row("DR-DUP", status="Completed"),
    ]
    _ingest(ingestion, tmp_path, "dupes.xlsx", rows, "run-dupes")

    assert count_rows(Request) == 1
    assert get_request("DR-DUP").status == "accepted"
