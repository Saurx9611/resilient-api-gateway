# ADR-009: Worker scheduling and REST control plane

Status: Accepted, 2026-10-27.

## Necessity decision

Problem/evidence: the Phase 3–4 worker has a safe PostgreSQL queue and CLI, but
no authenticated HTTP contract, delayed/priority admission, recurring schedule
materialization, or operator-facing attempt/status APIs. Doing nothing leaves
the requested scheduling and service boundary unavailable. A separate FastAPI
process and scheduler process are MUST; worker handlers remain independent.

## Choices

* PostgreSQL remains authoritative. Extend the existing schema with job priority,
  cancellation state, submitting idempotency fingerprint, recurring schedule
  definitions, and occurrence rows uniquely keyed by schedule and UTC instant.
  A scheduler transaction locks due schedules with `SKIP LOCKED`, inserts the
  occurrence/job/event, and advances the schedule together. The unique index is
  the final duplicate-materialization guard across processes.
* The API authenticates high-entropy bearer tokens configured at startup, maps
  each token to read/submit/operator scopes, validates strict bounded schemas,
  reads at most 64 KiB of request JSON, and applies a database-backed fixed
  60-second request budget per authenticated principal. Invalid-token traffic
  should additionally be bounded at the trusted ingress; forwarded address
  headers are not identity.
* Idempotency keys are scoped to a principal by hashing `(principal,key)` into
  the existing unique job key. The database stores a SHA-256 fingerprint of the
  canonical effective request; equal key/equal fingerprint returns the existing
  job, while equal key/different fingerprint is HTTP 409.
* Cron uses five-field croniter expressions in an IANA zone. Enumerate naive
  local wall-clock matches, resolve them via `zoneinfo`, skip nonexistent local
  times in the spring-forward gap, and emit one occurrence at the earlier UTC
  instant when a local time is ambiguous during fall-back. Occurrence identity
  is the resolved UTC timestamp. Late scheduler operation catches up occurrences
  in order; each bounded tick processes at most 100 rows.
* Cancellation changes queued jobs immediately; running cancellation is a
  request observed at the next lease renewal, where the worker stops its child
  and records a fenced canceled transition. It cannot undo an external effect
  already performed by a handler.
* Cursor pagination uses the monotonically increasing job/event IDs, with stable
  descending job order and bounded page sizes. State filters and date filters
  are SQL-bound parameters. No arbitrary SQL, handler name, code, shell command,
  or URL is accepted.

## Alternatives and rejected complexity

* Run FastAPI in the worker process: REJECT; couples execution lifecycle and
  scaling to request-serving availability.
* Let every API process calculate/materialize cron directly: REJECT; request
  bursts and concurrent replicas complicate occurrence ownership.
* Add Redis, a broker, a distributed lock service, or a full workflow engine:
  DEFER; PostgreSQL already owns the durable queue and can enforce uniqueness.
* Use process-local rate limiting as the authoritative API budget: REJECT;
  it resets on restart and multiplies across API replicas. PostgreSQL fixed
  windows are approximate burst controls, not a general abuse firewall.
* Use croniter's timezone-aware DST normalization as implicit policy: REJECT;
  enumerate wall times and make gap/fold choices explicit and test them.
* Cancel a running subprocess directly from the API: REJECT; only its lease
  owner can stop it safely. Cancellation therefore takes effect at renewal.
* Add per-tenant data partitioning, schedule misfire coalescing, recurrence
  editing/version history, or arbitrary cron dialect extensions: DEFER until
  product requirements establish those contracts.

## Trade-offs and verification

This is at-least-once queue execution. A scheduler crash rolls back the
materialization transaction; repeated scheduler processes are guarded by row
locks and a unique occurrence index. A crash after job execution can still
repeat external effects; handlers need idempotency contracts. API fixed-window
rate limits can burst at boundaries. The API trusts configured bearer keys and
should run behind TLS/trusted ingress; no built-in user directory, token
issuance, revocation store, or anonymous abuse budget is provided.

Verify strict schemas and body bounds, unauthorized/forbidden calls, rate-limit
and error structure, same/different fingerprints under concurrent requests,
cursor/filter behavior, delayed priority, queued/running cancellation, retry and
replay audit history, worker/queue views, Swagger/OpenAPI, cron timezone and DST
rules, and two concurrent real-PostgreSQL scheduler processes materializing each
occurrence once. Run the full worker integration and Go regressions afterward.

References: croniter timezone-aware expression behavior
(https://github.com/pallets-eco/croniter#about-dst); FastAPI OpenAPI generation
(https://fastapi.tiangolo.com/).
