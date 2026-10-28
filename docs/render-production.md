# Chronos production deployment on Render

Deployment state: preparation only. No Render resources were created by this
change. The Render workspace must be selected, live resources reviewed, and the
estimated recurring cost explicitly approved before provisioning.

## Architecture

`render.chronos.yaml` is a separate Blueprint so the existing root `render.yaml`
continues to manage the Go gateway demo. Create a second Render Blueprint from
the same GitHub repository and set its Blueprint path to `render.chronos.yaml`.
Use the repository default branch and enable deployment only after GitHub checks
pass. The Blueprint defines:

| Role | Render service | Count | Plan | Check/operation |
|---|---|---:|---|---|
| Dashboard and same-origin BFF | `chronos-dashboard` web | 1 | `0.5c-512mb` | `/api/healthz` |
| Authenticated API | `chronos-api` web | 1 | `0.5c-512mb` | `/readyz` checks PostgreSQL/schema |
| Job execution | `chronos-worker` background worker | 2 | `0.5c-512mb` each | DB heartbeat; SIGTERM drain |
| Cron occurrence materializer | `chronos-scheduler` background worker | 1 | `0.5c-512mb` | structured scheduler logs |
| Durable state and queue | `chronos-postgres` managed Postgres | 1 | `0.5c-1g` | internal-network DSN |

All services and the database are configured for Oregon to match the existing
gateway Blueprint. Confirm the account's live region before first sync; database
region and major version are immutable after creation. PostgreSQL 16 matches the
repository's tested integration environment. The Next.js BFF obtains the API's
private `host:port` from a Blueprint reference and prefixes HTTP only for that
private service connection. Public access is over Render's HTTPS `onrender.com`
endpoint and managed TLS.

## Secrets and database

During the initial Blueprint creation, enter `CHRONOS_API_KEYS` as a secret JSON
object. Create a random token of at least 32 characters and keep it in a password
manager. Example shape (replace the placeholder; never use it as a real key):

```json
{"chronos-operator":{"token":"REPLACE_WITH_RANDOM_SECRET_AT_LEAST_32_CHARS","scopes":["read","submit","operator"]}}
```

The dashboard asks its user to enter that bearer token; it stores it only in the
current browser session and the server-side BFF forwards it. The token is not
embedded in the Next.js build or public HTML. Rotate it by replacing the JSON
value in Render and redeploying the API. Do not add production credentials to
`.env`, this repository, CI logs, job payloads or this guide.

The API's psycopg pool is bounded to 8 connections; the scheduler pool is 2;
each of two worker parent processes uses one durable database connection and
handler child processes do not connect to PostgreSQL. Migrations use a temporary
connection. Budget fewer than 30 application connections during normal deploys,
including temporary API overlap, against the selected 100-connection database
limit. Recalculate before increasing replicas or pool sizes. No external
database URL is used. Managed PgBouncer is disabled until measured connection
pressure justifies enabling it.

Every Python service runs `python -m worker_runtime migrate` as its pre-deploy
command. The existing migration function takes a transaction-scoped advisory
lock and applies idempotent schema changes. This protects concurrent service
deploys from applying the DDL simultaneously. Keep migrations backward
compatible across rolling service changes.

## CI/CD, health, and operations

Render auto-deploy waits for GitHub status checks. The repository workflows
cover Go/Redis, Python/PostgreSQL, frontend build, migration validation and
container checks. Confirm that Render detects the expected check names for the
selected branch and that a failing check blocks the deploy before relying on
this gate. Render's worker service is an always-running background process; it
does not wait for HTTP traffic and does not use the free web-service sleep model.

The API's `/healthz` is process liveness. `/readyz` queries the database schema
and is Render's HTTP deploy/readiness check. It does not prove worker health. Use
`/v1/workers`, the overview and observability pages, and Render worker logs to
check heartbeat freshness and current work. `/metrics` requires operator scope;
do not configure unauthenticated public scraping. Application logs are bounded
JSON events without payloads or credentials. Render supplies service CPU/memory
and HTTP metrics; Chronos's protected API summaries expose queue and worker
measurements.

Render sends SIGTERM during replacement. Workers stop claiming, drain up to the
configured 45-second process grace, then exit; the service has a 60-second
platform shutdown allowance. If the service is killed before acknowledgment,
PostgreSQL lease expiry and generation fencing allow another worker to reclaim
the job. Handler effects can be repeated; only the idempotent demonstration
notification uses a PostgreSQL effect ledger. Do not claim exactly-once delivery.

## Deployment smoke and recovery evidence

After the cost-approved Blueprint sync and deployment, record the exact service
IDs, image/source commit, deployment IDs, timestamps and health results. Verify
using the dashboard with the operator token and the following bounded API flow:

1. Submit one `synthetic_report` job with an `Idempotency-Key`; verify `202`,
   repeat the identical request and verify the same job ID, then observe
   `succeeded`, worker identity, generation, and attempt history.
2. Submit a `notification_simulation` with `delay_ms: 5000`,
   `max_runtime_seconds: 1`, and `max_attempts: 2`. It intentionally exceeds
   its own bounded runtime; verify timeout, retry backoff, second attempt and
   terminal dead-letter state. This is a job timeout test, not arbitrary fault
   injection.
3. Create a recurring schedule for a safe `synthetic_report`, wait for one
   UTC occurrence and verify the unique occurrence/job event. Disable it after
   the occurrence check.
4. Start a bounded `notification_simulation` delay, redeploy or restart one
   worker instance through Render, and capture the worker's SIGTERM/drain logs.
   Verify the active job completes or is retried after lease expiry with a newer
   generation. If graceful draining completes the job, report that result and
   do not claim lease recovery from that run.
5. Run `scripts/verify-workers.ps1` for the forced SIGKILL recovery scenario on
   the disposable local PostgreSQL stack; this supplements, but does not replace,
   cloud termination evidence.

If a deploy fails, leave the last healthy service revision in place, inspect its
deploy events/logs, and roll back the relevant service to its last successful
deploy. Database migrations are forward-only; use the [operations runbook](operations-runbook.md)
for recovery and restore guidance. Before production traffic, conduct the
isolated PostgreSQL backup/restore drill and define a public-demo ingress budget.

## Monthly cost estimate (USD)

At Render's published pricing checked 2026-10-28, the planned Chronos increment
is approximately **$58.20/month** on a Hobby workspace:

| Resource | Calculation | Monthly estimate |
|---|---:|---:|
| FastAPI web service | 1 × $7 (`0.5c-512mb`) | $7.00 |
| Next.js dashboard | 1 × $7 (`0.5c-512mb`) | $7.00 |
| Scheduler worker | 1 × $7 (`0.5c-512mb`) | $7.00 |
| Job workers | 2 × $7 (`0.5c-512mb`) | $14.00 |
| PostgreSQL compute | 1 × $19 (`0.5c-1g`) | $19.00 |
| PostgreSQL disk | 15 GB default; first 1 GB included; 14 × $0.30 | $4.20 |
| **Total incremental Chronos** | | **$58.20** |

This excludes the pre-existing gateway stack, taxes, over-limit bandwidth/build
usage, and a possible $25/month Pro workspace subscription. The database plan
uses a 15 GB disk with storage autoscaling disabled; disk growth changes the
bill and cannot be reversed by shrinking. Re-check the selected workspace's
plan and the Render billing confirmation before provisioning. This estimate is
not an invoice or a cost ceiling.
