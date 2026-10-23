from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter


class JobCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: Literal["job.created.v1"] = "job.created.v1"
    occurred_at: AwareDatetime
    job_id: int = Field(gt=0, strict=True)
    company_id: int = Field(gt=0, strict=True)


class JobCreatedV2(JobCreated):
    event_type: Literal["job.created.v2"] = "job.created.v2"
    recipient_id: int = Field(gt=0, strict=True)


class ApplicationChanged(JobCreated):
    event_type: Literal["application.changed.v1"] = "application.changed.v1"
    application_id: int = Field(gt=0, strict=True)
    application_version: int = Field(gt=0, strict=True)
    recipient_id: int = Field(gt=0, strict=True)
    status: Literal["applied", "shortlisted", "rejected", "hired"]


event_adapter = TypeAdapter(Annotated[JobCreated | JobCreatedV2 | ApplicationChanged, Field(discriminator="event_type")])


def parse_event(body: bytes) -> JobCreated | JobCreatedV2 | ApplicationChanged:
    return event_adapter.validate_json(body)


def job_created(job_id: int, company_id: int, recipient_id: int | None = None) -> JobCreated | JobCreatedV2:
    values = dict(event_id=uuid4(), occurred_at=datetime.now(timezone.utc), job_id=job_id, company_id=company_id)
    return JobCreatedV2(**values, recipient_id=recipient_id) if recipient_id is not None else JobCreated(**values)
