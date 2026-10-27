"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertCircle, ArrowRight, CalendarClock, Check, Info, KeyRound, TerminalSquare, Timer } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card } from "@/components/ui/card";
import { PageHeading, Panel } from "@/components/console-ui";
import { apiRequest, jobTypeLabels, type JobType, type QueuePolicy } from "@/lib/chronos";
import { useAuth } from "@/components/auth-provider";
import { usePoll } from "@/hooks/use-poll";

const examples: Record<JobType, string> = {
  synthetic_report: JSON.stringify({ records: [{ category: "orders", amount: 18 }, { category: "orders", amount: 7 }, { category: "returns", amount: -2 }] }, null, 2),
  csv_transform: JSON.stringify({ csv_text: "name,region\nalex,west\nkim,east\n", column: "name" }, null, 2),
  notification_simulation: JSON.stringify({ template: "receipt", recipient_ref: "customer_001", delay_ms: 0 }, null, 2),
  image_metadata: JSON.stringify({ png_base64: "<base64-encoded PNG bytes>" }, null, 2),
};

export function CreateJobPage() {
  const router = useRouter();
  const { token } = useAuth();
  const policy = usePoll<QueuePolicy>("v1/settings/queue", 30000);
  const [jobType, setJobType] = useState<JobType>("synthetic_report");
  const [payload, setPayload] = useState(examples.synthetic_report);
  const [runAt, setRunAt] = useState("");
  const [priority, setPriority] = useState("0");
  const [maxAttempts, setMaxAttempts] = useState("");
  const [runtime, setRuntime] = useState("");
  const idempotencyKey = useRef<string | null>(null);
  const [validation, setValidation] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault(); setValidation("");
    let parsed: unknown;
    try { parsed = JSON.parse(payload); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(); }
    catch { setValidation("Payload must be a valid JSON object."); return; }
    if (!token) return;
    setBusy(true);
    const body: Record<string, unknown> = { job_type: jobType, payload: parsed, priority: Number(priority) };
    if (runAt) { const date = new Date(runAt); if (Number.isNaN(date.valueOf())) { setValidation("Choose a valid run time."); setBusy(false); return; } body.run_at = date.toISOString(); }
    if (maxAttempts) body.max_attempts = Number(maxAttempts);
    if (runtime) body.max_runtime_seconds = Number(runtime);
    const requestKey = idempotencyKey.current ?? `web-${crypto.randomUUID()}`;
    idempotencyKey.current = requestKey;
    try {
      const result = await apiRequest<{ job_id: number }>(token, "v1/jobs", { method: "POST", headers: { "Idempotency-Key": requestKey }, body: JSON.stringify(body) });
      toast.success(`Job #${result.job_id} accepted`, { description: "The job is recorded and available for independent workers." });
      router.push(`/jobs/${result.job_id}`);
    } catch (error) { setValidation(error instanceof Error ? error.message : "Job submission failed."); toast.error("Job was not confirmed", { description: error instanceof Error ? error.message : "Check the control plane and try again." }); }
    finally { setBusy(false); }
  }

  return <>
    <PageHeading eyebrow="Queue / Submission" title="Create a job" description="Submit an allowlisted workload now or at a future time. Validation and request fingerprinting are enforced by the control plane." />
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(280px,.7fr)]">
      <form onSubmit={submit} className="space-y-4">
        <Panel title="Job definition" subtitle="Choose a registered handler and provide its validated JSON input">
          <div className="space-y-4">
            <div><label htmlFor="job-type" className="mb-1.5 block text-xs font-medium">Workload type</label><select id="job-type" value={jobType} onChange={(e) => { const next = e.target.value as JobType; setJobType(next); setPayload(examples[next]); }} className="h-10 w-full rounded-lg border border-input bg-background px-3 text-sm focus-ring">{Object.entries(jobTypeLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
            <div><div className="mb-1.5 flex items-center justify-between gap-2"><label htmlFor="payload" className="text-xs font-medium">Payload · JSON object</label><span className="mono text-[10px] text-muted-foreground">Maximum request 64 KiB</span></div><Textarea id="payload" spellCheck={false} value={payload} onChange={(e) => setPayload(e.target.value)} rows={11} required className="mono min-h-[220px] resize-y bg-muted/25 text-xs leading-5" aria-describedby="payload-help"/><p id="payload-help" className="mt-1.5 text-[10px] text-muted-foreground">Accepted shape is checked against the {jobTypeLabels[jobType]} handler allowlist.</p></div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div><label htmlFor="run-at" className="mb-1.5 block text-xs font-medium">Run time</label><Input id="run-at" type="datetime-local" value={runAt} onChange={(e) => setRunAt(e.target.value)} className="h-10"/><p className="mt-1.5 text-[10px] text-muted-foreground">Leave empty to enqueue immediately · displayed in your local timezone</p></div>
              <div><label htmlFor="priority" className="mb-1.5 block text-xs font-medium">Priority <span className="font-normal text-muted-foreground">(-10 to 10)</span></label><Input id="priority" type="number" min={-10} max={10} step={1} value={priority} onChange={(e) => setPriority(e.target.value)} required className="h-10"/><p className="mt-1.5 text-[10px] text-muted-foreground">Higher-priority queued jobs are claimed first.</p></div>
            </div>
          </div>
        </Panel>
        <Panel title="Execution bounds" subtitle="Leave blank to use current queue defaults">
          <div className="grid gap-3 sm:grid-cols-2"><div><label htmlFor="max-attempts" className="mb-1.5 block text-xs font-medium">Maximum attempts</label><Input id="max-attempts" type="number" min={1} max={10} value={maxAttempts} onChange={(e) => setMaxAttempts(e.target.value)} placeholder={`Queue default · ${policy.data?.default_max_attempts ?? "loading"}`} className="h-10"/><p className="mt-1.5 text-[10px] text-muted-foreground">1 to 10 attempts before dead-lettering.</p></div><div><label htmlFor="max-runtime" className="mb-1.5 block text-xs font-medium">Maximum runtime · seconds</label><Input id="max-runtime" type="number" min={1} max={300} value={runtime} onChange={(e) => setRuntime(e.target.value)} placeholder={`Queue default · ${policy.data?.default_max_runtime_seconds ?? "loading"}`} className="h-10"/><p className="mt-1.5 text-[10px] text-muted-foreground">1 to 300 seconds. Worker enforces the deadline.</p></div></div>
        </Panel>
        <div className="flex flex-col-reverse justify-between gap-3 rounded-xl border border-border bg-card p-4 sm:flex-row sm:items-center"><p className="text-[10px] leading-5 text-muted-foreground"><KeyRound size={12} className="mr-1 inline"/>An idempotency key is generated on first submit and reused if you retry after a network error.</p><div className="flex gap-2"><Button type="button" variant="outline" onClick={() => router.push("/jobs")}>Discard</Button><Button type="submit" disabled={busy}>{busy ? "Submitting…" : runAt ? "Schedule job" : "Submit job"}<ArrowRight size={14}/></Button></div></div>
        {validation && <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-xs text-destructive"><AlertCircle size={14} className="mr-2 inline"/>{validation}</p>}
      </form>
      <div className="space-y-4">
        <Card className="border-border/80 bg-card p-5"><div className="mb-4 flex items-center gap-2"><TerminalSquare size={15} className="text-chart-2"/><h2 className="text-sm font-semibold">Supported workload</h2></div><p className="text-xs leading-5 text-muted-foreground">{jobTypeLabels[jobType]} runs in a separate worker process. Payloads are data only: the API does not load user code, invoke a shell, or perform arbitrary network delivery.</p><div className="mt-4 rounded-xl border border-border bg-muted/35 p-3"><div className="flex items-center gap-2 text-[11px] font-medium"><Check size={13} className="text-chart-1"/>Registered handler</div><p className="mono mt-1.5 break-all text-[10px] text-muted-foreground">{jobType}</p></div></Card>
        <Card className="border-chart-3/25 bg-chart-3/5 p-5"><div className="mb-3 flex items-center gap-2 text-chart-3"><Info size={15}/><h2 className="text-sm font-semibold">Execution semantics</h2></div><ul className="space-y-2 text-[11px] leading-5 text-muted-foreground"><li className="flex gap-2"><Timer size={13} className="mt-0.5 shrink-0"/>Delivery is at-least-once. Handlers with external effects need idempotency.</li><li className="flex gap-2"><CalendarClock size={13} className="mt-0.5 shrink-0"/>A future time is an offset-aware instant; schedules use explicit IANA time zones.</li></ul><Button variant="link" size="sm" className="mt-2 h-auto p-0 text-xs" asChild><a href="/docs" target="_blank">Open API reference<ArrowRight size={12}/></a></Button></Card>
      </div>
    </div>
  </>;
}
