"""Job CRUD routes and their transaction/ownership checks."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.errors import ForeignKeyViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, lazyload

from workwise.api.dependencies import DbSession
from workwise.api.concurrency import commit_versioned, require_version
from workwise.api.idempotency import IdempotencyKey, reserve, response as keyed_response
from workwise.api.schemas import JobCreate, JobRead, JobReplace, JobUpdate
from workwise.core.security import CurrentUser, require_company_access
from workwise.core.tracing import capture_context
from workwise.db.models import Company, Job, JobApplication, OutboxEvent
from workwise.messaging.events import job_created

router = APIRouter()


def require_company(company_id: int, db: Session, user) -> None:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    require_company_access(company, user)


def commit_job(job: Job, db: Session, *, recipient_id: int | None = None, reservation=None) -> dict | None:
    snapshot = None
    try:
        if recipient_id is not None:
            db.flush()
            event = job_created(job.id, job.company_id, recipient_id)
            db.add(OutboxEvent(id=event.event_id, payload=event.model_dump(mode="json"),
                               created_at=event.occurred_at, trace_context=capture_context()))
        if reservation is not None:
            snapshot = JobRead.model_validate(job).model_dump(mode="json")
            reservation.response_body = snapshot
        commit_versioned(db)
    except IntegrityError as error:
        db.rollback()
        if (
            isinstance(error.orig, ForeignKeyViolation)
            and error.orig.diag.constraint_name == "fk_jobs_company_id_companies"
        ):
            raise HTTPException(status_code=404, detail="Company not found") from error
        if isinstance(error.orig, ForeignKeyViolation) and error.orig.diag.constraint_name == "fk_application_job":
            raise HTTPException(409, "Job has applications and cannot be deleted") from error
        raise
    db.refresh(job)
    return snapshot


def protect_company_assignment(job, destination, version, db):
    if job.company_id != destination:
        db.scalar(select(Job).where(Job.id == job.id).options(lazyload(Job.employer)).with_for_update(of=Job).execution_options(populate_existing=True))
        require_version(job, version)
        if db.scalar(select(JobApplication.id).where(JobApplication.job_id == job.id).limit(1)) is not None:
            raise HTTPException(409, "A job with applications cannot change company")


@router.get("/jobs", response_model=list[JobRead])
def get_jobs(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    company: Annotated[
        str | None, Query(min_length=1, description="Exact company name, case-sensitive")
    ] = None,
    company_id: Annotated[int | None, Query(gt=0)] = None,
) -> list[Job]:
    statement = select(Job).order_by(Job.id)
    if company is not None:
        statement = statement.where(Job.employer.has(Company.name == company))
    if company_id is not None:
        statement = statement.where(Job.company_id == company_id)
    statement = statement.offset(offset).limit(limit)
    return list(db.scalars(statement).all())


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: int, db: DbSession) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/jobs", status_code=201, response_model=JobRead)
def create_job(job: JobCreate, db: DbSession, user: CurrentUser, idempotency_key: IdempotencyKey = None):
    record, replay = reserve(db, user.id, "jobs.create", idempotency_key, job.model_dump(mode="json"))
    if replay is not None:
        return replay
    require_company(job.company_id, db, user)
    new_job = Job(**job.model_dump())
    db.add(new_job)
    snapshot = commit_job(new_job, db, recipient_id=user.id, reservation=record)
    return keyed_response(snapshot, replayed=False) if record is not None else new_job


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, db: DbSession, user: CurrentUser,
               version: Annotated[int, Query(gt=0, description="Last observed job version")]) -> Response:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    require_company_access(job.employer, user)
    require_version(job, version)
    db.delete(job)
    try:
        commit_versioned(db)
    except IntegrityError as error:
        db.rollback()
        if isinstance(error.orig, ForeignKeyViolation) and error.orig.diag.constraint_name == "fk_application_job":
            raise HTTPException(409, "Job has applications and cannot be deleted") from error
        raise
    return Response(status_code=204)


@router.put("/jobs/{job_id}", response_model=JobRead)
def update_job(job_id: int, updated_job: JobReplace, db: DbSession, user: CurrentUser) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    require_company_access(job.employer, user)
    require_version(job, updated_job.version)
    protect_company_assignment(job, updated_job.company_id, updated_job.version, db)
    require_company(updated_job.company_id, db, user)
    job.title = updated_job.title
    job.company_id = updated_job.company_id
    job.location = updated_job.location
    job.description = updated_job.description
    job.version += 1
    commit_job(job, db)
    return job


@router.patch("/jobs/{job_id}", response_model=JobRead)
def patch_job(job_id: int, updated_job: JobUpdate, db: DbSession, user: CurrentUser) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    require_company_access(job.employer, user)
    require_version(job, updated_job.version)
    changes = updated_job.model_dump(exclude_unset=True, exclude={"version"})
    if "company_id" in changes:
        protect_company_assignment(job, changes["company_id"], updated_job.version, db)
        require_company(changes["company_id"], db, user)
    for field, value in changes.items():
        setattr(job, field, value)
    job.version += 1
    commit_job(job, db)

    return job
