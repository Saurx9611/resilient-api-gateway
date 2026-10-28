# ADR-017: Deploy Chronos as a separate Render Blueprint

Status: Proposed deployment topology; provisioning awaits workspace confirmation
and explicit recurring-cost approval.

## Necessity decision

Problem: Chronos needs an always-on authenticated HTTP control plane, durable
PostgreSQL state, independent long-running job executors, a separate cron
materializer and the server-rendered Next.js dashboard. The existing root
`render.yaml` describes the Go gateway, its two simulators and Redis. Replacing
that topology risks changing an unrelated deployed system. Doing nothing leaves
Chronos undeployed; merging both products into one service would violate their
existing process and failure boundaries. A distinct `render.chronos.yaml`
Blueprint is **must**.

## Decisions

* Use Render web services for FastAPI and the Next.js BFF/dashboard, one
  PostgreSQL database in the same region, a standalone scheduler worker, and a
  two-instance background-worker service. This keeps the current API/scheduler/
  worker separation and gives a second executor for process-level recovery.
* Use the `0.5c-512mb` plan for each of five service instances (dashboard, API,
  scheduler, two workers) and `0.5c-1g` Postgres. This is a low-resource initial
  operating profile, not a capacity claim. Two worker instances are retained
  because a single process cannot demonstrate worker replacement while another
  executor remains available.
* Let the Next.js BFF call FastAPI through Render's private host-port reference.
  Keep the dashboard's existing user-entered bearer-token flow; do not put an
  API key in the public browser bundle.
* Use the existing bounded psycopg pools: API maximum 8, scheduler maximum 2,
  and one long-lived connection per worker process. With two old/new API
  instances during a zero-downtime deploy, expected application connections
  remain far below the database plan's 100-connection limit. Managed PgBouncer
  is **defer** until observed connection pressure justifies it; transaction
  pooling can change session semantics and adds no useful capacity at this load.
* Run the idempotent, transaction-advisory-locked migration in each Python
  service's paid pre-deploy command. Parallel deploys serialize at the database
  lock; only the schema migration is performed, never demo jobs or arbitrary
  initialization.
* Use paid always-on services. Free HTTP spin-down and free-database expiry are
  incompatible with a persistent queue and independent polling workers.
* Keep `/readyz` as the API deploy check (it checks schema/database readiness),
  dashboard `/api/healthz` as dashboard process liveness, operator auth on
  `/metrics`, fixed application rate limits, and JSON logs. Worker health is
  established by durable heartbeat freshness and Render's service state; a
  worker has no inbound HTTP listener.
* Set Git deployment to `checksPass` on the repository's default branch. GitHub
  push/PR checks gate Render auto-deploy. No image registry, Redis, Vercel,
  extra proxy, observability SaaS, or new queue framework is necessary.

## Trade-offs and cost estimate

Current Render public pricing reviewed on 2026-10-28: five `0.5c-512mb` service
instances at $7/month each = $35; Postgres `0.5c-1g` at $19/month plus the
Basic tier's default 15 GB disk, with 1 GB included and 14 GB at $0.30/GB/month,
is about $23.20; total incremental Chronos estimate is **$58.20/month** before
usage overages, taxes or existing gateway services. This assumes the Hobby
workspace ($0). A Pro workspace adds $25/month if the account must upgrade for
another selected feature. Pricing can change; recheck the Render billing screen
before approval. Five GB monthly bandwidth is included on Hobby, then overage
pricing applies. Disk autoscaling is disabled to avoid unapproved cost growth;
monitor disk use and schedule an explicit disk increase before the limit.

No high availability standby is included. The database is a single-region
failure domain. The API and dashboard each start with one instance. Worker
process death is recoverable through Postgres leases and a second executor, but
database failure, a deploy-time migration incompatibility, or multi-service
region outage is not made highly available by this topology.

## Verification

Before provisioning: validate the Blueprint against Render's current schema and
check the selected workspace's existing resources and billing plan. Before
calling the system operational: deploy from the GitHub-backed Blueprint; verify
all deploy events, HTTPS health and readiness, PostgreSQL schema version, API
submission and worker heartbeat/completion, bounded timeout retry, a recurring
occurrence, and a graceful worker SIGTERM/redeploy while a bounded job is active.
Report lease recovery only if an observed expired-generation replacement is
confirmed in PostgreSQL events. Existing local PostgreSQL process tests do not
substitute for cloud evidence.
