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
- `baseline_db.py` recorded revision `0001` in one transaction after validating
  the schema and comparing before/after snapshots of the two existing rows.
- After initial adoption, `alembic current` reported `0001 (head)`.
- `alembic check` reports `No new upgrade operations detected.`
- `check_db.py` confirmed the runtime user can still read both existing jobs.

The existing jobs table and rows were preserved. Alembic's version table was
created to record the baseline.

## Existing database versus empty database

An empty database needs `upgrade head` to execute the creation migration.
The existing development database already has the jobs table. After checking
that it matches revision 0001, it needs a baseline stamp recording that revision
without recreating the table. Stamping does not itself check schema correctness.
The existing development database was first stamped at `0001`, then upgraded
to the description revision described below.

`baseline_db.py` performs the initial adoption only: it verifies the role,
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

## Source

- [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [Autogeneration and required review](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- [Command reference](https://alembic.sqlalchemy.org/en/latest/api/commands.html)
