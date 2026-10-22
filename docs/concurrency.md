# Concurrent-update protection

Jobs and companies use optimistic concurrency. Each stored row has a positive
integer `version`, initially 1. A write must identify the revision the client
actually read. Two clients editing revision 1 cannot silently overwrite one
another: the first successful write produces revision 2; the other gets 409.

## HTTP contract

1. GET the resource and retain its `version` with the displayed/editable data.
2. Send that version with the intended edit. For example, if GET returned 1:

   ```http
   PATCH /jobs/1
   Authorization: Bearer <access_token>
   Content-Type: application/json

   {"version": 1, "title": "Senior Python Developer"}
   ```

3. Success returns the updated resource with version 2. Retain the returned
   version for the next edit. A second request using 1 returns:

   ```json
   {"detail": "Resource has changed; fetch it again and retry with the latest version"}
   ```

On conflict, fetch the latest resource and review/merge the intended change
before retrying. Blindly fetching a version and resending old data can still
overwrite another person's intent. The server does not automatically retry.

| Request | Version contract |
|---|---|
| POST | Server assigns 1; client-supplied `version` is rejected |
| GET / list | Each job/company includes its current `version` |
| PUT | Required positive JSON integer `version`, plus all required replacement fields |
| PATCH | Required positive JSON integer `version`, plus any supplied editable fields |
| DELETE | Required positive integer query parameter, e.g. `/jobs/1?version=2` |

Missing/invalid versions return 422. JSON strings, booleans, nulls and fractional
numbers are rejected for PUT/PATCH versions. Empty PATCH `{}` returns 422.
Every successful PUT/PATCH advances exactly once, even when business values
are identical or the PATCH contains only `version`. This ensures there is an
actual conditional SQL write. A failed write never consumes a persisted revision.

Existing authentication and ownership requirements apply. With otherwise valid
requests, missing resources return 404, denied access returns 401/403, and stale
revisions return 409. The existing duplicate-company and linked-company-delete
409 responses retain their distinct messages. For a linked company, deleting
its jobs and then retrying with the unchanged company revision is allowed.

## Database guarantee and boundaries

`version_id_col` on each ORM mapper makes SQLAlchemy emit a conditional write
equivalent to `UPDATE jobs SET ..., version = 2 WHERE id = :id AND version = 1`.
DELETE also checks the loaded revision. An early API check handles already-stale
requests, while the SQL condition handles races after both requests pass that
check. If no row matches, SQLAlchemy raises `StaleDataError`; the shared commit
helper rolls back and translates it to 409. Separate API processes sharing
this PostgreSQL database receive the same row-level protection.

Versions identify individual rows, not the whole joined JSON representation.
Renaming a company changes the company revision and the company name returned
with linked jobs; those jobs' own revisions do not advance. These values are
not HTTP ETags. This does not provide cross-row or cross-service transaction
isolation. Multi-row rules still rely on their transactions and constraints.

The guard applies to ORM instance UPDATE/DELETE during flush. Raw SQL and bulk
UPDATE/DELETE bypass mapper tracking; any future writer using those paths must
explicitly check and advance revisions. The API uses guarded instance writes.

## Migration and verification

Alembic 0008 adds version columns/defaults and positive check constraints without
altering existing business fields. Existing rows start at 1. For a controlled
local rollout, stop the API/background processes, run:

```powershell
python -m scripts.database.migrate
```

Restart with the updated code, and update clients to the required-version
contract. No environment keys or dependencies are added. The independent
notification database is unchanged.

The PostgreSQL suite includes deterministic races: a before-flush barrier holds
both writers after they load the same revision. Independent sessions then
compete on PUT/PATCH for each resource. Tests assert exactly one 200 and one
409 and verify the winner through another connection. Separate tests check
stale deletes, retries, no-op semantics and migration data preservation.

On an isolated disposable Docker stack, run the HTTP demo:

```powershell
python -m scripts.smoke.concurrency
```

It targets `http://127.0.0.1:8001` by default (`--base-url` overrides it), creates
a random member/company/job, races two HTTP writers for each method/resource,
and verifies the stored winner. Finally it deletes its job/company with their
latest revisions. The account and event/inbox history remain until disposable
volumes are removed. CI runs this demo against its containerized API. The HTTP
demo synchronizes client starts; the database tests additionally synchronize
server flushes to force the critical race.

Reference: [SQLAlchemy version counter behavior and bulk-write limits](https://docs.sqlalchemy.org/en/20/orm/versioning.html).
