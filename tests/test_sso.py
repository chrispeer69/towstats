"""Sign in with US Tow -- the OpenID Connect flow in towbook_agent/web/sso.py.

The SSO is stood in for by an ``httpx.MockTransport`` that serves the discovery
document, the JWKS and the token endpoint from inside the process, so the real
authlib client, the real code exchange and the real RS256 verification all run
without a socket (conftest blocks the network, and this module keeps it so).
The ID token is minted with a throwaway RSA key generated per test.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

# The socket-guard narrowing test_web installs is what lets TestClient run at all
# on Windows; it is an autouse fixture, so importing it is enough.
from test_web import allow_loopback  # noqa: F401

from towbook_agent.web import sso
from towbook_agent.web.app import app
from towbook_agent.web.auth import COOKIE_NAME, DEFAULT_PASSWORD

#: The httpx flavour authlib's client speaks (httpx2 when installed), so the
#: mock transport below is the kind that client accepts.
httpx = sso.httpx

ISSUER = "https://sso.example.invalid"
CLIENT_ID = "ustowstats"
SITE = "https://www.ustowstats.com"
REDIRECT = f"{SITE}/auth/callback"


def _rsa_key(kid: str = "test-key") -> Any:
    from authlib.jose import JsonWebKey

    return JsonWebKey.generate_key("RSA", 2048, {"kid": kid}, is_private=True)


class FakeSSO:
    """The three endpoints the client talks to, served in-process."""

    def __init__(self) -> None:
        self.key = _rsa_key()
        #: The nonce the browser was given; the test copies it from the
        #: authorize URL so the minted ID token can carry it.
        self.nonce: str | None = None
        self.claims_override: dict[str, Any] = {}
        self.token_requests: list[dict[str, str]] = []
        self.transport = httpx.MockTransport(self.handle)

    def mint(self) -> str:
        from authlib.jose import jwt

        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-123",
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce,
            "email": "Owner@Example.com",
            "email_verified": True,
            "name": "Owner Person",
            "org_id": "org-1",
            "org_slug": "acme-towing",
            "org_name": "Acme Towing",
            "roles": ["owner"],
            "apps": ["ustowstats", "dispatch"],
            "sid": "sid-1",
        }
        claims.update(self.claims_override)
        header = {"alg": "RS256", "kid": self.key.kid}
        return jwt.encode(header, claims, self.key).decode("ascii")

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                    "token_endpoint": f"{ISSUER}/oauth/token",
                    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
                    "end_session_endpoint": f"{ISSUER}/oauth/logout",
                },
            )
        if path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [self.key.as_dict()]})
        if path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            form["authorization"] = request.headers.get("authorization", "")
            self.token_requests.append(form)
            if form.get("code") != "good-code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "id_token": self.mint(),
                },
            )
        return httpx.Response(404)


@pytest.fixture
def fake_sso(monkeypatch: pytest.MonkeyPatch) -> FakeSSO:
    """Configure the client and route every outbound call to the fake."""
    server = FakeSSO()
    monkeypatch.setenv("SSO_ISSUER", ISSUER)
    monkeypatch.setenv("SSO_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("SSO_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.setenv("SSO_REDIRECT_URI", REDIRECT)
    monkeypatch.setattr(sso, "_transport", server.transport)
    monkeypatch.setattr(sso, "_metadata", None)
    monkeypatch.setattr(sso, "_keys", None)
    return server


@pytest.fixture
def anonymous() -> TestClient:
    """A browser on the live domain, over https, with no session."""
    return TestClient(app, base_url=SITE)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def start(client: TestClient, fake: FakeSSO, next_path: str = "/") -> dict[str, str]:
    """GET /auth/login; return the authorize URL's query string."""
    response = client.get(f"/auth/login?next={next_path}", follow_redirects=False)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/oauth/authorize?"), location
    query = _query(location)
    fake.nonce = query["nonce"]
    return query


def finish(client: TestClient, state: str, code: str = "good-code") -> httpx.Response:
    return client.get(f"/auth/callback?code={code}&state={state}", follow_redirects=False)


def sign_in(client: TestClient, fake: FakeSSO, next_path: str = "/") -> httpx.Response:
    return finish(client, start(client, fake, next_path)["state"])


# --------------------------------------------------------------------------
# The login page
# --------------------------------------------------------------------------


def test_the_button_is_hidden_until_a_client_secret_is_set(anonymous: TestClient) -> None:
    assert "Sign in with US Tow" not in anonymous.get("/login").text


def test_the_button_is_shown_once_configured(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    body = anonymous.get("/login?next=/weekly").text
    assert "Sign in with US Tow" in body
    assert 'href="/auth/login?next=/weekly"' in body
    # The password form is still there: SSO is added alongside, not instead.
    assert 'action="/login"' in body


def test_the_password_login_still_works_alongside_sso(
    anonymous: TestClient, fake_sso: FakeSSO
) -> None:
    response = anonymous.post(
        "/login", data={"password": DEFAULT_PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert COOKIE_NAME in anonymous.cookies
    assert anonymous.get("/hourly").status_code == 200


def test_auth_login_explains_itself_when_unconfigured(anonymous: TestClient) -> None:
    response = anonymous.get("/auth/login", follow_redirects=False)
    assert response.status_code == 503
    assert "SSO_CLIENT_SECRET" in response.text


# --------------------------------------------------------------------------
# The redirect out
# --------------------------------------------------------------------------


def test_auth_login_redirects_to_the_authorize_endpoint_with_pkce(
    anonymous: TestClient, fake_sso: FakeSSO
) -> None:
    query = start(anonymous, fake_sso, "/weekly")
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT
    assert query["response_type"] == "code"
    assert query["scope"] == "openid profile email ustow"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] and query["nonce"] and query["state"]
    assert sso.FLOW_COOKIE in anonymous.cookies


def test_the_flow_cookie_is_httponly_lax_and_secure_on_https(
    anonymous: TestClient, fake_sso: FakeSSO
) -> None:
    response = anonymous.get("/auth/login", follow_redirects=False)
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header and "secure" in header


def test_the_redirect_uri_is_derived_from_the_request_when_unset(
    anonymous: TestClient, fake_sso: FakeSSO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSO_REDIRECT_URI")
    query = start(anonymous, fake_sso)
    assert query["redirect_uri"] == REDIRECT


# --------------------------------------------------------------------------
# The callback
# --------------------------------------------------------------------------


def test_a_successful_callback_opens_a_session(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    response = sign_in(anonymous, fake_sso, "/weekly")
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/weekly"
    assert sso.SESSION_COOKIE in anonymous.cookies
    assert sso.FLOW_COOKIE not in anonymous.cookies
    # The code was exchanged with the client secret and the PKCE verifier.
    exchange = fake_sso.token_requests[-1]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["redirect_uri"] == REDIRECT
    assert exchange["code_verifier"]
    assert exchange["authorization"].startswith("Basic ")
    # And the session is accepted by the gate, on pages and on the JSON API.
    page = anonymous.get("/weekly")
    assert page.status_code == 200
    assert "Owner Person" in page.text
    assert anonymous.get("/api/hourly").status_code == 200


def test_the_identity_is_what_the_cookie_carries(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    sign_in(anonymous, fake_sso)
    user = sso._read(anonymous.cookies[sso.SESSION_COOKIE], max_age=3600)
    assert user is not None
    assert user["email"] == "owner@example.com"  # case-folded
    assert user["org_slug"] == "acme-towing"
    assert user["roles"] == ["owner"]
    assert user["admin"] is True


def test_a_user_without_the_app_is_refused(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    fake_sso.claims_override = {"apps": ["dispatch"]}
    response = sign_in(anonymous, fake_sso)
    assert response.status_code == 403
    assert "not on your US Tow dashboard" in response.text
    assert sso.SESSION_COOKIE not in anonymous.cookies


def test_a_state_mismatch_is_refused(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    start(anonymous, fake_sso)
    response = finish(anonymous, "some-other-state")
    assert response.status_code == 400
    assert "mismatch" in response.text
    assert sso.SESSION_COOKIE not in anonymous.cookies


def test_a_callback_without_a_flow_is_refused(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    response = finish(anonymous, "y")
    assert response.status_code == 400
    assert sso.SESSION_COOKIE not in anonymous.cookies


def test_a_wrong_nonce_is_refused(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    state = start(anonymous, fake_sso)["state"]
    fake_sso.nonce = "not-the-nonce"
    response = finish(anonymous, state)
    assert response.status_code == 401
    assert sso.SESSION_COOKIE not in anonymous.cookies


def test_a_token_for_another_audience_is_refused(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    fake_sso.claims_override = {"aud": "dispatch"}
    assert sign_in(anonymous, fake_sso).status_code == 401


def test_a_token_from_another_issuer_is_refused(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    fake_sso.claims_override = {"iss": "https://evil.example.invalid"}
    assert sign_in(anonymous, fake_sso).status_code == 401


def test_an_expired_token_is_refused(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    fake_sso.claims_override = {"exp": int(time.time()) - 600}
    assert sign_in(anonymous, fake_sso).status_code == 401


def test_a_token_signed_with_the_wrong_key_is_refused(
    anonymous: TestClient, fake_sso: FakeSSO
) -> None:
    sso.key_set()  # cache the genuine key under kid "test-key"
    fake_sso.key = _rsa_key("test-key")  # same kid, different key: a forgery
    assert sign_in(anonymous, fake_sso).status_code == 401


def test_a_rotated_key_is_fetched_once_more(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    sso.key_set()  # cached: only "test-key"
    fake_sso.key = _rsa_key("rotated")  # the SSO now signs with a key we have not seen
    assert sign_in(anonymous, fake_sso).status_code == 303


def test_a_rejected_code_is_reported(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    state = start(anonymous, fake_sso)["state"]
    response = finish(anonymous, state, code="bad-code")
    assert response.status_code == 502
    assert "did not accept" in response.text


def test_an_error_from_the_sso_is_shown(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    state = start(anonymous, fake_sso)["state"]
    response = anonymous.get(
        f"/auth/callback?error=access_denied&error_description=Nope&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "Nope" in response.text


def test_the_session_cookie_cannot_be_forged(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    sign_in(anonymous, fake_sso)
    body, signature = anonymous.cookies[sso.SESSION_COOKIE].split(".")
    anonymous.cookies.clear()
    anonymous.cookies.set(sso.SESSION_COOKIE, f"{body}.{'0' * len(signature)}", domain="www.ustowstats.com")
    response = anonymous.get("/weekly", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_a_signed_in_visitor_hitting_auth_login_is_sent_on(
    anonymous: TestClient, fake_sso: FakeSSO
) -> None:
    sign_in(anonymous, fake_sso)
    response = anonymous.get("/auth/login?next=/weekly", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/weekly"


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------


def test_signing_out_of_an_sso_session_ends_it_at_the_sso_too(
    anonymous: TestClient, fake_sso: FakeSSO
) -> None:
    sign_in(anonymous, fake_sso)
    response = anonymous.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/oauth/logout?"), location
    query = _query(location)
    assert query["post_logout_redirect_uri"] == f"{SITE}/"
    assert query["id_token_hint"].count(".") == 2
    assert sso.SESSION_COOKIE not in anonymous.cookies
    assert anonymous.get("/weekly", follow_redirects=False).status_code == 303


def test_auth_logout_does_the_same(anonymous: TestClient, fake_sso: FakeSSO) -> None:
    sign_in(anonymous, fake_sso)
    response = anonymous.get("/auth/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"{ISSUER}/oauth/logout?")


def test_a_password_session_signs_out_locally_only(
    anonymous: TestClient, fake_sso: FakeSSO
) -> None:
    anonymous.post(
        "/login", data={"password": DEFAULT_PASSWORD, "next": "/"}, follow_redirects=False
    )
    response = anonymous.post("/logout", follow_redirects=False)
    assert response.headers["location"] == "/login"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_the_issuer_and_client_id_default_to_production() -> None:
    assert sso.issuer() == "https://us-tow-sso-production.up.railway.app"
    assert sso.client_id() == "ustowstats"
    assert not sso.is_configured()


def test_the_env_example_documents_every_variable_and_carries_no_secret() -> None:
    from conftest import REAL_REPO_ROOT

    text = (REAL_REPO_ROOT / ".env.example").read_text("utf-8")
    for name in ("SSO_ISSUER", "SSO_CLIENT_ID", "SSO_CLIENT_SECRET", "SSO_REDIRECT_URI"):
        assert f"\n{name}=" in text, name
    assert "\nSSO_CLIENT_SECRET=\n" in text
