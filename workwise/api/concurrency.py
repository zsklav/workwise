"""Public API revision checks and translation of atomic ORM write conflicts."""
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from workwise.db.models import Company, Job

CONFLICT_DETAIL = "Resource has changed; fetch it again and retry with the latest version"


def require_version(resource: Company | Job, expected: int) -> None:
    if resource.version != expected:
        raise HTTPException(409, CONFLICT_DETAIL)


def commit_versioned(db: Session) -> None:
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        raise HTTPException(409, CONFLICT_DETAIL) from None
