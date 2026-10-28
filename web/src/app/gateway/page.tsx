"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  Clock,
  ExternalLink,
  Flame,
  Gauge,
  Layers,
  Play,
  RefreshCw,
  Server,
  ShieldAlert,
  ShieldCheck,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

type Route = {
  id: string;
  request_count: number;
  outcomes: Record<string, number>;
  average_duration_seconds?: number;
  p95_bucket_upper_bound_seconds?: number;
  p95_overflow: boolean;
  average_upstream_seconds?: number;
  p95_upstream_bucket_upper_bound_seconds?: number;
  upstream_p95_overflow: boolean;
  average_redis_seconds?: number;
  limited: number;
  redis_errors: number;
  circuit_state: string;
  circuit_rejected: number;
  circuit_failures: number;
};

type Status = {
  sampled_at: string;
  ready: boolean;
  routes: Route[];
  rate_burst_enabled: boolean;
  rate_burst_request_cap: number;
  isolated_circuit_demo?: {
    sampled_at: string;
    steps: { name: string; gateway_status: number; state: string }[];
  };
};

type Action = {
  action: string;
  sampled_at: string;
  request_count?: number;
  statuses?: Record<string, number>;
  duration_milliseconds?: number;
  message?: string;
  circuit?: { steps: { name: string; gateway_status: number; state: string }[] };
};

function formatNumber(value?: number) {
  return value === undefined ? "—" : value.toLocaleString();
}

function formatLatency(value?: number) {
  return value === undefined ? "No samples" : `${(value * 1000).toFixed(2)} ms`;
}

export default function GatewayDashboard() {
  const [data, setData] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState<Action | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/demo/status", { cache: "no-store" });
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || `Status unavailable (${response.status})`);
      setData(value);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load gateway status");
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => {
      void refresh();
    }, 0);
    const timer = window.setInterval(() => {
      void refresh();
    }, 5000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [refresh]);

  async function run(name: string) {
    if (busy) return;
    setBusy(true);
    setAction(null);
    setActionError("");
    try {
      const response = await fetch(`/api/demo/actions/${name}`, {
        method: "POST",
        headers: { "Content-Length": "0" },
      });
      const value = await response.json();
      if (!response.ok) throw new Error(value.message || value.error || `Action unavailable (${response.status})`);
      setAction(value);
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Demo action failed");
    } finally {
      setBusy(false);
    }
  }

  const totalRequests = (data?.routes ?? []).reduce((acc, r) => acc + (r.request_count || 0), 0);
  const totalLimited = (data?.routes ?? []).reduce((acc, r) => acc + (r.limited || 0), 0);
  const totalCircuitRejections = (data?.routes ?? []).reduce((acc, r) => acc + (r.circuit_rejected || 0), 0);

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Top Navigation */}
      <header className="sticky top-0 z-40 border-b border-border bg-card/80 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <div className="grid size-9 place-items-center rounded-xl bg-primary text-primary-foreground font-black text-base shadow-sm">
              R
            </div>
            <div>
              <span className="font-semibold tracking-tight text-foreground text-sm sm:text-base">
                Resilient Gateway
              </span>
              <span className="ml-2 hidden text-xs text-muted-foreground sm:inline">
                High-Performance Reverse Proxy
              </span>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {/* Live Indicator */}
            <div className="flex items-center gap-2 rounded-full border border-border bg-muted/40 px-3 py-1 text-xs">
              <span className="relative flex size-2">
                <span
                  className={`absolute inline-flex size-full animate-ping rounded-full opacity-75 ${
                    error ? "bg-destructive" : data?.ready ? "bg-emerald-500" : "bg-amber-500"
                  }`}
                />
                <span
                  className={`relative inline-flex size-2 rounded-full ${
                    error ? "bg-destructive" : data?.ready ? "bg-emerald-500" : "bg-amber-500"
                  }`}
                />
              </span>
              <span className="font-medium text-foreground">
                {error
                  ? "Snapshot unavailable"
                  : data?.ready
                  ? "Gateway Ready"
                  : data
                  ? "Not Ready"
                  : "Connecting..."}
              </span>
            </div>

            <Button
              variant="outline"
              size="sm"
              onClick={() => void refresh()}
              disabled={busy}
              className="gap-1.5 text-xs"
            >
              <RefreshCw size={13} className={busy ? "animate-spin" : ""} />
              Refresh
            </Button>

            <Button variant="ghost" size="sm" asChild className="text-xs">
              <Link href="/">
                Chronos Jobs <ExternalLink size={12} className="ml-1 opacity-70" />
              </Link>
            </Button>
          </div>
        </div>
      </header>

      {/* Main Container */}
      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        {/* Page Hero */}
        <div className="mb-8 flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
          <div>
            <p className="text-[11px] font-semibold tracking-widest text-primary uppercase">
              Operational Telemetry
            </p>
            <h1 className="mt-1 text-2xl font-bold tracking-tight sm:text-3xl">
              Gateway Control & Failure Resilience
            </h1>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
              Real-time monitoring of reverse proxy routes, sliding-window rate limiting, and circuit breaker states.
            </p>
          </div>
          {data && (
            <p className="text-xs text-muted-foreground">
              Sampled at <span className="font-mono">{new Date(data.sampled_at).toLocaleTimeString()}</span>
            </p>
          )}
        </div>

        {/* Global Error Banner */}
        {error && (
          <div className="mb-6 flex items-center justify-between rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
            <div className="flex items-center gap-2">
              <AlertCircle size={18} />
              <span>{error}</span>
            </div>
            <Button variant="outline" size="sm" onClick={() => void refresh()}>
              Retry
            </Button>
          </div>
        )}

        {/* Top Summary Stats */}
        <div className="mb-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Card className="border-border/80 bg-card">
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">Total Requests</CardTitle>
              <Activity className="size-4 text-primary" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{formatNumber(totalRequests)}</div>
              <p className="mt-1 text-xs text-muted-foreground">Served through gateway</p>
            </CardContent>
          </Card>

          <Card className="border-border/80 bg-card">
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">Rate Limited</CardTitle>
              <Flame className="size-4 text-amber-500" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-amber-500">{formatNumber(totalLimited)}</div>
              <p className="mt-1 text-xs text-muted-foreground">Redis atomic limiter rejected</p>
            </CardContent>
          </Card>

          <Card className="border-border/80 bg-card">
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">Circuit Rejections</CardTitle>
              <ShieldAlert className="size-4 text-rose-500" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-rose-500">{formatNumber(totalCircuitRejections)}</div>
              <p className="mt-1 text-xs text-muted-foreground">Fail-fast during open state</p>
            </CardContent>
          </Card>

          <Card className="border-border/80 bg-card">
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">Active Routes</CardTitle>
              <Server className="size-4 text-emerald-500" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{data?.routes?.length ?? 0} Routes</div>
              <p className="mt-1 text-xs text-muted-foreground">Immutable route table snapshot</p>
            </CardContent>
          </Card>
        </div>

        {/* Route Details Cards */}
        <div className="mb-8">
          <h2 className="mb-4 text-lg font-semibold tracking-tight">Active Gateway Routes</h2>
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {(data?.routes ?? []).map((route) => {
              const isClosed = route.circuit_state === "closed";
              const isOpen = route.circuit_state === "open";

              return (
                <Card key={route.id} className="border-border/80 bg-card overflow-hidden">
                  <div className="border-b border-border bg-muted/30 px-5 py-4 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="grid size-8 place-items-center rounded-lg bg-primary/10 text-primary font-mono text-sm font-bold">
                        /{route.id}
                      </div>
                      <div>
                        <h3 className="font-semibold leading-tight text-foreground">
                          Route /{route.id}
                        </h3>
                        <p className="text-xs text-muted-foreground">Target Upstream: {route.id}</p>
                      </div>
                    </div>

                    <Badge
                      variant="outline"
                      className={`gap-1.5 px-2.5 py-1 text-xs font-medium capitalize ${
                        isClosed
                          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                          : isOpen
                          ? "border-destructive/30 bg-destructive/10 text-destructive"
                          : "border-amber-500/30 bg-amber-500/10 text-amber-500"
                      }`}
                    >
                      <span
                        className={`size-1.5 rounded-full ${
                          isClosed ? "bg-emerald-500" : isOpen ? "bg-destructive" : "bg-amber-500"
                        }`}
                      />
                      Circuit {route.circuit_state}
                    </Badge>
                  </div>

                  <CardContent className="p-5">
                    {/* Latency & Throughput Grid */}
                    <div className="grid grid-cols-2 gap-4 rounded-xl border border-border/60 bg-muted/10 p-4 sm:grid-cols-3">
                      <div>
                        <span className="block text-[11px] text-muted-foreground">Total Requests</span>
                        <span className="text-lg font-semibold font-mono text-foreground">
                          {formatNumber(route.request_count)}
                        </span>
                      </div>
                      <div>
                        <span className="block text-[11px] text-muted-foreground">Rate Limited</span>
                        <span className="text-lg font-semibold font-mono text-amber-500">
                          {formatNumber(route.limited)}
                        </span>
                      </div>
                      <div>
                        <span className="block text-[11px] text-muted-foreground">Avg Duration</span>
                        <span className="text-lg font-semibold font-mono text-foreground">
                          {formatLatency(route.average_duration_seconds)}
                        </span>
                      </div>
                      <div>
                        <span className="block text-[11px] text-muted-foreground">Request p95</span>
                        <span className="text-lg font-semibold font-mono text-foreground">
                          {route.p95_overflow ? "> 500 ms" : formatLatency(route.p95_bucket_upper_bound_seconds)}
                        </span>
                      </div>
                      <div>
                        <span className="block text-[11px] text-muted-foreground">Upstream Time</span>
                        <span className="text-lg font-semibold font-mono text-foreground">
                          {formatLatency(route.average_upstream_seconds)}
                        </span>
                      </div>
                      <div>
                        <span className="block text-[11px] text-muted-foreground">Redis Latency</span>
                        <span className="text-lg font-semibold font-mono text-foreground">
                          {formatLatency(route.average_redis_seconds)}
                        </span>
                      </div>
                    </div>

                    {/* HTTP Outcomes */}
                    <div className="mt-5">
                      <span className="mb-2 block text-xs font-medium text-muted-foreground">
                        HTTP Response Outcomes
                      </span>
                      <div className="flex flex-wrap gap-2">
                        {Object.entries(route.outcomes ?? {}).map(([code, count]) => {
                          const is2xx = code.startsWith("2");
                          const is4xx = code.startsWith("4");
                          const is5xx = code.startsWith("5");

                          return (
                            <span
                              key={code}
                              className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-mono font-medium ${
                                is2xx
                                  ? "border-emerald-500/20 bg-emerald-500/5 text-emerald-600 dark:text-emerald-400"
                                  : is4xx
                                  ? "border-amber-500/20 bg-amber-500/5 text-amber-600 dark:text-amber-400"
                                  : is5xx
                                  ? "border-destructive/20 bg-destructive/5 text-destructive"
                                  : "border-border bg-muted/30 text-muted-foreground"
                              }`}
                            >
                              <span>{code}</span>
                              <strong className="text-foreground">{formatNumber(count)}</strong>
                            </span>
                          );
                        })}
                      </div>
                    </div>

                    {/* Circuit & Failure Telemetry */}
                    <div className="mt-5 flex items-center justify-between border-t border-border pt-4 text-xs text-muted-foreground">
                      <span>
                        Failures: <strong className="text-foreground">{formatNumber(route.circuit_failures)}</strong>
                      </span>
                      <span>
                        Fast-Rejections:{" "}
                        <strong className="text-foreground">{formatNumber(route.circuit_rejected)}</strong>
                      </span>
                      <span>
                        Redis Errors:{" "}
                        <strong className="text-foreground">{formatNumber(route.redis_errors)}</strong>
                      </span>
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        </div>

        {/* Safe Demonstrations Lab */}
        <div className="mb-8">
          <div className="mb-4">
            <h2 className="text-lg font-semibold tracking-tight">Interactive Demonstration Lab</h2>
            <p className="text-xs text-muted-foreground">
              Trigger safe, bounded failure scenarios and live traffic to observe gateway reaction.
            </p>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {/* Action 1: Normal Request */}
            <Card className="border-border/80 bg-card">
              <CardHeader className="pb-3">
                <span className="text-[10px] font-bold text-primary tracking-wider uppercase">
                  Scenario 01 / Normal
                </span>
                <CardTitle className="text-base font-semibold">Send Single Request</CardTitle>
                <CardDescription className="text-xs">
                  Sends a real proxied GET request to upstream <code className="font-mono">/one</code>.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Button
                  className="w-full gap-2"
                  disabled={busy}
                  onClick={() => void run("normal")}
                >
                  <Play size={14} className={busy ? "animate-spin" : ""} />
                  {busy ? "Executing..." : "Send Request"}
                </Button>
              </CardContent>
            </Card>

            {/* Action 2: Rate Limit Burst */}
            <Card className="border-border/80 bg-card">
              <CardHeader className="pb-3">
                <span className="text-[10px] font-bold text-amber-500 tracking-wider uppercase">
                  Scenario 02 / Rate Limiting
                </span>
                <CardTitle className="text-base font-semibold">Trigger Burst Limit</CardTitle>
                <CardDescription className="text-xs">
                  Sends up to {data?.rate_burst_request_cap ?? 64} burst requests to trigger atomic 429 limiting.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Button
                  variant="secondary"
                  className="w-full gap-2"
                  disabled={busy || !data?.rate_burst_enabled}
                  onClick={() => void run("rate-burst")}
                >
                  <Flame size={14} className="text-amber-500" />
                  {data?.rate_burst_enabled ? "Run Burst Test" : "Burst Unavailable"}
                </Button>
              </CardContent>
            </Card>

            {/* Action 3: Circuit Recovery */}
            <Card className="border-border/80 bg-card">
              <CardHeader className="pb-3">
                <span className="text-[10px] font-bold text-rose-500 tracking-wider uppercase">
                  Scenario 03 / Circuit Breaker
                </span>
                <CardTitle className="text-base font-semibold">Simulate Failure & Recovery</CardTitle>
                <CardDescription className="text-xs">
                  Simulates upstream failure, triggers breaker tripping, and demonstrates probe recovery.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Button
                  variant="outline"
                  className="w-full gap-2 border-rose-500/30 hover:bg-rose-500/10 text-rose-500"
                  disabled={busy}
                  onClick={() => void run("circuit-recovery")}
                >
                  <ShieldAlert size={14} />
                  Simulate Recovery
                </Button>
              </CardContent>
            </Card>
          </div>
        </div>

        {/* Live Demonstration Results Box */}
        <Card className="mb-8 border-border/80 bg-card">
          <CardHeader className="flex flex-row items-center justify-between border-b border-border pb-4">
            <div>
              <CardTitle className="text-sm font-semibold">Latest Scenario Execution</CardTitle>
              <CardDescription className="text-xs">
                Real-time output and circuit state transitions
              </CardDescription>
            </div>
            {action && (
              <span className="font-mono text-xs text-muted-foreground">
                {new Date(action.sampled_at).toLocaleTimeString()}
              </span>
            )}
          </CardHeader>
          <CardContent className="p-5">
            {actionError && (
              <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
                {actionError}
              </div>
            )}

            {!action ? (
              <p className="text-xs text-muted-foreground">
                Click any of the scenario buttons above to run a live demonstration.
              </p>
            ) : (
              <div className="space-y-4">
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                  <span className="text-sm font-medium text-foreground">
                    {action.message ||
                      `Completed ${action.request_count ?? 0} request(s) in ${formatNumber(
                        action.duration_milliseconds
                      )} ms`}
                  </span>
                  {action.statuses && (
                    <div className="flex gap-2">
                      {Object.entries(action.statuses).map(([st, cnt]) => (
                        <Badge key={st} variant="secondary" className="font-mono text-xs">
                          {st}: {cnt}
                        </Badge>
                      ))}
                    </div>
                  )}
                </div>

                {/* Step-by-Step Circuit State Timeline */}
                {action.circuit?.steps && (
                  <div className="mt-4 rounded-xl border border-border bg-muted/20 p-4">
                    <span className="mb-3 block text-xs font-semibold uppercase text-muted-foreground tracking-wider">
                      Circuit State Progression
                    </span>
                    <div className="space-y-2">
                      {action.circuit.steps.map((step, idx) => (
                        <div
                          key={`${step.name}-${idx}`}
                          className="flex items-center justify-between rounded-lg border border-border/50 bg-card p-2.5 text-xs font-mono"
                        >
                          <span className="font-semibold text-foreground">{step.name}</span>
                          <div className="flex items-center gap-2">
                            <span
                              className={`rounded px-1.5 py-0.5 text-[11px] font-bold ${
                                step.gateway_status === 200
                                  ? "bg-emerald-500/10 text-emerald-500"
                                  : "bg-destructive/10 text-destructive"
                              }`}
                            >
                              HTTP {step.gateway_status}
                            </span>
                            <Badge variant="outline" className="text-[10px] capitalize">
                              {step.state}
                            </Badge>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Engineering Footer Notes */}
        <footer className="rounded-xl border border-border/60 bg-muted/10 p-5 text-xs text-muted-foreground leading-relaxed">
          <p>
            <strong>Engineering Contract:</strong> Request latency includes full response copying; upstream timing
            records the duration until upstream response headers arrive; Redis time represents the atomic Lua sliding-window
            check. Percentiles are finite histogram bucket upper bounds. Circuit breaker transitions follow strict
            Closed → Open → Half-Open state machines with concurrent probe admission limits.
          </p>
        </footer>
      </main>
    </div>
  );
}
