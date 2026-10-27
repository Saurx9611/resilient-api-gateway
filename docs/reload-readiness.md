# Route snapshots and Redis readiness

The gateway validates both routes and constructs their proxies privately, then
publishes one immutable snapshot with an atomic pointer swap. Each request selects
one snapshot before Redis admission and keeps its target, outage policy, circuit
and proxy until completion. Reload neither cancels old requests nor waits for
their streams. Old snapshots become collectible after references disappear.

One shared HTTP transport and Redis pool survive reload. Redis namespace, route
IDs (`one`/`two`), limit/window/identity cap, breaker policy and transport/request
timeouts are startup settings. They are deliberately absent from reload documents:
changing quota parameters while older requests/replicas still use them would
conflict with the distributed limiter's policy signature. Changing targets does
not reset quota. Breakers survive for canonical origins retained in the immediately
preceding snapshot, even when moved between routes. New origins start Closed;
removing then re-adding an origin resets its circuit. State is per process.

Use the local Go `Reload(Config)`/`ReloadJSON` methods or file polling. There is
no public/admin HTTP reload endpoint. `CurrentConfig` returns a value copy;
`ReloadStats` exposes version, applied replacements and rejected candidates/read
errors. Initial version is 1; no-op reloads do not advance it. Counters and versions
are local and reset on restart.

## Local file workflow

Build the binaries, start Redis and both upstreams using the README, then:

```text
./bin/gateway -config config/routes.example.json -reload-interval 1s
```

The file controls both targets, both outage modes and readiness. It overrides
`-upstream-one/two` and `-redis-failure-one/two`; other flags remain startup policy.
Polling is enabled only with `-config`, default interval 1s (allowed 100ms..1m).
Example JSON (entries correspond to `/one`, `/two`):

```json
{
  "targets": ["http://127.0.0.1:9001", "http://127.0.0.1:9002"],
  "redis_failure": ["closed", "open"],
  "readiness": "required"
}
```

Modify a private copy, then atomically replace the configured file using an
editor's atomic-save facility or `os.replace` in a local script. For example,
changing the first target to port 9002 sends future `/one` requests there while
older streams finish on port 9001. Both modes default to closed in the supplied
example; `open` is an explicit availability trade-off for non-sensitive routes.
Operators must protect file/directory write permissions: these targets are the
allowlist, so access to this file is privileged configuration authority.

Only regular files up to 64KiB are read. The parser rejects duplicate/unknown
fields, invalid modes, trailing documents and arrays with anything but two entries.
Targets must be HTTP(S) origins without credentials, path (except `/`), query or
fragment and must have valid ports. No target comes from request data. Validation
does not prove DNS/upstream availability or deployment egress isolation.

Invalid, partially written, oversized or missing files retain the last good
in-memory version, log a redacted rejection, and do not affect its readiness.
Restoring valid content applies automatically. Identical content and consecutive
read errors are deduplicated. No file content, target URL or parse diagnostic is
logged. A malformed initial file fails startup; last-good state is not persisted
separately, so restarting against a bad file also fails.

## Redis outage and readiness contract

Every routed request independently uses the outage mode in its selected snapshot:

| Mode | Successful Redis denial | Redis/script failure |
|---|---|---|
| `closed` | 429 with quota/retry headers | 503, no quota values |
| `open` | 429 with quota/retry headers | Forward without quota values; record fallback |

Malformed identity never fails open; cancellation never triggers forwarding.
There is no in-memory replacement advertised as globally consistent. A timed-out
script might already have consumed quota. Restored Redis resumes the same keys.

`/healthz` reports local liveness. `/readyz` reports serving/not-draining plus:

| Readiness mode | Redis required? |
|---|---|
| `required` (default, including omitted JSON readiness) | Always, when a limiter is configured |
| `policy` | Unless **both** routes explicitly fail open |

When required, each readiness request performs bounded PING using the limiter's
shared pool and configured `-redis-timeout` (100ms default), within a 1s gateway
health ceiling and the caller's deadline. It consumes no quota/identity slot and
does not run on `/healthz`. Failure gives uncached 503; recovery gives 200 on the
next probe without restart. PING proves reachability/authentication, not Lua ACLs,
memory capacity, matching rate policies or upstream health. A library Admission
must supply `Options.RedisHealth` when readiness requires Redis; nil is unhealthy.
Library mode with no limiter has no Redis dependency.

Data endpoints are not globally gated by Redis readiness: fail-open traffic can
still proceed while required readiness is 503. An ingress may remove the whole
instance based on that 503, which is why policy mode is explicit. Shutdown/draining
always overrides readiness, even if a successful Redis probe overlaps draining.
Readiness reflects the snapshot selected by that probe, not a reservation for a
later request.

Run `go test -v -count=1 ./...` and `go test -race -count=1 ./...` with
`REDIS_TEST_URL` pointing to isolated local Redis. Tests hold 16 long streams
through repeated swaps, reject invalid configuration, retain old admission policy
while blocked in Redis, preserve circuits/quota, and exercise native file polling.
A connection gate cuts only the integration test client's real Redis connections,
then restores them; other packages' Redis traffic is unaffected. Raw outcomes:
[Phase 4 verification](verification/phase-4-reload.txt), decision:
[ADR-008](decisions/ADR-008-snapshot-reload-readiness.md).
