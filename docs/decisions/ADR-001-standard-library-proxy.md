# ADR-001: Standard-library HTTP reverse proxy

Status: Accepted for v1; implementation deferred to Phase 1.

Problem/evidence: the repository has no proxy implementation. HTTP streaming,
cancellation and forwarding semantics are correctness and security boundaries.
Options: do nothing (no useful gateway); custom socket/HTTP implementation;
external gateway; Go `httputil.ReverseProxy` using `Rewrite`.
Necessity: MUST for v1, DEFER implementation until Phase 1.
Decision: use ReverseProxy with explicit URL/path/query/Host and forwarding-header
policy, static allowlisted upstreams and one shared bounded Transport. Prefer
standard behavior over custom buffering or connection management.
Trade-offs: net/http semantics and protocol limits must be documented and tested;
timeouts that bound resources may interrupt long streams. Admission can precede
application body reads, but cannot undo bytes already buffered by net/http, OS or
an ingress. No universal zero-buffering or zero-allocation claim is justified.
Verification: black-box httptest cases for paths, queries, headers, streaming,
cancellation, disconnects, timeouts and malicious forwarding headers; race tests.
Revisit only for a demonstrated unsupported protocol or measured bottleneck.
