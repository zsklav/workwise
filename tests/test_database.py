import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Job


@pytest.mark.parametrize("title", ["First test job", "Second test job"])
def test_committed_job_does_not_leak_between_tests(
    db_session: Session, title: str
) -> None:
    assert db_session.scalar(select(func.count()).select_from(Job)) == 0

    job = Job(title=title, company="Test Company", location="Remote")
    db_session.add(job)
    db_session.commit()

    assert isinstance(job.id, int)
    assert db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert db_session.scalar(select(Job.title).where(Job.id == job.id)) == title
