from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class JobCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: Literal["job.created.v1"] = "job.created.v1"
    occurred_at: AwareDatetime
    job_id: int = Field(gt=0, strict=True)
    company_id: int = Field(gt=0, strict=True)


def job_created(job_id: int, company_id: int) -> JobCreated:
    return JobCreated(event_id=uuid4(), occurred_at=datetime.now(timezone.utc),
                      job_id=job_id, company_id=company_id)
