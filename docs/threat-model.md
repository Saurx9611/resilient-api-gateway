# Gateway threat model

Initial design review, updated for Phase 1. Static operator targets, strict path
checks, forwarding-header replacement, hop-header removal, finite transport/server
limits and bounded shutdown now have tests linked in docs/requirements.md.
Phase 2 adds hashed socket-peer identities, bounded route identity registries,
expiring Redis state, explicit outage policies, deadlines and fixed-label hooks.
No forwarding header is trusted for rate-limit identity. NAT/ingress users share
quota; hashes are not anonymization. Redis must be private and use noeviction.
Fail-open, eviction, failover, clock jumps or loss of Redis history can weaken
enforcement. Tests establish healthy single-primary script isolation, not durable
admission across failover. The remaining mitigations are requirements, not
completed hardening claims.
The default listener is loopback; no public admin or fault endpoint exists.

Phase 4 publishes validated immutable snapshots through one atomic replacement.
File polling is local and operator-controlled: protect the config file and its
parent directory as privileged allowlist authority. Unsafe/malformed updates
retain the last good in-memory version; no HTTP request can select or mutate it.
Unchanged canonical origins retain breakers; stable route IDs retain quota.
Numeric quota/circuit policies remain startup-only. Readiness defaults to bounded
Redis PING, with explicit policy mode only tolerating outage when all routes fail
open. PING does not establish script permissions, capacity or upstream health.

Assets: upstream access, route integrity, Redis capacity, service availability,
client metadata, secrets and trustworthy telemetry. Actors: untrusted HTTP clients,
misconfigured trusted ingress, compromised upstream, local config operator.
Trust boundaries: client → ingress → gateway → allowlisted upstream; gateway →
Redis; local operator → configuration; private operator → telemetry. Operator
configuration is privileged, not a public endpoint. Redis is private/authenticated
in deployment; a public Redis is outside supported design.

| Threat | Required mitigation / verification |
|---|---|
| SSRF/open proxy/config injection | Static validated HTTP(S) allowlist, no request-supplied targets; reject credentials/unsafe URLs, no redirect-following client; H2/R1 |
| Identity spoofing/header smuggling | Strip untrusted forwarding headers, explicit ingress trust, standard HTTP parser; H2/L2 |
| Path ambiguity and route bypass | Define encoded/decoded path and prefix semantics, test traversal/escaping; H1/H2 |
| Slow clients, huge bodies, stalled upstreams | Finite header/transport/shutdown budgets and concurrency; streaming/body-limit policy with explicit long-stream trade-offs; H3/H4/S1 |
| Identity churn/Redis exhaustion | Expiring bounded keys, finite identities via admission/ingress budgets, bounded route IDs; L2/S1 |
| Redis partition/failover | Visible per-route outage policy; no claimed global consistency during failover or fail-open; L1/L3 |
| Failure amplification | No automatic retries; local breaker cannot guarantee a fleet-wide probe bound; C1/C2 |
| Secret leakage/cardinality attack | Redacted structured logs and fixed labels; `/metrics` bearer token (32..256 bytes) is required on non-loopback listeners and compared in constant time. Health probes remain open; keep hosted gateway private. `TestMetricsBearerAuthDoesNotProtectHealth` and CLI listener/token validation tests. |
| Unsafe demo/control plane | No public reload or arbitrary fault/load controls; isolated bounded scenarios only; D2 |

Residual risks: DNS rebinding requires deployment egress controls and careful
allowlist/DNS policy; a trusted hostname alone is not network isolation. HTTP/2,
upgrades, request smuggling behind a particular ingress and long-stream support
need explicit compatibility testing before claiming support. Gateway limits do
not replace edge DDoS protection. TLS, credentials, Redis durability, multi-tenant
isolation and public hosting remain unvalidated. No global client concurrency or
body-size budget exists. X-Forwarded-Host is client-controlled metadata even after
replacement from Host; upstreams must not use it for authorization. No ingress is
trusted: behind an ingress the peer/proto identifies its connection, not the original
client. Rejected ambiguous path cases do not guarantee safety for an upstream that
decodes a suffix repeatedly or uses different path semantics. Request trailers are
explicitly rejected (501), not silently treated as supported.

## Phase 6 adversarial review (2026-10-27)

Review scope: client-controlled forwarding metadata and request targets; operator
upstream allowlist and config reload; malformed HTTP paths; cancellation and
truncated streams; health/readiness under dependency faults; Redis failure policy;
telemetry cardinality; shutdown; and retry behavior. This review is for the local
HTTP/1.1 service contract. It does not establish internet-facing readiness.

| Priority | Threat / required control | Concrete evidence | Decision / residual condition |
|---|---|---|---|
| MUST-FIX before public exposure | Arbitrary-target SSRF/open proxy | `TestRejectsInvalidOperatorTargets`, `TestConfigDocumentValidationAndLastGood`, and `TestRejectsUpgradeAndAbsoluteTarget`; routes select only the two validated operator origins and redirects are returned without following | No client-selected URL or redirect fetch is present. DNS rebinding, operator config trust, and egress remain deployment controls; do not add hostname-only claims as SSRF protection. |
| MUST-FIX before public exposure | Forwarded-header identity/proto spoofing | `TestForwardingTrustAndHopHeaders`, `TestActualTLSForwardingProto`, `TestAdmissionHeadersAndNoBodyRead`, and `TestPeerIdentity` | All incoming Forwarded/X-Forwarded-* and X-Real-IP are removed; proxy metadata is rebuilt from the socket peer and TLS. No ingress is trusted; source identity behind ingress is that ingress. |
| MUST-FIX | Path confusion or route bypass | `TestRouteBoundariesAndMalformedInputs`, `TestRejectsBeforeApplicationBodyRead`; raw absolute-form case in `TestRejectsUpgradeAndAbsoluteTarget` | Dot segments, repeated separators, encoded separators/prefixes, malformed query, unsupported methods, and non-origin form are rejected before upstream I/O. Upstreams with repeated/different decoding semantics still need their own routing review. |
| MUST-FIX | Timeout/cancellation not reaching upstream; false complete response after truncation | `TestUpstreamFailureMapping`, `TestClientCancellationReachesUpstream`, `TestResponseDeadlineAfterHeadersAbortsStream`, `TestStreamDisconnectAbortsResponse`, `TestShutdownDeadlineCancelsActiveUpstream` | Request contexts propagate to shared Transport. A post-header failure aborts the stream; status cannot be rewritten after commit. Finite request/server deadlines bound streams. |
| MUST-FIX | Redis outage silently bypasses policy or cancellation fails open | `TestAdmissionOutagePoliciesAndHooks`, `TestCanceledAdmissionCannotFailOpen`, `TestRealRedisConnectionLossPolicies`, `TestRealRedisOutageReloadReadinessAndRecovery`, `TestRedisLatencyBudget` | Fail-open/closed is explicit per route and counters expose fallback. Real Redis outage tests require `REDIS_TEST_URL`; fail-open deliberately weakens enforcement during outage. |
| MUST-FIX before public exposure | User-controlled telemetry cardinality or secrets in labels/logs | `TestMetricsAndRequestID`, `TestRequestIDSanitizesClientValue`, `TestAdmissionOutagePoliciesAndHooks` | Route/outcome/state labels are fixed and paths, identities, URLs and error text are excluded. `/metrics` is unauthenticated: keep the listener private or place authenticated access control in the monitoring edge. |
| MUST-FIX | Shutdown stops admission, drains existing work, then cancels at deadline | `TestHealthReadinessAndGracefulDrain`, `TestShutdownDeadlineCancelsActiveUpstream`, `TestDrainWinsOverInProgressReadinessProbe` | Drain behavior is tested at both completion and forced-deadline edges; health remains liveness while readiness turns false. |
| MUST-FIX | Retrying a non-idempotent request after ambiguous upstream failure | `TestPostIsNotRetriedAfterUpstreamDisconnect` | Gateway code has no application retry. Test fixes this contract for POST. Go Transport may replay only its documented eligible/idempotent stale-connection requests; no retry wrapper or retry budget is added. |
| SHOULD-FIX | Health and readiness responses under Redis faults can be confused | `TestRedisReadinessModesAndRestoration`, `TestReadinessHealthDeadlineAndMissingProbe`, `TestDrainWinsOverInProgressReadinessProbe` | `/healthz` is process liveness; `/readyz` checks serving state and configured Redis policy, not upstream health. PING proves reachability/auth only. |
| SHOULD-FIX before exposing routes publicly | Public callers can use fixed routes without gateway-level authentication | No gateway auth layer exists; default listener is loopback and rates are per peer | Keep local/private or enforce authentication and edge abuse controls at a trusted ingress for sensitive upstreams. Authentication is deferred until a concrete public identity contract exists. |

### Deferred protections

Do not add broad private-IP/loopback bans to the operator allowlist: local Compose,
tests, and controlled private upstreams need those targets, and DNS resolution can
change after validation. Enforce egress/DNS policy in deployment. Do not buffer
whole bodies or responses to detect truncation; streaming tests prove the client
receives an incomplete-body error. A global inbound concurrency/body cap needs a
separate capacity policy and workload evidence. No TLS termination, HTTP/2,
trusted-ingress parsing, or fleet-wide breaker guarantee is claimed. Retry remains
disabled because ambiguous delivery of writes is unsafe without an explicit
idempotency contract.

Deployment follow-up (Phase 9): the earlier Phase 6 finding that `/metrics` was
unauthenticated is resolved for the gateway CLI. A non-loopback listener now
requires a 32..256-byte bearer token; token comparison is constant-time. The
Render topology also keeps the gateway private. See ADR-014 and the deployment
verification record; hosted configuration remains unverified.
