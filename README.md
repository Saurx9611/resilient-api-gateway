# Resilient API Gateway

The gateway is a Go HTTP/1.1 reverse proxy with two immutable routes, explicit header
trust, streaming, bounded upstream connections/timeouts, health/readiness and
graceful shutdown. Uses `httputil.ReverseProxy.Rewrite` and one shared Transport.
Atomic Redis admission is implemented and its real-Redis evidence is recorded in
`PLAN.md`. The independent Python/PostgreSQL worker runtime described below is a
companion subsystem requested separately. The per-upstream Go circuit breaker
is implemented under the current Phase 3 request (the plan previously moved it
to Phase 4). Current Phase 4 adds atomic route snapshots, local file reload and
Redis-aware readiness. Phase 5 adds bounded Prometheus metrics, request IDs,
structured completion logs and reproducible local benchmark/profile artifacts.
No live public demo or production performance results exist.

## Local setup

Install **Go 1.27.3** from https://go.dev/dl/ (verify its published SHA256).
Use a C compiler supported by Go for race tests; Linux CI provides GCC.
Set `GOTOOLCHAIN=local` to prevent implicit toolchain downloads. Pinned go-redis dependencies are recorded in go.mod and go.sum. The module path is `example.com` until a
canonical repository URL is chosen.

From the repository root, in PowerShell or a POSIX shell:

```text
go version
gofmt -l cmd internal
go vet ./...
go test ./...
go test -race ./...
go build -o bin/ ./cmd/...
go run ./cmd/gateway -h
```

`gofmt -l cmd internal` must print nothing. The command now serves until interrupted;
`-h` prints flags and exits. Windows binary names end in `.exe`.
CI repeats these checks on Linux; a workflow file does not establish a green
remote CI run until pushed and executed.

## Run locally

After building, start each command in a separate terminal (append `.exe` on Windows):

```text
./bin/upstream-sim -listen 127.0.0.1:9001 -name one
./bin/upstream-sim -listen 127.0.0.1:9002 -name two
./bin/gateway
```

```text
curl -i http://127.0.0.1:8080/healthz
curl -i http://127.0.0.1:8080/readyz
curl -i "http://127.0.0.1:8080/one/hello?q=a%20b"
curl -i http://127.0.0.1:8080/two/hello
curl -i -X POST --data-binary hello http://127.0.0.1:8080/one/upload
```

In Windows PowerShell use `curl.exe` to avoid the legacy alias. The simulator
returns JSON metadata (upstream, method, decoded suffix path, raw query), not a
request-body echo. Stop upstream one, then `/one/hello` returns **502** while
`/two/hello` still returns **200**. Readiness describes local serving state and Redis
dependency policy, not upstream availability. Header stalls produce **504**; deterministic
tests cover this without exposing a fault-injection API.

Ctrl+C (or SIGTERM on supported hosts) starts a 10s drain, then forcibly closes
unfinished connections. New work is rejected while draining. A request already
selected its proxy continues using that immutable route. Restart with
`-upstream-one http://host:port -upstream-two http://host:port` to change operator
allowlist entries. Alternatively use local `-config` file reload described below;
there is no public configuration endpoint.

Run the production-shaped local gateway demo with Docker Compose. Set two
different tokens first (the dashboard token is also configured in the gateway):

```powershell
$env:GATEWAY_DEMO_TOKEN = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:METRICS_BEARER_TOKEN = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
# Set this if host port 6379 is occupied.
$env:REDIS_PORT = '36380'
docker compose up --build --wait
```

The stack builds non-root multi-stage Go images, uses ephemeral local Redis and
two fixed upstream simulators, and publishes only the dashboard and gateway on
loopback (`3000` and `8080`). Redis is also loopback-only. `/metrics` requires
the metrics bearer token; `/healthz` is liveness and `/readyz` checks configured
Redis readiness. Stop with:

```text
docker compose down
```

See [deployment guide](docs/deployment.md) for Render topology, provider limits,
secrets, costs, smoke checklist, and current hosted verification status. No
public URL is claimed until a real deployment is available and checked. The
worker companion retains its separate Compose file and lifecycle:

```text
docker compose -p chronos -f compose.workers.yaml --profile control --profile workers up --build -d
docker compose -p chronos -f compose.workers.yaml --profile control --profile workers down
```
This worker stack uses ephemeral PostgreSQL and local-only credentials. The dashboard and API publish on loopback at ports 3000 and 8000; PostgreSQL stays on the Compose network. The optional monitoring profile publishes Prometheus and Grafana on loopback.

Worker metrics and benchmark methodology, raw before/after measurements, query
plans, and the optional Grafana profile are documented in
[`docs/worker-control-plane.md`](docs/worker-control-plane.md) and
[`benchmarks/worker/BENCHMARKS.md`](benchmarks/worker/BENCHMARKS.md). Local
worker-phase performance numbers are single-host observations, not capacity
claims; `/metrics` must remain private in a deployed topology.

## HTTP contract and limits

- `/one` and `/one/...` select upstream one; `/two` selects upstream two. Strip
  the prefix; a bare prefix becomes `/`. `/one-other` does not match. Upstream
  Host is the configured authority. A client Host/query/header cannot select a target.
- Preserve suffix escaping and valid raw query bytes, duplicate query values,
  methods, bodies, upstream status, end-to-end headers and response trailers.
  Reject dot segments, repeated separators, backslashes, encoded separators,
  encoded route-prefix spellings and queries that Go cannot parse (400).
- GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS are supported. Other methods receive 405
  when they match an origin-form route; malformed request-targets receive 400.
  Upgrades and declared request trailers receive 501; unknown routes receive 404.
- Strip hop-by-hop headers using ReverseProxy. Discard incoming `Forwarded`, all
  `X-Forwarded-*` and `X-Real-IP`; rebuild X-Forwarded-For from the **immediate peer**,
  Proto from actual TLS, Host from the incoming Host. The latter is client-controlled
  metadata, never an authorization signal. No ingress headers are trusted yet.
- Never follow upstream redirects: return their status/Location to the client.
  TLS certificates are verified; environment HTTP proxies are ignored. Operator
  origin configuration is privileged. DNS/egress isolation needs deployment controls.
- Defaults: request context **30s**; dial/TLS **3s**; response headers **5s**;
  expect-continue **1s**; upstream idle **90s**; **64** total/**16** idle connections
  per origin, **128** total idle; response headers **32KiB**. Server header/read/
  write/idle timeouts are **5s/30s/35s/60s**, with a **32KiB** configured header limit.
  See [ADR-004](docs/decisions/ADR-004-phase-one-http-contract.md) for trade-offs.
- Request/response bodies stream without whole-body buffering. Accepted requests
  enable HTTP/1 full duplex and response writes flush immediately. Streams have a
  finite lifetime; a blocked client write may last until the server write deadline.
  Admission precedes application body reads, but net/http/OS/ingress may already
  buffer bytes. Rejected requests close the connection to avoid implicit draining.
- Pre-header transport errors return generic 502; timeouts return 504. Once headers
  are committed, a copy error/deadline aborts the stream; it cannot change 200 to 504.
  Upstream 4xx/5xx statuses pass through. No application retries; standard Transport
  may replay eligible requests after certain stale-connection failures.
- `/healthz` and `/readyz` accept GET/HEAD and are uncached. Readiness requires
  serving/not-draining and the configured Redis policy; upstream failure does
  not itself make readiness fail. See [readiness contract](docs/reload-readiness.md).

`/metrics` exposes fixed route/outcome/state dimensions. A non-loopback listener
requires `METRICS_BEARER_TOKEN`; hosted metrics remain on the private gateway
service. Request IDs are returned in `X-Request-ID` and
logged with route/status/handler duration only. Timing definitions, benchmark
method, captured local results and profile limitations are in
[`docs/benchmarks.md`](docs/benchmarks.md). These local measurements do not
establish a production rate or latency guarantee.

There is no body-size or global inbound concurrency limit yet; upstream connection
limits do not bound all client sockets or queued goroutines. No HTTP/2, upgrades,
indefinite streams or request-trailer support is claimed. Detailed per-request
telemetry is bounded; public deployment still requires provider-side checks and
an actual smoke-tested URL. See the [deployment guide](docs/deployment.md).
Do not expose this local foundation as a hardened public gateway.

## Design and evidence

### Release-review evidence (local, 2026-10-28)

| Claim | Test or benchmark | Actual observed result | Caveat |
|---|---|---|---|
| Gateway tests and static checks pass | Go 1.27.3: `go test -count=1 ./...`, `go test -race -count=1 ./...`, `go vet ./...`, `go mod tidy -diff`; `REDIS_TEST_URL=redis://127.0.0.1:36379/0` for the Go tests | All Go packages passed with no race report; vet and tidy produced no findings or module diff | Windows 11 amd64 local host; Go also discovers a no-test package under `web/node_modules/flatted` |
| Gateway fault behavior works against controlled fixtures and real Redis | `scripts/circuit-chaos.py`; Redis integration suite | 10 sequential requests observed 500, 500, 503×3 (without added upstream hits), 504 on the slow half-open probe, 503, then 200×3 after recovery | Loopback-only fixtures and disposable Redis; not a production fault test |
| Gateway and dashboard Compose stack builds and serves fixed routes | `docker compose config --quiet`, `up --build --wait`; bounded HTTP smoke | Redis, both simulators, gateway, and web were healthy; `/healthz`, `/readyz`, `/one`, `/two`, dashboard `/`, and `/api/healthz` returned 200; `/metrics` returned 401 without a token and 200 with it | First startup found host port 6379 occupied; rerun used `REDIS_PORT=36380`. Verified URLs are local only: `http://127.0.0.1:8080` and `http://127.0.0.1:3000` |
| Worker API, scheduler, repository, retry, and process recovery pass with PostgreSQL | `docker compose -p rag-release-workers -f compose.workers.yaml --profile test run --build --rm tests` | 53 tests passed in 84.667s against disposable PostgreSQL 16.15 and independent worker processes | Expected connection-refusal lines are emitted by the database-outage test; suite passes |
| Worker Compose demo stack is runnable | Full `control` + `workers` profile startup and local HTTP smoke | PostgreSQL/dashboard healthy; API `/healthz` and Swagger 200; API and dashboard proxy returned 401 without token, 200 with token; one submitted `synthetic_report` reached `succeeded` | Loopback URLs `127.0.0.1:8000` and `127.0.0.1:3000`; checked-in token is for local demo only |
| Gateway benchmark reports measured direct, proxy, 429, and open-circuit behavior | `bench/run.py --requests 100 --concurrency 2`; raw `docs/verification/phase-5-benchmark.json` | 1KiB HTTP/1.1 loopback: direct 100×200 in 0.134599s (p50/95/99 1.551/2.510/22.444ms); proxy 100×200 in 0.186078s (3.386/5.082/7.705ms); 40×429 and 40×503, zero unexpected statuses | One short run using Python fixtures/client, no TLS or repeated-trial interval; nearest-rank percentiles do not establish gateway overhead targets |
| Worker query and throughput artifacts are measured | 15 before and 15 after worker matrix cases; `benchmarks/worker/BENCHMARKS.md` | All completed with zero job errors; 100-job throughput at 16 workers was 4.690→7.020 jobs/s; candidate plan was 4.019ms/183 shared hits→0.105ms/5 hits | One synthetic local matrix; EXPLAIN candidate selector omits update/event insert; no capacity inference |
| Frontend and container build checks complete | `npm run lint`, `npx tsc --noEmit`, Next production build during Compose image build | Lint/type/build passed for current UI changes | No hosted CI run or deployment smoke; no public URL is claimed |

- Git diff review included active worker observability, query-plan/benchmark, UI, and documentation edits. Only the duplicate retry-counter increment was fixed in that code, with a regression test. `git diff --check` passed.
- CI and hosted deployment remain unverified. No provider credentials or deployed HTTPS URL were available.

- [Smallest useful v1 and requirement-to-test matrix](docs/requirements.md)
- [Phase 1 architecture decisions](docs/decisions/ADR-004-phase-one-http-contract.md)
- [Threat model](docs/threat-model.md)
- [Phase evidence and remaining tasks](PLAN.md)

Current path: route/input checks, bounded context, Redis admission, circuit admission,
shared-transport proxy. See [circuit policy and chaos reproduction](docs/circuit-breaker.md), the matrix and plan
for implemented tests vs planned guarantees. Historical Phase 0 evidence is retained;
Phase 1 intentionally replaces the print-and-exit scaffold contract.

## Redis admission

Start Redis with docker compose up -d redis before the native gateway.
`REDIS_URL` defaults to `redis://127.0.0.1:6379/0`; use `rediss://` and private
authenticated networking in deployment. Never put credentials in command lines.
Both routes default to 60 unit requests per minute, fail-closed (503).
Use -limit-one, -limit-two, -rate-window, -redis-failure-one open
or -redis-failure-two open to choose explicit operator policies. Replicas must
share -rate-namespace, limits, window and identity cap. Changing policy requires
waiting up to two windows for expiry or deliberately selecting a fresh namespace.

The weighted two-bucket estimate is approximate, not an exact rolling-window
ceiling. Redis TIME drives atomic Lua check/increment. Only accepted requests
consume quota; upstream failures do not refund it. Peer IPs are canonicalized and
hashed; forwarding headers never select identity. NAT/ingress users share quota.
Active identities per route are capped (default 1024); new peers at capacity get
503. Denied requests do not extend identity-state expiry.

429 includes integer delay-seconds Retry-After. Compatibility fields
RateLimit-Limit/Remaining/Reset report the configured limit, whole requests left
in the estimate, and seconds until history fully decays without new traffic.
Reset is relative, not a timestamp or the next admission time. Fields describe
the decision instant, not reserved future capacity. See
[ADR-005](docs/decisions/ADR-005-distributed-admission.md) for formulas and trade-offs.
Unknown quota on Redis failure omits these fields; fail-open forwards, fail-closed
returns 503. A timed-out operation may have consumed quota and is never retried.
The default Redis budget is 100ms including pool wait; -redis-timeout accepts
5ms..1s. Runtime scheduling can add delay. Cancellation cannot fail open.

Gateway.AdmissionStats and ObserveAdmission provide fixed per-route counters
and timing hooks; CLI logs summaries on shutdown and redacted dependency failures.
`/metrics` is available for private scraping; it bypasses quota admission and
must not be exposed on an untrusted public listener. It exports fixed-cardinality
request, admission, latency, Redis error and process-local circuit metrics.
Health/readiness also bypass quota admission; required readiness uses bounded
Redis PING. Require noeviction; failover/clock jumps can lose
history, and fail-open does not preserve globally enforced quotas.

Run real Redis tests (use an isolated development Redis):

```powershell
$env:REDIS_TEST_URL = 'redis://127.0.0.1:6379/0'
go test -v -count=1 ./...
go test -race -count=1 ./...
```

Without REDIS_TEST_URL integration tests explicitly skip. Boundary tests execute
a private copy of the Lua script with controlled time against real Redis; the
production script separately verifies Redis TIME expiry. The process test launches
two independent gateway OS processes and checks aggregate 200/429 decisions.
Tests use unique namespaces and expiring keys, never FLUSHDB.

## Local route reload and readiness

Run `./bin/gateway -config config/routes.example.json` for validated file polling
(1s default). A successful change atomically replaces both routes; in-flight
requests finish using their old snapshot. Invalid/missing files retain the last
good version. The file controls origin targets, per-route Redis `closed`/`open`
outage modes and `required`/`policy` readiness; quota parameters remain startup
settings. Default readiness requires Redis. Explicit policy mode tolerates outage
only when both routes fail open. No HTTP admin API is exposed.

See [configuration format, restoration and readiness semantics](docs/reload-readiness.md).

## Gateway dashboard and safe demo controls

The Next.js dashboard is served at `/gateway`. It polls the gateway's
token-protected `/demo/status` endpoint through same-origin server routes. Set
the same server-only `GATEWAY_DEMO_TOKEN` (32–256 bytes) in the gateway and web
process, and set `GATEWAY_INTERNAL_URL` in the web process to the gateway's
internal origin (default `http://127.0.0.1:8080`). Do not expose the token to
browser JavaScript. Example local setup, using a generated local secret:

```powershell
$env:GATEWAY_DEMO_TOKEN = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:REDIS_URL = 'redis://127.0.0.1:6379/0'
go run ./cmd/gateway -listen 127.0.0.1:8080 -upstream-one http://127.0.0.1:9001 -upstream-two http://127.0.0.1:9002
```

In a second terminal, use the same `GATEWAY_DEMO_TOKEN`, set
`GATEWAY_INTERNAL_URL=http://127.0.0.1:8080`, then run `cd web; npm run dev`.
Open `http://localhost:3000/gateway`. The buttons issue one normal fixed GET,
up to the configured `/one` rate limit plus one (never over 64), or an isolated
breaker FSM simulation. Demo actions are single-flight per gateway process and
have per-process cooldowns;
the rate burst requires Redis admission and a configured limit below 64. Status
and action requests have no arbitrary target or body. For a public demo, expose
only the Next.js UI/BFF through the hosting ingress and keep the Go gateway
reachable only from that web tier. Do not expose the gateway's direct `/demo/*`
endpoints or `/metrics` publicly; allow metrics scraping only from trusted
monitoring.

The dashboard reports process-lifetime counters and one sample timestamp; its
independent atomic reads can differ slightly during concurrent traffic, and it
does not invent a rolling time series. Request duration includes response copying;
upstream duration ends at response headers; Redis duration is limiter `Check`
wall time. It shows averages and finite histogram bucket upper bounds, not exact
p95/p99. Empty samples
and API failures remain visible as such. The circuit recovery action is labeled
synthetic and does not fail a configured upstream.

## Independent PostgreSQL workers (Phases 3–6)

The control plane and cron materializer run as separate processes; neither runs
handler code. Workers claim jobs with PostgreSQL `FOR UPDATE SKIP LOCKED`, a process UUID, an expiring lease,
a fencing UUID, and monotonic claim generation. A stale worker cannot renew,
complete, or record the simulated effect after replacement claim. Retry uses
capped exponential backoff with stable hash jitter; terminal failures enter the
dead-letter state. Execution remains at-least-once: a process killed after work
but before acknowledgment may execute the handler again. The notification
simulation uses a PostgreSQL idempotency ledger to demonstrate deduplication;
this does not make arbitrary external delivery exactly-once.

The isolated Compose file starts a project-scoped local PostgreSQL database and at
least three independent worker processes. It publishes no database port and does
not start or change the gateway/Redis Compose stack:

```powershell
docker compose -p rag-worker-p3 -f compose.workers.yaml --profile workers up --build -d
docker compose -p rag-worker-p3 -f compose.workers.yaml logs -f worker-1 worker-2 worker-3
```

Enqueue a bounded synthetic report (JSON data only; no code or command field is
accepted):

```powershell
docker compose -p rag-worker-p3 -f compose.workers.yaml --profile ops run --rm enqueue enqueue --type synthetic_report --payload-json '{"records":[{"category":"books","amount":4},{"category":"books","amount":7}]}'
docker compose -p rag-worker-p3 -f compose.workers.yaml --profile ops run --rm status status --limit 10
docker compose -p rag-worker-p3 -f compose.workers.yaml --profile ops run --rm workers
docker compose -p rag-worker-p3 -f compose.workers.yaml --profile ops run --rm enqueue enqueue --type notification_simulation --idempotency-key demo-request-001 --payload-json '{"template":"receipt","recipient_ref":"demo_account"}'
docker compose -p rag-worker-p3 -f compose.workers.yaml --profile ops run --rm enqueue retry 42 --reason 'operator approved retry'
docker compose -p rag-worker-p3 -f compose.workers.yaml --profile ops run --rm enqueue replay 42 --reason 'reproduce reported result'
docker compose -p rag-worker-p3 -f compose.workers.yaml down
```

Other fixed job types are `image_metadata` (bounded base64 PNG IHDR metadata),
`csv_transform` (uppercase one named column, max 10000 rows), and
`notification_simulation` (allowlisted template; no network delivery). Payloads
and results are each limited to 512 KiB. Worker concurrency is 1..16 per process;
handler timeouts kill the isolated child. Ctrl+C/SIGTERM stops new claims and
drains current work for up to the configured grace period. A forced process or
container kill cannot run cleanup; PostgreSQL lease expiry and another live
worker recover the job. The local database uses a project-scoped Docker volume;
`down -v` removes it. The stack uses development-only credentials and is not a
production deployment.
An enqueue idempotency key returns the original job ID for matching content and
rejects reuse with changed content. `retry` only requeues a dead job and resets
its attempt budget with an audit reason. `replay` only accepts terminal jobs and
creates a new linked ID; replay can repeat logical work and requires an explicit
reason. `workers --stale-after-seconds N` reports heartbeat staleness.

Run unit plus real PostgreSQL process/recovery tests in a separate disposable
test database. The test container refuses a database name without `_test` suffix:

```powershell
docker compose -p rag-worker-p3-test -f compose.workers.yaml --profile test run --build --rm tests
```

Tests use real PostgreSQL and worker processes to inject pre-start, mid-work,
post-effect/pre-ack crashes, pause workers beyond lease expiry, race competing
claims, test generation fencing, repeated failures, duplicate submission and
handler runtime bounds. The verification script also stops and restarts only its
own disposable PostgreSQL service while workers are active. Full commands,
failure-mode matrix, logs and observed outcomes are in
`docs/verification/phase-4-recovery.txt`. See [ADR-006](docs/decisions/ADR-006-independent-worker-runtime.md)
and [ADR-008](docs/decisions/ADR-008-worker-recovery.md) for guarantees and
trade-offs.

The worker companion also includes a separate authenticated FastAPI control
plane and independent cron materializer. To run the API, scheduler and three
workers locally, see [the control-plane runbook](docs/worker-control-plane.md)
and executable [PowerShell request examples](docs/worker-api-examples.ps1).
Swagger is served at `http://127.0.0.1:8000/docs`; the control API never executes
handlers itself. PostgreSQL-backed API/scheduling tests and observed results are
recorded in `docs/verification/phase-5-control-plane.txt`.

The Chronos operator dashboard is a separate Next.js app in the same local
stack. It provides the overview, job lifecycle, recurring schedules, worker
registry, dead-letter, event-derived observability and bounded queue defaults.
Run it with:

```powershell
docker compose -p chronos -f compose.workers.yaml --profile control --profile workers up --build -d
```

Open <http://127.0.0.1:3000> and use the disposable local token documented in
[`docs/chronos-dashboard.md`](docs/chronos-dashboard.md). The UI uses a
same-origin allowlisted API proxy; the API remains authoritative for auth,
validation and mutations. Screenshots, verified journeys, data semantics,
local frontend checks and deployment limitations are described in that runbook.
Run `scripts/verify-dashboard.ps1` for the isolated PostgreSQL, live Compose and
Playwright verification. No dashboard data is decorative or synthesized.
