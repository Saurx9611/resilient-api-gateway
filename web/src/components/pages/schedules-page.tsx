"use client";

import { useState } from "react";
import { CalendarClock, Check, Clock3, Pause, Plus, RefreshCw, XCircle } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card } from "@/components/ui/card";
import { PageHeading, Panel, EmptyState } from "@/components/console-ui";
import { apiRequest, formatDate, JOB_TYPES, jobTypeLabels, type JobType, type QueuePolicy, type Schedule } from "@/lib/chronos";
import { useAuth } from "@/components/auth-provider";
import { usePoll } from "@/hooks/use-poll";

const initialPayload = JSON.stringify({ records: [{ category: "daily", amount: 1 }] }, null, 2);

export function SchedulesPage() {
  const { token } = useAuth();
  const schedules = usePoll<{ items: Schedule[] }>("v1/schedules?limit=100", 10000);
  const policy = usePoll<QueuePolicy>("v1/settings/queue", 30000);
  const [jobType, setJobType] = useState<JobType>("synthetic_report");
  const [payload, setPayload] = useState(initialPayload);
  const [cron, setCron] = useState("0 9 * * 1-5");
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [priority, setPriority] = useState("0");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function create(event: React.FormEvent) {
    event.preventDefault(); setError("");
    if (!token) return;
    let parsed: unknown;
    try { parsed = JSON.parse(payload); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(); }
    catch { setError("Payload must be a valid JSON object."); return; }
    setBusy(true);
    try {
      const result = await apiRequest<Schedule>(token, "v1/schedules", { method: "POST", body: JSON.stringify({ job_type: jobType, payload: parsed, cron_expression: cron, timezone, priority: Number(priority) }) });
      toast.success("Recurring schedule created", { description: `Next occurrence: ${formatDate(result.next_occurrence_at)}` });
      schedules.refresh();
    } catch (value) { setError(value instanceof Error ? value.message : "Schedule could not be created."); }
    finally { setBusy(false); }
  }

  async function cancel(schedule: Schedule) {
    if (!token || !window.confirm(`Disable this ${schedule.cron_expression} schedule? Already materialized jobs will continue.`)) return;
    const reason = window.prompt("Add an audit reason (3–200 characters):", "operator disabled schedule");
    if (!reason) return;
    try {
      await apiRequest(token, `v1/schedules/${schedule.schedule_id}/cancel`, { method: "POST", body: JSON.stringify({ reason }) });
      toast.success("Schedule disabled"); schedules.refresh();
    } catch (value) { toast.error(value instanceof Error ? value.message : "Could not disable schedule"); }
  }

  return <>
    <PageHeading eyebrow="Automation / Recurrence" title="Schedules" description="Recurring work is materialized by independent scheduler processes. Occurrences have a database-enforced UTC identity." action={<div className="flex items-center gap-1 text-xs text-muted-foreground"><span className="size-1.5 rounded-full bg-chart-1"/> Five-field cron</div>}/>
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.08fr)_minmax(340px,.92fr)]">
      <Panel title="Schedule registry" subtitle="Enabled schedules and their next UTC occurrence">
        {schedules.loading && !schedules.data ? <div role="status" className="space-y-3"><div className="h-14 animate-pulse rounded bg-muted"/><div className="h-14 animate-pulse rounded bg-muted"/></div> : schedules.error && !schedules.data ? <div role="alert" className="py-8 text-sm text-destructive">{schedules.error.message}<Button size="sm" variant="outline" className="ml-3" onClick={schedules.refresh}><RefreshCw size={12}/>Retry</Button></div> : !schedules.data?.items.length ? <EmptyState title="No recurring schedules" detail="Create a cron schedule to materialize repeatable work into the durable job queue." icon={CalendarClock}/> : <div className="space-y-2">{schedules.data.items.map((schedule) => <Card key={schedule.schedule_id} className="border-border/70 bg-background/45 p-4"><div className="flex flex-col gap-3 sm:flex-row sm:items-start"><div className="grid size-9 shrink-0 place-items-center rounded-xl bg-chart-2/10 text-chart-2"><CalendarClock size={16}/></div><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h3 className="text-xs font-semibold">{jobTypeLabels[schedule.job_type]}</h3><span className={`rounded-md px-2 py-0.5 text-[9px] font-medium ${schedule.enabled ? "bg-chart-1/10 text-chart-1" : "bg-muted text-muted-foreground"}`}>{schedule.enabled ? "Enabled" : "Disabled"}</span></div><p className="mono mt-2 text-xs">{schedule.cron_expression}<span className="ml-2 font-sans text-muted-foreground">{schedule.timezone}</span></p><p className="mt-2 text-[10px] text-muted-foreground">Next · {formatDate(schedule.next_occurrence_at)} <span className="mx-1">·</span> priority {schedule.priority}</p><p className="mono mt-1 truncate text-[9px] text-muted-foreground">{schedule.schedule_id}</p></div>{schedule.enabled && <Button variant="outline" size="sm" disabled={busy} onClick={() => void cancel(schedule)}><Pause size={12}/>Disable</Button>}</div></Card>)}</div>}
      </Panel>
      <form onSubmit={create} className="space-y-4">
        <Panel title="Create a schedule" subtitle="Times are interpreted in the selected IANA timezone">
          <div className="space-y-3.5">
            <div><label htmlFor="cron" className="mb-1.5 block text-xs font-medium">Cron expression</label><Input id="cron" value={cron} onChange={(e) => setCron(e.target.value)} required minLength={9} maxLength={128} placeholder="0 9 * * 1-5" className="mono h-10"/><p className="mt-1.5 text-[10px] text-muted-foreground">Minute hour day-of-month month day-of-week</p></div>
            <div><label htmlFor="timezone" className="mb-1.5 block text-xs font-medium">IANA timezone</label><Input id="timezone" value={timezone} onChange={(e) => setTimezone(e.target.value)} required maxLength={64} placeholder="America/New_York" className="h-10"/><p className="mt-1.5 text-[10px] text-muted-foreground">Your browser suggests {Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"}; verify the schedule’s intended timezone.</p></div>
            <div className="grid gap-3 sm:grid-cols-2"><div><label htmlFor="schedule-type" className="mb-1.5 block text-xs font-medium">Workload type</label><select id="schedule-type" value={jobType} onChange={(e) => setJobType(e.target.value as JobType)} className="h-10 w-full rounded-lg border border-input bg-background px-3 text-xs focus-ring">{JOB_TYPES.map((type) => <option key={type} value={type}>{jobTypeLabels[type]}</option>)}</select></div><div><label htmlFor="schedule-priority" className="mb-1.5 block text-xs font-medium">Priority</label><Input id="schedule-priority" type="number" min={-10} max={10} value={priority} onChange={(e) => setPriority(e.target.value)} required className="h-10"/></div></div>
            <div><label htmlFor="schedule-payload" className="mb-1.5 block text-xs font-medium">Payload · JSON object</label><Textarea id="schedule-payload" value={payload} onChange={(e) => setPayload(e.target.value)} rows={5} className="mono bg-muted/25 text-[11px] leading-5"/></div>
            <p className="flex items-start gap-2 rounded-lg bg-muted/50 p-3 text-[10px] leading-5 text-muted-foreground"><Clock3 size={12} className="mt-0.5 shrink-0"/>Spring-forward times that do not exist are skipped. Fall-back duplicate local times run once at the earlier UTC instant. Scheduler catch-up does not coalesce missed occurrences.</p>
            <Button type="submit" className="w-full" disabled={busy}>{busy ? "Validating schedule…" : "Create recurring schedule"}<Plus size={14}/></Button>
            {error && <p role="alert" className="flex items-start gap-2 text-xs text-destructive"><XCircle size={14} className="mt-0.5 shrink-0"/>{error}</p>}
          </div>
        </Panel>
        <Card className="flex items-start gap-3 border-chart-1/20 bg-chart-1/5 p-4"><Check size={14} className="mt-0.5 text-chart-1"/><p className="text-[10px] leading-5 text-muted-foreground">Attempts and runtime use current queue defaults: {policy.data ? `${policy.data.default_max_attempts} attempts, ${policy.data.default_max_runtime_seconds}s runtime` : "loading policy"}. Database uniqueness prevents duplicate materialization across scheduler instances.</p></Card>
      </form>
    </div>
  </>;
}
