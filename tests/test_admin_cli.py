import sys
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import manage_users
from models import User
from security import password_hasher


def test_local_cli_creates_admin_with_hashed_password(test_engine, monkeypatch):
    username = f"cli_{uuid4().hex}"
    password = "A private CLI test password!"
    prompts = iter([password, password])
    monkeypatch.setattr(sys, "argv", ["manage_users.py", "create-admin", username])
    monkeypatch.setattr(manage_users, "getpass", lambda prompt: next(prompts))
    # Keep the operator workflow in the dedicated test DB; credentials stay local.
    monkeypatch.setattr(manage_users, "create_engine", lambda *args, **kwargs: test_engine)
    try:
        assert manage_users.main() == 0
        with Session(test_engine) as session:
            user = session.scalar(select(User).where(User.username == username))
            assert user.role == "admin" and user.is_active
            assert user.password_hash != password
            assert password_hasher.verify(password, user.password_hash)
    finally:
        with test_engine.begin() as connection:
            connection.execute(delete(User).where(User.username == username))
