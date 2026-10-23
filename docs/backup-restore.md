# Database backups and restore drills

The operator tool captures custom-format PostgreSQL archives for the jobs and
notification databases. It restores them into **new, generated drill databases**,
checks their contents and permissions, and removes those copies afterward. It
does not replace an existing database or switch running applications to a restore.

## Capture a Docker backup

Run from the repository root with the stack already running:

```powershell
python -m scripts.database.backup backup
```

The command prints a newly created directory under `backups/`. Each directory
contains `jobs.dump`, `notifications.dump` and a completed `manifest.json`.
An existing output directory is never reused. `--output backups/my-checkpoint`
selects a new directory explicitly. An interrupted/failed capture has no completed
manifest and must not be treated as a usable backup set.

Each database uses an exported repeatable-read snapshot, shared by `pg_dump` and
the fingerprint reader. Writes can continue while the backup is taken; captured
rows and their verification hashes describe the same snapshot. Table rows are
streamed in primary-key order as UTF-8 JSON through SHA-256, including passwords'
stored hashes, refresh state, JSON payloads, timestamps and inbox read markers.
Raw row values are not written to logs or the manifest.

The manifest records archive SHA-256/size, per-table row counts and data hashes,
schema versions, column definitions, constraints, indexes, table/sequence owners,
and table/column grants. An unexpected public table fails the backup until its
coverage is reviewed. Locks wait at most five seconds; individual SQL statements
have a two-minute timeout and dump/restore subprocesses have a three-minute limit.
This deliberately bounded tool is intended for this project's measured scale.
Column order is compared logically: physical slot gaps left by dropped columns
are normalized, because a logical restore compacts those internal slots.

## Verify the backup by restoring it

Use the actual directory printed by the backup command:

```powershell
python -m scripts.database.backup drill backups/my-checkpoint
```

Only restore archives produced by your trusted installation. PostgreSQL restores
execute database definitions from the archive. Hashes detect accidental changes;
an attacker who can replace both archive and manifest can replace the hashes too.

Before creating any database, the tool checks every archive's checksum and size.
For each archive it then:

1. Creates `ww_restore_<random UUID>_jobs` or `_notifications`, using `template0`.
2. Uses `pg_restore --single-transaction --exit-on-error --no-owner --role=...`
   to restore as the original project's owner role, retaining archived grants.
3. Compares every captured table fingerprint, count, schema version, column,
   constraint, index, owner and table/column grant.
4. Exercises inserts with the jobs runtime role, including identity-generated user,
   company and job IDs, and verifies restricted auth/schema permissions. On the
   inbox copy, the worker inserts a receipt/inbox item and the API role changes
   only its read marker. These probes roll back; sequence probes affect only copies.
5. Drops only the database this invocation successfully created, even on failure.

There is no operator-supplied destination name, `--clean`, source-database drop,
forced disconnection, or in-place restore option. A create-name collision fails
without dropping the existing database. A killed process/host failure may leave a
drill database; inspect its exact generated name and ownership before manual cleanup.
Application connections and source databases remain pointed at their original names.

The existing Compose database containers supply PostgreSQL 17 client tools and
operator-local socket access. No host database password is put in command arguments.
Existing project roles must already exist: database dumps do not provision cluster
roles or role passwords. This drill checks recovery on the running PostgreSQL
clusters; it is not proof that an empty replacement host has been provisioned.

## Native Windows jobs backup

The native installation currently has the jobs database. With `.venv` active:

```powershell
python -m scripts.database.backup --mode native backup
```

This uses `.env`'s migration credentials, passed privately through `PGPASSWORD`.
Client tools are resolved from `PATH`, or `C:/Program Files/PostgreSQL/17/bin` on
Windows. Pass `--pg-bin "C:/path/to/PostgreSQL/17/bin"` before `backup` if needed.
Use a PostgreSQL client compatible with the server. Native mode intentionally has
no restore command requiring a local superuser or changes to native role privileges.
Both-database restore verification runs against the Docker stack in CI.

## Reproducible paired drill and CI

On a **disposable** Docker stack:

```powershell
python -m scripts.smoke.backup --allow-service-interruption
```

This pauses the public API, inbox API, publisher and worker, adds uniquely tagged
pending-outbox and read/unread-inbox fixtures, captures both databases, restores and
verifies both copies, then proves the paused sources are unchanged. Cleanup removes
only those unique fixtures before resuming the services in `finally`. Other operator
writers must remain stopped during this test. Do not run the smoke test on live data.

GitHub's containers job runs this after the auth/security checks. It uploads only
`backup-reports/restore_*.json`: schema versions, row counts, timings and verification
flags. It never uploads database archives, credential files, row values or data hashes.
The PostgreSQL test job covers corrupt archives, path traversal, unsafe target names,
name collisions, failed-restore cleanup and incomplete backups.

## Recovery boundaries and operator checklist

The two archives are **individually consistent snapshots**, not a distributed
transaction. For a coordinated checkpoint, stop all writers/retention/admin jobs
and wait for in-flight transactions before capture. A live backup command does
not itself pause services. Sequence counters are not transactional snapshots;
the restored-copy insert probes check usability rather than equality to a live counter.

These backups do not include RabbitMQ queues/DLQ/volume, deployment secrets,
cluster roles/passwords, PostgreSQL server configuration, or monitoring storage.
The outbox and notification receipts are included, but that alone does not make
a lost broker or mismatched database restore safe:

- A publisher-confirmed event can be marked published while still awaiting delivery.
  Losing the broker can therefore lose an event that a pending-only publisher will
  not resend. Reconcile retained outbox events against restored receipts before
  resuming delivery; preserve their original event IDs for deduplication.
- Outbox retention may already have removed old published events. Receipt retention
  preserves deduplication state but cannot recreate missing payload history.
- Restoring auth data can resurrect sessions revoked after the snapshot. Before a
  real recovery goes online, rotate the access signing secret **and** revoke restored
  refresh sessions/increment user token versions. A signing-key change alone does
  not invalidate restored opaque refresh tokens. Reauthenticate clients afterward.

For a real incident: keep writers stopped, select a trusted checkpoint, provision
matching roles/extensions and compatible PostgreSQL on an isolated replacement,
restore and verify both databases, reconcile broker/outbox/inbox state, invalidate
old sessions, check grants/migrations/readiness, then deliberately switch service
connections and resume traffic. The tool supplies verification copies; production
cutover and event reconciliation remain explicit operator work.

Backups contain private business data and password/session hashes. `backups/`,
`backup-reports/` and `*.dump` are Git-ignored and excluded from Docker builds.
New files use POSIX mode `0600` and directories `0700` where supported; Windows
inherits filesystem ACLs. Use private encrypted storage with tested recovery-key
access, store configuration/secrets separately, and keep a protected off-host copy.
No automatic schedule, off-host upload, encryption, WAL/PITR or backup pruning is
installed. Recovery-point loss is the time since the chosen snapshot; drill timings
measure this small dataset, not a production recovery-time guarantee.

References: PostgreSQL 17 [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)
and [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html).
