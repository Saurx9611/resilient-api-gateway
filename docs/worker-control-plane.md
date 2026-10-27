# Chronos scheduling and control API

The FastAPI service accepts and inspects work; it never imports or runs handler
code. A separate scheduler process materializes due cron occurrences into
PostgreSQL jobs, and the independent worker processes claim and execute those
jobs. PostgreSQL transactions and constraints coordinate all three roles.

## Local run

The Compose `control` profile includes PostgreSQL, migration, the HTTP API and
the independent cron scheduler. The optional `workers` profile adds three job
executors. It also builds the separate Next.js operator dashboard on loopback
port 3000. See the [dashboard runbook](chronos-dashboard.md) for UI setup,
security boundaries, screenshots and browser verification. The development
bearer key below is fixed for local use only; use a
secret manager and TLS-protected ingress outside this disposable demo.

```powershell
docker compose -p chronos-p5 -f compose.workers.yaml --profile control --profile workers up --build -d
Invoke-WebRequest http://127.0.0.1:8000/docs
Invoke-WebRequest http://127.0.0.1:3000
```

Swagger UI: <http://127.0.0.1:8000/docs>; OpenAPI JSON:
<http://127.0.0.1:8000/openapi.json>. Health and readiness are `/healthz` and
`/readyz`; readiness checks the schema, not worker or PostgreSQL failover health.

Run the executable request examples from the repository root:

```powershell
$env:CHRONOS_API_TOKEN = 'local-development-token-do-not-use-outside-compose-0001'
./docs/worker-api-examples.ps1
```

Stop the local stack and delete its disposable data with
`docker compose -p chronos-p5 -f compose.workers.yaml down --volumes`.

## Authentication and request limits

Send `Authorization: Bearer <token>`. `CHRONOS_API_KEYS` is a JSON object keyed
by a safe principal ID; each entry has a token (32–256 characters) and a scope
list. Supported scopes are `read`, `submit`, and `operator`; operator includes
the other two. Configure no more than 64 principals. Tokens are hashed in
process memory before comparison and principal IDs, not tokens, are stored in
jobs/rate-limit rows. Tokens are read at process start; rotate by restarting.

All authenticated API requests consume a PostgreSQL-enforced fixed 60-second
window budget per principal (`CHRONOS_API_RATE_LIMIT`, default 60). It is shared
between API replicas and its fixed-window boundary may allow a burst. Invalid
bearer attempts are rejected before body reads but are not stored in the rate
table; apply an additional request-rate limit at the trusted ingress. The API
does not trust `X-Forwarded-For` for caller identity. JSON request bodies are
read only after authentication and are capped at 64 KiB, including chunked
bodies. Schemas reject unknown top-level fields, and payloads must pass the
fixed worker-handler validators. Errors use `{"error":{"code","message",
"request_id"}}`; validation errors may also include bounded field locations.

## HTTP contract

| Method and path | Scope | Behavior |
|---|---|---|
| `POST /v1/jobs` | submit | Immediate when `run_at` is omitted; delayed when it is an offset-bearing timestamp. Priority is -10..10. |
| `GET /v1/jobs` | read | Descending ID cursor pagination, page size 1..100, optional state/type/created-time filters and bounded search over ID, job type or error code. |
| `GET /v1/jobs/{id}` | read | Current state, result, attempts, lease/generation and timestamps. |
| `GET /v1/jobs/{id}/attempts` | read | Append-only lifecycle events, ascending event-ID cursor. |
| `POST /v1/jobs/{id}/cancel` | operator | Queued jobs cancel immediately; running jobs request cancellation for their lease owner. |
| `POST /v1/jobs/{id}/retry` | operator | Reset a dead job's attempt budget and enqueue it with an audit reason. |
| `POST /v1/jobs/{id}/replay` | operator | Create a new job linked to a succeeded/dead source. Optional `Idempotency-Key` prevents accidental duplicate replay requests. |
| `GET /v1/workers` | read | Worker process identity, state and configurable heartbeat-staleness status; response capped at 500 rows. |
| `GET /v1/queue/stats` | read | Counts by state and ready/delayed/running queue totals. |
| `GET /v1/events` | read | Global newest-first lifecycle events with cursor pagination. |
| `GET /v1/observability?hours=24\|168` | read | Event-derived hourly/daily throughput, retries, failures and claim-to-success latency percentiles. |
| `GET /v1/settings/queue` | read | Current shared submission defaults. |
| `PUT /v1/settings/queue` | operator | Set validated defaults: max attempts 1..10 and runtime 1..300 seconds. Existing jobs are unchanged. |
| `POST /v1/schedules` | submit | Create a recurring five-field cron schedule and its timezone. |
| `GET /v1/schedules[/{id}]` | read | List or inspect schedules and next UTC occurrence. |
| `POST /v1/schedules/{id}/cancel` | operator | Disable future materialization; jobs already created remain independent. |

Job submission's `Idempotency-Key` is principal-scoped and hashed into the
database unique key. A canonical SHA-256 request fingerprint is stored alongside
it. Repeating an equal request returns the same job ID; reusing the key with
changed job type, payload, priority, attempt/runtime bounds or `run_at` returns
409. No key means the API creates a new job for each request. Replay uses the
same mechanism when an idempotency key is supplied.

Cancellation of a running job is cooperative: the API records a request, and
the worker stops its handler child at the next lease renewal (bounded by roughly
one third of the configured lease interval). If the worker died, the scheduler
does not recover that job as work; an expired cancellation is finalized by a
worker poll. A cancellation cannot undo an external side effect that already
occurred.

## Recurrence and daylight saving time

Expressions use the five-field croniter grammar (minute, hour, day of month,
month, day of week); seconds, macros and arbitrary handler names are rejected.
Cron dates describe local wall-clock time in the supplied IANA timezone. A
nonexistent local time in a spring-forward gap is skipped. If a fall-back fold
contains the same local time twice, only its earlier UTC instant is materialized.
Each resolved UTC timestamp is the occurrence identity. A unique
`(schedule_id, occurrence_at)` constraint and a `FOR UPDATE SKIP LOCKED` scheduler
transaction prevent two scheduler instances from creating the same occurrence.
If the scheduler is late, it catches up in occurrence order rather than
coalescing missed times, at most 100 schedules per poll transaction. This can
produce a catch-up burst after a long outage; schedule pause/disable is the
operator control. Updates to system timezone data affect future calculation;
already persisted UTC occurrences are not rewritten.

## Local API example

The script uses PowerShell `Invoke-RestMethod`, submits an idempotent synthetic
report twice, reads its state and attempt history, creates and cancels a cron
schedule, and fetches queue/worker status. It prints the responses for inspection.
No example runs user code, shell commands, or arbitrary network delivery.

Verification commands and observed PostgreSQL-backed tests are recorded in
`PLAN.md` and `docs/verification/phase-5-control-plane.txt`.
