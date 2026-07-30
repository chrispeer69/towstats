"""Ingestion: map the export onto the canonical record, or fail loudly.

The rule this module exists to enforce is the one from schema.yaml: header
drift is a hard failure. A wrong number is worse than a missing one, so an
export whose headers no longer match must write down what it actually saw,
raise a pipeline_failure, and store nothing at all. "Guessed the column" is not
an acceptable outcome.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import (
    SANDBOX_ROOT,
    attr,
    count_rows,
    get_request,
    ingest_file,
    make_row,
    set_schema_scalar,
    set_schema_source_timezone,
)
from fixture_generator import header_for, write_rows_xlsx
from towbook_agent.core.config_loader import get_schema
from towbook_agent.core.models import Request, Run

DETECTED_SCHEMA = SANDBOX_ROOT / "config" / "schema.detected.yaml"


@pytest.fixture(autouse=True)
def _no_stale_detected_file() -> None:
    if DETECTED_SCHEMA.exists():
        DETECTED_SCHEMA.unlink()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_valid_headers_ingest_every_row(ingestion, tmp_path: Path) -> None:
    rows = [
        make_row("DR-1", status="Accepted", service="Flatbed Tow", amount=250.0),
        make_row("DR-2", status="Denied", service="Tire Change", denial_reason="No trucks"),
        make_row("DR-3", status="Expired", service="Winch Out"),
    ]
    path = write_rows_xlsx(tmp_path / "ok.xlsx", rows)

    result, error = ingest_file(ingestion, path, run_id="run-ok")
    assert error is None, f"ingest raised on a valid export: {error!r}"
    assert count_rows(Request) == 3
    written = attr(
        result, "rows_upserted", "rows_inserted", "inserted", "rows", "row_count", "count"
    )
    assert int(written) == 3


def test_fields_map_onto_the_canonical_record(ingestion, tmp_path: Path) -> None:
    row = make_row(
        "DR-MAP",
        client="  Quest Roadside  ",
        offered_at=datetime(2026, 7, 20, 14, 30, 0),
        status="Completed",
        service="  Heavy Duty TOW - Accident  ",
        denial_reason="",
        amount="$1,234.50",
    )
    path = write_rows_xlsx(tmp_path / "map.xlsx", [row])

    _, error = ingest_file(ingestion, path, run_id="run-map")
    assert error is None

    stored = get_request("DR-MAP")
    assert stored is not None, "the row was not stored under its source ID"

    # Verbatim, hard constraint #6: the raw service string is never touched.
    assert stored.service_type_raw == "  Heavy Duty TOW - Accident  "
    # Controlled vocabulary: "Completed" is a source string, "accepted" is ours.
    assert stored.status == "accepted"
    # client_key is trimmed and casefolded; client_name keeps something readable.
    assert stored.client_key == "quest roadside"
    assert stored.client_name.strip() == "Quest Roadside"
    # TZ is UTC in the suite, so local and stored coincide -- see the Detroit
    # test below for the conversion itself.
    assert stored.offered_at == datetime(2026, 7, 20, 14, 30, 0)
    assert stored.offered_at.tzinfo is None, "timestamps are stored naive UTC"
    # "$1,234.50" is a currency string in the export, a number in the database.
    assert stored.amount is not None
    assert Decimal(str(stored.amount)) == Decimal("1234.50")
    assert stored.pickup_location
    assert stored.ingested_at is not None
    assert stored.source_run_id == "run-map"


def test_status_vocabulary_is_applied(ingestion, tmp_path: Path) -> None:
    pairs = [
        ("Accepted", "accepted"),
        ("Completed", "accepted"),
        ("Dispatched", "accepted"),
        ("Denied", "denied"),
        ("Declined", "denied"),
        ("Expired", "expired"),
        ("No Response", "expired"),
        ("Canceled By Client", "canceled"),
        ("GOA", "canceled"),
        ("Pending", "pending"),
    ]
    rows = [
        make_row(f"DR-S{index}", status=source) for index, (source, _) in enumerate(pairs)
    ]
    path = write_rows_xlsx(tmp_path / "status.xlsx", rows)

    _, error = ingest_file(ingestion, path, run_id="run-status")
    assert error is None

    for index, (_, canonical) in enumerate(pairs):
        stored = get_request(f"DR-S{index}")
        assert stored is not None, f"row DR-S{index} was dropped"
        assert stored.status == canonical


def test_null_tokens_become_empty_not_literal_text(ingestion, tmp_path: Path) -> None:
    rows = [make_row("DR-NULL", status="Expired", denial_reason="N/A", amount=None)]
    path = write_rows_xlsx(tmp_path / "nulls.xlsx", rows)

    _, error = ingest_file(ingestion, path, run_id="run-null")
    assert error is None

    stored = get_request("DR-NULL")
    assert stored is not None
    assert stored.denial_reason in (None, ""), f"null token stored verbatim: {stored.denial_reason!r}"
    assert stored.amount is None


def test_a_run_row_is_recorded(ingestion, tmp_path: Path) -> None:
    path = write_rows_xlsx(tmp_path / "run.xlsx", [make_row("DR-R1")])
    _, error = ingest_file(ingestion, path, run_id="run-recorded")
    assert error is None
    assert count_rows(Run) >= 1


# --------------------------------------------------------------------------
# Header drift -- the hard failure
# --------------------------------------------------------------------------


def _mismatched_headers_file(path: Path) -> Path:
    """An export whose headers no longer resemble the Digital Requests grid."""
    fields = ["request_id", "client_name", "offered_at", "status", "service_type_raw"]
    return write_rows_xlsx(
        path,
        [make_row("DR-X1"), make_row("DR-X2")],
        fields=fields,
        headers=["Ref", "Party", "Stamp", "Outcome", "Work Type"],
    )


def test_header_mismatch_ingests_zero_rows(ingestion, tmp_path: Path, captured_events) -> None:
    path = _mismatched_headers_file(tmp_path / "drifted.xlsx")

    _, error = ingest_file(ingestion, path, run_id="run-drift")

    assert count_rows(Request) == 0, "a drifted export must not be guessed at"


def test_header_mismatch_emits_pipeline_failure(ingestion, tmp_path: Path, captured_events) -> None:
    """Silence is never treated as success -- hard constraint #5."""
    path = _mismatched_headers_file(tmp_path / "drifted.xlsx")

    ingest_file(ingestion, path, run_id="run-drift")

    failures = captured_events.pipeline_failures
    assert failures, (
        "header drift must emit a pipeline_failure; "
        f"events seen: {[kind for kind, _ in captured_events.events]}"
    )
    payload = failures[0]
    assert payload.get("severity", "high") == "high"


def test_header_mismatch_writes_schema_detected_yaml(ingestion, tmp_path: Path, captured_events) -> None:
    """The operator has to be able to see what the export actually contained."""
    configured = (get_schema().get("header_validation") or {}).get(
        "detected_output", "config/schema.detected.yaml"
    )
    expected = SANDBOX_ROOT / configured

    path = _mismatched_headers_file(tmp_path / "drifted.xlsx")
    ingest_file(ingestion, path, run_id="run-drift")

    assert expected.is_file(), f"{configured} was not written"
    text = expected.read_text(encoding="utf-8")
    for header in ("Ref", "Party", "Stamp", "Outcome", "Work Type"):
        assert header in text, f"detected headers do not mention {header!r}"


def test_a_missing_required_header_is_also_drift(ingestion, tmp_path: Path, captured_events) -> None:
    """Only ``Status`` is gone. That is still a hard failure, not a default."""
    fields = ["request_id", "offered_at", "service_type_raw", "status"]
    headers = [
        header_for("request_id"),
        header_for("offered_at"),
        header_for("service_type_raw"),
        "Outcome Code",  # not a candidate for `status`
    ]
    path = write_rows_xlsx(
        tmp_path / "nostatus.xlsx",
        [make_row("DR-NS1"), make_row("DR-NS2")],
        fields=fields,
        headers=headers,
    )

    ingest_file(ingestion, path, run_id="run-nostatus")

    assert count_rows(Request) == 0
    assert captured_events.pipeline_failures


def test_extra_columns_are_not_drift(ingestion, tmp_path: Path) -> None:
    """allow_extra_headers is true: Towbook adding a column must not stop us."""
    row = make_row("DR-EXTRA")
    row["Brand New Column"] = "surprise"
    path = write_rows_xlsx(tmp_path / "extra.xlsx", [row])

    _, error = ingest_file(ingestion, path, run_id="run-extra")
    assert error is None
    assert count_rows(Request) == 1


def test_a_title_banner_above_the_grid_is_survivable(ingestion, tmp_path: Path) -> None:
    """header_row_scan exists so a report title above the header row is fine."""
    path = write_rows_xlsx(
        tmp_path / "banner.xlsx",
        [make_row("DR-B1"), make_row("DR-B2")],
        banner_rows=["Towbook - Digital Requests", "07/20/2026 - 07/26/2026", ""],
    )

    _, error = ingest_file(ingestion, path, run_id="run-banner")
    assert error is None
    assert count_rows(Request) == 2


# --------------------------------------------------------------------------
# Unknown statuses
# --------------------------------------------------------------------------


def _one_unknown_status(tmp_path: Path, name: str = "unknown.xlsx") -> Path:
    """19 ordinary rows and one status the vocabulary has never seen: 5%,
    comfortably under unknown_status_abort_threshold."""
    rows = [make_row(f"DR-U{index}", status="Accepted") for index in range(19)]
    rows.append(make_row("DR-UNKNOWN", status="Escalated To Manager"))
    return write_rows_xlsx(tmp_path / name, rows)


def test_an_unknown_status_is_never_guessed_at(ingestion, tmp_path: Path) -> None:
    """Whatever unknown_status_action is set to, the one thing that must never
    happen is an unmapped string being read as an acceptance."""
    path = _one_unknown_status(tmp_path)

    result, error = ingest_file(ingestion, path, run_id="run-unknown")
    assert error is None, f"5% unknown statuses is under the threshold: {error!r}"

    stored = get_request("DR-UNKNOWN")
    assert stored is None or stored.status != "accepted"

    unmapped = attr(result, "unmapped_statuses", "unknown_statuses", default={})
    assert "Escalated To Manager" in unmapped, (
        "the unmapped string must be reported so schema.yaml can be extended, "
        f"got {unmapped!r}"
    )


def test_reject_row_drops_the_row_and_keeps_the_others(ingestion, tmp_path: Path) -> None:
    """``unknown_status_action: reject_row`` -- a bad status never reaches the
    numbers. Config decides this; the ingester only obeys."""
    set_schema_scalar("unknown_status_action", "reject_row")
    path = _one_unknown_status(tmp_path, "reject.xlsx")

    _, error = ingest_file(ingestion, path, run_id="run-reject")
    assert error is None

    assert get_request("DR-UNKNOWN") is None
    assert count_rows(Request) == 19


def test_pending_action_parks_the_row_instead_of_dropping_it(
    ingestion, tmp_path: Path
) -> None:
    """``unknown_status_action: pending`` -- the row is still an offer, so it
    stays in the denominator rather than vanishing from it."""
    set_schema_scalar("unknown_status_action", "pending")
    path = _one_unknown_status(tmp_path, "park.xlsx")

    _, error = ingest_file(ingestion, path, run_id="run-park")
    assert error is None

    stored = get_request("DR-UNKNOWN")
    assert stored is not None
    assert stored.status == "pending"
    assert count_rows(Request) == 20


def test_too_many_unknown_statuses_alert(ingestion, tmp_path: Path, captured_events) -> None:
    """Over unknown_status_abort_threshold the export is not trustworthy, and
    that has to be said out loud -- whichever action is configured."""
    rows = [make_row(f"DR-K{index}", status="Accepted") for index in range(5)]
    rows += [make_row(f"DR-W{index}", status="Escalated To Manager") for index in range(5)]
    path = write_rows_xlsx(tmp_path / "mostly-unknown.xlsx", rows)

    result, _ = ingest_file(ingestion, path, run_id="run-mostly-unknown")

    assert captured_events.pipeline_failures, "50% unmapped statuses must alert"
    if result is not None:
        assert attr(result, "status", default="") != "succeeded", (
            "a run this degraded must not report success"
        )


def test_too_many_unknown_statuses_with_reject_row_stores_nothing_bad(
    ingestion, tmp_path: Path, captured_events
) -> None:
    set_schema_scalar("unknown_status_action", "reject_row")
    rows = [make_row(f"DR-K{index}", status="Accepted") for index in range(5)]
    rows += [make_row(f"DR-W{index}", status="Escalated To Manager") for index in range(5)]
    path = write_rows_xlsx(tmp_path / "mostly-unknown-reject.xlsx", rows)

    ingest_file(ingestion, path, run_id="run-mostly-unknown-reject")

    assert captured_events.pipeline_failures
    for index in range(5):
        assert get_request(f"DR-W{index}") is None


# --------------------------------------------------------------------------
# Timezone conversion
# --------------------------------------------------------------------------


def test_local_export_times_are_converted_to_utc(
    ingestion, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """America/Detroit in July is UTC-4, so 14:30 local is 18:30 stored."""
    monkeypatch.setenv("TZ", "America/Detroit")
    set_schema_source_timezone("America/Detroit")

    row = make_row("DR-TZ", offered_at=datetime(2026, 7, 20, 14, 30, 0))
    path = write_rows_xlsx(tmp_path / "tz.xlsx", [row])

    _, error = ingest_file(ingestion, path, run_id="run-tz")
    assert error is None

    stored = get_request("DR-TZ")
    assert stored is not None
    assert stored.offered_at == datetime(2026, 7, 20, 18, 30, 0), (
        "export times are local and must be stored as UTC"
    )


def test_winter_dates_use_the_standard_offset(
    ingestion, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """January is EST, UTC-5. A fixed offset would get this wrong."""
    monkeypatch.setenv("TZ", "America/Detroit")
    set_schema_source_timezone("America/Detroit")

    row = make_row("DR-TZ-WINTER", offered_at=datetime(2026, 1, 15, 9, 0, 0))
    path = write_rows_xlsx(tmp_path / "tz-winter.xlsx", [row])

    _, error = ingest_file(ingestion, path, run_id="run-tz-winter")
    assert error is None

    stored = get_request("DR-TZ-WINTER")
    assert stored is not None
    assert stored.offered_at == datetime(2026, 1, 15, 14, 0, 0)


# --------------------------------------------------------------------------
# Datetime parsing
# --------------------------------------------------------------------------


def test_string_timestamps_parse_through_the_configured_formats(
    ingestion, tmp_path: Path
) -> None:
    """openpyxl gives back real datetimes for date cells, strings otherwise.

    ``Responded`` is written as text by the fixture on purpose, so the
    datetime_formats list in schema.yaml is genuinely exercised.
    """
    row = make_row(
        "DR-STR",
        offered_at=datetime(2026, 7, 20, 14, 0, 0),
        responded_at="07/20/2026 02:05:00 PM",
    )
    path = write_rows_xlsx(tmp_path / "strings.xlsx", [row])

    _, error = ingest_file(ingestion, path, run_id="run-strings")
    assert error is None

    stored = get_request("DR-STR")
    assert stored is not None
    assert stored.responded_at == datetime(2026, 7, 20, 14, 5, 0)


def test_an_empty_sheet_is_not_a_crash(ingestion, tmp_path: Path) -> None:
    """A window with no requests is a real thing that happens at 4am."""
    path = write_rows_xlsx(tmp_path / "empty.xlsx", [])

    result, error = ingest_file(ingestion, path, run_id="run-empty")
    assert error is None, f"an empty export must not raise: {error!r}"
    assert count_rows(Request) == 0


# --------------------------------------------------------------------------
# job_number -- the Towbook reference the reports are read with
#
# The column exists so a report can say WHICH job it is talking about. What
# makes it interesting is that Towbook does not issue a job (call) number until
# an offer becomes a job, so it is blank on most of the offers this system
# exists to explain. These tests pin both halves: the number is stored when
# there is one, and nothing is invented when there is not.
# --------------------------------------------------------------------------


def _json_archive(path: Path, records: list[dict]) -> Path:
    """Write records in the envelope agents/acquisition_api.py archives."""
    path.write_text(json.dumps({"pages": [{"records": records}]}), encoding="utf-8")
    return path


def test_the_call_number_is_stored_as_the_job_number(ingestion, tmp_path: Path) -> None:
    row = make_row("DR-JOB", status="Accepted", **{"Call Number": "125169"})
    path = write_rows_xlsx(tmp_path / "job.xlsx", [row])

    _, error = ingest_file(ingestion, path, run_id="run-job")
    assert error is None

    stored = get_request("DR-JOB")
    assert stored is not None
    assert stored.job_number == "125169"


def test_a_blank_call_number_stores_nothing(ingestion, tmp_path: Path) -> None:
    """The normal case for work we did not take, and it must not invent a value."""
    row = make_row("DR-NOJOB", status="Expired", **{"Call Number": ""})
    path = write_rows_xlsx(tmp_path / "nojob.xlsx", [row])

    _, error = ingest_file(ingestion, path, run_id="run-nojob")
    assert error is None

    stored = get_request("DR-NOJOB")
    assert stored is not None
    assert stored.job_number is None


def test_a_zero_call_number_is_absence_not_the_number_zero(
    ingestion, tmp_path: Path
) -> None:
    """Towbook sends ``callNumber: 0`` rather than omitting the key.

    1,731 of the 3,124 archived records carry a zero. Storing it would put
    "Job #0" on the reports -- a reference somebody types into Towbook, gets
    nothing back for, and afterwards stops trusting the number. See
    schema.yaml -> value_cleanup.zero_means_absent.
    """
    records = [
        {
            "callRequestId": 900001,
            "requestDate": "2026-07-26T18:31:39.71",
            "status": 5,
            "statusName": "Expired",
            "serviceNeeded": "Light Tow",
            "providerName": "Agero (Swoop)",
            "callNumber": 0,
        },
        {
            "callRequestId": 900002,
            "requestDate": "2026-07-26T19:02:11.10",
            "status": 1,
            "statusName": "Accepted",
            "serviceNeeded": "Light Tow",
            "providerName": "Agero (Swoop)",
            "callNumber": 125169,
        },
    ]
    path = _json_archive(tmp_path / "callnumbers.json", records)

    _, error = ingest_file(ingestion, path, run_id="run-zero")
    assert error is None

    expired = get_request("900001")
    accepted = get_request("900002")
    assert expired is not None and accepted is not None
    assert expired.job_number is None, "a zero call number is not a job number"
    # An integer from JSON becomes the string a human would type, with no
    # float tail.
    assert accepted.job_number == "125169"


def test_the_job_number_never_becomes_the_identity(ingestion, tmp_path: Path) -> None:
    """The same offer, re-exported after it was accepted and given a number.

    Keying on the call number would make this two rows -- one offer counted
    twice, inflating the offered denominator. It upserts on request_id, and the
    later, more truthful pull wins.
    """
    offered = datetime(2026, 7, 20, 14, 0, 0)
    first = write_rows_xlsx(
        tmp_path / "before.xlsx",
        [make_row("DR-SAME", status="Expired", offered_at=offered, **{"Call Number": ""})],
    )
    _, error = ingest_file(ingestion, first, run_id="run-before")
    assert error is None

    second = write_rows_xlsx(
        tmp_path / "after.xlsx",
        [make_row("DR-SAME", status="Accepted", offered_at=offered, **{"Call Number": "125170"})],
    )
    _, error = ingest_file(ingestion, second, run_id="run-after")
    assert error is None

    assert count_rows(Request) == 1, "the call number arriving late created a second row"
    stored = get_request("DR-SAME")
    assert stored is not None
    assert stored.job_number == "125170"
