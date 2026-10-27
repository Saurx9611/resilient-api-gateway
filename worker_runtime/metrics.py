"""Low-cardinality Prometheus metrics shared by one worker process."""

from __future__ import annotations

import os
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

JOB_TYPES = ("image_metadata", "csv_transform", "synthetic_report", "notification_simulation")


class WorkerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.completed = Counter("chronos_jobs_completed_total", "Successfully acknowledged jobs", ("job_type",), registry=self.registry)
        self.failed = Counter("chronos_jobs_failed_total", "Failed handler attempts", ("job_type", "outcome"), registry=self.registry)
        self.retries = Counter("chronos_job_retries_total", "Retries scheduled after failed attempts", ("outcome",), registry=self.registry)
        self.expired = Counter("chronos_expired_leases_total", "Lease generations recovered after expiry", registry=self.registry)
        self.recovered = Counter("chronos_recovered_jobs_total", "Jobs reclaimed after lease expiry", registry=self.registry)
        self.claim_latency = Histogram("chronos_job_claim_latency_seconds", "Time spent in one PostgreSQL claim", ("result",), registry=self.registry)
        self.claim_batch = Histogram("chronos_claim_batch_size", "Jobs claimed during one worker poll", registry=self.registry,
                                     buckets=(0, 1, 2, 4, 8, 16))
        self.queue_wait = Histogram("chronos_job_queue_wait_seconds", "Enqueued-to-handler-start delay", ("job_type",), registry=self.registry,
                                    buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 300))
        self.execution = Histogram("chronos_job_execution_seconds", "Handler wall time", ("job_type",), registry=self.registry,
                                   buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 300))
        self.active = Gauge("chronos_worker_active_jobs", "Currently executing jobs", registry=self.registry)
        self.idle = Gauge("chronos_worker_idle_slots", "Available execution slots", registry=self.registry)
        self.capacity = Gauge("chronos_worker_concurrency", "Configured worker execution slots", registry=self.registry)

    def serve(self, concurrency: int) -> None:
        raw = os.environ.get("CHRONOS_WORKER_METRICS_PORT")
        if not raw:
            return
        port = int(raw)
        if not 1024 <= port <= 65535:
            raise ValueError("CHRONOS_WORKER_METRICS_PORT must be 1024..65535")
        self.capacity.set(concurrency)
        self.idle.set(concurrency)
        start_http_server(port, addr="0.0.0.0", registry=self.registry)

    def set_activity(self, active: int, concurrency: int) -> None:
        self.active.set(active)
        self.idle.set(max(0, concurrency - active))

    def observe_claim(self, started: float, result: str) -> None:
        import time
        self.claim_latency.labels(result=result).observe(time.monotonic() - started)

    def observe_queue(self, job_type: str, seconds: float) -> None:
        if job_type in JOB_TYPES:
            self.queue_wait.labels(job_type=job_type).observe(max(0.0, seconds))

    def observe_execution(self, job_type: str, seconds: float) -> None:
        if job_type in JOB_TYPES:
            self.execution.labels(job_type=job_type).observe(max(0.0, seconds))

    def outcome(self, job_type: str, result: str) -> None:
        if job_type not in JOB_TYPES:
            return
        if result == "succeeded":
            self.completed.labels(job_type=job_type).inc()
        elif result in {"retry", "dead", "timeout", "error"}:
            self.failed.labels(job_type=job_type, outcome=result).inc()


def api_registry() -> CollectorRegistry:
    return CollectorRegistry()
