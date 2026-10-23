from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestFormStrict
from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from workwise.api.dependencies import DbSession
from workwise.db.models import User
from workwise.api.schemas import RefreshRequest, TokenRead, UserRead, UserSignup
from workwise.core.security import CurrentUser, dummy_password_hash, password_hasher
from workwise.core.auth_limits import throttle
from workwise.core.auth_sessions import issue, locked_user, revoke_all, rotate


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", status_code=201, response_model=UserRead)
def signup(payload: UserSignup, db: DbSession, request: Request, response: Response) -> User:
    throttle(db,request,"signup")
    response.headers["Cache-Control"]="no-store"
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
    form: Annotated[OAuth2PasswordRequestFormStrict, Depends()], db: DbSession, response: Response, request: Request,
) -> TokenRead:
    throttle(db,request,"login",account=form.username)
    unauthorized = HTTPException(
        status_code=401, detail="Incorrect username or password",
        headers={"WWW-Authenticate": "Bearer", "Cache-Control":"no-store"},
    )
    if len(form.username) > 100 or not 1 <= len(form.password) <= 128:
        raise unauthorized
    user = db.scalar(select(User).where(User.username == form.username.strip().lower()))
    valid_password = password_hasher.verify(form.password, user.password_hash if user else dummy_password_hash)
    if user is None or not valid_password or not user.is_active:
        raise unauthorized
    checked_hash=user.password_hash
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    user=locked_user(db,user.id)
    if user is None or not user.is_active or (user.password_hash != checked_hash and not password_hasher.verify(form.password,user.password_hash)):
        raise unauthorized
    return issue(db,user)


@router.post("/refresh",response_model=TokenRead)
def refresh(payload: RefreshRequest, db: DbSession, request: Request, response: Response):
    throttle(db,request,"refresh")
    response.headers["Cache-Control"]="no-store"
    response.headers["Pragma"]="no-cache"
    return rotate(db,payload.refresh_token.get_secret_value())


@router.post("/logout",status_code=204)
def logout(user: CurrentUser, db: DbSession):
    current=locked_user(db,user.id)
    if current is None or not current.is_active:
        raise HTTPException(401,"Could not validate credentials",headers={"WWW-Authenticate":"Bearer","Cache-Control":"no-store"})
    revoke_all(db,current)
    return Response(status_code=204,headers={"Cache-Control":"no-store"})


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser, response: Response) -> User:
    response.headers["Cache-Control"]="no-store"
    return user
