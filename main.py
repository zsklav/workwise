from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import Job
from schemas import JobCreate, JobRead, JobUpdate


app = FastAPI(title="WorkWise API")

DbSession = Annotated[Session, Depends(get_db)]


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
) -> list[Job]:
    statement = select(Job).order_by(Job.id)
    if company is not None:
        statement = statement.where(Job.company == company)
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
    new_job = Job(**job.model_dump())
    db.add(new_job)
    db.commit()
    db.refresh(new_job)
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

    job.title = updated_job.title
    job.company = updated_job.company
    job.location = updated_job.location
    job.description = updated_job.description
    db.commit()
    db.refresh(job)
    return job


@app.patch("/jobs/{job_id}", response_model=JobRead)
def patch_job(job_id: int, updated_job: JobUpdate, db: DbSession) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    changes = updated_job.model_dump(exclude_unset=True)
    if changes:
        for field, value in changes.items():
            setattr(job, field, value)
        db.commit()
        db.refresh(job)

    return job
