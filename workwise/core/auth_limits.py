"""Atomic PostgreSQL fixed windows, committed before expensive credential checks."""
import hashlib
import hmac

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from workwise.core.security import auth_settings


def bucket_key(scope, identity):
    return hmac.new(auth_settings.jwt_secret.get_secret_value().encode(),
                    ("auth-rate:"+scope+":"+identity).encode(), hashlib.sha256).hexdigest()


def reserve(db, key, limit):
    return db.execute(text("""INSERT INTO auth_rate_limits(bucket_hash,attempts,window_end)
        VALUES (:key,1,statement_timestamp()+make_interval(secs => :seconds))
        ON CONFLICT (bucket_hash) DO UPDATE SET
        attempts=CASE WHEN auth_rate_limits.window_end <= statement_timestamp() THEN 1
                      ELSE LEAST(auth_rate_limits.attempts+1,:cap) END,
        window_end=CASE WHEN auth_rate_limits.window_end <= statement_timestamp()
                       THEN statement_timestamp()+make_interval(secs => :seconds) ELSE auth_rate_limits.window_end END
        RETURNING attempts,GREATEST(1,CEIL(EXTRACT(EPOCH FROM (window_end-statement_timestamp())))) AS retry_after"""),
        {"key":key,"seconds":auth_settings.auth_rate_window_seconds,"cap":limit+1}).mappings().one()


def throttle(db, request: Request, action, *, account=None):
    ip = request.client.host if request.client else "unknown-peer"
    limits={"login":auth_settings.auth_login_ip_limit,"signup":auth_settings.auth_signup_ip_limit,
            "refresh":auth_settings.auth_refresh_ip_limit}
    retry=None
    try:
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        row=reserve(db,bucket_key(action+":ip",ip),limits[action])
        if row["attempts"] > limits[action]:
            retry=int(row["retry_after"])
        elif account is not None:
            row=reserve(db,bucket_key(action+":account",account.strip().lower()[:100]),auth_settings.auth_login_account_limit)
            if row["attempts"] > auth_settings.auth_login_account_limit:
                retry=int(row["retry_after"])
        # Failed credential checks must not roll these reservations back.
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503,"Authentication temporarily unavailable",headers={"Retry-After":"5","Cache-Control":"no-store"}) from None
    if retry is not None:
        raise HTTPException(429,"Too many authentication attempts",headers={"Retry-After":str(retry),"Cache-Control":"no-store"})
