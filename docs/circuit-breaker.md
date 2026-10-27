# Local upstream circuit breaker

The gateway owns one circuit per configured origin per process. `/one` and `/two`
share it when scheme, case-normalized hostname and effective port match. DNS
aliases are not resolved to merge circuits. Replicas do not coordinate probes.

Defaults: 5 consecutive observed upstream failures open the circuit for 5 seconds;
one concurrent Half-Open probe; 2 successful probes recover. Configure at startup:

```text
./bin/gateway -circuit-failures 5 -circuit-open 5s -circuit-probes 1 -circuit-recovery 2
```

Policy is per gateway instance and immutable. A success clears the Closed failure
streak; a neutral outcome does not change it. Completion order defines the
streak. Cooldown expires lazily on admission, then Half-Open allows bounded probes.
Any failed probe reopens for another full cooldown. Recovery waits for all admitted
probes to finish and the configured success count. Neutral probes release their
slot without helping recovery. Stale results cannot affect a newer generation.

500..599, transport failures, response read errors and gateway/upstream deadlines
are faults. 400..499, client cancellation, upload read failures and abandoned
successful response bodies are neutral. Other completed responses are successes.
Empty responses complete at headers; streamed bodies hold probe slots until
EOF/Close. An unfinished 5xx closed by the proxy still counts as failure. There
is no automatic retry. Existing request/transport deadlines bound slow probes.

Admission order: validated route, Redis quota, circuit, proxy. Open and exhausted
probe capacity return 503 with no-store and integer Retry-After, before reading
the body or contacting upstream. Redis still runs first and consumes quota; its
deadline contributes to rejection latency. Already-running requests are allowed
to finish. Quota 429 and Redis failure 503 never count as upstream faults.

`Gateway.CircuitStats()` returns fixed route-indexed state, failure/success/neutral
counts, rejections, active probes and transition matrix (Closed/Open/Half-Open).
Routes sharing an origin return its shared snapshot; do not sum those duplicates.
Counters exclude stale completions. The CLI logs snapshots on shutdown. A metrics
endpoint remains future work. Readiness now also follows the Redis dependency
policy documented in [reload/readiness](reload-readiness.md). Reload preserves
circuits for canonical origins retained in the previous snapshot; newly introduced
origins start Closed. Numeric circuit policy remains a startup setting.

Run the bounded, local-only chaos reproduction with Python 3 and an isolated
loopback Redis. It starts its own upstream fixture and gateway; it accepts no
HTTP target or public fault commands. Build first:

```powershell
go build -o bin/ ./cmd/...
docker compose up -d redis
$env:REDIS_TEST_URL = 'redis://127.0.0.1:6379/0'
python scripts/circuit-chaos.py
```

POSIX: `REDIS_TEST_URL=redis://127.0.0.1:6379/0 python3 scripts/circuit-chaos.py`.
Ten sequential requests reproduce 500 -> Open 503 -> slow Half-Open 504 -> Open
503 -> two healthy recovery probes -> Closed. Every HTTP call has an 8s timeout;
startup is bounded. Local process cleanup forcibly terminates the child gateway,
so this script does not establish graceful OS-signal behavior. Timing output is
an incident reproduction, not a benchmark or deployment guarantee.

Design and tests: [ADR-007](decisions/ADR-007-upstream-circuit.md),
[raw transcript](verification/phase-3-circuit.txt), [plan](../PLAN.md).
