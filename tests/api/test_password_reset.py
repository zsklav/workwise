from datetime import datetime, timedelta, timezone
import re

import pytest
from sqlalchemy import select

from workwise.api.main import app
from workwise.core.mailer import get_email_sender
from workwise.core.security import auth_settings
from workwise.db.models import PasswordResetToken, User

PASSWORD = "a long first passphrase 2026"
NEW_PASSWORD = "a brand new passphrase 2027"


class Outbox:
    def __init__(self):
        self.messages = []

    def send(self, to, subject, body):
        self.messages.append((to, subject, body))

    def token(self, index=-1):
        return re.search(r"token=([\w-]+)", self.messages[index][2]).group(1)


@pytest.fixture
def outbox():
    box = Outbox()
    app.dependency_overrides[get_email_sender] = lambda: box
    return box


def signup(client, username="reset_user", email="Reset.User@Example.com"):
    body = {"username": username, "password": PASSWORD, "email": email}
    response = client.post("/v1/auth/signup", json=body)
    assert response.status_code == 201
    return response.json()


def login(client, username="reset_user", password=PASSWORD):
    return client.post("/v1/auth/login", data={"grant_type": "password", "username": username, "password": password})


def test_reset_flow_changes_password_and_signs_out_everywhere(anonymous_client, outbox):
    client = anonymous_client
    assert signup(client)["email"] == "reset.user@example.com"
    refresh = login(client).json()["refresh_token"]

    request = client.post("/v1/auth/password-reset/request", json={"email": " RESET.user@example.com "})
    assert request.status_code == 202
    assert len(outbox.messages) == 1 and outbox.messages[0][0] == "reset.user@example.com"

    token = outbox.token()
    assert client.post("/v1/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}).status_code == 204

    assert login(client, password=PASSWORD).status_code == 401
    assert login(client, password=NEW_PASSWORD).status_code == 200
    assert client.post("/v1/auth/refresh", json={"refresh_token": refresh}).status_code == 401
    reuse = client.post("/v1/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD + "x"})
    assert reuse.status_code == 400


def test_unknown_address_gets_the_same_answer_and_no_email(anonymous_client, outbox):
    signup(anonymous_client)
    known = anonymous_client.post("/v1/auth/password-reset/request", json={"email": "reset.user@example.com"})
    unknown = anonymous_client.post("/v1/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(outbox.messages) == 1


def test_new_request_invalidates_the_previous_link(anonymous_client, outbox):
    signup(anonymous_client)
    for _ in range(2):
        anonymous_client.post("/v1/auth/password-reset/request", json={"email": "reset.user@example.com"})
    first, second = outbox.token(0), outbox.token(1)
    body = {"new_password": NEW_PASSWORD}
    assert anonymous_client.post("/v1/auth/password-reset/confirm", json={"token": first, **body}).status_code == 400
    assert anonymous_client.post("/v1/auth/password-reset/confirm", json={"token": second, **body}).status_code == 204


def test_expired_and_malformed_tokens_are_rejected(anonymous_client, outbox, db_session):
    signup(anonymous_client)
    anonymous_client.post("/v1/auth/password-reset/request", json={"email": "reset.user@example.com"})
    stored = db_session.scalar(select(PasswordResetToken))
    assert stored.token_hash != outbox.token()  # only the hash is stored
    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    body = {"new_password": NEW_PASSWORD}
    assert anonymous_client.post("/v1/auth/password-reset/confirm", json={"token": outbox.token(), **body}).status_code == 400
    assert anonymous_client.post("/v1/auth/password-reset/confirm", json={"token": "x" * 40, **body}).status_code == 400
    assert anonymous_client.post("/v1/auth/password-reset/confirm", json={"token": "short", **body}).status_code == 422
    assert anonymous_client.post("/v1/auth/password-reset/confirm", json={"token": "x" * 40, "new_password": "short"}).status_code == 422


def test_request_is_refused_when_email_is_not_configured(anonymous_client, monkeypatch):
    monkeypatch.setattr(auth_settings, "email_backend", "disabled")
    response = anonymous_client.post("/v1/auth/password-reset/request", json={"email": "a@example.com"})
    assert response.status_code == 503


def test_email_must_be_unique_and_changing_it_needs_the_password(anonymous_client, db_session):
    first = signup(anonymous_client)
    assert anonymous_client.post("/v1/auth/signup", json={
        "username": "second_user", "password": PASSWORD, "email": "RESET.user@example.com"}).status_code == 409
    token = login(anonymous_client).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    wrong = anonymous_client.put("/v1/auth/me/email", headers=auth, json={"email": "new@example.com", "password": "wrong password here"})
    assert wrong.status_code == 403
    changed = anonymous_client.put("/v1/auth/me/email", headers=auth, json={"email": "New@Example.com", "password": PASSWORD})
    assert changed.status_code == 200 and changed.json()["email"] == "new@example.com"
    assert db_session.scalar(select(User.email).where(User.id == first["id"])) == "new@example.com"


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "two@@example.com", "x" * 250 + "@example.com"])
def test_bad_emails_are_rejected_at_signup(anonymous_client, email):
    assert anonymous_client.post("/v1/auth/signup", json={"username": "bad_mail", "password": PASSWORD, "email": email}).status_code == 422
