from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select

from models import User
from security import auth_settings, create_access_token, password_hasher


PASSWORD = "A long test password for WorkWise!"


def test_signup_login_and_me_without_exposing_password(anonymous_client, db_session):
    client = anonymous_client
    response = client.post("/auth/signup", json={"username": "  Alice_123  ", "password": PASSWORD})
    assert response.status_code == 201
    expected = {"id": response.json()["id"], "username": "alice_123", "role": "member", "is_active": True}
    assert response.json() == expected
    stored = db_session.scalar(select(User).where(User.username == "alice_123"))
    assert stored.password_hash != PASSWORD
    assert stored.password_hash.startswith("$argon2id$")
    assert password_hasher.verify(PASSWORD, stored.password_hash)

    login = client.post("/auth/login", data={
        "grant_type": "password", "username": "ALICE_123", "password": PASSWORD,
    })
    assert login.status_code == 200
    assert login.headers["cache-control"] == "no-store"
    assert login.json()["token_type"] == "bearer"
    assert login.json()["expires_in"] == auth_settings.access_token_expire_minutes * 60
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert client.get("/auth/me", headers=headers).json() == expected
    for content in (response.text, login.text, client.get("/auth/me", headers=headers).text):
        assert PASSWORD not in content and stored.password_hash not in content


def test_duplicate_signup_is_normalized_and_does_not_replace_credentials(anonymous_client):
    first = anonymous_client.post("/auth/signup", json={"username": "alice", "password": PASSWORD})
    assert first.status_code == 201
    duplicate = anonymous_client.post("/auth/signup", json={"username": " ALICE ", "password": "A different password entirely!"})
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Username already exists"}
    assert anonymous_client.post("/auth/login", data={
        "grant_type": "password", "username": "alice", "password": PASSWORD,
    }).status_code == 200
    assert anonymous_client.post("/auth/signup", json={"username": "bob", "password": PASSWORD}).status_code == 201


@pytest.mark.parametrize("changes", [
    {"username": "ab"}, {"username": "bad-name"}, {"username": "x" * 51},
    {"password": "too short"}, {"password": "x" * 129}, {"password": None},
    {"role": "admin"}, {"is_active": True}, {"id": 1},
])
def test_invalid_signup_cannot_escalate_or_echo_password(anonymous_client, changes):
    payload = {"username": "alice", "password": PASSWORD, **changes}
    response = anonymous_client.post("/auth/signup", json=payload)
    assert response.status_code == 422
    assert PASSWORD not in response.text
    if isinstance(payload["password"], str):
        assert payload["password"] not in response.text


def test_auth_validation_does_not_echo_password_in_whole_body_errors(anonymous_client):
    for payload in ({"password": PASSWORD}, PASSWORD):
        response = anonymous_client.post("/auth/signup", json=payload)
        assert response.status_code == 422
        assert PASSWORD not in response.text


def test_wrong_password_and_unknown_username_share_error(anonymous_client):
    assert anonymous_client.post("/auth/signup", json={"username": "alice", "password": PASSWORD}).status_code == 201
    responses = [anonymous_client.post("/auth/login", data={
        "grant_type": "password", "username": username, "password": "Incorrect password attempt!",
    }) for username in ("alice", "does_not_exist")]
    for response in responses:
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {"detail": "Incorrect username or password"}


def test_login_requires_password_grant(anonymous_client):
    response = anonymous_client.post("/auth/login", data={"username": "alice", "password": PASSWORD})
    assert response.status_code == 422
    assert PASSWORD not in response.text


@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer invalid-token"])
def test_me_requires_valid_bearer_token(anonymous_client, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    response = anonymous_client.get("/auth/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("change", [
    "expired", "future_iat", "future_nbf", "issuer", "audience", "type", "missing_exp",
    "subject_text", "subject_number", "subject_null", "subject_large", "subject_zero",
    "wrong_key", "wrong_algorithm", "unsigned", "null_exp", "boolean_iat", "string_nbf",
])
def test_invalid_token_claims_and_signatures_rejected(anonymous_client, auth_user, change):
    now = datetime.now(timezone.utc)
    claims = {"sub": str(auth_user.id), "iat": now, "nbf": now, "exp": now + timedelta(minutes=5),
              "iss": auth_settings.jwt_issuer, "aud": auth_settings.jwt_audience, "type": "access"}
    if change == "expired":
        claims["exp"] = now - timedelta(seconds=1)
    elif change == "future_iat":
        claims["iat"] = now + timedelta(minutes=5)
    elif change == "future_nbf":
        claims["nbf"] = now + timedelta(minutes=5)
    elif change in {"issuer", "audience", "type"}:
        claims[{"issuer": "iss", "audience": "aud", "type": "type"}[change]] = "wrong"
    elif change == "missing_exp":
        del claims["exp"]
    elif change == "null_exp":
        claims["exp"] = None
    elif change == "boolean_iat":
        claims["iat"] = False
    elif change == "string_nbf":
        claims["nbf"] = str(int(now.timestamp()))
    elif change.startswith("subject_"):
        claims["sub"] = {"subject_text": "alice", "subject_number": auth_user.id,
                         "subject_null": None, "subject_large": "9" * 100, "subject_zero": "0"}[change]
    key = "wrong-key-that-is-at-least-thirty-two-characters" if change == "wrong_key" else auth_settings.jwt_secret.get_secret_value()
    algorithm = "HS512" if change == "wrong_algorithm" else "HS256"
    if change == "unsigned":
        key, algorithm = "", "none"
    token = jwt.encode(claims, key, algorithm=algorithm)
    response = anonymous_client.get("/auth/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_tokens_recheck_current_user_role_and_activation(anonymous_client, auth_user, db_session):
    headers = {"Authorization": "Bearer " + create_access_token(auth_user.id)}
    auth_user.role = "admin"
    db_session.commit()
    assert anonymous_client.get("/auth/me", headers=headers).json()["role"] == "admin"
    auth_user.role = "member"
    db_session.commit()
    assert anonymous_client.get("/auth/me", headers=headers).json()["role"] == "member"
    auth_user.is_active = False
    db_session.commit()
    assert anonymous_client.get("/auth/me", headers=headers).status_code == 401


def test_disabled_user_cannot_login(anonymous_client, auth_user, db_session):
    auth_user.is_active = False
    db_session.commit()
    response = anonymous_client.post("/auth/login", data={
        "grant_type": "password", "username": auth_user.username,
        "password": "test password for WorkWise 2026",
    })
    assert response.status_code == 401


def test_token_for_nonexistent_user_rejected(anonymous_client):
    response = anonymous_client.get("/auth/me", headers={"Authorization": "Bearer " + create_access_token(2147483647)})
    assert response.status_code == 401
