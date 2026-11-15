"""Job CRUD routes and their transaction/ownership checks."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.errors import CheckViolation, ForeignKeyViolation
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, lazyload

from workwise.api.dependencies import DbSession
from workwise.api.concurrency import commit_versioned, require_version
from workwise.api.idempotency import IdempotencyKey, reserve, response as keyed_response
from workwise.api.schemas import JobCreate, JobRead, JobReplace, JobType, JobUpdate
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
        if isinstance(error.orig, CheckViolation) and error.orig.diag.constraint_name == "ck_jobs_salary_range":
            raise HTTPException(422, "salary_max must not be below salary_min") from error
        raise
    db.refresh(job)
    return snapshot


def protect_company_assignment(job, destination, version, db):
    if job.company_id != destination:
        db.scalar(select(Job).where(Job.id == job.id).options(lazyload(Job.employer)).with_for_update(of=Job).execution_options(populate_existing=True))
        require_version(job, version)
        if db.scalar(select(JobApplication.id).where(JobApplication.job_id == job.id).limit(1)) is not None:
            raise HTTPException(409, "A job with applications cannot change company")


def find_job(db: Session, job_id: int) -> Job:
    """Return a live job; soft-deleted jobs look missing."""
    job = db.get(Job, job_id)
    if job is None or job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs", response_model=list[JobRead])
def get_jobs(
    db: DbSession,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    company: Annotated[
        str | None, Query(min_length=1, description="Exact company name, case-sensitive")
    ] = None,
    company_id: Annotated[int | None, Query(gt=0)] = None,
    q: Annotated[
        str | None, Query(min_length=1, max_length=100, description="Case-insensitive title/description search")
    ] = None,
    sort: Literal["id", "-id", "title", "-title", "salary_max", "-salary_max"] = "id",
    remote: bool | None = None,
    job_type: JobType | None = None,
    min_salary: Annotated[int | None, Query(ge=0, description="Jobs whose maximum pay is at least this")] = None,
) -> list[Job]:
    statement = select(Job).where(Job.deleted_at.is_(None))
    if company is not None:
        statement = statement.where(Job.employer.has(Company.name == company))
    if company_id is not None:
        statement = statement.where(Job.company_id == company_id)
    if remote is not None:
        statement = statement.where(Job.remote.is_(remote))
    if job_type is not None:
        statement = statement.where(Job.job_type == job_type)
    if min_salary is not None:
        statement = statement.where(Job.salary_max >= min_salary)
    if q is not None:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        statement = statement.where(or_(Job.title.ilike(pattern, escape="\\"),
                                        Job.description.ilike(pattern, escape="\\"),
                                        Job.location.ilike(pattern, escape="\\")))
    response.headers["X-Total-Count"] = str(db.scalar(select(func.count()).select_from(statement.subquery())))
    column = getattr(Job, sort.lstrip("-"))
    order = [(column.desc() if sort.startswith("-") else column.asc()).nulls_last()]
    if column is not Job.id:
        order.append(Job.id)  # keeps pages deterministic
    return list(db.scalars(statement.order_by(*order).offset(offset).limit(limit)).all())


@router.get("/jobs/deleted", response_model=list[JobRead])
def get_deleted_jobs(
    db: DbSession,
    user: CurrentUser,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Job]:
    """Administrator view of soft-deleted jobs, newest deletion first."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can list deleted jobs")
    statement = select(Job).where(Job.deleted_at.is_not(None))
    response.headers["X-Total-Count"] = str(db.scalar(select(func.count()).select_from(statement.subquery())))
    return list(db.scalars(statement.order_by(Job.deleted_at.desc(), Job.id.desc()).offset(offset).limit(limit)).all())


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: int, db: DbSession) -> Job:
    job = find_job(db, job_id)
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
    job = find_job(db, job_id)

    require_company_access(job.employer, user)
    require_version(job, version)
    job.deleted_at = datetime.now(timezone.utc)
    job.version += 1
    commit_versioned(db)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/restore", response_model=JobRead)
def restore_job(job_id: int, db: DbSession, user: CurrentUser,
                version: Annotated[int, Query(gt=0, description="Version returned when the job was deleted")]) -> Job:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can restore jobs")
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.deleted_at is None:
        raise HTTPException(status_code=409, detail="Job is not deleted")
    require_version(job, version)
    job.deleted_at = None
    job.version += 1
    commit_versioned(db)
    db.refresh(job)
    return job


@router.put("/jobs/{job_id}", response_model=JobRead)
def update_job(job_id: int, updated_job: JobReplace, db: DbSession, user: CurrentUser) -> Job:
    job = find_job(db, job_id)

    require_company_access(job.employer, user)
    require_version(job, updated_job.version)
    protect_company_assignment(job, updated_job.company_id, updated_job.version, db)
    require_company(updated_job.company_id, db, user)
    job.title = updated_job.title
    job.company_id = updated_job.company_id
    job.location = updated_job.location
    job.description = updated_job.description
    for field in ("salary_min", "salary_max", "remote", "job_type"):
        setattr(job, field, getattr(updated_job, field))
    job.version += 1
    commit_job(job, db)
    return job


@router.patch("/jobs/{job_id}", response_model=JobRead)
def patch_job(job_id: int, updated_job: JobUpdate, db: DbSession, user: CurrentUser) -> Job:
    job = find_job(db, job_id)

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
