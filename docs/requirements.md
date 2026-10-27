# Smallest useful production-minded v1

A single Go HTTP gateway process, runnable twice against one Redis, routing to
two controlled upstreams. It must preserve HTTP streaming/cancellation, admit
requests before application body reads, enforce documented approximate shared
limits, isolate failing upstreams, reload safe configuration and expose truthful
operational signals. Local reproducibility and negative tests precede deployment.
This is not a general-purpose production gateway or a production-readiness claim.
TLS may terminate at a trusted ingress; forwarding trust must be configured.

Initial supported scope: HTTP/1.1 request/response streaming to explicit HTTP(S)
targets, deterministic method/path routes, local operator-controlled config and
one Redis authority shared by replicas. HTTP/2, protocol upgrades/WebSockets,
Redis Cluster, gateway authentication/OAuth and fleet-coordinated circuits are
deferred until separately specified and tested. Do not advertise them as supported
merely because a library can negotiate a protocol. Public access control, where
needed, belongs at a configured trusted ingress until a later explicit decision.

Core gateway v1 is Phases 1–7 with local Compose when services are introduced.
The independent Python worker runtime is a separately requested companion
subsystem in Phase 3. Dashboard and hosted demo (Phases 8–9) are portfolio
packaging, not core gateway acceptance gates.
No automatic retries, client-selected targets, public admin mutation, custom
pooling, service discovery, Kubernetes or gRPC. No throughput/latency release gate
without measured evidence; stretch targets remain hypotheses.

## Requirement-to-test matrix

Rows identify phase ownership; completion status cites implementation and observed evidence. Phase numbers identify
ownership; implemented test names are listed below. Completion requires
recorded commands and results, not merely a checkbox or a test file.

| ID / phase | Requirement | Acceptance test / evidence | Current status |
|---|---|---|---|
| F0 / 0 | Buildable honest scaffold, output failures reported | Historical TestRun/TestRunReportsOutputFailure transcript in phase-0.txt; replaced by TestHelp/TestInvalidStartup for serving CLI | Historical Phase 0 evidence retained |
| H1 / 1 | Two deterministic routes; fixed targets | TestForwardingAndStaticRoutes, TestRouteBoundariesAndMalformedInputs, TestUpstreamStatusAndRedirectPreserved | Implemented; Phase 1 transcript |
| H2 / 1,7 | Header trust, no client-selected targets | TestForwardingTrustAndHopHeaders, TestActualTLSForwardingProto, TestRejectsUpgradeAndAbsoluteTarget, TestRejectsInvalidOperatorTargets; path cases in H1 | Phase 1 cases implemented; ingress/DNS/smuggling hardening remains Phase 7 |
| H3 / 1 | Streaming and cancellation | TestStreamingUpload, TestStreamingResponse, TestEarlyResponseWithoutDrainingUpload, TestRejectsBeforeApplicationBodyRead, TestRejectedUploadRespondsWithoutWaitingForBody, TestClientCancellationReachesUpstream; TestRequestTrailersExplicitlyUnsupported | Implemented with documented finite streams/request-trailer restriction |
| H4 / 1,7 | Bounded transport, useful errors | TestUpstreamFailureMapping, TestStreamDisconnectAbortsResponse, TestResponseDeadlineAfterHeadersAbortsStream, TestSharedConnectionBudgetAndReuse, TestUpstreamResponseHeaderBudget, TestUpstreamTLSVerification | Phase 1 implemented; slow-client/dial/TLS-stall adversarial tests remain Phase 7 |
| L1 / 2 | Atomic shared approximate limit | TestRedisTwoGatewayProcesses, TestRedisServerTimeContentionAndCapacity, TestRedisBoundaries | Passed against Redis 8.10.3, including race run; phase-2-tests.txt / phase-2-race.txt |
| L2 / 2 | Expiry, reset, trustworthy identities | TestRedisBoundaries, TestRedisExpiryAndCapacityRecovery, TestPeerIdentity; forwarding spoof in process test | Passed; server time/TTL, controlled edges, identity cap and recovery |
| L3 / 2,current gateway Phase 4 | 429 headers and outage policies | TestAdmissionHeadersAndNoBodyRead, TestAdmissionOutagePoliciesAndHooks, TestRealRedisConnectionLossPolicies, TestRedisLatencyBudget, TestCanceledAdmissionCannotFailOpen, TestRealRedisOutageReloadReadinessAndRecovery; real container pause/resume smoke | Passed; phase-2-outage.txt and phase-4-reload.txt include outage/readiness/restoration evidence |
| C1 / current circuit Phase 3 | Per-upstream local FSM | TestTransitionsAndRecovery, TestHalfOpenFailureAndStaleProbes, TestPoliciesAndInstances, TestCircuitOutcomeClassification, TestCircuitHTTPTransitionsAndIsolation | Implemented; unit/race transcript phase-3-circuit.txt |
| C2 / current circuit Phase 3 | Bounded probes and fail-fast 503 | TestSimultaneousProbeAdmission, TestCircuitConcurrentHTTPProbes, TestCircuitStreamedFailureKeepsProbeSlot, TestCircuitSharedOriginAndTransportFailure, TestCircuitClientCancellationNeutral; scripts/circuit-chaos.py | Implemented; bounded local chaos and unit/race evidence phase-3-circuit.txt |
| R1 / current gateway Phase 4 | Atomic validated reload | TestReloadDuringConcurrentLongStreams, TestReloadRetainsSelectedAdmissionPolicy, TestReloadKeepsOldTargetBeforeRedisCompletes, TestReloadPreservesCircuitsForRetainedOrigins, TestConcurrentReloadWritersAndNoop, TestConfigDocumentValidationAndLastGood, TestWatchConfigInvalidMissingAndRestoration, TestConfigCLIProcessReloadAndRestoration | Implemented; phase-4-reload.txt unit/race and native file process evidence |
| O1 / 1,current gateway Phase 4 | Shutdown and health/readiness distinction | TestHealthReadinessAndGracefulDrain, TestShutdownDeadlineCancelsActiveUpstream, TestRedisReadinessModesAndRestoration, TestReadinessHealthDeadlineAndMissingProbe, TestDrainWinsOverInProgressReadinessProbe, TestRealRedisOutageReloadReadinessAndRecovery | Implemented; Redis PING readiness is bounded and does not consume quota; phase-4-reload.txt |
| O2 / 6 | Truthful bounded observability | `TestMetricsAndRequestID`, `TestRequestIDSanitizesClientValue`, admission/circuit tests; fixed route/outcome/state labels, no paths or IDs | Implemented; ADR-010 / Phase 5 artifacts |
| S1 / 7 | Resource and control boundaries | `TestPostIsNotRetriedAfterUpstreamDisconnect`, malformed/no-body-read, timeout, truncation, Redis-failure, drain, and bounded-cardinality negatives | Security review evidence in `phase-6-security.txt`; global inbound concurrency/body cap and public auth remain open |
| P1 / 6 | Reproducible performance evidence | Matched direct/proxy runs, raw output + versions/hardware/percentiles/errors/GC; no fabricated target | Implemented as bounded local-only Phase 5 experiment; no GC/production claim |
| D1 / dependency introduction | Local dependencies reproducible | Two simulator binaries + gateway process smoke; development Compose includes Redis | Real Redis container and Compose config validated in Phase 2; full Compose stack execution pending |
| D2 / 8,9 | Safe optional public demo | Protected metrics, HTTPS smoke, bounded isolated controls; real dashboard data | Planned |
| W1 / 3 | Independent Python worker process | Worker CLI starts without FastAPI; process UUID, heartbeat state, SIGTERM drain and structured lifecycle logs | Verified; 18-test Compose run, `phase-3-workers.txt` |
| W2 / 3 | Atomic durable claim and fenced lease | PostgreSQL `SKIP LOCKED` claims, two concurrent connections, unexpired-owner invariance, stale-token completion rejection | Verified against disposable PostgreSQL; `phase-3-workers.txt` |
| W3 / 3 | Lease renewal and crash recovery | Renewal SQL requires an unexpired token; SIGKILL leaves a lease which another process reclaims | Verified in Linux worker containers; `phase-3-workers.txt` |
| W4 / 3 | Bounded concurrency, deadlines, retry/abandon | Three worker processes distribute jobs; timed handler is terminated; retry, final-attempt abandonment and dead-letter state tests | Verified; `phase-3-workers.txt` |
| W5 / 3 | Fixed handler allowlist and safe workloads | PNG IHDR metadata, bounded CSV transformation, synthetic aggregation, simulated allowlisted notification; no shell/user code | Unit tests and all four live workloads passed; `phase-3-workers.txt` |
| W6 / 3 | Disconnect and diagnostics | Terminate a live PG backend; next repository call reconnects; append-only attempt events plus redacted JSON logs | Verified; `phase-3-workers.txt` |
| W7 / 3 | Local PostgreSQL/three-worker Compose | `compose.workers.yaml` validation and actual isolated PostgreSQL + three process run | Config and isolated run passed; `phase-3-workers.txt` |
| W8 / 4 | Expiry, generations, retry/dead-letter, job runtime and audit reasons | SIGKILL, pause/expiry, two contenders, old generation, repeated failure and timeout integration cases | Verified; 27/27 real PostgreSQL tests, phase-4-recovery.txt |
| W9 / 4 | Idempotent enqueue/effects and operator retry/replay | Same key deduplicates; changed content rejects; effect ledger deduplicates ack-gap retry; retry/replay events auditable | Verified; phase-4-recovery.txt |
| W10 / 4 | Heartbeat and PostgreSQL outage recovery | Configurable stale cutoff; live DB backend termination and isolated PostgreSQL stop/start while work is active | Verified; outage job succeeded at attempt 2, phase-4-recovery.txt |
| W11 / worker companion 5 | Authenticated control plane, authorization, bounded requests and rate limits | `test_unauthorized_and_forbidden_requests_have_consistent_errors`, `test_strict_schema_payload_bounds_and_content_type`, `test_postgresql_fixed_window_rate_limit_and_headers`, `test_database_unavailability_returns_safe_service_error` | 4 API integration tests pass against PostgreSQL; phase-5-control-plane.txt |
| W12 / worker companion 5 | Immediate/delayed jobs, priority, cancellation, retry/replay, status/history, worker health, stats and cursor filters | `test_delayed_job_priority_queue_stats_and_workers`, `test_cancel_queued_and_running_job_is_audited_and_fenced`, `test_retry_and_controlled_replay_apis`, `test_cursor_filter_and_attempt_history` | 4 API integration tests pass; live idempotent job succeeded; phase-5-control-plane.txt |
| W13 / worker companion 5 | Submission fingerprint idempotency, recurring occurrence uniqueness, cron timezone/DST semantics, OpenAPI | `test_submission_idempotency_fingerprint_and_principal_scope`, `test_concurrent_repeated_submission_creates_one_job`, `test_recurring_schedule_api_and_cancel`, `test_swagger_openapi_documents_bearer_auth_and_request_schemas`, `test_two_scheduler_instances_do_not_duplicate_an_occurrence`, `test_nonexistent_spring_forward_wall_time_is_skipped`, `test_ambiguous_fall_back_wall_time_runs_once_at_earlier_instant` | API/scheduler race and DST tests pass against Compose PostgreSQL; live Swagger and scheduled job succeeded; phase-5-control-plane.txt |
| W14 / worker companion 6 | Operator views for queue, jobs/search/details/create, schedules, workers, DLQ, observability, queue defaults | Playwright real-stack journey creates a job, searches/details, replays, cancels, schedules/disables, views workers/DLQ/charts, updates settings | Passed against API + PostgreSQL + three worker containers; screenshots and transcript in `phase-6-dashboard.txt` / screenshot directory |
| W15 / worker companion 6 | Dashboard API contract is truthful and bounded | `test_observability_and_recent_events_are_derived_from_postgres`, `test_queue_policy_is_authorized_bounded_and_used_for_defaults`; same-origin proxy and Swagger/OpenAPI smoke | 49/49 PostgreSQL integration tests passed; live authenticated proxy and docs smoke passed; event-derived chart contract/limits in ADR-012 |
| W16 / worker companion 6 | Secure accessible responsive operation | Browser checks rejected/accepted token, operator confirmations, axe scans on overview/form, 390px nav/dialog and no overflow; `npm run lint`, `npm run build` | Playwright 2/2, lint/build passed; 11 real-page screenshots captured; hosted CI and full assistive-technology review remain unverified |

Race tests accompany all concurrent behavior. Integration tests must use real
Redis, not a mock standing in for atomicity. No future row is satisfied by F0.
