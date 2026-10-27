# Deployment guide

## Current state

The repository includes a Render Blueprint and reproducible local containers.
No Render/Fly account credentials or deployed service addresses are available
in this workspace. **No hosted service has been created or smoke-tested.** Do
not treat a local Compose response as a public deployment check.

## Provider choice

Provider pricing/limits were checked on 2026-10-28 and can change.

Render is the recommended first deployment for this project. Its Blueprint can
build Dockerfiles, attach managed Key Value, and wire same-region private
services by reference. Key Value runs Valkey 8 and supports Redis-compatible
clients and `redis://`/`rediss://` URLs. Only the Next dashboard is public; the
gateway, simulator upstreams, and Redis stay on the private network. Render
terminates HTTPS at its public web ingress and offers generated service HTTPS.
See [private services](https://render.com/docs/private-services), [private
networking](https://render.com/docs/private-network), [web services](https://render.com/docs/web-services), and [Key Value](https://render.com/docs/key-value).

The checked-in `render.yaml` chooses Oregon; change all service regions together
before first creation because private networking requires same region. The
lowest nominal monthly setup uses a free dashboard, free Key Value, and three
`0.5c-512mb` private services. Render lists that private plan at $7/month each,
so the minimum is about **$21/month**, before workspace fees, bandwidth or tax.
The free web service sleeps after 15 minutes without inbound traffic and can
take about a minute to wake; free Key Value is 25 MB/50 connections, has no
persistence, and can restart. This is a portfolio demo tier, not durable quota
storage. Source: [Render pricing](https://render.com/pricing), [free plan
limitations](https://render.com/docs/free), and [Key Value plans](https://render.com/docs/compute-plans).

For an always-on dashboard and persisted quota state, choose a paid web plan
(currently $7/month for the smallest plan in the pricing table) and 256 MB Key
Value ($10/month), alongside the three $7 private services: about **$38/month**
before workspace fees, bandwidth or tax. Prices and plan limits can change;
confirm them in the Render dashboard before creating resources. Paid Key Value
supports persistence; free Key Value does not. The free web service can make
private-network requests but cannot receive them, so it can still act as the
public dashboard/BFF for this topology.

Fly Machines have lower listed US-region compute prices: shared CPU 1x/256 MB
is $2.19/month and 2x/512 MB is $4.39/month in listed US regions, billed by
the second while running. Rates vary by region and current organization pricing;
storage and egress are separate. Fly has private 6PN networking, but Redis and
public HTTPS ingress would need separate setup/validation. Upstash's Redis free
tier lists 256 MB and 500K commands/month; pay-as-you-go lists $0.20 per 100K
commands, and TLS is always enabled. The gateway issues one Lua EVAL per
admission, so estimate command volume before choosing the free tier. Source:
[Fly pricing](https://fly.io/pricing/), [Upstash Redis pricing](https://upstash.com/pricing/redis), and [Upstash Redis security](https://upstash.com/docs/redis/features/security). Render is selected
for the fewer moving pieces, not because it is the cheapest raw compute option.

Render internal Key Value authentication is off by default. Enable it in the
Key Value Connections settings before sharing the dashboard and verify the
Blueprint `connectionString` includes the generated credential. Keep public
ingress disabled (`ipAllowList: []`).

## Local stack

Use Docker Compose v2 and PowerShell from the repository root. Generate two
different random secrets (at least 32 bytes each); do not reuse production
tokens locally:

```powershell
$env:GATEWAY_DEMO_TOKEN = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:METRICS_BEARER_TOKEN = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:REDIS_PORT = '36380'
docker compose config --quiet
docker compose up --build --wait
```

Open `http://127.0.0.1:3000/`. The dashboard (3000), gateway (8080), and Redis
(6379) are published on loopback only; set `REDIS_PORT` to another free host
port if local Redis is already occupied. Redis data is intentionally ephemeral and never shared with
another project. Check:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/healthz
Invoke-WebRequest http://127.0.0.1:8080/readyz
Invoke-WebRequest http://127.0.0.1:8080/one/health
Invoke-WebRequest http://127.0.0.1:3000/api/healthz
Invoke-WebRequest -Headers @{ Authorization = "Bearer $env:METRICS_BEARER_TOKEN" } http://127.0.0.1:8080/metrics
docker compose ps
docker compose down
```

The protected `/metrics` endpoint is intentionally not proxied through the
public dashboard. `/healthz` reports process liveness; `/readyz` checks Redis
readiness and drain state. Compose stops services with 12 seconds for the
application’s 10-second graceful drain.

### Environment contract

| Variable | Validation and use |
| --- | --- |
| `GATEWAY_DEMO_TOKEN` | Gateway enforces a 32–256 byte bearer token when set; dashboard requires 32–256 bytes. The Render dashboard and gateway must receive the same value. |
| `METRICS_BEARER_TOKEN` | Optional only on a loopback listener; otherwise required, 32–256 bytes, bearer-token-safe characters, and distinct from the demo token. Protects `/metrics`. |
| `UPSTREAM_ONE_HOSTPORT`, `UPSTREAM_TWO_HOSTPORT` | Optional host-and-port values supplied by Render private service references. Scheme, credentials, path, nonnumeric/out-of-range ports are rejected. |
| `REDIS_URL` | Redis URL parsed by the limiter; defaults to local `redis://127.0.0.1:6379/0` only for local runs. Render wires the Key Value private connection string. |
| `GATEWAY_INTERNAL_HOSTPORT` | Dashboard BFF private gateway host/port from Render. Existing `GATEWAY_INTERNAL_URL` remains available for local development. |
| `REDIS_PORT` | Compose-only host port override; default `6379`. |

## Exact hosted deployment checklist

Before the checklist below is used for a real release, follow the
[release rollback and database recovery runbook](operations-runbook.md). It
defines the required compatibility checks and explicitly avoids in-place
database rollback.

1. Connect this repository to Render and create a Blueprint from `render.yaml`.
   Confirm all five service regions agree and review current prices before
   applying it. The Key Value `ipAllowList: []` is intentional: Redis is private.
2. Supply a fresh random 32-byte-or-longer `GATEWAY_DEMO_TOKEN` for both the
   dashboard and gateway prompts. Supply a different random token for
   `METRICS_BEARER_TOKEN` at the gateway prompt. Keep both in the provider secret
   store; never commit them or place them in a URL.
3. Confirm the gateway and both simulator services have no public URL, Redis
   allows only internal connections, and Key Value has `noeviction`. Enable
   internal authentication before sending gateway traffic, then confirm the
   gateway's `REDIS_URL` is the authenticated internal URL. For durable quota
   state, choose the paid 256 MB plan and `journal-snapshot` persistence. Do not
   share the public dashboard URL until this check passes.
4. Wait for successful builds and health checks. Confirm gateway logs show a
   wildcard bind and clean SIGTERM drain. Render’s private-service platform
   health check is TCP; `/readyz` is an app/container check, not a Render HTTP
   probe for that service.
5. Record the actual dashboard HTTPS URL from Render. Then smoke-test only that
   live service: `/api/healthz`, dashboard `/gateway`, `/api/demo/status`, and
   one normal action. Trigger at most one bounded rate burst and one isolated
   circuit failure/recovery through the dashboard controls. Record UTC time,
   URL, response codes, and observed dashboard state.
6. Verify there is no public gateway/metrics URL. If an operator-only internal
   metrics query is needed, run it from a same-region private service with the
   bearer token. Do not put the token in browser JavaScript or CI logs.
7. Set the Render deploy branch and alerts, then verify rollback from the
   previous successful deploy. Keep the verified HTTPS URL here only after those
   checks actually pass.

### Verified URLs and checks

Local Compose was smoke-tested on 2026-10-28. Dashboard: `http://127.0.0.1:3000`;
gateway: `http://127.0.0.1:8080`. Gateway `/healthz`, `/readyz`, `/one/release-smoke`,
and `/two/release-smoke` returned 200; dashboard `/` and `/api/healthz` returned
200. Gateway `/metrics` returned 401 without a token and 200 with the configured
bearer token. The stack was stopped after these checks. These are local URLs,
not hosted deployment evidence.

Hosted verified URLs: none. No provider credentials or deployed address were
available. CI is configured, but no remote CI run was verified from this
workspace. The earlier note that the Compose plugin was missing is superseded:
Compose v5.6.1 was installed in the repo-local tools directory and the stack
passed the checks above.
