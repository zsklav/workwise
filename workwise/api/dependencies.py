"""Request-scoped database dependency shared by API routes."""
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from workwise.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]
