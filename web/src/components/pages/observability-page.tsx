"use client";

import { useState } from "react";
import { Activity, AlertTriangle, ChartNoAxesCombined, Clock3, RefreshCw, RotateCcw, Timer } from "lucide-react";
import { PageHeading, MetricCard, ThroughputChart, LatencyChart, Panel, RefreshStamp, EmptyState } from "@/components/console-ui";
import { queryString, type Observability } from "@/lib/chronos";
import { usePoll } from "@/hooks/use-poll";
import { Button } from "@/components/ui/button";

export function ObservabilityPage() {
  const [hours, setHours] = useState<24 | 168>(24);
  const metrics = usePoll<Observability>(`v1/observability${queryString({ hours })}`, 15000);
  const data = metrics.data;
  const total = (data?.totals.succeeded ?? 0) + (data?.totals.failed ?? 0);
  return <>
    <PageHeading eyebrow="Signals / Job lifecycle" title="Observability" description="Historical activity derived from persisted PostgreSQL worker events. Latency measures claim-to-success for a lease generation, not external side-effect completion." action={<><label htmlFor="window-range" className="sr-only">Time window</label><select id="window-range" value={hours} onChange={(e) => setHours(Number(e.target.value) as 24 | 168)} className="h-9 rounded-lg border border-input bg-background px-3 text-xs focus-ring"><option value={24}>Last 24 hours</option><option value={168}>Last 7 days</option></select><RefreshStamp at={metrics.updatedAt} loading={metrics.loading} onRefresh={metrics.refresh}/></>} />
    {metrics.error && !data && <div role="alert" className="mb-4 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">{metrics.error.message}<Button variant="outline" size="sm" className="ml-3" onClick={metrics.refresh}><RefreshCw size={12}/>Retry</Button></div>}
    {data && <>
      <Panel title="Current operations" subtitle={`PostgreSQL snapshot and lifecycle samples over ${data.operations.window_hours}h`}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard label="Completed / sec" value={data.operations.jobs_completed_per_second.toFixed(2)} detail="Observed over the last 60 seconds" icon={ChartNoAxesCombined}/>
          <MetricCard label="Queue / running" value={`${data.operations.pending.toLocaleString()} / ${data.operations.running.toLocaleString()}`} detail={`${data.operations.active_workers} active · ${data.operations.idle_workers} idle workers`} icon={Activity}/>
          <MetricCard label="Dead letters" value={data.operations.dead_letter.toLocaleString()} detail={`${data.operations.recovered_24h} recovered leases · ${data.operations.expired_leases_24h} expired in 24h`} icon={AlertTriangle} accent={data.operations.dead_letter ? "red" : "primary"}/>
          <MetricCard label="DB pool" value={`${data.operations.pool.pool_available}/${data.operations.pool.pool_size}`} detail={`${data.operations.pool.requests_waiting} waiting · ${data.operations.pool.requests_queued} queued requests`} icon={Clock3}/>
        </div>
        <div className="mt-4 grid gap-3 lg:grid-cols-2">
          <p className="rounded-xl border border-border bg-background/50 p-4 text-xs text-muted-foreground">Queue wait p50 / p95 / p99: <span className="mono text-foreground">{[data.operations.queue_p50_ms,data.operations.queue_p95_ms,data.operations.queue_p99_ms].map(v => v === null ? "—" : `${v.toFixed(1)} ms`).join(" / ")}</span></p>
          <p className="rounded-xl border border-border bg-background/50 p-4 text-xs text-muted-foreground">Execution p50 / p95 / p99: <span className="mono text-foreground">{[data.operations.execution_p50_ms,data.operations.execution_p95_ms,data.operations.execution_p99_ms].map(v => v === null ? "—" : `${v.toFixed(1)} ms`).join(" / ")}</span></p>
        </div>
      </Panel>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><MetricCard label="Successful" value={data.totals.succeeded.toLocaleString()} detail="Completed worker attempts" icon={ChartNoAxesCombined}/><MetricCard label="Failures" value={data.totals.failed.toLocaleString()} detail={`${data.totals.failure_rate_percent === null ? "No completed samples" : `${data.totals.failure_rate_percent.toFixed(1)}% of terminal attempts`}`} icon={AlertTriangle} accent={data.totals.failed ? "red" : "primary"}/><MetricCard label="Retries scheduled" value={data.totals.retried.toLocaleString()} detail="Persisted retry_scheduled events" icon={RotateCcw} accent="orange"/><MetricCard label="Latency samples" value={data.latency.samples.toLocaleString()} detail="Claimed → succeeded event pairs" icon={Timer} accent="blue"/></div>
      <div className="mt-4 grid gap-4 xl:grid-cols-2"><ThroughputChart data={data} busy={metrics.loading}/><LatencyChart data={data} busy={metrics.loading}/></div>
      <Panel title="Execution latency percentiles" subtitle={data.latency.definition}><div className="grid gap-3 sm:grid-cols-3"><div className="rounded-xl border border-border bg-background/50 p-4"><p className="text-[10px] uppercase tracking-wide text-muted-foreground">P50</p><p className="mono mt-2 text-2xl font-semibold">{data.latency.p50_ms === null ? "—" : `${data.latency.p50_ms.toFixed(1)} ms`}</p></div><div className="rounded-xl border border-border bg-background/50 p-4"><p className="text-[10px] uppercase tracking-wide text-muted-foreground">P95</p><p className="mono mt-2 text-2xl font-semibold">{data.latency.p95_ms === null ? "—" : `${data.latency.p95_ms.toFixed(1)} ms`}</p></div><div className="rounded-xl border border-border bg-background/50 p-4"><p className="text-[10px] uppercase tracking-wide text-muted-foreground">P99</p><p className="mono mt-2 text-2xl font-semibold">{data.latency.p99_ms === null ? "—" : `${data.latency.p99_ms.toFixed(1)} ms`}</p></div></div><p className="mt-3 text-[10px] leading-5 text-muted-foreground">Percentiles include completed jobs with both a claim and success event in the selected window. Retry generations and completed event samples may be undercounted when older events are unavailable.</p>{total === 0 && <div className="mt-3"><EmptyState title="No terminal attempts in range" detail="Percentiles and outcome trends appear after workers complete jobs." icon={Activity}/></div>}</Panel>
      <div className="mt-3 flex items-start gap-2 rounded-lg border border-border/70 bg-muted/25 p-3 text-[10px] leading-5 text-muted-foreground"><Clock3 size={13} className="mt-0.5 shrink-0"/>No frontend-generated values are used. Empty buckets are database-derived zeros; percentiles show “—” when there are no observed samples.</div>
    </>}
  </>;
}
