from __future__ import annotations

import os
import psycopg
from psycopg.rows import dict_row

from worker_runtime.repository import migrate


def dsn() -> str:
    value = os.environ.get("WORKER_TEST_DSN", "")
    if not value:
        raise RuntimeError("WORKER_TEST_DSN is required for PostgreSQL integration tests")
    return value


def initialize_test_database() -> None:
    value = dsn()
    with psycopg.connect(value, row_factory=dict_row) as conn:
        db_name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if not db_name.endswith("_test"):
            raise RuntimeError(f"refusing integration tests outside *_test database (current database: {db_name})")
        conn.execute("CREATE SCHEMA IF NOT EXISTS worker_runtime_test")
    os.environ["WORKER_DB_SCHEMA"] = "worker_runtime_test"
    os.environ["WORKER_DATABASE_URL"] = value
    migrate(value)


def connection(*, application_name: str = "worker-integration-test") -> psycopg.Connection:
    return psycopg.connect(dsn(), application_name=application_name, options="-c search_path=worker_runtime_test,public", row_factory=dict_row)


def clear_database() -> None:
    with connection() as conn:
        conn.execute("TRUNCATE worker_job_events,worker_jobs,worker_processes,worker_schedule_events,worker_schedule_occurrences,worker_schedules,worker_api_rate_limits RESTART IDENTITY CASCADE")
        conn.execute("UPDATE worker_queue_policy SET default_max_attempts=5,default_max_runtime_seconds=30,updated_by='system',updated_at=clock_timestamp() WHERE singleton=true")


if os.environ.get("WORKER_TEST_DSN"):
    initialize_test_database()
