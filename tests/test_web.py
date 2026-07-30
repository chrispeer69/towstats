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
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from conftest import REAL_REPO_ROOT, ingest_file
from fixture_generator import generate_fixture_xlsx
from towbook_agent.core.db import get_session
from towbook_agent.web.app import DASH, TABS, app, f_pct
from towbook_agent.web.auth import COOKIE_NAME, DEFAULT_PASSWORD
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
    # The company switcher. "default" is the id a single-company install has,
    # and an unknown id would resolve to the default anyway -- the switcher is
    # deliberately incapable of 404ing on a stale bookmark.
    "company_id": "default",
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
def anonymous() -> TestClient:
    """A client with no session cookie. Used to prove the gate is closed."""
    return TestClient(app)


@pytest.fixture
def client(anonymous: TestClient) -> TestClient:
    """A signed-in client.

    The whole board is behind a shared password now, so every page test needs a
    session. Logging in through the real form rather than forging a cookie
    means the sweep below also proves the login path works: if
    :mod:`towbook_agent.web.auth` broke, this fixture would fail and take every
    route test with it, which is the correct blast radius.
    """
    response = anonymous.post(
        "/login",
        data={"password": DEFAULT_PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.status_code
    assert COOKIE_NAME in anonymous.cookies, "logging in did not set a session cookie"
    return anonymous


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


def test_the_four_tabs_are_served() -> None:
    """The board is the delivery mechanism; the four tabs are the board."""
    for _key, _label, href in TABS:
        assert href in ROUTES, f"{href} is not served; ROUTES={ROUTES}"
    assert [href for _k, _l, href in TABS] == ["/hourly", "/weekly", "/monthly", "/trends"]


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


# --------------------------------------------------------------------------
# The password gate
# --------------------------------------------------------------------------
#
# The board is on a public URL now, so "is it actually closed" is not a detail.
# These assert the gate is shut, that the documented default opens it, that
# /healthz stays open for Railway, and that the login form says out loud what
# "1234" is worth.


def test_the_board_is_closed_without_a_session(anonymous: TestClient) -> None:
    for path in ("/", "/hourly", "/weekly", "/monthly", "/trends", "/clients", "/health"):
        response = anonymous.get(path, follow_redirects=False)
        assert response.status_code == 303, f"{path} was served without a session"
        assert response.headers["location"].startswith("/login?next="), path


def test_the_api_answers_401_rather_than_redirecting(anonymous: TestClient) -> None:
    """A chart fetch that followed a redirect would parse HTML as JSON."""
    response = anonymous.get("/api/hourly", follow_redirects=False)
    assert response.status_code == 401
    assert response.json()["login"] == "/login"


def test_healthz_is_exempt_so_railway_can_probe_it(anonymous: TestClient) -> None:
    response = anonymous.get("/healthz", follow_redirects=False)
    assert response.status_code == 200


def test_the_stylesheet_is_exempt(anonymous: TestClient) -> None:
    """The login page has to be able to load its own CSS."""
    assert anonymous.get("/static/app.css", follow_redirects=False).status_code == 200


def test_the_login_page_renders_and_warns_about_the_default_password(
    anonymous: TestClient,
) -> None:
    body = anonymous.get("/login").text
    assert 'name="password"' in body
    assert "not adequate protection for real customer data" in body
    assert "DASHBOARD_PASSWORD" in body


def test_a_changed_password_removes_the_default_warning(
    anonymous: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_PASSWORD", "a-long-and-boring-passphrase")
    body = anonymous.get("/login").text
    assert "not adequate protection for real customer data" not in body


def test_the_wrong_password_is_refused(anonymous: TestClient) -> None:
    response = anonymous.post(
        "/login", data={"password": "wrong", "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 401
    assert COOKIE_NAME not in anonymous.cookies


def test_the_default_password_works_out_of_the_box(client: TestClient) -> None:
    """The ``client`` fixture logs in with 1234 and nothing else configured."""
    assert client.get("/hourly").status_code == 200


def test_rotating_the_password_invalidates_a_live_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotation that left old cookies working would not be a rotation."""
    assert client.get("/hourly", follow_redirects=False).status_code == 200
    monkeypatch.setenv("DASHBOARD_PASSWORD", "something-else-entirely")
    assert client.get("/hourly", follow_redirects=False).status_code == 303


def test_signing_out_ends_the_session(client: TestClient) -> None:
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert client.get("/hourly", follow_redirects=False).status_code == 303


def test_the_login_form_will_not_redirect_off_site(anonymous: TestClient) -> None:
    """An open redirect on a login form is a phishing link with a real domain."""
    response = anonymous.post(
        "/login",
        data={"password": DEFAULT_PASSWORD, "next": "//evil.example.com/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_the_session_cookie_is_httponly_and_lax(anonymous: TestClient) -> None:
    response = anonymous.post(
        "/login", data={"password": DEFAULT_PASSWORD, "next": "/"}, follow_redirects=False
    )
    header = response.headers["set-cookie"].casefold()
    assert "httponly" in header
    assert "samesite=lax" in header
    # No TLS on this transport, so no Secure flag -- a Secure cookie over plain
    # http is never stored and the login would silently loop.
    assert "secure" not in header


def test_the_cookie_is_secure_behind_an_https_proxy(anonymous: TestClient) -> None:
    """Railway terminates TLS, so the scheme is http and the header is the truth."""
    response = anonymous.post(
        "/login",
        data={"password": DEFAULT_PASSWORD, "next": "/"},
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    )
    assert "secure" in response.headers["set-cookie"].casefold()


# --------------------------------------------------------------------------
# The four tabs
# --------------------------------------------------------------------------


def test_every_tab_is_in_the_navigation_of_every_page(client: TestClient, seeded) -> None:
    for path in ("/", "/hourly", "/weekly", "/monthly", "/trends", "/health"):
        body = client.get(path).text
        for _key, label, href in TABS:
            assert f'href="{href}"' in body, f"{path} does not link to {href}"
            assert label in body, f"{path} does not name the {label} tab"


def test_the_detail_views_are_still_reachable_from_every_tab(
    client: TestClient, seeded
) -> None:
    """Demoting them to a second row must not have hidden them."""
    body = client.get("/hourly").text
    for href in (
        "/",
        "/blind-spots",
        "/close-off",
        "/live",
        "/daily",
        "/clients",
        "/rules",
        "/health",
    ):
        assert f'href="{href}"' in body, f"the Hourly tab does not link to {href}"


def test_the_hourly_tab_carries_everything_the_sms_carried(
    client: TestClient, seeded
) -> None:
    """This screen IS the hourly text message. Losing a line loses it for good.

    The message was three lines: the hour, the running day total, and an
    unanswered warning when there was one. The first two are unconditional and
    must be on the page verbatim.
    """
    body = client.get("/hourly").text
    payload = client.get("/api/hourly").json()

    lines = payload["text_block"].splitlines()
    assert len(lines) >= 2
    assert "| Offered " in lines[0] and "/ Accepted " in lines[0]
    assert lines[1].startswith("Day: ")
    for line in lines:
        assert line in body, f"the hourly board does not show {line!r}"

    assert "Unanswered this hour" in body
    assert "Day so far" in body
    assert "Hour by hour" in body


def test_the_hourly_tab_refreshes_itself(client: TestClient, seeded) -> None:
    """It replaced a message that arrived without being asked for."""
    body = client.get("/hourly").text
    assert 'hx-get="/partials/hourly' in body
    assert 'hx-trigger="every 60s"' in body
    assert client.get("/partials/hourly").status_code == 200


def test_the_hourly_grid_has_twenty_four_hours_and_no_fake_zeros(
    client: TestClient,
) -> None:
    """Against an empty datastore every hour is ``None`` -- not 0%."""
    payload = client.get("/api/hourly").json()
    assert len(payload["hours"]) == 24
    assert all(row["offered"] == 0 for row in payload["hours"])
    assert all(row["rate"] is None for row in payload["hours"])
    assert all(row["unanswered_rate"] is None for row in payload["hours"])


def test_the_period_tabs_compare_like_for_like(client: TestClient, seeded) -> None:
    """A two-day-old week against a full previous week reads as a collapse."""
    for kind in ("week", "month"):
        payload = client.get(f"/api/period?kind={kind}").json()
        assert payload["kind"] == kind
        current_days = (
            date.fromisoformat(payload["current_last"])
            - date.fromisoformat(payload["current_first"])
        ).days
        previous_days = (
            date.fromisoformat(payload["previous_last"])
            - date.fromisoformat(payload["previous_first"])
        ).days
        assert current_days == previous_days, f"{kind} compares unequal spans"


def test_the_weekly_tab_leads_with_coverage_causes_and_actions(
    client: TestClient, seeded
) -> None:
    body = client.get("/weekly").text
    assert "This week" in body
    assert "What to do about it" in body or "Nothing offered this week" in body
    assert "Cause, and whether it is growing" in body or "Nothing offered this week" in body


def test_the_monthly_tab_tracks_trajectories_and_close_offs(
    client: TestClient, seeded
) -> None:
    body = client.get("/monthly").text
    assert "This month" in body
    assert "Client trajectories" in body or "Nothing offered this month" in body
    assert "Did the close-offs work?" in body or "Nothing offered this month" in body


def test_the_trends_tab_carries_the_important_trends(client: TestClient, seeded) -> None:
    body = client.get("/trends").text
    for marker in (
        "Blind spots",  # the 7 x 24 grid
        "Coverage gap, week by week",
        "Client rate trajectories",
        "Offer volume over time",
        "Close-off candidates",
    ):
        assert marker in body, f"the Trends tab is missing {marker!r}"


def test_every_tab_states_what_unit_its_numbers_are_in(client: TestClient, seeded) -> None:
    """The single most important sentence in the UI, on the new tabs too.

    ``notifier.ranking_note`` is the one place that decides which of the two
    true sentences to print, so the board and the reports cannot end up making
    different claims about the same figures.
    """
    from towbook_agent.agents.notifier import ranking_note

    expected = ranking_note(False)
    for path in ("/hourly", "/weekly", "/monthly"):
        assert expected in client.get(path).text, f"{path} does not state its basis"
    assert RANKING_NOTE in client.get("/trends").text


def test_the_new_tabs_render_on_an_empty_datastore(client: TestClient) -> None:
    """A brand-new deployment has no data and must not 500."""
    for path in ("/hourly", "/weekly", "/monthly", "/trends", "/api/hourly", "/api/period"):
        assert client.get(path).status_code == 200, path


def test_the_period_tabs_never_write(client: TestClient, seeded) -> None:
    """Three compute_missed_work calls per page load, all with persist=False."""
    from sqlalchemy import func, select

    from towbook_agent.core.models import MetricsMissedWork

    def count() -> int:
        with get_session(commit=False) as session:
            return int(
                session.execute(select(func.count()).select_from(MetricsMissedWork)).scalar_one()
            )

    before = count()
    for path in ("/weekly", "/monthly", "/api/period?kind=week", "/api/period?kind=month"):
        assert client.get(path).status_code == 200, path
    assert count() == before, "rendering a period tab wrote to metrics_missed_work"


# ==========================================================================
# Print / PDF
#
# The letterhead is display:none on screen, so a broken one is invisible until
# it is on a document somebody has already sent. These assert the markup and
# the stylesheet that reveals it, which is as far as a test can go without
# driving a real print dialog.
# ==========================================================================


def test_every_tab_carries_the_print_button_and_the_letterhead(client: TestClient) -> None:
    for _key, _label, href in TABS:
        html = client.get(href).text
        assert 'id="print-btn"' in html, f"{href} has no Print button"
        assert 'class="letterhead"' in html, f"{href} has no letterhead"


def test_the_letterhead_names_the_company_the_numbers_belong_to(client: TestClient) -> None:
    """On a printed page the company name is the only provenance there is.

    Resolved through the same call the request uses rather than hard-coded, so
    this holds for the fixture roster, for the real one, and for the
    single-company fallback.
    """
    from towbook_agent.core import companies as companies_module

    expected = companies_module.resolve_company(None).letterhead_name
    assert expected, "a company with no usable name would print a blank header"

    html = client.get("/hourly").text
    letterhead = html.split('class="letterhead"', 1)[1].split("</header>", 1)[0]
    assert expected in letterhead


def test_the_letterhead_is_hidden_on_screen_and_shown_in_print() -> None:
    """Both halves matter: leaking it on screen duplicates the topbar."""
    css = (REAL_REPO_ROOT / "towbook_agent" / "web" / "static" / "app.css").read_text("utf-8")

    screen, _, printed = css.partition("@media print")
    assert ".letterhead { display: none; }" in screen
    assert "display: flex !important" in printed

    # The chrome must not survive into the document.
    for selector in (".topbar", ".subbar", "footer.foot", ".icon-btn", "#print-btn"):
        assert selector in printed, f"{selector} is not hidden in print"

    # A table split across pages loses its header; a row split loses its
    # meaning. Both are the difference between a report and a mess.
    assert "display: table-header-group" in printed
    assert "page-break-inside: avoid" in printed
    assert "@page" in css


def test_printing_repaints_the_charts_instead_of_printing_black_boxes() -> None:
    """CSS cannot recolour a <canvas>; dash.js has to redraw it."""
    js = (REAL_REPO_ROOT / "towbook_agent" / "web" / "static" / "dash.js").read_text("utf-8")
    assert "beforeprint" in js and "afterprint" in js
    assert "rebuildCharts" in js
    # The swap must not be persisted -- printing is not a theme preference.
    before = js.split("beforeprint", 1)[1].split("afterprint", 1)[0]
    assert "localStorage" not in before


# --------------------------------------------------------------------------
# The Towbook reference
#
# The reports are read with the portal open, so every list of individual jobs
# has to say WHICH job. Towbook only issues a job number once an offer becomes
# a job, so on the jobs we did not take the reference is usually the Digital
# Request id -- and a blank cell there would make the list unusable.
# --------------------------------------------------------------------------


def test_the_missed_page_lists_the_jobs_with_a_towbook_reference(
    client: TestClient, seeded
) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    assert "Jobs we did not get" in body
    assert "Towbook ref" in body
    # Both kinds of reference are labelled. A bare number would leave the
    # reader guessing which of two different Towbook screens to search.
    assert "Req " in body
    assert "never became a job" in body, "the page must explain the blank job numbers"


def test_the_daily_variance_table_says_which_jobs(client: TestClient, seeded) -> None:
    """Policy variance is a list of individual decisions to review."""
    response = client.get(f"/daily?date={END_DATE.isoformat()}")
    assert response.status_code == 200
    assert "Towbook ref" in response.text


def test_a_job_number_is_shown_as_a_job_number(client: TestClient, seeded) -> None:
    """An accepted job carries a real Towbook call number, and it is labelled."""
    from towbook_agent.web import queries as q

    rows = q.fetch_requests(
        *q.local_span_bounds(END_DATE - timedelta(days=DAYS), END_DATE)
    )
    numbered = [row for row in rows if row["job_number"]]
    assert numbered, "the fixture has no accepted job carrying a call number"

    for row in numbered:
        assert row["towbook_ref_kind"] == "job"
        assert row["towbook_ref"] == row["job_number"]

    unnumbered = [row for row in rows if not row["job_number"]]
    assert unnumbered, "the fixture has no offer without a call number"
    for row in unnumbered:
        # NEVER blank: the request id is what finds an offer we did not take.
        assert row["towbook_ref"] == row["request_id"]
        assert row["towbook_ref_kind"] == "request"
