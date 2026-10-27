# ADR-008: Atomic snapshots, local reload and Redis readiness

Status: Accepted for the current Phase 4 request (the plan previously labeled
this gateway scope Phase 5). Worker phase records remain independent.

Problem/evidence: routes/proxies are currently fixed fields, and readiness only
reports serving state. Updating individual fields would mix policies and upstreams
inside a request. Necessity: MUST validate/build everything before one atomic
replacement. Alternatives: restart only (no live reload), locks around request
lifetime (long streams block reload), public admin endpoint (unnecessary exposure),
or immutable snapshots plus atomic.Pointer. Choose the latter; no new dependency.

Keep /one and /two route IDs/prefixes fixed. Reloadable configuration contains two
operator-allowlisted origin targets, per-route Redis failure mode (closed/open),
and readiness mode (required/policy). Numeric quota/window/cap/namespace, circuit
policy, Redis connection and transport limits remain process startup settings.
Changing distributed quota policy live would conflict with old in-flight requests
and other replicas; reject unsupported fields instead of resetting quota history.

Build URL/proxy/policy objects privately, validate both routes, then atomically
store one versioned snapshot under a reload mutex. Each request loads once before
admission and retains its chosen route through Redis, circuit and proxy completion.
Reuse one shared HTTP transport and Redis pool; never close active resources on
reload. Stable route IDs retain quota on target changes. Preserve breakers for
canonical origins still in the current snapshot, including moved/shared origins;
new origins start Closed. Removed origins remain alive through old references,
but re-adding after removal creates new state. No unbounded historic-origin cache.
Startup policy is immutable, so retained breakers never acquire mixed policies.

Expose a local Go Reload method and optional -config JSON file polling (default
1s, configurable 100ms..1m). No HTTP/admin mutation endpoint or signal requirement.
Limit files to 64KiB, require regular files, reject unknown fields, wrong route
counts, malformed/trailing JSON, invalid origins and mode strings. File content is
untrusted diagnostic data: return/log generic errors, never contents/URLs. Hash
content to avoid repeated reloads of identical bytes. Invalid/missing files retain
the last good snapshot; restoration is retried when content changes. Operators
should replace files atomically; partial writes can be rejected safely. No config
version supplied by clients; successful changed snapshots monotonically increment
local version. Fixed counters expose successful replacements/rejections.

Readiness: /healthz remains local liveness. /readyz requires serving/not-draining
plus snapshot policy. Default required probes Redis even when every route is
fail-open. Explicit policy skips the dependency only if every route is fail-open;
with any fail-closed route Redis is required. A bounded Redis PING shares the
existing pool/deadline and consumes no quota/identity slot. PING proves reachability
and authentication, not script ACLs, quota-policy agreement or upstream health.
Library gateways without Redis need no probe; those using an Admission without
a health function fail readiness when Redis is required. Readiness never gates
data requests globally: routes independently apply their selected outage mode.
Startup can serve while Redis is unavailable, but default readiness returns 503.
Restoration makes the next probe ready without a restart; no cached health timer.
Drain/shutdown takes precedence, including a second state check after probing.

Trade-offs: readiness probes use bounded Redis work per HTTP probe, not a cached
signal or comprehensive dependency guarantee; ingress may remove a required-mode
instance even if some routes can fail open. Polling adds up to one interval delay.
Old snapshots are collected after request completion; repeated reload with long
requests can temporarily retain multiple snapshots, bounded by existing request
lifetime rather than a new global concurrency guarantee. Trusted operators can
change allowlisted origins; deployment egress controls still matter.

Verification: race-tested concurrent long streams with repeated swaps, requests
blocked in Redis during reload retaining old outage mode/target, invalid config
preserving version/state, breaker/quota retention, strict file parsing/watch
restoration, startup/drain/readiness modes and real Redis outage/recovery tests.
Retain all earlier HTTP/Lua/FSM regressions. Record actual checks in PLAN.md.
