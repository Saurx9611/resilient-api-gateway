"use client";

import { Activity, Cpu, HeartPulse, RefreshCw, Server, UsersRound, Workflow } from "lucide-react";
import { PageHeading, MetricCard, Panel, EmptyState, RefreshStamp } from "@/components/console-ui";
import { formatDate, timeAgo, type Worker } from "@/lib/chronos";
import { usePoll } from "@/hooks/use-poll";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

export function WorkersPage() {
  const fleet = usePoll<{ items: Worker[] }>("v1/workers?stale_after_seconds=15", 5000);
  const items = fleet.data?.items ?? [];
  const fresh = items.filter((item) => !item.stale && item.state === "active");
  const slots = fresh.reduce((sum, item) => sum + item.concurrency, 0);
  const activeJobs = items.reduce((sum, item) => sum + (item.running_jobs ?? 0), 0);
  return <>
    <PageHeading eyebrow="Runtime / Capacity" title="Worker fleet" description="Independent worker process identities and database heartbeats. A process can be present in the registry after it stops, so freshness is shown separately from its last reported state." action={<RefreshStamp at={fleet.updatedAt} loading={fleet.loading} onRefresh={fleet.refresh}/>}/>
    {fleet.error && !fleet.data && <div role="alert" className="mb-4 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">{fleet.error.message}<Button size="sm" variant="outline" className="ml-3" onClick={fleet.refresh}><RefreshCw size={12}/>Retry</Button></div>}
    {fleet.data && <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><MetricCard label="Fresh workers" value={fresh.length} detail="Heartbeat within 15 seconds" icon={UsersRound}/><MetricCard label="Available slots" value={Math.max(0, slots - activeJobs)} detail={`${activeJobs} active leased jobs`} icon={Cpu} accent="blue"/><MetricCard label="Leased work" value={activeJobs} detail="Current database ownership" icon={Workflow} accent="orange"/><MetricCard label="Stale identities" value={items.filter((item) => item.stale).length} detail="Heartbeat older than 15 seconds" icon={HeartPulse} accent={items.some((item) => item.stale) ? "red" : "primary"}/></div>
      <Panel title="Process registry" subtitle="Latest 500 registered processes · stale cutoff 15 seconds" action={<span className="rounded-md border border-border px-2 py-1 text-[10px] text-muted-foreground">PostgreSQL heartbeat source</span>}>
        {!items.length ? <EmptyState title="No worker processes registered" detail="The control plane is reachable, but the worker registry is empty. Start the worker profile to process queued work." icon={Server}/> : <div className="grid gap-3 lg:grid-cols-2">{items.map((worker) => {
          const health = worker.stale ? "stale" : worker.state === "active" ? "healthy" : worker.state;
          return <Card key={worker.worker_id} className="border-border/80 bg-background/35 p-5"><div className="flex items-start gap-3"><div className={`grid size-10 shrink-0 place-items-center rounded-xl ${health === "healthy" ? "bg-chart-1/10 text-chart-1" : health === "draining" ? "bg-chart-3/10 text-chart-3" : "bg-destructive/10 text-destructive"}`}><Server size={18}/></div><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">{worker.name}</h3><span className={`rounded-md px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${health === "healthy" ? "bg-chart-1/10 text-chart-1" : health === "draining" ? "bg-chart-3/10 text-chart-3" : "bg-destructive/10 text-destructive"}`}>{health}</span></div><p className="mono mt-1 break-all text-[10px] text-muted-foreground">{worker.worker_id}</p></div></div>
            <div className="mt-5 grid grid-cols-2 gap-4 border-t border-border pt-4 sm:grid-cols-4"><div><p className="text-[9px] uppercase tracking-wide text-muted-foreground">Concurrency</p><p className="mono mt-1 text-sm font-semibold">{worker.concurrency}</p></div><div><p className="text-[9px] uppercase tracking-wide text-muted-foreground">Running jobs</p><p className="mono mt-1 text-sm font-semibold">{worker.running_jobs ?? 0}</p></div><div><p className="text-[9px] uppercase tracking-wide text-muted-foreground">PID / host</p><p className="mono mt-1 truncate text-xs" title={`${worker.pid} / ${worker.hostname}`}>{worker.pid} / {worker.hostname}</p></div><div><p className="text-[9px] uppercase tracking-wide text-muted-foreground">Heartbeat</p><p className={`mt-1 text-xs ${worker.stale ? "text-destructive" : ""}`} title={formatDate(worker.heartbeat_at)}>{timeAgo(worker.heartbeat_at)}</p></div></div>
            <div className="mt-3 flex items-center justify-between text-[10px] text-muted-foreground"><span>Started {formatDate(worker.started_at)}</span><span className="inline-flex items-center gap-1"><Activity size={11}/> {worker.state}</span></div>
          </Card>;
        })}</div>}
      </Panel>
    </>}
  </>;
}
