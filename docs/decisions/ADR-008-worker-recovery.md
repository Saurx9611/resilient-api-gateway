# ADR-008: Worker recovery generations, idempotent submission, and replay

Status: Accepted, 2026-10-27.

## Necessity decision

Problem/evidence: ADR-006 has durable leases and UUID-token fencing, but queue
recovery needs a monotonic generation to make claim succession auditable, retry
jitter to decorrelate repeatedly failing jobs, explicit operator retry/replay,
and duplicate-submission protection. Existing execution is at-least-once; a
process can finish handler work and die before PostgreSQL records success.
Doing nothing leaves those recovery states hard to operate and duplicate inputs
can create redundant work. Generational fencing and transition history are MUST;
operator retry/replay and deduplicated submissions are SHOULD and required by
this request. Exactly-once effects are rejected as unattainable for arbitrary
external systems under a database lease.

## Design

Keep PostgreSQL authoritative. Each successful claim increments a `generation`
and creates a fresh UUID lease token. Every renew, success, and failure update
requires worker ID, token, generation, and an unexpired lease. Events capture the
generation and reason. Idempotency keys are optional, unique per job submission;
reusing a key with identical type/payload/max-attempts returns the original ID,
while changed contents are rejected. Replay creates a linked new job; retry
explicitly resets a dead job's attempt budget and immediately queues it. Both
operations append audit events and require a bounded operator reason.

Retry uses capped exponential delay with deterministic hash-derived 0.5–1.5
jitter keyed by job ID and attempt. This produces decorrelated stable delays and
keeps tests reproducible without storing a random seed. `max_attempts` remains
set at enqueue. The worker's configured handler deadline is the execution bound;
timeouts are killed and recorded through the same fenced retry path. Worker
heartbeat timestamps already exist; the status command exposes a configurable
stale threshold.

Demonstration handlers compute deterministic output. The simulated notification
records an idempotent effect in a PostgreSQL ledger keyed by stable job ID before
the completion acknowledgment. Tests kill a worker after this write and before
acknowledgment; replacement execution attempts the same effect key and records a
deduplication event. This demonstrates duplicate execution and one database-ledger
entry without claiming exactly-once delivery to an external service. Real external
effects still require a downstream idempotency contract or transactional outbox.

## Alternatives and trade-offs

* Random jitter: rejected for now; persisted delays are observable and deterministic
  hash jitter provides spread while making reproduction stable.
* Reset dead jobs in place for replay: retry is retained for that explicit operator
  action; replay instead clones into a new job with ancestry so prior history stays
  immutable.
* Depend on UUID lease tokens alone: they fence writes, but generations make claim
  order and stale-worker evidence queryable.
* Broker, distributed lock service, exactly-once delivery, or generalized scheduler:
  reject; PostgreSQL row locking is already the durable authority.
* Test fault injection in production CLI: rejected. Crash-window pauses are enabled
  only for the integration-test schema and bounded to a few seconds.

## Risks and verification

A replay may intentionally repeat a previously successful logical operation, and
at-least-once recovery can invoke a handler again after side effects but before
acknowledgment. CLI reason fields and event history make operator actions
attributable but do not authorize operators. Integration tests use real PostgreSQL
and worker OS processes for claim-start, mid-execution, pre-ack, pause/expiry,
competing claims, retry exhaustion, duplicate submission, and database outage.
Run the full worker suite plus Go regressions; failures block completion.
