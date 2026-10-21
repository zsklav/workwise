# PostgreSQL CRUD checkpoint

## Request flow

1. FastAPI validates create/PUT bodies with `JobCreate` and PATCH bodies with
   `JobUpdate` in `schemas.py`.
2. `Depends(get_db)` supplies a session for the request.
3. Endpoints in `main.py` query or modify `Job` objects mapped to `public.jobs`.
   Each job references `companies.id`; its company name is read through an ORM
   relationship, joined eagerly when loading jobs.
4. Write endpoints commit their transaction; create/update refresh the object.
5. `JobRead` reads object attributes and defines the response's public fields.
6. The dependency closes the session after the request, including error paths.

The shared engine manages a connection pool. `SessionLocal` creates independent
request sessions. An uncommitted transaction is rolled back when its session
is closed. Unexpected database failures propagate as server errors; recovery,
timeouts beyond connection establishment, and error logging need later work.

## Contracts

| Method and path | Behavior | Success | Missing job |
|---|---|---|---|
| `GET /health` | Process health; no database query | 200 | n/a |
| `GET /jobs` | Page of jobs, optionally filtered by company, ordered by ID | 200 | Empty list |
| `GET /jobs/{job_id}` | Retrieve a job | 200 | 404 |
| `POST /jobs` | Create with database-generated ID | 201 | n/a |
| `PUT /jobs/{job_id}` | Replace title, company_id, location, description | 200 | 404 |
| `PATCH /jobs/{job_id}` | Update only supplied fields | 200 | 404 |
| `DELETE /jobs/{job_id}` | Delete a job, empty body | 204 | 404 |

Create and PUT require nonempty title/location and a positive integer
`company_id` referring to an existing company. A missing company returns 404
before fields are changed. POST/PUT/PATCH reject unknown request keys, including
the former `company` text key. Responses retain read-only `company` name text
and add `company_id`.
Description is optional text, defaulting to null. It may be omitted or supplied
as null. All job responses include a description field. A full-replacement PUT
omitting description clears the previous value to null. This additive response
field is the intentional API change introduced in the description checkpoint.
PATCH uses `model_dump(exclude_unset=True)` so omitted fields retain their
stored values. Explicit `description: null` clears description. Explicit nulls
for title/company_id/location, empty title/location, and invalid company IDs return 422.
Unknown PATCH fields also return 422, preventing misspelled fields or ID updates
from silently succeeding. An empty PATCH body is a successful no-op for an
existing job. A valid PATCH body targeting an absent job returns 404.
Invalid bodies and non-integer path IDs return 422. Missing jobs return
`{"detail": "Job not found"}`. Repeated POSTs create distinct jobs. Authentication
and ownership checks now protect writes, while reads stay public. Version-based
concurrency conflict detection has not been added. See [access rules](auth.md).

## Phase 2: pagination and company filtering

`GET /jobs` now accepts integer query parameters `limit` and `offset`. Defaults
are 20 and 0. Limit must be between 1 and 100; offset must be nonnegative.
Invalid values produce 422 before the query executes. Query validation is
documented automatically in OpenAPI.

The SQLAlchemy statement applies `order_by(Job.id).offset(offset).limit(limit)`
before execution. The database returns the requested page; the application
does not first load every row and slice the Python list. Offsets count rows,
not IDs, which may contain gaps. Results remain a JSON array with an empty
array beyond the end. No count or next-page metadata is included yet.

The default changes from all jobs to at most twenty jobs. This intentional
contract change bounds each response; clients needing all jobs must paginate.
ID ordering makes pages deterministic for an unchanged dataset. Inserts or
deletions between requests do not provide a shared snapshot across pages, and
large offsets may require scanning skipped rows. No performance improvement
has been measured or claimed; query plans and keyset pagination are later work.

The first pagination test initially failed because the old route ignored the
query parameters and returned all three rows. It passed after implementation.
Tests also cover valid bounds, default page size with 21 stored jobs, offsets
beyond the end, and invalid parameters. This endpoint required no schema change.

Optional `company` query input adds an exact, case-sensitive equality condition
using a relationship predicate on `Company.name`. Company is not trimmed, normalized, or
interpreted as a pattern. Omission means all companies; empty text is rejected
with 422. No matches produce an empty array. Existing company values containing
quotes, percent signs, or underscores can be selected literally.

The request flow is: validate query parameters, select matching rows, order by
ID, skip matching rows using offset, and limit the result. SQLAlchemy binds the
company value as a parameter instead of concatenating it into SQL. Tests verify
this behavior with normal names, missing/partial/case-different names,
wildcard characters, and a SQL-like string. This does not replace future
authentication and authorization work.

The original pagination/filter checkpoint required no schema change. Revision
0004 later adds the relationship and `ix_jobs_company_id` index. The optional
`company_id` query parameter filters on that column; supplying both name and ID
requires both to match before ordering and pagination. No query performance
benchmark has been recorded, so the index is not a measured speedup claim.

## Data and schema

The in-memory jobs list has been removed from `main.py`. Only rows stored in
the configured database appear in API responses; former hardcoded or transient
jobs are not automatically inserted. IDs are generated by PostgreSQL identity
and can contain gaps.

The development table was created manually and is not created or altered on
application startup. It was baselined at Alembic revision `0001`, then upgraded
to `b1e18f4fbb2e` to add nullable description text. Tests now upgrade their own
database with Alembic too, replacing metadata-based table creation.
See [migration setup and verification](migrations.md).

Revision `0003` creates the companies table. Its create/read
routes live in `routers/companies.py` and translate the named unique-company
constraint violation to 409 after rolling back the failed transaction. Revision
`0004` backfills distinct existing job company names, reuses exact existing
Company names, and replaces the job text column with a required foreign key.
Deleting a referenced company is restricted at the database level. The ORM
uses the connection's default `public` schema for both models and foreign keys,
matching PostgreSQL's reflected default-schema names during Alembic comparison.
Migrations still explicitly target `public`.
See [Company APIs and constraints](companies.md).

Revision 0005 adds users and nullable company owner IDs. New company ownership
is assigned by the authenticated user; existing rows remain unowned and admin-only.
Jobs inherit permissions from the linked company. Source and destination
permissions are both checked before moving a job. Users hold Argon2 password
hashes, constrained member/admin roles, and active status. No default users or
admin credentials are seeded.

## PATCH learning checkpoint

The learner first added `test_patch_job_updates_only_title`, which failed
with 405 because the route did not exist. The assertion remained 200 while
the implementation was added; the same test then passed. This is the red-to-
green part of TDD. Additional cases verify null handling, omitted fields,
validation failures without data changes, missing jobs, and empty updates.

Only validated schema fields are passed to `setattr`; the PATCH schema forbids
unknown fields. The request session commits changes and refreshes the job.
Description explicitly set to null must remain in the changes dictionary, so
`exclude_none=True` would be incorrect here. Omission and null have different
meanings. No new database migration was required for this endpoint.

## Manual persistence experiment

With the virtual environment active, start the application:

```powershell
python -m uvicorn main:app --reload
```

1. Open `http://127.0.0.1:8000/docs` and create a job using `POST /jobs`.
2. Record the returned ID and retrieve it using `GET /jobs/{job_id}`.
3. Stop Uvicorn with Ctrl+C and start it again using the same command.
4. Retrieve the same ID and compare its fields.

Expected: the created job remains available in `workwise`. The learner reported
successfully completing this manual restart experiment on 2026-10-21.
Automated tests separately verified committed changes across independent
database connections, not a server restart.

## References

- [FastAPI response models](https://fastapi.tiangolo.com/tutorial/response-model/)
- [SQLAlchemy session basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)
- [Testing setup and evidence](testing.md)
