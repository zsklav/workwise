from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobCreate(BaseModel):
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    location: str = Field(min_length=1)
    description: str | None = None


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
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
    company: str | None = Field(
        default=None, min_length=1, description="May be omitted; explicit null is rejected."
    )
    location: str | None = Field(
        default=None, min_length=1, description="May be omitted; explicit null is rejected."
    )
    description: str | None = None

    @field_validator("title", "company", "location")
    @classmethod
    def reject_null_for_required_fields(cls, value: str | None) -> str:
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
