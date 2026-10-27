# Chronos worker performance report

Status: measurement harness added; results are populated only by local Docker
runs. Do not treat this report as a capacity or production-readiness claim.

## Reproduction

Prerequisites: Docker Compose with enough available resources, Python 3.11+,
`requirements.txt` installed. Run from the repository root:

```powershell
python scripts/benchmark_workers.py
python scripts/plot_worker_benchmarks.py
python scripts/analyze_worker_queue_sql.py
```

The worker matrix requests 1, 2, 4, 8 and 16 independent worker containers at
queue depths 20, 50 and 100. Each worker has concurrency 1, a 1 CPU / 256 MiB
container limit; benchmark PostgreSQL has 2 CPU / 512 MiB and is isolated on
loopback port 55432. The script checks Docker's reported memory and records an
unsupported worker count rather than estimating its result. The fixed handler is
the allowlisted `synthetic_report` with an empty records list. Payload seeding is
outside the timed completion interval. Throughput covers enqueued fixture rows
from the benchmark timer start until all rows reach a terminal state.

## Measurements

Each JSON file records host OS/Python/CPU and Docker limits, PostgreSQL version
and selected settings, job count/type, worker process/concurrency, observed DB
sessions, successful/failed counts, completion throughput, and sample-counted
claim p50/p95/p99 from structured worker logs plus queue-wait and execution
p50/p95/p99 derived from PostgreSQL lifecycle timestamps.
`summary.csv` is the tabular view, `run-metadata.json` records requested and
unsupported combinations, and `throughput.svg` is generated from measured JSON
only. `queue-claim-explain.json` records `EXPLAIN (ANALYZE, BUFFERS)` and
PostgreSQL table/index statistics for a 10,000-row ready queue.

No measured run has been recorded yet. The before/after comparison is therefore
pending; queue SQL changes are deferred until the captured plan and repeated
results identify a bottleneck. Missing or failed combinations must remain
unmeasured and must never be filled with estimates.

Raw outputs: `raw/`. Preserve environment and outputs with any externally cited
result. Reruns overwrite matching filenames; archive a copy before comparing
different hosts or code revisions.
