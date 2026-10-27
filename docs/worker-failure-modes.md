# Worker failure-mode matrix

This matrix records the intended recovery and the latest observed verification.
Process crash tests use isolated Linux worker processes and real PostgreSQL from
the Compose test profile. The database-outage scenario uses only the demo
Compose project's PostgreSQL container.

| Trigger | Expected behavior | Test / observation | Observed result |
|---|---|---|---|
| Worker killed after claim, before handler starts | Leave lease; after expiry another worker claims a higher generation | `test_worker_killed_after_claim_before_handler_start_is_reclaimed` | PASS; generation 1 reclaimed as generation 2 |
| Worker killed halfway through handler execution | Child and worker die; expired lease is reclaimed, attempts increment | `test_sigkill_expires_lease_then_another_process_recovers_job` | PASS; replacement succeeded on attempt 2 |
| Worker killed after notification effect, before acknowledgment | Handler may run again; stable effect idempotency key deduplicates ledger row | `test_worker_killed_after_handler_work_before_ack_repeats_idempotent_simulation` | PASS; 2 handler attempts, one effect row, applied then deduplicated events |
| Worker paused beyond lease expiry | Replacement increments generation; resumed worker cannot renew/complete stale generation | `test_paused_worker_cannot_ack_after_lease_expiry_and_replacement_claim` | PASS; replacement generation succeeded; old completion fenced |
| Two workers race after expiry | `SKIP LOCKED` gives one new valid owner and one new generation | `test_claim_after_expiry_is_competed_and_only_one_new_generation_wins` | PASS; exactly one of two contenders claimed the new generation |
| Old worker reports success after replacement claim | Fenced update returns false; replacement remains owner | `test_stale_lease_token_cannot_complete_after_reclaim` | PASS; stale success/renew returned false |
| Repeated retryable handler failures reach configured attempt cap | Capped exponential hash-jitter delay between attempts; dead state and abandonment event at cap | `test_repeated_failures_dead_letter_then_explicit_retry_and_replay`, `test_retry_backoff_and_final_attempt_abandonment` | PASS; attempts capped, dead state reached, explicit retry and linked replay recorded |
| Duplicate submission uses same idempotency key and same content | Return original job ID and append deduplication event; different content is rejected | `test_idempotency_key_deduplicates_and_rejects_changed_contents` | PASS; same ID returned, changed payload rejected |
| Temporary PostgreSQL outage interrupts active work | Stop child when lease cannot be renewed; reconnect; expiry/reclaim and later attempt can finish | `test_live_database_disconnect_stops_child_and_worker_recovers`; Compose PostgreSQL stop/start | PASS; job recovered at attempt 2 after PostgreSQL returned healthy |
| Job exceeds its allowed runtime | Kill handler child and record timeout; retry or dead-letter by max attempts | `test_job_specific_runtime_limit_is_enforced`, `test_handler_timeout_is_killed_and_recorded` | PASS; child killed and timed-out attempt dead-lettered |

All worker runtime state transitions are recorded in `worker_job_events` with
attempt and generation. Reasons are bounded codes/details; payloads, credentials,
and traceback text are excluded. Worker heartbeat staleness is measured from the
database timestamp and configurable in the `workers` operator command.

At-least-once handler execution remains possible. The simulated notification
ledger demonstrates idempotent deduplication in the same PostgreSQL authority;
it does not establish exactly-once delivery to an external provider.
