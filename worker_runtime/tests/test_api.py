from __future__ import annotations

import hashlib
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from worker_runtime.api import create_app
from worker_runtime.repository import Repository
from worker_runtime.tests.support import clear_database, connection, dsn

ADMIN = "local-integration-admin-token-0123456789-abcdef"
READER = "local-integration-reader-token-0123456789-abcdef"
SUBMITTER = "local-integration-submit-token-0123456789-abcdef"
KEYS = {
    hashlib.sha256(ADMIN.encode()).hexdigest(): ("test-operator", frozenset({"read", "submit", "operator"})),
    hashlib.sha256(READER.encode()).hexdigest(): ("test-reader", frozenset({"read"})),
    hashlib.sha256(SUBMITTER.encode()).hexdigest(): ("test-submitter", frozenset({"submit"})),
}


@unittest.skipUnless(os.environ.get("WORKER_TEST_DSN"), "set WORKER_TEST_DSN to a disposable *_test database")
class ControlAPIIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_database()
        self.app = create_app(dsn=dsn(), api_keys=KEYS, rate_limit=100)
        self.client = TestClient(self.app)
        self.admin = {"Authorization": f"Bearer {ADMIN}"}
        self.reader = {"Authorization": f"Bearer {READER}"}
        self.submitter = {"Authorization": f"Bearer {SUBMITTER}"}

    def tearDown(self) -> None:
        self.client.close()
        self.app.state.repository.close()

    @staticmethod
    def job_body(**updates):
        body = {"job_type": "synthetic_report", "payload": {"records": []}}
        body.update(updates)
        return body

    def test_unauthorized_and_forbidden_requests_have_consistent_errors(self) -> None:
        missing = self.client.post("/v1/jobs", json=self.job_body())
        self.assertEqual(401, missing.status_code)
        self.assertEqual("unauthorized", missing.json()["error"]["code"])
        self.assertIn("X-Request-ID", missing.headers)
        invalid = self.client.get("/v1/jobs", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(401, invalid.status_code)
        forbidden = self.client.post("/v1/jobs/1/cancel", json={"reason": "cancel requested"}, headers=self.reader)
        self.assertEqual(403, forbidden.status_code)
        self.assertEqual("forbidden", forbidden.json()["error"]["code"])

    def test_strict_schema_payload_bounds_and_content_type(self) -> None:
        extra = self.client.post("/v1/jobs", headers=self.submitter, json=self.job_body(unexpected="no"))
        self.assertEqual(422, extra.status_code)
        invalid_payload = self.client.post("/v1/jobs", headers=self.submitter,
            json={"job_type": "notification_simulation", "payload": {"template": "shell", "recipient_ref": "x"}})
        self.assertEqual(422, invalid_payload.status_code)
        too_large = self.client.post("/v1/jobs", headers={**self.submitter, "content-type": "application/json"},
                                     content=b" " * (64 * 1024 + 1))
        self.assertEqual(413, too_large.status_code)
        wrong_type = self.client.post("/v1/jobs", headers={**self.submitter, "content-type": "text/plain"}, content=b"{}")
        self.assertEqual(415, wrong_type.status_code)
        self.assertEqual("application_json_required", wrong_type.json()["error"]["code"])
        naive = self.client.post("/v1/jobs", headers=self.submitter,
            json=self.job_body(run_at="2027-01-01T00:00:00"))
        self.assertEqual(422, naive.status_code)
        invalid_priority = self.client.post("/v1/jobs", headers=self.submitter,
            json=self.job_body(priority=True))
        self.assertEqual(422, invalid_priority.status_code)
        anonymous_large = self.client.post("/v1/jobs", content=b" " * (64 * 1024 + 1))
        self.assertEqual(401, anonymous_large.status_code, "auth must run before request body reads")

    def test_submission_idempotency_fingerprint_and_principal_scope(self) -> None:
        headers = {**self.submitter, "Idempotency-Key": "request-001"}
        first = self.client.post("/v1/jobs", headers=headers, json=self.job_body(priority=2))
        repeat = self.client.post("/v1/jobs", headers=headers, json=self.job_body(priority=2))
        self.assertEqual(202, first.status_code)
        self.assertEqual(first.json()["job_id"], repeat.json()["job_id"])
        mismatch = self.client.post("/v1/jobs", headers=headers, json=self.job_body(priority=3))
        self.assertEqual(409, mismatch.status_code)
        # The same caller key is independent for a different authenticated principal.
        other = self.client.post("/v1/jobs", headers={**self.admin, "Idempotency-Key": "request-001"}, json=self.job_body(priority=2))
        self.assertEqual(202, other.status_code)
        self.assertNotEqual(first.json()["job_id"], other.json()["job_id"])
        with connection() as conn:
            stored = conn.execute("SELECT request_fingerprint FROM worker_jobs WHERE id=%s", (first.json()["job_id"],)).fetchone()
        self.assertRegex(stored["request_fingerprint"], r"^[0-9a-f]{64}$")

    def test_concurrent_repeated_submission_creates_one_job(self) -> None:
        barrier = threading.Barrier(8)
        headers = {**self.submitter, "Idempotency-Key": "concurrent-001"}

        def post(_):
            barrier.wait(timeout=5)
            return self.client.post("/v1/jobs", headers=headers, json=self.job_body())

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(post, range(8)))
        self.assertTrue(all(response.status_code == 202 for response in responses),
                        [response.text for response in responses])
        self.assertEqual(1, len({response.json()["job_id"] for response in responses}))
        with connection() as conn:
            self.assertEqual(1, conn.execute("SELECT count(*) AS n FROM worker_jobs").fetchone()["n"])

    def test_cursor_filter_and_attempt_history(self) -> None:
        ids = []
        for kind in ("synthetic_report", "csv_transform", "synthetic_report"):
            payload = {"records": []} if kind == "synthetic_report" else {"csv_text": "name\na\n", "column": "name"}
            ids.append(self.client.post("/v1/jobs", headers=self.submitter,
                json={"job_type": kind, "payload": payload}).json()["job_id"])
        page = self.client.get("/v1/jobs?limit=2&state=queued&job_type=synthetic_report", headers=self.reader)
        self.assertEqual([ids[2], ids[0]], [item["id"] for item in page.json()["items"]])
        first_page = self.client.get("/v1/jobs?limit=1", headers=self.reader).json()
        second_page = self.client.get(f"/v1/jobs?limit=1&cursor={first_page['next_cursor']}", headers=self.reader).json()
        self.assertNotEqual(first_page["items"][0]["id"], second_page["items"][0]["id"])
        attempts = self.client.get(f"/v1/jobs/{ids[0]}/attempts", headers=self.reader)
        self.assertEqual("enqueued", attempts.json()["items"][0]["event_type"])
        bad_cursor = self.client.get("/v1/jobs?cursor=not-a-cursor", headers=self.reader)
        self.assertEqual(422, bad_cursor.status_code)
        matching = self.client.get("/v1/jobs?q=synthetic_report", headers=self.reader)
        self.assertTrue(all(item["job_type"] == "synthetic_report" for item in matching.json()["items"]))
        self.assertEqual(422, self.client.get("/v1/jobs?q=invalid%20query", headers=self.reader).status_code)

    def test_delayed_job_priority_queue_stats_and_workers(self) -> None:
        delayed_response = self.client.post("/v1/jobs", headers=self.submitter,
            json=self.job_body(run_at="2027-01-01T00:00:00Z", priority=10))
        self.assertEqual(202, delayed_response.status_code, delayed_response.text)
        delayed = delayed_response.json()["job_id"]
        low = self.client.post("/v1/jobs", headers=self.submitter,
            json=self.job_body(priority=0)).json()["job_id"]
        high = self.client.post("/v1/jobs", headers=self.submitter,
            json=self.job_body(priority=5)).json()["job_id"]
        stats = self.client.get("/v1/queue/stats", headers=self.reader).json()
        self.assertEqual(2, stats["ready"])
        self.assertEqual(1, stats["delayed"])
        repo = Repository(dsn(), __import__("uuid").uuid4(), "priority-check", 1)
        repo.register()
        try:
            claim = repo.claim(10)
            self.assertEqual(high, claim.id)
            self.assertNotEqual(delayed, claim.id)
        finally:
            repo.close()
        self.assertIsInstance(self.client.get("/v1/workers", headers=self.reader).json()["items"], list)

    def test_cancel_queued_and_running_job_is_audited_and_fenced(self) -> None:
        queued_id = self.client.post("/v1/jobs", headers=self.submitter, json=self.job_body()).json()["job_id"]
        queued = self.client.post(f"/v1/jobs/{queued_id}/cancel", headers=self.admin,
                                  json={"reason": "operator requested"})
        self.assertEqual("canceled", queued.json()["state"])
        running_id = self.client.post("/v1/jobs", headers=self.submitter, json=self.job_body()).json()["job_id"]
        import uuid
        repo = Repository(dsn(), uuid.uuid4(), "cancel-owner", 1)
        repo.register()
        claimed = repo.claim(10)
        self.assertEqual(running_id, claimed.id)
        requested = self.client.post(f"/v1/jobs/{running_id}/cancel", headers=self.admin,
                                     json={"reason": "operator requested"})
        self.assertTrue(requested.json()["cancel_requested"])
        self.assertTrue(repo.acknowledge_cancel(claimed))
        self.assertEqual("canceled", repo.job_status(running_id)["state"])
        self.assertEqual("cancel_requested", repo.events(running_id)[2]["event_type"])
        self.assertEqual("canceled", repo.events(running_id)[3]["event_type"])
        repo.close()

    def test_retry_and_controlled_replay_apis(self) -> None:
        job_id = self.client.post("/v1/jobs", headers=self.submitter, json=self.job_body()).json()["job_id"]
        with connection() as conn:
            conn.execute("UPDATE worker_jobs SET state='dead' WHERE id=%s", (job_id,))
        retried = self.client.post(f"/v1/jobs/{job_id}/retry", headers=self.admin,
                                   json={"reason": "approved operator retry"})
        self.assertEqual(202, retried.status_code)
        import uuid
        repo = Repository(dsn(), uuid.uuid4(), "api-retry-worker", 1)
        repo.register()
        claimed = repo.claim(10)
        self.assertEqual(job_id, claimed.id)
        self.assertTrue(repo.succeed(claimed, {"ok": True}))
        replay_headers = {**self.admin, "Idempotency-Key": "replay-once"}
        replay = self.client.post(f"/v1/jobs/{job_id}/replay", headers=replay_headers,
                                  json={"reason": "reproduce reported output"})
        self.assertEqual(202, replay.status_code)
        repeated = self.client.post(f"/v1/jobs/{job_id}/replay", headers=replay_headers,
                                    json={"reason": "reproduce reported output"})
        self.assertEqual(replay.json()["job_id"], repeated.json()["job_id"])
        mismatch = self.client.post(f"/v1/jobs/{job_id}/replay", headers=replay_headers,
                                    json={"reason": "different replay reason"})
        self.assertEqual(409, mismatch.status_code)
        self.assertEqual(job_id, self.client.get(f"/v1/jobs/{replay.json()['job_id']}", headers=self.reader).json()["replay_of"])
        repo.close()

    def test_recurring_schedule_api_and_cancel(self) -> None:
        body = {"job_type": "synthetic_report", "payload": {"records": []},
                "cron_expression": "0 9 * * 1-5", "timezone": "America/New_York", "priority": 2}
        created = self.client.post("/v1/schedules", headers=self.submitter, json=body)
        self.assertEqual(201, created.status_code, created.text)
        schedule_id = created.json()["schedule_id"]
        self.assertEqual("America/New_York", created.json()["timezone"])
        listing = self.client.get("/v1/schedules", headers=self.reader)
        self.assertEqual(1, len(listing.json()["items"]))
        canceled = self.client.post(f"/v1/schedules/{schedule_id}/cancel", headers=self.admin,
                                    json={"reason": "retire test schedule"})
        self.assertFalse(canceled.json()["enabled"])
        invalid = self.client.post("/v1/schedules", headers=self.submitter,
            json={**body, "timezone": "Mars/Olympus"})
        self.assertEqual(422, invalid.status_code)

    def test_postgresql_fixed_window_rate_limit_and_headers(self) -> None:
        limited = TestClient(create_app(dsn=dsn(), api_keys=KEYS, rate_limit=2))
        first = limited.get("/v1/queue/stats", headers=self.reader)
        second = limited.get("/v1/queue/stats", headers=self.reader)
        third = limited.get("/v1/queue/stats", headers=self.reader)
        self.assertEqual("1", first.headers["RateLimit-Remaining"])
        self.assertEqual("0", second.headers["RateLimit-Remaining"])
        self.assertEqual(429, third.status_code)
        self.assertEqual("60", third.headers["Retry-After"])
        self.assertEqual("rate_limit_exceeded", third.json()["error"]["code"])
        limited.close()
        limited.app.state.repository.close()

    def test_database_unavailability_returns_safe_service_error(self) -> None:
        unreachable = TestClient(create_app(
            dsn="postgresql://gateway:unused@127.0.0.1:1/gateway_test?connect_timeout=1",
            api_keys=KEYS, rate_limit=10))
        response = unreachable.get("/v1/queue/stats", headers=self.reader)
        self.assertEqual(503, response.status_code)
        self.assertEqual("database_unavailable", response.json()["error"]["code"])
        self.assertNotIn("127.0.0.1", response.text)
        unreachable.close()
        unreachable.app.state.repository.close()

    def test_swagger_openapi_documents_bearer_auth_and_request_schemas(self) -> None:
        self.assertEqual(200, self.client.get("/healthz").status_code)
        self.assertEqual(200, self.client.get("/readyz").status_code)
        docs = self.client.get("/docs")
        schema = self.client.get("/openapi.json")
        self.assertEqual(200, docs.status_code)
        self.assertEqual(200, schema.status_code)
        document = schema.json()
        self.assertIn("BearerToken", document["components"]["securitySchemes"])
        self.assertIn("requestBody", document["paths"]["/v1/jobs"]["post"])
        body_schema = document["paths"]["/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual("JobSubmission", body_schema["title"])

    def test_observability_and_recent_events_are_derived_from_postgres(self) -> None:
        job_id = self.client.post("/v1/jobs", headers=self.submitter, json=self.job_body()).json()["job_id"]
        import uuid
        repo = Repository(dsn(), uuid.uuid4(), "observability-test", 1)
        repo.register()
        claim = repo.claim(10)
        self.assertEqual(job_id, claim.id)
        self.assertTrue(repo.mark_started(claim, claim_latency_seconds=0.012))
        self.assertTrue(repo.succeed(claim, {"ok": True}))
        repo.close()

        metrics = self.client.get("/v1/observability?hours=24", headers=self.reader)
        self.assertEqual(200, metrics.status_code)
        document = metrics.json()
        self.assertEqual(24, document["hours"])
        self.assertGreaterEqual(sum(bucket["succeeded"] for bucket in document["buckets"]), 1)
        self.assertGreaterEqual(document["latency"]["samples"], 1)
        self.assertEqual("ms", document["latency"]["unit"])
        self.assertGreaterEqual(document["operations"]["completed"], 1)
        self.assertEqual(24, document["operations"]["window_hours"])
        self.assertIsNotNone(document["operations"]["execution_p50_ms"])
        self.assertAlmostEqual(12.0, document["operations"]["claim_p50_ms"], delta=0.1)
        self.assertEqual(422, self.client.get("/v1/observability?hours=48", headers=self.reader).status_code)
        events = self.client.get("/v1/events?limit=3", headers=self.reader)
        self.assertEqual(200, events.status_code)
        self.assertTrue(any(item["job_id"] == job_id and item["event_type"] == "succeeded"
                            for item in events.json()["items"]))

    def test_prometheus_metrics_endpoint_is_private_format_without_job_labels(self) -> None:
        response = self.client.get("/metrics")
        self.assertEqual(200, response.status_code)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("chronos_api_db_pool_connections", response.text)
        self.assertIn('chronos_jobs{state="queued"}', response.text)
        self.assertIn('chronos_workers{state="idle"}', response.text)
        self.assertNotIn("job_id", response.text)

    def test_queue_policy_is_authorized_bounded_and_used_for_defaults(self) -> None:
        denied = self.client.put("/v1/settings/queue", headers=self.reader,
            json={"default_max_attempts": 3, "default_max_runtime_seconds": 45})
        self.assertEqual(403, denied.status_code)
        saved = self.client.put("/v1/settings/queue", headers=self.admin,
            json={"default_max_attempts": 3, "default_max_runtime_seconds": 45})
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(3, saved.json()["default_max_attempts"])
        invalid = self.client.put("/v1/settings/queue", headers=self.admin,
            json={"default_max_attempts": 11, "default_max_runtime_seconds": 45})
        self.assertEqual(422, invalid.status_code)
        submitted = self.client.post("/v1/jobs", headers=self.submitter, json=self.job_body())
        self.assertEqual(202, submitted.status_code, submitted.text)
        details = self.client.get(f"/v1/jobs/{submitted.json()['job_id']}", headers=self.reader).json()
        self.assertEqual(3, details["max_attempts"])
        self.assertEqual(45, details["max_runtime_seconds"])
        self.assertEqual("test-operator", self.client.get("/v1/settings/queue", headers=self.reader).json()["updated_by"])


if __name__ == "__main__":
    unittest.main()
