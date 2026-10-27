"""Repeatable benchmark of the allowlisted worker against isolated PostgreSQL."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import platform
import subprocess
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "worker" / "raw"
DSN = "postgresql://gateway:dev-only-local@127.0.0.1:55432/gateway_dev"


def command(argv: list[str], *, check: bool = True) -> str:
    result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {' '.join(argv)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    values.sort()
    index = (len(values) - 1) * quantile
    low = int(index)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (index - low)


def connect() -> psycopg.Connection:
    return psycopg.connect(DSN, connect_timeout=2, application_name="chronos-benchmark", row_factory=dict_row)


def wait_workers(count: int) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with connect() as conn:
                active = conn.execute("SELECT count(*) AS n FROM worker_processes WHERE state='active' AND heartbeat_at>clock_timestamp()-interval '15 seconds'").fetchone()["n"]
            if active >= count:
                return
        except psycopg.Error:
            pass
        time.sleep(.25)
    raise TimeoutError(f"expected {count} live workers, observed {active}")


def seed(depth: int) -> None:
    with connect() as conn, conn.transaction():
        conn.execute("TRUNCATE worker_jobs RESTART IDENTITY CASCADE")
        conn.execute("""WITH inserted AS (
          INSERT INTO worker_jobs(job_type,payload,max_attempts,max_runtime_seconds,priority)
          SELECT 'synthetic_report','{"records":[]}'::jsonb,1,30,0 FROM generate_series(1,%s) RETURNING id
        ) INSERT INTO worker_job_events(job_id,event_type,attempt,details)
          SELECT id,'enqueued',0,'{"job_type":"synthetic_report","benchmark":true}'::jsonb FROM inserted""", (depth,))


def wait_finished(depth: int, timeout: int) -> tuple[float, dict[str, int]]:
    began = time.monotonic()
    deadline = began + timeout
    states: dict[str, int] = {}
    while time.monotonic() < deadline:
        with connect() as conn:
            states = {row["state"]: row["n"] for row in conn.execute("SELECT state,count(*) AS n FROM worker_jobs GROUP BY state").fetchall()}
        terminal = states.get("succeeded", 0) + states.get("dead", 0) + states.get("canceled", 0)
        if terminal == depth:
            return time.monotonic() - began, states
        time.sleep(.1)
    raise TimeoutError(f"benchmark deadline reached with states {states}")


def measure(depth: int, workers: int, elapsed: float, states: dict[str, int]) -> dict:
    with connect() as conn:
        samples = conn.execute("""WITH starts AS (
             SELECT job_id,generation,attempt,occurred_at FROM worker_job_events WHERE event_type='started'
           ), claims AS (
             SELECT job_id,generation,attempt,occurred_at FROM worker_job_events WHERE event_type IN ('claimed','lease_reclaimed')
           ), enqueued AS (
             SELECT job_id,min(occurred_at) AS occurred_at FROM worker_job_events WHERE event_type='enqueued' GROUP BY job_id
           ), ended AS (
             SELECT job_id,generation,attempt,occurred_at FROM worker_job_events WHERE event_type IN ('succeeded','retry_scheduled','timed_out','permanent_failure','abandoned')
           ) SELECT extract(epoch FROM (s.occurred_at-q.occurred_at))*1000 AS queue_ms,
                    extract(epoch FROM (e.occurred_at-s.occurred_at))*1000 AS execution_ms
             FROM starts s JOIN claims c USING(job_id,generation,attempt) JOIN enqueued q USING(job_id)
             JOIN ended e USING(job_id,generation,attempt)""").fetchall()
        pg = conn.execute("SELECT current_setting('server_version') AS version,current_setting('max_connections') AS max_connections,current_setting('shared_buffers') AS shared_buffers,current_setting('work_mem') AS work_mem,current_setting('synchronous_commit') AS synchronous_commit").fetchone()
        sessions = conn.execute("SELECT count(*) AS total,count(*) FILTER (WHERE application_name LIKE 'worker-%') AS workers,count(*) FILTER (WHERE application_name='chronos-benchmark') AS benchmark_clients FROM pg_stat_activity WHERE datname=current_database()").fetchone()
        tables = conn.execute("SELECT relname,n_live_tup,n_dead_tup,seq_scan,idx_scan FROM pg_stat_user_tables WHERE relname IN ('worker_jobs','worker_job_events') ORDER BY relname").fetchall()
    queue = [float(row["queue_ms"]) for row in samples]
    execution = [float(row["execution_ms"]) for row in samples]
    def distribution(values: list[float]) -> dict:
        return {"samples":len(values),"p50_ms":percentile(values,.50),"p95_ms":percentile(values,.95),"p99_ms":percentile(values,.99)}
    return {"status":"measured","worker_processes":workers,"worker_concurrency_each":1,"queue_depth":depth,"jobs":depth,
            "handler":"synthetic_report with empty records; fixed spawned allowlisted handler","elapsed_seconds":elapsed,
            "throughput_jobs_per_second":depth/elapsed if elapsed else None,
            "latency_ms":{"queue_wait":distribution(queue),"execution":distribution(execution)},
            "error_rate":(depth-states.get("succeeded",0))/depth,"final_states":states,
            "database_connections":dict(sessions),"postgresql":dict(pg),"table_statistics":tables}


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--worker-counts",default="1,2,4,8,16")
    parser.add_argument("--depths",default="20,50,100")
    parser.add_argument("--project",default="chronos-benchmark-local")
    parser.add_argument("--timeout-seconds",type=int,default=900)
    parser.add_argument("--label",default="baseline")
    opts=parser.parse_args()
    output=OUT/opts.label
    output.mkdir(parents=True,exist_ok=True)
    compose=["docker","compose","--project-name",opts.project,"-f","compose.workers.yaml","--profile","benchmark"]
    command(compose+["up","-d","postgres-benchmark"])
    command(compose+["run","--rm","benchmark-migrate"])
    docker=json.loads(command(["docker","info","--format","{{json .}}"]))
    memory=int(docker.get("MemTotal",0)//(1024*1024))
    max_workers=min(16,max(1,(memory-768)//256))
    requested=[int(x) for x in opts.worker_counts.split(",")]
    depths=[int(x) for x in opts.depths.split(",")]
    results=[]
    try:
        for count in requested:
            if count>max_workers:
                results.append({"status":"unsupported","worker_processes":count,"reason":f"Docker memory {memory} MiB; configured limits need approximately {count*256+768} MiB"})
                continue
            command(compose+["up","-d","--scale",f"benchmark-worker={count}","benchmark-worker"])
            wait_workers(count)
            for depth in depths:
                seed(depth)
                timestamp=dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
                elapsed,states=wait_finished(depth,opts.timeout_seconds)
                logs=command(compose+["logs","--no-color","--since",timestamp,"benchmark-worker"],check=False)
                claims=[]
                for line in logs.splitlines():
                    start=line.find("{")
                    if start<0: continue
                    try: record=json.loads(line[start:])
                    except json.JSONDecodeError: continue
                    if record.get("event")=="job.claimed" and isinstance(record.get("claim_duration_ms"),(int,float)):
                        claims.append(float(record["claim_duration_ms"]))
                result=measure(depth,count,elapsed,states)
                result["latency_ms"]["claim"]= {"samples":len(claims),"p50_ms":percentile(claims,.50),"p95_ms":percentile(claims,.95),"p99_ms":percentile(claims,.99)}
                result.update({"timestamp_utc":timestamp,"environment":{"os":platform.platform(),"python":platform.python_version(),"host_cpu_count":os.cpu_count(),"docker_cpu_count":docker.get("NCPU"),"docker_memory_mib":memory,"worker_cpu_limit":1,"worker_memory_limit_mib":256,"postgres_cpu_limit":2,"postgres_memory_limit_mib":512},"postgresql_resource_limits":{"cpu":2,"memory_mib":512}})
                filename=f"workers-{count}-depth-{depth}.json"
                (output/filename).write_text(json.dumps(result,indent=2,default=str)+"\n",encoding="utf-8")
                results.append(result)
                print(json.dumps({"file":filename,"workers":count,"depth":depth,"throughput":result["throughput_jobs_per_second"],"error_rate":result["error_rate"]}),flush=True)
        measured=[row for row in results if row["status"]=="measured"]
        fields=["worker_processes","queue_depth","jobs","elapsed_seconds","throughput_jobs_per_second","error_rate","database_connections","latency_ms"]
        with (output/"summary.csv").open("w",newline="",encoding="utf-8") as handle:
            writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
            for row in measured: writer.writerow({key:json.dumps(row.get(key)) if isinstance(row.get(key),dict) else row.get(key) for key in fields})
        (output/"run-metadata.json").write_text(json.dumps({"label":opts.label,"timestamp_utc":dt.datetime.now(dt.timezone.utc).isoformat(),"docker_cpu_count":docker.get("NCPU"),"docker_memory_bytes":docker.get("MemTotal"),"requested_worker_counts":requested,"requested_depths":depths,"results":results},indent=2,default=str)+"\n",encoding="utf-8")
    finally:
        command(compose+["down","--volumes","--remove-orphans"],check=False)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
