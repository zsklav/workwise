from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    company_id: int = Field(gt=0, strict=True)
    location: str = Field(min_length=1)
    description: str | None = None


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    company_id: int
    company: str
    location: str
    description: str | None = None


class JobUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"title": "Python Developer"}]},
    )

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
    name: str
    description: str | None = None


class CompanyUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"name": "WorkWise Labs"}]},
    )

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
