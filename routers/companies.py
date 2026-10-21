from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import Company
from schemas import CompanyCreate, CompanyRead


router = APIRouter(prefix="/companies", tags=["companies"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("", status_code=201, response_model=CompanyRead)
def create_company(company: CompanyCreate, db: DbSession) -> Company:
    new_company = Company(**company.model_dump())
    db.add(new_company)
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
        raise

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
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company
