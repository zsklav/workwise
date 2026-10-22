from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trace_context: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_outbox_pending", "created_at", postgresql_where=text("published_at IS NULL")),
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_jobs_version_positive"),
        Index("ix_jobs_company_id_id", "company_id", "id"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        Identity(always=True),
        primary_key=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", name="fk_jobs_company_id_companies", ondelete="RESTRICT"),
        nullable=False,
    )
    employer: Mapped["Company"] = relationship(lazy="joined")
    location: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}

    @property
    def company(self) -> str:
        return self.employer.name


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("name", name="uq_companies_name"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_companies_name_not_blank"),
        CheckConstraint("version > 0", name="ck_companies_version_positive"),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(always=True), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_companies_owner_id_users", ondelete="RESTRICT"),
        nullable=True, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        CheckConstraint("username ~ '^[a-z0-9_]{3,50}$'", name="ck_users_username"),
        CheckConstraint("role IN ('member', 'admin')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'member'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
