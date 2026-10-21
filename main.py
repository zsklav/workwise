from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from psycopg.errors import ForeignKeyViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import Company, Job
from routers.companies import router as companies_router
from schemas import JobCreate, JobRead, JobUpdate


app = FastAPI(title="WorkWise API")
app.include_router(companies_router)

DbSession = Annotated[Session, Depends(get_db)]


def require_company(company_id: int, db: Session) -> None:
    if db.get(Company, company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found")


def commit_job(job: Job, db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if (
            isinstance(error.orig, ForeignKeyViolation)
            and error.orig.diag.constraint_name == "fk_jobs_company_id_companies"
        ):
            raise HTTPException(status_code=404, detail="Company not found") from error
        raise
    db.refresh(job)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "OK"}


@app.get("/jobs", response_model=list[JobRead])
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


@app.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: int, db: DbSession) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/jobs", status_code=201, response_model=JobRead)
def create_job(job: JobCreate, db: DbSession) -> Job:
    require_company(job.company_id, db)
    new_job = Job(**job.model_dump())
    db.add(new_job)
    commit_job(new_job, db)
    return new_job


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, db: DbSession) -> Response:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    db.delete(job)
    db.commit()
    return Response(status_code=204)


@app.put("/jobs/{job_id}", response_model=JobRead)
def update_job(job_id: int, updated_job: JobCreate, db: DbSession) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    require_company(updated_job.company_id, db)
    job.title = updated_job.title
    job.company_id = updated_job.company_id
    job.location = updated_job.location
    job.description = updated_job.description
    commit_job(job, db)
    return job


@app.patch("/jobs/{job_id}", response_model=JobRead)
def patch_job(job_id: int, updated_job: JobUpdate, db: DbSession) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    changes = updated_job.model_dump(exclude_unset=True)
    if "company_id" in changes:
        require_company(changes["company_id"], db)
    if changes:
        for field, value in changes.items():
            setattr(job, field, value)
        commit_job(job, db)

    return job
