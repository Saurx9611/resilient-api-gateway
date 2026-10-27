# ADR-004: Phase 1 HTTP, trust and lifecycle contract

Status: Accepted; verification recorded in PLAN.md after implementation.

Problem/evidence: Phase 0 only prints a message. There is no routing, HTTP trust
boundary or lifecycle. Phase 1 requires two controlled targets and streaming
failure handling; the existing ADR-001 selects ReverseProxy.Rewrite.

Options: do nothing (no proxy); custom HTTP/routing framework; standard-library
proxy with a fixed two-route table. Necessity: MUST implement the last option.
Use `/one` and `/two` with segment-boundary matching and prefix removal. Targets
are operator-supplied origin URLs, HTTP(S) only, no credentials/path/query/fragment.
Construction parses private copies and builds proxies once; no mutation API or
reload. Request Host, absolute URLs, queries and headers cannot select a target.
Reject non-origin-form targets, dot segments, repeated separators, backslashes,
encoded separators and encoded route-prefix spellings instead of normalizing.
Preserve valid suffix escaping and raw query bytes; reject queries that Go cannot
parse (including unescaped semicolons). Forward GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS;
reject CONNECT/TRACE/unknown methods and protocol upgrades. Preserve upstream
status/headers/body and response trailers via ReverseProxy, not redirects or status translation.

Compatibility review (SHOULD): ReverseProxy calls Request.Clone before upload EOF;
Clone copies the trailer map while incoming trailer values are still unavailable.
Options are to silently lose values, add a trailer-synchronizing body wrapper, or
reject declared request trailers. Choose explicit 501 before body reads for this phase;
test that behavior. A wrapper and trailer-field trust policy need their own later
use case. Response trailers remain supported and tested.

Trust: no forwarding headers are trusted, even on loopback. Remove Forwarded,
all X-Forwarded-* and X-Real-IP, then SetXForwarded from immediate peer, incoming
Host and actual TLS. X-Forwarded-Host remains client-controlled informational
metadata, never authorization or routing input. Upstream Host is its configured
authority. Behind an ingress, identity/proto describes that ingress until a later
explicit trusted-ingress policy. Ignore environment HTTP proxies and leave TLS
certificate verification enabled. DNS/network egress isolation remains deployment
work; operator URLs are privileged allowlist entries, not a general SSRF filter.

Resource policy: one HTTP/1.1 Transport, dial/TLS 3s, response headers 5s,
expect-continue 1s, idle 90s, 128 idle total, 16 idle/64 total connections per
origin, 32KiB response headers. No automatic decompression. A 30s request context
bounds queueing/upload/response lifetime. Server header/read/write/idle limits
are 5s/30s/35s/60s and 32KiB headers. These are conservative local defaults, not
measured optimums. Use default standard-library copy buffers; no custom pools.
No application retries; Transport's limited replay of eligible requests on stale
connections remains standard behavior.

Enable HTTP/1 full duplex for accepted requests so an early upstream response does
not require draining the incoming body. Reject routes/methods/inputs before body
reads and close rejected connections to avoid net/http's implicit body draining.
net/http, OS and intermediaries may already buffer bytes; this is not a promise of
zero buffering. Flush proxy response writes immediately. No whole-body buffers.
Streams end at the request deadline; slow-client writes also have a server deadline.
Before response headers, transport failures map to generic 502 and timeouts to
504; client cancellation writes nothing. After headers, a stream failure aborts
the connection because a replacement status is impossible. Redact error details
from structured failure logs; defer full request telemetry to Phase 5.

Lifecycle: loopback default listener; GET/HEAD `/healthz` is process liveness,
`/readyz` is serving/not-draining readiness, not an upstream health probe. Readiness
is false before Serve and during shutdown. Signal cancellation stops admission,
calls Server.Shutdown with a separate 10s context, then cancels active requests and
closes connections if draining expires. Do not bind request BaseContext to the
signal context: that would cancel in-flight requests before they can drain.

Local substitutes: a minimal upstream simulator, run twice, plus Compose using the
same binaries. MUST provide these now that upstream processes are introduced;
DEFER Redis, UI, breakers, reload, telemetry, load generators and public deployment.
The Phase 0 print-and-exit command is intentionally replaced by a serving command;
CI smoke must use `-h` instead of waiting for it to exit.

Trade-offs: strict path/query rules reject some valid but ambiguous client inputs;
origin-only targets forgo base-path composition; finite stream duration excludes
indefinite SSE; no body-size/global inbound concurrency budget yet. A connection
budget bounds upstream connections, not all accepted clients or waiting goroutines.
These constraints are documented rather than claimed as complete hardening.

Verification: httptest forwarding/path/query/method/header/trailer tests, two real
local upstreams, synchronized incremental upload/response and early-response tests,
cancel/disconnect/timeout/TLS/connection-budget tests, and shutdown drain/deadline
tests under race detection. Run formatting, unit/race, vet, build and process smoke;
record Compose as skipped if Docker remains absent. Revisit limits only with workload
evidence. Standard semantics: https://pkg.go.dev/net/http/httputil#ReverseProxy and
https://pkg.go.dev/net/http#Server.Shutdown.
