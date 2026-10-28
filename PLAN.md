# Resilient API Gateway — Engineering Execution Plan

> Status: Gateway Phases 0–6 and worker companion Phases 3–7 are implemented and locally tested. Gateway and worker Compose smoke, real-Redis Go integration, and real-PostgreSQL worker integration passed during release review. Worker observability and benchmark results are single-host local evidence only, not capacity or production-readiness claims. Hosted CI and deployment remain unverified.

## 0. Product definition
Build an observable, fault-tolerant Go HTTP reverse proxy demonstrating distributed admission control, per-upstream circuit breaking, connection management, safe dynamic routes, and reproducible performance/chaos testing. Deliver a safe public demo and a recruiter-readable repository. Not intended to replace Envoy, Kong, or a managed production gateway.

### Success criteria
- Go gateway routes to at least two controlled upstream test services.
- Distributed Redis Lua limiter enforces a documented weighted sliding-window counter across two gateway replicas, with correct 429 + Retry-After responses.
- Per-upstream breaker supports Closed/Open/Half-Open, bounded probe concurrency, transition metrics, and testable recovery rules.
- Config reload swaps validated immutable snapshots without dropping in-flight requests.
- Health/readiness, Prometheus metrics, structured logs, graceful shutdown, and Docker Compose work.
- Unit + real Redis integration tests, race test, CI, chaos test, and reproducible direct-vs-proxied performance report exist.
- Public HTTPS endpoint and read-only demo UI show real 200/429/503 behavior; public controls are bounded and isolated.
- No fabricated performance claims. 10,000 RPS and <5 ms incremental latency are stretch hypotheses to test, not gates to fake.

## 1. Decision protocol (use before every feature)
Write a brief entry in `docs/decisions/` when the choice is durable or contentious:
1. Actual problem / observed evidence.
2. Alternatives, including **do nothing** or defer.
3. Necessity: Must / Should / Defer / Reject.
4. Simplest safe solution and why.
5. Risks, security consequences, operational cost.
6. Validation: test or measurement that would change the decision.

Default decisions:
| Feature | Decision | Reason |
|---|---|---|
| Standard-library ReverseProxy | MUST | Correct HTTP semantics without reimplementation |
| Redis Lua distributed limiter | MUST | Cross-instance atomic admission |
| Weighted sliding-window counter | MUST | Efficient approximate limiting; explicitly document approximation |
| Exact rolling-window limiter | DEFER | Requires use case justifying extra storage/CPU |
| Per-upstream circuit breaker FSM | MUST | Core failure-isolation demonstration |
| Shared tuned HTTP transport | MUST | Reusable connections and bounded resources |
| Custom sync.Pool optimization | DEFER | Profile first; existing proxy has BufferPool support |
| Atomic immutable route reload | SHOULD | Useful live-reconfiguration demonstration |
| Authenticated admin control plane | DEFER | More exposure and maintenance than demo needs |
| Automatic retries | DEFER | Can amplify incidents or duplicate operations |
| Service discovery / Kubernetes / gRPC | REJECT for v1 | Inflates scope without validating core learning goals |
| Public read-only dashboard | SHOULD | Strong demonstration of core behavior |
| Multi-instance load distribution | SHOULD | Demonstrates shared rate-limit state |

## 2. Technical design
- Go module: `cmd/gateway`, `cmd/upstream-sim`, `internal/config`, `internal/routing`, `internal/proxy`, `internal/ratelimit`, `internal/breaker`, `internal/observability`, `internal/server`.
- Optional `web/` only after API and metrics work; one small frontend, not a separate backend microservice.
- Route-specific configuration: method/path match, static allowlisted upstream URL, limits, breaker policy, timeout, Redis outage policy.
- HTTP middleware order: request ID / observability -> route match -> trusted identity -> distributed limit -> circuit admission -> reverse proxy. Ensure admission precedes body consumption where possible.
- Transport: connection reuse, finite dial/TLS/header timeouts, max idle and total connections; propagate caller cancellation.
- Redis keys: namespace + stable route ID + hashed/normalized identity + window bucket. Atomic EVAL checks current and previous buckets; precise TTL behavior and allowed/remaining/reset calculation documented. Tests at window edges, multi-instance contention, Redis outages, clock/time shifts.
- Circuit: per-upstream state in each gateway process; lock-protected transitions; single or bounded probes in half-open; configurable consecutive-success threshold; failure classification includes transport errors, timeouts, and chosen 5xx statuses; excludes normal 4xx.
- Reload: parse -> validate -> build immutable route/proxy snapshot -> atomic swap; reject invalid configs and preserve last good state. Keep old in-flight references until requests complete.
- Demo endpoints: `/healthz`, `/readyz`, `/metrics` (private or access-controlled in deployment), `/demo/fast`, `/demo/slow`, `/demo/unstable`, plus read-only aggregate `/demo/status` with sanitized metrics. Fault injection restricted to local upstream simulator or authenticated/staged control, not arbitrary public mutation.

## 3. Implementation phases

### Phase 0 — Repo audit, scope, ADRs
- [x] Check whether this is a new or existing repository; inspect before changing.
- [x] Select latest stable supported Go toolchain, pin dependencies, configure gofmt/go vet/static checks.
- [x] Create threat model, ADR-001 on proxy-vs-custom implementation, ADR-002 on limiter accuracy/failure modes.
- [x] Define smallest useful v1 and explicit requirement-to-test matrix (`docs/requirements.md`).
- [x] Establish baseline tests, gofmt/go vet checks and GitHub Actions workflow (remote execution pending).
Evidence: audit found an unborn Git branch with only three untracked planning files,
no remote, existing code, tests or ADRs. Read AGENTS.md, all of this plan and
CODEX_PROMPTS.md before edits. Go 1.27.3 selected from official release history
and download metadata on 2026-10-27. No external Go dependencies introduced.
See Phase 0 execution record below for actual checks and limitations.

### Phase 1 — Correct, streaming reverse proxy
- [x] Route deterministic requests to two controlled upstreams.
- [x] Use ReverseProxy.Rewrite, sanitize forwarding headers, avoid client-directed targets/open proxy.
- [x] Shared Transport, cancellation, reasonable resource/time limits, graceful shutdown.
- [x] Tests: methods, paths, query, headers, POST body streaming, upstream disconnect, timeout, context cancel, security cases.
Evidence: local unit/race/vet/build gates passed; two native simulator processes
and gateway verified 200/404/405/502 behavior; httptest verifies 504 and stream aborts.
See `docs/verification/phase-1.txt`, `phase-1-smoke.txt` and execution record below.
Development Compose is provided but unexecuted (Docker missing). No Redis/frontend
or future-phase behavior was added. See ADR-004 for explicit HTTP restrictions.

### Phase 2 — Atomic distributed admission control
- [x] Implement weighted sliding-window Redis Lua algorithm with server-side atomic check + increment; TTL policy.
- [x] Return RFC-consistent 429 and helpful RateLimit/Retry-After headers; document approximation.
- [x] Trusted-client identity, bounded key names, Redis timeout and explicit fail-open/fail-closed policies.
- [x] Run multi-instance integration and boundary tests against real Redis.
Observed evidence: real Redis 8.10.3; two independent gateway OS processes admitted
20/80 concurrent requests in a clean shared namespace (unit run [9,11], race run
[12,8]), with 60 HTTP 429. Controlled boundary/TTL tests passed. This demonstrates
the tested approximate-counter invariant, not an exact rolling-window ceiling.
See Phase 2 execution record and raw verification transcripts below.

### Phase 3 — Independent distributed worker processes (requested scope revision)
- [x] Implement independent Python CLI/runtime; no FastAPI server or dependency.
- [x] PostgreSQL schema, atomic `SKIP LOCKED` claims, DB-clock leases and fencing tokens.
- [x] Lease renewal, configurable bounded concurrency, process identity and heartbeats.
- [x] Fixed allowlist workloads: PNG metadata, bounded CSV transform, synthetic report, simulated notification.
- [x] Hard handler timeout, safe retry/abandon behavior, database reconnect and structured attempt events.
- [x] Compose runs PostgreSQL and at least three independent workers.
- [x] Tests prove distribution, single owner during a valid lease, SIGTERM drain and SIGKILL expiry/recovery.
- [x] Capture actual lifecycle logs and test output.
Evidence: ADR-006 captures scope, trade-offs and guarantees. On Windows amd64 with Docker Desktop, `scripts/verify-workers.ps1` passed the actual PostgreSQL-backed integration suite (18/18, no skips), then ran three independent containers and all four allowlisted workload jobs to `succeeded`; worker lifecycle logs and exact commands are in `docs/verification/phase-3-workers.txt`. Local `compileall`, dependency-free unit suite (8 pass, 10 integration skips by design), and `docker compose config --quiet` passed. No exactly-once claim is made.

### Phase 3 worker companion verification record (2026-10-27)

The worker scope is implemented as an independent Python/PostgreSQL companion,
without coupling it to FastAPI or Go gateway request handling. The full isolated
verification script built the pinned image, applied the schema, ran 18 tests
against disposable real PostgreSQL with no skips (including concurrency across
three worker OS processes, valid-lease single ownership, backend disconnect and
reconnect, handler timeout, retry/abandon, SIGTERM drain and SIGKILL recovery),
then started three workers and enqueued four example jobs. Job IDs 1–4 (PNG
metadata, CSV transform, synthetic report, simulated notification) each reached
`succeeded` at attempt 1. Logs show claims and completion by distinct worker UUIDs.
Raw output and JSON lifecycle log excerpt: `docs/verification/phase-3-workers.txt`.

Additional local checks: `python -m compileall -q worker_runtime`,
`python -m unittest discover -s worker_runtime/tests -v` (8 passed, 10 skipped
because this local invocation intentionally had no `WORKER_TEST_DSN`; the
separate Compose run exercised all 18), and `docker compose -f
compose.workers.yaml config --quiet` all passed. The Compose stack was isolated
under unique project names and removed after execution.

Limitations: delivery is at-least-once; handlers with external effects require
idempotency keys or an outbox before use for real side effects. Notifications are
simulated and the runtime intentionally has no arbitrary plugin loading, shell
execution, or network delivery. PostgreSQL loss stops active child work when the
lease becomes uncertain; retries are bounded and final attempts are abandoned.
This demonstrates local container behavior, not production readiness or hosted CI.

### Phase 4 — Distributed fault tolerance and recovery (worker companion request)
- [x] Reclaim expired leases atomically; increment claim generation and require generation + worker + lease token + live lease for renew, effect recording, success and failure.
- [x] Apply capped exponential retry delays with stable hash-derived jitter; honor configurable `max_attempts`; dead-letter exhausted or permanent failures.
- [x] Add explicit operator `retry` and linked `replay` CLI operations with bounded audit reasons; make enqueue idempotency-key-aware.
- [x] Record worker heartbeat staleness and enforce per-job runtime ceiling and worker-level timeout.
- [x] Reconnect after PostgreSQL session/backend loss; terminate active child work while lease certainty is lost.
- [x] Demonstrate idempotent simulated notification side effect with a PostgreSQL ledger and ack-gap duplicate-attempt test.
- [x] Add deterministic real-PostgreSQL/worker-process tests for claim-start, mid-work, pre-ack, expired pause, competing reclaim, stale completion, retry exhaustion, duplicate submit and connection loss.
- [x] Run isolated PostgreSQL stop/start with active jobs; record failure-mode matrix and observed lifecycle logs.

Observed verification (2026-10-27): `scripts/verify-workers.ps1` completed the full
Compose run on Windows amd64 / Docker Desktop with PostgreSQL 16 and Linux Python
3.11 worker containers. All 27 unit and PostgreSQL integration tests passed with
no skips in 55.236s. Three independent workers ran the four fixed demonstration
jobs successfully. The script then stopped/restarted its own PostgreSQL service
while job 5 was active. Logs observed `AdminShutdown:57P01` and reconnect errors;
the job was reclaimed at attempt 2 / generation 2 and succeeded after PostgreSQL
returned healthy. Complete raw command output/logs: `docs/verification/phase-4-recovery.txt`.
Failure trigger/expected behavior/test/result matrix: `docs/worker-failure-modes.md`.

Other gates: `python -m compileall -q worker_runtime` passed; local
`python -m unittest discover -s worker_runtime/tests -v` ran 27 tests with 8
passing and 19 intentionally skipped because this local invocation had no test
DSN (all 27 ran without skips in Compose); `docker compose -f compose.workers.yaml
config --quiet` passed. Existing Go gateway unit/race/vet regression gates are
recorded below: full Go unit and race suites passed with real Redis, gofmt was clean, and `go vet ./...` reported no findings. See the gateway Phase 4 verification record later in this plan for exact results.

Guarantees/limits: at-least-once execution only. The notification ledger's unique
key prevents duplicate rows for this demonstration inside the same PostgreSQL
database; it does not guarantee exactly-once external delivery. Arbitrary external
effects require their own idempotency contract or an outbox. Enqueue idempotency
keys have no expiry; storage retention/cleanup is not yet implemented. An explicit
replay creates a new job and can repeat logical side effects. Jitter is stable
hash-derived for deterministic reproduction, not cryptographically random.
PostgreSQL failover, TLS/auth deployment, hosted CI and external-provider behavior
remain unverified.

### Phase 5 — Production scheduling and control plane (worker companion)
- [x] Separate authenticated FastAPI control plane and cron materializer from worker execution processes.
- [x] Immediate/delayed submissions, static allowlisted handler types, priorities, cancellation, retry/replay, status, attempt history, workers and queue statistics.
- [x] Strict request schemas, bounded payloads, consistent request-ID errors, bearer scopes and database-backed per-principal rate limits.
- [x] Enforce submission idempotency with a database unique key plus request fingerprint; repeated same requests return the same job, changed content conflicts.
- [x] Materialize recurring occurrences transactionally with `FOR UPDATE SKIP LOCKED` and a unique `(schedule_id, occurrence_at)` key.
- [x] Define five-field cron and timezone behavior; nonexistent spring-forward local times are skipped and ambiguous fall-back local times run once at the earlier UTC instant.
- [x] Cursor pagination/filtering, OpenAPI/Swagger, executable request examples and operator documentation.
- [x] Test auth, malformed/oversized input, database outage, API and scheduler races, and live API/scheduler/three-worker lifecycle.

Observed verification (2026-10-27): `scripts/verify-control-plane.ps1` passed all
47 tests against isolated real PostgreSQL in Docker Compose with no skips (69.600s).
The live smoke returned 200 from `/docs` and `/openapi.json`, readiness succeeded,
and three independent workers were visible. An immediate submission repeated with
the same idempotency key reached `succeeded`; the separate scheduler materialized
one forced-due cron occurrence as job 3, which also reached `succeeded`. Raw
commands, output, and process logs are in
`docs/verification/phase-5-control-plane.txt`. `python -m compileall -q
worker_runtime`, `docker compose -f compose.workers.yaml config --quiet`,
`git diff --check`, and the local dependency-free unit run passed; the latter ran
47 tests with 12 passing and 35 PostgreSQL integration tests skipped because no
local `WORKER_TEST_DSN` was configured. The Compose run exercised all 47 with no
skips.

Necessary choices: PostgreSQL is both the job authority and uniqueness boundary;
the API and scheduler are separate processes, while trusted worker handlers remain
the only execution mechanism. The API uses database-backed per-subject fixed-window
rate limits and does not derive identity from forwarding headers. Cron is exactly
five fields. Occurrences are UTC instants generated from the schedule timezone.
The trade-offs and alternatives are in ADR-009.

Limits: the fixed-window limiter can burst at boundaries; the API limits reads to
the latest 500 worker records; no tenant-level data isolation or retention/cleanup
policy exists; scheduler catch-up does not coalesce and can materialize a burst
(bounded to 100 schedules per poll); cancellation of running work is cooperative
and cannot reverse external side effects; execution remains at-least-once. Compose
uses a demonstration token and loopback port; deployment needs managed secrets,
TLS/authenticated ingress, database policy, retention, and operational monitoring.
No exactly-once or production-readiness claim is made.

Phase 7 (worker observability and performance) is completed below. Exact next
worker-companion prompt:
> Read AGENTS.md, PLAN.md, relevant code/tests and ADRs. Implement only Worker
> companion Phase 8: define bounded retention and cleanup for completed jobs,
> attempt events, schedule occurrences and API rate-limit rows while preserving
> idempotency, audit requirements and active/replay references. Write the
> Necessity Decision first; add real-PostgreSQL tests for retention cutoffs,
> concurrent cleanup and protected records; run the complete worker/control-plane
> suite and isolated Compose smoke; update PLAN.md with exact observed evidence,
> remaining risks and the next prompt. Do not implement external delivery,
> arbitrary code execution, frontend or gateway changes.

### Phase 6 — Chronos control-plane dashboard (worker companion)
- [x] Build a typed Next.js/TypeScript control-plane UI for overview, job search/detail/create, schedules, workers, dead letters, observability and queue settings.
- [x] Add real PostgreSQL event-derived metrics/recent events and validated shared submission defaults to the authenticated API.
- [x] Use an endpoint-allowlisted, same-origin bearer proxy; require confirmations/reasons for operator mutations; keep browser preferences separate from API credentials.
- [x] Add live polling, loading/error/empty states, dark/light theme, accessible responsive navigation and charts/tables/forms.
- [x] Verify frontend build/lint, auth and primary journeys, accessibility scan, mobile layout, API integration against PostgreSQL, live Swagger and Compose lifecycle; capture screenshots.

Observed verification (2026-10-27, Windows amd64 / Docker Desktop):
`npm.cmd run lint` and `npm.cmd run build` passed (Next.js 16.4.3, TypeScript
passed). `scripts/verify-dashboard.ps1` passed all 49 worker/control-plane tests
against isolated PostgreSQL with no skips (65.115s), then built and started the
control plane, scheduler, PostgreSQL and three separate workers. `/`, `/docs`,
`/openapi.json`, and the same-origin authenticated API proxy returned successful
responses. Playwright Chromium passed 2/2 browser journeys: rejected/accepted
bearer connection, read-only jobs filtering, job submission/detail/replay,
delayed job cancellation, cron schedule create/disable, workers, dead-letter,
observability, queue policy save, theme toggle, axe checks on overview/form,
and 390px mobile navigation/no horizontal overflow. A real synthetic report
submitted during the run completed as job 1, attempt 1. The Docker build ran
`npm ci` from the lockfile and produced the standalone app. Raw command output,
three-worker lifecycle logs and cleanup are recorded in
`docs/verification/phase-6-dashboard.txt`; actual page captures are in
`docs/verification/screenshots/phase-6/` and linked from
`docs/chronos-dashboard.md`.

The distribution-process integration check exceeded its original 20-second
test deadline in a loaded Compose run; its isolated rerun passed in 18.382s.
Its wait was extended to 45 seconds to absorb container scheduling jitter while
leaving the assertions on lease validity, worker distribution and completion
unchanged. The final full Compose suite passed with that bounded deadline.

Necessity/trade-offs: ADR-012 records the decision. PostgreSQL lifecycle events
are the dashboard's source for historical metrics to avoid invented samples or
a second time-series write path. The current-chart percentiles describe
claim-to-success time for completed claims. Browser polling (15-second minimum)
was chosen over SSE/broker infrastructure. Queue defaults are a bounded shared
policy for new submissions; existing jobs are not rewritten. The API remains
the security boundary; UI confirmations and validation improve operator safety
but do not replace backend authorization.

Remaining risks: no tenant isolation; browser bearer token is in per-tab
sessionStorage and remains exposed to same-origin XSS; use trusted HTTPS ingress
and a secret manager outside local Compose. Event-derived aggregation cost and
unbounded lifecycle/event/idempotency retention have not been load-tested or
bounded. Polling can create repeated read load at larger operator fleet sizes.
Frontend tests cover primary workflows and a baseline axe scan on two screens,
not a full assistive-technology audit. Hosted CI and deployment security remain
unverified. No production-readiness claim is made.

### Phase 7 — Worker observability and performance engineering (worker companion)
- [x] Export Prometheus-compatible worker/API metrics with bounded labels and structured lifecycle logs; expose authenticated operational summaries through the existing UI.
- [x] Record claim batch/latency, queue wait, execution distributions, worker activity, queue states, retries, dead letters, lease recovery and DB pool usage without job IDs as metric labels.
- [x] Provision an optional local Prometheus/Grafana dashboard; keep scrape ports private to the Compose network and UI summaries authenticated.
- [x] Capture EXPLAIN ANALYZE/BUFFERS plus PostgreSQL table/index statistics for the claim query.
- [x] Run before/after matrix at 1,2,4,8,16 worker processes and queue depths 20,50,100; save raw JSON, CSV, plot script and SVG report.
- [x] Apply only the queue candidate split justified by the observed plan; rerun the plan and same benchmark matrix.

Observed evidence (2026-10-28, Windows 11 amd64 / Python 3.14.7 / Docker Desktop
12 CPUs and 7,606 MiB; PostgreSQL 16.15 limited to 2 CPU/512 MiB; workers each
1 CPU/256 MiB): full real-PostgreSQL Compose suite passed **53/53** in 84.667s
after the release-review fixes. All 15 before and all 15 after benchmark combinations
at depths 20/50/100 and 1/2/4/8/16 workers ran with zero job errors. At 100 jobs,
throughput before→after was 1.581→1.605, 2.600→2.742, 3.587→4.574, 4.662→4.956,
and 4.690→7.020 jobs/s respectively. Claim p95 at 16 workers was 144.331→54.982ms.
This is one local matrix and not a capacity claim. Raw data is in
`benchmarks/worker/raw/{before,after}/`; the generated plot is
`benchmarks/worker/throughput.svg`; report and exact resource/config details are
in `benchmarks/worker/BENCHMARKS.md`. On a 10,000-ready-job local fixture, the
claim candidate-selector plan changed from 4.019ms/183 shared buffer hits to
0.105ms/5 hits (the EXPLAIN capture does not include the subsequent row update
and lifecycle insert);
raw `queue-claim-explain-{before,after}.json`. The local Prometheus API and all
three worker scrape targets were healthy; Grafana 12.1.0 `/api/health` returned
database ok and version 12.1.0, and authenticated Grafana search found the
provisioned `Chronos Workers` dashboard. Initial all-profile startup encountered the
pre-existing host port 3000 dashboard binding, then Prometheus/Grafana were
started and checked independently; all project containers/volumes were removed.
`python -m compileall`, Compose config validation, frontend lint and production
build passed. `go test ./...` was attempted but Go is unavailable on this host;
no Go source was changed. Raw SQL plans are single warm local observations.

Necessary choices and rejected complexity: ADR-015 records the decision to use
bounded Prometheus dimensions, persisted `started` events, a bounded control API
connection pool, and an isolated local benchmark. The UI derives operational
summaries from PostgreSQL and requires API auth. No IDs or worker UUIDs become
metric labels. A two-branch claim query is worthwhile based on the measured scan
and sort; it may briefly lock one unselected candidate. No telemetry database,
SSE broker, per-job metrics, renewal event stream, broad queue rewrite, fake
percentiles, or benchmark framework was added.

Remaining risks: benchmarks are single-run, synthetic, single-host experiments;
there are no confidence intervals, realistic CPU-heavy handlers, or load tests
for event-derived API summaries. Worker metrics reset on process restart and
Prometheus/Grafana are demo-only optional services; production auth, TLS, storage,
retention, alerting, and capacity remain deployment work. API pool configuration
is bounded but saturation queues callers. Claim split locking behavior should be
revisited under larger mixed queued/expired workloads. Go regression suite could
not run because the Go executable is absent. No exactly-once or production
performance guarantee is made.

### Go gateway per-upstream circuit breaker — current Phase 3 request (previously numbered Phase 4)
- [x] Implement explicit FSM + deterministic clock injectable for tests.
- [x] Track failures over defined window/minimum volume or simpler documented consecutive threshold.
- [x] Bound half-open probes and require configured successful probes before closure.
- [x] Verify proxy/response error classification and instant open-state 503 with observability.
- [x] Race tests and chaos scripts for slow, failing, then recovering upstream.
Evidence: deterministic state/transition snapshots, 100 simultaneous FSM admissions,
concurrent HTTP probes and local 10-request failure/timeout/recovery chaos script.
See the Phase 3 circuit execution record and `docs/verification/phase-3-circuit.txt`.

### Current gateway Phase 4 — Safe reload and resilience (previously numbered Phase 5)
- [x] Validated immutable routing snapshot and atomic swap.
- [x] Preserve in-flight requests; reject malformed or unsafe targets and keep last-good config.
- [x] Add startup/readiness handling for Redis and shutdown draining.
- [x] Test concurrent request load during reload and intentional Redis loss.
Observed evidence: 16 long-running streams completed on their original target
across 21 swaps; invalid candidates retained the last good version. Unit/race
tests exercise concurrent writers/readers, native file polling, real Redis
connection loss and recovery, both outage modes and readiness policies. See
`docs/verification/phase-4-reload.txt` and the execution record below.

### Phase 6 — Observability and performance (delivered as requested Phase 5)
- [x] Prometheus counters/histograms/gauges with bounded metric cardinality.
- [x] Structured logs, request ID, status distinction (429/503/502/504), latency breakdown.
- [x] Profiles/benchmarks before introducing any custom pooling.
- [x] `bench/` script measures direct upstream vs gateway and bounded 429/circuit-open workloads on loopback.
- [x] Archive raw benchmark outputs and a report including versions/hardware/payload/RPS/errors/p50/p95/p99/test duration and allocation profile.
Evidence: ADR-010, `docs/benchmarks.md`, `docs/verification/phase-5-benchmark.json`, and heap/allocation artifacts. Single local run only; no production performance claim.

### Phase 7 — Security and operational hardening
- [x] Analyze SSRF, trusted proxies, hop-by-hop headers, query/path handling, body size/streaming, auth boundaries, config injection, Redis outages.
- [ ] Define and apply a global inbound concurrency/body budget; preserve streaming and bound queued work.
- [x] Verify existing upstream connection bounds, finite error responses, redacted logs and fixed-cardinality metrics.
- [x] Run dependency integrity/vulnerability checks; document supported protocols and known limitations. Negative regression coverage is recorded below; global ingress limits remain open.
Evidence: `docs/threat-model.md`, ADR-011, `docs/verification/phase-6-dependencies.txt` and 64 passing test cases in the full raw race transcript `docs/verification/phase-6-security.txt`.

### Phase 8 — Recruiter-friendly demo UI
- [x] Implement small dashboard showing actual process counters, outcomes, limiter results, circuit state and latency estimates. (No p50/p99 guessed; existing backend exports bucketed latency estimates only.)
- [x] Demo buttons invoke safe, server-enforced bounded scenarios: one normal request, capped burst-to-429 where configured, isolated breaker failure-to-open and recovery.
- [x] Label synthetic circuit results and process-lifetime samples; browser cannot choose a URL, route, method, policy or failure duration.
- [x] Add keyboard-visible focus, responsive styles, no-sample and backend error states.
Evidence: ADR-013; `docs/verification/phase-7-dashboard.txt`; Go demo tests and Next build/type checks. Browser visual verification was attempted but the installed CUA browser surface was unavailable on Windows; see limits in the verification record. Action cooldowns are process-local; public multi-replica deployment still needs shared or ingress-level action throttling.

### Phase 9 — Deploy + portfolio packaging
- [x] Multi-stage Docker image, non-root runtime, Docker Compose local Redis + two upstreams.
- [x] Choose current hosting plan and prepare deployable Render Blueprint with private gateway/upstreams, compatible Redis and HTTPS dashboard ingress. Hosted creation and plan billing still require operator credentials/approval.
- [x] Keep `/metrics` bearer-protected and private in hosted topology; validate secret lengths/distinctness, probes and bounded shutdown.
- [x] Configure deployment CI for frontend, container builds, Compose health, and bounded smoke requests. Remote CI has not run from this workspace.
- [ ] Deploy and run low-volume public smoke tests against actual accessible HTTPS URLs.
- [x] README: architecture, local start, curl examples, benchmarks, failure-mode demo, decisions, tests and limitations.
- [ ] Add a live URL after deployment and actual HTTPS smoke checks.
- [ ] Draft CV bullets using **only** measured benchmark/test results.
Evidence: verified public HTTPS endpoints, working demonstration, deployment runbook, CI green.

Phase 9 implementation record (2026-10-28): added multi-stage static Go images,
UID/GID 10001 runtime, Redis + two fixed simulator services + dashboard Compose,
loopback-only published ports, health probes, bounded stop grace, and
Render-compatible env wiring. `/metrics` is bearer-protected with constant-time
comparison; non-loopback listeners require a 32..256-byte metrics secret, and
metrics/demo tokens must differ. Added `render.yaml`, hosting comparison and
exact credential/deploy checklist in `docs/deployment.md`, plus CI steps for
frontend, images, Compose, and authenticated metrics smoke. No credentials or
provider URL were available, so no deployment/public smoke was attempted. The
Render Blueprint is a template, not a verified deployment; prices must be
rechecked before provisioning.

Verification on Windows amd64, Go 1.27.3, Node 24.21.0/npm 11.19.0: Go unit
suite, race suite, vet, build, formatting, frontend lint, TypeScript no-emit,
Next production build, and YAML parsing passed. `REDIS_TEST_URL` was unset, so
real-Redis integration checks were not exercised in this turn. Docker CLI
27.5.1 built the gateway, upstream, and dashboard images; inspect showed UID/GID
10001 and sizes 32,527,978 bytes (gateway), 28,611,563 bytes (upstream), and
304,957,150 bytes (dashboard), with `/readyz`, `/healthz`, and `/api/healthz`
healthchecks. The Compose plugin was absent, so Compose config/up and local HTTP
smoke are unverified. GitHub Actions has not been run remotely. No public or
local smoke URL is recorded as verified. See
`docs/verification/phase-8-deployment.txt` for exact outputs and limitations.

Remaining risks: free Render Key Value loses quota state on restart; Render
private health checks are TCP rather than HTTP; provider secret prompts,
Blueprint service references, TLS URL, and actual billing need live operator
verification. Full hosted deploy and measured public smoke remain open.

Exact next-phase prompt:
> Deploy the Render Blueprint only after credentials and spend approval are
> available. Verify the actual generated HTTPS dashboard URL and private service
> topology, then run the bounded public dashboard smoke cases from
> `docs/deployment.md`. Record UTC time, exact URLs, responses, observed state,
> plan limits and actual cost. Do not expose the gateway or metrics publicly.

### Release-review update (2026-10-28)

This update supersedes the earlier Phase 9 checkpoint note that Redis-backed Go
tests and local Compose smoke had not run. A dedicated Redis container on
`127.0.0.1:36379` was used for gateway unit/race and black-box checks; the full
gateway Compose stack was built and started with `REDIS_PORT=36380`. Gateway,
Redis, both simulators, and dashboard probes became healthy. Local smoke observed
200 for `/healthz`, `/readyz`, `/one`, `/two`, dashboard `/` and `/api/healthz`,
401 for unauthenticated `/metrics`, and 200 with the configured bearer token.
The worker Compose stack also started PostgreSQL 16.15, three worker processes,
scheduler, API and dashboard; API health and Swagger returned 200, API and BFF
returned 401 without bearer auth and 200 with it, and a submitted fixed
`synthetic_report` reached `succeeded`. Only local URLs were checked.

Final worker test-profile run rebuilt its test-only dependency image and passed
**53/53 tests in 84.667s** against disposable PostgreSQL. Retry metric review
found and removed a duplicate counter increment; `test_one_scheduled_retry_increments_counter_once` now verifies the exact count. `httpx2` is imported by
Starlette's `TestClient`, not application code, so it moved to
`requirements-test.txt`; the production worker image built with UID/GID 10001
and an import check confirmed `httpx2` is absent.

Go 1.27.3 commands with `REDIS_TEST_URL=redis://127.0.0.1:36379/0`: `go test
-count=1 ./...`, `go test -race -count=1 ./...`, and `go vet ./...` passed;
`go mod tidy -diff` exited 0 with no module changes. Frontend lint and TypeScript
checks passed, and the Next production build completed during both Compose image
builds. `scripts/circuit-chaos.py` passed its 10-request fail/open/timeout/
recovery flow. Gateway benchmark rerun and corrected nearest-rank values are in
`docs/benchmarks.md` and `docs/verification/phase-5-benchmark.json`. Worker
matrix/plan artifacts are documented in `benchmarks/worker/BENCHMARKS.md`.

| Claim | Check | Actual observed result | Caveat |
|---|---|---|---|
| Gateway request/security/fault regressions pass | Go unit/race suites with real Redis; `release-review-gateway-tests.txt`; circuit chaos script | Forwarding trust, malformed paths, timeout/truncation, Redis policies, bounded metrics, retries and breaker transitions pass; circuit reproduction passed 10/10 steps | Local fixtures and one Redis; not an internet-facing penetration test |
| Worker API/runtime behavior passes | Worker test Compose profile | 53/53 tests passed in 84.667s; includes API auth, real PostgreSQL, process failure/recovery and retry metric regression | Disposable PostgreSQL and local Docker resources |
| Container stacks are runnable | Gateway Compose config/build/health/HTTP smoke; worker full-profile Compose smoke | Both stacks started; fixed gateway routes and authenticated metrics behaved as configured; worker dashboard/API served and one job succeeded | Gateway Redis host port 6379 was occupied, so tested run used 36380. All checked URLs were loopback |
| Local benchmark artifacts reflect measurements | Gateway direct/proxy/fail-fast raw JSON; worker before/after JSON and EXPLAIN plans | Gateway: 100 direct and 100 proxied 200s; 40 expected 429s and 40 expected 503s. Worker matrix: all 30 cases had zero job errors; candidate plan 4.019ms/183 hits before, 0.105ms/5 hits after | Single-host synthetic measurements, one run per case, no production inference; selector EXPLAIN excludes later update/event insert |
| Hosted release is available | Render/CI credentials and public URL check | Not run; no credentials or deployed service URL | No hosted URL or remote CI result is claimed |

`git diff --check` passed. Working-tree changes unrelated to the retry metric
fix and test-only dependency split were preserved. Historical Phase 8 transcripts
remain records of the earlier checkpoint and are superseded for local Redis/
Compose status by this update. Exact gateway negative-test output is in
`docs/verification/release-review-gateway-tests.txt`.

## 4. Intended repository map
```
.
├── AGENTS.md
├── PLAN.md
├── README.md
├── cmd/
│   ├── gateway/
│   └── upstream-sim/
├── internal/
│   ├── config/
│   ├── routing/
│   ├── proxy/
│   ├── ratelimit/
│   ├── breaker/
│   ├── observability/
│   └── server/
├── scripts/
├── bench/
├── deploy/
├── docs/
│   ├── decisions/
│   ├── threat-model.md
│   ├── benchmarks.md
│   └── operations.md
├── web/                  # Only after core backend is validated
├── .github/workflows/
├── Dockerfile
├── compose.yaml
└── go.mod
```
Keep structure flexible. Create folders when used; don't create meaningless empty package scaffolding.

## 5. Quality gates
Run as applicable:
- `gofmt -w` on touched Go files
- `go test ./...`
- `go test -race ./...`
- `go vet ./...`
- `docker compose up --build` and black-box checks
- Redis integration tests with real Redis, not only a mock
- Load-test scripts must have safe limits; do not run abusive tests against public free-tier URLs
Every phase records outputs, assumptions, any failed/skipped checks, and next task.

## 6. Performance measurement contract
Before discussing 10k RPS or <5ms overhead:
1. Record environment (CPU, RAM, OS, Go version, Redis location, service placement).
2. Measure the identical endpoint directly and via gateway, matching payload, connection reuse, TLS, concurrency, run length, and warm-up.
3. Report achieved request rate, error rate, p50/p95/p99, and whether Redis admission was enabled.
4. Separate gateway-added delta from Redis/network RTT and upstream time; do not subtract unrelated benchmark percentiles and claim a precise per-request overhead.
5. Profile allocations/CPU first, optimize bottlenecks second, rerun tests and document regressions.
6. If a target is missed, report it factually and investigate; no unexplained tunings or selective reports.

## 7. Deployment selection
Selected path (deployment template only): Render public Docker web service for the
Next dashboard, three same-region private Docker services (gateway and two fixed
simulators), and private Render Key Value/Valkey 8. Render handles HTTPS at the
public web ingress. Current listed floor is about $21/month with free web/Key
Value tiers; paid always-on web and persisted 256 MB Key Value raise the estimate
to about $38/month. Free web cold starts and ephemeral free Key Value make that
the demo tier only. Fly Machines cost less per compute unit but need separate
HTTPS/Redis setup, so Render is the simpler first path. Pricing and constraints
are dated and sourced in `docs/deployment.md`. No service has been deployed;
Compose and hosted smoke remain unverified.

## 8. Interview evidence checklist
- [ ] Explain Lua atomicity and the weighted sliding-window approximation.
- [ ] Explain local vs distributed breaker coordination.
- [ ] Show deterministic half-open concurrency test and race test results.
- [ ] Explain transport pooling, MaxIdleConnsPerHost vs MaxConnsPerHost.
- [ ] Explain safe forwarding header and trusted proxy handling.
- [ ] Show incident-style chaos experiment and recovery traces.
- [ ] Explain Redis failure policies and impact on availability/abuse protection.
- [ ] Show one measured optimization (or defensible decision not to optimize).
- [ ] Show a real live URL, dashboard, runbook and reproducible bench scripts.

## 9. Work log
Append per phase:
| Date | Phase | Decision / change | Verification | Outstanding issue |
|---|---|---|---|---|
| 2026-10-27 | 0 | Audit, v1 scope/matrix, threat model, three ADRs, Go CLI scaffold, CI and local setup | See execution record below | Remote CI unavailable; future behavior unimplemented |
| 2026-10-27 | 1 | Two static routes, shared transport, streaming/header trust, lifecycle/health, simulator and development Compose | Unit/race/vet/build/help and native process smoke passed; transcripts retained | Docker/hosted CI unavailable; no public-hardening or performance claim |
| 2026-10-27 | 2 | Atomic approximate Redis admission, peer identity/cap, deadline, per-route outage policy, quota headers and metrics hooks | Unit/race/vet/build; real Redis boundaries and two OS processes; container pause/recovery; Compose config | Full Compose execution, Linux signals and hosted CI remain unverified |
| 2026-10-27 | Current gateway Phase 3 | Per-origin local circuit FSM, bounded probes, outcome classification, CLI policy, snapshots and bounded chaos reproduction | Full Go unit/race suite with real Redis, vet/build/format and 10-request chaos PASS; phase-3-circuit.txt | Local breaker only; no fleet coordination, live metrics endpoint or full Compose run |
| 2026-10-27 | Current gateway Phase 4 | Immutable versioned route/proxy snapshots, atomic reload, local file polling, explicit outage/readiness policies | Full Go unit/race suite with real Redis, native config-process restoration, format/vet/build; phase-4-reload.txt | PING is reachability/auth only; startup numeric policies remain fixed; full gateway Compose/hosted CI not run |
| 2026-10-27 | Worker companion Phase 3 | Independent Python workers, fenced PostgreSQL leases, allowlisted handlers, recovery and isolated three-worker Compose | 18/18 PostgreSQL-backed tests; four live workloads succeeded; phase-3-workers.txt | At-least-once; side-effect idempotency needed before real delivery; hosted CI unverified |
| 2026-10-27 | Worker companion Phase 4 | Generational recovery, idempotency, jittered retry/dead-letter, operator retry/replay, outage recovery and failure-mode matrix | 27/27 real PostgreSQL tests; PostgreSQL stop/start recovered active job at attempt 2; phase-4-recovery.txt | At-least-once; external effect contract and retention remain out of scope |
| 2026-10-27 | Worker companion Phase 5 | FastAPI control plane, scheduling, idempotency fingerprints, recurring occurrence uniqueness, auth/scopes, rate limits, pagination and OpenAPI | 47/47 real PostgreSQL tests; live Swagger/API/scheduler and three-worker smoke; phase-5-control-plane.txt | At-least-once; fixed-window boundary bursts, no tenant isolation or retention, and deployment ingress/secrets remain open |
| 2026-10-27 | Gateway Phase 5 observability/performance | Fixed-cardinality Prometheus scrape, request IDs/completion logs, timing semantics, loopback workload harness and heap profile | `go test ./...`, `go test -race ./...`, `go vet ./...`, build, benchmark and profile passed; raw JSON and pprof artifacts under `docs/verification/phase-5-*` | Single host short run; RAM unavailable; no TLS/cross-host/repeated-run confidence; no pooling change justified |
| 2026-10-27 | Requested Phase 6 security review (plan Phase 7) | Prioritized threat model; negative metrics-cardinality test; POST no-retry regression; reviewed SSRF/path/header/timeout/Redis/readiness/truncation/shutdown controls | `REDIS_TEST_URL=redis://127.0.0.1:6379/0 go test -race -count=1 -v ./...` passed all packages including real Redis; vet/build/format/py_compile/diff checks and govulncheck passed; transcripts under `docs/verification/phase-6-*` | No demonstrated exploitable defect in reviewed cases; public listener requires private metrics and deployment authentication; global inbound budget remains unresolved |
| 2026-10-28 | Requested Phase 8 deployment work (repository Phase 9) | Multi-stage non-root images, full-stack Compose config, protected metrics, Render Blueprint, hosting comparison, secret/probe/CI wiring | Go unit/race/vet/build, frontend lint/type/build, YAML parse, three image builds/inspections and runtime UID checks passed; phase-8-deployment.txt | Compose plugin/CI run and hosted deploy/smoke unavailable; credentials absent; no public URL |

### Phase 0 execution record (2026-10-27)

Architectural rationale: standard-library-only scaffold establishes a runnable
baseline without implementing Phase 1. ADR-001 selects ReverseProxy.Rewrite;
ADR-002 records approximate Redis admission and explicit outage policy;
ADR-003 records toolchain, foundation and rejected complexity. No existing user
files were removed; AGENTS.md and CODEX_PROMPTS.md remain unchanged.

Validation: all Phase 0 local gates passed on Windows amd64, Go 1.27.3,
GCC 16.2.0, CGO_ENABLED=1 and GOTOOLCHAIN=local. The official Windows archive
was SHA256-verified against downloaded release metadata. Raw command output,
archive hash and exit codes: `docs/verification/phase-0.txt`.

| Actual command (repository-local Go tools) | Observed result |
|---|---|
| `go version` | go1.27.3 windows/amd64 |
| `gofmt -w cmd/gateway/main.go cmd/gateway/main_test.go` | Exit 0 |
| `gofmt -l cmd` | No output, exit 0 |
| `go test -v ./...` | Both scaffold tests PASS; package ok (1.394s) |
| `go test -race ./...` | Package ok (1.888s), exit 0 |
| `go vet ./...` | No findings, exit 0 |
| `go build -o bin/ ./cmd/gateway` | Exit 0 |
| `./bin/gateway.exe` and `go run ./cmd/gateway` | Both print scaffold status and exit 0 |

Setup issues encountered: initial `go version` and `docker --version` failed
because commands were absent from PATH. Sandbox network request failed; approved
network download succeeded. An initial metadata selection command failed before
download and was corrected. Slow PowerShell archive extraction was interrupted;
Python reverified the same archive hash and completed extraction. These are setup
issues, not passing checks. Toolchain/cache artifacts remain ignored in `.tools/`.

Changes: `.gitignore`, `go.mod`, `cmd/gateway/main.go`, its test,
`.github/workflows/ci.yml`, `README.md`, `docs/requirements.md`,
`docs/threat-model.md`, ADR-001/002/003, verification transcript and this plan.
Final source and new-file diffs reviewed separately because the repository is
entirely untracked; `git diff --check` alone cannot inspect untracked files.

Outstanding issues at Phase 0 completion: Go is available through the ignored repository-local toolchain,
not installed globally; Docker remains unavailable. No Git remote or commits exist, so hosted CI cannot
be verified here. Compose/Redis integration are not applicable to a CLI without
external dependencies and are deferred until those dependencies are introduced.
No proxy, server, Redis client, circuit, reload, metrics, benchmark, UI or deployment
exists. The example.com module path awaits a canonical remote. Action tags are
mutable (see ADR-003). Phase 0 does not establish production readiness.

Next smallest task at Phase 0 completion: implement only Phase 1 static routing/proxy behavior and its
black-box tests; do not add Redis or frontend.

Exact next prompt at Phase 0 completion:
> Read AGENTS.md and PLAN.md completely, then inspect all code, tests and ADRs.
> Implement only Phase 1 of PLAN.md: two controlled static upstream routes using
> httputil.ReverseProxy.Rewrite, explicit URL/path/query/Host and forwarding trust
> rules, a shared bounded Transport, streaming/cancellation, graceful shutdown and
> health/readiness endpoints. Write a Necessity Decision before substantive changes.
> Add black-box tests for HTTP semantics, streaming, failure/timeouts, cancellation
> and malicious forwarding headers. Do not implement Redis, breakers, reload,
> metrics or frontend yet. Run formatting, unit/race tests, vet and relevant local
> smoke checks. Update the requirement-to-test matrix and PLAN.md with actual
> evidence, skipped checks, remaining risks and the exact next prompt.

### Phase 1 execution record (2026-10-27)

Read AGENTS.md and all of this plan, existing source/tests, requirements, threat
model and ADRs before changes. Git still has an unborn branch and untracked files;
there is no remote. Captured original file hashes, inspected source/new-file diffs
and compared changed-file hashes to avoid overwriting unrelated work. AGENTS.md,
CODEX_PROMPTS.md, go.mod, .gitignore and the Phase 0 transcript remain unchanged.

Implementation and rationale: ADR-004 records choices before implementation.
`internal/gateway` owns a private two-entry route table and one HTTP/1.1 Transport.
Operator origin URLs are validated and privately parsed; `/one` and `/two` match
segment boundaries and strip their prefix. Rewrite preserves suffix escaping and
valid raw query bytes, sets upstream Host and rebuilds forwarding metadata from the
immediate peer only. Reject ambiguous paths/queries, arbitrary request targets,
unsupported methods/upgrades and declared request trailers before body reads.
Use native proxy streaming/full duplex, finite deadlines and generic 502/504 errors;
after headers, a stream failure aborts rather than inventing a replacement status.
No custom pools, whole-body buffers, application retries or routing framework.

`internal/server` owns liveness/readiness, HTTP limits and shutdown. Signal context
initiates a 10s drain using a separate context; it does not immediately cancel active
requests. After the drain deadline, cancel their base context and close connections.
Readiness means serving/not-draining, not healthy upstreams. `cmd/upstream-sim` runs
twice as the controlled local dependency. Development Compose uses these same Go
commands, building then execing binaries; no production Dockerfile/deployment yet.
The CLI intentionally replaces Phase 0 print-and-exit; CI help smoke avoids hanging.

Verification environment: Windows amd64, official repository-local Go 1.27.3,
GCC 16.2.0, CGO_ENABLED=1, GOTOOLCHAIN=local. Raw outputs:
`docs/verification/phase-1.txt` and `docs/verification/phase-1-smoke.txt`.

| Actual check | Observed result |
|---|---|
| `gofmt -w cmd internal`; `gofmt -l cmd internal` | Exit 0; no unformatted files |
| `go test -v -timeout 60s ./...` | All tests passed; latest gateway package 2.617s; CLI/server cached from passing unchanged runs |
| `go test -race -timeout 60s ./...` | All packages passed; latest gateway package 3.576s; no race reports |
| `go vet ./...` | Exit 0; no findings |
| `go build -o bin/ ./cmd/...` | Both executables built, exit 0 |
| `bin/gateway.exe -h`; `bin/upstream-sim.exe -h` | Documented flags, exit 0 |
| Two native simulators + gateway process smoke | Health/readiness 200; exact route/path/query metadata; 404/405; stopped upstream one produces 502 while upstream two remains 200; all children reaped |
| httptest failure/lifecycle checks | 502 disconnect, 504 header/request timeout, client cancellation reaches upstream; truncated/expired streams abort; shared connection budget/reuse; TLS verification and response-header budget; graceful drain and deadline cancellation |
| `docker compose config` | Could not run: Docker command not installed; no Compose success claimed |
| Git diff/status and explicit review of untracked files | No files removed; unrelated originals unchanged; whitespace checks passed |

Initial full unit and race runs also passed. During review the early-response test
fixture was given explicit Content-Length: 0 so its response close does not wait
for the deliberately absent upload; final unit/race gates above include that fix.
No load test or benchmark ran. The simulator has no separate unit package tests;
native process smoke exercises its actual executable. No real Redis integration
applies yet. Physical OS-signal handling was not smoke-tested on this Windows host:
smoke cleanup forcibly terminates local processes; Go lifecycle tests cancel the
same context path and verify draining/cancellation. Hosted Linux CI cannot be
confirmed without a remote. Docker/Compose and a Linux SIGTERM process smoke remain
explicit operational follow-ups, not blockers to the verified native proxy core.

Files changed: `cmd/gateway/main.go` and its tests, `.github/workflows/ci.yml`,
`README.md`, `docs/requirements.md`, `docs/threat-model.md`, this plan.
Files added: `internal/gateway/gateway.go`, forwarding/streaming/failure tests;
`internal/server/server.go` and its tests; `cmd/upstream-sim/main.go`, `compose.yaml`,
ADR-004 and the two Phase 1 verification transcripts. No external Go dependencies.

Remaining risks/limits: 30s request context and 35s server write deadline exclude
indefinite streams; defaults are conservative choices, not measured optima. There
is no body-size or global client concurrency limit, and bounded upstream sockets do
not bound queued requests. No ingress headers are trusted; X-Forwarded-Host is
untrusted metadata. DNS/egress isolation and deployment trust remain unverified.
Strict path/query rules reject some valid ambiguous inputs; upstreams must not
reinterpret/decode paths repeatedly. HTTP/2, upgrades and request trailers are not
supported. Slow-client/dial/TLS-stall adversarial coverage remains Phase 6.
No Redis admission, breaker, reload, metrics, UI, public deployment or benchmark
exists. The placeholder module path and mutable CI action tags remain as recorded
in ADR-003. No production-readiness or throughput guarantee follows from these tests.

Next smallest task: Phase 2 Redis-backed admission only, retaining this HTTP contract.

Exact next prompt:
> Read AGENTS.md and PLAN.md completely, then inspect code, tests and ADRs. Implement
> only Phase 2 of PLAN.md: an atomic Redis Lua weighted two-bucket sliding-window
> counter using Redis server time, documented approximation/admission invariants,
> bucket TTL/reset and Retry-After semantics. Choose identity from the immediate
> peer unless a separately justified trusted-ingress policy is implemented; bound
> key names/cardinality and Redis latency. Add explicit per-route fail-open/fail-closed
> policies with secure defaults and visible fallback hooks. Run real Redis boundary,
> outage and multi-instance contention tests, preserving streaming and admission
> before body reads. Add Redis to local Compose. Do not add breakers, reload, frontend
> or benchmarks. Write Necessity Decisions, run formatting/unit/race/vet and relevant
> integration checks, and update the matrix/PLAN.md with actual evidence and skipped
> checks. Do not claim exact rolling-window enforcement or global consistency during
> fail-open/failover. End with remaining risks and the exact next prompt.

### Phase 2 execution record (2026-10-27)

Decision/rationale: ADR-005 compares token bucket, fixed window, weighted counter,
exact log and doing nothing. Choose the required two-bucket approximation: constant
per-peer history and integer-scaled check/increment in one Lua invocation. Redis
TIME supplies milliseconds; accepted state expires at the end of the following
bucket. Denials do not increment or extend state TTL. Retry-After solves the next
unit's eligibility without intervening traffic; Reset describes full history decay.
RateLimit-* fields are documented compatibility headers, not an implementation
claim for evolving structured IETF fields. Lua atomic execution is isolation, not
rollback or failover durability. No exact rolling-window guarantee is claimed;
the boundary test explicitly admits three requests within 1s with limit 2 when
the previous bucket's traffic clusters late.

Implementation: shared bounded go-redis pool, no command retries, total context
budget (default 100ms), server-time Lua, policy mismatch rejection, SHA-256 socket
peer identity, fixed safe key components and atomic active-identity cap. Both
routes default fail-closed; explicit fail-open has no invented quota headers.
Admission precedes application body reads, preserving the existing streaming
contract. Fixed per-route counters/timing and event hooks expose fallback; CLI
logs redacted failures and shutdown summaries. No metrics server, circuit,
reload, frontend or benchmark was added. A small final-header flush wrapper fixes
an observed pre-existing empty-response/upload race (ADR-005 review note).

Observed checks on Windows amd64, Go 1.27.3, GCC 16.2.0, real Redis 8.10.3 Linux
container on Docker Desktop, loopback mapped port, noeviction / 64MiB / ephemeral:

| Check | Actual result / artifact |
|---|---|
| `go test -v -count=1 -timeout 90s ./...` with REDIS_TEST_URL | All packages PASS; no integration skips; `docs/verification/phase-2-tests.txt` |
| `go test -race -v -count=1 -timeout 90s ./...` with REDIS_TEST_URL | All packages PASS, no races; `docs/verification/phase-2-race.txt` |
| Final added approximation example: unit/race tests for ratelimit package | PASS; `phase-2-boundary-final.txt`; package race PASS (4.127s) |
| Two OS-process contention | Unit admissions [9,11], race [12,8]; both aggregate 20 admitted / 60 limited from 80 requests; spoofed X-Forwarded-For does not split identity |
| Real Redis boundaries | Rollover, fractional remaining, retry rounding, unchanged denial TTL, idle gap, clock rollback error, Redis TIME expiry bracket, capacity/policy mismatch and expiry recovery PASS |
| Stalled TCP Redis dependency | 30ms configured deadline returned in 30.3671ms in race run; test allows 250ms scheduler tolerance; not a benchmark |
| Real Redis pause/resume, native gateway | 50ms budget: closed route 503 in 52.92ms, open route 200 in 55.49ms, both omit quota; resumed route 200; `phase-2-outage.txt` |
| Formatting, vet, build, diff whitespace | PASS; `phase-2-checks.txt` |
| `go mod tidy` | PASS after approved module downloads; go.mod/go.sum pinned |
| `docker compose config` | PASS after using installed Docker Desktop plugin outside sandbox |

Redis image digest/setup: `docs/verification/phase-2-redis-setup.txt`. Initial
Docker check failed because Desktop was stopped; Desktop was started and the
isolated container succeeded. Earlier image pull timed out; no success inferred
from that attempt. Container pause smoke uses native binaries, two loopback ports,
`-redis-failure-two open -redis-timeout 50ms`, Docker pause/unpause on only the
test container. Child processes are forcibly reaped; this is not a SIGTERM test.

Files changed/added: `internal/ratelimit/{limiter.go,window.lua,identity_test.go,redis_test.go}`,
`internal/gateway/{gateway.go,admission.go,admission_test.go}`,
`cmd/gateway/{main.go,redis_integration_test.go}`, `go.mod`, `go.sum`, Compose,
CI Redis service, README, requirements, threat model, ADR-005, verification
transcripts and this plan. Reviewed source against the Phase 1 snapshot because
the entire repository remains untracked; no originals removed, unrelated files
and user instructions preserved.

Remaining risks/skipped checks: full Compose stack execution and hosted CI not
run; native executable integration substitutes for stack execution, not for CI.
Linux physical signals, Redis TLS/auth/cluster/failover and production ingress
remain unverified. No public deployment, performance or exact-window claim.
Redis TIME is wall time; forward jumps/eviction/history loss can relax quota,
rollback across buckets errors through route policy. A timed-out script may have
consumed quota. Fail-open sacrifices enforcement. NAT/ingress shares peer quota;
the finite identity cap can deny new clients (503). Per-route synchronous hooks
must not block. Dependency-aware readiness remains Phase 4. No global inbound
concurrency/body budget or metrics endpoint is introduced in Phase 2.

Next smallest task: Phase 3 local per-upstream circuit breaker only.

Exact next-phase prompt:
> Read AGENTS.md, PLAN.md, relevant code/tests and ADRs. Implement Phase 3 only:
> a per-upstream local Closed/Open/Half-Open circuit breaker with monotonic-safe
> timing, fake-clock tests, bounded concurrent probes and deterministic recovery.
> Define failure classification explicitly; exclude upstream 4xx and client
> cancellation. Preserve Redis admission, header trust, streaming and cancellation.
> Add transition/state metrics hooks and concurrency/race/black-box tests. Write
> the Necessity Decision before substantive changes. Run formatting, unit/race,
> vet and real Redis regressions. Update PLAN.md with observed evidence, limitations
> and exact next prompt. Do not implement reload, frontend, deployment or benchmarks.

### Current Phase 3 circuit execution record (2026-10-27)

Scope: latest user request explicitly specifies the original gateway Phase 3
circuit breaker. The worker companion and ADR-006 are preserved; no Python worker,
PostgreSQL, worker Compose or worker CI behavior was modified. The breaker section
above is completed regardless of the earlier plan renumbering.

Decision: ADR-007 chooses a minimal mutex-protected consecutive-failure FSM over
ratio windows, dependencies or no isolation. One instance per canonical configured
origin; shared between routes to that origin, independent across processes.
Default threshold 5, cooldown 5s, one simultaneous probe, two recovery successes;
CLI flags configure instance policy. Completion generations ignore stale results;
permits finish once. Outstanding old probes remain in the concurrency budget.
Injectable concurrency-safe clock; production uses time.Now's monotonic component.

Integration: route/input checks -> Redis -> circuit -> shared-transport ReverseProxy.
Open/capacity rejection is uncached 503 + rounded Retry-After with no upstream I/O
or application body reads. Redis still precedes the circuit and consumes quota.
5xx, transport/response-read errors and gateway deadlines are faults; 4xx, client
cancellation, failed uploads and abandoned successful streams are neutral.
Successful streams must reach EOF. Empty responses complete at headers; other
bodies hold probe slots until EOF/Close, including slow error bodies. No new
retry, buffering, public fault-control endpoint or background goroutine.
Fixed-label CircuitStats snapshots expose states, transitions, outcomes and probe
occupancy; CLI logs snapshots at shutdown. No metrics server was added.

Observed validation: `docs/verification/phase-3-circuit.txt` contains exact commands,
raw output and exit codes on Windows amd64, Go 1.27.3, CGO=1, local Redis 8.10.3
Linux container with noeviction and 64MiB memory limit, loopback mapped port.

| Actual command/check | Result |
|---|---|
| gofmt touched Go files; `gofmt -l cmd internal` | PASS, no unformatted files |
| `go test -v -count=1 -timeout 90s ./...` with REDIS_TEST_URL | All packages PASS; gateway 4.317s; no Redis integration skips |
| `go test -race -v -count=1 -timeout 90s ./...` with REDIS_TEST_URL | All packages PASS; gateway 4.281s; no race reports |
| Deterministic FSM | Closed/Open/Half-Open boundaries, reset/neutral threshold, failed probe reopen, recovery count, stale/duplicate completion and separate instances PASS |
| Simultaneous admissions | Exactly 3 probes from 100 concurrent attempts; recovered after 5 successful completions |
| HTTP concurrency and classification | 2 blocked probes, 18 extra requests rejected 503; streamed 5xx retains permit; 4xx neutral, upstream failure, cancellation, deadline, upload failure, truncated and abandoned bodies PASS |
| HTTP Open rejection | 503/no-store/Retry-After with untouched upload and unchanged upstream count; independent origin remains usable; shared origin routes share state |
| Redis regression | Unit process admissions [9,11]; race [10,10]; aggregate 20 admitted/60 HTTP 429 from 80 across independent gateway OS processes; Lua boundaries/TTL/cap/outage tests PASS |
| `go vet ./...`, `go build -o bin/ ./cmd/...` | PASS, no findings |
| `python scripts/circuit-chaos.py` | PASS, 10 sequential loopback requests, 6 upstream requests; failure/open/slow-probe/reopen/recovery reproduced |
| `git diff --check` and explicit source/snapshot review | PASS; existing repository is entirely untracked, so Git diff alone does not review new files |

Chaos observation: two 500s open the breaker; three Open requests return 503 in
2.01/1.76/1.74ms with unchanged upstream hits. A Half-Open slow probe times out as
504 after 5003.53ms, then Open returns 503 in 2.33ms. Two healthy probes followed
by a healthy Closed request return 200. These are local incident reproduction
timings, not benchmark percentiles or a latency guarantee. The script starts only
its own loopback fixture and gateway, enforces literal-loopback Redis, finite
startup/HTTP deadlines and ten requests, and reaps its child process. No external
traffic target or public fault API exists.

An initial race run exposed a test assertion racing empty-response handler
cleanup (not a data race); tests now wait for server-side permit completion before
asserting recovery. Final full gates above include this synchronization. Review
also caught early release at 4xx/5xx headers; retaining slots until body completion
and the streamed-failure test verify the corrected concurrency contract.

Files added: `internal/breaker/breaker.go`, `breaker_test.go`,
`internal/gateway/circuit.go`, `circuit_test.go`, `circuit_transport_test.go`,
`scripts/circuit-chaos.py`, `docs/circuit-breaker.md`, ADR-007 and raw transcript.
Files modified: `internal/gateway/gateway.go`, `cmd/gateway/main.go`, README,
requirements matrix and this plan. No Go module/dependency or worker file changed.

Limitations/skipped checks: breaker is per-process, not fleet coordinated. Long
streams keep probe slots until existing deadlines/EOF. No full Compose stack or
hosted CI run, Linux SIGTERM smoke, deployment, load benchmark, reload or public
metrics endpoint was attempted in this scope. Native chaos cleanup forcibly
terminates the child and does not prove graceful OS-signal handling. No production
or percentile claim follows. Previously admitted Closed traffic may continue after
Open; completion-order thresholds are not a rolling failure ratio. Neutral client
4xx does not heal the circuit. Worker status remains independent.

Next smallest gateway task: immutable validated route reload and dependency-aware
readiness (original gateway Phase 4, currently numbered Phase 5 in this plan).

Exact next-phase prompt:
> Read AGENTS.md, PLAN.md, relevant code/tests and ADRs. Implement only the next
> gateway phase: safe reload and resilience (original Phase 4, currently Phase 5).
> Build immutable validated route snapshots and atomically swap them; keep in-flight
> requests on their selected route/proxy. Reject invalid reloads and retain the last
> good snapshot. Use local operator-controlled reload with no public mutation API.
> Define breaker/limiter policy identity and state retention across reloads before
> coding. Add Redis-aware readiness and concurrent reload/cancellation/outage tests.
> Preserve streaming, trusted identity, distributed quota and bounded local circuits.
> Write the Necessity Decision, run formatting, unit/race, vet and real Redis checks,
> and update PLAN.md with observed evidence, limits and the exact next prompt.
> Do not add frontend, deployment, retries, benchmarks or worker features.

### Current gateway Phase 4 execution record (2026-10-27)

Scope: implement the user's snapshot reload/readiness request (this scope was
previously numbered Phase 5 after adding the independent worker companion).
Worker code, verification, Compose and phase records are preserved. No public
configuration API, metrics server, retries, benchmark or frontend was introduced.

Decision/rationale: ADR-008 records alternatives, necessity, lifecycle/state
retention, verification and limitations before implementation. Replace fixed
route fields with a private immutable versioned snapshot; validate both targets
and modes, construct proxies privately, then publish via atomic.Pointer under
serialized reload writers. Each request selects once before Redis and retains
the same route/outage policy/circuit/proxy through completion. Reuse one shared
transport/Redis pool. Preserve breakers for canonical origins retained from the
previous snapshot, including origins moving between routes; new origins start
Closed. Removing and later re-adding an origin resets its local breaker. Stable
route IDs preserve Redis quota; numeric quotas/window/cap/namespace, transport and
circuit policy remain immutable startup settings to avoid mixed-policy requests.

Reloadable fields: two origin targets, two explicit closed/open Redis failure
modes, and required/policy readiness. Local Go Reload/ReloadJSON and optional
`-config` polling (1s default, 100ms..1m) share the same builder. Files are bounded
regular files (64KiB); reject unknown/duplicate fields, incorrect array lengths,
trailing/malformed JSON, unsafe origins and invalid modes. Invalid/missing files
retain the last good memory snapshot/version; valid restoration applies without
restart. Hash deduplication avoids repeatedly processing the same bytes; read
failures deduplicate until a read succeeds. Logs never include file data/URLs.
Fixed ReloadStats exposes version/applied/rejected without cardinality growth.

Readiness: health remains local. Default required readiness checks Redis using
bounded PING with its shared pool/timeout, without quota or identity mutations.
Explicit policy mode skips Redis only when both routes fail open. Any fail-closed
route requires it. Startup can listen during Redis loss while readiness is 503;
data routes still independently apply outage policy rather than a global readiness
gate. Restore makes the next probe 200. Drain/shutdown wins even over an already
running successful probe. PING proves reachability/auth, not Lua permissions,
capacity, matching distributed policy or upstream health; this limit is documented.

Observed checks: raw commands/output/exit codes are in
`docs/verification/phase-4-reload.txt`. Environment: Windows amd64, Go 1.27.3,
CGO=1, isolated real Redis 8.10.3 Linux Docker container, noeviction, 64MiB,
loopback port 63426. No shared Redis server state was flushed or paused.

| Actual check | Observed result |
|---|---|
| gofmt touched Go files; `gofmt -l cmd internal` | PASS, no unformatted files |
| `go test -v -count=1 -timeout 90s ./...` with REDIS_TEST_URL | All packages PASS; gateway 3.501s; no Redis integration skips |
| `go test -race -v -count=1 -timeout 90s ./...` with REDIS_TEST_URL | All packages PASS; gateway 5.093s; no race reports |
| Concurrent long streams | All 16 completed with original `old:finished` body across 21 snapshot replacements; new requests used replacement target, invalid second target did not partially replace first, restoration worked |
| Requests blocked in Redis during reload | Closed->open request retained old closed policy; open->closed request retained old target and open policy, while subsequent request used closed policy |
| Concurrent reload/stat readers and writers | Readers saw complete route pairs; 40 concurrent writers produced 40 replacements/version 41; no-op did not advance version |
| Circuit/quota retention | Moving retained origin preserved Open circuit; invalid mode did not reset it; real Redis quota remained across target spelling change and readiness probes, reaching expected final 429 |
| Invalid file/target handling | Unsafe URLs, credentials, paths, fragments, queries, invalid ports/modes, duplicate/unknown fields, wrong array counts, trailing JSON, missing/oversized/non-regular files rejected; no secret diagnostics |
| Native gateway process + config file | Actual run/flag/listener/watch wiring rejected unsafe update, served old target, applied valid restoration, kept readiness 200, returned 404 for nonexistent admin/reload endpoint |
| Redis connection loss/restoration | Test-specific TCP gate disconnected/denied only the limiter's connections to real Redis; closed data 503, open data 200 with no quota claims; readiness required 503, policy all-open 200, mixed policy 503; restore returned 200 without restart |
| Startup/readiness/drain | Startup 503, bounded caller health deadline, missing probe unhealthy, health 200 through outage; probe overlapping drain still returned 503 |
| Existing regressions | HTTP streaming/cancellation/header trust, circuit FSM and bounded probes, real Redis Lua/time/TTL/cap and two OS-process quota tests PASS; unit process admissions [10,10], race [9,11], both 20/80 admitted with 60 HTTP 429 |
| `go vet ./...`; `go build -o bin/ ./cmd/...`; diff whitespace | PASS, no findings |

An initial compile check exposed a missing httputil import after moving proxy
construction into the snapshot builder; corrected before all passing suites above.
No result is inferred from that failed compile. File-watcher fixture writes use
atomic replacement so tests do not assert exact rejection counts for partial
operator writes. Partial files remain safely rejectable in actual operation.

Files added: `internal/gateway/{snapshot.go,config.go,readiness.go,config_test.go,reload_test.go,redis_reload_test.go}`,
`cmd/gateway/{reload.go,reload_test.go,config_process_test.go}`,
`internal/server/readiness_test.go`, `config/routes.example.json`, ADR-008,
`docs/reload-readiness.md` and verification transcript.
Files modified: gateway/admission/circuit integration, limiter Health, server
readiness, gateway CLI, README, requirements, threat model, circuit documentation
and this plan. No dependency/module or worker files changed. Source diffs were
reviewed against pre-change snapshots because this repository is entirely
untracked; Git diff alone cannot inspect those files.

Remaining limits/skipped checks: full gateway Compose execution, hosted CI,
Linux physical-signal process smoke, Redis TLS/auth/failover and production
ingress remain unverified here. Native process tests forcibly terminate children;
existing lifecycle tests verify drain/cancellation. The watcher does bounded
reads of operator-owned regular files; hostile filesystem replacement and blocked
network filesystems are outside its local-file contract. Old snapshots can remain
until requests finish under existing deadlines; no new global concurrency/memory
bound is claimed. Last-good state is in memory only; restart with invalid config
fails. Readiness is a current-snapshot observation, not a guarantee for later
requests. Trusted operators can reset a removed/re-added circuit. No benchmark
or production-readiness claim follows from these tests.

### Gateway Phase 5 execution record (2026-10-27)

Implemented fixed-schema Prometheus text exposition on `/metrics`, request IDs,
structured request completion events, admission/upstream/request histograms,
Redis error/admission outcomes and process-local circuit state/outcomes. Timing
definitions and private-listener requirement are in ADR-010 and `docs/benchmarks.md`.
The harness captures direct/proxied success and bounded 429/open-circuit loads;
raw JSON, allocation output, heap profile and pprof summary are under
`docs/verification/phase-5-*`. The Go benchmark observed 257,164 ns/op,
47,977 B/op and 187 allocs/op in an in-process httptest stack. The largest
allocation-space site was ReverseProxy copyBuffer in that test stack; this is not
enough evidence to add custom pooling.

Verification on Windows 11 amd64, Go 1.27.3: `go test ./...`, `go test -race
./...`, `go vet ./...`, `go build -o bin/ ./cmd/...`, `gofmt -l cmd internal bench`
(no output) and `python -m py_compile bench/run.py` passed. A race run first
exposed unsynchronized bytes.Buffer test sinks and copying atomics in scrape code;
both were corrected and the final race suite passed. Local benchmark fixture run:
100 direct and 100 proxied 1KiB HTTP 200 responses, concurrency 2, zero unexpected
statuses; 40/40 expected 429 and 40/40 circuit-open 503. This single short loopback
run is noisy and Python-fixture-bound. RAM was unavailable in host inventory; no
TLS, cross-host, GC pause or repeated-run confidence measurement was performed.
No performance target or production guarantee is claimed. `/metrics` remains
unauthenticated and must be bound privately. Next risks: protect metrics in a
public deployment and complete Phase 7 hardening.

Next smallest gateway task: resolve the remaining Phase 7 global ingress budget
once a concrete workload policy is available.

Exact next-phase prompt:
> Continue gateway Phase 7 with a concrete workload-based decision for any global
> inbound body/concurrency budgets. Preserve streaming and current static-route contracts.
> Do not expose unauthenticated metrics publicly; keep deployment access controls
> explicit. Add only evidence-backed controls, then run focused tests, race, vet,
> build and formatting. Update threat model, PLAN.md, exact evidence and next prompt.

### Worker CI dependency correction (2026-10-28)

The failed `Python worker checks` run followed the release-review move of
`httpx2` from runtime to test-only dependencies. Its workflow still installed
`requirements.txt` before importing Starlette `TestClient`; `requirements-test.txt`
extends runtime dependencies with `httpx2`. Both worker CI workflows now install
the test requirements. This is necessary for test import correctness; leaving
runtime dependencies unchanged avoids shipping a test-only package. `python -m
compileall -q worker_runtime` and `git diff --check` pass locally. The DB-backed
Compose test could not be rerun in this session because Docker CLI could not
connect to the Docker daemon; a GitHub Actions rerun is needed to verify the CI
fix on its runner.

### Phase 8 — Security hardening, CI/CD, and operational readiness (2026-10-28)

- [x] Add pinned GitHub Actions gates for Go formatting/unit/race/vet/build, real Redis tests, Python/PostgreSQL integration tests and repeatable migration validation, frontend lint/type/build, source secrets, dependency and container vulnerability checks.
- [x] Verify secret/local artifact ignore rules; add API readiness healthcheck and retain bounded API/worker timeouts, SIGTERM drain and private metrics controls.
- [x] Add sanitized API and database-pool error logging; test authentication/authorization, rate limits, request bounds, database outage readiness and cancellation/ack contention.
- [x] Audit queue transaction lock ordering for deadlocks and document rollback, backup restore, worker recovery and public demo abuse procedures.
- [ ] Complete external hosted CI review, an isolated backup/restore drill, and public-ingress budget before any public deployment.

Observed local evidence on Windows amd64 / Docker Desktop, real PostgreSQL Compose, Go 1.27.3 (prior recorded verification), Node 24, Python 3.11:

| Check | Observed result |
|---|---|
| Full worker/PostgreSQL API/process suite after latest logging change | `docker compose -f compose.workers.yaml --profile test run --build --rm tests`: 55 tests passed in 98.426s, no skips |
| Focused API suite after log filter | 17 tests passed in 9.571s; actual DB outage emitted only `database_pool_diagnostic` without peer address or driver detail |
| API outage readiness | `/healthz` stays live while `/readyz` returns 503; raw psycopg connection diagnostics scrubbed |
| Worker lifecycle and race cases | Full suite includes SIGTERM drain, SIGKILL lease recovery, timeout, expired-lease fencing, stale completion and cross-process ownership cases; all passed |
| Migration repeatability | PostgreSQL migration applied twice; schema version 6 / 6 verified in the preceding full-stack run |
| Git ignore rules | `python scripts/verify_gitignore.py`: 18 required entries verified |
| Go CI syntax and Go regressions | actionlint and Go unit/race/vet/build passed in the earlier local execution recorded during this task; a later shell had no `go` executable on PATH, so no additional rerun is claimed |
| Python / frontend dependencies | `pip-audit` and `npm audit --audit-level=high` had passed in the preceding checks; no new dependency added |
| Go/Python dependency vulnerabilities | govulncheck and pip-audit reported no known vulnerabilities in the preceding local run |
| Container scans | Gateway, upstream and dashboard had no HIGH/CRITICAL findings. Worker had no CRITICAL or fixable HIGH findings; Trivy reported 17 unique HIGH advisory IDs without published fixes across Debian 13.7 packages. These remain visible risks, not a clean scan |
| CI workflows | YAML/actionlint checked locally earlier; GitHub-hosted workflow execution not available from this workspace |
| Compose services | PostgreSQL-backed test stack passed; production control-profile image/healthcheck was not rebuilt and smoked after final log-filter edit |

Deadlock/race review: inspected transaction paths and found consistent job-row-before-event ordering; scheduler locks schedule rows in deterministic order and then inserts occurrences/jobs; no reverse job/schedule lock path was found. `SKIP LOCKED`, transaction advisory migration lock, statement/lock timeouts and the cancellation/ack integration race are present. This is a static wait-for-graph review plus tested scenarios, not a formal proof for future SQL, triggers or untested schedules.

Security/operations changes include pinned action revisions and scanner images, dependency/container gates, the `scripts/check_trivy_report.py` gate that fails CRITICAL and fixable HIGH while reporting unfixed HIGHs, required `.gitignore` assertions, API Compose readiness healthcheck, database-pool log scrubbing, refreshed Node/Python image digests with build-only package removal, and `docs/operations-runbook.md` procedures. No hosted release or public deployment was performed.

Remaining risks: 17 unfixed HIGH OS advisory IDs in the worker image require reachability review and re-scan when Debian publishes fixes. No isolated PostgreSQL restore/RPO/RTO drill was performed. The dashboard demo remains intentionally unauthenticated for a small set of fixed simulator actions; current cooldown/rate budgets are process-local and are not sufficient for horizontally scaled public ingress. Static API bearer keys need deployment-grade secret storage/rotation. Hosted GitHub Actions, external ingress, backup restoration and production rollback are unverified. No public deployment is approved by this evidence.

Exact next prompt:
> Complete Phase 8 residual verification: run the pinned GitHub Actions workflows on a hosted PR; review the worker image's 17 unfixed HIGH advisory IDs for runtime reachability and refresh the base when fixes are available; perform an isolated PostgreSQL backup/restore drill with measured RPO/RTO; and define a trusted ingress-wide demo request/concurrency budget before public horizontal scaling. Do not deploy while critical findings or correctness failures remain. Update PLAN.md with observed evidence and the next smallest task.

### Phase 9 — Production deployment on Render (2026-10-28)

- [x] Preserve the existing root gateway Blueprint and define Chronos separately in `render.chronos.yaml`; validate it against Render's published schema.
- [x] Add production Docker configuration for the API, persistent worker/scheduler processes, and Next.js dashboard; use Render's injected API `PORT`, private service host-port discovery, bounded database pools, operator-protected `/metrics`, and health endpoints.
- [x] Document topology, GitHub checks-pass deployment setup, migration behavior, health and worker verification, shutdown/recovery steps, cost estimate, limitations and smoke checklist in `docs/render-production.md` and ADR-017.
- [ ] Confirm the Render workspace and inspect its existing services/database; obtain explicit approval for the estimated $58.20/month incremental Hobby-workspace resources before provisioning.
- [ ] Provision/sync the Blueprint, verify CI-gated GitHub deployments, deployed HTTPS health, Postgres, actual task/retry/schedule lifecycle, controlled worker termination/recovery, and real dashboard API data. No cloud deployment was performed during this preparation turn.

Observed local evidence (Windows amd64 / Docker Desktop; PostgreSQL Compose integration environment):

| Check | Observed result |
|---|---|
| Render Blueprint | `python scripts/validate_render_blueprint.py render.chronos.yaml`: valid against Render's published schema |
| Python compile | `python -m compileall -q worker_runtime scripts`: passed |
| PostgreSQL process/API/scheduler suite | `docker compose -f compose.workers.yaml --profile test run --build --rm tests`: 56 tests passed in 84.509s, including SIGKILL recovery, expired lease replacement/fencing, independent process ownership and two schedulers |
| Dashboard static checks | `npm run lint`: passed; `npx tsc --noEmit`: passed |
| Dashboard production build | `npm run build`: passed; Next.js generated `/api/healthz` and all app routes |
| Dashboard production container | `docker build -f Dockerfile.web -t chronos-dashboard:phase9 .`: image built successfully. After setting `HOSTNAME=0.0.0.0`, in-container Docker health check reported `healthy` and `/api/healthz` returned HTTP 200 `{"status":"ok"}`. |
| Source diff hygiene | `git diff --check`: passed |
| Render cloud state | Not inspected because workspace confirmation is pending; no services/database deployed and no public URL exists |

Cost estimate from Render public pricing reviewed 2026-10-28: five $7 `0.5c-512mb` service instances ($35), one $19 `0.5c-1g` Postgres instance and approximately $4.20/month for 14 billable GB of the configured 15 GB disk = **$58.20/month incremental**. This excludes taxes, overages, any Pro workspace subscription, and the existing gateway stack. It is an estimate, not a spending cap. See ADR-017 and `docs/render-production.md`.

Remaining deployment risks/blockers: workspace identity and existing Render resources are unverified; no explicit cost approval; no deployed API/dashboard/worker/scheduler, GitHub-to-Render deployment run, HTTPS check, or cloud lifecycle evidence. The existing Phase 8 risks remain, including no isolated restore drill, public-demo ingress budget, unresolved unfixed HIGH advisories and no hosted CI run. Do not report Chronos as operational until cloud smoke evidence is captured.

Exact next prompt:
> Confirm the Render workspace “My Workspace” and approve provisioning the Chronos topology at the estimated incremental cost of $58.20/month on Hobby (plus any usage overages/taxes; no Pro workspace upgrade). Inspect existing resources first, then sync the validated `render.chronos.yaml` Blueprint from the GitHub repository after CI checks pass. Deploy and verify API/dashboard HTTPS health, PostgreSQL readiness, task success/retry/scheduling, and a controlled worker termination/recovery. Record real service IDs, deployment evidence, public URLs, and actual billing-plan costs in PLAN.md; do not claim unobserved behavior.
