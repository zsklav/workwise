from sqlalchemy import select

from workwise.db.session import SessionLocal, engine
from workwise.db.models import Job


def main() -> None:
    try:
        with SessionLocal() as session:
            jobs = session.scalars(select(Job).order_by(Job.id)).all()

            for job in jobs:
                print(job.id, job.title, job.company, job.location)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
