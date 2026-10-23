"""Opaque, hashed, single-use refresh tokens and account-wide logout epochs."""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update

from workwise.api.schemas import TokenRead
from workwise.core.security import auth_settings, create_access_token
from workwise.db.models import RefreshToken, User


def digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def locked_user(db, user_id):
    return db.scalar(select(User).where(User.id==user_id).with_for_update().execution_options(populate_existing=True))


def issue(db, user, *, expires_at=None):
    now=datetime.now(timezone.utc)
    expires_at=expires_at or now+timedelta(days=auth_settings.refresh_token_expire_days)
    raw=secrets.token_urlsafe(48)
    db.add(RefreshToken(id=uuid4(),user_id=user.id,token_hash=digest(raw),token_version=user.token_version,expires_at=expires_at))
    result=TokenRead(access_token=create_access_token(user.id,user.token_version),
                     expires_in=auth_settings.access_token_expire_minutes*60,refresh_token=raw,
                     refresh_expires_in=max(0,int((expires_at-now).total_seconds())))
    db.commit()
    return result


def revoke_all(db, user):
    user.token_version+=1
    db.execute(update(RefreshToken).where(RefreshToken.user_id==user.id,RefreshToken.revoked_at.is_(None))
               .values(revoked_at=datetime.now(timezone.utc)))
    db.commit()


def rotate(db, raw):
    unauthorized=HTTPException(401,"Invalid refresh token",headers={"WWW-Authenticate":"Bearer","Cache-Control":"no-store"})
    token=db.scalar(select(RefreshToken).where(RefreshToken.token_hash==digest(raw)))
    if token is None:
        raise unauthorized
    # Login, refresh and logout all take the user lock first to avoid lock cycles.
    user=locked_user(db,token.user_id)
    token=db.scalar(select(RefreshToken).where(RefreshToken.id==token.id).with_for_update().execution_options(populate_existing=True))
    now=datetime.now(timezone.utc)
    if user is None or not user.is_active or token is None or token.revoked_at is not None or token.expires_at <= now or token.token_version != user.token_version:
        raise unauthorized
    if token.used_at is not None:
        revoke_all(db,user)
        raise unauthorized
    token.used_at=now
    return issue(db,user,expires_at=token.expires_at)
