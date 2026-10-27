from __future__ import annotations

import unittest

from prometheus_client import generate_latest

from worker_runtime.metrics import WorkerMetrics


class WorkerMetricTests(unittest.TestCase):
    def test_export_is_prometheus_format_with_bounded_labels(self) -> None:
        metrics = WorkerMetrics()
        metrics.completed.labels(job_type="synthetic_report").inc()
        metrics.failed.labels(job_type="synthetic_report", outcome="timeout").inc()
        metrics.retries.labels(outcome="retry").inc()
        metrics.claim_latency.labels(result="claimed").observe(0.012)
        metrics.claim_batch.observe(1)
        metrics.observe_queue("synthetic_report", 0.25)
        metrics.observe_execution("synthetic_report", 0.5)
        metrics.set_activity(1, 2)

        output = generate_latest(metrics.registry).decode("utf-8")
        self.assertIn("# TYPE chronos_jobs_completed_total counter", output)
        self.assertIn('job_type="synthetic_report"', output)
        self.assertIn('outcome="timeout"', output)
        self.assertIn("chronos_job_claim_latency_seconds_bucket", output)
        self.assertIn("chronos_claim_batch_size_bucket", output)
        self.assertIn("chronos_worker_idle_slots 1.0", output)
        self.assertNotIn("job_id", output)

    def test_unknown_job_type_never_creates_a_metric_label(self) -> None:
        metrics = WorkerMetrics()
        metrics.outcome("user-input-that-is-not-allowlisted", "succeeded")
        metrics.observe_queue("user-input-that-is-not-allowlisted", 0.1)
        output = generate_latest(metrics.registry).decode("utf-8")
        self.assertNotIn("user-input", output)


if __name__ == "__main__":
    unittest.main()
