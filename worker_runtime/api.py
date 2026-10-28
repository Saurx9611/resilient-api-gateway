"""Authenticated FastAPI control plane; handler execution stays in worker processes."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from .control_repository import ControlRepository
from .repository import DatabaseUnavailable
from .scheduling import validate_schedule
from .handlers import validate_payload

UTC = timezone.utc
MAX_REQUEST_BYTES = 64 * 1024
security = HTTPBearer(auto_error=False, scheme_name="BearerToken")
JOB_TYPES = ("image_metadata", "csv_transform", "synthetic_report", "notification_simulation")
JOB_STATES = ("queued", "running", "succeeded", "dead", "canceled")
logger = logging.getLogger("chronos.api")


def _log_api_event(level: int, event: str, request_id: str | None, *, error_code: str | None = None) -> None:
    record = {"time": datetime.now(UTC).isoformat(), "level": logging.getLevelName(level).lower(),
              "event": event, "request_id": request_id}
    if error_code is not None:
        record["error_code"] = error_code
    logger.log(level, json.dumps(record, separators=(",", ":"), ensure_ascii=True))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class JobSubmission(StrictModel):
    job_type: Literal["image_metadata", "csv_transform", "synthetic_report", "notification_simulation"]
    payload: dict[str, Any]
    run_at: datetime | None = None
    priority: int = Field(default=0, ge=-10, le=10)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    max_runtime_seconds: int | None = Field(default=None, ge=1, le=300)

    @field_validator("run_at", mode="before")
    @classmethod
    def parse_run_at_json_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("run_at must be an ISO-8601 timestamp") from None
        return value

    @field_validator("run_at")
    @classmethod
    def run_at_must_be_aware_and_bounded(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("run_at must include a UTC offset")
            if value > datetime.now(UTC) + timedelta(days=365):
                raise ValueError("run_at cannot be more than 365 days in the future")
            return value.astimezone(UTC)
        return value


class ScheduleSubmission(StrictModel):
    job_type: Literal["image_metadata", "csv_transform", "synthetic_report", "notification_simulation"]
    payload: dict[str, Any]
    cron_expression: str = Field(min_length=9, max_length=128)
    timezone: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=0, ge=-10, le=10)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    max_runtime_seconds: int | None = Field(default=None, ge=1, le=300)

    @field_validator("cron_expression", "timezone")
    @classmethod
    def validate_cron_zone(cls, value: str, info):
        # The full pair is validated in the endpoint after strict decoding.
        if not value.strip() or len(value) != len(value.strip()):
            raise ValueError("value must not be empty or padded with whitespace")
        return value


class OperatorReason(StrictModel):
    reason: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9 _.,:/-]+$")


class QueuePolicyUpdate(StrictModel):
    default_max_attempts: int = Field(ge=1, le=10)
    default_max_runtime_seconds: int = Field(ge=1, le=300)


class APIPrincipal:
    def __init__(self, subject: str, scopes: frozenset[str]):
        self.subject = subject
        self.scopes = scopes


def _load_keys(raw: str) -> dict[str, tuple[str, frozenset[str]]]:
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("CHRONOS_API_KEYS must be a JSON object") from None
    if not isinstance(configured, dict) or not configured or len(configured) > 64:
        raise ValueError("CHRONOS_API_KEYS must define at least one API principal")
    result: dict[str, tuple[str, frozenset[str]]] = {}
    for subject, value in configured.items():
        if not isinstance(subject, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", subject):
            raise ValueError("API principal names must be safe 1..64 character tokens")
        if not isinstance(value, dict) or not isinstance(value.get("token"), str):
            raise ValueError("each API principal requires a token and scopes")
        token = value["token"]
        scopes = value.get("scopes")
        if not 32 <= len(token) <= 256 or not isinstance(scopes, list) or not scopes or not set(scopes) <= {"read", "submit", "operator"}:
            raise ValueError("API tokens need at least 32 characters and valid scopes")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        result[digest] = (subject, frozenset(scopes))
    return result


def create_app(*, dsn: str | None = None,
               api_keys: dict[str, tuple[str, frozenset[str]]] | None = None,
               rate_limit: int = 60) -> FastAPI:
    dsn = dsn if dsn is not None else os.environ.get("WORKER_DATABASE_URL", "")
    configured_keys = api_keys if api_keys is not None else _load_keys(os.environ.get("CHRONOS_API_KEYS", "{}"))
    if not dsn:
        raise ValueError("WORKER_DATABASE_URL must be set")
    if not 1 <= rate_limit <= 10000:
        raise ValueError("CHRONOS_API_RATE_LIMIT must be between 1 and 10000")
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            yield
        finally:
            application.state.repository.close()

    app = FastAPI(
        title="Chronos Job Control API", version="1.0.0",
        description="Authenticated control plane for PostgreSQL-backed allowlisted jobs. Workers execute jobs in separate processes.",
        docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json", lifespan=lifespan,
    )
    try:
        pool_min = int(os.environ.get("CHRONOS_DB_POOL_MIN", "1"))
        pool_max = int(os.environ.get("CHRONOS_DB_POOL_MAX", "8"))
    except ValueError:
        raise ValueError("CHRONOS_DB_POOL_MIN/MAX must be integers") from None
    app.state.repository = ControlRepository(dsn, pool_min_size=pool_min, pool_max_size=pool_max)
    app.state.api_keys = configured_keys
    app.state.rate_limit = rate_limit
    app.state.metrics_registry = CollectorRegistry()
    pool_size = Gauge("chronos_api_db_pool_connections", "Control API database pool connections", ("state",), registry=app.state.metrics_registry)
    pool_waiters = Gauge("chronos_api_db_pool_waiters", "Requests waiting for a control API DB connection", registry=app.state.metrics_registry)
    pool_max = Gauge("chronos_api_db_pool_max_size", "Configured maximum control API database connections", registry=app.state.metrics_registry)
    job_states = Gauge("chronos_jobs", "Current jobs by durable PostgreSQL state", ("state",), registry=app.state.metrics_registry)
    worker_states = Gauge("chronos_workers", "Fresh worker processes by activity state", ("state",), registry=app.state.metrics_registry)
    lifecycle_metrics = Gauge("chronos_lifecycle_events_24h", "Durable lifecycle event counts over the last 24 hours", ("event",), registry=app.state.metrics_registry)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code, message = _http_error(exc.status_code, exc.detail)
        headers = exc.headers or {}
        return JSONResponse(status_code=exc.status_code,
                            content={"error": {"code": code, "message": message,
                                                "request_id": getattr(request.state, "request_id", None)}},
                            headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = [{"location": ".".join(str(part) for part in item.get("loc", ())),
                   "message": item.get("msg", "invalid value")} for item in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request",
                            "message": "request parameters failed validation", "fields": fields,
                            "request_id": getattr(request.state, "request_id", None)}})

    @app.exception_handler(DatabaseUnavailable)
    async def database_error(request: Request, exc: DatabaseUnavailable):
        _log_api_event(logging.WARNING, "database_unavailable",
                       getattr(request.state, "request_id", None), error_code=exc.code)
        return JSONResponse(status_code=503, content={"error": {"code": "database_unavailable",
                            "message": "control-plane storage is temporarily unavailable",
                            "request_id": getattr(request.state, "request_id", None)}})

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        # Do not log exception text, request bodies, SQL, or credentials. The
        # request ID is the correlation handle for an operator-side trace.
        _log_api_event(logging.ERROR, "request_failed", getattr(request.state, "request_id", None))
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error",
                            "message": "request could not be completed",
                            "request_id": getattr(request.state, "request_id", None)}})

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request):
        request.app.state.repository.check_schema()
        return {"status": "ready"}

    def authorize(scope: str):
        def dependency(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> APIPrincipal:
            if credentials is None or credentials.scheme.lower() != "bearer":
                raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
            supplied = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
            match: tuple[str, frozenset[str]] | None = None
            for digest, principal in request.app.state.api_keys.items():
                if hmac.compare_digest(digest, supplied):
                    match = principal
            if match is None:
                raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
            subject, scopes = match
            if scope not in scopes and "operator" not in scopes:
                raise HTTPException(403, "forbidden")
            allowed, remaining, window_start = request.app.state.repository.consume_rate_limit(
                subject, request.app.state.rate_limit, 60)
            request.state.rate_limit = (request.app.state.rate_limit, remaining, window_start)
            if not allowed:
                raise HTTPException(429, "rate_limit_exceeded", headers={"Retry-After": "60"})
            return APIPrincipal(subject, scopes)
        return dependency

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request, _principal: APIPrincipal = Depends(authorize("operator"))):
        # The API has public ingress in production; require operator scope here.
        stats = request.app.state.repository.pool_stats()
        snapshot = request.app.state.repository.live_metrics()
        pool_size.labels(state="open").set(stats["pool_size"])
        pool_size.labels(state="available").set(stats["pool_available"])
        pool_size.labels(state="in_use").set(max(0, stats["pool_size"] - stats["pool_available"]))
        pool_waiters.set(stats["requests_waiting"])
        pool_max.set(stats["pool_max_size"])
        for state in ("queued", "running", "succeeded", "dead", "canceled"):
            job_states.labels(state=state).set(snapshot["states"].get(state, 0))
        worker_states.labels(state="active").set(snapshot["workers"]["active"])
        worker_states.labels(state="idle").set(snapshot["workers"]["idle"])
        for event, value in (("retries", snapshot["recent"]["retries"]), ("expired_leases", snapshot["recent"]["expired"]), ("recovered_jobs", snapshot["recent"]["recovered"])):
            lifecycle_metrics.labels(event=event).set(value)
        return Response(generate_latest(request.app.state.metrics_registry), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def rate_headers(request: Request, call_next):
        response = await call_next(request)
        value = getattr(request.state, "rate_limit", None)
        if value:
            limit, remaining, _ = value
            response.headers["RateLimit-Limit"] = str(limit)
            response.headers["RateLimit-Remaining"] = str(remaining)
        return response

    async def read_model(request: Request, model: type[BaseModel]) -> BaseModel:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "request_too_large")
            except ValueError:
                raise HTTPException(400, "invalid_content_length") from None
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise HTTPException(415, "application_json_required")
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "request_too_large")
        try:
            decoded = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")))
            return model.model_validate(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError):
            raise HTTPException(422, "invalid_request_body") from None

    @app.post("/v1/jobs", status_code=202, openapi_extra={"requestBody": _request_body_schema(JobSubmission)})
    async def submit_job(request: Request,
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
                         principal: APIPrincipal = Depends(authorize("submit"))):
        submitted = await read_model(request, JobSubmission)
        try:
            payload = validate_payload(submitted.job_type, submitted.payload)
            policy = await run_in_threadpool(request.app.state.repository.queue_policy)
            max_attempts = submitted.max_attempts if submitted.max_attempts is not None else policy["default_max_attempts"]
            max_runtime_seconds = submitted.max_runtime_seconds if submitted.max_runtime_seconds is not None else policy["default_max_runtime_seconds"]
            body = {"job_type": submitted.job_type, "payload": payload,
                    "run_at": submitted.run_at.isoformat() if submitted.run_at else None,
                    "priority": submitted.priority, "max_attempts": max_attempts,
                    "max_runtime_seconds": max_runtime_seconds}
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True,
                            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid_job_payload") from None
        key = idempotency_key
        if key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise HTTPException(400, "invalid_idempotency_key")
        scoped_key = hashlib.sha256(f"{principal.subject}\0{key}".encode()).hexdigest() if key else None
        try:
            job_id = await run_in_threadpool(request.app.state.repository.enqueue,
                job_type=submitted.job_type, payload=payload, max_attempts=max_attempts,
                max_runtime_seconds=max_runtime_seconds, available_at=submitted.run_at,
                priority=submitted.priority, idempotency_key=scoped_key, submitted_by=principal.subject,
                request_fingerprint=fingerprint)
        except ValueError:
            raise HTTPException(409, "idempotency_fingerprint_mismatch") from None
        return {"job_id": job_id, "state": "queued"}

    @app.get("/v1/jobs")
    def list_jobs(request: Request, limit: int = 50, cursor: str | None = None,
                  state: str | None = None, job_type: str | None = None,
                  q: str | None = None,
                  created_after: datetime | None = None, created_before: datetime | None = None,
                  principal: APIPrincipal = Depends(authorize("read"))):
        if not 1 <= limit <= 100:
            raise HTTPException(422, "limit_must_be_1_to_100")
        if state is not None and state not in JOB_STATES:
            raise HTTPException(422, "invalid_state_filter")
        if job_type is not None and job_type not in JOB_TYPES:
            raise HTTPException(422, "invalid_job_type_filter")
        if q is not None and (len(q) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:#-]*", q)):
            raise HTTPException(422, "invalid_search_query")
        if any(dt is not None and (dt.tzinfo is None or dt.utcoffset() is None) for dt in (created_after, created_before)):
            raise HTTPException(422, "date_filter_requires_offset")
        if created_after is not None and created_before is not None and created_after >= created_before:
            raise HTTPException(422, "invalid_date_range")
        before = _decode_cursor(cursor)
        items, next_value = request.app.state.repository.page_jobs(
            limit=limit, cursor=before, state=state, job_type=job_type,
            created_after=created_after.astimezone(UTC) if created_after else None,
            created_before=created_before.astimezone(UTC) if created_before else None, search=q or None)
        return {"items": items, "next_cursor": _encode_cursor(next_value) if next_value else None}

    @app.get("/v1/events")
    def recent_events(request: Request, limit: int = 50, cursor: str | None = None,
                      principal: APIPrincipal = Depends(authorize("read"))):
        if not 1 <= limit <= 100:
            raise HTTPException(422, "limit_must_be_1_to_100")
        items, next_value = request.app.state.repository.recent_events(limit=limit, cursor=_decode_cursor(cursor))
        return {"items": items, "next_cursor": _encode_cursor(next_value) if next_value else None}

    @app.get("/v1/observability")
    def observability(request: Request, hours: int = 24,
                      principal: APIPrincipal = Depends(authorize("read"))):
        if hours not in {24, 168}:
            raise HTTPException(422, "hours_must_be_24_or_168")
        return request.app.state.repository.observability(hours)

    @app.get("/v1/settings/queue")
    def queue_policy(request: Request, principal: APIPrincipal = Depends(authorize("read"))):
        return request.app.state.repository.queue_policy()

    @app.put("/v1/settings/queue", openapi_extra={"requestBody": _request_body_schema(QueuePolicyUpdate)})
    async def update_queue_policy(request: Request, principal: APIPrincipal = Depends(authorize("operator"))):
        body = await read_model(request, QueuePolicyUpdate)
        return await run_in_threadpool(request.app.state.repository.update_queue_policy,
            max_attempts=body.default_max_attempts,
            max_runtime_seconds=body.default_max_runtime_seconds,
            actor=principal.subject)

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: int, request: Request, principal: APIPrincipal = Depends(authorize("read"))):
        job = request.app.state.repository.job(job_id)
        if job is None:
            raise HTTPException(404, "job_not_found")
        return job

    @app.get("/v1/jobs/{job_id}/attempts")
    def get_attempts(job_id: int, request: Request, limit: int = 50, cursor: int | None = None,
                     principal: APIPrincipal = Depends(authorize("read"))):
        if not 1 <= limit <= 100 or (cursor is not None and cursor < 1):
            raise HTTPException(422, "invalid_pagination")
        if request.app.state.repository.job(job_id) is None:
            raise HTTPException(404, "job_not_found")
        items, next_cursor = request.app.state.repository.page_events(job_id, limit=limit, cursor=cursor)
        return {"items": items, "next_cursor": next_cursor}

    @app.post("/v1/jobs/{job_id}/cancel", openapi_extra={"requestBody": _request_body_schema(OperatorReason)})
    async def cancel_job(job_id: int, request: Request, principal: APIPrincipal = Depends(authorize("operator"))):
        body = await read_model(request, OperatorReason)
        result = await run_in_threadpool(request.app.state.repository.cancel_job, job_id, body.reason)
        if result is None:
            raise HTTPException(404, "job_not_found")
        if result["state"] not in {"queued", "running", "canceled"}:
            raise HTTPException(409, "job_already_terminal")
        return {"job_id": job_id, **result}

    @app.post("/v1/jobs/{job_id}/retry", status_code=202, openapi_extra={"requestBody": _request_body_schema(OperatorReason)})
    async def retry_job(job_id: int, request: Request, principal: APIPrincipal = Depends(authorize("operator"))):
        body = await read_model(request, OperatorReason)
        if not await run_in_threadpool(request.app.state.repository.retry, job_id, body.reason):
            if await run_in_threadpool(request.app.state.repository.job, job_id) is None:
                raise HTTPException(404, "job_not_found")
            raise HTTPException(409, "job_not_dead")
        return {"job_id": job_id, "state": "queued"}

    @app.post("/v1/jobs/{job_id}/replay", status_code=202, openapi_extra={"requestBody": _request_body_schema(OperatorReason)})
    async def replay_job(job_id: int, request: Request,
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
                         principal: APIPrincipal = Depends(authorize("operator"))):
        body = await read_model(request, OperatorReason)
        key = idempotency_key
        if key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise HTTPException(400, "invalid_idempotency_key")
        scoped_key = hashlib.sha256(f"{principal.subject}\0{key}".encode()).hexdigest() if key else None
        fingerprint = hashlib.sha256(json.dumps({"source_job_id": job_id, "reason": body.reason},
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            replay_id = await run_in_threadpool(request.app.state.repository.replay, job_id, body.reason,
                idempotency_key=scoped_key, request_fingerprint=fingerprint if scoped_key else None)
        except ValueError:
            raise HTTPException(409, "idempotency_fingerprint_mismatch") from None
        if replay_id is None:
            if await run_in_threadpool(request.app.state.repository.job, job_id) is None:
                raise HTTPException(404, "job_not_found")
            raise HTTPException(409, "job_not_terminal")
        return {"source_job_id": job_id, "job_id": replay_id, "state": "queued"}

    @app.get("/v1/workers")
    def list_workers(request: Request, stale_after_seconds: int = 15,
                     principal: APIPrincipal = Depends(authorize("read"))):
        if not 1 <= stale_after_seconds <= 3600:
            raise HTTPException(422, "stale_after_seconds_must_be_1_to_3600")
        return {"items": request.app.state.repository.workers(stale_after_seconds)}

    @app.get("/v1/queue/stats")
    def queue_stats(request: Request, principal: APIPrincipal = Depends(authorize("read"))):
        return request.app.state.repository.queue_stats()

    @app.post("/v1/schedules", status_code=201, openapi_extra={"requestBody": _request_body_schema(ScheduleSubmission)})
    async def create_schedule(request: Request, principal: APIPrincipal = Depends(authorize("submit"))):
        submitted = await read_model(request, ScheduleSubmission)
        try:
            validate_schedule(submitted.cron_expression, submitted.timezone)
            payload = validate_payload(submitted.job_type, submitted.payload)
            policy = await run_in_threadpool(request.app.state.repository.queue_policy)
            schedule = await run_in_threadpool(request.app.state.repository.create_schedule,
                job_type=submitted.job_type, payload=payload, cron_expression=submitted.cron_expression,
                timezone_name=submitted.timezone, priority=submitted.priority,
                max_attempts=submitted.max_attempts if submitted.max_attempts is not None else policy["default_max_attempts"],
                max_runtime_seconds=submitted.max_runtime_seconds if submitted.max_runtime_seconds is not None else policy["default_max_runtime_seconds"],
                created_by=principal.subject)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return schedule

    @app.get("/v1/schedules")
    def list_schedules(request: Request, limit: int = 50,
                       principal: APIPrincipal = Depends(authorize("read"))):
        if not 1 <= limit <= 100:
            raise HTTPException(422, "limit_must_be_1_to_100")
        return {"items": request.app.state.repository.list_schedules(limit)}

    @app.get("/v1/schedules/{schedule_id}")
    def get_schedule(schedule_id: uuid.UUID, request: Request,
                     principal: APIPrincipal = Depends(authorize("read"))):
        schedule = request.app.state.repository.schedule(schedule_id)
        if schedule is None:
            raise HTTPException(404, "schedule_not_found")
        return schedule

    @app.post("/v1/schedules/{schedule_id}/cancel", openapi_extra={"requestBody": _request_body_schema(OperatorReason)})
    async def cancel_schedule(schedule_id: uuid.UUID, request: Request,
                              principal: APIPrincipal = Depends(authorize("operator"))):
        body = await read_model(request, OperatorReason)
        schedule = await run_in_threadpool(request.app.state.repository.cancel_schedule,
                                           schedule_id, principal.subject, body.reason)
        if schedule is None:
            raise HTTPException(404, "schedule_not_found")
        return schedule

    def custom_openapi():
        if app.openapi_schema is None:
            app.openapi_schema = get_openapi(title=app.title, version=app.version,
                description=app.description, routes=app.routes)
            app.openapi_schema.setdefault("components", {}).setdefault("schemas", {}).update({
                "JobSubmission": JobSubmission.model_json_schema(),
                "ScheduleSubmission": ScheduleSubmission.model_json_schema(),
                "OperatorReason": OperatorReason.model_json_schema(),
                "QueuePolicyUpdate": QueuePolicyUpdate.model_json_schema(),
            })
        return app.openapi_schema
    app.openapi = custom_openapi
    return app


def _request_body_schema(model: type[BaseModel]) -> dict[str, Any]:
    return {"required": True, "content": {"application/json": {"schema": model.model_json_schema(ref_template="#/components/schemas/{model}")}}}


def _encode_cursor(value: int | None) -> str | None:
    if value is None:
        return None
    import base64
    return base64.urlsafe_b64encode(str(value).encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> int | None:
    if value is None:
        return None
    import base64
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        if not re.fullmatch(r"[1-9][0-9]{0,18}", decoded):
            raise ValueError
        return int(decoded)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "invalid_cursor") from None


def _http_error(status: int, detail: Any) -> tuple[str, str]:
    if status == 401:
        return "unauthorized", "valid bearer authentication is required"
    if status == 403:
        return "forbidden", "the configured principal lacks the required permission"
    if status == 404:
        return str(detail), "the requested resource was not found"
    if status == 409:
        return str(detail), "the requested state transition is not available"
    if status == 413:
        return str(detail), "request exceeds the configured size limit"
    if status == 415:
        return str(detail), "application/json content type is required"
    if status == 429:
        return "rate_limit_exceeded", "authenticated request rate limit exceeded"
    if status == 422:
        return str(detail) if isinstance(detail, str) else "invalid_request", "request failed validation"
    if status == 400:
        return str(detail), "request could not be accepted"
    return "request_error", "request could not be completed"


def main() -> None:
    keys = _load_keys(os.environ.get("CHRONOS_API_KEYS", "{}"))
    app = create_app(api_keys=keys, rate_limit=int(os.environ.get("CHRONOS_API_RATE_LIMIT", "60")))
    host = os.environ.get("CHRONOS_API_HOST", "0.0.0.0")
    # Render injects PORT for web services. Keep the explicit Chronos value as
    # a local fallback, but never bind a Render service to the wrong port.
    port = int(os.environ.get("PORT") or os.environ.get("CHRONOS_API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, access_log=False, timeout_graceful_shutdown=10)


app = create_app(dsn=os.environ.get("WORKER_DATABASE_URL", "postgresql://unconfigured.invalid/chronos"), api_keys={})


if __name__ == "__main__":
    main()
