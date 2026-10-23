"""Private applicant/employer views and version-guarded recruitment transitions."""
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm import lazyload

from workwise.api.concurrency import CONFLICT_DETAIL, commit_versioned, require_version
from workwise.api.dependencies import DbSession
from workwise.api.idempotency import IdempotencyKey, reserve, response as keyed_response
from workwise.api.schemas import ApplicationCreate, ApplicationRead, ApplicationReview, ApplicationStatus
from workwise.core.security import CurrentUser, require_company_access
from workwise.core.tracing import capture_context
from workwise.db.models import Job, JobApplication, OutboxEvent
from workwise.messaging.events import ApplicationChanged

router = APIRouter(tags=["applications"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
TRANSITIONS = {"applied": {"shortlisted", "rejected"}, "shortlisted": {"hired", "rejected"}, "rejected": set(), "hired": set()}


def find_job(db, job_id, *, lock=False):
    statement = select(Job).where(Job.id == job_id)
    if lock:
        # After waiting on a moved job, PostgreSQL can recheck the job tuple
        # against an earlier outer-join tuple. Load the company after the lock.
        statement = statement.options(lazyload(Job.employer)).with_for_update(of=Job).execution_options(populate_existing=True)
    job = db.scalar(statement)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def add_event(db, application):
    try:
        db.flush()
    except StaleDataError:
        db.rollback()
        raise HTTPException(409, CONFLICT_DETAIL) from None
    event = ApplicationChanged(event_id=uuid4(), occurred_at=datetime.now(timezone.utc),
        application_id=application.id, application_version=application.version,
        job_id=application.job_id, company_id=application.job.company_id,
        recipient_id=application.applicant_id, status=application.status)
    db.add(OutboxEvent(id=event.event_id, payload=event.model_dump(mode="json"),
                       created_at=event.occurred_at, trace_context=capture_context()))


@router.post("/jobs/{job_id}/applications", status_code=201, response_model=ApplicationRead)
def apply(job_id: int, payload: ApplicationCreate, db: DbSession, user: CurrentUser,
          response: Response, idempotency_key: IdempotencyKey = None):
    response.headers["Cache-Control"] = "no-store"
    record, replay = reserve(db, user.id, f"applications.create:{job_id}", idempotency_key, payload.model_dump(mode="json"))
    if replay is not None:
        return replay
    job = find_job(db, job_id, lock=True)
    if job.employer.owner_id == user.id:
        raise HTTPException(409, "You cannot apply to your own company's job")
    application = JobApplication(job_id=job.id, applicant_id=user.id, cover_letter=payload.cover_letter, status="applied")
    db.add(application)
    try:
        add_event(db, application)
        snapshot = ApplicationRead.model_validate(application).model_dump(mode="json")
        if record is not None:
            record.response_body = snapshot
        commit_versioned(db)
    except IntegrityError as error:
        db.rollback()
        if isinstance(error.orig, UniqueViolation) and error.orig.diag.constraint_name == "uq_application_job_applicant":
            raise HTTPException(409, "You already applied to this job") from error
        if isinstance(error.orig, ForeignKeyViolation):
            raise HTTPException(404, "Job or applicant no longer exists") from error
        raise
    return keyed_response(snapshot, replayed=False) if record is not None else snapshot


@router.get("/applications/mine", response_model=list[ApplicationRead])
def mine(db: DbSession, user: CurrentUser, response: Response, limit: Limit = 20, offset: Offset = 0,
         status: ApplicationStatus | None = None):
    response.headers["Cache-Control"] = "no-store"
    statement = select(JobApplication).where(JobApplication.applicant_id == user.id)
    if status is not None:
        statement = statement.where(JobApplication.status == status)
    return list(db.scalars(statement.order_by(JobApplication.id).offset(offset).limit(limit)))


@router.get("/jobs/{job_id}/applications", response_model=list[ApplicationRead])
def candidates(job_id: int, db: DbSession, user: CurrentUser, response: Response,
               limit: Limit = 20, offset: Offset = 0, status: ApplicationStatus | None = None):
    response.headers["Cache-Control"] = "no-store"
    job = find_job(db, job_id)
    require_company_access(job.employer, user)
    statement = select(JobApplication).where(JobApplication.job_id == job_id)
    if status is not None:
        statement = statement.where(JobApplication.status == status)
    return list(db.scalars(statement.order_by(JobApplication.id).offset(offset).limit(limit)))


def find_application(db, application_id):
    application = db.get(JobApplication, application_id)
    if application is None:
        raise HTTPException(404, "Application not found")
    return application


@router.get("/applications/{application_id}", response_model=ApplicationRead)
def get_application(application_id: int, db: DbSession, user: CurrentUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    application = find_application(db, application_id)
    if user.id != application.applicant_id and user.role != "admin" and application.job.employer.owner_id != user.id:
        raise HTTPException(404, "Application not found")
    return application


@router.patch("/applications/{application_id}", response_model=ApplicationRead)
def review(application_id: int, payload: ApplicationReview, db: DbSession, user: CurrentUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    application = find_application(db, application_id)
    require_company_access(application.job.employer, user)
    require_version(application, payload.version)
    if payload.status not in TRANSITIONS[application.status]:
        raise HTTPException(409, "Application status transition is not allowed")
    application.status = payload.status
    application.version += 1
    application.updated_at = datetime.now(timezone.utc)
    add_event(db, application)
    commit_versioned(db)
    db.refresh(application)
    return application
