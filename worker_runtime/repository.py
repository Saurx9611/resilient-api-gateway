"""Short, fenced PostgreSQL transactions for the worker control plane."""

from __future__ import annotations

import json
import hashlib
import os
import re
import socket
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .handlers import MAX_PAYLOAD_BYTES, MAX_RESULT_BYTES, validate_payload

_SCHEMA = Path(__file__).with_name("schema.sql")


def _retryable_database_error(exc: psycopg.Error) -> bool:
    state = getattr(exc, "sqlstate", None) or ""
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)) or state.startswith(("08", "40")) or state in {"55P03", "57014", "57P01", "57P02", "57P03"}


class DatabaseUnavailable(RuntimeError):
    """Database operation failed. Details intentionally omit DSN and SQL values."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Job:
    id: int
    job_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    max_runtime_seconds: int
    generation: int
    lease_token: uuid.UUID
    lease_until: Any
    created_at: Any
    queue_wait_seconds: float
    recovered: bool = False


def connect(dsn: str, *, application_name: str) -> psycopg.Connection:
    schema = os.environ.get("WORKER_DB_SCHEMA", "")
    if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
        raise DatabaseUnavailable("InvalidTestSchema")
    options = "-c statement_timeout=3000 -c lock_timeout=1000"
    if schema:
        options += f" -c search_path={schema},public"
    try:
        return psycopg.connect(
            dsn,
            connect_timeout=2,
            application_name=application_name[:63],
            options=options,
            row_factory=dict_row,
        )
    except psycopg.Error as exc:
        raise DatabaseUnavailable(type(exc).__name__, retryable=_retryable_database_error(exc)) from None


def migrate(dsn: str) -> None:
    conn = connect(dsn, application_name="gateway-worker-migrate")
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('gateway-worker-schema-v1'))")
            conn.execute(_SCHEMA.read_text(encoding="utf-8"))
    except psycopg.Error as exc:
        raise DatabaseUnavailable(type(exc).__name__, retryable=_retryable_database_error(exc)) from None
    finally:
        conn.close()


class Repository:
    def __init__(self, dsn: str, worker_id: uuid.UUID, name: str, concurrency: int):
        self.dsn = dsn
        self.worker_id = worker_id
        self.name = name
        self.concurrency = concurrency
        self.conn: psycopg.Connection | None = None

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None

    def _connection(self) -> psycopg.Connection:
        if self.conn is None or self.conn.closed:
            self.conn = connect(self.dsn, application_name=f"worker-{self.worker_id}")
        return self.conn

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection]:
        conn = self._connection()
        try:
            with conn.transaction():
                yield conn
        except psycopg.Error as exc:
            self.close()
            code = f"{type(exc).__name__}:{exc.sqlstate or 'no_sqlstate'}"
            raise DatabaseUnavailable(code, retryable=_retryable_database_error(exc)) from None

    def check_schema(self) -> None:
        with self._transaction() as conn:
            row = conn.execute("SELECT max(version) AS version FROM worker_schema").fetchone()
            if not row or row["version"] < 3:
                raise DatabaseUnavailable("SchemaVersionMismatch")

    def register(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO worker_processes(worker_id,name,pid,hostname,concurrency,state)
                   VALUES (%s,%s,%s,%s,%s,'active')
                   ON CONFLICT(worker_id) DO UPDATE SET state='active', heartbeat_at=clock_timestamp()""",
                (self.worker_id, self.name, os.getpid(), socket.gethostname()[:255], self.concurrency),
            )

    def heartbeat(self, state: str = "active") -> None:
        with self._transaction() as conn:
            row = conn.execute(
                "UPDATE worker_processes SET state=%s, heartbeat_at=clock_timestamp() WHERE worker_id=%s RETURNING worker_id",
                (state, self.worker_id),
            ).fetchone()
            if row is None:
                raise DatabaseUnavailable("WorkerRegistrationMissing")

    def enqueue(
        self,
        job_type: str,
        payload: Any,
        max_attempts: int = 5,
        *,
        max_runtime_seconds: int = 30,
        idempotency_key: str | None = None,
        available_at: Any | None = None,
        priority: int = 0,
        submitted_by: str | None = None,
        request_fingerprint: str | None = None,
    ) -> int:
        valid = validate_payload(job_type, payload)
        raw = json.dumps(valid, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > MAX_PAYLOAD_BYTES or not 1 <= max_attempts <= 10:
            raise ValueError("payload exceeds limit or max_attempts is outside 1..10")
        if not 1 <= max_runtime_seconds <= 300:
            raise ValueError("max_runtime_seconds must be 1..300")
        if not isinstance(priority, int) or isinstance(priority, bool) or not -10 <= priority <= 10:
            raise ValueError("priority must be between -10 and 10")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key)
        ):
            raise ValueError("idempotency_key must be a 1..128 character safe token")
        canonical = json.dumps({
            "job_type": job_type, "payload": valid, "max_attempts": max_attempts,
            "max_runtime_seconds": max_runtime_seconds, "priority": priority,
            "available_at": available_at.isoformat() if available_at is not None else None,
        }, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        fingerprint = request_fingerprint or hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("request_fingerprint must be a lowercase SHA-256 digest")
        if submitted_by is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", submitted_by):
            raise ValueError("submitted_by must be a 1..64 character safe subject")
        with self._transaction() as conn:
            row = conn.execute(
                """INSERT INTO worker_jobs(job_type,payload,max_attempts,max_runtime_seconds,idempotency_key,
                           request_fingerprint,available_at,priority,submitted_by)
                   VALUES (%s,%s::jsonb,%s,%s,%s,%s,COALESCE(%s,clock_timestamp()),%s,%s)
                   ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
                   RETURNING id""",
                (job_type, raw, max_attempts, max_runtime_seconds, idempotency_key,
                 fingerprint, available_at, priority, submitted_by),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    "SELECT id,request_fingerprint,job_type,payload,max_attempts,max_runtime_seconds,priority,available_at FROM worker_jobs WHERE idempotency_key=%s",
                    (idempotency_key,),
                ).fetchone()
                if existing is None:
                    raise DatabaseUnavailable("IdempotencyConflictRetry", retryable=True)
                if existing["request_fingerprint"] != fingerprint:
                    raise ValueError("idempotency key was already used with different job contents")
                conn.execute(
                    "INSERT INTO worker_job_events(job_id,event_type,attempt,details) VALUES (%s,'submission_deduplicated',0,%s::jsonb)",
                    (existing["id"], json.dumps({"reason": "same_idempotency_key_and_fingerprint"})),
                )
                return existing["id"]
            job_id = row["id"]
            conn.execute(
                "INSERT INTO worker_job_events(job_id,event_type,attempt,generation,details) VALUES (%s,'enqueued',0,0,%s::jsonb)",
                (job_id, json.dumps({"job_type": job_type, "idempotency_key_present": idempotency_key is not None,
                                     "priority": priority, "delayed": available_at is not None})),
            )
            return job_id

    @staticmethod
    def _retry_delay(job_id: int, attempt: int) -> float:
        """Capped exponential delay with stable 0.5..1.5 hash jitter."""
        base = min(300.0, float(2 ** min(max(0, attempt - 1), 8)))
        digest = hashlib.sha256(f"{job_id}:{attempt}".encode("ascii")).digest()
        fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return min(300.0, base * (0.5 + fraction))

    def abandon_exhausted(self) -> int:
        with self._transaction() as conn:
            rows = conn.execute(
                """WITH expired AS (
                       SELECT id, attempts, worker_id, lease_token, generation FROM worker_jobs
                       WHERE state='running' AND cancel_requested_at IS NULL
                         AND lease_until <= clock_timestamp() AND attempts >= max_attempts
                       ORDER BY lease_until, id FOR UPDATE SKIP LOCKED LIMIT 100
                   ), abandoned AS (
                       UPDATE worker_jobs j SET state='dead', worker_id=NULL, lease_token=NULL, lease_until=NULL,
                           last_error_code='attempts_exhausted', updated_at=clock_timestamp()
                       FROM expired e WHERE j.id=e.id AND j.lease_token=e.lease_token
                       RETURNING j.id, e.attempts, e.worker_id, e.lease_token, e.generation
                   )
                   INSERT INTO worker_job_events(job_id,event_type,attempt,worker_id,lease_token,generation,details)
                   SELECT id,'abandoned',attempts,worker_id,lease_token,generation,'{"reason":"attempts_exhausted"}'::jsonb
                   FROM abandoned RETURNING job_id"""
            ).fetchall()
            return len(rows)

    def claim(self, lease_seconds: int) -> Job | None:
        token = uuid.uuid4()
        with self._transaction() as conn:
            row = conn.execute(
                """WITH candidate AS (
                       SELECT id, state AS previous_state, generation AS previous_generation, worker_id AS previous_worker,
                              created_at
                       FROM worker_jobs
                       WHERE attempts < max_attempts AND (
                           (state='queued' AND available_at <= clock_timestamp()) OR
                           (state='running' AND cancel_requested_at IS NULL AND lease_until <= clock_timestamp())
                       )
                       ORDER BY priority DESC, available_at, created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
                   ), claimed AS (
                       UPDATE worker_jobs j SET state='running', attempts=j.attempts+1,
                           worker_id=%s, lease_token=%s, generation=j.generation+1,
                           lease_until=clock_timestamp() + (%s * interval '1 second'),
                           updated_at=clock_timestamp()
                       FROM candidate c WHERE j.id=c.id
                       RETURNING j.id,j.job_type,j.payload,j.attempts,j.max_attempts,j.max_runtime_seconds,
                           j.generation,j.lease_token,j.lease_until,c.previous_state,c.previous_generation,c.previous_worker,
                           c.created_at,extract(epoch FROM (clock_timestamp()-c.created_at)) AS queue_wait_seconds
                   ), recorded AS (
                       INSERT INTO worker_job_events(job_id,event_type,attempt,worker_id,lease_token,generation,details)
                       SELECT id, CASE WHEN previous_state='running' THEN 'lease_reclaimed' ELSE 'claimed' END,
                           attempts,%s,lease_token,generation,
                           jsonb_build_object('job_type',job_type,'reason',CASE WHEN previous_state='running' THEN 'expired_lease' ELSE 'initial_claim' END,
                               'previous_generation',previous_generation,'previous_worker_id',previous_worker)
                       FROM claimed RETURNING job_id
                   ) SELECT claimed.* FROM claimed JOIN recorded ON recorded.job_id=claimed.id""",
                (self.worker_id, token, lease_seconds, self.worker_id),
            ).fetchone()
            if row is None:
                return None
            return Job(row["id"], row["job_type"], row["payload"], row["attempts"], row["max_attempts"], row["max_runtime_seconds"], row["generation"], row["lease_token"], row["lease_until"], row["created_at"], float(row["queue_wait_seconds"]), row["previous_state"] == "running")

    def mark_started(self, job: Job) -> bool:
        """Persist the handler start boundary only while this generation owns a live lease."""
        with self._transaction() as conn:
            row = conn.execute(
                """INSERT INTO worker_job_events(job_id,event_type,attempt,worker_id,lease_token,generation)
                   SELECT id,'started',attempts,worker_id,lease_token,generation FROM worker_jobs
                   WHERE id=%s AND state='running' AND worker_id=%s AND lease_token=%s AND generation=%s
                     AND lease_until>clock_timestamp() RETURNING id""",
                (job.id, self.worker_id, job.lease_token, job.generation),
            ).fetchone()
            return row is not None

    def renew(self, job: Job, lease_seconds: int) -> bool:
        with self._transaction() as conn:
            row = conn.execute(
                """UPDATE worker_jobs SET lease_until=clock_timestamp() + (%s * interval '1 second'),
                       updated_at=clock_timestamp()
                   WHERE id=%s AND state='running' AND worker_id=%s AND lease_token=%s
                     AND generation=%s AND lease_until > clock_timestamp() AND cancel_requested_at IS NULL
                   RETURNING lease_until""",
                (lease_seconds, job.id, self.worker_id, job.lease_token, job.generation),
            ).fetchone()
            return row is not None

    def acknowledge_cancel(self, job: Job) -> bool:
        """Finalize cancellation only for the current fenced lease owner."""
        with self._transaction() as conn:
            row = conn.execute(
                """UPDATE worker_jobs SET state='canceled',worker_id=NULL,lease_token=NULL,lease_until=NULL,
                       cancel_requested_at=COALESCE(cancel_requested_at,clock_timestamp()),updated_at=clock_timestamp()
                   WHERE id=%s AND state='running' AND worker_id=%s AND lease_token=%s AND generation=%s
                     AND cancel_requested_at IS NOT NULL
                   RETURNING attempts,generation""",
                (job.id, self.worker_id, job.lease_token, job.generation),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "INSERT INTO worker_job_events(job_id,event_type,attempt,worker_id,lease_token,generation,details) VALUES (%s,'canceled',%s,%s,%s,%s,'{\"reason\":\"operator_requested\"}'::jsonb)",
                (job.id, row["attempts"], self.worker_id, job.lease_token, job.generation),
            )
            return True

    def finalize_expired_cancellations(self) -> int:
        """Recover cancellation requests whose owning process died before ack."""
        with self._transaction() as conn:
            rows = conn.execute(
                """WITH expired AS (
                       SELECT id,attempts,worker_id,lease_token,generation FROM worker_jobs
                       WHERE state='running' AND cancel_requested_at IS NOT NULL AND lease_until<=clock_timestamp()
                       FOR UPDATE SKIP LOCKED LIMIT 100
                   ), changed AS (
                       UPDATE worker_jobs j SET state='canceled',worker_id=NULL,lease_token=NULL,lease_until=NULL,
                           updated_at=clock_timestamp()
                       FROM expired e WHERE j.id=e.id AND j.lease_token=e.lease_token
                       RETURNING j.id,e.attempts,e.worker_id,e.lease_token,e.generation
                   ) INSERT INTO worker_job_events(job_id,event_type,attempt,worker_id,lease_token,generation,details)
                     SELECT id,'canceled',attempts,worker_id,lease_token,generation,
                       '{\"reason\":\"cancel_owner_lease_expired\"}'::jsonb FROM changed RETURNING job_id"""
            ).fetchall()
            return len(rows)

    def succeed(self, job: Job, result: dict[str, Any]) -> bool:
        raw = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > MAX_RESULT_BYTES:
            return self.fail(job, "result_too_large", permanent=True)
        with self._transaction() as conn:
            row = conn.execute(
                """UPDATE worker_jobs SET state='succeeded', result=%s::jsonb,
                       worker_id=NULL, lease_token=NULL, lease_until=NULL, last_error_code=NULL,
                       updated_at=clock_timestamp()
                   WHERE id=%s AND state='running' AND worker_id=%s AND lease_token=%s
                     AND generation=%s AND lease_until > clock_timestamp()
                   RETURNING attempts""",
                (raw, job.id, self.worker_id, job.lease_token, job.generation),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "INSERT INTO worker_job_events(job_id,event_type,attempt,worker_id,lease_token,generation) VALUES (%s,'succeeded',%s,%s,%s,%s)",
                (job.id, row["attempts"], self.worker_id, job.lease_token, job.generation),
            )
            return True

    def record_notification_effect(self, job: Job, result: dict[str, Any]) -> bool | None:
        """Idempotently persist the simulated effect before success acknowledgment."""
        if job.job_type != "notification_simulation" or result.get("delivery") != "simulated":
            return None
        key = f"worker-notification-job-{job.id}"
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        with self._transaction() as conn:
            owner = conn.execute(
                """SELECT 1 FROM worker_jobs WHERE id=%s AND state='running' AND worker_id=%s
                   AND lease_token=%s AND generation=%s AND lease_until > clock_timestamp()
                   AND cancel_requested_at IS NULL""",
                (job.id, self.worker_id, job.lease_token, job.generation),
            ).fetchone()
            if owner is None:
                return None
            inserted = conn.execute(
                """INSERT INTO worker_demo_effects(job_id,effect_type,idempotency_key,result)
                   VALUES (%s,'notification_simulation',%s,%s::jsonb)
                   ON CONFLICT (job_id,effect_type) DO NOTHING RETURNING job_id""",
                (job.id, key, encoded),
            ).fetchone()
            applied = inserted is not None
            conn.execute(
                """INSERT INTO worker_job_events(job_id,event_type,attempt,worker_id,lease_token,generation,details)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (job.id, "effect_applied" if applied else "effect_deduplicated", job.attempts,
                 self.worker_id, job.lease_token, job.generation,
                 json.dumps({"effect_type": "notification_simulation", "idempotency_key": key,
                             "reason": "first_application" if applied else "duplicate_attempt"})),
            )
            return applied

    def fail(self, job: Job, error_code: str, *, retryable: bool = True, timed_out: bool = False) -> bool:
        if not error_code.isascii() or not error_code.replace("_", "").isalnum() or len(error_code) > 64:
            error_code = "handler_error"
        retry_delay = self._retry_delay(job.id, job.attempts)
        with self._transaction() as conn:
            row = conn.execute(
                """UPDATE worker_jobs SET
                       state=CASE WHEN NOT %s OR attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
                       available_at=CASE WHEN %s AND attempts < max_attempts
                           THEN clock_timestamp() + (%s * interval '1 second') ELSE available_at END,
                       worker_id=NULL, lease_token=NULL, lease_until=NULL, last_error_code=%s,
                       updated_at=clock_timestamp()
                   WHERE id=%s AND state='running' AND worker_id=%s AND lease_token=%s
                     AND generation=%s AND lease_until > clock_timestamp() AND cancel_requested_at IS NULL
                   RETURNING attempts,state""",
                (retryable, retryable, retry_delay, error_code, job.id, self.worker_id, job.lease_token, job.generation),
            ).fetchone()
            if row is None:
                return False
            event = "timed_out" if timed_out else ("retry_scheduled" if row["state"] == "queued" else "permanent_failure" if not retryable else "abandoned")
            conn.execute(
                "INSERT INTO worker_job_events(job_id,event_type,attempt,worker_id,lease_token,generation,details) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (job.id, event, row["attempts"], self.worker_id, job.lease_token, job.generation,
                 json.dumps({"error_code": error_code, "reason": "handler_timeout" if timed_out else error_code,
                             "retry_delay_seconds": retry_delay if row["state"] == "queued" else 0})),
            )
            return True

    def job_status(self, job_id: int) -> dict[str, Any] | None:
        with self._transaction() as conn:
            return conn.execute(
                """SELECT id,job_type,state,attempts,max_attempts,available_at,worker_id,
                          lease_token,lease_until,generation,idempotency_key,replay_of,max_runtime_seconds,
                          result,last_error_code,created_at,updated_at
                   FROM worker_jobs WHERE id=%s""",
                (job_id,),
            ).fetchone()

    def events(self, job_id: int) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            return conn.execute(
                """SELECT id,event_type,attempt,worker_id,generation,occurred_at,details
                   FROM worker_job_events WHERE job_id=%s ORDER BY id""",
                (job_id,),
            ).fetchall()

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        with self._transaction() as conn:
            return conn.execute(
                "SELECT id,job_type,state,attempts,max_attempts,worker_id,lease_until,last_error_code,created_at FROM worker_jobs ORDER BY id DESC LIMIT %s",
                (limit,),
            ).fetchall()

    def retry_dead(self, job_id: int, reason: str) -> bool:
        reason = _operator_reason(reason)
        with self._transaction() as conn:
            row = conn.execute(
                """UPDATE worker_jobs SET state='queued',attempts=0,available_at=clock_timestamp(),
                       worker_id=NULL,lease_token=NULL,lease_until=NULL,last_error_code=NULL,updated_at=clock_timestamp()
                   WHERE id=%s AND state='dead' RETURNING attempts,generation""",
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """INSERT INTO worker_job_events(job_id,event_type,attempt,generation,details)
                   VALUES (%s,'operator_retry',%s,%s,%s::jsonb)""",
                (job_id, row["attempts"], row["generation"], json.dumps({"reason": reason, "reason_code": "operator_requested"})),
            )
            return True

    def replay(self, job_id: int, reason: str, *, idempotency_key: str | None = None,
               request_fingerprint: str | None = None) -> int | None:
        reason = _operator_reason(reason)
        if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise ValueError("idempotency_key must be a 1..128 character safe token")
        if request_fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint):
            raise ValueError("request_fingerprint must be a lowercase SHA-256 digest")
        with self._transaction() as conn:
            source = conn.execute(
                "SELECT id,job_type,payload,max_attempts,max_runtime_seconds,state,generation,priority,submitted_by FROM worker_jobs WHERE id=%s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if source is None or source["state"] not in ("dead", "succeeded"):
                return None
            if idempotency_key is not None:
                existing = conn.execute("SELECT id,request_fingerprint,replay_of FROM worker_jobs WHERE idempotency_key=%s", (idempotency_key,)).fetchone()
                if existing is not None:
                    if existing["replay_of"] == job_id and existing["request_fingerprint"] == request_fingerprint:
                        return existing["id"]
                    raise ValueError("idempotency key was already used with different replay contents")
            row = conn.execute(
                """INSERT INTO worker_jobs(job_type,payload,max_attempts,max_runtime_seconds,replay_of,
                           idempotency_key,request_fingerprint,priority,submitted_by)
                   VALUES (%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (source["job_type"], json.dumps(source["payload"], separators=(",", ":")),
                 source["max_attempts"], source["max_runtime_seconds"], source["id"], idempotency_key,
                 request_fingerprint, source.get("priority", 0), source.get("submitted_by")),
            ).fetchone()
            new_id = row["id"]
            conn.execute(
                """INSERT INTO worker_job_events(job_id,event_type,attempt,generation,details)
                   VALUES (%s,'enqueued',0,0,%s::jsonb)""",
                (new_id, json.dumps({"job_type": source["job_type"], "replay_of": source["id"], "reason": reason})),
            )
            conn.execute(
                """INSERT INTO worker_job_events(job_id,event_type,attempt,generation,details)
                   VALUES (%s,'replay_created',%s,%s,%s::jsonb)""",
                (source["id"], 0, source["generation"], json.dumps({"replay_job_id": new_id, "reason": reason})),
            )
            return new_id

    def workers(self, stale_after_seconds: int = 15) -> list[dict[str, Any]]:
        if not 1 <= stale_after_seconds <= 3600:
            raise ValueError("stale_after_seconds must be 1..3600")
        with self._transaction() as conn:
            return conn.execute(
                """SELECT worker_id,name,pid,hostname,concurrency,state,started_at,heartbeat_at,
                          heartbeat_at < clock_timestamp() - (%s * interval '1 second') AS stale
                   FROM worker_processes ORDER BY started_at,worker_id""", (stale_after_seconds,)
            ).fetchall()


def _operator_reason(reason: str) -> str:
    if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9 _.,:/-]{3,200}", reason):
        raise ValueError("reason must be 3..200 safe printable characters")
    return reason.strip()

