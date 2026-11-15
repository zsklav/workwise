"""Email-based password reset with single-use, expiring, hashed tokens."""
from datetime import datetime, timedelta, timezone
import secrets
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select, update

from workwise.api.dependencies import DbSession
from workwise.api.schemas import PasswordResetConfirm, PasswordResetRequest
from workwise.core.auth_limits import throttle
from workwise.core.auth_sessions import digest, locked_user, revoke_all
from workwise.core.mailer import EmailSender, deliver, get_email_sender
from workwise.core.security import auth_settings, password_hasher
from workwise.db.models import PasswordResetToken, User

router = APIRouter(prefix="/auth/password-reset", tags=["auth"])
Sender = Annotated[EmailSender | None, Depends(get_email_sender)]
NO_STORE = {"Cache-Control": "no-store"}
GENERIC = {"detail": "If the address is registered, a reset link has been sent."}


@router.post("/request", status_code=202)
def request_reset(payload: PasswordResetRequest, db: DbSession, request: Request, response: Response,
                  background: BackgroundTasks, sender: Sender) -> dict:
    throttle(db, request, "reset", account=payload.email)
    response.headers.update(NO_STORE)
    if sender is None:
        raise HTTPException(503, "Password reset is not configured", headers=NO_STORE)
    user = db.scalar(select(User).where(User.email == payload.email))
    # The answer is identical for unknown, inactive and valid addresses.
    if user is not None and user.is_active:
        now = datetime.now(timezone.utc)
        raw = secrets.token_urlsafe(32)
        db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                                                    PasswordResetToken.used_at.is_(None)).values(used_at=now))
        db.add(PasswordResetToken(id=uuid4(), user_id=user.id, token_hash=digest(raw), created_at=now,
                                  expires_at=now + timedelta(minutes=auth_settings.password_reset_minutes)))
        db.commit()
        link = f"{auth_settings.password_reset_url}?token={raw}"
        body = (f"Someone asked to reset the WorkWise password for this address.\n\n{link}\n\n"
                f"The link works once and expires in {auth_settings.password_reset_minutes} minutes. "
                "If this was not you, ignore this email.")
        background.add_task(deliver, sender, user.email, "Reset your WorkWise password", body)
    return GENERIC


@router.post("/confirm", status_code=204)
def confirm_reset(payload: PasswordResetConfirm, db: DbSession, request: Request) -> Response:
    throttle(db, request, "reset")
    invalid = HTTPException(400, "Invalid or expired reset token", headers=NO_STORE)
    found = db.scalar(select(PasswordResetToken).where(
        PasswordResetToken.token_hash == digest(payload.token.get_secret_value())))
    if found is None:
        raise invalid
    # Same lock order as login and refresh: user first, then the token row.
    user = locked_user(db, found.user_id)
    token = db.scalar(select(PasswordResetToken).where(PasswordResetToken.id == found.id)
                      .with_for_update().execution_options(populate_existing=True))
    now = datetime.now(timezone.utc)
    if user is None or not user.is_active or token is None or token.used_at is not None or token.expires_at <= now:
        db.rollback()
        raise invalid
    user.password_hash = password_hasher.hash(payload.new_password.get_secret_value())
    db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                                                PasswordResetToken.used_at.is_(None)).values(used_at=now))
    revoke_all(db, user)  # signs out every session and commits this transaction
    return Response(status_code=204, headers=NO_STORE)
