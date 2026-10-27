# ADR-011: Security review findings and negative-test scope

Status: Accepted for the requested Phase 6 review (the repository execution plan
labels this work Phase 7).

Problem/evidence: the gateway has a written threat model and broad Phase 1–4
negative coverage, but a reviewer needs evidence for the requested attack/failure
matrix and assurance that non-idempotent requests are not retried. Doing nothing
would leave gaps in evidence rather than a demonstrated exploit. Necessity:
MUST review and test; SHOULD fix only an observed behavior defect. Options were
to add blanket network/path/body restrictions, implement auth/retry frameworks,
or preserve the existing static-route/streaming contracts and add narrowly
targeted regression tests. Choose the latter because the code already enforces
the relevant properties and broad restrictions would break loopback/private
upstreams or reject legitimate streamed traffic without workload evidence.

Added negative assertions ensure unique request paths and client request IDs do
not appear in Prometheus output, and a POST whose upstream drops its connection
is attempted once and returns 502. Existing tests cover forwarding-header
replacement, static target validation, absolute-form rejection, malformed paths,
client cancellation, request deadlines, response truncation, readiness/drain,
Redis fail-open/closed, and shutdown drain/forced cancellation.

Trade-offs and remaining controls: `/metrics` is unauthenticated and must remain
private; loopback is the default but `-listen` can expose it. Fixed routes are
not client-selected targets, but the service has no route-level authentication;
place sensitive/public use behind trusted authentication and abuse controls.
Operator allowlists do not prevent DNS rebinding or dangerous egress. Do not add
private-IP bans, a body buffer, or automatic retries without a concrete product
contract and evidence. Global inbound concurrency/body budgets remain unresolved.

Verification: focused negatives and complete `go test -race -count=1 -v ./...`
passed with `REDIS_TEST_URL` set to a dedicated loopback Redis; raw output is in
`docs/verification/phase-6-security.txt`. `go mod verify` passed, the full module
inventory was captured, and `govulncheck` v1.9.0 reported no vulnerabilities;
see `docs/verification/phase-6-dependencies.txt`. Unimplemented public deployment
controls remain explicit in `docs/threat-model.md`.
