"""Sign-up, login, bearer tokens, and the app-wide error shape outside teams.

Backs the contract's Auth section and its Conventions: errors in one shape, 401 for a missing or bad token, 404 and 405
in that shape too. Login timing for unknown emails is tested as an invariant (one argon2 verification per attempt,
counted with a spy), not by measuring time, so it cannot be flaky; it shows equal work, not equal time. Token expiry
uses the fake clock.

Not here: exact key sets of each object (`test_contract.py`), the body cap and response headers (`test_http.py`), what a
token may reach inside a team (`test_isolation.py`).
"""

from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, security
from helpers import Api, FakeClock, assert_error

SIGNUP = {"email": "Alice@Example.com", "password": "correct horse", "name": "Alice"}


def test_signup_lowercases_email(client: TestClient) -> None:
    """Sign-up answers 201 with the email lower-cased.

    Storing the address lower-cased is what makes email uniqueness case-insensitive. That the User carries exactly its
    public fields (no password or hash) is pinned with every other object's key set in `test_contract.py`.
    """
    response = client.post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201
    assert response.json()["email"] == "alice@example.com"


def test_duplicate_email_is_refused_case_insensitively(client: TestClient) -> None:
    """A second sign-up with the same address in other letter case is 409 `email_taken`."""
    client.post("/auth/signup", json=SIGNUP)
    response = client.post("/auth/signup", json={**SIGNUP, "email": "ALICE@example.com"})
    assert_error(response, 409, "email_taken")


@pytest.mark.parametrize(
    "change",
    [
        {"email": "not-an-email"},
        {"password": "short"},
        {"password": "x" * 129},
        {"name": ""},
        {"name": "x" * 101},
    ],
)
def test_signup_validates_limits(client: TestClient, change: dict[str, str]) -> None:
    """Each sign-up field limit in the contract is 422 `validation_error` with `details`.

    Case ids are `change0` to `change4` in list order: invalid email, 5-character password, 129-character password,
    empty name, 101-character name.
    """
    response = client.post("/auth/signup", json={**SIGNUP, **change})
    error = assert_error(response, 422, "validation_error")
    assert error["details"]


def test_login_returns_token_that_identifies_the_user(client: TestClient) -> None:
    """Login (email in any case) returns a token, and `/me` with that token is the same user."""
    client.post("/auth/signup", json=SIGNUP)

    response = client.post("/auth/login", json={"email": "alice@EXAMPLE.com", "password": SIGNUP["password"]})

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "alice@example.com"
    assert body["expires_at"]
    me = client.get("/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json() == body["user"]


@pytest.mark.parametrize("email", ["  Bob@Example.com ", "Bob <bob@EXAMPLE.com>"])
def test_login_accepts_the_address_as_typed_at_signup(client: TestClient, email: str) -> None:
    """An address with surrounding spaces or a display name is normalised the same way at sign-up and at login.

    Otherwise a user could sign up with an address they can never log in with. Case ids are the address as typed.
    """
    signup = client.post("/auth/signup", json={**SIGNUP, "email": email})
    assert signup.json()["email"] == "bob@example.com"

    response = client.post("/auth/login", json={"email": email, "password": SIGNUP["password"]})

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "bob@example.com"


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("alice@example.com", "wrong password"),
        ("nobody@example.com", "correct horse"),
        ("not an email", "correct horse"),
    ],
    ids=["wrong-password", "unknown-email", "malformed-email"],
)
def test_bad_credentials_are_refused_alike_at_the_same_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, email: str, password: str
) -> None:
    """Wrong password, unknown email and malformed email get the same 401 `invalid_credentials` after one hash check.

    A different answer for an unknown email would let anyone test which emails have accounts, and so would a faster
    one: without the dummy hash, a known email takes 48 ms and an unknown one 1.4 ms. The test counts
    argon2 verifications (one per attempt) rather than timing them, so it shows equal work, not equal time, and cannot
    be flaky. Ids name the case.
    """
    client.post("/auth/signup", json=SIGNUP)
    hasher = Mock(wraps=security._hasher)
    monkeypatch.setattr(security, "_hasher", hasher)

    assert_error(client.post("/auth/login", json={"email": email, "password": password}), 401, "invalid_credentials")

    assert hasher.verify.call_count == 1


def test_the_dummy_hash_has_the_production_cost() -> None:
    """The dummy hash for unknown emails carries the production argon2 memory cost.

    Verifying costs what the hash's own parameters say, so a cheaper dummy would make unknown emails fast again. Reads
    the private `security._DUMMY_HASH` because nothing public exposes it.
    """
    # Hashed at import, before the suite swaps in a cheap hasher, so it costs
    # what a real account's hash costs.
    assert security._DUMMY_HASH.startswith(f"$argon2id$v=19$m={security.PasswordHasher().memory_cost},")


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-real-token"}, {"Authorization": "Basic YWxpY2U6cHc="}],
    ids=["missing", "unknown", "wrong-scheme"],
)
def test_me_requires_a_valid_bearer_token(client: TestClient, headers: dict[str, str]) -> None:
    """A missing token, an unknown token and a non-Bearer scheme are each 401 `unauthorized`.

    The answer carries `WWW-Authenticate: Bearer`. Ids `missing`, `unknown` and `wrong-scheme` name the `Authorization`
    header sent.
    """
    response = client.get("/me", headers=headers)
    assert_error(response, 401, "unauthorized")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_token_expires_after_seven_days(api: Api, fake_clock: FakeClock) -> None:
    """A token works a minute before its 7-day lifetime ends and is 401 a minute after (fake clock)."""
    alice = api.signup("alice")
    fake_clock.advance(7 * 24 * 3600 - 60)
    assert api.client.get("/me", headers=alice.headers).status_code == 200

    fake_clock.advance(120)
    assert_error(api.client.get("/me", headers=alice.headers), 401, "unauthorized")


def test_health(client: TestClient) -> None:
    """`GET /health` is 200 `{"status": "ok"}` without a token."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    """An unknown path is 404 `not_found` in the contract's error shape, not FastAPI's default `detail`."""
    assert_error(client.get("/no/such/route"), 404, "not_found")


def test_wrong_method_uses_error_shape(client: TestClient) -> None:
    """A wrong method is 405 `method_not_allowed` in the contract's error shape."""
    assert_error(client.delete("/health"), 405, "method_not_allowed")


def test_body_that_cannot_be_read_is_400_bad_request(client: TestClient) -> None:
    """A body that is not UTF-8 at all is 400 `bad_request` in the error shape; malformed JSON is 422 instead.

    The contract lists both codes. FastAPI answers an unreadable body with its own 400, which must still come out in
    the contract's shape with a code a client can switch on.
    """
    unreadable = client.post("/auth/login", content=b"\x80 not utf-8", headers={"Content-Type": "application/json"})
    malformed = client.post("/auth/login", content=b"{not json", headers={"Content-Type": "application/json"})

    assert_error(unreadable, 400, "bad_request")
    assert_error(malformed, 422, "validation_error")


def test_unexpected_server_error_is_500_internal_error_without_details(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception is 500 `internal_error` in the error shape, and its text does not reach the client.

    The catch-all handler in `app/main.py` exists so a bug never answers in a shape the contract does not define, nor
    leaks internals. Forced by making the store's token lookup raise.
    """
    monkeypatch.setattr(app.state.store, "user_for_token", Mock(side_effect=RuntimeError("secret internals")))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/me", headers={"Authorization": "Bearer any"})

    assert_error(response, 500, "internal_error")
    assert "secret internals" not in response.text


def test_missing_test_page_is_a_404_not_a_crash(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the test page file, `/` is a contract 404 and the API keeps working.

    The page is a test aid; a deployment without it must not break.
    """
    monkeypatch.setattr(main, "INDEX_PATH", tmp_path / "absent.html")
    assert_error(client.get("/"), 404, "not_found")
