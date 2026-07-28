"""The dashboard, end to end, offline.

Two things are checked here and they are both about the same failure mode: a
page that is fine with data and explodes without it, or the reverse.

1. **Every GET route returns 200** -- against an empty datastore *and* against
   the full fixture. Routes are discovered from the app itself rather than
   listed, so a new view added without a test still gets one, and a view that
   is accidentally removed fails loudly instead of quietly.

2. **The missed-work views say what they are.** The inventory is the front
   page now; the rules that make it trustworthy -- a rate of ``None`` renders
   as an em-dash and never ``0%``, and every ranked list states that it counts
   jobs and not money -- are asserted, not assumed.

``TestClient`` speaks ASGI in-process, so the ``no_network`` guard in conftest
is untouched. Nothing here starts a server.
"""

from __future__ import annotations

import socket
from datetime import date
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from conftest import ingest_file
from fixture_generator import generate_fixture_xlsx
from towbook_agent.core.db import get_session
from towbook_agent.web.app import DASH, app, f_pct
from towbook_agent.web.queries import RANKING_NOTE

#: Captured at import, before conftest's ``no_network`` fixture replaces them.
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex

#: Addresses that are the machine talking to itself.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", ""})


def _is_loopback(address: object) -> bool:
    if isinstance(address, (tuple, list)) and address:
        return str(address[0]) in _LOOPBACK
    return False


@pytest.fixture(autouse=True)
def allow_loopback(monkeypatch: pytest.MonkeyPatch, no_network: None) -> None:
    """Narrow the suite's offline guard to "loopback only" for this module.

    ``TestClient`` runs the ASGI app on an anyio portal, and asyncio's Windows
    proactor loop builds its internal self-pipe with :func:`socket.socketpair`,
    which connects to 127.0.0.1. That is the process talking to itself, not a
    request leaving the machine, and conftest's guard cannot tell the two
    apart -- it sees a ``connect`` and refuses.

    So the guard is not lifted, only narrowed: a connection to anything that is
    not loopback still raises exactly as before. The dashboard is read-only
    against SQLite and makes no outbound call, so nothing legitimate in this
    module needs more than this.

    Depends on ``no_network`` explicitly so it is always the later of the two
    and its patch is the one that survives.
    """
    from conftest import NetworkAccessAttempted

    def guarded(original):
        def call(self, address, *args, **kwargs):
            if not _is_loopback(address):
                raise NetworkAccessAttempted(
                    f"a test tried to reach {address!r}; the suite is offline by design"
                )
            return original(self, address, *args, **kwargs)

        return call

    monkeypatch.setattr(socket.socket, "connect", guarded(_REAL_CONNECT), raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(_REAL_CONNECT_EX), raising=False)

#: The same fixed window test_e2e uses, so the two suites describe one dataset.
END_DATE = date(2026, 7, 26)
DAYS = 14
SEED = 42

#: Values substituted into path parameters when a route is exercised generically.
PATH_PARAMS = {
    "slug": "agero",
    "proposal_id": "does-not-exist",
}

#: Routes that are not a page and are exercised elsewhere. ``/healthz`` is in
#: the generic sweep; nothing is excluded for being inconvenient.
SKIP_PATHS: frozenset[str] = frozenset()


def _get_routes() -> list[str]:
    """Every GET path the app serves, with path parameters filled in."""
    paths: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in (route.methods or set()):
            continue
        path = route.path
        if path in SKIP_PATHS:
            continue
        for name, value in PATH_PARAMS.items():
            path = path.replace("{%s:path}" % name, value).replace("{%s}" % name, value)
        if "{" in path:  # a parameter nobody taught this test about
            raise AssertionError(f"no test value for a path parameter in {route.path}")
        paths.append(path)
    return sorted(set(paths))


ROUTES = _get_routes()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def seeded(ingestion, classifier, tmp_path: Path) -> None:
    """The bundled fixture, ingested and classified. Same data as test_e2e."""
    path = generate_fixture_xlsx(
        tmp_path / "digital_requests.xlsx", days=DAYS, seed=SEED, end_date=END_DATE
    )
    _, error = ingest_file(ingestion, path, run_id="web-test-run")
    assert error is None, f"the fixture failed to ingest: {error!r}"
    with get_session() as session:
        classifier.backfill(session)


# --------------------------------------------------------------------------
# Every route, both states
# --------------------------------------------------------------------------


def test_the_route_table_is_not_empty() -> None:
    """A discovery bug that found nothing would make every sweep below pass."""
    assert len(ROUTES) >= 20, ROUTES
    for expected in ("/", "/live", "/blind-spots", "/close-off", "/clients", "/daily"):
        assert expected in ROUTES, f"{expected} is not served; ROUTES={ROUTES}"


@pytest.mark.parametrize("path", ROUTES)
def test_every_route_renders_on_an_empty_datastore(client: TestClient, path: str) -> None:
    """The empty state is the state a new install is in. It must not 500."""
    response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"


@pytest.mark.parametrize("path", ROUTES)
def test_every_route_renders_with_data(client: TestClient, seeded, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"


def test_the_six_original_views_still_work(client: TestClient, seeded) -> None:
    """Demoting Live to /live must not have cost anything."""
    for path, marker in (
        ("/live", "Live"),
        # An explicit date: /daily defaults to yesterday, and the fixture ends
        # before that, so the default view is legitimately the empty state.
        (f"/daily?date={END_DATE}", "Offers by hour"),
        ("/clients", "Acceptance by client"),
        ("/trends", "Hour of week"),
        ("/rules", "rules.yaml"),
        ("/health", "Health"),
        ("/partials/live", "stats"),
        (f"/partials/daily?date={END_DATE}", "Policy variance"),
        ("/partials/clients", "client-table"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text, f"{path} lost {marker!r}"


def test_an_unknown_path_renders_the_404_page(client: TestClient) -> None:
    response = client.get("/no-such-view")
    assert response.status_code == 404
    assert "Not found" in response.text


# --------------------------------------------------------------------------
# The missed-work views
# --------------------------------------------------------------------------


def test_the_front_page_is_missed_work(client: TestClient, seeded) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Missed work" in body
    assert "Recoverable" in body
    # The four buckets, by their labels, as large numbers.
    for label in ("Accepted", "Never responded", "Declined", "Client withdrew"):
        assert label in body, f"the headline is missing {label!r}"


def test_every_ranked_view_states_that_it_counts_jobs(client: TestClient, seeded) -> None:
    """The single most important sentence in the UI.

    ``offerAmount`` is empty on 100% of this account's records, so a table of
    counts that somebody reads as a table of dollars is the one way this
    dashboard could actively mislead. Every page with a ranked list says so.
    """
    for path in ("/", "/blind-spots", "/close-off", "/clients", "/partials/clients"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert RANKING_NOTE in response.text, f"{path} does not state its ranking basis"


def test_no_view_shows_a_dollar_figure(client: TestClient, seeded) -> None:
    """No screen may imply revenue while offer amounts are unpopulated."""
    for path in ("/", "/blind-spots", "/close-off"):
        body = client.get(path).text
        assert "$" not in body, f"{path} renders a dollar sign"


def test_the_front_page_compares_covered_and_uncovered_hours(
    client: TestClient, seeded
) -> None:
    """The coverage comparison is the argument, and it sits above the fold.

    Both rows must be present. Outside-hours missed work is the headline
    because that is where essentially all of it sits, but a 61.7% miss rate
    only means something next to the 5.8% one -- printing the bad number alone
    turns evidence back into an assertion.
    """
    body = client.get("/").text

    assert "Covered hours vs uncovered hours" in body
    assert "Outside covered hours" in body
    assert "Inside covered hours" in body
    assert "Recoverable outside covered hours" in body

    # Above the fold: before the inventory, and so before the blind-spot
    # summary and everything else below it. ("Blind spots" is not a usable
    # marker here -- it is also a nav link, which sits above everything.)
    assert body.index("Covered hours vs uncovered hours") < body.index("The inventory")


def test_blind_spots_leads_with_the_response_window(client: TestClient, seeded) -> None:
    response = client.get("/blind-spots")
    body = response.text
    assert "Response window Towbook allows" in body
    assert "min median" in body
    # Stated as measured, never implied to be recomputed from these rows.
    assert "Towbook publishes no response timestamp" in body


def test_close_off_groups_by_client_with_a_paste_ready_note(
    client: TestClient, seeded
) -> None:
    response = client.get("/close-off")
    assert response.status_code == 200
    body = response.text
    assert "One conversation per client" in body or "Close-off candidates" in body
    if "copyblock" in body:
        # The note must be about jobs, and must not have picked up a currency
        # figure on the way out of the building.
        assert "job counts, not dollar figures" in body


def test_clients_carries_no_response_as_a_first_class_column(
    client: TestClient, seeded
) -> None:
    body = client.get("/clients").text
    assert "No response" in body
    assert "Unanswered" in body


def test_the_client_table_sorts_by_no_response_without_raising(
    client: TestClient, seeded
) -> None:
    for sort in ("miss", "no_response", "volume", "rate", "not-a-column"):
        response = client.get(f"/partials/clients?sort={sort}&dir=desc")
        assert response.status_code == 200, sort


def test_a_sort_key_the_daily_table_lacks_degrades_instead_of_raising(
    client: TestClient, seeded
) -> None:
    """The daily client table shares the sorter but not the columns."""
    response = client.get("/partials/daily?date=2026-07-26&sort=miss")
    assert response.status_code == 200


# --------------------------------------------------------------------------
# The formatting rules that make the numbers trustworthy
# --------------------------------------------------------------------------


def test_a_rate_over_no_offers_is_none_and_not_zero(client: TestClient) -> None:
    """An empty datastore is every rate being ``None`` at once.

    Reporting those as 0% would tell the owner he turned down work nobody ever
    offered him, which is the one lie this dashboard is built not to tell. An
    hour with no offers has no miss rate; the grid draws it as an outline.
    """
    payload = client.get("/api/blind-spots").json()
    cells = [cell for row in payload["grid"] for cell in row["cells"]]
    assert len(cells) == 7 * 24
    assert all(cell["offers"] == 0 for cell in cells)
    assert all(cell["no_response_rate"] is None for cell in cells)
    # -1 is "draw an outline", not step 0 of the ramp.
    assert all(cell["step"] == -1 for cell in cells)
    assert all(cell["flag"] is False for cell in cells)


def test_no_page_ever_colours_a_missing_rate_as_a_real_one(client: TestClient) -> None:
    """A ``None`` rate must never reach the good / warn / bad colouring.

    A literal ``0%`` is still legitimate -- nought accepted of five offered is a
    fact -- so this asserts on the pairing, not on the string.
    """
    for path in ("/", "/live", "/blind-spots", "/close-off", "/daily", "/clients"):
        body = client.get(path).text
        for band in ("band-good", "band-warn", "band-bad"):
            assert f'{band}">{DASH}' not in body, f"{path} coloured a missing rate"


def test_the_percentage_filter_separates_none_from_zero() -> None:
    assert f_pct(None) == DASH
    assert f_pct(0.0) == "0%"
    assert f_pct(0.345, 1) == "34.5%"


def test_the_missed_work_json_declares_its_ranking_basis(client: TestClient, seeded) -> None:
    payload = client.get("/api/missed-work").json()
    assert payload["ranking_basis"] == "job_count"
    assert payload["ranking_note"] == RANKING_NOTE
    assert payload["available"] is True
    assert payload["error"] is None


def test_the_missed_work_json_is_json_safe(client: TestClient, seeded) -> None:
    """dates, datetimes and Decimals all survive :func:`queries.jsonable`."""
    for path in ("/api/missed-work", "/api/blind-spots", "/api/close-off"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert isinstance(response.json(), dict), path


def test_the_blind_spot_grid_is_seven_by_twenty_four(client: TestClient, seeded) -> None:
    payload = client.get("/api/blind-spots").json()
    assert len(payload["grid"]) == 7
    for row in payload["grid"]:
        assert len(row["cells"]) == 24
    assert len(payload["by_hour"]) == 24


def test_the_dashboard_never_writes(client: TestClient, seeded) -> None:
    """The read-only guarantee, checked on the newest and least-proven path.

    ``compute_missed_work`` persists by default. The dashboard must always call
    it with ``persist=False``, so loading the front page cannot leave a row
    behind in ``metrics_missed_work``.
    """
    from sqlalchemy import func, select

    from towbook_agent.core.models import MetricsMissedWork

    def count() -> int:
        with get_session(commit=False) as session:
            return int(
                session.execute(select(func.count()).select_from(MetricsMissedWork)).scalar_one()
            )

    before = count()
    for path in ("/", "/blind-spots", "/close-off", "/clients", "/api/missed-work"):
        assert client.get(path).status_code == 200
    assert count() == before, "rendering the dashboard wrote to metrics_missed_work"
