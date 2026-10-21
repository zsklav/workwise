"""Create an administrator locally using migration credentials, never public signup."""

import argparse
from getpass import getpass

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import User
from schemas import UserSignup
from security import password_hasher
from settings import MigrationSettings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create-admin"])
    parser.add_argument("username")
    args = parser.parse_args()
    password = getpass("Admin password (15-128 characters): ")
    if password != getpass("Confirm password: "):
        print("Passwords do not match.")
        return 1
    try:
        payload = UserSignup(username=args.username, password=password)
    except ValidationError:
        print("Use a 3-50 character username (letters, digits, underscores) and a 15-128 character password.")
        return 1

    settings = MigrationSettings()
    url = URL.create(
        "postgresql+psycopg", username=settings.migration_db_user,
        password=settings.migration_db_password.get_secret_value(), host=settings.db_host,
        port=settings.db_port, database=settings.db_name,
    )
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with Session(engine) as session:
            session.add(User(username=payload.username, role="admin", is_active=True,
                             password_hash=password_hasher.hash(payload.password.get_secret_value())))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                print("Could not create admin; check whether the username already exists.")
                return 1
        print(f"Created admin {payload.username}. Log in through /auth/login.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
