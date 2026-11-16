from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker
from collections.abc import Iterator

from workwise.core.config import Settings


settings = Settings()

database_url = URL.create(
    drivername="postgresql+psycopg",
    username=settings.db_user,
    password=settings.db_password.get_secret_value(),
    host=settings.db_host,
    port=settings.db_port,
    database=settings.db_name,
)

engine = create_engine(
    database_url,
    pool_pre_ping=True,
    # Transaction poolers cannot keep prepared statements between requests.
    connect_args={"connect_timeout": 5, **({"prepare_threshold": None} if settings.db_pooler else {})},
)

SessionLocal = sessionmaker(bind=engine)

def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session