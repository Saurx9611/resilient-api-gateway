"use client";

import { useState } from "react";
import { useTheme } from "next-themes";
import { Moon, RefreshCw, RotateCcw, Save, ShieldCheck, Sun } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { PageHeading, Panel } from "@/components/console-ui";
import { apiRequest, type QueuePolicy } from "@/lib/chronos";
import { useAuth } from "@/components/auth-provider";
import { usePoll } from "@/hooks/use-poll";
import { useRefreshPreference } from "@/hooks/use-refresh-preference";

export function SettingsPage() {
  const { token } = useAuth();
  const { theme, setTheme } = useTheme();
  const policy = usePoll<QueuePolicy>("v1/settings/queue", 60000);
  const interval = useRefreshPreference();
  const [saving, setSaving] = useState(false);

  async function savePolicy(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!token) return;
    setSaving(true);
    const values = new FormData(event.currentTarget);
    try {
      await apiRequest<QueuePolicy>(token, "v1/settings/queue", { method: "PUT", body: JSON.stringify({ default_max_attempts: Number(values.get("default_max_attempts")), default_max_runtime_seconds: Number(values.get("default_max_runtime_seconds")) }) });
      toast.success("Queue defaults updated", { description: "New jobs and schedules will use these defaults when an override is omitted." });
      policy.refresh();
    } catch (error) { toast.error(error instanceof Error ? error.message : "Queue policy update failed"); }
    finally { setSaving(false); }
  }

  function savePreferences(nextTheme: string | undefined, nextInterval: number) {
    localStorage.setItem("chronos.refresh-ms", String(nextInterval));
    window.dispatchEvent(new Event("chronos:preferences"));
    if (nextTheme) setTheme(nextTheme);
    toast.success("Preferences saved in this browser");
  }

  return <>
    <PageHeading eyebrow="Configuration / Guardrails" title="Settings" description="Change bounded queue defaults and personal dashboard behavior. Queue policy updates require operator scope and are stored in PostgreSQL." />
    <div className="grid gap-4 xl:grid-cols-[1.1fr_.9fr]">
      <form key={policy.data?.updated_at ?? "initial"} onSubmit={savePolicy}>
        <Panel title="Queue defaults" subtitle="Applied to new submissions only when the request omits an override" action={<span className="rounded-md bg-accent px-2 py-1 text-[9px] font-semibold text-accent-foreground">OPERATOR SCOPE</span>}>
          {policy.error && !policy.data && <p role="alert" className="mb-3 text-xs text-destructive">{policy.error.message}</p>}
          <div className="grid gap-4 sm:grid-cols-2"><div><label htmlFor="default-attempts" className="mb-1.5 block text-xs font-medium">Default maximum attempts</label><Input id="default-attempts" name="default_max_attempts" type="number" min={1} max={10} required defaultValue={policy.data?.default_max_attempts ?? 5} className="h-10"/><p className="mt-1.5 text-[10px] text-muted-foreground">Bounded from 1 to 10. Retries use the existing exponential backoff policy.</p></div><div><label htmlFor="default-runtime" className="mb-1.5 block text-xs font-medium">Default runtime ceiling · seconds</label><Input id="default-runtime" name="default_max_runtime_seconds" type="number" min={1} max={300} required defaultValue={policy.data?.default_max_runtime_seconds ?? 30} className="h-10"/><p className="mt-1.5 text-[10px] text-muted-foreground">Bounded from 1 to 300 seconds. Existing jobs are unchanged.</p></div></div>
          <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4"><p className="text-[10px] text-muted-foreground">{policy.data ? `Last changed ${new Date(policy.data.updated_at).toLocaleString()} by ${policy.data.updated_by}` : "Loading current policy…"}</p><Button disabled={saving || !token} type="submit">{saving ? "Saving…" : "Save queue defaults"}<Save size={14}/></Button></div>
        </Panel>
      </form>
      <div className="space-y-4">
        <Panel title="Profile preferences" subtitle="Saved locally to this browser; not sent to the control API">
          <div className="space-y-5">
            <div><label htmlFor="theme-choice" className="mb-1.5 block text-xs font-medium">Appearance</label><div className="flex items-center gap-2"><Sun size={15} className="text-muted-foreground"/><select id="theme-choice" value={theme ?? "dark"} onChange={(e) => savePreferences(e.target.value, interval)} className="h-10 flex-1 rounded-lg border border-input bg-background px-3 text-xs focus-ring"><option value="dark">Dark</option><option value="light">Light</option><option value="system">System</option></select><Moon size={15} className="text-muted-foreground"/></div></div>
            <div><label htmlFor="refresh-interval" className="mb-1.5 block text-xs font-medium">Live refresh interval</label><div className="flex items-center gap-2"><RefreshCw size={14} className="text-muted-foreground"/><select id="refresh-interval" value={interval} onChange={(e) => savePreferences(theme, Number(e.target.value))} className="h-10 flex-1 rounded-lg border border-input bg-background px-3 text-xs focus-ring"><option value={15000}>15 seconds</option><option value={30000}>30 seconds</option><option value={60000}>60 seconds</option></select></div><p className="mt-1.5 text-[10px] text-muted-foreground">Overview has five live panels. A 15-second floor keeps polling under the API’s default shared request budget.</p></div>
          </div>
        </Panel>
        <Card className="border-chart-2/20 bg-chart-2/5 p-5"><div className="mb-3 flex items-center gap-2"><ShieldCheck size={16} className="text-chart-2"/><h2 className="text-sm font-semibold">Control boundaries</h2></div><ul className="space-y-2 text-[11px] leading-5 text-muted-foreground"><li>Workers remain separate processes; this browser cannot change worker concurrency.</li><li>Rate limits, token scopes, lease duration and scheduler cadence are deployment-level configuration.</li><li>Theme and refresh cadence stay in session-scoped browser preferences, not in job records.</li></ul></Card>
        <Card className="flex items-start gap-3 border-border/80 bg-muted/20 p-4"><RotateCcw size={14} className="mt-0.5 text-muted-foreground"/><p className="text-[10px] leading-5 text-muted-foreground">Queue policy is shared by API replicas. A submission can still set an explicit max-attempt or runtime override within the API’s allowed range.</p></Card>
      </div>
    </div>
  </>;
}
