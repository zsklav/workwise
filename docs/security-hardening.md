# Authentication sessions, throttling and broker boundaries

This checkpoint adds rotating refresh tokens, account-wide logout, shared
PostgreSQL authentication rate limits and separate RabbitMQ identities.
It is a development hardening checkpoint, not a production security audit.
Password recovery, MFA, deployment TLS and ingress protections remain separate work.

## Login, refresh and logout

`POST /auth/login` retains its password-grant form. Successful responses now
include `access_token`, `token_type`, `expires_in`, `refresh_token` and
`refresh_expires_in`. Access tokens default to 30 minutes; refresh sessions
default to seven days. Token responses use no-store/no-cache headers.

Send refresh credentials as JSON:

```http
POST /auth/refresh
Content-Type: application/json

{"refresh_token": "<the 64-character token returned at login>"}
```

Each successful refresh returns a new token pair. The refresh credential is
single-use: store the replacement and discard the previous value. Rotation
retains the original absolute expiry, so continuous refresh cannot extend a
session indefinitely. Unknown, expired, revoked, disabled-user and stale-epoch
tokens return generic 401. Malformed JSON/token formats return 422 with credential
inputs removed. Do not blindly retry refresh after a lost response.

Refresh credentials are opaque 384-bit random values. PostgreSQL stores only
their SHA-256 hashes, user IDs, account epochs, expiry and used/revoked timestamps.
Raw tokens are returned to the client once and are absent from application logs,
traces and metrics. Used hashes are retained through expiry to detect replay.
Secrets are not cookies in this API; browser clients still need a deliberate
secure storage/CSRF/XSS design. Real deployments must use HTTPS.

Reusing a consumed, unexpired, otherwise valid refresh token is treated as a
possible theft: the transaction increments the user's token epoch and revokes
**all** their refresh sessions, including tokens issued to another device.
Outstanding access JWTs then fail their database epoch check. This intentionally
also logs out a legitimate client that raced/retried a refresh. Already revoked
or expired tokens simply fail; they cannot repeatedly invalidate later logins.
Password login can create a new session afterward.

`POST /auth/logout` takes the current access bearer token and returns empty 204.
It also revokes **all devices**, not just the presenting token. There is no
single-device logout or session-management UI. Current in-flight operations
that authenticated before logout are not canceled; subsequent authentication
checks reject the old epoch. Fresh password login after logout works normally.

Login issuance, refresh and logout lock the same user row before changing
session state. Refresh also reloads/locks its token after acquiring that user
lock. This serializes rotation/revocation across API processes and avoids a
token/user lock-order cycle. Concurrent refresh tests assert one initial success,
one reuse rejection and rejection of the winner's session after containment.

Access JWTs now require a positive integer `ver` claim matching
`users.token_version`; other signature/issuer/audience/time/type checks remain.
**Previously issued access JWTs without this claim require sign-in again**.
The epoch is server-managed and not editable in signup/user schemas. Current
roles/activation still come from the database. JWT signing-key rotation alone
does not revoke opaque refresh credentials; revoke account sessions as well
when responding to a compromise.

## Shared authentication limits

Limits are atomic fixed windows stored in PostgreSQL. They count every validly
formed attempt, including successes, unknown accounts and wrong passwords.
Reservations commit before Argon2 verification so a failed login does not undo
its budget. Account names are normalized using the login normalization rules.
Raw account names and peer addresses are not stored: bucket keys use HMAC-SHA256
with the configured signing secret and an action/scope prefix.

| Setting | Default per 60-second window |
|---|---:|
| `AUTH_LOGIN_IP_LIMIT` | 30 |
| `AUTH_LOGIN_ACCOUNT_LIMIT` | 10 across peer addresses |
| `AUTH_SIGNUP_IP_LIMIT` | 20 |
| `AUTH_REFRESH_IP_LIMIT` | 60 |

`AUTH_RATE_WINDOW_SECONDS` configures the fixed window (10..3600 seconds).
Settings have bounded positive ranges; no public disable flag is added.
Successful logins do not reset the window. Limits return 429, `Retry-After` and
no-store. Database/lock/query failures during reservation return generic 503
with Retry-After 5; there is no in-memory fallback that admits requests when
shared state is unavailable. Reservation SQL has two-second lock and five-second
statement timeouts. Overall response time also depends on database pool/network
availability and password hashing.

These are application limits, not a volumetric DDoS defense. Fixed-window
boundaries can admit two adjacent windows' budgets in a short interval; NAT
users share an IP budget, and attackers can temporarily throttle an account.
Unknown accounts get the same limits/error bodies. JSON/form validation failures
can return 422 before these limits and must be bounded by a real ingress/body
size policy. Multi-node deployments share limits only when using this database
and signing secret. Signing-key rotation changes HMAC bucket keys and resets
the effective budgets until old records are cleaned up.

The limiter uses the ASGI peer address, not an application-parsed forwarded
header. Docker and the native benchmark run Uvicorn with `--no-proxy-headers`.
Use the same flag natively:

```powershell
python -m uvicorn workwise.api.main:app --reload --no-access-log --no-proxy-headers
```

A future reverse proxy needs an explicit trusted-proxy design; otherwise all
traffic may share its peer address, or a misconfigured server may trust forged
forwarding headers. This stack does not automatically trust a proxy chain.

## Broker identities and topology

The operator-only `broker-setup` service provisions the existing durable main
and dead-letter queues, two durable direct exchanges and exact bindings. It
creates/updates separate random service passwords in ignored `.env.docker`.
Existing broker data, queue names and message/event contracts are preserved.
RabbitMQ is pinned to the verified 4.2.9 management image, including the patched
passive-declaration authorization behavior.

| Identity | Write exchanges | Read queues | Configure / management API |
|---|---|---|---|
| `workwise_publisher` | Main events | None | Denied |
| `workwise_worker` | Main events (retry), dead letters | Main queue | Denied |
| `workwise_replay` | Main events | Dead-letter queue | Denied |
| Existing `workwise` operator | Full operator permissions | Full operator permissions | Allowed |

Main exchange is `workwise.notifications.events.v1`; dead-letter exchange is
`workwise.notifications.dead.events.v1`. Exact routing keys bind each exchange
to its queue. Runtime identities cannot configure or bind topology, so the
publisher cannot bypass its write scope by using another routing key. They
receive no permission on `amq.default`. Dedicated exchanges are necessary
because permission on the default exchange would not isolate destinations.
Worker/replay read rights also permit reading/purging their assigned queues:
RabbitMQ resource permissions do not offer per-method restrictions for those
read operations. Trusted services must still handle message contents correctly.

Runtime processes receive only their own broker secret. They no longer receive
the operator credential. Bootstrap has the operator credential and the three
service passwords, but no application/notification database or JWT credentials.
Normal runtime reconnects do not declare topology; missing queues/bindings need
operator repair. Definitions survive normal broker restarts in its named volume.

Upgrade configuration and recreate the stack after migration:

```powershell
python -m scripts.docker.create_env --add-missing
docker compose --env-file .env.docker up --build --detach --wait
```

For changed permissions/passwords on an existing stack, explicitly rerun
`broker-setup` and recreate publisher/worker connections; authorization can be
cached on an established connection. Provisioning does not display secrets and
does not delete existing queues. A rollback to the old default-exchange producer
requires coordinated permission/topology changes; it is not an isolated binary
rollback. Real operator credentials should remain private and restricted.

Replay now uses its own short-lived tool container:

```powershell
docker compose --env-file .env.docker run --rm --no-deps replay --event-id <UUID>
```

## Migration, permissions and retention

Revision **0010** adds positive `users.token_version` default 1,
`auth_refresh_tokens` and `auth_rate_limits`. Existing user hashes, roles,
activation, job/company rows and resource versions are preserved. Refresh
credentials cascade away if an operator deletes their account. The migration
runner grants runtime SELECT/INSERT/UPDATE on the two auth tables, plus UPDATE
only on the users' token_version column. It does not grant user role/password
updates, account deletion, auth-table deletion or schema management.

Run `python -m scripts.database.migrate` before starting the updated API.
Downgrade loses refresh/limit state and token epochs; test it only on disposable
data. Existing access tokens need sign-in again after the new claim contract.

Operator retention adds `auth-tokens` and `auth-limits` targets. They preview by
default, require `--apply`, and share the existing bounded/locked batch rules.
The default 30-day cutoff is measured **after** token expiry/window expiry, not
after issuance. Active/recently expired state survives. Keep consumed refresh
hashes until expiration so replay detection remains possible. Example native
preview: `python -m workwise.operations.retention auth-tokens`. Rate-window
rows can grow with distinct peers/accounts until operator cleanup; no automatic
schedule is installed.

## Verification and limits

PostgreSQL tests cover rotation, absolute expiry, hashed storage, replay
containment, all-device logout, disabled/stale/expired tokens, strict epoch
claims, redaction, normalized limits, window reset, fail-closed behavior,
concurrent rate reservations/refreshes, legacy migration and auth retention.
The full existing CRUD, messaging, monitoring and performance checks also run.

Docker CI verifies actual HTTP refresh/logout/reuse, the account throttle and
Retry-After, real AMQP permission denials for publisher/worker/replay, denied
management/default-exchange access, narrow SQL column permissions, and the
existing delivery/retry/DLQ/replay tests with the new identities.
No external emails/messages, live deployment or production penetration test is
part of this phase. Password reset and MFA remain unimplemented.

References: [OAuth refresh replay/rotation guidance](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14),
[OWASP authentication throttling](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html),
[RabbitMQ resource permissions](https://www.rabbitmq.com/docs/access-control),
[patched passive-declaration authorization](https://github.com/rabbitmq/rabbitmq-server/security/advisories/GHSA-9q2j-2hq8-22r2).
