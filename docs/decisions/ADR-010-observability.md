# ADR-010: Bounded gateway metrics and request logs

Status: Accepted for gateway Phase 5 observability.

Problem/evidence: admission had internal fixed counters and circuit state had
snapshots, but operators could not scrape them or correlate a response with a
structured completion record. Necessity: MUST for the portfolio gateway's
observable failure handling. Alternatives: do nothing/defer; add a third-party
Prometheus client; or expose a small fixed-schema Prometheus text endpoint using
existing counters and snapshots. The standard-library exporter is the smallest
robust choice for this repository and adds no dependency.

`/metrics` exports fixed `route=one|two`, finite outcome and circuit-state labels.
It never labels by path, identity, IP, upstream URL, request ID, error text or
arbitrary status. Request IDs accept 1..64 ASCII alphanumeric/`._-` characters;
invalid/missing values are replaced with 128-bit random hex and returned in
`X-Request-ID`. Request completion logs contain only that ID, route, status and
duration; no URL, query, headers or body.

Timing semantics: `gateway_request_duration_seconds` begins at gateway handler
entry and ends when handler processing returns (streamed response copies are
included). `gateway_upstream_roundtrip_duration_seconds` measures one shared
Transport RoundTrip through response headers; it excludes response body transfer.
`gateway_redis_admission_duration_seconds` measures each configured limiter Check
through return, including client pool and Redis work; identity errors are not
observed as Redis calls, and the histogram remains empty when admission is disabled. Histograms use fixed cumulative
seconds buckets 1ms..500ms plus +Inf. Circuit gauges are local-process snapshots;
transitions are totals, not a fleet-consensus signal. `/metrics` has no auth and
must remain on loopback/private monitoring network until separately protected.

Risks/trade-offs: a hand-written exposition format has less feature surface than
the Prometheus client library; keep it covered by scrape-format tests if labels
or types change. Request completion logs add one structured event per request.
No request path or client-selected dimension can expand metric cardinality.
Verification includes bounded labels/request ID behavior, go test/race/vet, local
benchmark scripts and raw profile artifacts. No pooling optimization follows
from a heap profile alone; profile interpretation and follow-up are recorded in
`docs/benchmarks.md`.
