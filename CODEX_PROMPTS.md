# Copy-Paste Codex Prompts — Resilient API Gateway

## Prompt 0 — Repository audit and architecture
Read AGENTS.md and PLAN.md completely. Inspect the existing repository (if any) before writing code. Produce a short requirement-to-test matrix, security threat model, initial ADRs, and a minimal runnable Go project scaffold. Define the smallest useful v1 and explicitly reject/defer unnecessary technologies. Update PLAN.md. Compile/test before declaring success. Do not build the whole gateway in one pass.

## Prompt 1 — Proxy core
Implement only Phase 1 of PLAN.md. Use Go's httputil.ReverseProxy with Rewrite and explicit trusted forwarding rules. Add immutable static route selection for two mock upstreams; shared reusable Transport with bounded timeouts/connection budgets; cancellation, graceful shutdown, health endpoints. Preserve streaming and HTTP semantics where possible. Add httptest scenarios covering forwarding, query/path, POST bodies, hop-by-hop headers, upstream failure, timeouts and malicious forwarding headers. Do not add Redis or frontend yet. Explain necessary vs unnecessary choices, run tests, and update the plan.

## Prompt 2 — Redis Lua limiter
Implement Phase 2 only. Compare token bucket, fixed window, weighted sliding-window counter and exact sliding log in an ADR; choose a weighted two-bucket counter unless evidence demands otherwise. Implement Redis Lua atomic check/increment with TTL and server-time semantics, correct limit headers, 429, identity security, fail-open vs fail-closed, latency bound, and metrics hooks. Test boundaries and concurrent requests across two independent gateway processes against real Redis. Do not claim exact rolling-window guarantees. Update PLAN.md with observed evidence.

## Prompt 3 — Circuit breaker FSM
Implement Phase 3 only. Build minimal Closed/Open/Half-Open FSM scoped to upstream with per-instance policy, protected concurrency, injectable clock, threshold definition, bounded half-open probe admission and configurable recovery count. Distinguish upstream 5xx/transport failures from client 4xx. Test all transitions and simultaneous probes under go test -race. Integrate before proxying and ensure fast 503 in Open state. Add a chaos reproduction script with bounded load. Record design decisions and tests.


## Prompt 4 — Routing reload + Redis outage behavior
Implement Phase 4. Snapshot-based validation and atomic replacement of route/proxy configuration; old in-flight traffic completes using previously selected snapshot. Reject invalid targets and retain last good version. Add explicit, testable Redis outage modes and readiness behavior. Test reload during concurrent long-running requests, invalid config and restoration. Avoid building a public admin API. Update docs and PLAN.md.

## Prompt 5 — Observability and benchmark rigor
Implement Phase 5. Add Prometheus metrics with bounded cardinality, structured logs and IDs, upstream/circuit/Redis/request timing with well-defined semantics. Create reproducible bench/ scripts for direct vs proxied requests, 429 and circuit-open fail-fast workloads. Collect raw data and document hardware, Go version, concurrency, payload, p50/p95/p99, errors, test duration and limitations. Profile heap/allocations before proposing pooling. Do not invent data or assert targets without evidence.

## Prompt 6 — Adversarial review
Act as a skeptical senior infra/security reviewer. Implement Phase 6, beginning with a concrete threat model and negative tests: client-supplied forwarded headers, SSRF, open proxy, malformed paths, timeout propagation, response truncation, health/readiness faults, Redis failures, high-cardinality metrics, graceful shutdown, and unsafe retries. Prioritize MUST-FIX, then SHOULD-FIX; defer speculative protections. Change as little as possible and show exact test evidence.

## Prompt 7 — Recruiter-facing dashboard
After verifying backend phases, implement Phase 7. Design a minimal, clean, modern, accessible dashboard that displays live gateway state and actual sampled metrics; include safe buttons for normal request, bounded rate-limit burst, and isolated circuit-breaker failure/recovery. Public controls must be server-authorized and limited; no arbitrary URL target, unbounded load or live reconfiguration. Do not fabricate graphs, stats, or status. Prefer simple frontend integration, explain if a separate framework is truly needed, verify mobile and empty/error states.

## Prompt 8 — Production-minded deployment
Implement Phase 8. Prepare small multi-stage Docker image, non-root execution, local Docker Compose including Redis and two upstreams, health probes, env config validation, CI, protected metrics, secrets and graceful shutdown. Evaluate current Render/Fly.io/other hosting constraints and choose simplest viable affordable deployment with HTTPS and compatible Redis. If authentication/credentials are missing, produce an exact deployment checklist rather than pretending it is live. Perform smoke tests only on a deployment actually accessible and record verified URLs.

## Prompt 9 — Final engineering audit
Perform a release-review of the entire project. Read AGENTS.md, PLAN.md, ADRs, code, tests, benchmark evidence and git diff. Identify correctness risks, bogus claims, overengineering, duplicate logic, unused dependencies and documentation mismatch. Fix only issues that materially improve correctness, safety, reliability or demonstrability. Run go test, -race, go vet, integration tests, black-box fault tests and local Compose smoke tests. Write an evidence table: claim / test or benchmark / actual observed result / caveat. Update README and PLAN.md. Do not claim unexecuted tests ran.

## Prompt 10 — Resume and interview packaging
Read verified repository evidence, actual benchmarks and live-demo screenshots only. Produce a concise GitHub README structure, 3 ATS-friendly quantified resume bullets using only measured numbers, one compelling 90-second technical explanation, and 20 rigorous SDE interview questions with grounded answers. Focus on Redis Lua atomicity, approximation, HTTP semantics, circuit FSM races, connection reuse, fault containment, performance trade-offs and deployment. Mark unverified claims as TODO rather than inventing numbers.

## Reusable necessity/optimization reviewer
Before implementing the current requested feature, inspect existing behavior and answer: What concrete issue does this solve? What measurement or failing test proves the issue? Can standard library or existing code solve it? What is the simplest robust option? What happens if we do nothing? What are new failure modes and maintenance costs? Classify MUST / SHOULD / DEFER / REJECT. Implement only MUST/justified SHOULD changes, then show validation. Update documentation/PLAN.md. Do not introduce speculative microservices, caches, pooling, retry frameworks or abstractions.
