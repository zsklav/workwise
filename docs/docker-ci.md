# Docker and CI

This checkpoint packages WorkWise and verifies it in GitHub Actions. It does
not publish an image or deploy a cloud service. The native PostgreSQL database
and existing local `.env` are independent of this stack.

## Stack

| Service | Purpose | Credentials |
|---|---|---|
| db | PostgreSQL 17 with named volume | Initial admin and role bootstrap |
| migrate | One-shot Alembic upgrade plus runtime grants | Migration role |
| api | FastAPI/Uvicorn on container port 8000 | Runtime role and JWT secret |
| admin | Explicit tools-profile admin creation | Migration role and JWT secret |
| publisher | Outbox-to-RabbitMQ process | Jobs runtime role and broker password |
| rabbitmq | Durable queue and DLQ, management on loopback 15672 | Private broker password |
| notification-worker | Idempotent receipt consumer, retries and quarantine | Broker and notification runtime credentials |
| notification-db | Independent notification state and volume | Separate admin/runtime credentials |
| notification-migrate | Independent inbox schema upgrade and grants | Notification admin credentials |
| notification-api | Internal recipient inbox/read API | Restricted inbox DB role and separate service signing secret |
| jaeger | OTLP tracing and local UI on 16686 | No business credentials |

The API is exposed only on host loopback port 8001 by default (`API_PORT`).
The database has no host port mapping. Compose service DNS uses host `db`;
native host `localhost` is not used for container connections. Project name
`workwise-docker` gives its named volume an independent namespace.

`scripts/create_docker_env.py` creates ignored `.env.docker` using random
credentials without printing values. It never reads/copies native `.env` and
refuses to overwrite an existing file. `.env.docker.example` documents variable
names only. Always pass `--env-file .env.docker`; this avoids using the native
credentials for Compose interpolation.

For an existing config, `python scripts/create_docker_env.py --add-missing`
appends new broker/notification keys without changing existing values. See
[distributed event processing](distributed-systems.md) for failure demos and replay.

On an empty volume, `docker/postgres/init.sh` creates runtime/migration roles,
the application database owned by the migration role, and a separate empty
test database owned by the runtime role. PostgreSQL's normal initialization
mechanism runs this only once. Existing volumes are never reset or reinitialized
by application startup. Retain credentials alongside volume backups; changing
an environment file alone does not rotate existing database passwords.

Compose waits for PostgreSQL's health check, then for migration success, then
starts the API. The migration runner applies reviewed revisions and explicit
grants in a transaction. It grants users SELECT/INSERT and company/job CRUD;
it also grants read-only access to Alembic's version table for readiness. It
does not give the runtime role DDL or user role-management access.
Revision 0006 additionally grants SELECT/INSERT/UPDATE on outbox events to the
jobs runtime role used by API and publisher. The notification worker has separate
credentials and cannot access the jobs database.

## Image and health checks

The Dockerfile uses Python 3.13 on Debian slim, installs pinned direct runtime
requirements, and runs UID/GID 10001. It starts Uvicorn without reload. Compose
adds a read-only API filesystem, temporary `/tmp`, dropped capabilities, and
no-new-privileges. Migration credentials are absent from the API environment.
The image uses explicit source COPY operations and a `.dockerignore` that
excludes `.env*`, Git metadata, the Windows virtualenv, caches, and logs.

`GET /health` is a process check. `GET /ready` queries the Alembic version and
compares it to the code's migration head. Database failure or stale/missing
schema returns 503 without connection details. The image health check calls
`/ready`; Compose's `--wait` waits for that health result. Readiness does not
exercise every CRUD permission or replace backup/monitoring procedures.

Base images use maintained version tags (`python:3.13-slim-bookworm` and
`postgres:17-alpine`), not immutable digests. Direct Python dependencies are
pinned; transitive dependencies are not fully locked. This is a local learning
stack, with cloud TLS/secrets/backups and deployment rollout still pending.

## Commands

Run these from the repository root with Docker Desktop's Linux engine running:

```powershell
python scripts/create_docker_env.py
docker compose --env-file .env.docker up --build --detach --wait
docker compose --env-file .env.docker ps
```

Generate credentials only once. Open `http://127.0.0.1:8001/docs` and use signup
and Authorize. An optional administrator can be created interactively:

```powershell
docker compose --env-file .env.docker run --rm admin
```

The admin service is in a tools profile and does not start during normal `up`.
To choose another new admin username, override its command:

```powershell
docker compose --env-file .env.docker run --rm admin python manage_users.py create-admin another_admin
```

For a code/schema upgrade, stop API writes before applying migrations:

```powershell
docker compose --env-file .env.docker stop api publisher notification-worker
docker compose --env-file .env.docker build
docker compose --env-file .env.docker run --rm migrate
docker compose --env-file .env.docker up --detach --wait
```

Normal `docker compose --env-file .env.docker down` retains the volume. Do not
add `--volumes` when retaining personal data; CI uses it only on its disposable
runner. Never mount the native PostgreSQL data directory into this stack.

## CI

`.github/workflows/ci.yml` runs on pushes/PRs to main and manual dispatch,
with read-only repository permission and a timeout on each job. It has two jobs:

- **tests:** bootstrap fresh PostgreSQL roles/databases, apply all migrations
  and grants, run dependency/schema and runtime version-table access checks,
  then the full Pytest suite. CI-only
  credentials refer to the disposable service container, never local data.
- **containers:** generate random disposable credentials, validate Compose,
  build/start the image, verify UID and absence of privileged environment
  credentials, then exercise readiness, signup/login, ownership, CRUD, and
  restricted deletion through HTTP. Cleanup removes only this runner's stack.
  It also runs broker/worker outage tests, recipient inbox isolation/read tests,
  verifies exported cross-service traces, and tests inbox/collector outages.

`scripts/smoke_test.py` is for disposable CI stacks. It creates a random member
account and cleans up its company/job through the API. The account itself
remains until the disposable volume is removed; do not run it against personal
or production data expecting a complete account cleanup.

Local validation on 2026-10-21: 204 PostgreSQL tests passed, Python/YAML syntax
checks passed, and the migration runner completed idempotently. Docker was
not installed on the local Windows host; image/runtime validation runs in
GitHub's Linux CI job. Check the actual [workflow runs](https://github.com/zsklav/workwise/actions/workflows/ci.yml)
for the current commit's result rather than treating configuration alone as a pass.

The subsequent observability checkpoint adds JSON request logs and UUID response
headers. Docker disables Uvicorn's duplicate raw access logger. HTTP smoke tests
check correlation headers and deny metrics access to anonymous/member callers;
the PostgreSQL suite separately verifies the admin response. The full local
suite now passes 219 cases. See [observability](observability.md) for metrics
lifetime and collection limitations.

## References

- [Docker Python guide](https://docs.docker.com/guides/python/)
- [Compose dependency readiness](https://docs.docker.com/compose/how-tos/startup-order/)
- [PostgreSQL services in GitHub Actions](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers)
