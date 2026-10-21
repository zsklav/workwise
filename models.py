from sqlalchemy import CheckConstraint, ForeignKey, Identity, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(
        Integer,
        Identity(always=True),
        primary_key=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", name="fk_jobs_company_id_companies", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    employer: Mapped["Company"] = relationship(lazy="joined")
    location: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def company(self) -> str:
        return self.employer.name


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("name", name="uq_companies_name"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_companies_name_not_blank"),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(always=True), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
