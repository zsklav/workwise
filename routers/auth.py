from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import OAuth2PasswordRequestFormStrict
from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import User
from schemas import TokenRead, UserRead, UserSignup
from security import CurrentUser, auth_settings, create_access_token, dummy_password_hash, password_hasher


router = APIRouter(prefix="/auth", tags=["auth"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("/signup", status_code=201, response_model=UserRead)
def signup(payload: UserSignup, db: DbSession) -> User:
    user = User(
        username=payload.username,
        password_hash=password_hasher.hash(payload.password.get_secret_value()),
        role="member", is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if isinstance(error.orig, UniqueViolation) and error.orig.diag.constraint_name == "uq_users_username":
            raise HTTPException(status_code=409, detail="Username already exists") from error
        raise
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenRead)
def login(
    form: Annotated[OAuth2PasswordRequestFormStrict, Depends()], db: DbSession, response: Response,
) -> TokenRead:
    unauthorized = HTTPException(
        status_code=401, detail="Incorrect username or password",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if len(form.username) > 100 or not 1 <= len(form.password) <= 128:
        raise unauthorized
    user = db.scalar(select(User).where(User.username == form.username.strip().lower()))
    valid_password = password_hasher.verify(form.password, user.password_hash if user else dummy_password_hash)
    if user is None or not valid_password or not user.is_active:
        raise unauthorized
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenRead(
        access_token=create_access_token(user.id),
        expires_in=auth_settings.access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser) -> User:
    return user
