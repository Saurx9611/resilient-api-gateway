"use client";

import Link from "next/link";
import { Activity, ArrowDown, ArrowRight, ArrowUpRight, Boxes, CircleAlert, Clock3, RefreshCw, Timer, Workflow } from "lucide-react";
import { Area, AreaChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import type { Job, JobState, Observability, QueueStats } from "@/lib/chronos";
import { formatDate, jobTypeLabels, timeAgo } from "@/lib/chronos";

export function PageHeading({ eyebrow, title, description, action }: { eyebrow?: string; title: string; description?: string; action?: React.ReactNode }) {
  return <div className="mb-7 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
    <div><p className="mb-2 text-[10px] font-semibold uppercase tracking-[.18em] text-muted-foreground">{eyebrow ?? "Chronos / Operations"}</p><h1 className="text-[27px] font-semibold leading-tight tracking-[-.035em] sm:text-[32px]">{title}</h1>{description && <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">{description}</p>}</div>
    {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
  </div>;
}

export function SectionTitle({ title, detail, action }: { title: string; detail?: string; action?: React.ReactNode }) {
  return <div className="mb-4 flex items-center justify-between gap-3"><div><h2 className="text-sm font-semibold tracking-tight">{title}</h2>{detail && <p className="mt-1 text-xs text-muted-foreground">{detail}</p>}</div>{action}</div>;
}

export function LoadState({ loading, error, hasData, onRetry }: { loading: boolean; error: Error | null; hasData: boolean; onRetry?: () => void }) {
  if (loading && !hasData) return <div className="space-y-3" role="status" aria-label="Loading data"><Skeleton className="h-12 w-full"/><Skeleton className="h-12 w-4/5"/><span className="sr-only">Loading data…</span></div>;
  if (error && !hasData) return <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/5 p-5"><div className="flex items-start gap-3"><CircleAlert size={18} className="mt-0.5 text-destructive"/><div><p className="text-sm font-semibold">Couldn’t load this view</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{error.message}</p>{onRetry && <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}><RefreshCw size={13}/>Try again</Button>}</div></div></div>;
  return null;
}

export function RefreshStamp({ at, loading, onRefresh }: { at: Date | null; loading: boolean; onRefresh: () => void }) {
  return <div className="flex items-center gap-2 text-[11px] text-muted-foreground"><span className={`size-1.5 rounded-full ${loading ? "animate-pulse bg-chart-3" : "bg-chart-1"}`} aria-hidden="true"/><span>{at ? `Updated ${timeAgo(at.toISOString())}` : "Waiting for data"}</span><Button variant="ghost" size="icon-xs" aria-label="Refresh data" onClick={onRefresh} disabled={loading}><RefreshCw size={12} className={loading ? "animate-spin" : ""}/></Button></div>;
}

const stateStyles: Record<JobState, string> = {
  queued: "border-chart-2/25 bg-chart-2/10 text-chart-2",
  running: "border-chart-3/25 bg-chart-3/10 text-chart-3",
  succeeded: "border-chart-1/25 bg-chart-1/10 text-chart-1",
  dead: "border-destructive/25 bg-destructive/10 text-destructive",
  canceled: "border-border bg-muted text-muted-foreground",
};
export function StateBadge({ state }: { state: JobState }) {
  return <Badge variant="outline" className={`gap-1.5 rounded-md px-2 py-0.5 text-[10px] font-medium capitalize ${stateStyles[state]}`}><span className={`size-1.5 rounded-full ${state === "succeeded" ? "bg-chart-1" : state === "running" ? "animate-pulse bg-chart-3" : state === "dead" ? "bg-destructive" : state === "queued" ? "bg-chart-2" : "bg-muted-foreground"}`}/>{state}</Badge>;
}

export function MetricCard({ label, value, detail, icon: Icon, accent = "primary", href }: { label: string; value: string | number; detail: React.ReactNode; icon: typeof Activity; accent?: "primary" | "blue" | "orange" | "red"; href?: string }) {
  const iconColor = { primary: "text-chart-1 bg-chart-1/10", blue: "text-chart-2 bg-chart-2/10", orange: "text-chart-3 bg-chart-3/10", red: "text-chart-4 bg-chart-4/10" }[accent];
  const content = <Card className="group relative overflow-hidden border-border/80 bg-card transition-colors hover:border-border"><CardContent className="p-5"><div className="flex items-start justify-between"><div><p className="text-xs font-medium text-muted-foreground">{label}</p><div className="mt-3 text-[29px] font-semibold leading-none tracking-[-.04em]">{value}</div></div><div className={`grid size-9 place-items-center rounded-xl ${iconColor}`}><Icon size={17}/></div></div><div className="mt-4 flex items-center gap-1.5 text-[11px] text-muted-foreground">{detail}</div>{href && <ArrowUpRight className="absolute bottom-5 right-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" size={14}/>}</CardContent></Card>;
  return href ? <Link href={href} className="block focus-ring rounded-xl">{content}</Link> : content;
}

export function QueueOverview({ stats }: { stats: QueueStats }) {
  return <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
    <MetricCard label="Ready to run" value={stats.ready} detail={<><span className="text-chart-2">●</span> Available to claim</>} icon={Workflow} accent="blue" href="/jobs?state=queued"/>
    <MetricCard label="Running now" value={stats.running} detail={<><span className="text-chart-3">●</span> Active leases</>} icon={Activity} accent="orange" href="/jobs?state=running"/>
    <MetricCard label="Waiting" value={stats.delayed} detail={<><Clock3 size={12}/> Scheduled for later</>} icon={Timer} href="/jobs?state=queued"/>
    <MetricCard label="Dead letters" value={stats.states.dead ?? 0} detail={<><CircleAlert size={12}/> Needs operator attention</>} icon={CircleAlert} accent="red" href="/dead-letter"/>
  </div>;
}

export function JobTable({ jobs, compact = false }: { jobs: Job[]; compact?: boolean }) {
  if (!jobs.length) return <EmptyState title="Nothing here yet" detail="Jobs will appear here as they are submitted." icon={Workflow}/>;
  return <div className="overflow-x-auto scrollbar-thin"><table className="w-full min-w-[700px] text-left text-xs">
    <caption className="sr-only">Chronos jobs and execution state</caption>
    <thead><tr className="border-b border-border text-[10px] uppercase tracking-[.12em] text-muted-foreground"><th scope="col" className="px-4 py-3 font-medium">Job</th><th scope="col" className="px-4 py-3 font-medium">Type</th><th scope="col" className="px-4 py-3 font-medium">State</th><th scope="col" className="px-4 py-3 font-medium">Attempts</th><th scope="col" className="px-4 py-3 font-medium">Priority</th><th scope="col" className="px-4 py-3 font-medium">Created</th><th scope="col" className="px-4 py-3 font-medium"><span className="sr-only">Open</span></th></tr></thead>
    <tbody>{jobs.map((job) => <tr key={job.id} className="group border-b border-border/70 last:border-0 hover:bg-muted/35">
      <td className="px-4 py-3.5"><Link href={`/jobs/${job.id}`} className="mono rounded text-xs font-semibold text-foreground hover:text-chart-2 focus-ring">#{job.id}</Link>{job.replay_of && <span className="ml-2 text-[10px] text-muted-foreground">replay of #{job.replay_of}</span>}</td>
      <td className="px-4 py-3.5 text-muted-foreground">{jobTypeLabels[job.job_type]}</td><td className="px-4 py-3.5"><StateBadge state={job.state}/></td>
      <td className="px-4 py-3.5"><span className="mono">{job.attempts}</span><span className="text-muted-foreground"> / {job.max_attempts}</span></td><td className="px-4 py-3.5"><span className="inline-flex items-center gap-1 text-muted-foreground">{job.priority > 0 ? <ArrowUpRight size={12} className="text-chart-1"/> : job.priority < 0 ? <ArrowDown size={12}/> : null}{job.priority}</span></td>
      <td className="px-4 py-3.5 text-muted-foreground" title={formatDate(job.created_at)}>{timeAgo(job.created_at)}</td><td className="px-4 py-3.5 text-right"><Link href={`/jobs/${job.id}`} className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground opacity-70 hover:text-foreground group-hover:opacity-100">Inspect <ArrowRight size={12}/></Link></td>
    </tr>)}</tbody>
  </table>{compact && <div className="border-t border-border/70 px-4 py-3 text-right"><Link href="/jobs" className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground">All jobs <ArrowRight size={13}/></Link></div>}</div>;
}

export function EmptyState({ title, detail, icon: Icon = Boxes, action }: { title: string; detail: string; icon?: typeof Boxes; action?: React.ReactNode }) {
  return <div className="flex min-h-[190px] flex-col items-center justify-center px-5 py-10 text-center"><div className="mb-4 grid size-11 place-items-center rounded-2xl border border-border bg-muted/60 text-muted-foreground"><Icon size={18}/></div><h3 className="text-sm font-semibold">{title}</h3><p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">{detail}</p>{action && <div className="mt-4">{action}</div>}</div>;
}

export function ThroughputChart({ data, busy = false }: { data: Observability; busy?: boolean }) {
  const total = data.totals.succeeded + data.totals.failed;
  return <Card className="h-full border-border/80 bg-card"><CardHeader className="flex-row items-start justify-between space-y-0 px-5 pb-0 pt-5"><div><CardTitle className="text-sm font-semibold">Execution volume</CardTitle><p className="mt-1 text-[11px] text-muted-foreground">Succeeded and failed executions · last {data.hours === 24 ? "24 hours" : "7 days"}</p></div><span className="rounded-md border border-border px-2 py-1 text-[10px] text-muted-foreground">{total.toLocaleString()} outcomes</span></CardHeader><CardContent className="px-3 pb-4 pt-4">
    {busy ? <Skeleton className="h-[230px] w-full"/> : total === 0 ? <EmptyState title="No execution history in this window" detail="This chart fills from persisted worker lifecycle events as jobs run." icon={Activity}/> : <div role="img" aria-label={`Execution volume chart with ${total} terminal outcomes in the last ${data.hours} hours`}>
      <p className="sr-only">Total {data.totals.succeeded} succeeded, {data.totals.failed} failed, and {data.totals.retried} retries.</p>
      <ResponsiveContainer width="100%" height={230}><AreaChart data={data.buckets} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
        <defs><linearGradient id="successFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.24}/><stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0}/></linearGradient><linearGradient id="failureFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--chart-4)" stopOpacity={0.14}/><stop offset="100%" stopColor="var(--chart-4)" stopOpacity={0}/></linearGradient></defs>
        <CartesianGrid stroke="var(--border)" strokeDasharray="3 5" vertical={false}/><XAxis dataKey="bucket" tickFormatter={(value) => new Date(value).toLocaleTimeString([], { hour: "numeric", ...(data.hours > 24 ? {} : { minute: "2-digit" }) })} minTickGap={35} tick={{ fill: "var(--muted-foreground)", fontSize: 10 }} axisLine={false} tickLine={false}/><YAxis allowDecimals={false} width={32} tick={{ fill: "var(--muted-foreground)", fontSize: 10 }} axisLine={false} tickLine={false}/><Tooltip contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 12, color: "var(--popover-foreground)", fontSize: 12 }} labelFormatter={(value) => formatDate(String(value))} /><Legend wrapperStyle={{ fontSize: 10, color: "var(--muted-foreground)" }} iconType="circle" iconSize={7}/><Area type="monotone" dataKey="succeeded" name="Succeeded" stroke="var(--chart-1)" strokeWidth={2} fill="url(#successFill)"/><Area type="monotone" dataKey="failed" name="Failed" stroke="var(--chart-4)" strokeWidth={2} fill="url(#failureFill)"/>
      </AreaChart></ResponsiveContainer></div>}
  </CardContent></Card>;
}

export function LatencyChart({ data, busy = false }: { data: Observability; busy?: boolean }) {
  const points = data.buckets.map((bucket) => ({ bucket: bucket.bucket, submitted: bucket.submitted, retried: bucket.retried }));
  const hasData = points.some((point) => point.submitted > 0 || point.retried > 0);
  return <Card className="h-full border-border/80 bg-card"><CardHeader className="px-5 pb-0 pt-5"><CardTitle className="text-sm font-semibold">Submission & retry activity</CardTitle><p className="mt-1 text-[11px] text-muted-foreground">Observed lifecycle events · last {data.hours === 24 ? "24 hours" : "7 days"}</p></CardHeader><CardContent className="px-3 pb-4 pt-4">
    {busy ? <Skeleton className="h-[230px] w-full"/> : !hasData ? <EmptyState title="No events in this window" detail="New submissions and retries will appear here." icon={Activity}/> : <div role="img" aria-label="Job submissions and scheduled retries over time"><p className="sr-only">{data.totals.retried} retries recorded in this window.</p><ResponsiveContainer width="100%" height={230}><LineChart data={points} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}><CartesianGrid stroke="var(--border)" strokeDasharray="3 5" vertical={false}/><XAxis dataKey="bucket" tickFormatter={(value) => new Date(value).toLocaleTimeString([], { hour: "numeric" })} minTickGap={35} tick={{ fill: "var(--muted-foreground)", fontSize: 10 }} axisLine={false} tickLine={false}/><YAxis allowDecimals={false} width={32} tick={{ fill: "var(--muted-foreground)", fontSize: 10 }} axisLine={false} tickLine={false}/><Tooltip contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 12, color: "var(--popover-foreground)", fontSize: 12 }} labelFormatter={(value) => formatDate(String(value))}/><Legend wrapperStyle={{ fontSize: 10 }} iconType="circle" iconSize={7}/><Line type="monotone" dataKey="submitted" name="Submitted" stroke="var(--chart-2)" strokeWidth={2} dot={false}/><Line type="monotone" dataKey="retried" name="Retries" stroke="var(--chart-3)" strokeWidth={2} dot={false}/></LineChart></ResponsiveContainer></div>}
  </CardContent></Card>;
}

export function Panel({ title, subtitle, action, children, className = "" }: { title: string; subtitle?: string; action?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return <Card className={`border-border/80 bg-card ${className}`}><CardHeader className="flex-row items-start justify-between space-y-0 px-5 pb-4 pt-5"><div><CardTitle className="text-sm font-semibold">{title}</CardTitle>{subtitle && <p className="mt-1 text-[11px] text-muted-foreground">{subtitle}</p>}</div>{action}</CardHeader><CardContent className="px-5 pb-5 pt-0">{children}</CardContent></Card>;
}

export function PageLoading() { return <div className="space-y-5" role="status" aria-label="Loading view"><Skeleton className="h-10 w-64"/><div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Skeleton className="h-28"/><Skeleton className="h-28"/><Skeleton className="h-28"/><Skeleton className="h-28"/></div><Skeleton className="h-64"/></div>; }
