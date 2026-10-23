from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from workwise.api.main import app
from workwise.core.auth_limits import bucket_key
from workwise.core.auth_sessions import digest
from workwise.core.security import auth_settings, password_hasher
from workwise.db.models import AuthRateLimit, RefreshToken, User
from workwise.db.session import get_db

PASSWORD="Security test password with enough characters"


def login(client, username="session_owner"):
    return client.post("/auth/login",data={"grant_type":"password","username":username,"password":PASSWORD})


def registered(client):
    assert client.post("/auth/signup",json={"username":"session_owner","password":PASSWORD}).status_code==201
    result=login(client)
    assert result.status_code==200
    return result.json()


def test_refresh_rotates_hashes_and_preserves_absolute_expiry(anonymous_client,db_session):
    pair=registered(anonymous_client)
    original=db_session.scalars(select(RefreshToken)).one()
    expires=original.expires_at
    assert original.token_hash==digest(pair["refresh_token"])
    assert original.token_hash!=pair["refresh_token"]
    response=anonymous_client.post("/auth/refresh",json={"refresh_token":pair["refresh_token"]})
    assert response.status_code==200
    assert response.headers["cache-control"]=="no-store"
    assert response.json()["refresh_token"]!=pair["refresh_token"]
    db_session.expire_all()
    tokens=db_session.scalars(select(RefreshToken)).all()
    assert len(tokens)==2 and sum(t.used_at is not None for t in tokens)==1
    assert all(t.expires_at==expires for t in tokens)
    assert anonymous_client.get("/auth/me",headers={"Authorization":"Bearer "+response.json()["access_token"]}).status_code==200


def test_reusing_consumed_refresh_revokes_account_sessions(anonymous_client,db_session):
    pair=registered(anonymous_client)
    rotated=anonymous_client.post("/auth/refresh",json={"refresh_token":pair["refresh_token"]}).json()
    reused=anonymous_client.post("/auth/refresh",json={"refresh_token":pair["refresh_token"]})
    assert reused.status_code==401 and reused.headers["cache-control"]=="no-store"
    for token in (pair,rotated):
        assert anonymous_client.get("/auth/me",headers={"Authorization":"Bearer "+token["access_token"]}).status_code==401
        assert anonymous_client.post("/auth/refresh",json={"refresh_token":token["refresh_token"]}).status_code==401
    db_session.expire_all()
    assert all(t.revoked_at is not None for t in db_session.scalars(select(RefreshToken)))
    assert login(anonymous_client).status_code==200


def test_logout_revokes_all_devices_and_new_login_works(anonymous_client):
    first=registered(anonymous_client)
    second=login(anonymous_client).json()
    response=anonymous_client.post("/auth/logout",headers={"Authorization":"Bearer "+first["access_token"]})
    assert response.status_code==204 and response.content==b""
    for pair in (first,second):
        assert anonymous_client.get("/auth/me",headers={"Authorization":"Bearer "+pair["access_token"]}).status_code==401
        assert anonymous_client.post("/auth/refresh",json={"refresh_token":pair["refresh_token"]}).status_code==401
    new=login(anonymous_client).json()
    assert anonymous_client.get("/auth/me",headers={"Authorization":"Bearer "+new["access_token"]}).status_code==200
    assert anonymous_client.post("/auth/logout").status_code==401


@pytest.mark.parametrize("state",["expired","disabled","revoked","epoch_changed"])
def test_refresh_rejects_invalid_session_state(anonymous_client,db_session,state):
    pair=registered(anonymous_client)
    token=db_session.scalars(select(RefreshToken)).one()
    user=db_session.get(User,token.user_id)
    if state=="expired": token.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
    elif state=="disabled": user.is_active=False
    elif state=="revoked": token.revoked_at=datetime.now(timezone.utc)
    else: user.token_version+=1
    db_session.commit()
    assert anonymous_client.post("/auth/refresh",json={"refresh_token":pair["refresh_token"]}).status_code==401


@pytest.mark.parametrize("payload",[{}, {"refresh_token":"secret-value"}, {"refresh_token":"é"*64},
                                     {"refresh_token":"a"*64,"role":"admin"}, "private-body"])
def test_refresh_validation_does_not_echo_credentials(anonymous_client,payload):
    response=anonymous_client.post("/auth/refresh",json=payload)
    assert response.status_code==422
    assert "secret-value" not in response.text and "private-body" not in response.text and "a"*64 not in response.text


@pytest.mark.parametrize("version",[None,True,"1",0,-1])
def test_access_token_epoch_claim_is_strict(anonymous_client,auth_user,version):
    from workwise.core.security import create_access_token
    claims=jwt.decode(create_access_token(auth_user.id),auth_settings.jwt_secret.get_secret_value(),algorithms=["HS256"],audience=auth_settings.jwt_audience)
    if version is None: del claims["ver"]
    else: claims["ver"]=version
    token=jwt.encode(claims,auth_settings.jwt_secret.get_secret_value(),algorithm="HS256")
    assert anonymous_client.get("/auth/me",headers={"Authorization":"Bearer "+token}).status_code==401


def test_account_limit_is_normalized_survives_failed_logins_and_expires(anonymous_client,db_connection,monkeypatch):
    monkeypatch.setattr(auth_settings,"auth_login_account_limit",2)
    for name in ("unknown_user"," UNKNOWN_USER "):
        assert anonymous_client.post("/auth/login",data={"grant_type":"password","username":name,"password":PASSWORD}).status_code==401
    response=login(anonymous_client,"Unknown_User")
    assert response.status_code==429 and 1<=int(response.headers["retry-after"])<=60
    assert response.headers["cache-control"]=="no-store"
    key=bucket_key("login:account","unknown_user")
    assert db_connection.scalar(text("SELECT attempts FROM auth_rate_limits WHERE bucket_hash=:key"),{"key":key})==3
    db_connection.execute(text("UPDATE auth_rate_limits SET window_end=now()-interval '1 second' WHERE bucket_hash=:key"),{"key":key})
    assert login(anonymous_client,"unknown_user").status_code==401
    assert db_connection.scalar(text("SELECT attempts FROM auth_rate_limits WHERE bucket_hash=:key"),{"key":key})==1


def test_ip_limit_ignores_forwarded_headers_and_limits_distinct_accounts(anonymous_client,monkeypatch):
    monkeypatch.setattr(auth_settings,"auth_login_ip_limit",2)
    for index in range(2):
        assert anonymous_client.post("/auth/login",data={"grant_type":"password","username":f"unknown_{index}","password":PASSWORD},headers={"X-Forwarded-For":f"192.0.2.{index}"}).status_code==401
    assert login(anonymous_client,"another_account").status_code==429


def test_signup_is_throttled_before_hashing(anonymous_client,monkeypatch):
    monkeypatch.setattr(auth_settings,"auth_signup_ip_limit",1)
    assert anonymous_client.post("/auth/signup",json={"username":"first_user","password":PASSWORD}).status_code==201
    monkeypatch.setattr(password_hasher,"hash",lambda value: (_ for _ in ()).throw(AssertionError("Hashing must not run")))
    assert anonymous_client.post("/auth/signup",json={"username":"second_user","password":PASSWORD}).status_code==429


def test_refresh_rate_limit_and_unknown_token_are_safe(anonymous_client,monkeypatch):
    monkeypatch.setattr(auth_settings,"auth_refresh_ip_limit",1)
    payload={"refresh_token":"a"*64}
    result=anonymous_client.post("/auth/refresh",json=payload)
    assert result.status_code==401 and "a"*64 not in result.text
    assert anonymous_client.post("/auth/refresh",json=payload).status_code==429


def test_auth_database_failure_is_fail_closed(anonymous_client,monkeypatch):
    from sqlalchemy.exc import OperationalError
    from workwise.core import auth_limits
    def broken(*args):
        raise OperationalError("private-sql",{},Exception("private-password"))
    monkeypatch.setattr(auth_limits,"reserve",broken)
    response=login(anonymous_client)
    assert response.status_code==503 and response.headers["retry-after"]=="5"
    assert "private" not in response.text


def test_concurrent_login_requests_share_atomic_limit(test_engine,monkeypatch):
    monkeypatch.setattr(auth_settings,"auth_login_account_limit",3)
    name="limit_"+uuid4().hex
    previous=app.dependency_overrides.copy()
    def database():
        with Session(test_engine) as session: yield session
    app.dependency_overrides[get_db]=database
    try:
        with TestClient(app,client=("198.51.100.41",41000)) as client:
            barrier=Barrier(6)
            def attempt(_):
                barrier.wait(timeout=10)
                return login(client,name).status_code
            with ThreadPoolExecutor(max_workers=6) as pool:
                statuses=list(pool.map(attempt,range(6)))
            assert sorted(statuses)==[401,401,401,429,429,429]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_concurrent_refresh_rotation_detects_reuse(test_engine):
    username="refresh_"+uuid4().hex
    user_id=None
    previous=app.dependency_overrides.copy()
    def database():
        with Session(test_engine) as session: yield session
    app.dependency_overrides[get_db]=database
    try:
        with test_engine.begin() as connection:
            user_id=connection.scalar(text("INSERT INTO users(username,password_hash) VALUES (:name,:hash) RETURNING id"),{"name":username,"hash":password_hasher.hash(PASSWORD)})
        with TestClient(app,client=("198.51.100.42",42000)) as client:
            pair=login(client,username).json()
            barrier=Barrier(2)
            def refresh(_):
                barrier.wait(timeout=10)
                return client.post("/auth/refresh",json={"refresh_token":pair["refresh_token"]})
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(refresh,range(2)))
            assert sorted(r.status_code for r in results)==[200,401]
            winner=next(r.json() for r in results if r.status_code==200)
            assert client.get("/auth/me",headers={"Authorization":"Bearer "+winner["access_token"]}).status_code==401
            assert client.post("/auth/refresh",json={"refresh_token":winner["refresh_token"]}).status_code==401
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        if user_id is not None:
            with test_engine.begin() as connection: connection.execute(delete(User).where(User.id==user_id))
