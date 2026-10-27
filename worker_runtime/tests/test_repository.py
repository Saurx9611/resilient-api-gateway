from __future__ import annotations

import threading
import time
import unittest
import uuid
import os
from concurrent.futures import ThreadPoolExecutor

from worker_runtime.repository import DatabaseUnavailable, Repository

from worker_runtime.tests.support import clear_database, connection, dsn


@unittest.skipUnless(os.environ.get("WORKER_TEST_DSN"), "set WORKER_TEST_DSN to a disposable *_test database")
class RepositoryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_database()

    def test_two_independent_connections_cannot_claim_one_valid_lease(self) -> None:
        worker_ids = [uuid.uuid4(), uuid.uuid4()]
        repos = [Repository(dsn(), worker_id, f"claim-test-{i}", 1) for i, worker_id in enumerate(worker_ids)]
        for repo in repos:
            repo.check_schema()
            repo.register()
        job_id = repos[0].enqueue("notification_simulation", {"template": "alert", "recipient_ref": "acct_1", "delay_ms": 700})
        gate = threading.Barrier(2)

        def claim(repo: Repository):
            gate.wait(timeout=3)
            return repo.claim(4)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, repos))
            claims = [job for job in results if job is not None]
            self.assertEqual(1, len(claims))
            with connection() as conn:
                row = conn.execute("SELECT state,worker_id,lease_token,lease_until FROM worker_jobs WHERE id=%s", (job_id,)).fetchone()
                events = conn.execute("SELECT event_type,worker_id,lease_token FROM worker_job_events WHERE job_id=%s AND event_type IN ('claimed','lease_reclaimed')", (job_id,)).fetchall()
                self.assertEqual("running", row["state"])
                self.assertGreater(row["lease_until"], conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"])
                self.assertEqual(claims[0].lease_token, row["lease_token"])
                self.assertEqual(1, len(events))
                self.assertEqual(str(claims[0].lease_token), str(events[0]["lease_token"]))
        finally:
            for repo in repos:
                repo.close()

    def test_database_connection_termination_is_detected_and_reconnected(self) -> None:
        repo = Repository(dsn(), uuid.uuid4(), "disconnect-test", 1)
        repo.check_schema()
        repo.register()
        job_id = repo.enqueue("synthetic_report", {"records": []})
        job = repo.claim(10)
        self.assertIsNotNone(job)
        backend_pid = repo.conn.info.backend_pid
        with connection() as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
        try:
            with self.assertRaises(DatabaseUnavailable) as raised:
                repo.heartbeat()
            self.assertTrue(raised.exception.retryable)
            repo.heartbeat()
            self.assertIsNotNone(repo.job_status(job_id))
        finally:
            repo.close()

    def test_stale_lease_token_cannot_complete_after_reclaim(self) -> None:
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        first = Repository(dsn(), first_id, "fence-old", 1)
        second = Repository(dsn(), second_id, "fence-new", 1)
        first.check_schema()
        first.register()
        second.register()
        job_id = first.enqueue("synthetic_report", {"records": []}, max_attempts=3)
        old_job = first.claim(10)
        self.assertIsNotNone(old_job)
        with connection() as conn:
            conn.execute("UPDATE worker_jobs SET lease_until=clock_timestamp()-interval '1 millisecond' WHERE id=%s", (job_id,))
        new_job = second.claim(3)
        self.assertIsNotNone(new_job)
        self.assertEqual(job_id, new_job.id)
        self.assertNotEqual(old_job.lease_token, new_job.lease_token)
        self.assertEqual(old_job.generation + 1, new_job.generation)
        self.assertFalse(first.succeed(old_job, {"stale": True}))
        self.assertFalse(first.renew(old_job, 3))
        self.assertTrue(second.succeed(new_job, {"fresh": True}))
        self.assertEqual("succeeded", second.job_status(job_id)["state"])
        first.close()
        second.close()

    def test_retry_backoff_and_final_attempt_abandonment(self) -> None:
        repo = Repository(dsn(), uuid.uuid4(), "retry-test", 1)
        repo.check_schema()
        repo.register()
        job_id = repo.enqueue("synthetic_report", {"records": []}, max_attempts=2)
        first = repo.claim(3)
        self.assertIsNotNone(first)
        self.assertTrue(repo.fail(first, "temporary_failure", retryable=True))
        row = repo.job_status(job_id)
        self.assertEqual("queued", row["state"])
        delay = repo._retry_delay(job_id, first.attempts)
        self.assertGreaterEqual(delay, 0.5)
        self.assertLessEqual(delay, 1.5)
        self.assertAlmostEqual(delay, repo._retry_delay(job_id, first.attempts))
        self.assertIsNone(repo.claim(3), "retry ignored its backoff time")
        with connection() as conn:
            conn.execute("UPDATE worker_jobs SET available_at=clock_timestamp() WHERE id=%s", (job_id,))
        second = repo.claim(3)
        self.assertEqual(2, second.attempts)
        self.assertTrue(repo.fail(second, "temporary_failure", retryable=True))
        self.assertEqual("dead", repo.job_status(job_id)["state"])
        self.assertEqual(["enqueued", "claimed", "retry_scheduled", "claimed", "abandoned"], [event["event_type"] for event in repo.events(job_id)])
        repo.close()

    def test_claim_after_expiry_is_competed_and_only_one_new_generation_wins(self) -> None:
        repos = [Repository(dsn(), uuid.uuid4(), f"expired-contender-{i}", 1) for i in range(3)]
        for repo in repos:
            repo.check_schema()
            repo.register()
        job_id = repos[0].enqueue("synthetic_report", {"records": []}, max_attempts=4)
        first = repos[0].claim(10)
        self.assertIsNotNone(first)
        with connection() as conn:
            conn.execute("UPDATE worker_jobs SET lease_until=clock_timestamp()-interval '1 millisecond' WHERE id=%s", (job_id,))
        gate = threading.Barrier(2)

        def compete(repo):
            gate.wait(timeout=3)
            return repo.claim(10)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                replacements = list(executor.map(compete, repos[1:]))
            winners = [item for item in replacements if item]
            self.assertEqual(1, len(winners))
            self.assertEqual(first.generation + 1, winners[0].generation)
            self.assertNotEqual(first.lease_token, winners[0].lease_token)
            with connection() as conn:
                current = conn.execute("SELECT worker_id,lease_token,generation,lease_until > clock_timestamp() AS valid FROM worker_jobs WHERE id=%s", (job_id,)).fetchone()
                owners = conn.execute("SELECT count(*) AS n FROM worker_job_events WHERE job_id=%s AND event_type='lease_reclaimed' AND generation=%s", (job_id, winners[0].generation)).fetchone()["n"]
            self.assertTrue(current["valid"])
            self.assertEqual(winners[0].lease_token, current["lease_token"])
            self.assertEqual(1, owners)
        finally:
            for repo in repos:
                repo.close()

    def test_repeated_failures_dead_letter_then_explicit_retry_and_replay(self) -> None:
        repo = Repository(dsn(), uuid.uuid4(), "repeated-failures", 1)
        repo.check_schema()
        repo.register()
        job_id = repo.enqueue("synthetic_report", {"records": []}, max_attempts=3)
        for attempt in range(1, 4):
            with connection() as conn:
                conn.execute("UPDATE worker_jobs SET available_at=clock_timestamp() WHERE id=%s", (job_id,))
            job = repo.claim(10)
            self.assertEqual(attempt, job.attempts)
            self.assertTrue(repo.fail(job, "injected_failure", retryable=True))
        self.assertEqual("dead", repo.job_status(job_id)["state"])
        self.assertTrue(repo.retry_dead(job_id, "operator approved retry"))
        self.assertEqual("queued", repo.job_status(job_id)["state"])
        self.assertFalse(repo.retry_dead(job_id, "duplicate operator retry"))
        retried = repo.claim(10)
        self.assertEqual(1, retried.attempts)
        self.assertTrue(repo.succeed(retried, {"ok": True}))
        replay_id = repo.replay(job_id, "reproduce verified result")
        self.assertIsNotNone(replay_id)
        self.assertNotEqual(job_id, replay_id)
        self.assertEqual(job_id, repo.job_status(replay_id)["replay_of"])
        self.assertEqual(["enqueued", "claimed", "retry_scheduled", "claimed", "retry_scheduled",
                          "claimed", "abandoned", "operator_retry", "claimed", "succeeded", "replay_created"],
                         [event["event_type"] for event in repo.events(job_id)])
        repo.close()

    def test_idempotency_key_deduplicates_and_rejects_changed_contents(self) -> None:
        repo = Repository(dsn(), uuid.uuid4(), "idempotency", 1)
        repo.check_schema()
        repo.register()
        payload = {"records": [{"category": "sales", "amount": 7}]}
        original = repo.enqueue("synthetic_report", payload, idempotency_key="request-001")
        duplicate = repo.enqueue("synthetic_report", payload, idempotency_key="request-001")
        self.assertEqual(original, duplicate)
        with self.assertRaisesRegex(ValueError, "different job contents"):
            repo.enqueue("synthetic_report", {"records": []}, idempotency_key="request-001")
        self.assertEqual(["enqueued", "submission_deduplicated"], [event["event_type"] for event in repo.events(original)])
        repo.close()

    def test_worker_heartbeat_stale_threshold_is_operator_configurable(self) -> None:
        repo = Repository(dsn(), uuid.uuid4(), "heartbeat-monitor", 1)
        repo.check_schema()
        repo.register()
        with connection() as conn:
            conn.execute("UPDATE worker_processes SET heartbeat_at=clock_timestamp()-interval '3 seconds' WHERE worker_id=%s", (repo.worker_id,))
        self.assertFalse(next(item for item in repo.workers(5) if item["worker_id"] == repo.worker_id)["stale"])
        self.assertTrue(next(item for item in repo.workers(2) if item["worker_id"] == repo.worker_id)["stale"])
        repo.close()

    def test_operator_replay_preserves_source_and_creates_new_idempotent_handler_work(self) -> None:
        from worker_runtime.handlers import execute

        payload = {"template": "receipt", "recipient_ref": "demo_1", "delay_ms": 0}
        first = execute("notification_simulation", payload, 1)
        duplicate = execute("notification_simulation", payload, 1)
        self.assertEqual(first, duplicate, "simulated delivery handler must be safe to repeat")
        repo = Repository(dsn(), uuid.uuid4(), "replay", 1)
        repo.check_schema()
        repo.register()
        source = repo.enqueue("notification_simulation", payload, max_attempts=1)
        claimed = repo.claim(10)
        self.assertTrue(repo.succeed(claimed, first))
        replay = repo.replay(source, "duplicate side effect recovery exercise")
        self.assertIsNotNone(replay)
        self.assertEqual("succeeded", repo.job_status(source)["state"])
        self.assertEqual("queued", repo.job_status(replay)["state"])
        self.assertEqual(source, repo.job_status(replay)["replay_of"])
        repo.close()

    def test_expired_final_attempt_is_abandoned(self) -> None:
        worker_id = uuid.uuid4()
        repo = Repository(dsn(), worker_id, "abandon-test", 1)
        repo.check_schema()
        repo.register()
        job_id = repo.enqueue("synthetic_report", {"records": []}, max_attempts=1)
        job = repo.claim(10)
        self.assertIsNotNone(job)
        with connection() as conn:
            conn.execute("UPDATE worker_jobs SET lease_until=clock_timestamp()-interval '1 millisecond' WHERE id=%s", (job_id,))
        self.assertEqual(1, repo.abandon_exhausted())
        self.assertEqual("dead", repo.job_status(job_id)["state"])
        self.assertEqual("abandoned", repo.events(job_id)[-1]["event_type"])
        repo.close()


if __name__ == "__main__":
    unittest.main()
