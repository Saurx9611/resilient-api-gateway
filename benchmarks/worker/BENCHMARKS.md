# Chronos worker performance report

This report records one repeatable local before/after run, not a capacity claim.
Raw per-case results and environment metadata are kept under `raw/before/` and
`raw/after/`; the matching claim-query plans are `raw/queue-claim-explain-*.json`.
The SVG is generated from those JSON files by `scripts/plot_worker_benchmarks.py`.

## Reproduce

Prerequisites: Docker Compose, Python 3.11+, and repository `requirements.txt`.
From the repository root:

```powershell
python scripts/benchmark_workers.py --label after
python scripts/analyze_worker_queue_sql.py --label after
python scripts/plot_worker_benchmarks.py
```

The harness asks Docker for its CPU/memory limits and records host OS/Python/CPU,
worker and PostgreSQL container limits, PostgreSQL version and configuration,
observed DB sessions, job count/type, worker count, elapsed time, throughput,
error rate, and sample-counted p50/p95/p99 for claim, queue wait, and execution.
It records unsupported worker counts instead of estimating them. Each raw JSON is
one observation, not a repeated-trial confidence interval. The handler is the
allowlisted `synthetic_report` with an empty records list; it exercises the real
claim, child-process, lifecycle event, and acknowledgment path but does almost no
application work. Seeding is outside the throughput timer. DB timings are from
PostgreSQL lifecycle timestamps; claim latency is saved in each `started` event.

## Environment and matrix

Observed on Windows 11 amd64, Python 3.14.7, Docker Desktop reporting 12 CPUs
and 7,606 MiB available. Benchmark workers were each limited to 1 CPU/256 MiB;
isolated PostgreSQL 16.15 was limited to 2 CPU/512 MiB, with `max_connections=100`,
`shared_buffers=128MB`, `work_mem=4MB`, `synchronous_commit=on`. Job counts were
20, 50 and 100 at worker counts 1, 2, 4, 8 and 16, each worker concurrency 1.
All 15 combinations were supported and returned zero job errors in each run.
The 100-job results:

| Workers | DB sessions after | Before jobs/s | After jobs/s | Claim p95 before (ms) | Claim p95 after (ms) |
|---:|---:|---:|---:|---:|---:|
| 1 | 2 | 1.581 | 1.605 | 7.119 | 6.070 |
| 2 | 3 | 2.600 | 2.742 | 8.155 | 8.607 |
| 4 | 5 | 3.587 | 4.574 | 12.637 | 8.476 |
| 8 | 9 | 4.662 | 4.956 | 36.822 | 16.334 |
| 16 | 17 | 4.690 | 7.020 | 144.331 | 54.982 |

The before and after runs were separate local executions, so results include
container scheduling and host noise. The optimization improved the observed
100-job throughput at 4/8/16 workers; this single matrix does not establish a
general scaling curve. Execution and queue-wait percentiles, per-case connection
counts, PostgreSQL table stats and environment details are present in each raw
JSON. For example, 16 workers used 17 database sessions including the benchmark
client. PostgreSQL pool metrics are exposed by the API; worker processes use one
persistent connection each.

## Claim candidate-selector investigation

`scripts/analyze_worker_queue_sql.py` runs `EXPLAIN (ANALYZE, BUFFERS)` against a
10,000-row ready queue and saves `pg_stat_user_tables`, `pg_stat_user_indexes`,
and relevant settings. Baseline candidate selection scanned and sorted the
10,000-row queue, taking 4.019 ms and 183 shared-buffer hits in this local plan.
After splitting queued and expired candidates into independently indexable,
materialized `FOR UPDATE SKIP LOCKED` branches and merging at most two rows, the
observed plan took 0.105 ms and 5 shared-buffer hits. This explains the
candidate-selection CTE, not the full claim transaction's update and event
insert. The selected indexes were
`worker_jobs_queued_priority_order` and `worker_jobs_current_worker`. The query
preserves the prior priority/availability/creation/id ordering. It may lock one
candidate from each branch before selecting one; transaction duration remains
short and this consequence is documented in ADR-015. Plan timings are one small,
warm, local fixture, not production query latency.

## Limits

This is a single-host, low-depth, synthetic handler experiment with no repeated
trials, external traffic, realistic payload CPU, network delivery, or production
hardware. Queue wait increases with batch depth and available worker throughput;
worker count alone does not guarantee lower completion latency. No benchmark
result is extrapolated to another host. Missing percentiles remain `null` with a
zero sample count. Reruns overwrite matching files, so copy the raw directory
before comparing another revision or environment.
