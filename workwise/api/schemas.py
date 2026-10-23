from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    company_id: int = Field(gt=0, strict=True)
    location: str = Field(min_length=1)
    description: str | None = None


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    title: str
    company_id: int
    company: str
    location: str
    description: str | None = None


class JobReplace(JobCreate):
    version: int = Field(gt=0, strict=True, description="Version returned by the last job read.")


class JobUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"version": 1, "title": "Python Developer"}]},
    )

    version: int = Field(gt=0, strict=True, description="Version returned by the last job read.")
    title: str | None = Field(
        default=None, min_length=1, description="May be omitted; explicit null is rejected."
    )
    company_id: int | None = Field(
        default=None, gt=0, strict=True, description="May be omitted; explicit null is rejected."
    )
    location: str | None = Field(
        default=None, min_length=1, description="May be omitted; explicit null is rejected."
    )
    description: str | None = None

    @field_validator("title", "company_id", "location")
    @classmethod
    def reject_null_for_required_fields(cls, value: str | int | None) -> str | int:
        # Omitted defaults are not validated; explicitly supplied nulls are.
        if value is None:
            raise ValueError("Field must not be null")
        return value


class CompanyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    description: str | None = None


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    name: str
    description: str | None = None


class CompanyReplace(CompanyCreate):
    version: int = Field(gt=0, strict=True, description="Version returned by the last company read.")


class CompanyUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"version": 1, "name": "WorkWise Labs"}]},
    )

    version: int = Field(gt=0, strict=True, description="Version returned by the last company read.")
    name: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"\S",
        description="May be omitted; explicit null is rejected.",
    )
    description: str | None = None

    @field_validator("name")
    @classmethod
    def reject_null_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Name must not be null")
        return value


class UserSignup(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    username: str = Field(min_length=3, max_length=50, pattern=r"^[a-z0-9_]+$")
    password: SecretStr = Field(min_length=15, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: Literal["member", "admin"]
    is_active: bool


class TokenRead(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid",hide_input_in_errors=True)
    refresh_token: SecretStr = Field(min_length=64,max_length=64)

    @field_validator("refresh_token")
    @classmethod
    def valid_refresh_token(cls,value):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{64}",value.get_secret_value()):
            raise ValueError("Invalid refresh token format")
        return value


ApplicationStatus = Literal["applied", "shortlisted", "rejected", "hired"]


class ApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    cover_letter: str | None = Field(default=None, max_length=4000)


class ApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    applicant_id: int
    cover_letter: str | None
    status: ApplicationStatus
    version: int
    created_at: datetime
    updated_at: datetime


class ApplicationReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(gt=0, strict=True)
    status: Literal["shortlisted", "rejected", "hired"]
