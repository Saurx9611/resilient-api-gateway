# ADR-006: Independent PostgreSQL worker runtime

Status: Accepted, 2026-10-27. Implements the user's independent worker Phase 3
scope request as a companion subsystem. The Go gateway circuit breaker remains a
separately tracked Phase 3 item; this ADR neither renumbers nor implements it.
Retry policy, monotonic generations, idempotent enqueue/effect ledger, and manual
retry/replay behavior are extended by ADR-008.

## Necessity decision

Problem/evidence: the requested Phase 3 requires independently scheduled Python
processes, durable shared jobs, renewable leases, retries, and SIGTERM/SIGKILL
recovery. The existing Phase 3 in PLAN.md is a Go gateway circuit breaker and
cannot satisfy those requirements. Doing nothing leaves the explicit worker
request unmet; pretending the breaker and worker are the same feature would be
misleading. MUST implement the worker request as an isolated companion runtime;
MUST keep its evidence and lifecycle separate from gateway circuit-breaker evidence.

Python is required by the request. PostgreSQL is required for a shared durable
queue and transactional row claims. Use psycopg 3, the standard library's
`multiprocessing` spawn mode, and one controller connection per worker process.
No FastAPI dependency or web server is introduced. No Redis, frontend, general
task framework, shell runner, plugin loading, arbitrary code, or client-supplied
handler is necessary; reject/defer them. A small CLI only migrates, enqueues
validated allowlisted jobs, and starts workers.

## Claim, lease, and execution contract

Claim one ordered row in a short transaction using `FOR UPDATE SKIP LOCKED`,
increment attempts, assign a fresh lease token and worker UUID, and record the
claim event in that transaction. PostgreSQL `clock_timestamp()` is authoritative
for lease comparisons. Renew only an unexpired matching `(job, worker, token)`;
never resurrect an expired lease. Complete/fail/abandon only with the same live
token. This fencing prevents a stale worker from changing the row after another
worker reclaims it; it cannot undo side effects outside PostgreSQL.

Execution is at-least-once. A killed worker leaves a running row until lease
expiry; another process can then reclaim it. Retried handlers must be idempotent.
The included workloads either compute bounded results or simulate delivery and
have no external side effect. Backoff is deterministic exponential, capped at
five minutes; a claim at the attempt ceiling is abandoned after its lease ends.
No retry jitter, outbox, exactly-once promise, or cross-database transaction is
included.

Run the four fixed handlers in short-lived spawned child processes. This is
necessary to enforce a hard handler deadline without unsafe thread termination
and to isolate interpreter failures. Parent worker processes supervise children,
renew leases, and never pass a shell command or import path from a job. A hard
kill of a worker process/container also kills its child process group; lease
expiry handles the durable job recovery. Handler payload and result sizes,
concurrency, runtime, CSV row counts, and simulated notification delay are
bounded. Image processing accepts PNG bytes only. Notification delivery is a
simulation; no message is sent to a real recipient.

One synchronous controller loop per worker owns one PostgreSQL connection and
supervises at most the configured number of job subprocesses (1..16). This avoids
an additional thread pool and prevents the handler GIL from serializing work.
Transient database failures close the broken connection, terminate active child
work whose lease can no longer be renewed, log a redacted structured error, and
retry connection establishment with a bounded delay. The row remains recoverable
by lease expiry if the database is unavailable.

Worker liveness is a database heartbeat row keyed by process UUID, with active,
draining, or stopped state. Job events record claim/reclaim, success, retry,
timeout, permanent failure, and abandonment; heartbeat ticks are not duplicated
as unbounded event rows. Payloads, recipients, DSNs, error messages, and stack
traces are not logged. The development Compose stack is isolated from the
existing gateway Compose stack, binds no PostgreSQL host port, and uses
disposable local credentials and project-scoped storage, removed by Compose `down -v`.

## Alternatives and trade-offs

* Go goroutines in the existing gateway: reject; violates independent Python
  runtime and request scope.
* Redis list/stream or third-party queue framework: reject for this phase; the
  requested authority is PostgreSQL and simple row locking is sufficient.
* Run handlers in the worker thread: reject; cannot safely enforce hard timeouts.
* Exact once / distributed handler transaction: reject; external side effects
  cannot be made atomic with a PostgreSQL lease without a separate outbox and
  idempotent downstream contract.
* Persist an event for each lease renewal: reject; it creates write amplification
  without necessary diagnostic value. Current lease and worker heartbeat state
  plus attempt lifecycle events provide the requested evidence.
* Unbounded queue, arbitrary Python callable/import, shell command, network
  notification, image library, or dynamic handler registry: reject; they add
  execution or resource exposure beyond the demonstrable workloads.

Trade-offs: spawning isolates timeouts but costs process startup and serializes
job arguments/results; the strict 512 KiB payload/result bounds cap that cost.
PostgreSQL's `SKIP LOCKED` intentionally does not provide a globally consistent
queue snapshot. Lease recovery can duplicate a handler that ran past lease loss;
fencing protects database state, not external side effects. The included
simulated workloads establish mechanics, not a production delivery guarantee.

## Verification that can change this decision

Run Python unit tests and PostgreSQL integration tests through a disposable local
Compose stack. Verify multiple OS worker processes distribute jobs, competing
transactions cannot own one valid lease simultaneously, lease expiry plus SIGKILL
reclaims a job, SIGTERM drains active work, timeout retries, and a terminated DB
connection is re-established. Run Go tests too to detect regressions in the
existing gateway. No throughput or production readiness claim follows.

Sources: PostgreSQL `SELECT` and `SKIP LOCKED` documentation
(https://www.postgresql.org/docs/current/sql-select.html); Psycopg 3 official
documentation (https://www.psycopg.org/psycopg3/docs/).
