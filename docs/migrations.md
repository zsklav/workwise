# Alembic setup and baseline

## Current state

On 2026-10-21:

- Alembic was initialized under `migrations/`.
- `env.py` loads a migration-specific URL from `MigrationSettings` and uses
  `Base.metadata`. Runtime settings continue to use `workwise_app`.
- No password is stored in `alembic.ini`.
- Read-only schema comparison against `workwise` found no differences from
  the current ORM model, including column types and server defaults.
- Initial inspection showed no recorded revision or `alembic_version` table.
- Revision `0001` describes creation of `public.jobs` for an empty database.
- `alembic upgrade head --sql` successfully rendered its SQL without applying it.
- Migration-role login, jobs ownership, and schema creation permissions were
  verified before baselining.
- `workwise/cli/baseline_db.py` recorded revision `0001` in one transaction after validating
  the schema and comparing before/after snapshots of the two existing rows.
- After initial adoption, `alembic current` reported `0001 (head)`.
- `alembic check` reports `No new upgrade operations detected.`
- `workwise/cli/check_db.py` confirmed the runtime user can still read both existing jobs.

The existing jobs table and rows were preserved. Alembic's version table was
created to record the baseline.

## Existing database versus empty database

An empty database needs `upgrade head` to execute the creation migration.
The existing development database already has the jobs table. After checking
that it matches revision 0001, it needs a baseline stamp recording that revision
without recreating the table. Stamping does not itself check schema correctness.
The existing development database was first stamped at `0001`, then upgraded
to the description revision described below.

`workwise/cli/baseline_db.py` performs the initial adoption only: it verifies the role,
ownership, schema privileges, model/schema match, and expected starting
revision. A short SHARE lock prevents concurrent writes/DDL while the helper
compares existing rows and stamps through a shared Alembic connection. The
lock has a five-second wait timeout. If verification fails, the transaction
rolls back. Future schema changes must use reviewed migration revisions.

## Migration credentials and permissions

Initial inspection found that `workwise_app` could not create objects in
`public`, and `public.jobs` was owned by `postgres`. The runtime role retains
its CRUD permissions. A separate migration role needs ownership of jobs and
schema creation privileges to create Alembic's version table and apply DDL.

As the PostgreSQL administrator connected to `workwise`, role setup is:

```sql
CREATE ROLE workwise_migrator LOGIN;
```

Set its password interactively with `\password workwise_migrator`, then run:

```sql
GRANT CONNECT ON DATABASE workwise TO workwise_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO workwise_migrator;
ALTER TABLE public.jobs OWNER TO workwise_migrator;
```

Role setup, ownership, and permissions have been verified successfully using
the migration credentials supplied locally by the learner.

Local `.env` supplies `MIGRATION_DB_USER=workwise_migrator` and its password.
Do not replace runtime `DB_USER` or `DB_PASSWORD`. `.env.example` contains only
placeholders for setting up another local environment.

`Settings` ignores unrelated dotenv fields so the runtime can read the shared
local file. `MigrationSettings` additionally requires the migration password.
Migration tools no longer import the runtime engine. For a deployed app, inject
runtime and migration credentials into their respective processes separately.

The development database was stamped initially; the initial table-creation upgrade and
its table-dropping downgrade were not executed. Offline SQL generation has
been checked; applying the creation migration to an empty database remains a
separate verification step.

## First schema change: optional job description

Revision `b1e18f4fbb2e` follows `0001` and adds nullable TEXT column
`public.jobs.description`. The learner wrote the migration, inspected its SQL,
and applied it to the development database. Existing rows naturally have NULL
in this new column. Its downgrade removes the column and any descriptions;
that downgrade has not been executed.

The ORM and API now support optional descriptions. Existing create payloads
remain valid, with responses gaining `description: null`. PUT uses replacement
semantics, so omitting description clears its prior value. No default values
or backfill were required for the nullable column.

The original empty test table was verified against the initial model, baselined
at 0001, and upgraded to `b1e18f4fbb2e` inside one transaction. Test setup now
runs Alembic migrations and compares their resulting schema to the ORM model.

Verification on 2026-10-21: all 18 automated cases passed, including optional
description CRUD, validation, clearing, and independent-connection persistence.
Development `alembic check` reported `No new upgrade operations detected.`

## Company table: revision 0003

Revision `0003` follows `b1e18f4fbb2e` and creates `public.companies` with an
identity ID, required `VARCHAR(200)` name, nullable text description, a unique
name constraint, and a check rejecting empty or space-only names. The API also
rejects other whitespace-only names. This revision does not alter jobs or add
a foreign key.

On 2026-10-21, the development upgrade was applied in a transaction with a
five-second lock timeout and a SHARE lock on jobs. Before/after row snapshots
matched: all three existing jobs were preserved. Alembic reported revision
`0003`, and `alembic check` found no pending schema operations. The 66-case
test suite passed against the migrated test database.

Runtime `SELECT, INSERT` permissions on companies were granted separately
from the migration. The migration contains no environment-specific role names;
fresh installations must apply the grants documented in the README. An insert
and read using the runtime role were verified in a transaction and rolled back.
No temporary company row was retained. The company-table-dropping downgrade
has not been executed.

## Linking jobs: revision 0004

Revision `0004` replaces `jobs.company` text with a required `company_id`
foreign key referencing `companies.id`, using `ON DELETE RESTRICT`, and adds
index `ix_jobs_company_id`. The API accepts company IDs for job writes and
retains company name text only in responses and the name filter.

The upgrade takes an ACCESS EXCLUSIVE lock on both tables with a five-second
lock wait timeout, then checks legacy names before altering the schema. Empty,
whitespace-only, or over-200-character names abort the migration for explicit
review. It inserts distinct exact names, reuses existing unique company names
without changing their descriptions, backfills IDs, adds NOT NULL and the
foreign key, then drops the redundant job name column. These changes run in
one PostgreSQL transaction. Schedule this migration while the app is stopped
for deployment: it blocks table access and is not a zero-downtime rollout.

On 2026-10-21, 103 tests passed, including upgrade/downgrade with seeded legacy
data and rejection of invalid names without partial changes. The development
upgrade then preserved snapshots of all three jobs and backfilled two companies.
The migrated schema matched ORM metadata. Existing runtime table grants still
cover job writes; no new role privileges were added.
Read/create/update/delete through the API were also verified using the runtime
role in an outer rollback transaction; existing development jobs were unchanged.

Downgrade restores job text from each referenced company's current name,
removes the foreign key/index/ID column, and retains Company rows. It does not
recover historical company names after future renames. Downgrade was tested
inside rollback transactions in `workwise_test`, and was not run on development.

## Company update/delete permissions

The company PUT/PATCH/DELETE checkpoint required no schema changes: revision
0004 already supplies the unique-name and restricted-delete constraints.
On 2026-10-21, UPDATE and DELETE on `public.companies` were granted to the
runtime role separately from Alembic. Existing SELECT/INSERT permissions remain.
Fresh installations should use the complete company CRUD grant in the README.
The application still has no schema-management privileges.

## Users and ownership: revision 0005

Revision 0005 adds `public.users` with identity IDs, unique canonical usernames,
password hashes, constrained member/admin roles (default member), and active
status (default true). It adds nullable `companies.owner_id` with an indexed,
restricted foreign key to users. Jobs remain linked to their company and inherit
the company's authorization rules in the API.

On 2026-10-21 the test suite passed 201 cases, including migration up/down with
legacy rows and no automatically assigned ownership. The development upgrade
then compared snapshots of all three jobs and two companies, preserving every
existing field and adding only null owner IDs. No users or admin credentials
were seeded. Schema comparison and `alembic check` found no pending operations.

Runtime SELECT/INSERT on users was granted separately; user UPDATE/DELETE was
not granted. Trusted local admin creation uses migration credentials via
`workwise/cli/manage_users.py create-admin`. Signup always creates members. Runtime signup,
login, bearer authentication, owned writes, denied legacy writes, and linked
deletion conflicts were verified in an outer rollback transaction. Temporary
users and data were rolled back, and existing snapshots were unchanged.

Downgrade removes ownership metadata and users, including their credential
hashes, while preserving jobs and companies. It is destructive to account data
and was tested only in rollback transactions in `workwise_test`.

## Concurrent updates: revision 0008

Revision 0008 adds a NOT NULL integer `version` with server default 1 to jobs
and companies, and positive-version check constraints. Existing business fields,
IDs, relationships and ownership are preserved. It uses a five-second lock
timeout so contention aborts the transaction rather than waiting indefinitely.
No inbox or outbox schema change is needed.

On 2026-10-22 the native development database was upgraded from 0007 to 0008.
Before/after snapshots verified that all fields of its three jobs and two
companies were preserved, with only version 1 added. Users and outbox rows
were also unchanged. Schema comparison found no pending operations.

Upgrade with `python -m scripts.database.migrate` using migration credentials.
Coordinate the API rollout: old clients must start sending their last-read
version on PUT/PATCH and DELETE. Existing workers/publisher do not modify these
rows, but stop the local stack for a controlled upgrade and restart it afterward.
Downgrade removes revision metadata and protection; its round trip is tested
only inside rollback transactions in the dedicated test database.
See [the concurrency contract](concurrency.md).

## Ordered company job lookup: revision 0009

Revision 0009 replaces the single-column job company index with
`ix_jobs_company_id_id(company_id, id)`. An isolated 50,000-row query-plan
experiment showed lower buffer work for company-filtered first pages. The
leftmost company column still supports lookup and FK checks. The wider index
costs more storage/write maintenance; deep offsets remain expensive.

On 2026-10-22 the native upgrade preserved all fields and versions of the three
jobs and two companies, plus unchanged users/outbox rows. The migration is
transactional with a five-second lock timeout. Its index build is appropriate
for this small local dataset; large live tables need a planned concurrent
index rollout. Tests verify upgrade/downgrade indexes and row preservation.
See [measurement details](performance.md).

## Authentication sessions and limits: revision 0010

Revision 0010 adds a positive default-1 user token epoch and two auth-state
tables: hashed refresh credentials with expiry/used/revoked state, and HMAC-keyed
fixed-window limits. Existing hashes, roles, activation and business rows are
preserved. Runtime gets auth-state SELECT/INSERT/UPDATE and user UPDATE only on
token_version. Account deletion cascades refresh credentials; there is no public
account-delete endpoint. Downgrade removes auth state and invalidates the new
session contract; it is tested only against disposable rollback fixtures.
See [security contract and rollout](security-hardening.md).

Native revision 0010 was applied on 2026-10-22 with before/after snapshots
preserving the three jobs, two companies and all other existing business state.
The actual runtime role then completed signup/login/refresh/logout inside an
outer rollback transaction; no synthetic native account, token or limit rows
remained. Role updates remained denied while token_version updates succeeded.

## Source

- [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [Autogeneration and required review](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- [Command reference](https://alembic.sqlalchemy.org/en/latest/api/commands.html)
## Idempotency and applications: 0011

Revision `0011` creates `idempotency_records` and `job_applications`, their foreign
keys, unique/check constraints and bounded-list/expiry indexes. Existing rows and
columns are unchanged. The migration runner grants SELECT/INSERT/UPDATE to the
runtime role, with no DELETE or schema-management privileges on these tables.

Native verification on 2026-10-23 compared every existing business/auth row before
and after upgrade, verified the new grants, and found no schema drift. A real
runtime-role API flow created a keyed job, retried it, applied, shortlisted and
hired inside an outer rollback transaction, leaving no synthetic application,
idempotency or outbox rows. Identity sequences can advance across rollback normally.
Tests separately cover upgrade/downgrade and expired-key cleanup. Backups include
both new tables; notification storage remains at schema v2.

Downgrading drops the new tables and their history. Do not downgrade a populated
installation without a reviewed data-recovery plan. In a staged release, deploy
workers that understand `application.changed.v1` before enabling new event producers.

See [application and retry contracts](idempotency-applications.md).
