# Testing WorkWise

## Run tests

From the project root with the virtual environment active:

```powershell
python -m pytest -v
```

The current suite has forty-two API cases backed by PostgreSQL, two database
transaction isolation cases, and one independent-connection persistence case.
All jobs endpoints now use PostgreSQL.

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
tables without an Alembic version require explicit verification and baseline
adoption; the fixture stops rather than guessing their version. The original
empty metadata-created test table was checked against revision 0001, stamped,
and upgraded to the description revision on 2026-10-21.

The jobs table must start empty. If it contains existing rows, the fixture
stops without deleting them.

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

## Recorded result

On 2026-10-21, `python -m pytest -v --tb=short` completed with **45 passed**
against local PostgreSQL, including all CRUD endpoints. The command was run
with access to the local database server outside the restricted agent sandbox.

## References

- [SQLAlchemy external transaction test recipe](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites)
- [FastAPI dependency overrides](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- [FastAPI partial updates and exclude_unset](https://fastapi.tiangolo.com/tutorial/body-updates/)
