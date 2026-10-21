# Company foundation

Companies are now stored in `public.companies`. Jobs still store their own
`company` text; the two tables do not have a relationship yet. Creating a
company neither creates jobs nor updates existing company strings.

## API

| Method | Path | Behavior |
|---|---|---|
| POST | `/companies` | Create a company; return 201 and its generated ID |
| GET | `/companies` | List in ID order; default limit 20, maximum 100 |
| GET | `/companies/{company_id}` | Return a company, or 404 if absent |

Create accepts `name` and optional `description`. Name must be 1–200
characters with at least one non-whitespace character. Unknown keys and
invalid values return 422. Description defaults to null. Names retain their
case and surrounding whitespace, so `WorkWise` and `workwise` are distinct.

Listing accepts `limit` from 1 to 100 and nonnegative `offset`, defaulting to
zero. Offsets count rows; an offset beyond the end returns an empty array.
The response is a JSON array without total-count metadata.

## Constraints and transactions

The table has an identity primary key, required `VARCHAR(200)` name, nullable
TEXT description, unique constraint `uq_companies_name`, and check constraint
`ck_companies_name_not_blank`. The database check rejects empty and space-only
names; API validation additionally rejects other whitespace-only input.

The unique constraint makes PostgreSQL responsible for rejecting duplicate
names. The create route catches that specific constraint violation, rolls back
the failed transaction, and returns 409 with
`{"detail": "Company name already exists"}`. Other integrity failures are
re-raised. This avoids relying on an application-level lookup before insertion.
No concurrent-request test has been run.

Routes use `APIRouter` in `routers/companies.py`, registered in `main.py`.
Each request receives a database session through the existing `get_db`
dependency. Company request/response schemas are in `schemas.py` and the ORM
mapping is in `models.py`.

## Setup and verification

Alembic revision `0003` creates the table. Run `python -m alembic upgrade head`
using the configured migration role, then grant the runtime role access as the
table owner or administrator:

```sql
GRANT SELECT, INSERT ON TABLE public.companies TO workwise_app;
```

The development upgrade preserved all three existing jobs, and an insert/read
under the runtime role was verified then rolled back. `alembic check` found no
schema drift. The full PostgreSQL suite passed with 66 cases, covering Company
validation, duplicates, reads, pagination, direct database uniqueness, and
existing job behavior. See [testing notes](testing.md) for isolation limits.

## Next step

Introduce a reviewed Company-to-Job foreign key migration with an explicit
backfill for existing company strings. Job payloads and filtering will need
an intentional compatibility decision. Company update/delete endpoints and
rules for deleting companies with jobs are not implemented at this checkpoint.
