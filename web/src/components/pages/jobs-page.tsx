"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowLeft, ArrowRight, FilterX, Plus, Search, SlidersHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { JobTable, LoadState, PageHeading, Panel, RefreshStamp } from "@/components/console-ui";
import { JOB_STATES, JOB_TYPES, type Job, type Page } from "@/lib/chronos";
import { queryString } from "@/lib/chronos";
import { usePoll } from "@/hooks/use-poll";

export function JobsPage({ initialState = "", title = "Jobs", description = "Search the durable job ledger, inspect attempts, and operate on work by its current state." }: { initialState?: string; title?: string; description?: string }) {
  const [state, setState] = useState(initialState);
  const [type, setType] = useState("");
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  const [cursor, setCursor] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  useEffect(() => { const timer = window.setTimeout(() => { setSearch(input.trim()); setCursor(""); setHistory([]); }, 250); return () => window.clearTimeout(timer); }, [input]);
  const path = useMemo(() => `v1/jobs${queryString({ limit: 25, state, job_type: type, q: search, cursor })}`, [state, type, search, cursor]);
  const page = usePoll<Page<Job>>(path, 7000);
  const hasFilter = Boolean(state || type || search);
  function reset() { setState(""); setType(""); setInput(""); setSearch(""); setCursor(""); setHistory([]); }
  function next() { if (page.data?.next_cursor) { setHistory((v) => [...v, cursor]); setCursor(page.data.next_cursor); } }
  function previous() { const v = [...history]; const prev = v.pop() ?? ""; setHistory(v); setCursor(prev); }
  return <>
    <PageHeading eyebrow={initialState === "dead" ? "Queue / Failure recovery" : "Queue / Work items"} title={title} description={description} action={<Button asChild><Link href="/jobs/new"><Plus size={15}/> Create job</Link></Button>}/>
    <Panel title="Job ledger" subtitle="Newest first · filters apply on the server" action={<RefreshStamp at={page.updatedAt} loading={page.loading} onRefresh={page.refresh}/> }>
      <div className="mb-4 flex flex-col gap-2 border-b border-border pb-4 md:flex-row md:items-center">
        <div className="relative min-w-0 flex-1"><Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"/><Input aria-label="Search jobs" placeholder="Search ID, job type, or error code…" value={input} maxLength={64} onChange={(e) => setInput(e.target.value)} className="h-9 pl-9"/></div>
        <label className="sr-only" htmlFor="job-state-filter">Filter by state</label><select id="job-state-filter" value={state} onChange={(e) => { setState(e.target.value); setCursor(""); setHistory([]); }} className="h-9 rounded-lg border border-input bg-background px-3 text-xs focus-ring"><option value="">All states</option>{JOB_STATES.map((item) => <option key={item} value={item}>{item[0].toUpperCase() + item.slice(1)}</option>)}</select>
        <label className="sr-only" htmlFor="job-type-filter">Filter by job type</label><select id="job-type-filter" value={type} onChange={(e) => { setType(e.target.value); setCursor(""); setHistory([]); }} className="h-9 rounded-lg border border-input bg-background px-3 text-xs focus-ring"><option value="">All job types</option>{JOB_TYPES.map((item) => <option key={item} value={item}>{item.replaceAll("_", " ")}</option>)}</select>
        {hasFilter && <Button variant="ghost" size="sm" onClick={reset} aria-label="Clear filters"><FilterX size={14}/> Clear</Button>}
        <span className="hidden items-center gap-1.5 text-[10px] text-muted-foreground xl:flex"><SlidersHorizontal size={12}/> Live filters</span>
      </div>
      <LoadState loading={page.loading} error={page.error} hasData={page.data !== null} onRetry={page.refresh}/>
      {page.data && <><JobTable jobs={page.data.items}/><div className="mt-3 flex items-center justify-between border-t border-border pt-3"><span className="text-[11px] text-muted-foreground">{page.data.items.length} jobs on this page {search && `matching “${search}”`}</span><div className="flex items-center gap-2"><Button variant="outline" size="sm" disabled={!history.length || page.loading} onClick={previous}><ArrowLeft size={13}/>Previous</Button><Button variant="outline" size="sm" disabled={!page.data.next_cursor || page.loading} onClick={next}>Next<ArrowRight size={13}/></Button></div></div></>}
    </Panel>
  </>;
}
