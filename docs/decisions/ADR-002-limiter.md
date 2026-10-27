# ADR-002: Approximate distributed admission

Status: Accepted design; implementation deferred to Phase 2.

Problem/evidence: v1 must enforce a shared limit across two gateway replicas;
there is no limiter yet. Per-process counters cannot provide that property.
Options: do nothing; fixed windows (boundary bursts); token bucket (different
burst contract); exact sliding log (per-request storage); weighted two-bucket
counter (constant bucket state).
Necessity: MUST for v1; exact rolling windows DEFER.
Decision: Redis Lua checks and increments atomically using Redis TIME. Estimate
`current + previous * (1 - elapsed/window)` before admission; accept a unit-cost
request only if the estimate plus one is at most the limit, then increment.
This estimates a rolling window assuming prior traffic was
uniform: it can over- or under-estimate clustered traffic and is not an exact
rolling-window ceiling. Atomicity applies to each invocation, not Redis failover
durability or multiple independent Redis primaries.
Use finite Redis timeouts, validated route IDs and fixed-length hashed trusted
identities. Bound identity/key creation through admission limits and expiring
buckets; hashing alone does not bound the number of attacker identities.
Retain a bucket through the following window; implementation must specify exact
expiry, time-boundary rounding and Retry-After semantics before coding.
Default fail-closed; allow explicit fail-open only for documented non-sensitive
routes, with fallback metrics. No silent local substitute. Redis outage policy
trades availability against abuse protection.
Verification: real Redis tests at edges, TTL/reset, backward/forward time
assumptions, two independent gateway instances under contention, timeouts and
both outage policies. Assert approximation invariants, not an exact log bound.
Revisit for measured accuracy or Redis latency requirements.
