# ADR-015: Worker observability and repeatable performance measurements

Status: Accepted for Chronos worker companion Phase 7.

## Necessity decision

Problem/evidence: the worker dashboard currently derives historical activity
from lifecycle events, but it cannot distinguish queue wait from handler time,
expose process claim latency, or report DB pool utilization. Workers use one
persistent DB session each; the API opens a new session per transaction. Queue
claim and aggregation plans have not been measured. Doing nothing leaves the
requested operational measurements and reproducible performance evidence
missing. Low-cardinality metrics, structured outcomes, a bounded API connection
pool, query-plan evidence, and repeatable benchmarks are MUST for this phase.

## Choices

* Use the maintained Prometheus Python client with fixed job-type and outcome
  labels. Job IDs, worker UUIDs, names, principals, and exception strings are
  excluded from labels. Histogram quantiles are calculated from observed bucket
  counts by Prometheus, not emitted as fabricated exact percentiles.
* Persist a `started` event once per claimed generation after its handler starts.
  This gives PostgreSQL an explicit queue-wait boundary and execution boundary.
  Existing claim/success/retry/dead-letter events remain the source for durable
  summaries; process metrics add claim latency and current resource signals.
* Use a bounded `psycopg_pool` for control-plane transactions and export pool
  size, available, and waiting counts. Worker processes retain one persistent
  connection, consistent with ADR-006. The benchmark records actual PostgreSQL
  sessions rather than inferring them from configured concurrency.
* Keep Prometheus and Grafana optional in a local observability Compose profile.
  Scrape endpoints are exposed only on the internal Compose network; the
  authenticated UI continues using the authenticated operational API. No
  unauthenticated scrape endpoint is published to the host.
* Benchmark valid allowlisted synthetic-report payloads at explicit queue sizes
  and worker counts. Record unsupported configurations as unsupported; never
  fill gaps with estimates. Capture PostgreSQL settings, plans, host/container
  limits, raw JSON and SVG plots. Change queue SQL only when EXPLAIN and measured
  results show a specific bottleneck.

## Rejected complexity and trade-offs

Do not add a metrics database, per-job metric labels, renewal events, an external
telemetry agent, a benchmark framework, fake dashboard samples, or a new queue
implementation. Prometheus histograms have bounded quantile resolution and
reset on process restart; durable PostgreSQL events preserve lifecycle history.
The API pool bounds connection use but can queue requests when saturated, so
pool wait/timeout is measured. Docker benchmark results are local observations,
not production capacity claims. Grafana is optional and its dashboard is a
starting point, not a substitute for deployment-specific alerting.

## Verification

Run formatting, unit tests, full real-PostgreSQL integration tests, worker
process tests, API auth/metrics tests, Compose validation, benchmark matrix where
the Docker resource budget permits, and `EXPLAIN (ANALYZE, BUFFERS)` on the claim
selector. Persist the actual environment, PostgreSQL statistics and outputs;
record failures and skipped worker counts explicitly. No exact-once guarantee is
made.
