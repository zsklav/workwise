from datetime import datetime, timedelta, timezone
from typing import Annotated
import secrets

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
import jwt
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from database import get_db
from models import Company, User
from settings import AuthSettings


auth_settings = AuthSettings()
password_hasher = PasswordHash.recommended()
# Unknown users still perform a password verification operation.
dummy_password_hash = password_hasher.hash(secrets.token_urlsafe(32))
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": str(user_id), "iat": now, "nbf": now,
        "exp": now + timedelta(minutes=auth_settings.access_token_expire_minutes),
        "iss": auth_settings.jwt_issuer, "aud": auth_settings.jwt_audience,
        "type": "access",
    }, auth_settings.jwt_secret.get_secret_value(), algorithm="HS256")


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    unauthorized = HTTPException(
        status_code=401, detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        claims = jwt.decode(
            token, auth_settings.jwt_secret.get_secret_value(), algorithms=["HS256"],
            issuer=auth_settings.jwt_issuer, audience=auth_settings.jwt_audience,
            options={"require": ["sub", "exp", "iat", "nbf", "iss", "aud", "type"]},
        )
        if any(type(claims[field]) is not int for field in ("iat", "nbf", "exp")):
            raise unauthorized
        subject = claims["sub"]
        if (
            claims["type"] != "access" or not isinstance(subject, str)
            or not subject.isascii() or not subject.isdecimal()
            or len(subject) > 10 or not 1 <= int(subject) <= 2147483647
        ):
            raise unauthorized
        user_id = int(subject)
    except (InvalidTokenError, TypeError, ValueError, OverflowError):
        raise unauthorized from None

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_company_access(company: Company, user: User) -> None:
    # Null owners are legacy records: only administrators may manage them.
    if user.role != "admin" and company.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not allowed to manage this company")
