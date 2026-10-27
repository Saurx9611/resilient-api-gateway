# ADR-016: Security and operational readiness gates

Status: Accepted for the requested Phase 8 security/operations review.

## Necessity decision

Problem/evidence: runtime controls and tests already cover fixed allowlisted
routes/handlers, token scopes, bounded API requests and principal rate limits,
timeouts, private metrics, worker fencing/retry/shutdown, container healthchecks
for gateway/upstreams/dashboard/PostgreSQL, and DB-backed API readiness. API
Compose health checking and database-outage readiness assertions are gaps. The
CI workflows currently run unit/integration/build gates, but there is no secret
scan, `.gitignore` assertion, automated dependency/container vulnerability gate,
or explicit repeatable migration check. Two workflows also run the same worker
integration suite. Deployment instructions ask an operator to verify rollback but
do not give a recovery sequence. Doing nothing preserves those gaps. Closing them
with repository-native checks and an operations runbook is MUST.

## Choices

* Keep GitHub Actions and existing service/test topologies. Pin action references
  to commit SHAs, set least-privilege workflow permissions, and retain separate
  Go/Redis, Python/PostgreSQL, and frontend/container validation.
* Add local CLI-based source/dependency/image scans (Gitleaks, govulncheck,
  pip-audit, npm audit, Trivy) to CI; pin scanner versions and make HIGH/CRITICAL
  image findings a blocking gate. Do not upload source or artifacts to a new
  hosted scanning service. Dependency databases still come from their respective
  public advisory feeds during CI.
* Add a standard-library `.gitignore` verifier for the required local secret,
  Python, Node, and Go artifact patterns. Do not create probe files or add broad
  ignore rules for source formats.
* Remove only the redundant worker test workflow after confirming its checks are
  represented by the maintained PostgreSQL-backed worker workflow.
* Keep runtime authentication, request limits, timeouts, shutdown, and worker
  execution semantics. Add API readiness health check, PostgreSQL concurrency
  and outage readiness tests, and redaction assertions at actual boundaries; do
  not introduce a proxy, queue abstraction, or security middleware without a
  demonstrated defect.
* Document rollback and database recovery using the existing deployment/local
  topologies. Do not claim schema rollback where migrations are forward-only; use
  a tested backup restore to a separate target before any production cutover.

## Deadlock/race audit

Worker transactions follow job-row then job-event ordering for claim, completion,
failure, retry, replay, and cancellation. Scheduler transactions lock schedule
rows in `(next_occurrence_at,schedule_id)` order, then create a job, its occurrence,
and audit rows before advancing the schedule. No audited worker path locks a job
and then a schedule, so there is no reverse edge in the inspected wait-for graph.
Claims/recovery use `FOR UPDATE SKIP LOCKED`; migrations take one transaction
advisory lock; API/worker database statements have finite lock and statement
timeouts. This static review is not a proof against future SQL or external DB
triggers. Add a simultaneous cancellation/ack integration case and retain the
existing multi-worker claim/scheduler/idempotency races as regression coverage.

## Trade-offs and rejected complexity

Local CLI scanners add minutes and depend on fresh public advisory data, but avoid
source disclosure to a new service. Pinning action commits improves integrity but
requires deliberate update PRs; retain Dependabot compatibility rather than
mutable action tags. Do not add a new image-signing service, SBOM platform, SIEM,
WAF, Terraform module, distributed demo limiter, migration framework, or database
proxy in this phase. Public demo abuse resistance remains a bounded demo action
cap plus bearer-protected mutations, loopback/private origin topology, and an
operator ingress checklist; process-local cooldowns do not become a fleet-wide
guarantee.

## Verification plan

Run GitHub workflow syntax checks, `.gitignore` verification, gitleaks, Go module
verification and govulncheck, pip-audit, npm audit, image scans, go formatting/
unit/race/vet/build with real Redis, Python unit and real-PostgreSQL process/API
suite, migration twice/version assertion, Compose health/auth smoke, plus a
concurrent cancellation/ack test. Record scanner findings and distinguish local
verification from hosted Actions results. A finding blocks deploy only when it
affects a shipped component at an applicable severity; otherwise document a
specific, expiring exception rather than suppressing broad paths.
