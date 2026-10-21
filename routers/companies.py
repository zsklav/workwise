from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import Company
from schemas import CompanyCreate, CompanyRead, CompanyUpdate
from security import CurrentUser, require_company_access


router = APIRouter(prefix="/companies", tags=["companies"])
DbSession = Annotated[Session, Depends(get_db)]


def find_company(company_id: int, db: Session) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def commit_company(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if (
            isinstance(error.orig, UniqueViolation)
            and error.orig.diag.constraint_name == "uq_companies_name"
        ):
            raise HTTPException(
                status_code=409, detail="Company name already exists"
            ) from error
        if (
            isinstance(error.orig, ForeignKeyViolation)
            and error.orig.diag.constraint_name == "fk_jobs_company_id_companies"
        ):
            raise HTTPException(
                status_code=409, detail="Company has jobs and cannot be deleted"
            ) from error
        raise


@router.post("", status_code=201, response_model=CompanyRead)
def create_company(company: CompanyCreate, db: DbSession, user: CurrentUser) -> Company:
    new_company = Company(**company.model_dump(), owner_id=user.id)
    db.add(new_company)
    commit_company(db)
    db.refresh(new_company)
    return new_company


@router.get("", response_model=list[CompanyRead])
def get_companies(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Company]:
    statement = select(Company).order_by(Company.id).offset(offset).limit(limit)
    return list(db.scalars(statement).all())


@router.get("/{company_id}", response_model=CompanyRead)
def get_company(company_id: int, db: DbSession) -> Company:
    return find_company(company_id, db)


@router.put("/{company_id}", response_model=CompanyRead)
def replace_company(company_id: int, updated_company: CompanyCreate, db: DbSession, user: CurrentUser) -> Company:
    company = find_company(company_id, db)
    require_company_access(company, user)
    company.name = updated_company.name
    company.description = updated_company.description
    commit_company(db)
    db.refresh(company)
    return company


@router.patch("/{company_id}", response_model=CompanyRead)
def patch_company(company_id: int, updated_company: CompanyUpdate, db: DbSession, user: CurrentUser) -> Company:
    company = find_company(company_id, db)
    require_company_access(company, user)
    changes = updated_company.model_dump(exclude_unset=True)
    if changes:
        for field, value in changes.items():
            setattr(company, field, value)
        commit_company(db)
        db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=204)
def delete_company(company_id: int, db: DbSession, user: CurrentUser) -> Response:
    company = find_company(company_id, db)
    require_company_access(company, user)
    db.delete(company)
    commit_company(db)
    return Response(status_code=204)
