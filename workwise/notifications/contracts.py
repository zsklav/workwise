from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel


class NotificationRead(BaseModel):
    event_id: UUID
    job_id: int
    company_id: int
    message: str
    occurred_at: datetime
    created_at: datetime
    read_at: datetime | None


def service_token(user_id: int, secret: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "iat": now, "nbf": now,
                       "exp": now + timedelta(seconds=30), "iss": "workwise-api",
                       "aud": "workwise-notifications", "type": "internal"}, secret, algorithm="HS256")


def service_subject(token: str, secret: str) -> int:
    claims = jwt.decode(token, secret, algorithms=["HS256"], issuer="workwise-api",
                        audience="workwise-notifications",
                        options={"require": ["sub", "iat", "nbf", "exp", "iss", "aud", "type"]})
    subject = claims["sub"]
    if (claims["type"] != "internal" or not isinstance(subject, str) or not subject.isascii()
            or not subject.isdecimal() or len(subject) > 10 or not 1 <= int(subject) <= 2147483647
            or any(type(claims[key]) is not int for key in ("iat", "nbf", "exp"))
            or claims["exp"] - claims["iat"] > 30):
        raise InvalidTokenError("Invalid internal subject or lifetime")
    return int(subject)
