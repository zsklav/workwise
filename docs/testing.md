# Testing WorkWise

## Run tests

From the project root with the virtual environment active:

```powershell
python -m pytest -v
```

The current suite has 135 cases covering job and company APIs backed by
PostgreSQL, database constraints, migrations, transaction isolation, and independent-connection
job/company persistence. All job and company endpoints use PostgreSQL.

## PostgreSQL setup

The PostgreSQL server must be running. Local `.env` supplies the host, port,
username, and password. Never commit `.env` or put passwords in these notes.
The test engine explicitly selects `workwise_test`, regardless of `DB_NAME`.

One-time setup, executed as the PostgreSQL administrator:

```sql
CREATE DATABASE workwise_test OWNER workwise_app;
```

The test fixture verifies the connected database name, then runs Alembic
`upgrade head` on its shared test connection. This connection uses
`workwise_app`, which owns the test database and is allowed to create its
tables. Runtime development permissions are not broadened. The fixture also
compares the migrated schema to the ORM metadata and fails on drift.

A fresh empty test database is initialized through migrations. Existing jobs
or companies tables without an Alembic version require explicit verification and baseline
adoption; the fixture stops rather than guessing their version. The original
empty metadata-created test table was checked against revision 0001, stamped,
and upgraded to the description revision on 2026-10-21.

The jobs and companies tables must start empty. If either contains existing
rows, the fixture stops without deleting them.

## Isolation and cleanup

`tests/conftest.py` provides:

- `test_engine`: connects only to the dedicated test database.
- `db_connection`: starts an outer transaction and rolls it back after a test.
- `db_session`: provides an ORM session using `create_savepoint` transaction mode.
- `db_client`: overrides FastAPI's `get_db` dependency with a fresh session per
  request, bound to the test's connection, and restores overrides afterward.

Session commits release savepoints while the outer transaction remains open.
The fixture can therefore undo committed test changes at teardown. Identity
sequences are not reset, so tests must use returned IDs rather than expect ID 1.

The two database test cases each assert an initially empty table, insert and
commit a job, then query its stored data. The second case also detects a row
leaked by the first case. API tests use `db_client` through their `client`
fixture. The former in-memory `fresh_jobs` fixture has been removed.

These rollback fixtures support sequential requests in each test. They do not
validate concurrent request behavior or persistence across server restarts.

`tests/test_persistence.py` separately verifies real API commits: it creates,
updates, and deletes a job, checking each result through an independent
connection to `workwise_test`. It uses fresh sessions without the outer
rollback transaction. A unique title identifies its row for cleanup if an
assertion fails. It does not restart the server or database.

A second persistence case creates its own company and linked job, renames the
company through PUT, clears description through PATCH, and checks committed
values through independent connections. It verifies deletion returns 409 while
linked, then deletes the job and company and confirms the company row is gone.
Cleanup targets only that case's UUID-marked rows, including the renamed company.

Description cases cover text and explicit null input, omitted input, response
and list output, PUT replacement and omission clearing, and invalid input.
The independent-connection case also checks description persistence, including
clearing it through PATCH. PATCH API cases cover title-only updates preserving
other fields, description replacement/clearing, invalid values and unknown
keys without data changes, missing jobs, and empty-body no-ops.

Pagination cases verify the requested row window and ID order, valid limits 1
and 100, offsets beyond the end, a default twenty-row page from 21 stored jobs,
and six invalid query parameter cases. The default-size test seeds ORM objects
within the isolated test transaction to avoid unnecessary HTTP setup requests.

Company-filter cases verify exact matching, filtering before pagination,
case-sensitive and partial-name non-matches, literal wildcard/quote handling,
SQL-like input as data, and rejection of an empty company value. The first
company-filter test initially failed because all companies were returned,
then passed once the query condition was added.

Company cases cover create/read/list, optional descriptions, case-sensitive
names, duplicate-name conflicts preserving the original row, subsequent writes
after a conflict, invalid bodies, missing/invalid IDs, pagination, and keeping
jobs unchanged. A direct ORM insert also verifies the database unique constraint.
These cases use rollback fixtures; they do not simulate concurrent inserts.

Company mutation tests cover PUT replacement and omission clearing, PATCH
preservation and explicit-null clearing, empty PATCH, validation and unknown
fields, missing/invalid IDs, and duplicate renames with all changes rolled back.
Both PUT and PATCH renames propagate to linked job responses and name filters
without changing IDs. Delete cases verify empty 204 responses, repeated-delete
404, linked-company 409 preserving both jobs, continued writes after rollback,
and successful retry only after all linked jobs are removed.

Job-company cases verify reassignment through PUT/PATCH, matching name and ID
filters before pagination, rejecting missing/invalid company references and old
text write keys without changing jobs, rejecting direct foreign-key/null
violations, restricting company deletion while linked, and keeping companies
when jobs are deleted. The independent-connection job case now creates its own
uniquely marked company and cleans it up after removing its job.

Migration tests temporarily downgrade the test schema to 0003 inside an outer
transaction, seed legacy rows, and apply 0004. They verify exact name matching,
existing-company reuse and description preservation, multiple jobs sharing a
company, case/whitespace/quote preservation, and downgrade restoration. Invalid
legacy names abort before schema/data changes. All these DDL/data changes are
rolled back at teardown; the development database is never downgraded.

## Recorded result

On 2026-10-21, `python -m pytest -q -x --tb=short` completed with **135 passed**
against local PostgreSQL, including job and company CRUD endpoints. The command was run
with access to the local database server outside the restricted agent sandbox.

## References

- [SQLAlchemy external transaction test recipe](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites)
- [FastAPI dependency overrides](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- [FastAPI partial updates and exclude_unset](https://fastapi.tiangolo.com/tutorial/body-updates/)
