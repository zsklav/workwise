# Project structure

Application code lives in the `workwise` package. Configuration, migrations,
Docker files, operator scripts, tests and documentation have separate locations.
Run the commands below from the repository root with the virtual environment active.

## Where code belongs

| Directory | Responsibility |
|---|---|
| `workwise/api/main.py` | Assemble the public FastAPI app, middleware, error handler and routers. |
| `workwise/api/routes/` | Auth, companies, jobs, notification gateway and operational endpoints. |
| `workwise/api/dependencies.py` | Shared request-scoped database session dependency. |
| `workwise/api/concurrency.py` | Revision validation and atomic ORM conflict translation. |
| `workwise/api/schemas.py` | Public request/response validation models. |
| `workwise/core/` | Settings, repository paths, authentication helpers, logs/metrics and tracing. |
| `workwise/db/` | Jobs database engine/session setup and ORM models. |
| `workwise/messaging/` | Event contracts, broker operations, outbox publisher, consumer, heartbeat and replay. |
| `workwise/notifications/` | Independent inbox API, service-token contracts, notification storage and schema upgrade. |
| `workwise/cli/` | Local admin creation, read-only database check and historical baseline helper. |
| `workwise/operations/` | Service scrape authentication/aggregates and bounded retention CLI. |
| `scripts/database/` | Privileged jobs/notification migration runners and runtime grants. |
| `scripts/docker/` | Generate or extend ignored Docker configuration without exposing secrets. |
| `scripts/smoke/` | Executable API, failure-recovery and inbox/tracing demos for disposable stacks. |
| `scripts/performance/` | Isolated fixture lifecycle, pooled HTTP workloads, report generation and temporary-table query-plan comparisons. |
| `tests/` | `api`, `db`, `messaging`, `notifications`, `observability`, `cli` test groups; shared root fixtures. |
| `migrations/` | Existing jobs Alembic revision history and environment. |
| `docker/` | Database initialization scripts mounted by Compose. |
| `docker/monitoring/` | Prometheus scrape/alert configuration, rule tests and provisioned Grafana dashboard. |
| `docs/` | Design explanations, setup, guarantees and operational instructions. |

Jobs and inbox storage remain separate. The inbox's storage module now belongs
to `workwise/notifications/store.py`; the worker imports it explicitly. Shared
infrastructure helpers do not import API routers or create application instances.
Use absolute `workwise.*` imports to make these boundaries visible.

## Entry points

| Purpose | Command |
|---|---|
| Public API | `python -m uvicorn workwise.api.main:app --reload --no-access-log` |
| Internal inbox API | `python -m uvicorn workwise.notifications.api:app --host 0.0.0.0 --no-access-log` |
| Publisher | `python -m workwise.messaging.publisher` |
| Worker | `python -m workwise.messaging.worker` |
| DLQ replay | `python -m workwise.messaging.replay --event-id <UUID>` |
| Create admin | `python -m workwise.cli.manage_users create-admin <username>` |
| Read-only jobs check | `python -m workwise.cli.check_db` |
| Jobs migrations/grants | `python -m scripts.database.migrate` |
| Inbox migrations/grants | `python -m scripts.database.migrate_notifications` |
| Docker config | `python -m scripts.docker.create_env --add-missing` |
| Full test suite | `python -m pytest -q --tb=short` |

The internal service and background commands require their configured databases
and broker; Compose supplies those environments. For the complete Docker demo,
use `docker compose --env-file .env.docker up --build --detach --wait`.

The root `main.py` remains a small compatibility entry point, so the earlier
`python -m uvicorn main:app --reload` command still loads the same application.
New documentation and Docker use the explicit package path.

Smoke demos are now `python -m scripts.smoke.api`,
`python -m scripts.smoke.concurrency`,
`python -m scripts.smoke.distributed --allow-service-interruption` and
`python -m scripts.smoke.notifications --allow-service-interruption`.
They retain their existing disposable-stack behavior; see the Docker and
distributed-systems guides before running interruption demos.

## Configuration and migrations

`.env`, `.env.docker`, their examples, `alembic.ini`, `pytest.ini`, Dockerfile and
Compose configuration remain at the root. `workwise/core/paths.py` locates that
root consistently on Windows and inside the Docker image. Scripts run as modules,
without editing `sys.path`. `pytest.ini` points discovery to grouped tests.

This reorganization does not add a database revision or change API contracts.
Alembic history stays in place; its imports reference the packaged models/settings.
The public OpenAPI document was compared before/after the route split and is identical.
