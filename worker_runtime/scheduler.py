"""Independent, bounded PostgreSQL cron materializer process."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from typing import Any

from .control_repository import ControlRepository
from .repository import DatabaseUnavailable


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, separators=(",", ":")), flush=True)


class Scheduler:
    def __init__(self, dsn: str, *, poll_interval: float = 1.0):
        if not 0.05 <= poll_interval <= 60:
            raise ValueError("poll_interval must be between 0.05 and 60 seconds")
        self.repo = ControlRepository(dsn)
        self.poll_interval = poll_interval
        self.stopping = False
        self._stop_event = threading.Event()

    def stop(self, signum: int, _frame: Any) -> None:
        self.stopping = True
        self._stop_event.set()
        emit("scheduler.draining", signal=signum)

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        backoff = 0.1
        emit("scheduler.starting", poll_interval=self.poll_interval)
        while not self.stopping:
            try:
                job_ids = self.repo.materialize_due(limit=100)
                if job_ids:
                    emit("scheduler.materialized", count=len(job_ids), job_ids=job_ids)
                backoff = 0.1
                self._stop_event.wait(self.poll_interval)
            except DatabaseUnavailable as exc:
                emit("scheduler.database_unavailable", error_code=exc.code, retryable=exc.retryable)
                self._stop_event.wait(backoff)
                backoff = min(5.0, backoff * 2)
            except Exception as exc:
                # Deliberately log only a bounded exception type; payload/SQL aren't logged.
                emit("scheduler.error", error_type=type(exc).__name__)
                self._stop_event.wait(backoff)
                backoff = min(5.0, backoff * 2)
        emit("scheduler.stopped")
        return 0


def main() -> None:
    dsn = os.environ.get("WORKER_DATABASE_URL", "")
    if not dsn:
        print("chronos-scheduler: WORKER_DATABASE_URL must be set", file=sys.stderr)
        raise SystemExit(2)
    try:
        interval = float(os.environ.get("CHRONOS_SCHEDULER_POLL_SECONDS", "1"))
        code = Scheduler(dsn, poll_interval=interval).run()
    except ValueError as exc:
        print(f"chronos-scheduler: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
