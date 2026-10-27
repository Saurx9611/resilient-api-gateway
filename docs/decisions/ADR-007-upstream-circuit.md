# ADR-007: Per-instance upstream circuit admission

Status: Accepted for the current Phase 3 circuit request. The worker companion
and ADR-006 are preserved; this request implements the original gateway Phase 3
scope, currently labeled Phase 4 in the plan.

Problem/evidence: the gateway forwards every Redis-admitted request even after
repeated upstream failure. Necessity: MUST isolate failure and bound recovery
traffic. Options: do nothing (no isolation), rolling error ratios (more policy
and history), dependency (unnecessary), or a mutex-protected consecutive-failure
FSM. Choose the latter with no new dependency.

One breaker per canonical configured HTTP(S) origin per gateway instance; routes
sharing an origin share its breaker. No Redis coordination or fleet-wide probe
guarantee. Default: 5 consecutive completed failures, 5s Open, one Half-Open
probe at a time, two successful probes to recover. CLI config applies per instance.
Inject a concurrency-safe Now function for tests; production time.Now retains
Go's monotonic component for duration comparisons. No timers or background loops.
Open becomes Half-Open lazily on the first admission at/after its deadline.

Closed: success clears consecutive failures; neutral leaves them unchanged;
threshold failure opens. Completion order defines consecutive observations.
Half-Open: any failure reopens for a fresh interval; neutral frees the slot but
does not advance recovery. Close only after the configured successes and all
outstanding probes finish. Admit at most min(probe capacity, remaining required
successes) probes. Track outstanding probes across generations: slow old probes
cannot allow overlapping recovery waves beyond the configured bound. Ignore
stale results after transitions, with exactly-once permit completion.

Failure classification: HTTP 500..599, transport errors, response-body read
errors and gateway/upstream deadlines are failures. Client cancellation and
request-body read failures are neutral; 400..499 are neutral, not upstream
faults or recovery evidence. Other final statuses are successful only after
body EOF. Abandoned bodies are neutral. No retry/refund. A wrapper around the
shared Transport observes the final outcome without buffering; body Close and
handler cleanup release permits even on early exit/panic. Empty responses complete
at headers; all other responses retain the permit through body EOF/Close.
Closing an unfinished 5xx body is still a failure; closing an unfinished successful
body is neutral. Client cancellation takes precedence when observed at completion.
Review necessity: releasing permits at 4xx/5xx headers would let slow response
bodies overlap later probe waves. MUST retain their slots until body completion;
verify a real streamed 5xx probe holds its slot. The existing finite request
deadline bounds this delay; no extra buffering or cancellation machinery.

Order remains route/input validation -> Redis -> circuit -> proxy. Open/probe
capacity rejection returns uncached 503 before application body reads, without
upstream I/O. Redis latency still precedes circuit rejection, so fail-fast means
no upstream wait, not bypassing distributed quota. Already-admitted work is not
cancelled on opening. Retry-After rounds Open duration up; full probe capacity
uses 1s advisory retry. Fixed per-route snapshots expose state, transitions,
rejections, failure streak and active probes (shared snapshots for shared origin).

Trade-offs: long streams occupy probe slots until completion; no probe leaks
after cancellation, but a live slow probe relies on existing request/transport
timeouts. A local sequential threshold is intentionally simpler than a traffic
ratio and can open on a small sample. No public fault/control endpoint, reload,
metrics server or worker changes. Verification: deterministic FSM edges and
stale/duplicate completions, concurrent probe admission under race, HTTP status/
transport/body/cancellation classification, shared-origin vs independent-origin,
fast uncached 503 without upstream/body read, Redis regressions, and loopback-only
bounded chaos reproduction. No latency/production-readiness claim follows.
