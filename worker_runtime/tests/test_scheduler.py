from __future__ import annotations

import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from worker_runtime.control_repository import ControlRepository
from worker_runtime.tests.support import clear_database, connection, dsn


@unittest.skipUnless(os.environ.get("WORKER_TEST_DSN"), "set WORKER_TEST_DSN to a disposable *_test database")
class SchedulerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_database()
        self.repo = ControlRepository(dsn())

    def tearDown(self) -> None:
        pass

    def _schedule(self, cron: str = "0 0 1 1 *") -> uuid.UUID:
        row = self.repo.create_schedule(job_type="synthetic_report", payload={"records": []},
            cron_expression=cron, timezone_name="UTC", priority=3, max_attempts=4,
            max_runtime_seconds=20, created_by="scheduler-test")
        schedule_id = row["schedule_id"]
        with connection() as conn:
            conn.execute("UPDATE worker_schedules SET next_occurrence_at=clock_timestamp()-interval '1 second' WHERE schedule_id=%s", (schedule_id,))
        return schedule_id

    def test_two_scheduler_instances_do_not_duplicate_an_occurrence(self) -> None:
        schedule_id = self._schedule()
        gate = threading.Barrier(2)

        def tick():
            instance = ControlRepository(dsn())
            gate.wait(timeout=5)
            return instance.materialize_due(limit=10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            materialized = list(pool.map(lambda _: tick(), range(2)))
        with connection() as conn:
            occurrences = conn.execute("SELECT occurrence_at,job_id FROM worker_schedule_occurrences WHERE schedule_id=%s", (schedule_id,)).fetchall()
            jobs = conn.execute("SELECT id,state,priority,submitted_by FROM worker_jobs WHERE id IN (SELECT job_id FROM worker_schedule_occurrences WHERE schedule_id=%s)", (schedule_id,)).fetchall()
        self.assertEqual(1, len(occurrences))
        self.assertEqual(1, sum(len(ids) for ids in materialized))
        self.assertEqual(1, len(jobs))
        self.assertEqual("queued", jobs[0]["state"])
        self.assertEqual(3, jobs[0]["priority"])
        self.assertEqual("scheduler-test", jobs[0]["submitted_by"])
        status = self.repo.job(jobs[0]["id"])
        self.assertEqual("scheduled", self.repo.page_events(status["id"], limit=10, cursor=None)[0][0]["event_type"])

    def test_scheduler_catches_up_without_coalescing_and_never_repeats_key(self) -> None:
        schedule_id = self._schedule("* * * * *")
        first = self.repo.materialize_due(limit=1)
        self.assertEqual(1, len(first))
        # Move the next due time into the past to model scheduler downtime.
        with connection() as conn:
            conn.execute("UPDATE worker_schedules SET next_occurrence_at=clock_timestamp()-interval '1 minute' WHERE schedule_id=%s", (schedule_id,))
        second = self.repo.materialize_due(limit=1)
        self.assertEqual(1, len(second))
        self.assertNotEqual(first[0], second[0])
        with connection() as conn:
            rows = conn.execute("SELECT occurrence_at,job_id FROM worker_schedule_occurrences WHERE schedule_id=%s ORDER BY occurrence_at", (schedule_id,)).fetchall()
        self.assertEqual(2, len(rows))
        self.assertNotEqual(rows[0]["occurrence_at"], rows[1]["occurrence_at"])
        self.assertEqual({first[0], second[0]}, {row["job_id"] for row in rows})

    def test_disabled_schedule_is_not_materialized(self) -> None:
        schedule_id = self._schedule()
        self.repo.cancel_schedule(schedule_id, "scheduler-test", "disable test schedule")
        self.assertEqual([], self.repo.materialize_due(limit=10))
        self.assertFalse(self.repo.schedule(schedule_id)["enabled"])


if __name__ == "__main__":
    unittest.main()
