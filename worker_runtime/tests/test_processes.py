from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import os
from pathlib import Path

from worker_runtime.repository import Repository
from worker_runtime.control_repository import ControlRepository

from worker_runtime.tests.support import clear_database, connection, dsn


@unittest.skipUnless(os.environ.get("WORKER_TEST_DSN"), "set WORKER_TEST_DSN to a disposable *_test database")
class WorkerProcessIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_database()
        self.temp = tempfile.TemporaryDirectory(prefix="worker-runtime-")
        self.processes: list[tuple[subprocess.Popen[bytes], str]] = []
        self.log_streams = []

    def tearDown(self) -> None:
        for process, _ in self.processes:
            if process.poll() is None:
                self._stop(process, hard=True)
        for stream in self.log_streams:
            stream.close()
        self.temp.cleanup()

    def _start(self, name: str, *, concurrency: int = 1, lease: int = 4, timeout: float = 8, grace: float = 2,
               fault_env: dict[str, str] | None = None) -> tuple[subprocess.Popen[bytes], str]:
        log = str(Path(self.temp.name) / f"{name}.jsonl")
        stream = open(log, "ab", buffering=0)
        self.log_streams.append(stream)
        env = os.environ.copy()
        env["WORKER_DATABASE_URL"] = dsn()
        env["WORKER_DB_SCHEMA"] = "worker_runtime_test"
        env.update(fault_env or {})
        command = [sys.executable, "-m", "worker_runtime", "worker", "--name", name,
                   "--concurrency", str(concurrency), "--lease-seconds", str(lease),
                   "--handler-timeout", str(timeout), "--shutdown-grace", str(grace)]
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        self.processes.append((process, log))
        return process, log

    @staticmethod
    def _stop(process: subprocess.Popen[bytes], *, hard: bool = False) -> None:
        if process.poll() is not None:
            return
        if hard:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        else:
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=8)

    @staticmethod
    def _wait_for(predicate, timeout: float = 12.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(0.05)
        raise AssertionError(f"condition did not become true before timeout (last={last!r})")

    def _enqueue(self, payload: dict, *, attempts: int = 3, max_runtime: int = 30) -> int:
        repo = Repository(dsn(), uuid.uuid4(), "test-enqueue", 1)
        try:
            return repo.enqueue("notification_simulation", payload, max_attempts=attempts,
                                max_runtime_seconds=max_runtime)
        finally:
            repo.close()

    def _job(self, job_id: int):
        with connection() as conn:
            return conn.execute("SELECT * FROM worker_jobs WHERE id=%s", (job_id,)).fetchone()

    def _event_rows(self, job_id: int):
        with connection() as conn:
            return conn.execute("SELECT event_type,attempt,worker_id,lease_token FROM worker_job_events WHERE job_id=%s ORDER BY id", (job_id,)).fetchall()

    def _worker_rows(self):
        with connection() as conn:
            return conn.execute("SELECT worker_id,state,heartbeat_at FROM worker_processes").fetchall()

    def test_three_processes_distribute_work_and_keep_a_single_valid_lease_owner(self) -> None:
        workers = [self._start(f"distribution-{i}", lease=5) for i in range(3)]
        self._wait_for(lambda: len(self._worker_rows()) == 3, timeout=10)
        initial_heartbeats = {row["worker_id"]: row["heartbeat_at"] for row in self._worker_rows()}
        jobs = [self._enqueue({"template": "receipt", "recipient_ref": "batch_0", "delay_ms": 3000})]
        jobs.extend(self._enqueue({"template": "alert", "recipient_ref": f"batch_{i}", "delay_ms": 120}) for i in range(23))

        long_job = jobs[0]
        self._wait_for(lambda: self._job(long_job)["state"] == "running", timeout=5)
        with connection() as conn:
            first = conn.execute("SELECT worker_id,lease_token,lease_until FROM worker_jobs WHERE id=%s", (long_job,)).fetchone()
            self.assertTrue(conn.execute("SELECT lease_until > clock_timestamp() AS valid FROM worker_jobs WHERE id=%s", (long_job,)).fetchone()["valid"])
            claim_count = conn.execute("SELECT count(*) AS n FROM worker_job_events WHERE job_id=%s AND event_type IN ('claimed','lease_reclaimed')", (long_job,)).fetchone()["n"]
        self.assertEqual(1, claim_count)
        time.sleep(1.7)
        with connection() as conn:
            second = conn.execute("SELECT worker_id,lease_token,lease_until FROM worker_jobs WHERE id=%s", (long_job,)).fetchone()
            self.assertTrue(conn.execute("SELECT state='running' AS running FROM worker_jobs WHERE id=%s", (long_job,)).fetchone()["running"])
            claim_count = conn.execute("SELECT count(*) AS n FROM worker_job_events WHERE job_id=%s AND event_type IN ('claimed','lease_reclaimed')", (long_job,)).fetchone()["n"]
        self.assertEqual(first["worker_id"], second["worker_id"])
        self.assertEqual(first["lease_token"], second["lease_token"])
        self.assertGreater(second["lease_until"], first["lease_until"], "active work lease was not renewed")
        self.assertEqual(1, claim_count, "a valid lease was concurrently claimed twice")

        # Each job launches an isolated handler process. Under loaded container
        # hosts, process scheduling adds material overhead to the short handler
        # delays, so leave room for scheduler jitter without weakening the
        # assertions about lease validity or distribution.
        self._wait_for(lambda: all(self._job(job)["state"] == "succeeded" for job in jobs), timeout=45)
        with connection() as conn:
            claims = conn.execute("SELECT DISTINCT worker_id FROM worker_job_events WHERE event_type='claimed'").fetchall()
            total = conn.execute("SELECT count(*) AS n FROM worker_job_events WHERE event_type='succeeded'").fetchone()["n"]
        self.assertEqual(3, len(claims), "all three independent worker processes should receive work")
        self.assertEqual(len(jobs), total)
        current_heartbeats = {row["worker_id"]: row["heartbeat_at"] for row in self._worker_rows()}
        self.assertTrue(all(current_heartbeats[key] > value for key, value in initial_heartbeats.items()), "worker heartbeat timestamps did not advance")
        for process, _ in workers:
            self._stop(process)
        for process, log in workers:
            raw = Path(log).read_text(encoding="utf-8")
            self.assertIn('"event":"worker.stopped"', raw)
            self.assertIn('"event":"job.succeeded"', raw)

    def test_sigterm_stops_claiming_and_drains_active_work(self) -> None:
        first = self._enqueue({"template": "welcome", "recipient_ref": "graceful_1", "delay_ms": 450})
        second = self._enqueue({"template": "welcome", "recipient_ref": "graceful_2", "delay_ms": 0})
        worker, log = self._start("graceful", lease=4, grace=3)
        self._wait_for(lambda: self._job(first)["state"] == "running", timeout=8)
        self._stop(worker)
        self.assertEqual("succeeded", self._job(first)["state"])
        self.assertEqual("queued", self._job(second)["state"], "draining worker claimed new work")
        self.assertIn('"event":"worker.draining"', Path(log).read_text(encoding="utf-8"))
        self.assertTrue(any(row["state"] == "stopped" for row in self._worker_rows()))

    def test_running_job_cancellation_stops_handler_at_lease_renewal(self) -> None:
        job_id = self._enqueue({"template": "welcome", "recipient_ref": "cancel_running", "delay_ms": 5000}, attempts=3)
        worker, log = self._start("cancel-running", lease=4, timeout=8)
        self._wait_for(lambda: self._job(job_id)["state"] == "running", timeout=8)
        control = ControlRepository(dsn())
        result = control.cancel_job(job_id, "operator requested cancellation")
        self.assertEqual({"state": "running", "cancel_requested": True}, result)
        self._wait_for(lambda: self._job(job_id)["state"] == "canceled", timeout=5)
        self.assertIn('"event":"job.canceled"', Path(log).read_text(encoding="utf-8"))
        self.assertEqual(["enqueued", "claimed", "started", "cancel_requested", "canceled"],
                         [event["event_type"] for event in self._event_rows(job_id)])
        self._stop(worker)

    def test_sigkill_expires_lease_then_another_process_recovers_job(self) -> None:
        if os.name != "posix":
            self.skipTest("SIGKILL process-group recovery is exercised inside the Linux Compose test container")
        job_id = self._enqueue({"template": "receipt", "recipient_ref": "crash_1", "delay_ms": 2000}, attempts=3)
        victim, victim_log = self._start("sigkill-victim", lease=3, timeout=8)
        self._wait_for(lambda: '"event":"job.started"' in Path(victim_log).read_text(encoding="utf-8"), timeout=8)
        recovery, _ = self._start("sigkill-recovery", lease=3, timeout=8)
        self._wait_for(lambda: len(self._worker_rows()) == 2, timeout=8)
        first = self._job(job_id)
        first_worker, first_token = first["worker_id"], first["lease_token"]
        time.sleep(0.4)
        still_valid = self._job(job_id)
        self.assertEqual(first_worker, still_valid["worker_id"])
        self.assertEqual(first_token, still_valid["lease_token"])
        self.assertEqual(1, sum(e["event_type"] in ("claimed", "lease_reclaimed") for e in self._event_rows(job_id)))

        self._stop(victim, hard=True)
        self._wait_for(lambda: self._job(job_id)["state"] == "succeeded", timeout=12)
        self.assertEqual(2, self._job(job_id)["attempts"])
        events = self._event_rows(job_id)
        self.assertEqual(["enqueued", "claimed", "started", "lease_reclaimed", "started", "effect_applied", "succeeded"], [event["event_type"] for event in events])
        self.assertNotEqual(first_worker, events[3]["worker_id"])
        self._stop(recovery)

    def test_worker_killed_after_claim_before_handler_start_is_reclaimed(self) -> None:
        job_id = self._enqueue({"template": "alert", "recipient_ref": "before_start", "delay_ms": 0}, attempts=3)
        victim, log = self._start("before-start-victim", lease=3,
                                  fault_env={"WORKER_TEST_PAUSE_AFTER_CLAIM_SECONDS": "3"})
        self._wait_for(lambda: '"point":"after_claim"' in Path(log).read_text(encoding="utf-8"), timeout=8)
        self.assertEqual("running", self._job(job_id)["state"])
        replacement, replacement_log = self._start("before-start-replacement", lease=3)
        self._stop(victim, hard=True)
        self._wait_for(lambda: self._job(job_id)["state"] == "succeeded", timeout=12)
        self.assertEqual(2, self._job(job_id)["attempts"])
        events = self._event_rows(job_id)
        self.assertEqual(["enqueued", "claimed", "lease_reclaimed", "started", "effect_applied", "succeeded"], [e["event_type"] for e in events])
        self.assertNotEqual(events[1]["worker_id"], events[2]["worker_id"])
        self.assertIn('"event":"job.started"', Path(replacement_log).read_text(encoding="utf-8"))
        self._stop(replacement)

    def test_worker_killed_after_handler_work_before_ack_repeats_idempotent_simulation(self) -> None:
        job_id = self._enqueue({"template": "receipt", "recipient_ref": "ack_gap", "delay_ms": 0}, attempts=3)
        victim, log = self._start("ack-gap-victim", lease=3,
                                  fault_env={"WORKER_TEST_PAUSE_BEFORE_ACK_SECONDS": "3"})
        self._wait_for(lambda: '"point":"before_ack"' in Path(log).read_text(encoding="utf-8"), timeout=8)
        self.assertEqual("running", self._job(job_id)["state"])
        recovery, _ = self._start("ack-gap-recovery", lease=3)
        self._stop(victim, hard=True)
        self._wait_for(lambda: self._job(job_id)["state"] == "succeeded", timeout=12)
        self.assertEqual(2, self._job(job_id)["attempts"], "handler executes again when its acknowledgment is lost")
        self.assertEqual({"delivery": "simulated", "template": "receipt"}, self._job(job_id)["result"])
        with connection() as conn:
            effects = conn.execute("SELECT count(*) AS n FROM worker_demo_effects WHERE job_id=%s", (job_id,)).fetchone()["n"]
            event_types = [row["event_type"] for row in conn.execute(
                "SELECT event_type FROM worker_job_events WHERE job_id=%s ORDER BY id", (job_id,)).fetchall()]
        self.assertEqual(1, effects, "duplicate handler execution must not duplicate the simulated side effect")
        self.assertIn("effect_applied", event_types)
        self.assertIn("effect_deduplicated", event_types)
        self._stop(recovery)

    def test_paused_worker_cannot_ack_after_lease_expiry_and_replacement_claim(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group SIGSTOP and SIGCONT are exercised in Linux Compose")
        job_id = self._enqueue({"template": "alert", "recipient_ref": "paused_worker", "delay_ms": 1800}, attempts=3)
        paused, paused_log = self._start("paused-worker", lease=3, timeout=8)
        self._wait_for(lambda: '"event":"job.started"' in Path(paused_log).read_text(encoding="utf-8"), timeout=8)
        replacement, _ = self._start("paused-replacement", lease=3, timeout=8)
        first_generation = self._job(job_id)["generation"]
        os.killpg(paused.pid, signal.SIGSTOP)
        try:
            self._wait_for(lambda: self._job(job_id)["generation"] > first_generation, timeout=10)
            self._wait_for(lambda: self._job(job_id)["state"] == "succeeded", timeout=10)
        finally:
            if paused.poll() is None:
                os.killpg(paused.pid, signal.SIGCONT)
        time.sleep(0.3)
        self.assertEqual("succeeded", self._job(job_id)["state"])
        self.assertEqual(2, self._job(job_id)["generation"])
        self.assertIn('"event":"job.lease_lost"', Path(paused_log).read_text(encoding="utf-8"))
        self._stop(paused)
        self._stop(replacement)

    def test_live_database_disconnect_stops_child_and_worker_recovers(self) -> None:
        job_id = self._enqueue({"template": "alert", "recipient_ref": "db_loss_1", "delay_ms": 2200}, attempts=3)
        worker, log = self._start("db-loss", lease=4, timeout=8)
        self._wait_for(lambda: self._job(job_id)["state"] == "running", timeout=8)
        with connection() as conn:
            process = conn.execute("SELECT worker_id FROM worker_jobs WHERE id=%s", (job_id,)).fetchone()["worker_id"]
            backend = conn.execute("SELECT pid FROM pg_stat_activity WHERE application_name=%s", (f"worker-{process}",)).fetchone()
            self.assertIsNotNone(backend, "worker should retain a database session for its supervisor loop")
            conn.execute("SELECT pg_terminate_backend(%s)", (backend["pid"],))

        self._wait_for(lambda: self._job(job_id)["state"] == "succeeded", timeout=12)
        self.assertIsNone(worker.poll(), "worker exited instead of reconnecting")
        self.assertEqual(2, self._job(job_id)["attempts"])
        events = [event["event_type"] for event in self._event_rows(job_id)]
        self.assertEqual(["enqueued", "claimed", "started", "lease_reclaimed", "started", "effect_applied", "succeeded"], events)
        self.assertIn('"event":"database.unavailable"', Path(log).read_text(encoding="utf-8"))
        self._stop(worker)

    def test_handler_timeout_is_killed_and_recorded(self) -> None:
        job_id = self._enqueue({"template": "alert", "recipient_ref": "timeout_1", "delay_ms": 1000}, attempts=1)
        worker, _ = self._start("timeout", lease=3, timeout=0.25)
        self._wait_for(lambda: self._job(job_id)["state"] == "dead", timeout=8)
        self.assertEqual("handler_timeout", self._job(job_id)["last_error_code"])
        self.assertIn("timed_out", [event["event_type"] for event in self._event_rows(job_id)])
        self._stop(worker)

    def test_job_specific_runtime_limit_is_enforced(self) -> None:
        job_id = self._enqueue({"template": "alert", "recipient_ref": "job_deadline", "delay_ms": 2500},
                               attempts=1, max_runtime=1)
        worker, _ = self._start("job-runtime-bound", lease=3, timeout=8)
        self._wait_for(lambda: self._job(job_id)["state"] == "dead", timeout=8)
        self.assertEqual("handler_timeout", self._job(job_id)["last_error_code"])
        self._stop(worker)


if __name__ == "__main__":
    unittest.main()
