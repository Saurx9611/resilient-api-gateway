# Phase 5 local measurement record

## Reproduction

Use a dedicated local Redis with no concurrent users. Build the binaries, then
run the bounded harness (default 300 request workload, max 10,000 and max 64
workers). It refuses Redis endpoints that do not resolve to loopback:

```powershell
go build -o bin/ ./cmd/...
$env:REDIS_TEST_URL = 'redis://127.0.0.1:6379/0'
python bench/run.py --requests 100 --concurrency 2 --output docs/verification/phase-5-benchmark.json
```

POSIX uses `REDIS_TEST_URL=redis://127.0.0.1:6379/0 python3 bench/run.py ...`.
The harness starts two isolated Python HTTP/1.1 upstream fixtures and local
gateway processes. It runs direct and proxied successful 1KiB response scenarios,
primes a one-request quota before measuring 429s, and starts a separate circuit
process with one controlled 500 trigger before measuring open-state 503s. The
trigger/warmup are excluded from measured counts. Per scenario output includes
wall duration, achieved requests/s, status counts, unexpected-status percentage,
and nearest-index p50/p95/p99 sample latency. Every request has a 4s timeout and
HTTP/1.1 connections are reused per worker thread.

## Captured run (2026-10-27)

Raw JSON: [`phase-5-benchmark.json`](verification/phase-5-benchmark.json).
Host: Windows 11 10.0.26200, amd64, 12 logical CPUs; benchmark test reports AMD
Ryzen 5 5600H. Go 1.27.3. RAM was unavailable from the restricted host inventory.
All traffic was cleartext loopback on one host; Redis was reachable at loopback.
Payload was 1024 bytes, concurrency 2, one unmeasured direct and proxied warmup.
The raw report holds exact duration and percentile values. Summary:

| Workload | Requests | Duration | Statuses | Errors | p50 / p95 / p99 (ms) |
|---|---:|---:|---|---:|---:|
| Direct upstream | 100 | 0.147485 s | 100×200 | 0% | 1.342 / 13.998 / 22.690 |
| Through gateway | 100 | 0.218732 s | 100×200 | 0% | 3.081 / 10.890 / 20.186 |
| Rate-limited | 40 | 0.071720 s | 40×429 | 0% | 2.212 / 6.048 / 19.058 |
| Circuit open | 40 | 0.098473 s | 40×503 | 0% | 2.151 / 22.806 / 25.527 |

The two successful scenarios are broadly comparable in setup, but this is a
single short, noisy run: separate fixture scheduling and Python client overhead
are material. Percentiles are not subtracted to claim gateway overhead. The
high p95/p99 variation on short fail-fast batches is retained as observed. No
target, production rate, or latency guarantee is inferred.

## Go allocation and heap baseline

Reproduce with:

```text
go test ./bench -run '^$' -bench BenchmarkGatewayRequest -benchmem -benchtime=3s -memprofile docs/verification/phase-5-heap.pprof
go tool pprof -top -alloc_space docs/verification/phase-5-heap.pprof
```

Captured benchmark output is [`phase-5-allocations.txt`](verification/phase-5-allocations.txt)
and the raw heap profile is [`phase-5-heap.pprof`](verification/phase-5-heap.pprof).
Observed: 257,164 ns/op, 47,977 B/op, 187 allocs/op on the same host. This is an
in-process `httptest` stack including client, server, proxy and 1-byte fixture,
not an isolated production request cost. Allocation-space profile top sites:
`ReverseProxy.copyBuffer` 66.84%, `textproto.readMIMEHeader` 4.02%, and
`Request.WithContext` 2.19%; cumulative allocation-site values are in
[`phase-5-heap-top.txt`](verification/phase-5-heap-top.txt). The profile includes
the test server/client and repeats payload copying; it does not measure retained
heap pressure or attribute costs solely to the gateway. No custom pool is
proposed. The existing native proxy buffer behavior remains until a representative
CPU/heap profile demonstrates a specific reusable buffer bottleneck.

## Limitations

This harness is for local reproduction only. It has no TLS, cross-host network,
long warmup, CPU pinning, stable isolated host, GC pause report, Redis RTT
separation, or repeated-run confidence interval. Its Python threaded upstream
and client can dominate or perturb results. Circuit-open and 429 scenarios
measure end-to-end admission/handler response, not the gateway's raw internal
service time. A failed exploratory run caused route origins to share one circuit
and is retained as [`phase-5-benchmark-invalid-harness.json`](verification/phase-5-benchmark-invalid-harness.json);
it is explicitly excluded from the table. Production-like claims require a
controlled repeated experiment with equivalent network placement and a dedicated
upstream/Redis, plus CPU/GC and error accounting.
