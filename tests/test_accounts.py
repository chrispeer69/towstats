"""One board, several towing companies, and never one customer's reader in
another customer's numbers.

``test_companies.py`` proves the ROWS stay apart: every query filters on
``company_id`` and one tenant's offers never reach another tenant's totals.
That was always true, and it was never enough to sell this to a company that
does not own the server, because the reader was not fenced at all. There was
one shared password, the company switcher was a preference rather than a
permission, and typing another company's slug into ``/company/<id>`` worked.

So these tests are adversarial about the READER rather than about the row. A
member scoped to one company is pointed at the other one every way the URL
allows -- the switcher, the path, the query string on a JSON endpoint, the
merged view, a stale cookie -- and every one of them has to come back with
their own company's numbers or with nothing.

The other properties proved here, each because its absence is a way in:

* the shared password stops working the moment an account exists, so a
  DASHBOARD_PASSWORD left set on the Railway service is not a spare key;
* a v1 cookie minted before the first account does not survive it;
* disabling an account ends its live sessions, not just its next sign-in;
* changing one user's password ends that user's other sessions and nobody
  else's, while an iteration-count re-hash ends none;
* the merged view sums the reader's companies and not the install's;
* the last operator cannot be removed, because there is no recovery password
  to fall back on afterwards.
"""

from __future__ import annotations

import socket

import pytest
from starlette.testclient import TestClient

from towbook_agent.core import companies as companies_module
from towbook_agent.web import accounts
from towbook_agent.web.app import app
from towbook_agent.web.auth import COOKIE_NAME, DEFAULT_PASSWORD

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

    The same reasoning as in test_web.py: ``TestClient`` runs the app on an
    anyio portal, and asyncio's Windows proactor loop builds its self-pipe with
    :func:`socket.socketpair`, which connects to 127.0.0.1. The guard is
    narrowed rather than lifted -- anything that is not loopback still raises.
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

# --------------------------------------------------------------------------
# A roster with two companies that are plainly not each other
# --------------------------------------------------------------------------

ROSTER = {
    "version": 1,
    "default_company": "acme",
    "companies": [
        {
            "id": "acme",
            "name": "Acme Towing",
            "credentials_env": "ACME",
            "timezone": "America/Detroit",
            "enabled": True,
        },
        {
            "id": "beta",
            "name": "Beta Recovery",
            "credentials_env": "BETA",
            "timezone": "America/Chicago",
            "enabled": True,
        },
        {
            "id": "gamma",
            "name": "Gamma Hauling",
            "credentials_env": "GAMMA",
            "timezone": "America/Denver",
            "enabled": True,
        },
    ],
}


@pytest.fixture
def roster(write_config):
    write_config("companies", ROSTER)
    companies_module.reload_companies()
    try:
        yield ROSTER
    finally:
        companies_module.reload_companies()


@pytest.fixture
def anonymous() -> TestClient:
    """A client with no session cookie."""
    return TestClient(app)


@pytest.fixture
def operator(roster):
    """An operator account. Creating it ends shared-password mode."""
    return accounts.create_user(
        "boss",
        "correct-horse-battery",
        role=accounts.ROLE_OPERATOR,
        display_name="The Operator",
        must_change_password=False,
    )


@pytest.fixture
def acme_user(roster, operator):
    """A member who may see Acme and nothing else."""
    return accounts.create_user(
        "dave",
        "acme-password-here",
        role=accounts.ROLE_MEMBER,
        company_ids=["acme"],
        must_change_password=False,
    )


def sign_in(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.status_code
    assert COOKIE_NAME in client.cookies, "signing in did not set a session cookie"


@pytest.fixture
def dave(anonymous, acme_user) -> TestClient:
    sign_in(anonymous, "dave", "acme-password-here")
    return anonymous


# ==========================================================================
# Passwords
# ==========================================================================


def test_a_password_verifies_against_its_own_hash_and_nothing_else() -> None:
    encoded = accounts.hash_password("correct-horse-battery")
    assert accounts.verify_hash("correct-horse-battery", encoded)[0] is True
    assert accounts.verify_hash("correct-horse-batteryy", encoded)[0] is False
    assert accounts.verify_hash("", encoded)[0] is False
    assert accounts.verify_hash(None, encoded)[0] is False


def test_the_same_password_hashes_differently_every_time() -> None:
    """Per-user salt. Two accounts with one password must not look alike."""
    first = accounts.hash_password("correct-horse-battery")
    second = accounts.hash_password("correct-horse-battery")
    assert first != second


def test_a_hash_below_the_current_cost_asks_to_be_rehashed() -> None:
    """How the iteration count is raised without invalidating every password."""
    cheap = accounts.hash_password("correct-horse-battery", iterations=1000)
    matches, needs_rehash = accounts.verify_hash("correct-horse-battery", cheap)
    assert (matches, needs_rehash) == (True, True)

    current = accounts.hash_password("correct-horse-battery")
    assert accounts.verify_hash("correct-horse-battery", current) == (True, False)


def test_a_wrong_password_is_never_asked_to_be_rehashed() -> None:
    cheap = accounts.hash_password("correct-horse-battery", iterations=1000)
    assert accounts.verify_hash("something-else-here", cheap) == (False, False)


def test_a_malformed_stored_hash_is_a_non_match_not_an_exception() -> None:
    for broken in ("", None, "not-a-hash", "pbkdf2_sha256$x$y$z", "md5$1$aa$bb"):
        assert accounts.verify_hash("anything at all", broken) == (False, False)


def test_short_passwords_are_refused_with_a_reason() -> None:
    assert accounts.password_complaint("short") is not None
    assert accounts.password_complaint("x" * accounts.MIN_PASSWORD_LENGTH) is None
    assert accounts.password_complaint(" padded password ") is not None


def test_usernames_are_stored_casefolded_so_two_cases_are_one_account(roster) -> None:
    accounts.create_user("Dave", "acme-password-here", company_ids=["acme"])
    assert accounts.get_user(username="DAVE") is not None
    assert accounts.get_user(username="dave") is not None
    with pytest.raises(accounts.AccountError):
        accounts.create_user("DAVE", "another-password-x", company_ids=["acme"])


# ==========================================================================
# The two modes
# ==========================================================================


def test_with_no_accounts_the_shared_password_still_works(anonymous) -> None:
    """Every install already running is in this state and must not change."""
    assert accounts.accounts_are_configured() is False
    response = anonymous.post(
        "/login", data={"password": DEFAULT_PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert anonymous.get("/hourly").status_code == 200


def test_creating_the_first_account_kills_the_shared_password(anonymous, roster) -> None:
    """A DASHBOARD_PASSWORD left set on the service is not a spare key."""
    anonymous.post(
        "/login", data={"password": DEFAULT_PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert anonymous.get("/hourly").status_code == 200

    accounts.create_user(
        "boss", "correct-horse-battery", role=accounts.ROLE_OPERATOR,
        must_change_password=False,
    )

    # The live session dies: a v1 cookie is not accepted once accounts exist.
    assert anonymous.get("/hourly", follow_redirects=False).status_code == 303
    # And the password itself no longer opens anything.
    refused = anonymous.post(
        "/login", data={"password": DEFAULT_PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert refused.status_code == 401


def test_the_shared_password_session_may_bootstrap_the_first_account(
    anonymous, roster
) -> None:
    """The upgrade path: it must not require a redeploy to get started."""
    anonymous.post(
        "/login", data={"password": DEFAULT_PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert anonymous.get("/accounts").status_code == 200

    created = anonymous.post(
        "/accounts/create",
        data={
            "username": "boss",
            "password": "correct-horse-battery",
            "role": accounts.ROLE_OPERATOR,
            "display_name": "The Operator",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert accounts.get_user(username="boss") is not None
    # ...and that session is now over, because the shared password is.
    assert anonymous.get("/accounts", follow_redirects=False).status_code == 303


def test_the_bootstrap_operator_is_not_forced_to_change_a_password_it_chose(
    anonymous, roster
) -> None:
    anonymous.post(
        "/login", data={"password": DEFAULT_PASSWORD, "next": "/"}, follow_redirects=False
    )
    anonymous.post(
        "/accounts/create",
        data={
            "username": "boss",
            "password": "correct-horse-battery",
            "role": accounts.ROLE_OPERATOR,
        },
        follow_redirects=False,
    )
    assert accounts.get_user(username="boss").must_change_password is False


def test_an_account_created_for_somebody_else_must_change_its_password(
    anonymous, roster, operator
) -> None:
    """That password travels down a phone line. It must not survive first use."""
    client = TestClient(app)
    sign_in(client, "boss", "correct-horse-battery")
    client.post(
        "/accounts/create",
        data={
            "username": "dave",
            "password": "acme-password-here",
            "role": accounts.ROLE_MEMBER,
            "company_ids": ["acme"],
        },
        follow_redirects=False,
    )
    assert accounts.get_user(username="dave").must_change_password is True


# ==========================================================================
# Signing in
# ==========================================================================


def test_an_unknown_user_a_wrong_password_and_a_disabled_account_look_identical(
    anonymous, acme_user
) -> None:
    """Telling them apart is free reconnaissance for whoever is guessing."""
    accounts.update_user(acme_user.id, enabled=False)

    bodies = []
    for username, password in (
        ("nobody", "acme-password-here"),
        ("dave", "the-wrong-password"),
        ("dave", "acme-password-here"),
    ):
        response = anonymous.post(
            "/login",
            data={"username": username, "password": password, "next": "/"},
            follow_redirects=False,
        )
        assert response.status_code == 401
        # The form echoes back what was typed, so the bodies differ by the
        # username the caller themselves submitted. Normalise that out: what
        # must not differ is anything the SERVER knows and they do not.
        bodies.append(response.text.replace(username, "USERNAME"))

    assert len(set(bodies)) == 1, "the three failures are distinguishable from outside"

    # The same two failures under one username are byte-for-byte identical, so
    # "this account exists but is disabled" cannot be read off the response.
    assert bodies[1] == bodies[2]


def test_disabling_an_account_ends_the_session_it_already_has(dave, acme_user) -> None:
    """Not just the next sign-in. The reader is out on their next click."""
    assert dave.get("/hourly").status_code == 200
    accounts.update_user(acme_user.id, enabled=False)
    assert dave.get("/hourly", follow_redirects=False).status_code == 303


def test_changing_a_password_ends_that_users_other_sessions(dave, acme_user) -> None:
    other = TestClient(app)
    sign_in(other, "dave", "acme-password-here")
    assert other.get("/hourly").status_code == 200

    accounts.set_password(acme_user.id, "a-brand-new-password")

    assert dave.get("/hourly", follow_redirects=False).status_code == 303
    assert other.get("/hourly", follow_redirects=False).status_code == 303


def test_changing_one_password_leaves_everybody_elses_session_alone(
    dave, acme_user, operator
) -> None:
    boss = TestClient(app)
    sign_in(boss, "boss", "correct-horse-battery")

    accounts.set_password(acme_user.id, "a-brand-new-password")

    assert boss.get("/hourly").status_code == 200
    assert dave.get("/hourly", follow_redirects=False).status_code == 303


def test_a_cost_upgrade_rehash_does_not_sign_anybody_out(anonymous, roster) -> None:
    """Raising the iteration count must not be a global password reset."""
    accounts.create_user(
        "dave", "acme-password-here", company_ids=["acme"], must_change_password=False
    )
    user = accounts.get_user(username="dave")
    accounts.set_password(user.id, "acme-password-here")

    # Force the stored hash below the current cost, the way an old row would be.
    from towbook_agent.core.db import get_session
    from towbook_agent.core.models import DashboardUser

    with get_session() as session:
        row = session.get(DashboardUser, user.id)
        row.password_hash = accounts.hash_password("acme-password-here", iterations=1000)
        stamp_before = row.password_changed_at

    sign_in(anonymous, "dave", "acme-password-here")
    after = accounts.get_user(username="dave")
    assert after.password_hash.split("$")[1] == str(accounts.PBKDF2_ITERATIONS)
    assert after.password_changed_at == stamp_before, "a re-hash moved the session stamp"
    assert anonymous.get("/hourly").status_code == 200


# ==========================================================================
# THE FENCE. A member scoped to Acme, pointed at Beta every way there is.
# ==========================================================================


def test_the_switcher_offers_only_the_readers_own_companies(dave) -> None:
    body = dave.get("/hourly").text
    assert "Acme Towing" in body
    assert "Beta Recovery" not in body
    assert "Gamma Hauling" not in body


def test_switching_to_a_company_you_may_not_see_lands_on_your_own(dave) -> None:
    """The old hole: /company/<id> was a preference, so it simply worked."""
    dave.get("/company/beta", follow_redirects=False)
    body = dave.get("/hourly").text
    assert "Beta Recovery" not in body
    assert "Acme Towing" in body


def test_a_query_string_cannot_widen_a_json_endpoint(dave) -> None:
    """`?company=` is a request, not an answer, and the payload says which."""
    assert dave.get("/api/hourly?company=beta").json()["company_id"] == "acme"
    listed = dave.get("/api/companies").json()
    assert [entry["id"] for entry in listed["companies"]] == ["acme"]
    assert listed["merged_available"] is False


def test_a_forged_company_cookie_does_not_reach_another_tenant(dave) -> None:
    dave.cookies.set("towbook_company", "beta")
    body = dave.get("/hourly").text
    assert "Beta Recovery" not in body


def test_a_member_with_one_company_is_not_offered_the_merged_view(dave) -> None:
    """There is nothing to merge, and `__all__` must not mean "the install"."""
    assert companies_module.MERGED_COMPANY_ID not in dave.get("/hourly").text
    dave.get(f"/company/{companies_module.MERGED_COMPANY_ID}", follow_redirects=False)
    assert "Beta Recovery" not in dave.get("/hourly").text


def test_the_merged_view_sums_the_readers_companies_and_not_the_installs(
    roster, operator
) -> None:
    """Two members, two scopes, one merged id: it must mean different things."""
    accounts.create_user(
        "two", "two-companies-here", company_ids=["acme", "beta"],
        must_change_password=False,
    )
    accounts.create_user(
        "three", "three-companies-x", company_ids=["acme", "beta", "gamma"],
        must_change_password=False,
    )

    def merged_members(username: str, password: str) -> list[str]:
        client = TestClient(app)
        sign_in(client, username, password)
        client.get(f"/company/{companies_module.MERGED_COMPANY_ID}", follow_redirects=False)
        return sorted(client.get("/api/companies").json()["merged_members"])

    assert merged_members("two", "two-companies-here") == ["acme", "beta"]
    assert merged_members("three", "three-companies-x") == ["acme", "beta", "gamma"]


def test_an_operator_sees_every_company(roster, operator) -> None:
    client = TestClient(app)
    sign_in(client, "boss", "correct-horse-battery")
    body = client.get("/hourly").text
    for name in ("Acme Towing", "Beta Recovery", "Gamma Hauling"):
        assert name in body, f"the operator cannot see {name}"


def test_the_scope_does_not_leak_out_of_the_request(dave) -> None:
    """The pipeline must still see every company after a scoped page render.

    The fence is a ContextVar set by middleware. If it were not reset, one
    customer opening their board would leave the scheduler reporting on their
    companies alone -- every other tenant's numbers would silently stop.
    """
    dave.get("/hourly")
    assert companies_module.visible_company_ids() is None
    assert {c.id for c in companies_module.enabled_companies()} == {
        "acme",
        "beta",
        "gamma",
    }


# ==========================================================================
# The accounts screen
# ==========================================================================


def test_a_member_cannot_reach_the_accounts_screen(dave) -> None:
    """404 rather than 403: a member has no business knowing it is there."""
    assert dave.get("/accounts").status_code == 404


def test_a_member_cannot_widen_their_own_scope(dave, acme_user) -> None:
    refused = dave.post(
        f"/accounts/{acme_user.id}/update",
        data={"role": accounts.ROLE_MEMBER, "company_ids": ["acme", "beta"], "enabled": "1"},
        follow_redirects=False,
    )
    assert refused.status_code == 404
    assert accounts.get_user(user_id=acme_user.id).company_scope == ["acme"]


def test_a_member_account_must_name_at_least_one_company(roster) -> None:
    """An account that can sign in and see nothing reads as a broken board."""
    with pytest.raises(accounts.AccountError):
        accounts.create_user("nobody", "some-password-x", company_ids=[])


def test_an_operator_stores_no_company_scope(roster) -> None:
    """So that demoting them cannot leave a wildcard behind."""
    user = accounts.create_user(
        "boss", "correct-horse-battery", role=accounts.ROLE_OPERATOR,
        company_ids=["acme", "beta"],
    )
    assert accounts.get_user(user_id=user.id).company_scope == []


def test_the_last_operator_cannot_be_deleted_or_disabled(roster, operator) -> None:
    """There is no recovery password to fall back on afterwards."""
    with pytest.raises(accounts.AccountError):
        accounts.delete_user(operator.id)
    with pytest.raises(accounts.AccountError):
        accounts.update_user(operator.id, enabled=False)

    accounts.create_user(
        "second", "second-operator-x", role=accounts.ROLE_OPERATOR,
        must_change_password=False,
    )
    accounts.delete_user(operator.id)
    assert accounts.get_user(user_id=operator.id) is None


# ==========================================================================
# The password-change wall
# ==========================================================================


@pytest.fixture
def fresh_member(roster, operator):
    return accounts.create_user(
        "newbie", "handed-over-password", company_ids=["acme"], must_change_password=True
    )


def test_an_account_on_its_handed_over_password_reaches_nothing_else(
    anonymous, fresh_member
) -> None:
    sign_in(anonymous, "newbie", "handed-over-password")
    for path in ("/hourly", "/weekly", "/clients", "/"):
        response = anonymous.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/account/password", path
    assert anonymous.get("/account/password").status_code == 200


def test_json_endpoints_say_so_rather_than_redirecting(anonymous, fresh_member) -> None:
    sign_in(anonymous, "newbie", "handed-over-password")
    response = anonymous.get("/api/hourly")
    assert response.status_code == 403
    assert response.json()["change_password"] == "/account/password"


def test_changing_the_password_opens_the_board(anonymous, fresh_member) -> None:
    sign_in(anonymous, "newbie", "handed-over-password")
    response = anonymous.post(
        "/account/password",
        data={
            "current_password": "handed-over-password",
            "new_password": "chosen-by-the-reader",
            "confirm_password": "chosen-by-the-reader",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert anonymous.get("/hourly").status_code == 200
    assert accounts.get_user(username="newbie").must_change_password is False


def test_the_current_password_is_required_to_change_it(anonymous, fresh_member) -> None:
    """An unattended browser must not be a permanent handover of the account."""
    sign_in(anonymous, "newbie", "handed-over-password")
    response = anonymous.post(
        "/account/password",
        data={
            "current_password": "not-the-right-one",
            "new_password": "chosen-by-the-reader",
            "confirm_password": "chosen-by-the-reader",
        },
    )
    assert response.status_code == 400
    assert accounts.get_user(username="newbie").must_change_password is True


def test_a_mistyped_confirmation_changes_nothing(anonymous, fresh_member) -> None:
    sign_in(anonymous, "newbie", "handed-over-password")
    response = anonymous.post(
        "/account/password",
        data={
            "current_password": "handed-over-password",
            "new_password": "chosen-by-the-reader",
            "confirm_password": "chosen-by-the-readerr",
        },
    )
    assert response.status_code == 400
    assert accounts.authenticate("newbie", "handed-over-password") is not None


# ==========================================================================
# Bootstrap from the environment
# ==========================================================================


def test_the_environment_can_create_the_first_operator(roster, monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_ADMIN_USER", "boss")
    monkeypatch.setenv("DASHBOARD_ADMIN_PASS", "correct-horse-battery")
    assert accounts.bootstrap_operator_from_env() is not None
    user = accounts.get_user(username="boss")
    assert user is not None and user.role == accounts.ROLE_OPERATOR
    assert user.must_change_password is False


def test_the_environment_never_resets_an_existing_password(roster, monkeypatch) -> None:
    """The variables stay set forever. A boot that pushed them back over the
    top would silently undo every password change, on every redeploy."""
    monkeypatch.setenv("DASHBOARD_ADMIN_USER", "boss")
    monkeypatch.setenv("DASHBOARD_ADMIN_PASS", "correct-horse-battery")
    accounts.bootstrap_operator_from_env()

    accounts.set_password(accounts.get_user(username="boss").id, "changed-it-later")
    assert accounts.bootstrap_operator_from_env() is None
    assert accounts.authenticate("boss", "changed-it-later") is not None
    assert accounts.authenticate("boss", "correct-horse-battery") is None


def test_an_unusable_admin_password_creates_nothing(roster, monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_ADMIN_USER", "boss")
    monkeypatch.setenv("DASHBOARD_ADMIN_PASS", "short")
    assert accounts.bootstrap_operator_from_env() is None
    assert accounts.get_user(username="boss") is None
    assert accounts.accounts_are_configured() is False
