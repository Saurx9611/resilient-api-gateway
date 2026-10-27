"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import styles from "./dashboard.module.css";

type Route = { id: string; request_count: number; outcomes: Record<string, number>; average_duration_seconds?: number; p95_bucket_upper_bound_seconds?: number; p95_overflow: boolean; average_upstream_seconds?: number; p95_upstream_bucket_upper_bound_seconds?: number; upstream_p95_overflow: boolean; average_redis_seconds?: number; limited: number; redis_errors: number; circuit_state: string; circuit_rejected: number; circuit_failures: number };
type Status = { sampled_at: string; ready: boolean; routes: Route[]; rate_burst_enabled: boolean; rate_burst_request_cap: number; isolated_circuit_demo?: { sampled_at: string; steps: { name: string; gateway_status: number; state: string }[] } };
type Action = { action: string; sampled_at: string; request_count?: number; statuses?: Record<string, number>; duration_milliseconds?: number; message?: string; circuit?: { steps: { name: string; gateway_status: number; state: string }[] } };

function number(value?: number) { return value === undefined ? "—" : value.toLocaleString(); }
function latency(value?: number) { return value === undefined ? "No samples" : `${(value * 1000).toFixed(2)} ms`; }

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
      setData(value); setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Could not load gateway status"); }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => { void refresh(); }, 0);
    const timer = window.setInterval(() => { void refresh(); }, 5000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [refresh]);

  async function run(name: string) {
    if (busy) return;
    setBusy(true); setAction(null); setActionError("");
    try {
      const response = await fetch(`/api/demo/actions/${name}`, { method: "POST", headers: { "Content-Length": "0" } });
      const value = await response.json();
      if (!response.ok) throw new Error(value.message || value.error || `Action unavailable (${response.status})`);
      setAction(value); await refresh();
    } catch (err) { setActionError(err instanceof Error ? err.message : "Demo action failed"); }
    finally { setBusy(false); }
  }

  return <main className={styles.shell}>
    <a className={styles.skip} href="#main">Skip to content</a>
    <header className={styles.top}><Link href="/" className={styles.brand}><span className={styles.mark} aria-hidden="true">R</span> Resilient Gateway</Link><span className={styles.live}><i className={error ? styles.dotDown : data ? styles.dot : styles.dotWait} aria-hidden="true"/> {error ? "Snapshot unavailable" : data ? "Live snapshot · refreshes every 5s" : "Connecting to gateway"}</span></header>
    <section id="main" className={styles.content}>
      <div className={styles.intro}><div><p className={styles.eyebrow}>GATEWAY / OVERVIEW</p><h1>Request control, at a glance.</h1><p className={styles.lede}>Live process counters and safe, bounded demonstrations of gateway behavior.</p></div><div className={`${styles.ready} ${error ? styles.pending : data?.ready ? styles.good : data ? styles.bad : styles.pending}`} aria-live="polite"><span className={error ? styles.dotWait : data?.ready ? styles.dot : styles.dotDown}/>{error && data ? (data.ready ? "Last sample ready" : "Last sample not ready") : data ? (data.ready ? "Ready" : "Not ready") : "Checking"}</div></div>
      {error && <div className={styles.error} role="alert"><span>{error}</span><button onClick={() => void refresh()} className={styles.textButton}>Retry status</button></div>}
      <div className={styles.sectionHeading}><h2>Route state</h2><span>{data ? `Sampled ${new Date(data.sampled_at).toLocaleTimeString()}` : "Waiting for first sample"}</span></div>
      <div className={styles.routes}>
        {(data?.routes ?? []).map(route => <article className={styles.card} key={route.id}>
          <div className={styles.cardHead}><div><span className={styles.routeLabel}>ROUTE</span><h3>/{route.id}</h3></div><span className={`${styles.pill} ${route.circuit_state === "closed" ? styles.pillGood : styles.pillWarn}`}>{route.circuit_state}</span></div>
          <div className={styles.metrics}><div><span>Requests</span><strong>{number(route.request_count)}</strong></div><div><span>Rate limited</span><strong>{number(route.limited)}</strong></div><div><span>Avg request time</span><strong>{latency(route.average_duration_seconds)}</strong></div><div><span>Request p95 bucket</span><strong>{route.p95_overflow ? "> 500 ms" : latency(route.p95_bucket_upper_bound_seconds)}</strong></div><div><span>Avg upstream to headers</span><strong>{latency(route.average_upstream_seconds)}</strong></div><div><span>Upstream p95 to headers</span><strong>{route.upstream_p95_overflow ? "> 500 ms" : latency(route.p95_upstream_bucket_upper_bound_seconds)}</strong></div><div><span>Avg Redis admission</span><strong>{latency(route.average_redis_seconds)}</strong></div></div>
          <div className={styles.statuses} aria-label={`HTTP outcomes for route ${route.id}`}><span>Outcomes</span>{Object.entries(route.outcomes ?? {}).map(([code, count]) => <span className={styles.status} key={code}><b>{code}</b> {number(count)}</span>)}</div>
          <div className={styles.cardFoot}><span>Circuit failures <b>{number(route.circuit_failures)}</b></span><span>Fail-fast <b>{number(route.circuit_rejected)}</b></span><span>Redis errors <b>{number(route.redis_errors)}</b></span></div>
        </article>)}
        {!data && !error && <div className={styles.empty}>Loading the first gateway sample…</div>}
      </div>
      <div className={styles.sectionHeading}><h2>Safe demonstrations</h2><span>Fixed routes · bounded requests · no live configuration</span></div>
      <div className={styles.actions}>
        <article className={styles.actionCard}><span className={styles.actionIndex}>01 / NORMAL</span><h3>Send one request</h3><p>Send one fixed GET through route /one and report its real response.</p><button disabled={busy} onClick={() => void run("normal")}>{busy ? "Running…" : "Send normal request"}</button></article>
        <article className={styles.actionCard}><span className={styles.actionIndex}>02 / RATE LIMIT</span><h3>See the limit</h3><p>Send at most the configured route limit plus one request, up to {data?.rate_burst_request_cap ?? 64} total.</p><button disabled={busy || !data?.rate_burst_enabled} onClick={() => void run("rate-burst")}>{data?.rate_burst_enabled ? "Run bounded burst" : "Burst unavailable"}</button></article>
        <article className={styles.actionCard}><span className={styles.actionIndex}>03 / CIRCUIT</span><h3>Fail, then recover</h3><p>Walk through open, half-open and closed states in an isolated breaker simulation.</p><button disabled={busy} onClick={() => void run("circuit-recovery")}>{busy ? "Running…" : "Show failure & recovery"}</button></article>
      </div>
      <section className={styles.result} aria-live="polite" aria-atomic="true"><div className={styles.resultHead}><h2>Latest demonstration</h2>{action && <time dateTime={action.sampled_at}>{new Date(action.sampled_at).toLocaleTimeString()}</time>}</div>
        {actionError && <p className={styles.actionError} role="alert">{actionError}</p>}
        {!action ? <p className={styles.muted}>No demonstration run in this page session.</p> : <><p>{action.message || `${action.request_count ?? 0} request(s) completed in ${number(action.duration_milliseconds)} ms`}</p>{action.statuses && <div className={styles.resultStatuses}>{Object.entries(action.statuses).map(([status, count]) => <span className={styles.status} key={status}>{status}: <b>{count}</b></span>)}</div>}{action.circuit?.steps && <ol className={styles.steps}>{action.circuit.steps.map((step, i) => <li key={`${step.name}-${i}`}><span>{step.name}</span><span>HTTP {step.gateway_status} · {step.state}</span></li>)}</ol>}</>}
      </section>
      <p className={styles.note}>Request time includes response copying; upstream time ends when response headers arrive; Redis time is limiter Check wall time. Counters are process-lifetime samples read from independent atomics, so concurrent traffic can make values differ slightly within one snapshot. Percentiles are finite histogram bucket upper bounds. No samples are shown as “No samples.” Circuit demonstration outcomes are synthetic and isolated from configured upstreams.</p>
    </section>
    <footer className={styles.footer}>Resilient API Gateway <span>Operational state from this gateway process</span></footer>
  </main>;
}
