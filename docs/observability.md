# Request logs and metrics

WorkWise emits one structured HTTP request event and exposes aggregate metrics
for administrative inspection. This checkpoint does not install a monitoring
backend or claim measured performance improvements.

## Request IDs and logs

Every HTTP request receives a UUID4 `X-Request-ID`. The server generates it;
incoming IDs are ignored and an application-provided duplicate response header
is replaced. The same ID is placed in request state, a context variable, and
the JSON completion event. Context is reset even on errors or cancellation,
and concurrent requests retain separate IDs. This is request correlation,
not distributed trace propagation.

The `workwise.requests` logger writes JSON to stdout with an allowlist:

- UTC timestamp, level, and event name;
- request ID, normalized method, route template, and response status;
- duration in milliseconds, response-start/completion flags, and exception type.

Request/response bodies, raw URLs, query strings, headers, tokens, cookies,
client/user identifiers, exception messages, and local variables are not part
of these records. Routes come from routing metadata; unmatched paths use
`__unmatched__` rather than the incoming path. Unknown methods use `OTHER`.

Start native Uvicorn with the flag shown below. Its default access logger
includes raw URLs and is independent of WorkWise's sanitized request logger.
The Docker command already disables that duplicate access logger.

```powershell
python -m uvicorn workwise.api.main:app --reload --no-access-log
```

Expected HTTP errors retain their existing status and body. An unhandled
application exception before response headers produces generic JSON
`{"detail": "Internal server error"}` with status 500, plus a request ID and
sanitized error event. Its exception message/traceback is not emitted by this
request logger. After a streaming response has started, middleware cannot send
a second status: it records the original status, incomplete response and error
type, then propagates the error so the server can terminate the stream.
Server/runtime diagnostics are separate and may emit their own traceback in
that case; no streaming business endpoint exists at this checkpoint.

Cancellation before headers is recorded with operational status 499 and no
manufactured response. The middleware passes lifespan/WebSocket scopes through
unchanged. Request duration covers the HTTP ASGI call, including response
streaming and background work in that call; it is not database query time or
time-to-first-byte.

## Metrics

`GET /metrics` returns Prometheus exposition text and `Cache-Control: no-store`.
It requires a valid active admin's bearer token. No credentials gives 401;
member credentials give 403. The current database role is checked on every
request, so downgrading an admin immediately removes access on the next request.

| Metric | Labels | Meaning |
|---|---|---|
| `workwise_http_requests_total` | method, route, status | Observed HTTP calls |
| `workwise_http_request_duration_seconds` | method, route | Duration histogram with default client buckets |
| `workwise_http_unhandled_errors_total` | method, route | Unhandled application exceptions |

Actual IDs, user inputs, and request IDs are never metric labels. Histogram
`_count` and `_sum` are exported along with buckets. A streaming failure after
headers can increment the exception counter while retaining HTTP status 200,
because that status was already sent. Handled 401/403/404/422/503 responses are
counted by status but do not increment the unhandled-exception counter.

The metrics response is rendered before its own request-completion observation,
so that observation appears on the next snapshot. Health/readiness and rejected
metrics requests are also counted. A private CollectorRegistry holds only these
application metrics; default process collectors are not registered here.

Counters and histograms are per process, held in memory, and reset at restart.
The current Docker/Uvicorn configuration uses one worker. Multiple workers
require a deliberate collection/aggregation design before interpreting results.
The admin endpoint relies on the database for authentication, so database failure
can also prevent metric access. Existing 30-minute JWTs support manual snapshots;
durable automated scraping credentials are not implemented.

No Prometheus server, Grafana, alerting or external error/log storage is configured.
The later [inbox/tracing checkpoint](notifications-tracing.md) adds OpenTelemetry
OTLP export, W3C propagation across HTTP/outbox/AMQP, trace IDs in logs/responses,
and a Jaeger demo collector. Trace storage is transient; tracing is disabled when
the exporter endpoint environment variable is absent.

## Evidence

The full local PostgreSQL suite passed 219 cases on 2026-10-21. Fifteen
observability cases cover log/header correlation, sensitive-input exclusion,
bounded labels, streaming, generic errors, cancellation, concurrent context
isolation, non-HTTP passthrough, admin access/revocation, and request IDs on
normal and handled-error responses. Docker CI's HTTP smoke test also validates
UUID response headers and rejection of anonymous/member metrics access.

No database schema or stored business data changed in this checkpoint.

## References

- [Prometheus Python counters](https://prometheus.github.io/client_python/instrumenting/counter/)
- [Prometheus Python histograms](https://prometheus.github.io/client_python/instrumenting/histogram/)
- [Starlette pure ASGI middleware](https://starlette.dev/middleware/#pure-asgi-middleware)
