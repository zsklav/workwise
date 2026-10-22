"""UUID-owned synthetic records, scoped cleanup and verified fixture counts."""
import secrets
from uuid import uuid4

from sqlalchemy import text

from pwdlib import PasswordHash


def seed(connection, *, rows=20000, companies=100):
    if not 1000 <= rows <= 100000 or not 2 <= companies <= min(1000,rows//20):
        raise ValueError("Use 1000..100000 jobs and 2..1000 companies with >=20 jobs each")
    if connection.scalar(text("SELECT count(*) FROM jobs")) or connection.scalar(text("SELECT count(*) FROM companies")):
        raise RuntimeError("HTTP benchmark requires empty jobs/companies; existing data was preserved")
    username = "perf_"+uuid4().hex
    password = secrets.token_urlsafe(32)
    user_id=connection.scalar(text("INSERT INTO users(username,password_hash) VALUES (:name,:hash) RETURNING id"),
                              {"name":username,"hash":PasswordHash.recommended().hash(password)})
    ids=connection.scalars(text("INSERT INTO companies(name,owner_id) SELECT :prefix||i,:owner FROM generate_series(1,:n) i RETURNING id"),
                           {"prefix":username+" company ","owner":user_id,"n":companies}).all()
    connection.execute(text("INSERT INTO jobs(title,company_id,location,description) SELECT 'Fixture job '||i, (CAST(:ids AS integer[]))[((i-1)%:n)+1], 'Remote', repeat('x',120) FROM generate_series(1,:rows) i"),
                       {"ids":ids,"n":companies,"rows":rows})
    connection.execute(text("ANALYZE jobs"))
    connection.execute(text("ANALYZE companies"))
    selected = ids[-1]
    return {"username":username,"password":password,"user_id":user_id,"company_id":selected,
            "company_name":connection.scalar(text("SELECT name FROM companies WHERE id=:id"),{"id":selected}),
            "job_id":connection.scalar(text("SELECT id FROM jobs WHERE company_id=:id ORDER BY id LIMIT 1"),{"id":selected}),
            "jobs":rows,"companies":companies}


def cleanup(connection, fixture):
    # Only this fixture's recipient/owner can match; unrelated history is retained.
    params={"owner":fixture["user_id"],"recipient":str(fixture["user_id"])}
    connection.execute(text("DELETE FROM outbox_events WHERE payload->>'recipient_id'=:recipient"),params)
    connection.execute(text("DELETE FROM jobs WHERE company_id IN (SELECT id FROM companies WHERE owner_id=:owner)"),params)
    connection.execute(text("DELETE FROM companies WHERE owner_id=:owner"),params)
    connection.execute(text("DELETE FROM users WHERE id=:owner"),params)
