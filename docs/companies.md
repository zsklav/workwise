# Company foundation

Companies are stored in `public.companies`. Jobs store a required `company_id`
referencing `companies.id`. One company can have many jobs. Creating a company
does not create jobs; deleting a job does not remove its company.

All writes now require login. New companies belong to their creating user;
only that owner or an admin may manage the company and its jobs. Existing
unowned companies remain admin-only. Reads stay public. See [auth rules](auth.md).

## API

| Method | Path | Behavior |
|---|---|---|
| POST | `/companies` | Create a company; return 201 and its generated ID |
| GET | `/companies` | List in ID order; default limit 20, maximum 100 |
| GET | `/companies/{company_id}` | Return a company, or 404 if absent |
| PUT | `/companies/{company_id}` | Replace name and description; return 200 |
| PATCH | `/companies/{company_id}` | Update supplied fields only; return 200 |
| DELETE | `/companies/{company_id}` | Delete an unlinked company; return empty 204 |

Create accepts `name` and optional `description`. Name must be 1–200
characters with at least one non-whitespace character. Unknown keys and
invalid values return 422. Description defaults to null. Names retain their
case and surrounding whitespace, so `WorkWise` and `workwise` are distinct.

Listing accepts `limit` from 1 to 100 and nonnegative `offset`, defaulting to
zero. Offsets count rows; an offset beyond the end returns an empty array.
The response is a JSON array without total-count metadata.

PUT requires name; omitted description becomes null. PATCH preserves omitted
fields and clears description only when explicitly supplied as null. An empty
PATCH leaves an existing company unchanged. Name cannot be null in either
method. Invalid names, descriptions, unknown keys, and ID write keys return 422.
Valid requests targeting missing companies return 404.

Rename preserves the company's ID. Linked jobs keep their IDs and company IDs;
their returned company name and the name filter reflect the new value on
subsequent requests. Name is stored once on the company, so jobs need no text
backfill when it changes.

## Constraints and transactions

The table has an identity primary key, required `VARCHAR(200)` name, nullable
TEXT description, unique constraint `uq_companies_name`, and check constraint
`ck_companies_name_not_blank`. The database check rejects empty and space-only
names; API validation additionally rejects other whitespace-only input.

The unique constraint makes PostgreSQL responsible for rejecting duplicate
names. The shared company commit helper catches that specific constraint violation, rolls back
the failed transaction, and returns 409 with
`{"detail": "Company name already exists"}`. Other integrity failures are
re-raised. This avoids relying on an application-level lookup before insertion.
No concurrent-request test has been run.

The same helper translates the named job-company foreign-key violation during
deletion into 409 with `{"detail": "Company has jobs and cannot be deleted"}`
after rollback. Existing jobs and the company remain unchanged. No pre-delete
job-count check is used; the database constraint makes the decision. Once all
jobs have been deleted or reassigned, retrying company deletion succeeds with
an empty 204 response. No child jobs are deleted by the company endpoint.

Job create/PUT requests now require `company_id` instead of company text.
PATCH accepts a new company ID to move a job. IDs must be positive JSON integers
and refer to existing companies; nulls and invalid types return 422, missing
companies return 404. Old `company` write keys are rejected with 422, even when
provided alongside an ID. Job responses include both `company_id` and read-only
`company` text derived from the relationship. The company is joined eagerly
when loading jobs, rather than loaded separately for every list row.

The `fk_jobs_company_id_companies` foreign key prevents invalid references and
uses `ON DELETE RESTRICT` to reject deletion of companies that still have jobs.
No ORM delete cascade is configured. A company lookup gives a clear API error;
the database constraint still protects writes if the company disappears before
commit. The job write helper rolls back and translates that specific foreign
key violation to 404. Unexpected integrity errors still propagate.

Job lists support an exact company-name filter and a positive `company_id`
filter. Both supplied together must match. Filtering precedes pagination.
The foreign key column has index `ix_jobs_company_id`; no speedup was measured.

Routes use `APIRouter` in `routers/companies.py`, registered in `main.py`.
Each request receives a database session through the existing `get_db`
dependency. Company request/response schemas are in `schemas.py` and the ORM
mapping is in `models.py`.

## Setup and verification

Alembic revision `0003` creates the table; `0004` links jobs and backfills names.
Run `python -m alembic upgrade head`
using the configured migration role, then grant the runtime role access as the
table owner or administrator:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.companies TO workwise_app;
```

Revision 0004 preserved all three development jobs and backfilled two companies.
Before/after snapshots compared job IDs, titles, exact company names, locations,
and descriptions. The schema matched the ORM after migration. The full
PostgreSQL suite passed with 201 cases, including authentication, ownership, relation constraints,
reassignment, legacy-name backfill, downgrade restoration, and rejected legacy
data without partial schema changes. Company mutation cases additionally cover
rename propagation, duplicate rollback, validation, description semantics,
restricted deletion and retries. A second real-commit test verifies company
changes and deletion through independent connections. This step required no
schema migration for the earlier company-mutation step; revision 0005 later
adds users and ownership. Runtime UPDATE/DELETE permissions were granted separately.
See [testing notes](testing.md).

## Next step

Authentication and ownership checks are implemented. Concurrent rename
conflict detection beyond the unique constraint has not been added; updates
do not yet use version fields or conditional requests.

## References

- [SQLAlchemy relationships](https://docs.sqlalchemy.org/en/20/orm/basic_relationships.html)
- [PostgreSQL foreign keys and delete actions](https://www.postgresql.org/docs/17/ddl-constraints.html#DDL-CONSTRAINTS-FK)
