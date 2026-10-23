"""Client-key reservation and response snapshot share the business transaction."""
import hashlib
import json
from typing import Annotated
from uuid import uuid4

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError

from workwise.db.models import IdempotencyRecord

IdempotencyKey = Annotated[str | None, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


def response(body, *, replayed):
    return JSONResponse(status_code=201, content=body,
                        headers={"Idempotency-Replayed": str(replayed).lower(), "Cache-Control": "no-store"})


def reserve(db, user_id, scope, key, payload):
    if key is None:
        return None, None
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    statement = insert(IdempotencyRecord).values(id=uuid4(), user_id=user_id, scope=scope, key_hash=key_hash,
        request_hash=request_hash, expires_at=func.statement_timestamp() + text("interval '24 hours'"))
    statement = statement.on_conflict_do_update(
        constraint="uq_idempotency_actor_scope_key",
        set_={"request_hash": statement.excluded.request_hash, "response_body": None, "expires_at": statement.excluded.expires_at},
        where=IdempotencyRecord.expires_at <= func.statement_timestamp(),
    ).returning(IdempotencyRecord).execution_options(populate_existing=True)
    try:
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        record = db.scalar(statement)
        if record is not None:
            return record, None
        record = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id, IdempotencyRecord.scope == scope,
            IdempotencyRecord.key_hash == key_hash).with_for_update().execution_options(populate_existing=True))
    except OperationalError as error:
        db.rollback()
        if getattr(error.orig, "sqlstate", None) in ("55P03", "57014"):
            raise HTTPException(409, "Request is still being processed; retry with the same key", headers={"Retry-After": "1"}) from None
        raise
    if record is None or record.response_body is None:
        raise HTTPException(503, "Request snapshot unavailable; retry later", headers={"Retry-After": "1"})
    if record.request_hash != request_hash:
        raise HTTPException(409, "Idempotency key was already used with a different request")
    return None, response(record.response_body, replayed=True)
