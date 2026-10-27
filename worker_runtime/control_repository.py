"""Transactional read/write operations for the HTTP API and cron scheduler."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout
from psycopg.rows import dict_row

from .repository import DatabaseUnavailable, Repository
from .handlers import validate_payload
from .scheduling import next_occurrence, validate_schedule

UTC = timezone.utc


class ControlRepository:
    def __init__(self, dsn: str, *, pool_min_size: int = 1, pool_max_size: int = 8):
        if not 1 <= pool_min_size <= pool_max_size <= 32:
            raise ValueError("database pool bounds must satisfy 1 <= min <= max <= 32")
        self.dsn = dsn
        self.pool_max_size = pool_max_size
        self._pool_lock = threading.Lock()
        self._pool_open = False
        schema = os.environ.get("WORKER_DB_SCHEMA", "")
        options = "-c statement_timeout=3000 -c lock_timeout=1000"
        if schema:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
                raise ValueError("invalid test schema")
            options += f" -c search_path={schema},public"
        self.pool = ConnectionPool(
            conninfo=dsn, min_size=pool_min_size, max_size=pool_max_size,
            timeout=1.0, open=False,
            kwargs={"connect_timeout": 2, "application_name": "chronos-control-api", "options": options, "row_factory": dict_row},
        )

    def close(self) -> None:
        if self._pool_open:
            self.pool.close()

    def _ensure_pool_open(self) -> None:
        if self._pool_open:
            return
        with self._pool_lock:
            if not self._pool_open:
                self.pool.open(wait=False)
                self._pool_open = True

    def pool_stats(self) -> dict[str, int]:
        self._ensure_pool_open()
        stats = self.pool.get_stats()
        return {key: int(stats.get(key, 0)) for key in ("pool_size", "pool_available", "requests_waiting", "requests_queued", "requests_wait_ms")} | {"pool_max_size": self.pool_max_size}

    def live_metrics(self) -> dict[str, Any]:
        with self._transaction() as conn:
            states = conn.execute("SELECT state,count(*) AS count FROM worker_jobs GROUP BY state").fetchall()
            workers = conn.execute(
                """SELECT count(*) FILTER (WHERE state='active' AND fresh AND running>0) AS active,
                          count(*) FILTER (WHERE state='active' AND fresh AND running=0) AS idle
                   FROM (SELECT w.state,w.heartbeat_at>=clock_timestamp()-interval '15 seconds' AS fresh,
                                (SELECT count(*) FROM worker_jobs j WHERE j.state='running' AND j.worker_id=w.worker_id) AS running
                         FROM worker_processes w) observed"""
            ).fetchone()
            recent = conn.execute(
                """SELECT count(*) FILTER (WHERE event_type='retry_scheduled') AS retries,
                          count(*) FILTER (WHERE event_type='lease_reclaimed' AND details->>'reason'='expired_lease') AS expired,
                          count(*) FILTER (WHERE event_type='lease_reclaimed') AS recovered
                   FROM worker_job_events WHERE occurred_at>=clock_timestamp()-interval '24 hours'"""
            ).fetchone()
        return {"states": {row["state"]: int(row["count"]) for row in states}, "workers": workers, "recent": recent}

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection]:
        try:
            self._ensure_pool_open()
            with self.pool.connection(timeout=1.0) as conn:
                with conn.transaction():
                    yield conn
        except PoolTimeout:
            raise DatabaseUnavailable("DatabasePoolTimeout", retryable=True) from None
        except psycopg.Error as exc:
            state = exc.sqlstate or "no_sqlstate"
            retryable = isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)) or state.startswith(("08", "40")) or state in {"55P03", "57014", "57P01", "57P02", "57P03"}
            raise DatabaseUnavailable(f"{type(exc).__name__}:{state}", retryable=retryable) from None

    @contextmanager
    def _worker_repository(self, name: str) -> Iterator[Repository]:
        """Adapt worker repository operations to the same bounded API pool."""
        try:
            self._ensure_pool_open()
            with self.pool.connection(timeout=1.0) as conn:
                repo = Repository(self.dsn, uuid.uuid4(), name, 1)
                repo.conn = conn
                try:
                    yield repo
                finally:
                    # The pool owns this connection; do not close it on success.
                    repo.conn = None
        except PoolTimeout:
            raise DatabaseUnavailable("DatabasePoolTimeout", retryable=True) from None

    def check_schema(self) -> None:
        with self._transaction() as conn:
            row = conn.execute("SELECT max(version) AS version FROM worker_schema").fetchone()
            if not row or row["version"] < 6:
                raise DatabaseUnavailable("SchemaVersionMismatch")

    def consume_rate_limit(self, principal: str, limit: int, window_seconds: int = 60) -> tuple[bool, int, datetime]:
        with self._transaction() as conn:
            row = conn.execute(
                """WITH bucket AS (
                       SELECT date_bin(%s * interval '1 second', clock_timestamp(), timestamptz '2000-01-01') AS start
                   )
                   INSERT INTO worker_api_rate_limits(principal,window_start,request_count)
                   SELECT %s,start,1 FROM bucket
                   ON CONFLICT (principal) DO UPDATE SET
                       window_start=EXCLUDED.window_start,
                       request_count=CASE WHEN worker_api_rate_limits.window_start=EXCLUDED.window_start
                           THEN worker_api_rate_limits.request_count+1 ELSE 1 END
                   RETURNING request_count,window_start""",
                (window_seconds, principal),
            ).fetchone()
            return row["request_count"] <= limit, max(0, limit - row["request_count"]), row["window_start"]

    def enqueue(self, *, job_type: str, payload: dict[str, Any], max_attempts: int,
                max_runtime_seconds: int, available_at: datetime | None, priority: int,
                idempotency_key: str | None, submitted_by: str, request_fingerprint: str) -> int:
        with self._worker_repository("api-submitter") as repo:
            return repo.enqueue(job_type, payload, max_attempts,
                                max_runtime_seconds=max_runtime_seconds,
                                idempotency_key=idempotency_key, available_at=available_at,
                                priority=priority, submitted_by=submitted_by,
                                request_fingerprint=request_fingerprint)

    def job(self, job_id: int) -> dict[str, Any] | None:
        with self._transaction() as conn:
            return conn.execute(
                """SELECT id,job_type,payload,state,attempts,max_attempts,max_runtime_seconds,available_at,
                          priority,worker_id,lease_until,generation,replay_of,result,last_error_code,
                          cancel_requested_at,submitted_by,created_at,updated_at
                   FROM worker_jobs WHERE id=%s""", (job_id,)
            ).fetchone()

    def page_jobs(self, *, limit: int, cursor: int | None, state: str | None,
                  job_type: str | None, created_after: datetime | None,
                  created_before: datetime | None, search: str | None = None) -> tuple[list[dict[str, Any]], int | None]:
        clauses = ["TRUE"]
        params: list[Any] = []
        if cursor is not None:
            clauses.append("id < %s")
            params.append(cursor)
        if state is not None:
            clauses.append("state = %s")
            params.append(state)
        if job_type is not None:
            clauses.append("job_type = %s")
            params.append(job_type)
        if created_after is not None:
            clauses.append("created_at >= %s")
            params.append(created_after)
        if created_before is not None:
            clauses.append("created_at < %s")
            params.append(created_before)
        if search:
            clauses.append("(id::text ILIKE %s OR job_type ILIKE %s OR coalesce(last_error_code,'') ILIKE %s)")
            pattern = f"%{search}%"
            params.extend((pattern, pattern, pattern))
        params.append(limit + 1)
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT id,job_type,state,attempts,max_attempts,available_at,priority,
                           worker_id,lease_until,last_error_code,replay_of,created_at,updated_at
                    FROM worker_jobs WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT %s""", params
            ).fetchall()
        next_cursor = rows[limit - 1]["id"] if len(rows) > limit else None
        return rows[:limit], next_cursor

    def page_events(self, job_id: int, *, limit: int, cursor: int | None) -> tuple[list[dict[str, Any]], int | None]:
        cursor_clause = "id > %s" if cursor is not None else "TRUE"
        params: tuple[Any, ...] = (job_id, cursor, limit + 1) if cursor is not None else (job_id, limit + 1)
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT id,event_type,attempt,worker_id,generation,occurred_at,details
                   FROM worker_job_events WHERE job_id=%s AND {cursor_clause}
                   ORDER BY id LIMIT %s""", params
            ).fetchall()
        next_cursor = rows[limit - 1]["id"] if len(rows) > limit else None
        return rows[:limit], next_cursor

    def cancel_job(self, job_id: int, reason: str) -> dict[str, Any] | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT state,cancel_requested_at,attempts,generation FROM worker_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if row is None:
                return None
            if row["state"] == "queued":
                conn.execute("UPDATE worker_jobs SET state='canceled',updated_at=clock_timestamp() WHERE id=%s", (job_id,))
                conn.execute("INSERT INTO worker_job_events(job_id,event_type,attempt,generation,details) VALUES (%s,'canceled',%s,%s,%s::jsonb)",
                             (job_id, row["attempts"], row["generation"], json.dumps({"reason": reason})))
                return {"state": "canceled", "cancel_requested": False}
            if row["state"] == "running" and row["cancel_requested_at"] is None:
                conn.execute("UPDATE worker_jobs SET cancel_requested_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s", (job_id,))
                conn.execute("INSERT INTO worker_job_events(job_id,event_type,attempt,generation,details) VALUES (%s,'cancel_requested',%s,%s,%s::jsonb)",
                             (job_id, row["attempts"], row["generation"], json.dumps({"reason": reason, "reason_code": "operator_requested"})))
                return {"state": "running", "cancel_requested": True}
            return {"state": row["state"], "cancel_requested": row["cancel_requested_at"] is not None}

    def retry(self, job_id: int, reason: str) -> bool:
        with self._worker_repository("api-operator") as repo:
            return repo.retry_dead(job_id, reason)

    def replay(self, job_id: int, reason: str, *, idempotency_key: str | None = None,
               request_fingerprint: str | None = None) -> int | None:
        with self._worker_repository("api-operator") as repo:
            return repo.replay(job_id, reason, idempotency_key=idempotency_key,
                               request_fingerprint=request_fingerprint)

    def workers(self, stale_after_seconds: int) -> list[dict[str, Any]]:
        if not 1 <= stale_after_seconds <= 3600:
            raise ValueError("stale_after_seconds must be 1..3600")
        with self._transaction() as conn:
            return conn.execute(
                """SELECT w.worker_id,w.name,w.pid,w.hostname,w.concurrency,w.state,w.started_at,w.heartbeat_at,
                          w.heartbeat_at < clock_timestamp() - (%s * interval '1 second') AS stale,
                          (SELECT count(*) FROM worker_jobs j WHERE j.state='running' AND j.worker_id=w.worker_id) AS running_jobs
                   FROM worker_processes w ORDER BY w.started_at DESC,w.worker_id LIMIT 500""",
                (stale_after_seconds,),
            ).fetchall()

    def queue_stats(self) -> dict[str, Any]:
        with self._transaction() as conn:
            states = conn.execute("SELECT state,count(*) AS count FROM worker_jobs GROUP BY state ORDER BY state").fetchall()
            row = conn.execute(
                """SELECT count(*) FILTER (WHERE state='queued' AND available_at<=clock_timestamp()) AS ready,
                          count(*) FILTER (WHERE state='queued' AND available_at>clock_timestamp()) AS delayed,
                          count(*) FILTER (WHERE state='running') AS running,
                          min(created_at) FILTER (WHERE state='queued') AS oldest_queued_at,
                          count(*) FILTER (WHERE state='queued') AS queued
                   FROM worker_jobs"""
            ).fetchone()
            return {"states": {item["state"]: item["count"] for item in states}, **row}

    def recent_events(self, *, limit: int, cursor: int | None) -> tuple[list[dict[str, Any]], int | None]:
        clauses = ["TRUE"]
        params: list[Any] = []
        if cursor is not None:
            clauses.append("e.id < %s")
            params.append(cursor)
        params.append(limit + 1)
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT e.id,e.job_id,e.event_type,e.attempt,e.worker_id,e.generation,
                           e.occurred_at,e.details,j.job_type
                    FROM worker_job_events e JOIN worker_jobs j ON j.id=e.job_id
                    WHERE {' AND '.join(clauses)} ORDER BY e.id DESC LIMIT %s""", params
            ).fetchall()
        next_cursor = rows[limit - 1]["id"] if len(rows) > limit else None
        return rows[:limit], next_cursor

    def observability(self, hours: int) -> dict[str, Any]:
        if hours not in {24, 168}:
            raise ValueError("hours must be 24 or 168")
        interval = "1 hour" if hours == 24 else "1 day"
        with self._transaction() as conn:
            buckets = conn.execute(
                f"""WITH bounds AS (
                         SELECT clock_timestamp() AS now,
                           date_bin('{interval}'::interval, clock_timestamp() - (%s * interval '1 hour'), timestamptz '2000-01-01') AS start
                       ), series AS (
                         SELECT generate_series(start, date_bin('{interval}'::interval, now, timestamptz '2000-01-01'), '{interval}'::interval) AS bucket
                         FROM bounds
                       ), counts AS (
                         SELECT date_bin('{interval}'::interval,e.occurred_at,timestamptz '2000-01-01') AS bucket,
                           count(*) FILTER (WHERE e.event_type='succeeded') AS succeeded,
                           count(*) FILTER (WHERE e.event_type IN ('abandoned','permanent_failure','timed_out')) AS failed,
                           count(*) FILTER (WHERE e.event_type='retry_scheduled') AS retried,
                           count(*) FILTER (WHERE e.event_type='enqueued') AS submitted
                         FROM worker_job_events e,bounds b
                         WHERE e.occurred_at >= b.start AND e.occurred_at <= b.now
                         GROUP BY 1
                       )
                       SELECT s.bucket,coalesce(c.succeeded,0) AS succeeded,
                         coalesce(c.failed,0) AS failed,coalesce(c.retried,0) AS retried,
                         coalesce(c.submitted,0) AS submitted
                       FROM series s LEFT JOIN counts c USING(bucket) ORDER BY s.bucket""", (hours,)
            ).fetchall()
            latency = conn.execute(
                """WITH successes AS (
                       SELECT job_id,generation,occurred_at
                       FROM worker_job_events
                       WHERE event_type='succeeded' AND occurred_at >= clock_timestamp() - (%s * interval '1 hour')
                   ), durations AS (
                       SELECT extract(epoch FROM s.occurred_at - min(c.occurred_at))*1000 AS ms
                       FROM successes s JOIN worker_job_events c
                         ON c.job_id=s.job_id AND c.generation=s.generation AND c.event_type='claimed'
                         AND c.occurred_at<=s.occurred_at
                       GROUP BY s.job_id,s.generation,s.occurred_at
                   )
                   SELECT count(*) AS samples, percentile_cont(0.50) WITHIN GROUP (ORDER BY ms) AS p50_ms,
                     percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) AS p95_ms,
                     percentile_cont(0.99) WITHIN GROUP (ORDER BY ms) AS p99_ms
                   FROM durations""", (hours,)
            ).fetchone()
            totals = conn.execute(
                """SELECT count(*) FILTER (WHERE event_type='succeeded') AS succeeded,
                          count(*) FILTER (WHERE event_type IN ('abandoned','permanent_failure','timed_out')) AS failed,
                          count(*) FILTER (WHERE event_type='retry_scheduled') AS retried
                   FROM worker_job_events WHERE occurred_at >= clock_timestamp() - (%s * interval '1 hour')""", (hours,)
            ).fetchone()
            terminal = totals["succeeded"] + totals["failed"]
            rate = (totals["failed"] / terminal * 100.0) if terminal else None
            operations = conn.execute(
                """WITH job_states AS (
                       SELECT count(*) FILTER (WHERE state='queued') AS pending,
                              count(*) FILTER (WHERE state='running') AS running,
                              count(*) FILTER (WHERE state='succeeded') AS completed,
                              count(*) FILTER (WHERE state='dead') AS failed,
                              count(*) FILTER (WHERE state='dead') AS dead_letter
                       FROM worker_jobs
                   ), worker_load AS (
                       SELECT count(*) FILTER (WHERE w.state='active' AND w.heartbeat_at>=clock_timestamp()-interval '15 seconds'
                                                   AND coalesce(j.running,0)>0) AS active_workers,
                              count(*) FILTER (WHERE w.state='active' AND w.heartbeat_at>=clock_timestamp()-interval '15 seconds'
                                                   AND coalesce(j.running,0)=0) AS idle_workers
                       FROM worker_processes w LEFT JOIN LATERAL
                           (SELECT count(*) AS running FROM worker_jobs WHERE state='running' AND worker_id=w.worker_id) j ON true
                   ), recent AS (
                       SELECT count(*) FILTER (WHERE event_type='succeeded' AND occurred_at>=clock_timestamp()-interval '60 seconds') AS completed_last_minute,
                              count(*) FILTER (WHERE event_type='retry_scheduled' AND occurred_at>=clock_timestamp()-interval '24 hours') AS retries_24h,
                              count(*) FILTER (WHERE event_type='lease_reclaimed' AND occurred_at>=clock_timestamp()-interval '24 hours') AS recovered_24h,
                              count(*) FILTER (WHERE event_type='lease_reclaimed' AND occurred_at>=clock_timestamp()-interval '24 hours'
                                               AND details->>'reason'='expired_lease') AS expired_leases_24h,
                              count(*) FILTER (WHERE event_type IN ('succeeded','retry_scheduled','timed_out','permanent_failure','abandoned')
                                               AND occurred_at>=clock_timestamp()-interval '24 hours') AS attempts_24h
                       FROM worker_job_events
                   )
                   SELECT job_states.*, worker_load.active_workers,worker_load.idle_workers,
                          recent.completed_last_minute/60.0 AS jobs_completed_per_second,
                          recent.retries_24h,recent.recovered_24h,recent.expired_leases_24h,
                          CASE WHEN recent.attempts_24h=0 THEN NULL ELSE recent.retries_24h::double precision/recent.attempts_24h END AS retry_rate_24h
                   FROM job_states CROSS JOIN worker_load CROSS JOIN recent"""
            ).fetchone()
            timing = conn.execute(
                """WITH claims AS (
                       SELECT job_id,generation,attempt,occurred_at FROM worker_job_events
                       WHERE event_type IN ('claimed','lease_reclaimed') AND occurred_at>=clock_timestamp()-(%s*interval '1 hour')
                   ), starts AS (
                       SELECT job_id,generation,attempt,occurred_at,details FROM worker_job_events
                       WHERE event_type='started' AND occurred_at>=clock_timestamp()-(%s*interval '1 hour')
                   ), ends AS (
                       SELECT job_id,generation,attempt,occurred_at FROM worker_job_events
                       WHERE event_type IN ('succeeded','retry_scheduled','timed_out','permanent_failure','abandoned')
                         AND occurred_at>=clock_timestamp()-(%s*interval '1 hour')
                   ), enqueued AS (
                       SELECT job_id,occurred_at FROM worker_job_events
                       WHERE event_type='enqueued' AND occurred_at>=clock_timestamp()-(%s*interval '1 hour')
                   ), samples AS (
                       SELECT s.details,
                              extract(epoch FROM (s.occurred_at-q.occurred_at))*1000 AS queue_ms,
                              extract(epoch FROM (e.occurred_at-s.occurred_at))*1000 AS execution_ms
                       FROM starts s JOIN claims c USING(job_id,generation,attempt)
                       JOIN enqueued q USING(job_id)
                       JOIN ends e USING(job_id,generation,attempt) WHERE e.occurred_at>=s.occurred_at AND s.occurred_at>=q.occurred_at
                   ) SELECT count(*) AS samples,
                       percentile_cont(0.50) WITHIN GROUP(ORDER BY queue_ms) AS queue_p50_ms,
                       percentile_cont(0.95) WITHIN GROUP(ORDER BY queue_ms) AS queue_p95_ms,
                       percentile_cont(0.99) WITHIN GROUP(ORDER BY queue_ms) AS queue_p99_ms,
                       percentile_cont(0.50) WITHIN GROUP(ORDER BY execution_ms) AS execution_p50_ms,
                       percentile_cont(0.95) WITHIN GROUP(ORDER BY execution_ms) AS execution_p95_ms,
                       percentile_cont(0.99) WITHIN GROUP(ORDER BY execution_ms) AS execution_p99_ms,
                       percentile_cont(0.50) WITHIN GROUP(ORDER BY (details->>'claim_latency_seconds')::double precision)*1000 AS claim_p50_ms,
                       percentile_cont(0.95) WITHIN GROUP(ORDER BY (details->>'claim_latency_seconds')::double precision)*1000 AS claim_p95_ms,
                       percentile_cont(0.99) WITHIN GROUP(ORDER BY (details->>'claim_latency_seconds')::double precision)*1000 AS claim_p99_ms
                   FROM samples""", (hours, hours, hours, hours)
            ).fetchone()
            operations.update(timing)
            operations["window_hours"] = hours
            operations["pool"] = self.pool_stats()
            return {"hours": hours, "bucket_size": "hour" if hours == 24 else "day",
                    "buckets": buckets, "latency": {**latency, "unit": "ms", "definition": "claimed-to-success event duration by lease generation"},
                    "totals": {**totals, "failure_rate_percent": rate}, "operations": operations}

    def queue_policy(self) -> dict[str, Any]:
        with self._transaction() as conn:
            return conn.execute(
                """SELECT default_max_attempts,default_max_runtime_seconds,updated_by,updated_at
                   FROM worker_queue_policy WHERE singleton=true"""
            ).fetchone()

    def update_queue_policy(self, *, max_attempts: int, max_runtime_seconds: int, actor: str) -> dict[str, Any]:
        with self._transaction() as conn:
            return conn.execute(
                """UPDATE worker_queue_policy SET default_max_attempts=%s,
                          default_max_runtime_seconds=%s,updated_by=%s,updated_at=clock_timestamp()
                   WHERE singleton=true
                   RETURNING default_max_attempts,default_max_runtime_seconds,updated_by,updated_at""",
                (max_attempts,max_runtime_seconds,actor),
            ).fetchone()

    def create_schedule(self, *, job_type: str, payload: dict[str, Any], cron_expression: str,
                        timezone_name: str, priority: int, max_attempts: int,
                        max_runtime_seconds: int, created_by: str) -> dict[str, Any]:
        payload = validate_payload(job_type, payload)
        validate_schedule(cron_expression, timezone_name)
        with self._transaction() as conn:
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            next_at = next_occurrence(cron_expression, timezone_name, now)
            schedule_id = uuid.uuid4()
            row = conn.execute(
                """INSERT INTO worker_schedules(schedule_id,job_type,payload,cron_expression,timezone,
                           priority,max_attempts,max_runtime_seconds,next_occurrence_at,created_by)
                   VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING schedule_id,job_type,cron_expression,timezone,priority,max_attempts,
                       max_runtime_seconds,enabled,next_occurrence_at,created_by,created_at""",
                (schedule_id, job_type, json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                 cron_expression, timezone_name, priority, max_attempts, max_runtime_seconds, next_at, created_by),
            ).fetchone()
            conn.execute("INSERT INTO worker_schedule_events(schedule_id,event_type,actor,details) VALUES (%s,'created',%s,%s::jsonb)",
                         (schedule_id, created_by, json.dumps({"cron_expression": cron_expression, "timezone": timezone_name})))
            return row

    def schedule(self, schedule_id: uuid.UUID) -> dict[str, Any] | None:
        with self._transaction() as conn:
            return conn.execute(
                """SELECT schedule_id,job_type,payload,cron_expression,timezone,priority,max_attempts,
                          max_runtime_seconds,enabled,next_occurrence_at,created_by,created_at,updated_at
                   FROM worker_schedules WHERE schedule_id=%s""", (schedule_id,)
            ).fetchone()

    def list_schedules(self, limit: int) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            return conn.execute(
                """SELECT schedule_id,job_type,cron_expression,timezone,priority,enabled,
                          next_occurrence_at,created_by,created_at FROM worker_schedules
                   ORDER BY created_at DESC,schedule_id DESC LIMIT %s""", (limit,)
            ).fetchall()

    def cancel_schedule(self, schedule_id: uuid.UUID, actor: str, reason: str) -> dict[str, Any] | None:
        with self._transaction() as conn:
            row = conn.execute("UPDATE worker_schedules SET enabled=false,updated_at=clock_timestamp() WHERE schedule_id=%s AND enabled=true RETURNING schedule_id", (schedule_id,)).fetchone()
            if row is None:
                return conn.execute(
                    """SELECT schedule_id,job_type,payload,cron_expression,timezone,priority,max_attempts,
                              max_runtime_seconds,enabled,next_occurrence_at,created_by,created_at,updated_at
                       FROM worker_schedules WHERE schedule_id=%s""", (schedule_id,)
                ).fetchone()
            conn.execute("INSERT INTO worker_schedule_events(schedule_id,event_type,actor,details) VALUES (%s,'disabled',%s,%s::jsonb)",
                         (schedule_id, actor, json.dumps({"reason": reason})))
            return conn.execute(
                """SELECT schedule_id,job_type,payload,cron_expression,timezone,priority,max_attempts,
                          max_runtime_seconds,enabled,next_occurrence_at,created_by,created_at,updated_at
                   FROM worker_schedules WHERE schedule_id=%s""", (schedule_id,)
            ).fetchone()

    def materialize_due(self, *, limit: int = 100) -> list[int]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._transaction() as conn:
            due = conn.execute(
                """SELECT schedule_id,job_type,payload,cron_expression,timezone,priority,max_attempts,
                          max_runtime_seconds,next_occurrence_at,created_by
                   FROM worker_schedules WHERE enabled AND next_occurrence_at<=clock_timestamp()
                   ORDER BY next_occurrence_at,schedule_id FOR UPDATE SKIP LOCKED LIMIT %s""", (limit,)
            ).fetchall()
            job_ids: list[int] = []
            for schedule in due:
                occurrence = schedule["next_occurrence_at"]
                created = conn.execute(
                    """INSERT INTO worker_jobs(job_type,payload,max_attempts,max_runtime_seconds,
                               available_at,priority,submitted_by)
                       VALUES (%s,%s::jsonb,%s,%s,%s,%s,%s) RETURNING id""",
                    (schedule["job_type"], json.dumps(schedule["payload"], ensure_ascii=True, separators=(",", ":")),
                     schedule["max_attempts"], schedule["max_runtime_seconds"], occurrence,
                     schedule["priority"], schedule["created_by"]),
                ).fetchone()
                job_id = created["id"]
                conn.execute("INSERT INTO worker_schedule_occurrences(schedule_id,occurrence_at,job_id) VALUES (%s,%s,%s)",
                             (schedule["schedule_id"], occurrence, job_id))
                conn.execute(
                    """INSERT INTO worker_job_events(job_id,event_type,attempt,generation,details)
                       VALUES (%s,'scheduled',0,0,%s::jsonb)""",
                    (job_id, json.dumps({"schedule_id": str(schedule["schedule_id"]), "occurrence_at": occurrence.isoformat()})),
                )
                following = next_occurrence(schedule["cron_expression"], schedule["timezone"], occurrence)
                conn.execute("UPDATE worker_schedules SET next_occurrence_at=%s,updated_at=clock_timestamp() WHERE schedule_id=%s",
                             (following, schedule["schedule_id"]))
                conn.execute("INSERT INTO worker_schedule_events(schedule_id,event_type,actor,details) VALUES (%s,'materialized',%s,%s::jsonb)",
                             (schedule["schedule_id"], "scheduler", json.dumps({"job_id": job_id, "occurrence_at": occurrence.isoformat()})))
                job_ids.append(job_id)
            return job_ids
