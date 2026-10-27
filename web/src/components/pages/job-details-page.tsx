"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { ArrowLeft, Ban, CheckCircle2, Clock3, ExternalLink, FileClock, Layers3, Play, RotateCcw, UserRound } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Job, JobEvent, Page, apiRequest, formatDate, jobTypeLabels, shortId } from "@/lib/chronos";
import { useAuth } from "@/components/auth-provider";
import { usePoll } from "@/hooks/use-poll";
import { EmptyState, LoadState, PageHeading, Panel, RefreshStamp, StateBadge } from "@/components/console-ui";

function eventLabel(event: JobEvent) {
  const labels: Record<string, string> = { enqueued: "Enqueued", scheduled: "Created by schedule", claimed: "Lease claimed", lease_reclaimed: "Lease reclaimed", succeeded: "Execution succeeded", retry_scheduled: "Retry scheduled", timed_out: "Runtime limit exceeded", permanent_failure: "Permanent failure", abandoned: "Moved to dead letter", lease_lost: "Lease lost", cancel_requested: "Cancellation requested", canceled: "Canceled", operator_retry: "Operator retry", replay_created: "Replay created", effect_applied: "Demonstration effect applied", effect_deduplicated: "Duplicate effect suppressed" };
  return labels[event.event_type] ?? event.event_type.replaceAll("_", " ");
}

export function JobDetailsPage() {
  const params = useParams<{ jobId: string }>();
  const id = params.jobId;
  const router = useRouter();
  const { token } = useAuth();
  const [busy, setBusy] = useState(false);
  const jobPoll = usePoll<Job>(`v1/jobs/${id}`, 5000);
  const attemptsPoll = usePoll<Page<JobEvent>>(`v1/jobs/${id}/attempts?limit=100`, 5000);
  async function perform(action: "cancel" | "retry" | "replay") {
    if (!jobPoll.data || !token) return;
    const label = action === "cancel" ? "cancel this job" : action === "retry" ? "reset its attempt budget and requeue it" : "create a new replay job";
    if (!window.confirm(`Are you sure you want to ${label}?`)) return;
    const reason = window.prompt("Add an audit reason (3–200 characters):", action === "cancel" ? "operator requested cancellation" : action === "retry" ? "operator approved retry" : "operator approved replay");
    if (!reason) return;
    setBusy(true);
    try {
      const result = await apiRequest<{ job_id: number }>(token, `v1/jobs/${id}/${action}`, { method: "POST", headers: action === "replay" ? { "Idempotency-Key": `ui-replay-${id}-${crypto.randomUUID()}` } : undefined, body: JSON.stringify({ reason }) });
      toast.success(action === "replay" ? `Replay created as job #${result.job_id}` : action === "retry" ? "Job was queued for retry" : "Cancellation requested");
      jobPoll.refresh(); attemptsPoll.refresh();
      if (action === "replay") router.push(`/jobs/${result.job_id}`);
    } catch (error) { toast.error(error instanceof Error ? error.message : "Action failed"); }
    finally { setBusy(false); }
  }
  const job = jobPoll.data;
  const terminalCanReplay = job?.state === "dead" || job?.state === "succeeded";
  return <>
    <PageHeading eyebrow="Jobs / Execution record" title={job ? `Job #${job.id}` : "Job details"} description={job ? `${jobTypeLabels[job.job_type]} · submitted ${formatDate(job.created_at)}` : "Inspect the durable state, payload metadata, ownership, and every recorded lease transition."} action={<><Button variant="outline" asChild><Link href="/jobs"><ArrowLeft size={14}/> All jobs</Link></Button>{job && ["queued", "running"].includes(job.state) && <Button variant="destructive" disabled={busy} onClick={() => void perform("cancel")}><Ban size={14}/> Cancel</Button>}{job?.state === "dead" && <Button variant="outline" disabled={busy} onClick={() => void perform("retry")}><RotateCcw size={14}/> Retry</Button>}{terminalCanReplay && <Button disabled={busy} onClick={() => void perform("replay")}><Play size={14}/> Replay</Button>}</>} />
    <div className="mb-4 flex justify-end"><RefreshStamp at={jobPoll.updatedAt} loading={jobPoll.loading} onRefresh={() => { jobPoll.refresh(); attemptsPoll.refresh(); }}/></div>
    <LoadState loading={jobPoll.loading} error={jobPoll.error} hasData={job !== null} onRetry={jobPoll.refresh}/>
    {job && <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Card className="border-border/80 p-4"><p className="text-[10px] uppercase tracking-[.14em] text-muted-foreground">Current state</p><div className="mt-3"><StateBadge state={job.state}/></div></Card>
        <Card className="border-border/80 p-4"><p className="text-[10px] uppercase tracking-[.14em] text-muted-foreground">Attempts</p><p className="mono mt-2 text-2xl font-semibold">{job.attempts}<span className="text-sm font-normal text-muted-foreground"> / {job.max_attempts}</span></p></Card>
        <Card className="border-border/80 p-4"><p className="text-[10px] uppercase tracking-[.14em] text-muted-foreground">Priority</p><p className="mono mt-2 text-2xl font-semibold">{job.priority > 0 ? "+" : ""}{job.priority}</p></Card>
        <Card className="border-border/80 p-4"><p className="text-[10px] uppercase tracking-[.14em] text-muted-foreground">Lease generation</p><p className="mono mt-2 text-2xl font-semibold">{job.generation ?? 0}</p></Card>
      </div>
      <div className="mt-4 grid gap-4 xl:grid-cols-[1.45fr_1fr]">
        <Panel title="Execution timeline" subtitle="Append-only worker events, including lease ownership and recovery generations" action={<RefreshStamp at={attemptsPoll.updatedAt} loading={attemptsPoll.loading} onRefresh={attemptsPoll.refresh}/> }>
          <LoadState loading={attemptsPoll.loading} error={attemptsPoll.error} hasData={attemptsPoll.data !== null} onRetry={attemptsPoll.refresh}/>
          {!attemptsPoll.loading && !attemptsPoll.error && !attemptsPoll.data?.items.length ? <EmptyState title="No attempt events" detail="The event ledger has no entries for this job." icon={FileClock}/> : <ol className="space-y-0">{attemptsPoll.data?.items.map((event, index) => <li key={event.id} className="relative flex gap-4 pb-6 last:pb-0"><div className="relative flex w-4 justify-center"><span className={`relative z-10 mt-0.5 grid size-4 place-items-center rounded-full border ${event.event_type === "succeeded" ? "border-chart-1/40 bg-chart-1/15 text-chart-1" : ["timed_out", "abandoned", "permanent_failure", "lease_lost"].includes(event.event_type) ? "border-destructive/30 bg-destructive/10 text-destructive" : "border-chart-2/30 bg-chart-2/10 text-chart-2"}`}>{event.event_type === "succeeded" ? <CheckCircle2 size={10}/> : <span className="size-1 rounded-full bg-current"/>}</span>{index < (attemptsPoll.data?.items.length ?? 0) - 1 && <span className="absolute top-4 h-full w-px bg-border"/>}</div><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1"><h3 className="text-xs font-semibold">{eventLabel(event)}</h3><time className="text-[10px] text-muted-foreground" dateTime={event.occurred_at}>{formatDate(event.occurred_at)}</time></div><div className="mono mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted-foreground"><span>attempt {event.attempt}</span><span>generation {event.generation}</span>{event.worker_id && <span className="inline-flex items-center gap-1"><UserRound size={10}/>{shortId(event.worker_id, 22)}</span>}</div>{Object.keys(event.details ?? {}).length > 0 && <pre className="mono mt-2 overflow-x-auto rounded-lg bg-muted/60 p-2.5 text-[10px] leading-4 text-muted-foreground">{JSON.stringify(event.details, null, 2)}</pre>}</div></li>)}</ol>}
        </Panel>
        <div className="space-y-4">
          <Panel title="Execution context" subtitle="Current record from PostgreSQL"><dl className="grid grid-cols-[120px_1fr] gap-x-4 gap-y-3 text-xs"><dt className="text-muted-foreground">Type</dt><dd>{jobTypeLabels[job.job_type]}</dd><dt className="text-muted-foreground">Created</dt><dd>{formatDate(job.created_at)}</dd><dt className="text-muted-foreground">Run after</dt><dd>{formatDate(job.available_at)}</dd><dt className="text-muted-foreground">Owner</dt><dd className="mono break-all">{job.worker_id ?? "Not currently leased"}</dd><dt className="text-muted-foreground">Lease until</dt><dd>{job.lease_until ? formatDate(job.lease_until) : "—"}</dd><dt className="text-muted-foreground">Runtime limit</dt><dd>{job.max_runtime_seconds ?? "—"} seconds</dd><dt className="text-muted-foreground">Submitted by</dt><dd>{job.submitted_by ?? "—"}</dd>{job.replay_of && <><dt className="text-muted-foreground">Replay source</dt><dd><Link className="text-chart-2 hover:underline" href={`/jobs/${job.replay_of}`}>Job #{job.replay_of} <ExternalLink size={11} className="inline"/></Link></dd></>}{job.cancel_requested_at && <><dt className="text-muted-foreground">Cancel requested</dt><dd>{formatDate(job.cancel_requested_at)}</dd></>}{job.last_error_code && <><dt className="text-muted-foreground">Last error</dt><dd className="mono text-destructive">{job.last_error_code}</dd></>}</dl></Panel>
          <Panel title="Payload" subtitle="Validated input accepted by the allowlisted handler"><div className="mb-3 flex items-center gap-2 text-[10px] text-muted-foreground"><Layers3 size={13}/> JSON · {new TextEncoder().encode(JSON.stringify(job.payload ?? {})).length.toLocaleString()} bytes</div><pre className="mono max-h-[320px] overflow-auto rounded-xl border border-border bg-muted/45 p-4 text-[11px] leading-5 text-foreground">{JSON.stringify(job.payload ?? {}, null, 2)}</pre></Panel>
          {job.result && <Panel title="Result" subtitle="Persisted handler output"><pre className="mono max-h-[280px] overflow-auto rounded-xl border border-border bg-muted/45 p-4 text-[11px] leading-5">{JSON.stringify(job.result, null, 2)}</pre></Panel>}
          <Card className="flex items-start gap-3 border-border/80 bg-muted/25 p-4"><Clock3 size={15} className="mt-0.5 shrink-0 text-muted-foreground"/><p className="text-[11px] leading-5 text-muted-foreground">Execution is at-least-once. Retrying or replaying can repeat external effects; demonstration handlers are idempotent where their effect ledger applies.</p></Card>
        </div>
      </div>
    </>}
  </>;
}
