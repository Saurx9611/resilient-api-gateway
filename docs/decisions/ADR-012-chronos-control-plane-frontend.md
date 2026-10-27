# ADR-012: Chronos operator dashboard and data contract

- Status: accepted for worker companion Phase 6.
- Problem/evidence: the worker control API is functional but has no operator
  interface. It exposes current queue counts and per-job events, but no global
  recent-event feed or historical throughput/latency series. There is no mutable
  global queue-default policy.
- Necessity: MUST for an operator dashboard that does not fabricate activity;
  SHOULD for persistent queue defaults used by submissions; DEFER custom
  analytics storage, SSE, and multi-tenant UI until workload evidence warrants it.
- Options: keep Swagger-only (do nothing); render decorative/client-generated
  chart samples (rejected as misleading); store duplicate metrics on a timer
  (rejected due duplicate state); derive fixed time buckets and success latency
  from existing PostgreSQL events (chosen); add SSE or a broker (deferred).
- Simplest robust solution: a Next.js App Router/TypeScript client with Tailwind
  and repository-owned shadcn/ui primitives; same-origin bounded Next route
  handlers forward only known Chronos API paths and caller bearer tokens. Add
  read-only event/observability queries to PostgreSQL and a constrained singleton
  queue policy used only as submission defaults. Keep existing API auth/scopes,
  body bounds and database uniqueness authoritative.
- Trade-offs: dashboard reads issue bounded aggregate queries; historical charts
  are event-derived and bounded to the requested 24h/7d interval. Percentiles
  describe completed claims with matching success events, not end-to-end external
  side effects. Browser polling is simpler and observable but has a fixed request
  cadence; SSE is unnecessary for the current small operator workload. Browser
  bearer tokens live only for the current tab session. A same-origin proxy avoids
  CORS and keeps API origin server-side, but does not make XSS harmless.
- Security: never accept arbitrary destination URLs in the frontend proxy; allow
  only the fixed `/v1` endpoint set, cap request bytes, forward only JSON and
  Authorization/Idempotency-Key, and avoid logging credentials. Policies have
  narrow min/max limits and require operator authorization. Destructive actions
  require an explicit reason and confirmation.
- Verification: PostgreSQL API tests for aggregate boundaries, policy validation
  and defaults; frontend lint/type/build; browser journey checks for auth, create,
  cancel, retry/replay, schedule management, filters, settings and responsive
  navigation; keyboard/focus and accessible labels review; screenshots captured
  from the running app/API/Compose stack.
- Revisit if measured polling load is material, query plans show unacceptable
  aggregation cost, policies need fleet-specific overrides, or tenant isolation
  becomes a requirement.
