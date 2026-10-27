"""Capture EXPLAIN ANALYZE/BUFFERS and PostgreSQL table/index statistics."""
from __future__ import annotations

import json
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path

import psycopg

root=Path(__file__).resolve().parents[1]
out=root/"benchmarks"/"worker"/"raw"
dsn="postgresql://gateway:dev-only-local@127.0.0.1:55432/gateway_dev"
compose=["docker","compose","--project-name","chronos-plan-local","-f","compose.workers.yaml","--profile","benchmark"]

def run(args,check=True):
    result=subprocess.run(args,cwd=root,text=True,capture_output=True)
    if check and result.returncode: raise RuntimeError(f"{' '.join(args)} failed\n{result.stdout}\n{result.stderr}")
    return result.stdout

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--label",default="after")
    label=parser.parse_args().label
    out.mkdir(parents=True,exist_ok=True)
    run(compose+["up","-d","postgres-benchmark"])
    run(compose+["run","--rm","benchmark-migrate"])
    try:
        with psycopg.connect(dsn,connect_timeout=2,application_name="chronos-query-plan") as conn:
            with conn.transaction():
                conn.execute("TRUNCATE worker_jobs RESTART IDENTITY CASCADE")
                conn.execute("""WITH inserted AS (
                    INSERT INTO worker_jobs(job_type,payload,max_attempts,max_runtime_seconds,priority)
                    SELECT 'synthetic_report','{"records":[]}'::jsonb,5,30,0 FROM generate_series(1,10000) RETURNING id
                ) INSERT INTO worker_job_events(job_id,event_type,attempt,details)
                  SELECT id,'enqueued',0,'{"job_type":"synthetic_report","plan_fixture":true}'::jsonb FROM inserted""")
                conn.execute("ANALYZE worker_jobs")
                conn.execute("ANALYZE worker_job_events")
                conn.execute("SET LOCAL statement_timeout='20s'")
                plan=conn.execute("""EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON)
                    SELECT id FROM worker_jobs
                    WHERE attempts<max_attempts AND (
                      (state='queued' AND available_at<=clock_timestamp()) OR
                      (state='running' AND cancel_requested_at IS NULL AND lease_until<=clock_timestamp())
                    ) ORDER BY priority DESC,available_at,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()[0]
            tables=conn.execute("SELECT relname,n_live_tup,n_dead_tup,seq_scan,idx_scan,last_analyze FROM pg_stat_user_tables WHERE relname IN ('worker_jobs','worker_job_events') ORDER BY relname").fetchall()
            indexes=conn.execute("SELECT indexrelname,idx_scan,idx_tup_read,idx_tup_fetch FROM pg_stat_user_indexes WHERE relname='worker_jobs' ORDER BY indexrelname").fetchall()
            settings=conn.execute("SELECT current_setting('server_version') AS version,current_setting('max_connections') AS max_connections,current_setting('shared_buffers') AS shared_buffers,current_setting('work_mem') AS work_mem,current_setting('random_page_cost') AS random_page_cost,current_setting('effective_cache_size') AS effective_cache_size,current_setting('synchronous_commit') AS synchronous_commit").fetchone()
        target=out/f"queue-claim-explain-{label}.json"
        target.write_text(json.dumps({"timestamp_utc":datetime.now(timezone.utc).isoformat(),"query":"worker_runtime.Repository.claim candidate SELECT with SKIP LOCKED and priority ordering","fixture_jobs":10000,"plan":plan,"table_statistics":tables,"index_statistics":indexes,"postgresql":settings},indent=2,default=str)+"\n",encoding="utf-8")
        print(target)
    finally:
        run(compose+["down","--volumes","--remove-orphans"],check=False)

if __name__=="__main__": main()
