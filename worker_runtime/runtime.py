"""Process supervisor: short PostgreSQL transactions and bounded handler children."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any

from .handlers import MAX_RESULT_BYTES, execute
from .repository import DatabaseUnavailable, Job, Repository
from .metrics import WorkerMetrics


def emit(event: str, *, level: str = "info", **fields: Any) -> None:
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "event": event, **fields}
    print(json.dumps(record, ensure_ascii=True, separators=(",", ":")), flush=True)


def _execute_child(job_type: str, payload: dict[str, Any], timeout: float, send: Connection) -> None:
    """Spawn target. The only executable target is this module's static allowlist."""
    try:
        result = {"ok": True, "result": execute(job_type, payload, timeout)}
    except BaseException as exc:
        from .handlers import HandlerTimeout, PermanentJobError

        if isinstance(exc, PermanentJobError):
            code = "invalid_job"
            retryable = False
        elif isinstance(exc, HandlerTimeout):
            code = "handler_timeout"
            retryable = True
        else:
            code = "handler_error"
            retryable = True
        result = {"ok": False, "error_code": code, "retryable": retryable, "timed_out": isinstance(exc, HandlerTimeout)}
    try:
        data = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > MAX_RESULT_BYTES + 1024:
            data = b'{"ok":false,"error_code":"result_too_large","retryable":false,"timed_out":false}'
        try:
            send.send_bytes(data)
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send.close()


@dataclass
class ActiveJob:
    job: Job
    process: multiprocessing.Process
    receive: Connection
    started_at: float
    renew_at: float
    runtime_timeout: float
    execution_started_at: float


class Worker:
    def __init__(
        self,
        dsn: str,
        *,
        name: str = "worker",
        concurrency: int = 2,
        lease_seconds: int = 15,
        handler_timeout: float = 10.0,
        poll_interval: float = 0.1,
        shutdown_grace: float = 10.0,
        worker_id: uuid.UUID | None = None,
    ):
        if not 1 <= concurrency <= 16:
            raise ValueError("concurrency must be 1..16")
        if not 2 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be 2..300")
        if not 0.05 <= handler_timeout <= 300:
            raise ValueError("handler_timeout must be 0.05..300 seconds")
        if not 0.02 <= poll_interval <= 5:
            raise ValueError("poll_interval must be 0.02..5 seconds")
        if not 0.1 <= shutdown_grace <= 300:
            raise ValueError("shutdown_grace must be 0.1..300 seconds")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("worker name must be a 1..64 character safe token")
        self.worker_id = worker_id or uuid.uuid4()
        self.name = name
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.handler_timeout = handler_timeout
        self.poll_interval = poll_interval
        self.shutdown_grace = shutdown_grace
        self.repo = Repository(dsn, self.worker_id, name, concurrency)
        self.metrics = WorkerMetrics()
        self._ctx = multiprocessing.get_context("spawn")
        self._active: dict[int, ActiveJob] = {}
        self._stop = False
        self._signal: str | None = None
        self._drain_started: float | None = None
        self._next_heartbeat = 0.0
        self._db_backoff = 0.1

    def request_shutdown(self, signum: int, _frame: Any) -> None:
        self._stop = True
        self._signal = signal.Signals(signum).name

    def run(self) -> int:
        old_handlers = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, self.request_shutdown)
        try:
            return self._run()
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            self.repo.close()

    def _run(self) -> int:
        self.metrics.serve(self.concurrency)
        self.metrics.capacity.set(self.concurrency)
        self.metrics.set_activity(0, self.concurrency)
        emit("worker.starting", worker_id=str(self.worker_id), name=self.name, concurrency=self.concurrency, pid=os.getpid())
        initialized = False
        while not initialized and not self._stop:
            try:
                self.repo.check_schema()
                self.repo.register()
                initialized = True
                self._db_backoff = 0.1
                emit("worker.started", worker_id=str(self.worker_id), name=self.name)
            except DatabaseUnavailable as exc:
                if not exc.retryable:
                    emit("worker.start_failed", level="error", worker_id=str(self.worker_id), error_code=exc.code)
                    return 1
                emit("database.unavailable", level="warning", worker_id=str(self.worker_id), error_code=exc.code)
                time.sleep(self._db_backoff)
                self._db_backoff = min(2.0, self._db_backoff * 2)
        if not initialized:
            return 0

        while self._active or not self._stop:
            now = time.monotonic()
            if self._stop and self._drain_started is None:
                self._drain_started = now
                try:
                    self.repo.heartbeat("draining")
                except DatabaseUnavailable as exc:
                    self._database_lost(exc)
                emit("worker.draining", worker_id=str(self.worker_id), signal=self._signal, active=len(self._active))

            try:
                self._tick(now)
                self._db_backoff = 0.1
            except DatabaseUnavailable as exc:
                if not exc.retryable:
                    emit("worker.database_error", level="error", worker_id=str(self.worker_id), error_code=exc.code)
                    self._terminate_all("database_error")
                    return 1
                self._database_lost(exc)

            if self._stop and self._active and self._drain_started is not None:
                if time.monotonic() - self._drain_started >= self.shutdown_grace:
                    emit("worker.drain_deadline", level="warning", worker_id=str(self.worker_id), active=len(self._active))
                    self._terminate_all("shutdown_deadline", retry=True)
            if self._stop and not self._active:
                break
            time.sleep(self.poll_interval if not self._stop else min(self.poll_interval, 0.05))

        try:
            self.repo.heartbeat("stopped")
        except DatabaseUnavailable as exc:
            emit("worker.stop_record_failed", level="warning", worker_id=str(self.worker_id), error_code=exc.code)
        emit("worker.stopped", worker_id=str(self.worker_id), signal=self._signal)
        return 0

    def _tick(self, now: float) -> None:
        if now >= self._next_heartbeat:
            self.repo.heartbeat("draining" if self._stop else "active")
            self._next_heartbeat = now + min(5.0, max(0.25, self.lease_seconds / 3))
        self.repo.finalize_expired_cancellations()
        self.repo.abandon_exhausted()

        for job_id, active in list(self._active.items()):
            if active.receive.poll():
                try:
                    raw = active.receive.recv_bytes(MAX_RESULT_BYTES + 1024)
                    result = json.loads(raw)
                    self._reap(active)
                except (EOFError, OSError, ValueError, json.JSONDecodeError):
                    self._reap(active)
                    result = {"ok": False, "error_code": "handler_process_error", "retryable": True, "timed_out": False}
                self._record_result(active.job, result)
                self._active.pop(job_id, None)
                continue
            if not active.process.is_alive():
                active.process.join(timeout=0)
                active.receive.close()
                self._record_result(active.job, {"ok": False, "error_code": "handler_process_error", "retryable": True, "timed_out": False})
                self._active.pop(job_id, None)
                continue
            elapsed = now - active.started_at
            if elapsed >= active.runtime_timeout:
                self._terminate(active)
                self._record_result(active.job, {"ok": False, "error_code": "handler_timeout", "retryable": True, "timed_out": True})
                self._active.pop(job_id, None)
                continue
            if now >= active.renew_at:
                if not self.repo.renew(active.job, self.lease_seconds):
                    self._terminate(active)
                    if self.repo.acknowledge_cancel(active.job):
                        emit("job.canceled", worker_id=str(self.worker_id), job_id=job_id,
                             attempt=active.job.attempts, generation=active.job.generation)
                        self._active.pop(job_id, None)
                        continue
                    self._active.pop(job_id, None)
                    emit("job.lease_lost", level="warning", worker_id=str(self.worker_id), job_id=job_id, attempt=active.job.attempts)
                else:
                    active.renew_at = now + max(0.1, self.lease_seconds / 3)

        claimed_count = 0
        while not self._stop and len(self._active) < self.concurrency:
            claim_started = time.monotonic()
            try:
                job = self.repo.claim(self.lease_seconds)
            except DatabaseUnavailable:
                self.metrics.observe_claim(claim_started, "error")
                raise
            self.metrics.observe_claim(claim_started, "claimed" if job else "empty")
            claim_duration = time.monotonic() - claim_started
            if job is None:
                break
            claimed_count += 1
            self.metrics.observe_queue(job.job_type, job.queue_wait_seconds)
            if job.recovered:
                self.metrics.expired.inc()
                self.metrics.recovered.inc()
            emit("job.claimed", worker_id=str(self.worker_id), job_id=job.id, job_type=job.job_type,
                 attempt=job.attempts, generation=job.generation, claim_duration_ms=round(claim_duration * 1000, 4))
            _test_pause("AFTER_CLAIM")
            receive, send = self._ctx.Pipe(duplex=False)
            runtime_timeout = min(float(job.max_runtime_seconds), self.handler_timeout)
            process = self._ctx.Process(target=_execute_child, args=(job.job_type, job.payload, runtime_timeout, send))
            try:
                process.start()
            except (OSError, RuntimeError):
                receive.close()
                send.close()
                if not self.repo.fail(job, "handler_start_error", retryable=True):
                    emit("job.lease_lost", level="warning", worker_id=str(self.worker_id), job_id=job.id, attempt=job.attempts)
                continue
            send.close()
            try:
                if not self.repo.mark_started(job):
                    self._terminate(ActiveJob(job, process, receive, now, now, runtime_timeout, now))
                    emit("job.lease_lost", level="warning", worker_id=str(self.worker_id), job_id=job.id,
                         attempt=job.attempts, generation=job.generation)
                    continue
            except DatabaseUnavailable:
                self._terminate(ActiveJob(job, process, receive, now, now, runtime_timeout, now))
                raise
            handler_started = time.monotonic()
            active = ActiveJob(job, process, receive, handler_started,
                               handler_started + max(0.1, self.lease_seconds / 3), runtime_timeout, handler_started)
            self._active[job.id] = active
            emit("job.started", worker_id=str(self.worker_id), job_id=job.id, job_type=job.job_type,
                 attempt=job.attempts, generation=job.generation, runtime_timeout=runtime_timeout)

        self.metrics.claim_batch.observe(claimed_count)
        self.metrics.set_activity(len(self._active), self.concurrency)

    def _record_result(self, job: Job, result: dict[str, Any]) -> None:
        active = self._active.get(job.id)
        if active is not None:
            self.metrics.observe_execution(job.job_type, time.monotonic() - active.execution_started_at)
        if result.get("ok") is True:
            if job.job_type == "notification_simulation":
                effect_applied = self.repo.record_notification_effect(job, result["result"])
                if effect_applied is None:
                    if self.repo.acknowledge_cancel(job):
                        emit("job.canceled", worker_id=str(self.worker_id), job_id=job.id,
                             attempt=job.attempts, generation=job.generation)
                        return
                    emit("job.lease_lost", level="warning", worker_id=str(self.worker_id), job_id=job.id,
                         attempt=job.attempts, generation=job.generation)
                    return
                emit("job.effect_applied" if effect_applied else "job.effect_deduplicated",
                     worker_id=str(self.worker_id), job_id=job.id, generation=job.generation,
                     idempotency_key=f"worker-notification-job-{job.id}")
            _test_pause("BEFORE_ACK")
            if self.repo.succeed(job, result["result"]):
                self.metrics.outcome(job.job_type, "succeeded")
                emit("job.succeeded", worker_id=str(self.worker_id), job_id=job.id, job_type=job.job_type, attempt=job.attempts)
            else:
                if self.repo.acknowledge_cancel(job):
                    emit("job.canceled", worker_id=str(self.worker_id), job_id=job.id,
                         attempt=job.attempts, generation=job.generation)
                    return
                emit("job.lease_lost", level="warning", worker_id=str(self.worker_id), job_id=job.id, attempt=job.attempts)
            return
        error_code = str(result.get("error_code", "handler_error"))[:64]
        retryable = result.get("retryable") is True
        recorded = self.repo.fail(job, error_code, retryable=retryable, timed_out=result.get("timed_out") is True)
        if recorded:
            outcome = "timeout" if result.get("timed_out") is True else ("retry" if retryable and job.attempts < job.max_attempts else "dead" if retryable else "error")
            self.metrics.outcome(job.job_type, outcome)
            if retryable:
                self.metrics.retries.labels(outcome="retry" if job.attempts < job.max_attempts else "dead").inc()
            if retryable:
                self.metrics.retries.labels(outcome="retry" if job.attempts < job.max_attempts else "dead").inc()
            emit("job.failed", level="warning", worker_id=str(self.worker_id), job_id=job.id, job_type=job.job_type, attempt=job.attempts, error_code=error_code, retryable=retryable)
        else:
            emit("job.lease_lost", level="warning", worker_id=str(self.worker_id), job_id=job.id, attempt=job.attempts)

    def _reap(self, active: ActiveJob) -> None:
        active.process.join(timeout=0.5)
        if active.process.is_alive():
            active.process.terminate()
            active.process.join(timeout=0.5)
        if active.process.is_alive():
            active.process.kill()
            active.process.join(timeout=0.5)
        active.receive.close()

    def _terminate(self, active: ActiveJob) -> None:
        if active.process.is_alive():
            active.process.terminate()
            active.process.join(timeout=0.5)
            if active.process.is_alive():
                active.process.kill()
                active.process.join(timeout=0.5)
        active.receive.close()

    def _terminate_all(self, reason: str, *, retry: bool = False) -> None:
        for job_id, active in list(self._active.items()):
            self._terminate(active)
            if retry:
                try:
                    self._record_result(active.job, {"ok": False, "error_code": reason, "retryable": True, "timed_out": False})
                except DatabaseUnavailable as exc:
                    emit("job.recovery_deferred", level="warning", worker_id=str(self.worker_id), job_id=job_id, error_code=exc.code)
            self._active.pop(job_id, None)

    def _database_lost(self, exc: DatabaseUnavailable) -> None:
        emit("database.unavailable", level="warning", worker_id=str(self.worker_id), error_code=exc.code, active=len(self._active))
        # A controller unable to renew leases stops work; PostgreSQL expiry recovers it.
        self._terminate_all("database_unavailable")
        time.sleep(self._db_backoff)
        self._db_backoff = min(2.0, self._db_backoff * 2)


def _test_pause(point: str) -> None:
    """Bounded crash-window injection, unavailable outside the disposable test schema."""
    if os.environ.get("WORKER_DB_SCHEMA") != "worker_runtime_test":
        return
    raw = os.environ.get(f"WORKER_TEST_PAUSE_{point}_SECONDS", "")
    if not raw:
        return
    try:
        seconds = float(raw)
    except ValueError:
        return
    if not 0 < seconds <= 4:
        return
    emit("job.test_pause", level="warning", point=point.lower(), duration_seconds=seconds)
    time.sleep(seconds)
