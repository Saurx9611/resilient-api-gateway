# ADR-005: Atomic approximate Redis admission (Phase 2)

Status: Accepted, 2026-10-27. Refines ADR-002; verification follows in PLAN.md.

Problem/evidence: Phase 1 forwards every matched request. Two independent processes
need one admission decision before body reads. In-memory counters cannot share it.
Doing nothing fails that requirement. Necessity: MUST implement Redis admission;
DEFER exact history, local fallback, retries, ingress trust and a metrics server.

| Algorithm | State/cost per identity | Semantics / trade-off | Decision |
|---|---|---|---|
| Fixed window | One counter, O(1) | Almost two windows' allowance can cluster across a boundary | Reject for v1 |
| Token bucket | Tokens + timestamp, O(1) | Clear refill/burst allowance, but a different product contract | Defer |
| Weighted two-bucket counter | Current/previous counters + bucket index, O(1) | Assumes uniform prior-window traffic; can over- or under-estimate an exact rolling window | Choose |
| Exact sliding log | Timestamp per admitted request; pruning/index cost | Exact chosen interval at greater memory/work cost | Defer without an accuracy requirement |

Use one private Redis hash per route/identity containing bucket/current/previous.
For integer window W milliseconds, Redis TIME supplies now (millisecond precision),
bucket b=floor(now/W), elapsed e=now-bW. Roll previous=current on one-bucket advance;
discard history after a larger gap. Scale the estimate as S=current*W+previous*(W-e).
Admit a unit request only when S+W <= limit*W, increment current only on admission.
Limits are 1..1,000,000, W is whole milliseconds in [1s,1h]; products fit Lua's exact
integer range. No client clock or runtime test-time override enters the script.
This is NOT an exact rolling-window ceiling. A cluster at the end of the previous
bucket can be underweighted; an early cluster can be overweighted.

Expire accepted state at (b+2)W via PEXPIREAT, retaining its current count throughout
the next bucket. Denials neither increment nor extend state expiry. Reset is the
time until the estimate becomes zero without new traffic: end of next bucket if
current>0, else end of this bucket. Retry delay solves the linear decay for the
next unit: if current<limit, wait for prior weight to free one unit; if current is
full, wait for rollover plus decay of that full bucket. Round up, never return zero
Retry-After on rejection. Redis wall time is authoritative: backward movement
within a bucket is conservative; crossing back behind stored bucket fails via the
configured outage policy. Forward jumps/expiry/failover can lose history; no
monotonic-time, durable-count or failover consistency guarantee is claimed.

Identity: parse RemoteAddr as an IP:port, remove IPv6 zone, unmap IPv4-mapped IPv6,
SHA-256 the canonical address. Never consult X-Forwarded-For, credentials, request
Host/path/query or cookies for identity. Invalid socket identity is 400 even on a
fail-open route. NAT/ingress peers share quota until explicit ingress trust exists.
Namespace and route IDs are bounded safe tokens; Redis keys contain fixed digests,
not raw IPs. Hashing is not anonymization and does not itself bound cardinality.

MUST bound active identities: one expiring sorted-set registry per route, capped
at 1024 by default (maximum 4096), checked/pruned inside the same script. Unknown
identities at capacity receive 503 + Retry-After until a slot expires; existing
identities retain service. State plus two fixed keys per route bounds healthy
successful-script storage to cap+2 keys, not the number of arbitrary client claims.
A route policy key rejects inconsistent limit/window/cap across replicas instead
of silently splitting counters. All replicas must share namespace and policy;
changing policy requires expiry (up to two windows) or a deliberate namespace reset.
Require private Redis and noeviction. Persistence/failover/eviction/OOM or partial
script errors are outside healthy-operation invariants; Lua isolation is not rollback.

Use maintained go-redis/v9 v9.23.0 (resolved from the Go module proxy), pin modules
and sums. The standard library has no Redis client; reject writing a RESP client.
One shared bounded 16-connection pool; 100ms default total context budget (allowed
5ms..1s) including pool wait, dial/handshake and script cache miss. Disable command
retries. EVALSHA -> EVAL only on NOSCRIPT is safe; never retry an ambiguous timeout
that might have consumed quota. TLS/auth use REDIS_URL, never logs or CLI secrets.

Both routes default fail-closed (503). Explicit fail-open admits on Redis/script
errors without claiming quota headers or a globally consistent local substitute.
Record fixed per-route atomic counters and a bounded event hook for admitted,
limited, fail-open, fail-closed, identity-capacity and invalid identity, plus Redis
error counts/time. Hooks are internal, synchronous and must not block; no public
metrics endpoint or future telemetry framework. Log fallback without error strings,
URLs, IPs or request bodies. Client cancellation never triggers fail-open admission.

HTTP: 429 is RFC 6585; Retry-After is RFC 9110 integer delay-seconds (ceil). Expose
documented compatibility fields RateLimit-Limit, RateLimit-Remaining and
RateLimit-Reset (relative seconds, NOT Unix time). These are a project compatibility
contract, not a claim of implementing the evolving IETF RateLimit structured fields.
Remaining is floor((limit*W-S)/W) after the decision. Omit quota values when unknown
(dependency/capacity failures). Protect gateway quota fields from upstream overwrite
and prevent caching limited responses. Reserved quota response trailers are rejected.
An admitted request consumes quota even if upstream fails or the client later leaves.
Health/readiness bypass limits; dependency-aware readiness remains Phase 4.

Verification: real Redis script tests at boundaries, TTL/reset, integer rounding,
clock jumps, NOSCRIPT, policy mismatch, identity-cap churn and contention; fake
transport failures test latency/outage HTTP policy only, never Lua atomicity.
Boundary tests replace the TIME expression only in a private test copy of the exact
script, executing it in real Redis; production TIME is separately bracketed against
Redis TIME. Run two separate gateway OS processes against one real Redis with
concurrent bounded traffic, assert aggregate admission and per-process participation.
No throughput benchmark or exact rolling-window result is inferred.

Sources: https://redis.io/docs/latest/develop/programmability/eval-intro/,
https://redis.io/docs/latest/commands/time/,
https://redis.io/docs/latest/commands/pexpireat/,
https://httpwg.org/specs/rfc9110.html#field.retry-after,
https://www.rfc-editor.org/rfc/rfc6585#section-4.

Implementation review necessity: a full test run exposed Phase 1's empty early
response occasionally waiting for an unfinished upload. ReverseProxy's asynchronous
negative flush timer can be stopped after an empty copy before flushing; deferred
body Close can then wait on the incoming upload. MUST flush final response headers
explicitly through a small ResponseWriter wrapper (ResponseController Unwrap), but
never flush interim 1xx into a final response. Verify the existing synchronized
early-response test. Also require an explicit Redis URL hostname (ParseURL otherwise
defaults to localhost), disable dial retries and managed maintenance notifications
so they cannot add hidden connection behavior or relaxed deadlines.
