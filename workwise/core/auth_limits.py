"""Atomic PostgreSQL fixed windows, committed before expensive credential checks."""
import hashlib
from ipaddress import ip_address, ip_network
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


def client_ip(request: Request) -> str:
    """Peer address, or the real client when the peer is a trusted proxy.

    X-Forwarded-For is read right to left and the first address that is not a
    trusted proxy wins, so entries a client prepends cannot be used to spoof.
    """
    peer = request.client.host if request.client else "unknown-peer"
    trusted = [ip_network(item.strip()) for item in auth_settings.trusted_proxies.split(",") if item.strip()]
    if not trusted:
        return peer

    def is_trusted(value):
        try:
            return any(ip_address(value) in network for network in trusted)
        except ValueError:
            return False

    if not is_trusted(peer):
        return peer
    for hop in reversed(request.headers.get("x-forwarded-for", "").split(",")):
        hop = hop.strip()
        if not hop:
            continue
        try:
            ip_address(hop)
        except ValueError:
            return peer  # malformed header: fall back to the proxy address
        if not is_trusted(hop):
            return hop
    return peer


def throttle(db, request: Request, action, *, account=None):
    ip = client_ip(request)
    limits={"login":auth_settings.auth_login_ip_limit,"signup":auth_settings.auth_signup_ip_limit,
            "refresh":auth_settings.auth_refresh_ip_limit,"api":auth_settings.api_ip_limit,"reset":auth_settings.auth_reset_ip_limit}
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
        raise HTTPException(503,"Rate limiting temporarily unavailable" if action == "api" else "Authentication temporarily unavailable",headers={"Retry-After":"5","Cache-Control":"no-store"}) from None
    if retry is not None:
        raise HTTPException(429,"Too many requests" if action == "api" else "Too many authentication attempts",headers={"Retry-After":str(retry),"Cache-Control":"no-store"})
