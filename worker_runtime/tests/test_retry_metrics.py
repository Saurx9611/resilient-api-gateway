from __future__ import annotations

import unittest
import uuid

from prometheus_client import generate_latest

from worker_runtime.metrics import WorkerMetrics
from worker_runtime.repository import Job
from worker_runtime.runtime import Worker


class _RecordingRepository:
    def fail(self, job: Job, error_code: str, *, retryable: bool, timed_out: bool = False) -> bool:
        return True


class RetryMetricTests(unittest.TestCase):
    def test_one_scheduled_retry_increments_counter_once(self) -> None:
        worker = Worker.__new__(Worker)
        worker._active = {}
        worker.metrics = WorkerMetrics()
        worker.repo = _RecordingRepository()
        worker.worker_id = uuid.uuid4()
        job = Job(1, "synthetic_report", {}, 1, 3, 30, 1, uuid.uuid4(), None)

        worker._record_result(job, {"ok": False, "retryable": True, "error_code": "temporary"})

        metric = next(sample for family in worker.metrics.registry.collect()
                      for sample in family.samples
                      if sample.name == "chronos_job_retries_total")
        self.assertEqual(1.0, metric.value)


if __name__ == "__main__":
    unittest.main()
