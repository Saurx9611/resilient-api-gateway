# ADR-014: Container and hosting shape

**Status:** Accepted for Phase 9 deployment work. No service has been deployed.

## Problem and evidence

The repository has a non-root standalone Next image and worker images, but no
gateway image. `compose.yaml` compiled Go from bind-mounted source, omitted the
dashboard and health checks, and published Redis. The Go server exposes
`/metrics`; that endpoint had no authentication. A remote deployment is not
available from this workspace because no provider credentials or service URLs
are configured.

## Alternatives and decision

- Leave Docker/hosting as a manual operator task: defer; this would not make the
  local build reproducible.
- Combine UI, gateway and simulators into one process/image: reject; this changes
  process boundaries and makes the upstream failure demo less representative.
- Build multi-stage Go targets, retain separate services in Compose, protect
  metrics with a deployment token, and describe a Render Blueprint: **MUST**.

Render is selected as the simplest viable hosted topology because its Blueprint
can build the existing Dockerfiles, connect same-region private services and
managed Redis-compatible Key Value, expose only the UI, and provide HTTPS for the
public web service. Fly Machines offer lower listed compute prices, but this
repository would need more manual wiring for HTTPS ingress and a compatible
external Redis service. See `docs/deployment.md` for dated plan details and
limitations.

## Simplest implementation and trade-offs

- Separate Go build stages produce only the gateway or simulator binary needed
  by that image; Alpine runtime images run as UID/GID 10001. The dashboard uses
  its existing standalone multi-stage Next.js image and non-root UID 10001.
- Compose uses ephemeral Redis and fixed in-repository simulators; it publishes
  the gateway and UI only on loopback. Tokens are required through environment
  variables and never have committed defaults.
- `/metrics` accepts a 32–256 byte bearer token. A non-loopback gateway listener
  refuses startup without it. Health and readiness stay probe-accessible; hosted
  gateway metrics remain unreachable because the gateway is a private service.
- Render’s demo preset can use free web/Key Value services, but free web sleeps
  after inactivity and free Key Value is not durable. Three private services
  (gateway plus two simulators) use paid compute. The more reliable preset uses
  paid web and persistent Key Value too.

The two Go Dockerfiles duplicate a short runtime recipe so each runtime carries
only one executable and Render can build them directly without custom build
targets. Their measured Docker sizes and configured healthchecks are in
`docs/verification/phase-8-deployment.txt`.

## Risks and verification

Render private services only receive TCP platform checks; application readiness
is available at `/readyz` and container health checks, but is not represented as
a Render HTTP health probe. Redis Key Value `ipAllowList: []` disables public
ingress. Internal authentication should be enabled if offered for the selected
instance/plan. No public endpoints or service prices are frozen by this ADR.

Required local checks: Go tests/race/vet/build, frontend lint/type/build,
`docker compose config`, image builds, Compose health and `/one`/metrics/web
smoke. Hosted smoke remains pending a real deployment and actual URLs.
