# Security and recovery runbook

This runbook is an operator procedure, not evidence that a hosted environment
or a restore has been exercised. Keep backup credentials and artifacts outside
the repository and restrict access to the control plane and database.

## Release rollback

1. Stop promotion and record the failing release SHA, UTC time, error rate, and
   affected routes/jobs. Do not run destructive demo fault controls against a
   shared environment.
2. Redeploy the previously successful immutable image digest (or Render's
   previous successful deploy) for the affected service. Do not use a mutable
   `latest` tag. Roll back API, scheduler, worker, and dashboard as a compatible
   release set when their contracts changed together.
3. Check process liveness, database readiness, worker heartbeats, and a bounded
   synthetic read/write job using an idempotency key. Verify errors and queue
   state before reopening ingress.
4. Schema changes are forward-only. Do not restore an old application against
   a schema it cannot read, and do not drop columns/tables to make rollback
   appear successful. Use expand/contract migrations; if a migration is not
   backward-compatible, restore a database snapshot to a separate instance
   and validate the old application against that copy before cutover.
5. Record the deployed image digests, migration version, validation results,
   and any jobs that require operator replay. A replay creates new work and can
   repeat external effects; demonstrate handlers are idempotent before replay.

## PostgreSQL backup and restore

Take backups using the deployment provider's supported encrypted backup/PITR
facility where available. For a manual logical backup, use a dedicated
least-privilege credential and a protected destination:

```sh
umask 077
pg_dump --format=custom --no-owner --file="$BACKUP_DIR/chronos-$(date -u +%Y%m%dT%H%M%SZ).dump" "$DATABASE_URL"
pg_restore --list "$BACKUP_DIR/chronos-<timestamp>.dump" >/dev/null
```

Do not put connection strings in shell history or logs. Copy the encrypted dump
to the approved backup store and record its checksum, database/migration
version, UTC time, and retention expiry. Restore into a **new isolated database**
first; never test restore by overwriting the active database:

```sh
createdb --maintenance-db "$RESTORE_ADMIN_URL" chronos_restore_check
pg_restore --no-owner --dbname "$RESTORE_DATABASE_URL" "$BACKUP_FILE"
```

Validate migration version, job/event counts, representative payload/result
rows, indexes/constraints, and application read-only health against the restored
database. Only an incident commander may approve a cutover. A logical backup is
not point-in-time recovery; data loss can include all writes after its capture.
Record an explicit measured RPO/RTO only after a timed restore drill.

## Readiness and worker recovery

`/healthz` reports that the API process is alive and does not depend on
PostgreSQL. `/readyz` checks schema/database availability and returns 503 while
the control plane cannot safely serve requests. Worker health is determined by
database heartbeat age, not by a container being merely running. On a database
outage, keep ingress closed if readiness remains 503; workers retry transient
connection errors with bounded backoff. On recovery, confirm current worker
heartbeats and lease recovery events. A reclaimed job may repeat an external
side effect if the process performed it before losing its lease/acknowledgment.
Use idempotency at the side-effect boundary.

For a stuck worker, send SIGTERM first and allow the configured Compose
`stop_grace_period` for active bounded handlers to finish. If the process must
be killed, do not manually clear a lease: wait for lease expiry/reclamation and
verify the generation changed. Never treat exactly-once side effects as a
database guarantee.

## Public demo abuse controls

Keep Redis, gateway, upstreams, API, and worker metrics on private networks;
only the dashboard/BFF should receive public ingress. The BFF keeps the
high-entropy gateway bearer secret server-side and allows only fixed simulator
actions. **The dashboard demo is intentionally callable without user login**:
the BFF same-origin check is a CSRF defense, not abuse authentication. This is
acceptable only while actions remain bounded and affect isolated simulators;
normal actions have a 3-second cooldown, burst/circuit actions a one-minute
cooldown, and rate bursts are capped at 64 requests per gateway process. These
limits are process-local, not fleet-wide. Before horizontal scale or a shared
public demo with meaningful cost/side effects, add and validate an ingress-wide
request and concurrency budget using trusted ingress identity. Monitor 429/503
and resource use, and disable demo controls when unattended. Never expose the
fixed local Compose credentials in a public environment.

## Incident evidence

Capture structured event names, request IDs, image digest, migration version,
worker heartbeat/generation changes, and relevant PostgreSQL logs. Do not copy
authorization headers, tokens, raw job payloads, DSNs, or unredacted exception
messages into tickets. Follow the failure mode and recovery evidence in
`worker-failure-modes.md` and update it after each verified recovery drill.
