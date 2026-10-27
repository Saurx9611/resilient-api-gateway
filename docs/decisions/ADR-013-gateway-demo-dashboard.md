# ADR-013: Gateway dashboard and bounded demo controls

Status: Accepted for the requested dashboard phase (Prompt 7; currently listed
as Phase 8 in the repository plan). The existing `web/` Next.js scaffold is used;
no frontend framework or charting dependency is added. The unrelated worker UI
contract in ADR-012 remains separate.

Problem/evidence: gateway Phase 5 provides real bounded Prometheus counters,
latency histograms, readiness and process-local circuit snapshots, but no operator
view or safe demonstration actions. The existing upstream allowlist must not be
altered for a browser demo. Necessity: MUST for an honest dashboard and bounded
demonstration. Alternatives: do nothing; add a charting framework; create a
public arbitrary proxy/fault endpoint; or reuse the installed Next app with fixed
same-origin server routes and a small authenticated Go demo API. Choose the latter.

Next server routes can call only fixed Go endpoints and use a configured internal
gateway origin plus `GATEWAY_DEMO_TOKEN`. The token stays server-side and is
required by the gateway for both status and actions. Browser POSTs require a
same-origin Origin and actions have fixed names, no request body, per-process
cooldowns, one in-flight operation per gateway process, and hard request caps.
Rate-limit demonstration sends
at most 64 GETs through the real `/one` path using a dedicated synthetic demo
identity; it is disabled when admission is absent or the configured limit exceeds
that cap. Normal action sends one fixed GET through `/one`. No URL, method, header,
route, policy or failure duration comes from the browser. The circuit scenario
uses the existing breaker FSM with an isolated synthetic upstream outcome sequence;
it never touches configured production upstreams or route breakers and the UI
labels it as a simulation.

The UI polls a token-protected JSON snapshot derived directly from current
gateway request, upstream, Redis admission and circuit counters/timings plus
readiness state. It labels process-lifetime counters and sampled time, uses empty
and error states rather than invented values, and labels percentiles as bucket
upper-bound estimates. Reads are independent atomics, not a transactional snapshot.
Controls are unavailable when the server token is not configured. The Go
`/demo/*` endpoints and `/metrics` remain private deployment surfaces; the Next
same-origin BFF is the intentional public view/control surface and has no
client-controlled destination.

Trade-offs: the rate-limit scenario can consume demo quota and produces at most
64 real requests per invocation; it is cooled down per gateway process and uses a reserved synthetic peer
identity to avoid consuming a user's normal key. The circuit scenario demonstrates
the actual FSM but is not an upstream/network outage test. A public deployment
still needs ingress limits/authentication appropriate to its audience and a
private service network between the web tier and Go gateway. Cooldowns are local
to a gateway process; a multi-replica public deployment must also enforce a
shared or ingress-level action rate limit.

Verification: Go tests cover token enforcement, fixed action routing, cooldowns,
request caps, quota behavior, isolated circuit transitions and telemetry overflow.
Next scoped lint/type/build and HTTP smoke checks cover the BFF's missing-backend
error, cross-origin rejection, body rejection and same-origin forwarding. Responsive
breakpoints and keyboard focus styles are present in the CSS, but visual browser
verification could not be completed because the available CUA browser surface
could not identify a current Chrome URL. Evidence and limitations are in the
Phase 7/Prompt 7 verification transcript; no fake status or performance result.
