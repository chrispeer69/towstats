"""The JSON API acquisition path, offline.

Every request is served by an ``httpx.MockTransport``, so the suite stays
offline (conftest blocks sockets outright) while still exercising the real
client, the real redirect following, the real cookie jar and the real
pagination loop. Only the wire is faked.

What these tests are actually protecting
----------------------------------------
* The **page size cap**. ``pageSize=2000`` does not clamp on the live endpoint,
  it returns HTTP 500 after a ~30 second server-side timeout. A caller or a
  config edit must not be able to ask for it.
* The **login success test**: the ``.xtl`` cookie AND being off the login page.
  Towbook does not issue ``.AspNetCore.Cookies``; a test for that name fails a
  working login, which is how this path was originally got wrong.
* **Pagination termination.** A loop that never stops, or one that stops one
  page early, both produce a plausible-looking wrong number.
* **The archive is verbatim.** The whole point of keeping raw/ forever is being
  able to answer a question nobody has thought of yet.
* **Identity.** ``callRequestId`` is the reason this path exists. A payload
  without it must abort, not fall back to a fingerprint.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from conftest import all_requests, count_rows, load_agent
from towbook_agent.core.models import Request

httpx = pytest.importorskip("httpx")


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def api():
    return load_agent("acquisition_api")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry in a unit test must not sleep for 30 seconds."""
    monkeypatch.setenv("TOWBOOK_RETRY_BACKOFF_S", "0,0,0")


LOGIN_HTML = """
<html><body><form method="post" action="/Security/Login">
  <input id="Username" name="Username" type="text" value="" />
  <input id="Password" name="Password" type="password" />
  <input name="RequestVerificationToken" type="hidden" value="TOKEN-12345" />
  <button type="submit" name="bSignIn">Log in</button>
</form></body></html>
"""

REJECTED_HTML = """
<html><body><form method="post">
  <div class="bg-red-3 text-red-10">The username/password you specified is invalid.
     Passwords are case-sensitive.</div>
  <input name="RequestVerificationToken" type="hidden" value="TOKEN-12345" />
</form></body></html>
"""


def record(request_id: int, *, status_name: str = "Expired", status: int = 5, **extra: Any) -> dict:
    """One callrequests record, shaped like the live payload."""
    row = {
        "callRequestId": request_id,
        "providerName": "Agero (Swoop)",
        "requestDate": "2026-07-26T18:31:39.71",
        "requestDateUtc": "0001-01-01T00:00:00",
        "status": status,
        "statusName": status_name,
        "responseReasonName": "",
        "serviceNeeded": "Light Tow",
        "startingLocation": "1 Woodward Ave, Detroit MI",
        "towDestination": "500 Main St, Detroit MI",
        "offerAmount": 0.0,
        "ownerUserName": "",
        "vehicle": "2012 HONDA ODYSSEY EX red",
        "expirationDate": "2026-07-26T18:34:39",
        "companyId": 61343,
        # Populated on every live record, and mapped since 0005: the ZIP decides
        # territory, the distance is half of what prices the job, and
        # expirationDate above closes the decision window three minutes out.
        "zip": "43201",
        "distance": 4.3,
    }
    row.update(extra)
    return row


class Portal:
    """A scripted Towbook. Records every callrequests query it was asked."""

    def __init__(
        self,
        pages: list[list[dict]] | None = None,
        *,
        credentials_ok: bool = True,
        total_header: int | None = None,
        status_code: int = 200,
        omit_total_header: bool = False,
        rejected_html: str | None = None,
        company: str | None = None,
        switchable: tuple[str, ...] = (),
        switch_silently_fails: bool = False,
    ) -> None:
        self.pages = pages if pages is not None else [[]]
        self.credentials_ok = credentials_ok
        self.total_header = total_header
        self.status_code = status_code
        # Some real responses carry no X-Records-Count at all. Pagination then
        # has only the empty page to stop on, which is a different code path.
        self.omit_total_header = omit_total_header
        self.rejected_html = rejected_html
        # -- the company switcher (the book icon, top right) ------------------
        # `company` None means "this portal does not render the active-company
        # attribute at all", which is the shape every test written before the
        # switcher existed expects. Set it and the portal starts behaving like
        # the real multi-company account.
        self.company = company
        self.switchable = set(switchable)
        # The nastiest real failure mode: /change answers 200 and changes
        # nothing. Silence must not be read as success.
        self.switch_silently_fails = switch_silently_fails
        self.switches: list[str] = []
        self.queries: list[dict[str, str]] = []
        self.posted: dict[str, Any] | None = None

    def _page_html(self) -> str:
        """A portal page, carrying the active company the way the real one does."""
        if self.company is None:
            return "<html><body>Dispatching</body></html>"
        links = "".join(
            f'<li><a href="/change?c={other}&amp;returnUrl=%2F">Other</a></li>'
            for other in sorted(self.switchable - {self.company})
        )
        return (
            f'<html><body data-current-company-id="{self.company}">'
            f"<ul>{links}</ul>Dispatching</body></html>"
        )

    def __call__(self, request: "httpx.Request") -> "httpx.Response":
        path = request.url.path

        if path == "/Security/Login" and request.method == "GET":
            return httpx.Response(200, html=LOGIN_HTML)

        if path == "/Security/Login" and request.method == "POST":
            self.posted = dict(
                pair.split("=", 1)
                for pair in request.content.decode().split("&")
                if "=" in pair
            )
            if not self.credentials_ok:
                return httpx.Response(200, html=self.rejected_html or REJECTED_HTML)
            return httpx.Response(
                302,
                headers={
                    "Location": "https://app.towbook.com/",
                    "Set-Cookie": ".xtl=" + "s" * 40 + "; path=/; httponly",
                },
            )

        if path == "/change":
            wanted = dict(request.url.params).get("c", "")
            self.switches.append(wanted)
            if not self.switch_silently_fails and wanted in self.switchable:
                self.company = wanted
            return httpx.Response(302, headers={"Location": "https://app.towbook.com/"})

        if path in ("/", "/Index"):
            return httpx.Response(200, html=self._page_html())

        if path == "/api/digitaldispatch/callrequests":
            params = dict(request.url.params)
            self.queries.append(params)
            if self.status_code >= 400:
                return httpx.Response(self.status_code, text="server error")
            index = int(params.get("page", "1")) - 1
            batch = self.pages[index] if 0 <= index < len(self.pages) else []
            if self.omit_total_header:
                return httpx.Response(200, json=batch)
            total = (
                self.total_header
                if self.total_header is not None
                else sum(len(page) for page in self.pages)
            )
            return httpx.Response(
                200,
                json=batch,
                headers={"X-Records-Count": str(total)},
            )

        return httpx.Response(404, text="not found")


@pytest.fixture
def portal(api, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Portal]:
    """Install a scripted portal and return it. Nothing touches a socket."""

    def install(**kwargs: Any) -> Portal:
        scripted = Portal(**kwargs)

        def fake_client() -> "httpx.Client":
            return httpx.Client(
                base_url="https://app.towbook.com",
                follow_redirects=True,
                transport=httpx.MockTransport(scripted),
                headers={"User-Agent": "test"},
            )

        monkeypatch.setattr(api, "new_client", fake_client)
        return scripted

    return install


def archived_document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def archived_records(path: Path) -> list[dict]:
    document = archived_document(path)
    return [row for page in document["pages"] for row in page["records"]]


# --------------------------------------------------------------------------
# The antiforgery token
# --------------------------------------------------------------------------


def test_the_antiforgery_token_is_found(api) -> None:
    name, token = api.extract_antiforgery_token(LOGIN_HTML)
    assert name == "RequestVerificationToken"
    assert token == "TOKEN-12345"


def test_the_token_is_found_with_value_written_before_name(api) -> None:
    """Attribute order is not a contract. A positional regex would break here."""
    html = '<input type="hidden" value="ABC-999" name="RequestVerificationToken">'
    assert api.extract_antiforgery_token(html) == ("RequestVerificationToken", "ABC-999")


def test_a_page_with_no_token_reports_none_rather_than_guessing(api) -> None:
    assert api.extract_antiforgery_token("<html><body>nothing here</body></html>") == ("", "")


# --------------------------------------------------------------------------
# The page size cap
# --------------------------------------------------------------------------


def test_the_page_size_is_capped_even_when_the_environment_asks_for_more(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pageSize=2000 is not slow, it is HTTP 500 after a 30s timeout."""
    monkeypatch.setenv("TOWBOOK_API_PAGE_SIZE", "2000")
    assert api._page_size() == api.MAX_PAGE_SIZE == 1000


def test_the_page_size_is_capped_even_when_the_caller_asks_for_more(api, portal) -> None:
    scripted = portal(pages=[[record(1)]])
    api.acquire_api("2026-07-26", "2026-07-27", page_size=5000)
    assert scripted.queries[0]["pageSize"] == "1000"


# --------------------------------------------------------------------------
# Window conversion
# --------------------------------------------------------------------------


def test_a_daily_window_asks_for_that_one_calendar_day(api) -> None:
    """endDate is INCLUSIVE, and the pipeline's windows are half-open."""
    start = datetime(2026, 7, 26)
    end = datetime(2026, 7, 27)
    assert api._query_days(start, end) == (date(2026, 7, 26), date(2026, 7, 26))


def test_an_hourly_window_asks_for_the_day_that_contains_it(api) -> None:
    """There is no time-of-day filter, so an hour pulls its whole day."""
    start = datetime(2026, 7, 26, 14, 0)
    end = datetime(2026, 7, 26, 15, 0)
    assert api._query_days(start, end) == (date(2026, 7, 26), date(2026, 7, 26))


def test_a_multi_day_window_keeps_both_ends(api) -> None:
    start = datetime(2026, 7, 20)
    end = datetime(2026, 7, 27)
    assert api._query_days(start, end) == (date(2026, 7, 20), date(2026, 7, 26))


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


def test_login_succeeds_when_the_session_cookie_is_issued(api, portal) -> None:
    scripted = portal()
    with api.new_client() as client:
        assert api.login_api(client) is None
        assert any(cookie.name == ".xtl" for cookie in client.cookies.jar)
    assert scripted.posted is not None
    assert scripted.posted["RequestVerificationToken"] == "TOKEN-12345"
    assert scripted.posted["Username"]


def test_login_failure_is_named_bad_credentials_not_a_mystery(api, portal) -> None:
    portal(credentials_ok=False)
    with api.new_client() as client:
        with pytest.raises(api.LoginFailed) as caught:
            api.login_api(client)
    assert caught.value.outcome == "bad_credentials"
    assert "TOWBOOK_USER" in str(caught.value)


def test_login_check_probes_the_endpoint_not_just_the_cookie(api, portal) -> None:
    """A cookie is not data. A green login-check has to mean 'we can fetch'."""
    portal(pages=[[record(1)]])
    result = api.login_check_api()
    assert result["ok"] is True
    assert result["stage"] == "ok"
    assert result["details"]["callrequests_probe"]["ok"] is True
    assert ".xtl" in result["cookies_seen"]


def test_login_check_fails_when_the_endpoint_stops_answering_json(api, portal) -> None:
    portal(status_code=500)
    result = api.login_check_api()
    assert result["ok"] is False
    assert result["details"]["callrequests_probe"]["ok"] is False


def test_the_password_never_appears_in_the_login_check_result(api, portal) -> None:
    portal(pages=[[record(1)]])
    blob = json.dumps(api.login_check_api(), default=str)
    assert "not-a-real-password" not in blob


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def test_every_page_is_collected_and_pagination_stops(api, portal) -> None:
    pages = [
        [record(index) for index in range(1, 1001)],
        [record(index) for index in range(1001, 1101)],
    ]
    scripted = portal(pages=pages, total_header=1100)
    archived = api.acquire_api("2026-07-26", "2026-07-27")

    document = archived_document(archived)
    assert document["record_count"] == 1100
    assert document["distinct_ids"] == 1100
    assert document["duplicate_ids"] == 0
    assert document["x_records_count"] == 1100
    assert document["page_count"] == 2
    assert [query["page"] for query in scripted.queries] == ["1", "2"]


def test_an_empty_window_is_a_clean_zero_not_a_failure(api, portal) -> None:
    portal(pages=[[]], total_header=0)
    archived = api.acquire_api("2026-07-26", "2026-07-27")
    document = archived_document(archived)
    assert document["record_count"] == 0
    assert document["status"] == "ok"


def test_a_short_pull_alerts_rather_than_reporting_success(api, portal, captured_events) -> None:
    """The endpoint claims 500 rows and hands over 10. Silence would be worse."""
    portal(pages=[[record(index) for index in range(10)]], total_header=500)
    api.acquire_api("2026-07-26", "2026-07-27")
    failures = [event for event in captured_events.events if event[0] == "pipeline_failure"]
    assert failures, "a truncated pull must emit pipeline_failure"
    assert "TRUNCATED PULL" in failures[-1][1]["error"]


def test_the_max_pages_guard_stops_a_runaway(api, portal, monkeypatch, captured_events) -> None:
    monkeypatch.setenv("TOWBOOK_API_MAX_PAGES", "2")
    monkeypatch.setenv("TOWBOOK_API_PAGE_SIZE", "2")
    scripted = portal(
        pages=[[record(1), record(2)], [record(3), record(4)], [record(5), record(6)]],
        total_header=6,
    )
    api.acquire_api("2026-07-26", "2026-07-27")
    assert len(scripted.queries) == 2
    assert any(event[0] == "pipeline_failure" for event in captured_events.events)


# --------------------------------------------------------------------------
# The archive
# --------------------------------------------------------------------------


def test_the_archive_lands_under_the_window_start_date(api, portal) -> None:
    portal(pages=[[record(1)]])
    archived = api.acquire_api("2026-07-26", "2026-07-27")
    assert archived.parts[-4:-1] == ("2026", "07", "26")
    assert archived.name.startswith("run_")
    assert archived.suffix == ".json"


def test_the_archive_is_verbatim(api, portal) -> None:
    """A field we have no use for today is the one somebody needs next year."""
    portal(pages=[[record(1, someFutureField={"nested": [1, 2, 3]})]])
    archived = api.acquire_api("2026-07-26", "2026-07-27")
    stored = archived_records(archived)[0]
    assert stored["someFutureField"] == {"nested": [1, 2, 3]}
    assert stored["requestDateUtc"] == "0001-01-01T00:00:00"


def test_a_second_pull_never_overwrites_the_first(api, portal) -> None:
    portal(pages=[[record(1)]])
    first = api.acquire_api("2026-07-26", "2026-07-27", run_id="SAME")
    second = api.acquire_api("2026-07-26", "2026-07-27", run_id="SAME")
    assert first != second
    assert first.exists() and second.exists()


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_total_failure_alerts_and_raises(api, portal, captured_events) -> None:
    portal(credentials_ok=False)
    with pytest.raises(api.AcquisitionError):
        api.acquire_api("2026-07-26", "2026-07-27")
    failures = [event for event in captured_events.events if event[0] == "pipeline_failure"]
    assert failures
    assert failures[-1][1]["stage"] == "acquisition"
    assert failures[-1][1]["source"] == "api"


def test_a_rejected_password_is_not_retried(api, portal) -> None:
    """Four attempts against a wrong password is four lockout events."""
    scripted = portal(credentials_ok=False)
    with pytest.raises(api.AcquisitionError):
        api.acquire_api("2026-07-26", "2026-07-27")
    assert scripted.posted is not None
    assert scripted.queries == []


def test_an_end_before_the_start_is_rejected_outright(api, portal) -> None:
    portal()
    with pytest.raises(ValueError):
        api.acquire_api("2026-07-27", "2026-07-26")


# --------------------------------------------------------------------------
# The API archive through the real ingester
# --------------------------------------------------------------------------


def test_an_api_archive_ingests_and_keys_on_call_request_id(api, portal, ingestion) -> None:
    portal(pages=[[record(900001), record(900002, status_name="Accepted", status=1)]])
    archived = api.acquire_api("2026-07-26", "2026-07-27")

    result = ingestion.ingest(archived, "api-run-1")
    assert result.rows_read == 2
    assert result.rows_inserted == 2
    assert result.rows_rejected == 0
    # The whole reason this path exists: a real id, not a fingerprint.
    assert {row.request_id for row in all_requests()} == {"900001", "900002"}
    assert result.matched_headers["request_id"] == "callRequestId"
    assert result.matched_headers["offered_at"] == "requestDate"
    assert result.matched_headers["status"] == "statusName"


def test_the_zip_distance_and_expiry_are_stored(api, portal, ingestion) -> None:
    """All three arrive on every record and were discarded until 0005.

    The ZIP decides territory, so it is taken from the API's own field rather
    than parsed back out of the address; the distance is half of what prices a
    job; and expires_at minus offered_at is the decision window the owner
    actually gets, which on real data is a median of 2.8 minutes.
    """
    portal(pages=[[record(900003, zip="43015", distance=12.4)]])
    archived = api.acquire_api("2026-07-26", "2026-07-27")

    result = ingestion.ingest(archived, "api-run-zip")
    assert result.rows_rejected == 0

    row = all_requests()[0]
    assert row.pickup_zip == "43015"
    assert float(row.distance_miles) == 12.4
    # 18:31:39.71 offered -> 18:34:39 expiry, both company-local, stored UTC.
    window = (row.expires_at - row.offered_at).total_seconds()
    assert 175 <= window <= 185, f"expected a ~3 min decision window, got {window}s"


def test_a_record_without_a_zip_still_ingests(api, portal, ingestion) -> None:
    """The CSV export carries no ZIP at all, and 2 of 3,124 API records lacked one.

    A missing ZIP must leave the row usable and the field NULL -- territory is
    then simply unknown for it, which is honest. Rejecting the row would
    understate the offered count, which is the number this system exists to
    get right.
    """
    payload = record(900004)
    del payload["zip"]
    portal(pages=[[payload]])

    result = ingestion.ingest(api.acquire_api("2026-07-26", "2026-07-27"), "api-run-nozip")

    assert result.rows_rejected == 0
    assert result.rows_inserted == 1
    assert all_requests()[0].pickup_zip is None


def test_re_ingesting_the_same_window_is_idempotent(api, portal, ingestion) -> None:
    portal(pages=[[record(900001), record(900002)]])
    first = api.acquire_api("2026-07-26", "2026-07-27", run_id="ONE")
    second = api.acquire_api("2026-07-26", "2026-07-27", run_id="TWO")

    ingestion.ingest(first, "api-run-1")
    repeat = ingestion.ingest(second, "api-run-2")

    assert repeat.rows_inserted == 0
    assert repeat.rows_updated == 2
    assert count_rows(Request) == 2


def test_the_status_codes_that_are_lost_work_are_not_counted_as_declines(
    api, portal, ingestion
) -> None:
    """21 and 80 are jobs we LOST, not jobs we refused. Conflating them would
    inflate the denial rate and point the owner at the wrong problem."""
    portal(
        pages=[
            [
                record(1, status_name="Accept Failed", status=21),
                record(2, status_name="Another Provider Responded", status=80),
                record(3, status_name="Rejected", status=2),
            ]
        ]
    )
    archived = api.acquire_api("2026-07-26", "2026-07-27")
    ingestion.ingest(archived, "api-run-1")

    stored = {row.request_id: row.status for row in all_requests()}
    assert stored["1"] == "expired"
    assert stored["2"] == "expired"
    assert stored["3"] == "denied"


def test_a_payload_without_call_request_id_aborts(api, ingestion, tmp_path: Path) -> None:
    """No id means no idempotency. Fingerprinting our way out of it here would
    hide a real change in the endpoint."""
    payload = tmp_path / "run_bad.json"
    payload.write_text(
        json.dumps(
            [{"providerName": "Agero", "requestDate": "2026-07-26T10:00:00", "status": 1}]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ingestion.HeaderValidationError) as caught:
        ingestion.ingest(payload, "api-run-bad")
    assert "NOTHING WAS INGESTED" in str(caught.value)


def test_a_bare_json_array_ingests_too(api, ingestion, tmp_path: Path) -> None:
    """A response saved by hand with curl must not need repackaging."""
    payload = tmp_path / "run_bare.json"
    payload.write_text(json.dumps([record(1), record(2)]), encoding="utf-8")
    result = ingestion.ingest(payload, "api-run-bare")
    assert result.rows_read == 2
    assert result.rows_inserted == 2


def test_the_numeric_status_codes_cover_a_payload_with_no_status_name(
    ingestion, tmp_path: Path
) -> None:
    payload = tmp_path / "run_numeric.json"
    rows = [
        {"callRequestId": 1, "requestDate": "2026-07-26T10:00:00", "status": 1,
         "serviceNeeded": "Tow", "providerName": "Agero"},
        {"callRequestId": 2, "requestDate": "2026-07-26T11:00:00", "status": 40,
         "serviceNeeded": "Tow", "providerName": "Agero"},
    ]
    payload.write_text(json.dumps(rows), encoding="utf-8")
    result = ingestion.ingest(payload, "api-run-numeric")

    assert result.matched_headers["status"] == "status"
    assert result.unmapped_statuses == {}
    assert {row.request_id: row.status for row in all_requests()} == {
        "1": "accepted",
        "2": "canceled",
    }


def test_the_numeric_code_is_stored_even_when_the_label_won_the_mapping(
    api, portal, ingestion
) -> None:
    """Both halves of the status survive, not whichever one the mapping picked.

    ``schema.yaml -> api.columns.status`` is ``[statusName, status]``: the label
    wins, and for a long time the numeric code beside it was simply dropped.
    Measured on a real 2,932-record pull, ``status_code`` was NULL on 2,932 of
    2,932 rows -- so ``rules.yaml -> missed_work.buckets``, a 14-entry table
    keyed on that integer and documented as the primary bucketing route, never
    fired once. Every row fell through to the label map instead.

    The buckets came out right, because the labels are exact. The danger is the
    failure mode when they stop being: an unrecognised label does not raise, it
    falls through to the coarse canonical map, where "Accept Failed" quietly
    becomes ``no_response`` instead of ``accept_failed``.
    """
    portal(
        pages=[
            [
                record(1, status_name="Accepted", status=1),
                record(2, status_name="Accept Failed", status=21),
                record(3, status_name="Another Provider Responded", status=80),
                record(4, status_name="Rejected By Motor Club", status=40),
            ]
        ]
    )
    archived = api.acquire_api("2026-07-26", "2026-07-27")
    ingestion.ingest(archived, "api-run-codes")

    stored = {row.request_id: (row.status_raw, row.status_code) for row in all_requests()}
    assert stored == {
        "1": ("Accepted", 1),
        "2": ("Accept Failed", 21),
        "3": ("Another Provider Responded", 80),
        "4": ("Rejected By Motor Club", 40),
    }, f"the numeric code was dropped where the label won the mapping: {stored!r}"


def test_the_numeric_code_is_what_actually_buckets_a_real_row(
    api, portal, ingestion
) -> None:
    """The whole point of storing the code: missed_work buckets ON it.

    ``bucket_sources`` names which of the three maps answered. If this says
    ``status_name`` the code is not reaching the model, whatever the column
    contains.
    """
    missed_work = load_agent("missed_work")
    portal(pages=[[record(1, status_name="Accept Failed", status=21)]])
    ingestion.ingest(api.acquire_api("2026-07-26", "2026-07-27"), "api-run-bucket")

    row = all_requests()[0]
    assert missed_work.classify_bucket(row) == "accept_failed"

    # And not by accident: the canonical status alone cannot tell 21 from 5.
    assert row.status == "expired"
    assert missed_work.classify_bucket({"status": "expired"}) == "no_response"


def test_service_type_raw_survives_the_json_path_verbatim(api, portal, ingestion) -> None:
    portal(pages=[[record(1, serviceNeeded="Light Duty  Unleaded Fuel Delivery")]])
    archived = api.acquire_api("2026-07-26", "2026-07-27")
    ingestion.ingest(archived, "api-run-verbatim")
    assert all_requests()[0].service_type_raw == "Light Duty  Unleaded Fuel Delivery"


# --------------------------------------------------------------------------
# The realistic rejected-login page
#
# TOWBOOK_PORTAL_FACTS.md section 1 gives the verified failure test: still on
# /Security/Login after the POST, with `field-validation-error` / a populated
# `data-valmsg-for` span in the body. REJECTED_HTML above uses the styled
# banner; this is the ASP.NET unobtrusive-validation markup the same page
# emits, which is the marker listed FIRST in selectors.yaml and therefore the
# one an operator will see quoted back at them in the error message.
# --------------------------------------------------------------------------

FIELD_VALIDATION_HTML = """
<html><body>
  <form method="post" action="/Security/Login" novalidate="novalidate">
    <div class="validation-summary-errors" data-valmsg-summary="true">
      <ul><li>The username/password you specified is invalid.</li></ul>
    </div>
    <input id="Username" name="Username" type="text" value="someone@example.invalid" />
    <span class="field-validation-error" data-valmsg-for="Username"
          data-valmsg-replace="true">Invalid username or password</span>
    <input id="Password" name="Password" type="password" />
    <span class="field-validation-error" data-valmsg-for="Password"
          data-valmsg-replace="true">Passwords are case-sensitive.</span>
    <input name="RequestVerificationToken" type="hidden" value="TOKEN-12345" />
    <button type="submit" name="bSignIn">Log in</button>
  </form>
</body></html>
"""


def test_a_field_validation_error_is_read_as_rejected_credentials(api, portal) -> None:
    """The verified failure signature, not a guess at one."""
    portal(credentials_ok=False, rejected_html=FIELD_VALIDATION_HTML)
    with api.new_client() as client:
        with pytest.raises(api.LoginFailed) as caught:
            api.login_api(client)

    assert caught.value.outcome == "bad_credentials"
    # The marker is quoted back, so an operator can see WHY it was called a
    # rejection rather than being told "login failed" and left guessing.
    assert "field-validation-error" in str(caught.value)


def test_the_markers_that_fired_are_recorded_on_the_login_check(api, portal) -> None:
    portal(credentials_ok=False, rejected_html=FIELD_VALIDATION_HTML)
    result = api.login_check_api()

    assert result["ok"] is False
    assert result["outcome"] == "bad_credentials"
    markers = result["details"]["login_failure_markers"]
    assert "field-validation-error" in markers
    # No session cookie was issued and we never left the login page. Both halves
    # of the verified success test are recorded as false.
    assert result["details"]["session_cookie_present"] is False
    assert "/Security/Login" in result["details"]["url_after_post"]


def test_a_rejection_is_never_read_as_success_just_because_a_cookie_appeared(
    api, portal
) -> None:
    """The success test is the .xtl cookie AND being off the login page.

    A login page that happens to set a cookie -- an antiforgery cookie always
    is one -- is still a failure. Testing either half alone is how this path was
    originally got wrong.
    """
    html = FIELD_VALIDATION_HTML

    def scripted(request: "httpx.Request") -> "httpx.Response":
        if request.url.path == "/Security/Login" and request.method == "POST":
            return httpx.Response(
                200,
                html=html,
                headers={"Set-Cookie": ".xtl=" + "s" * 40 + "; path=/; httponly"},
            )
        return httpx.Response(200, html=LOGIN_HTML)

    def fake_client() -> "httpx.Client":
        return httpx.Client(
            base_url="https://app.towbook.com",
            follow_redirects=True,
            transport=httpx.MockTransport(scripted),
        )

    import towbook_agent.agents.acquisition_api as module

    original, module.new_client = module.new_client, fake_client
    try:
        with api.new_client() as client:
            with pytest.raises(api.LoginFailed) as caught:
                api.login_api(client)
            assert any(cookie.name == ".xtl" for cookie in client.cookies.jar)
    finally:
        module.new_client = original

    assert caught.value.outcome == "bad_credentials"


# --------------------------------------------------------------------------
# Pagination termination, the other way
# --------------------------------------------------------------------------


def test_pagination_stops_on_an_empty_page_when_there_is_no_count_header(
    api, portal, monkeypatch, captured_events
) -> None:
    """X-Records-Count is the primary stop condition; the empty page is the
    backstop, and it has to work on its own.

    Without the header there is no total to compare against, so a full page can
    only mean "there may be more". The loop must ask for the next page, get
    ``[]``, and stop -- cleanly, with no truncation alert, because nothing was
    actually missed.
    """
    monkeypatch.setenv("TOWBOOK_API_PAGE_SIZE", "2")
    scripted = portal(
        pages=[[record(1), record(2)], []],
        omit_total_header=True,
    )
    archived = api.acquire_api("2026-07-26", "2026-07-27")

    assert [query["page"] for query in scripted.queries] == ["1", "2"]

    document = archived_document(archived)
    assert document["record_count"] == 2
    assert document["page_count"] == 1, "the empty page is a stop signal, not a page"
    assert document["x_records_count"] is None
    assert document["status"] == "ok"
    assert not [e for e in captured_events.events if e[0] == "pipeline_failure"]


def test_a_short_page_ends_the_pull_without_asking_for_another(
    api, portal, monkeypatch
) -> None:
    """An endpoint that fills every page it can means a short page is the last."""
    monkeypatch.setenv("TOWBOOK_API_PAGE_SIZE", "10")
    scripted = portal(
        pages=[[record(index) for index in range(3)]],
        omit_total_header=True,
    )
    api.acquire_api("2026-07-26", "2026-07-27")
    assert [query["page"] for query in scripted.queries] == ["1"]


def test_the_count_header_stops_the_pull_on_an_exact_page_boundary(
    api, portal, monkeypatch
) -> None:
    """20 records at pageSize 10 is two full pages. Without the header the loop
    would ask for a third; with it, it stops knowing it has everything."""
    monkeypatch.setenv("TOWBOOK_API_PAGE_SIZE", "10")
    scripted = portal(
        pages=[
            [record(index) for index in range(10)],
            [record(index) for index in range(10, 20)],
        ],
        total_header=20,
    )
    archived = api.acquire_api("2026-07-26", "2026-07-27")

    assert [query["page"] for query in scripted.queries] == ["1", "2"]
    assert archived_document(archived)["record_count"] == 20


# --------------------------------------------------------------------------
# HTTP 500
# --------------------------------------------------------------------------


def test_a_500_is_retried_and_then_raises_pipeline_failure(
    api, portal, captured_events
) -> None:
    """A server error is transient until proven otherwise, so it is retried --
    and then it MUST alert. Silence after a failed pull is the one outcome this
    system exists to prevent: the report would simply show a quiet day.
    """
    scripted = portal(status_code=500)

    with pytest.raises(api.AcquisitionError) as caught:
        api.acquire_api("2026-07-26", "2026-07-27")

    # TOWBOOK_RETRY_BACKOFF_S is "0,0,0" in this module, so four attempts.
    assert len(scripted.queries) == 4, "every attempt must actually reach the endpoint"

    failures = [event for event in captured_events.events if event[0] == "pipeline_failure"]
    assert failures, "a total acquisition failure must emit pipeline_failure"
    payload = failures[-1][1]
    assert payload["stage"] == "acquisition"
    assert payload["source"] == "api"
    assert payload["severity"] == "high"
    assert payload["attempts"] == 4
    assert len(payload["all_errors"]) == 4
    # The alert names the actual HTTP status rather than "something went wrong".
    assert "500" in payload["error"]
    assert "500" in str(caught.value)
    assert payload["next_step"]


def test_a_500_on_a_later_page_still_alerts_rather_than_archiving_a_partial(
    api, portal, captured_events
) -> None:
    """Failing halfway through is the dangerous case: 1000 rows in hand look
    like a successful small day."""
    scripted = portal(pages=[[record(index) for index in range(1, 1001)]])
    api.acquire_api("2026-07-26", "2026-07-27")
    assert not [e for e in captured_events.events if e[0] == "pipeline_failure"]

    scripted.status_code = 500
    with pytest.raises(api.AcquisitionError):
        api.acquire_api("2026-07-26", "2026-07-27", run_id="SECOND")

    failures = [e for e in captured_events.events if e[0] == "pipeline_failure"]
    assert failures
    assert failures[-1][1]["run_id"] == "SECOND"


def test_a_retry_that_succeeds_produces_no_alert(api, portal, captured_events) -> None:
    """The other half of the retry contract: a transient blip must not page
    anyone. An alert on every recovered 500 is an alert nobody reads."""
    scripted = portal(pages=[[record(1)]], status_code=500)

    calls = {"n": 0}
    inner = scripted.__call__

    def flaky(request: "httpx.Request") -> "httpx.Response":
        if request.url.path == "/api/digitaldispatch/callrequests":
            calls["n"] += 1
            if calls["n"] == 1:
                return inner(request)
            scripted.status_code = 200
        return inner(request)

    def fake_client() -> "httpx.Client":
        return httpx.Client(
            base_url="https://app.towbook.com",
            follow_redirects=True,
            transport=httpx.MockTransport(flaky),
        )

    import towbook_agent.agents.acquisition_api as module

    original, module.new_client = module.new_client, fake_client
    try:
        archived = api.acquire_api("2026-07-26", "2026-07-27")
    finally:
        module.new_client = original

    assert archived_document(archived)["record_count"] == 1
    assert archived_document(archived)["attempt"] == 2, "it took the second attempt"
    assert not [e for e in captured_events.events if e[0] == "pipeline_failure"]


# --------------------------------------------------------------------------
# Credentials never reach a log record
#
# Hard constraint: credentials come from the environment and are never logged.
# The archive is scrubbed by `redact` and the login-check result is asserted
# clean elsewhere; this closes the third and least visible channel. Logs are
# the one output that gets pasted into a chat window when something breaks.
# --------------------------------------------------------------------------

USERNAME = "test-user@example.invalid"   # conftest._ENV_DEFAULTS
PASSWORD = "not-a-real-password"         # conftest._ENV_DEFAULTS


def _log_text(caplog) -> str:
    """Every log record, message and raw args, as one blob.

    Both are needed: a lazily-formatted record (``logger.info("user %s", name)``)
    keeps the value in ``args``, so asserting only on the formatted message
    would miss a credential that a handler will happily render later.
    """
    parts: list[str] = []
    for entry in caplog.records:
        parts.append(str(entry.getMessage()))
        parts.append(str(entry.msg))
        parts.append(repr(entry.args))
    return "\n".join(parts)


def test_no_credential_reaches_a_log_record_during_a_successful_pull(
    api, portal, caplog
) -> None:
    import logging

    portal(pages=[[record(1)]])
    with caplog.at_level(logging.DEBUG):
        api.acquire_api("2026-07-26", "2026-07-27")

    blob = _log_text(caplog)
    assert caplog.records, "nothing was logged, so this test would pass vacuously"
    assert PASSWORD not in blob
    assert USERNAME not in blob
    # Proof the username was handled at all, and handled the intended way.
    assert "te******r@example.invalid" in blob


def test_no_credential_reaches_a_log_record_when_the_login_is_rejected(
    api, portal, caplog
) -> None:
    """The failure path logs the most, and is the path an operator will be
    reading when they paste the output somewhere."""
    import logging

    portal(credentials_ok=False, rejected_html=FIELD_VALIDATION_HTML)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(api.AcquisitionError):
            api.acquire_api("2026-07-26", "2026-07-27")

    blob = _log_text(caplog)
    assert caplog.records
    assert PASSWORD not in blob
    assert USERNAME not in blob


def test_no_credential_reaches_a_log_record_during_login_check(
    api, portal, caplog
) -> None:
    import logging

    portal(pages=[[record(1)]])
    with caplog.at_level(logging.DEBUG):
        api.login_check_api()

    blob = _log_text(caplog)
    assert PASSWORD not in blob
    assert USERNAME not in blob


def test_no_credential_reaches_a_log_record_when_the_endpoint_fails(
    api, portal, caplog, captured_events
) -> None:
    import logging

    portal(status_code=500)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(api.AcquisitionError):
            api.acquire_api("2026-07-26", "2026-07-27")

    blob = _log_text(caplog)
    assert caplog.records
    assert PASSWORD not in blob
    assert USERNAME not in blob

    # ...and not in the alert payload either, which is emailed and texted.
    failures = [e for e in captured_events.events if e[0] == "pipeline_failure"]
    payload = json.dumps(failures[-1][1], default=str)
    assert PASSWORD not in payload
    assert USERNAME not in payload


def test_the_password_is_posted_but_never_written_to_the_archive(
    api, portal, caplog
) -> None:
    """It has to go on the wire -- that is what a login is. It must not survive
    anywhere on disk afterwards."""
    import logging

    scripted = portal(pages=[[record(1)]])
    with caplog.at_level(logging.DEBUG):
        archived = api.acquire_api("2026-07-26", "2026-07-27")

    assert scripted.posted is not None
    assert scripted.posted["Password"], "the password really was sent"

    contents = archived.read_text(encoding="utf-8")
    assert PASSWORD not in contents
    assert USERNAME not in contents


# ==========================================================================
# ONE LOGIN, SEVERAL COMPANIES
#
# Towbook scopes /api/digitaldispatch/callrequests to whatever company the
# SESSION is on -- the book icon, top right of the portal. There is no
# companyId parameter. A login lands on the user's home company and stays
# there, so an account carrying two towing entities silently reports on one.
#
# That is not hypothetical. On this install every pull ever made ran as
# Roadside Towing and Recovery Inc (61343) while all the HONK work was being
# assigned through Auto Lyft USA, Inc (254467), so HONK was absent from every
# report -- not mis-parsed, never asked for.
#
# The failure these tests exist to make impossible is the OTHER one, though.
# A run that claims to be about company B, fails to switch, pulls company A's
# rows and files them under B produces a report that is entirely plausible and
# entirely wrong, forever, with no error anywhere. So: the switch is confirmed
# from the portal's own attribute, the rows are confirmed against it, and
# either disagreement aborts the run before a single row is archived.
# ==========================================================================


ROADSIDE, AUTOLYFT = "61343", "254467"


@pytest.fixture
def two_companies(write_config):
    """A roster shaped like the real account: two entities, one login."""
    write_config(
        "companies",
        {
            "version": 1,
            "default_company": "default",
            "companies": [
                {
                    "id": "default",
                    "name": "Roadside Towing and Recovery Inc",
                    "towbook_company_id": int(ROADSIDE),
                    "credentials_env": "",
                    "enabled": True,
                },
                {
                    "id": "auto-lyft",
                    "name": "Auto Lyft USA, Inc",
                    "towbook_company_id": int(AUTOLYFT),
                    "credentials_env": "",
                    "enabled": True,
                },
            ],
        },
    )
    from towbook_agent.core import companies as companies_module

    companies_module.reload_companies()
    yield
    companies_module.reload_companies()


def test_the_session_is_switched_to_the_company_before_anything_is_fetched(
    api, portal, two_companies
) -> None:
    """The whole bug in one assertion.

    The login lands on Roadside. A run for auto-lyft must move the session to
    254467 BEFORE the first callrequests query, or it pulls Roadside's offers
    and files them under Auto Lyft.
    """
    scripted = portal(
        pages=[[record(1, companyId=int(AUTOLYFT), providerName="Honk")]],
        company=ROADSIDE,
        switchable=(ROADSIDE, AUTOLYFT),
    )

    archived = api.acquire_api("2026-07-26", "2026-07-27", company_id="auto-lyft")

    assert scripted.switches == [AUTOLYFT], "the session was switched exactly once, to Auto Lyft"
    assert scripted.company == AUTOLYFT
    assert scripted.queries, "and only then was any data asked for"

    document = json.loads(archived.read_text(encoding="utf-8"))
    assert document["company_id"] == "auto-lyft"
    assert document["towbook_company_id"] == AUTOLYFT
    assert document["company_selection"]["before"] == ROADSIDE
    assert document["company_selection"]["after"] == AUTOLYFT
    assert document["company_selection"]["confirmed"] is True


def test_the_scheduler_spelling_of_the_company_argument_is_honoured(
    api, portal, two_companies
) -> None:
    """Regression: ``scheduler._call`` drops keyword arguments the callee does
    not declare. ``acquire_api`` took ``account_id``, the scheduler passed
    ``company_id=``, and so the company was silently discarded on every single
    run. Both spellings must reach the switch."""
    scripted = portal(company=ROADSIDE, switchable=(ROADSIDE, AUTOLYFT))
    api.acquire_api("2026-07-26", "2026-07-27", company_id="auto-lyft")
    assert scripted.switches == [AUTOLYFT]

    positional = portal(company=ROADSIDE, switchable=(ROADSIDE, AUTOLYFT))
    api.acquire_api("2026-07-26", "2026-07-27", "auto-lyft")
    assert positional.switches == [AUTOLYFT], "the legacy positional name still works"


def test_a_switch_that_silently_does_not_take_aborts_the_run(
    api, portal, two_companies, captured_events
) -> None:
    """/change answering 200 and changing nothing is the dangerous case: the
    run would continue and file Roadside's rows under Auto Lyft. It must not
    archive anything at all."""
    scripted = portal(
        pages=[[record(1)]],
        company=ROADSIDE,
        switchable=(ROADSIDE, AUTOLYFT),
        switch_silently_fails=True,
    )

    # RAW_DIR is the whole session's sandbox, not this test's, so "nothing was
    # archived" has to mean "nothing NEW" -- every archive an earlier test
    # legitimately wrote is still sitting there.
    from towbook_agent.core.paths import RAW_DIR

    before = set(RAW_DIR.glob("**/*.json"))

    with pytest.raises(api.AcquisitionError) as caught:
        api.acquire_api("2026-07-26", "2026-07-27", company_id="auto-lyft")

    assert AUTOLYFT in str(caught.value)
    assert scripted.queries == [], "not one row was fetched after the failed switch"
    assert set(RAW_DIR.glob("**/*.json")) == before, "and nothing was archived"


def test_a_failed_switch_is_not_retried(api, portal, two_companies) -> None:
    """A company mismatch is configuration or portal behaviour, never a blip.
    Retrying asks the wrong question three more times -- and the one outcome
    that must never happen is a retry that 'succeeds' on another tenant."""
    scripted = portal(
        company=ROADSIDE, switchable=(ROADSIDE, AUTOLYFT), switch_silently_fails=True
    )
    with pytest.raises(api.AcquisitionError):
        api.acquire_api("2026-07-26", "2026-07-27", company_id="auto-lyft")

    assert len(scripted.switches) == 1, "one attempt, not four"


def test_rows_carrying_another_companys_id_abort_the_run(
    api, portal, two_companies
) -> None:
    """Belt and braces. The switch is confirmed from the portal's <body>
    attribute; the payload is then confirmed against the rows themselves.
    config/schema.yaml has declared `account_id: [companyId]` as the source of
    record for exactly this check since before there was a second company."""
    portal(
        # The session says Auto Lyft. The rows say Roadside. Somebody is wrong
        # and there is no safe way to guess which.
        pages=[[record(1, companyId=int(ROADSIDE))]],
        company=ROADSIDE,
        switchable=(ROADSIDE, AUTOLYFT),
    )

    with pytest.raises(api.AcquisitionError) as caught:
        api.acquire_api("2026-07-26", "2026-07-27", company_id="auto-lyft")

    message = str(caught.value)
    assert ROADSIDE in message and AUTOLYFT in message


def test_a_single_company_install_is_left_exactly_where_the_login_landed(
    api, portal
) -> None:
    """No roster, no towbook_company_id, nothing to switch to. The switcher
    must stay entirely out of the way -- this is the install the system was
    built on and it must not acquire a new failure mode."""
    scripted = portal(pages=[[record(1)]])
    api.acquire_api("2026-07-26", "2026-07-27")
    assert scripted.switches == [], "nothing was switched"
    assert scripted.queries, "and the pull happened anyway"


def test_a_session_already_on_the_right_company_is_not_switched(
    api, portal, two_companies
) -> None:
    """The home company costs no extra round trip."""
    scripted = portal(pages=[[record(1)]], company=ROADSIDE, switchable=(ROADSIDE, AUTOLYFT))
    api.acquire_api("2026-07-26", "2026-07-27", company_id="default")
    assert scripted.switches == [], "already there"
    assert scripted.company == ROADSIDE
