"""Operator CLI. It accepts only JSON data for four fixed job types."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from .repository import DatabaseUnavailable, Repository, migrate
from .runtime import Worker


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _dsn() -> str:
    dsn = os.environ.get("WORKER_DATABASE_URL", "")
    if not dsn:
        raise ValueError("WORKER_DATABASE_URL must be set")
    return dsn


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="worker-runtime", description="Allowlisted PostgreSQL worker runtime")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="create/update the worker schema")
    commands.add_parser("api", help="run the separate authenticated FastAPI control plane")
    scheduler = commands.add_parser("scheduler", help="run the separate cron occurrence materializer")
    scheduler.add_argument("--poll-interval", type=float, default=float(os.environ.get("CHRONOS_SCHEDULER_POLL_SECONDS", "1")))

    worker = commands.add_parser("worker", help="run one independent worker process")
    worker.add_argument("--name", default=os.environ.get("WORKER_NAME", "worker"))
    worker.add_argument("--concurrency", type=int, default=2)
    worker.add_argument("--lease-seconds", type=int, default=15)
    worker.add_argument("--handler-timeout", type=float, default=10.0)
    worker.add_argument("--poll-interval", type=float, default=0.1)
    worker.add_argument("--shutdown-grace", type=float, default=10.0)

    enqueue = commands.add_parser("enqueue", help="enqueue a validated allowlisted JSON job")
    enqueue.add_argument("--type", required=True, choices=("image_metadata", "csv_transform", "synthetic_report", "notification_simulation"))
    enqueue.add_argument("--payload-json", required=True, help="job payload as JSON data (never code)")
    enqueue.add_argument("--max-attempts", type=int, default=5)
    enqueue.add_argument("--max-runtime-seconds", type=int, default=30)
    enqueue.add_argument("--idempotency-key", help="optional stable key; same contents return the original job ID")

    status = commands.add_parser("status", help="show recent job state, without payloads")
    status.add_argument("--limit", type=int, default=20)
    workers = commands.add_parser("workers", help="show process identity and last database heartbeat")
    workers.add_argument("--stale-after-seconds", type=int, default=15)

    retry = commands.add_parser("retry", help="explicitly reset a dead job's attempt budget")
    retry.add_argument("job_id", type=int)
    retry.add_argument("--reason", required=True)

    replay = commands.add_parser("replay", help="create a new job linked to a terminal source job")
    replay.add_argument("job_id", type=int)
    replay.add_argument("--reason", required=True)

    events = commands.add_parser("events", help="show lifecycle events for one job")
    events.add_argument("job_id", type=int)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        dsn = _dsn()
        if args.command == "migrate":
            migrate(dsn)
            print(json.dumps({"event": "schema.ready", "version": 6}))
            return 0
        if args.command == "api":
            from .api import main as api_main
            api_main()
            return 0
        if args.command == "scheduler":
            from .scheduler import Scheduler
            return Scheduler(dsn, poll_interval=args.poll_interval).run()
        if args.command == "worker":
            return Worker(
                dsn,
                name=args.name,
                concurrency=args.concurrency,
                lease_seconds=args.lease_seconds,
                handler_timeout=args.handler_timeout,
                poll_interval=args.poll_interval,
                shutdown_grace=args.shutdown_grace,
            ).run()
        repo = Repository(dsn, uuid.uuid4(), "operator", 1)
        try:
            if args.command == "enqueue":
                payload = json.loads(args.payload_json, parse_constant=_reject_json_constant)
                job_id = repo.enqueue(args.type, payload, args.max_attempts,
                                      max_runtime_seconds=args.max_runtime_seconds,
                                      idempotency_key=args.idempotency_key)
                print(json.dumps({"job_id": job_id, "job_type": args.type, "state": "queued"}))
                return 0
            if args.command == "status":
                print(json.dumps(repo.recent_jobs(args.limit), default=_json_default, ensure_ascii=True))
                return 0
            if args.command == "workers":
                print(json.dumps(repo.workers(args.stale_after_seconds), default=_json_default, ensure_ascii=True))
                return 0
            if args.command == "retry":
                changed = repo.retry_dead(args.job_id, args.reason)
                print(json.dumps({"job_id": args.job_id, "state": "queued" if changed else "unchanged"}))
                return 0 if changed else 1
            if args.command == "replay":
                new_id = repo.replay(args.job_id, args.reason)
                print(json.dumps({"source_job_id": args.job_id, "job_id": new_id, "state": "queued" if new_id else "unchanged"}))
                return 0 if new_id else 1
            if args.command == "events":
                events = repo.events(args.job_id)
                if not events:
                    print("job not found", file=sys.stderr)
                    return 1
                print(json.dumps(events, default=_json_default, ensure_ascii=True))
                return 0
        finally:
            repo.close()
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"worker-runtime: {exc}", file=sys.stderr)
        return 2
    except DatabaseUnavailable as exc:
        print(f"worker-runtime: database unavailable ({exc.code})", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
