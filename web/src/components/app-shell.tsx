"use client";

import { Suspense, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTheme } from "next-themes";
import { Activity, CalendarClock, CircleHelp, Gauge, HardDrive, KeyRound, ListTodo, Menu, Moon, Plus, Radio, Settings2, ShieldCheck, Sun, UsersRound, Workflow, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/components/auth-provider";
import { AuthProvider } from "@/components/auth-provider";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { ApiError, apiRequest } from "@/lib/chronos";

const groups = [
  { label: "Operate", items: [
    { href: "/", label: "Overview", icon: Gauge },
    { href: "/jobs", label: "Jobs", icon: ListTodo },
    { href: "/jobs/new", label: "Create job", icon: Plus },
    { href: "/schedules", label: "Schedules", icon: CalendarClock },
    { href: "/workers", label: "Workers", icon: UsersRound },
    { href: "/dead-letter", label: "Dead letter", icon: HardDrive },
  ] },
  { label: "Understand", items: [
    { href: "/observability", label: "Observability", icon: Activity },
  ] },
  { label: "Configure", items: [
    { href: "/settings", label: "Settings", icon: Settings2 },
  ] },
];

const titles: Record<string, string> = {
  "/": "Overview", "/jobs": "Jobs", "/jobs/new": "Create job", "/schedules": "Schedules",
  "/workers": "Workers", "/dead-letter": "Dead letter queue", "/observability": "Observability", "/settings": "Settings",
};

function ConnectScreen() {
  const { connect } = useAuth();
  const [candidate, setCandidate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { await apiRequest(candidate, "v1/queue/stats"); connect(candidate); }
    catch (value) { setError(value instanceof ApiError && value.status === 401 ? "That token was not accepted by Chronos." : value instanceof Error ? value.message : "Could not connect to the control plane."); }
    finally { setBusy(false); }
  }
  return <main id="main-content" className="relative flex min-h-screen items-center justify-center overflow-hidden px-5 py-10">
    <div className="surface-grid pointer-events-none absolute inset-x-0 top-0 h-[68vh] opacity-50" />
    <div className="relative w-full max-w-[980px] overflow-hidden rounded-3xl border border-border bg-card shadow-[0_28px_100px_rgba(0,0,0,.16)] lg:grid lg:grid-cols-[1.08fr_.92fr]">
      <section className="relative hidden min-h-[570px] flex-col justify-between overflow-hidden bg-[#17242a] p-10 text-white lg:flex">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_74%_24%,rgba(184,232,107,.15),transparent_31%),radial-gradient(circle_at_18%_90%,rgba(93,147,174,.18),transparent_42%)]" />
        <div className="relative flex items-center gap-3"><div className="grid size-10 place-items-center rounded-xl bg-[#b8e86b] text-[#21301a]"><Workflow size={21} /></div><div><div className="font-semibold tracking-wide">CHRONOS</div><div className="text-[10px] tracking-[.25em] text-white/50">CONTROL PLANE</div></div></div>
        <div className="relative max-w-sm"><div className="mb-5 flex items-center gap-2 text-xs font-medium uppercase tracking-[.2em] text-[#c3e593]"><span className="size-1.5 rounded-full bg-[#b8e86b]" /> Distributed work, in focus</div><h1 className="text-[42px] font-semibold leading-[1.08] tracking-[-.045em]">Know what your queue is doing.</h1><p className="mt-5 max-w-xs text-sm leading-6 text-white/60">A clear operational view across schedules, workers, retries, and every job in flight.</p></div>
        <div className="relative flex items-center justify-between border-t border-white/10 pt-5 text-xs text-white/45"><span>Independent workers · PostgreSQL leases</span><span className="mono">v1.0</span></div>
      </section>
      <section className="flex min-h-[560px] flex-col justify-center px-7 py-10 sm:px-12">
        <div className="mb-8 flex items-center gap-3 lg:hidden"><div className="grid size-10 place-items-center rounded-xl bg-primary text-primary-foreground"><Workflow size={21} /></div><span className="font-semibold tracking-wide">CHRONOS</span></div>
        <div className="mb-8"><p className="mb-2 text-xs font-semibold uppercase tracking-[.16em] text-muted-foreground">Secure connection</p><h2 className="text-2xl font-semibold tracking-tight">Connect to your control plane</h2><p className="mt-2 text-sm leading-6 text-muted-foreground">Enter a bearer token with read, submit, or operator access. It stays in this browser tab and is cleared when the tab closes.</p></div>
        <form onSubmit={submit} className="space-y-4">
          <label htmlFor="api-token" className="block text-sm font-medium">API bearer token</label>
          <Input id="api-token" autoComplete="off" type="password" value={candidate} onChange={(e) => setCandidate(e.target.value)} placeholder="Paste token" minLength={32} maxLength={256} required className="h-11 mono" />
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          <Button type="submit" size="lg" disabled={busy || candidate.length < 32} className="mt-2 h-11 w-full font-semibold">{busy ? "Checking access…" : "Connect securely"}<KeyRound size={15} /></Button>
        </form>
        <div className="mt-7 flex items-start gap-3 rounded-xl border border-border bg-muted/50 p-4"><ShieldCheck className="mt-0.5 shrink-0 text-primary" size={17} /><p className="text-xs leading-5 text-muted-foreground">Authentication is verified against the live API before any credentials are stored. Requests pass through a same-origin, endpoint-allowlisted server route.</p></div>
        <p className="mt-5 text-center text-xs text-muted-foreground">Need API access details? Open the <a href="/docs" target="_blank" className="underline underline-offset-4 hover:text-foreground">control-plane API reference</a>.</p>
      </section>
    </div>
  </main>;
}

function Navigation({ close }: { close?: () => void }) {
  const pathname = usePathname();
  return <nav aria-label="Primary navigation" className="space-y-7 px-3">
    {groups.map((group) => <div key={group.label}>
      <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-[.18em] text-muted-foreground/80">{group.label}</p>
      <div className="space-y-1">{group.items.map(({ href, label, icon: Icon }) => {
        const active = href === "/" ? pathname === href : pathname === href || pathname.startsWith(`${href}/`) || (href === "/jobs" && pathname.startsWith("/jobs/"));
        return <Link key={href} href={href} onClick={close} aria-current={active ? "page" : undefined} className={`group flex h-10 items-center gap-3 rounded-xl px-3 text-[13px] font-medium transition-colors focus-ring ${active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
          <Icon size={16} strokeWidth={active ? 2.1 : 1.7} className={active ? "text-primary" : "text-muted-foreground group-hover:text-foreground"} />{label}
          {label === "Dead letter" && <span className="ml-auto rounded-md bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">DLQ</span>}
        </Link>;
      })}</div>
    </div>)}
  </nav>;
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const dark = theme === "dark";
  return <Button variant="ghost" size="icon" aria-label={`Switch to ${dark ? "light" : "dark"} theme`} onClick={() => setTheme(dark ? "light" : "dark")} className="size-9 rounded-xl text-muted-foreground">
    {dark ? <Sun size={16} /> : <Moon size={16} />}
  </Button>;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const { token, ready, disconnect } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);
  const pathname = usePathname();
  const title = useMemo(() => titles[pathname] ?? (pathname.startsWith("/jobs/") ? "Job details" : "Chronos"), [pathname]);
  if (!ready) return <div className="grid min-h-screen place-items-center text-sm text-muted-foreground" role="status">Preparing secure session…</div>;
  if (!token) return <ConnectScreen />;
  return <div className="min-h-screen">
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-[248px] flex-col border-r border-sidebar-border bg-sidebar lg:flex">
      <Link href="/" className="flex h-[76px] items-center gap-3 px-6 focus-ring">
        <div className="grid size-9 place-items-center rounded-xl bg-primary text-primary-foreground"><Workflow size={19} /></div>
        <div><div className="text-sm font-bold tracking-[.08em]">CHRONOS</div><div className="mt-0.5 text-[9px] font-semibold tracking-[.23em] text-muted-foreground">CONTROL PLANE</div></div>
      </Link>
      <div className="mx-4 mb-5 flex items-center gap-2 rounded-xl border border-sidebar-border bg-background/60 px-3 py-2.5 text-xs text-muted-foreground"><div className="size-1.5 rounded-full bg-primary shadow-[0_0_8px_var(--primary)]"/><span>API origin configured</span><span className="mono ml-auto text-[10px]">v1</span></div>
      <Navigation />
      <div className="mt-auto px-4 pb-4 pt-6"><div className="rounded-2xl border border-sidebar-border bg-background/70 p-3.5"><div className="mb-2 flex items-center gap-2"><Radio size={14} className="text-primary"/><span className="text-xs font-semibold">API session</span><span className="ml-auto rounded-full bg-accent px-2 py-0.5 text-[9px] font-semibold text-accent-foreground">AUTHENTICATED</span></div><p className="text-[11px] leading-4 text-muted-foreground">Jobs persist in PostgreSQL; workers execute separately.</p></div><div className="mt-4 flex items-center gap-2 px-1 text-[10px] text-muted-foreground"><span>Chronos</span><span className="text-border">/</span><span>Operations</span><span className="mono ml-auto">v1</span></div></div>
    </aside>

    <Dialog open={mobileOpen} onOpenChange={setMobileOpen}><DialogContent showCloseButton={false} className="inset-y-0 left-0 top-0 h-dvh max-h-none w-[min(82vw,300px)] max-w-none translate-x-0 translate-y-0 grid-rows-[auto_1fr] content-start gap-4 overflow-y-auto rounded-none border-y-0 border-l-0 border-r border-sidebar-border bg-sidebar p-0 text-sidebar-foreground sm:max-w-none lg:hidden">
      <DialogTitle className="sr-only">Primary navigation</DialogTitle>
      <div className="mb-3 flex items-center justify-between px-5 pt-4"><Link onClick={() => setMobileOpen(false)} href="/" className="flex items-center gap-3"><span className="grid size-9 place-items-center rounded-xl bg-primary text-primary-foreground"><Workflow size={19}/></span><span className="text-sm font-bold tracking-[.08em]">CHRONOS</span></Link><Button variant="ghost" size="icon" aria-label="Close navigation" onClick={() => setMobileOpen(false)}><X size={18}/></Button></div>
      <Navigation close={() => setMobileOpen(false)} />
    </DialogContent></Dialog>

    <div className="lg:pl-[248px]">
      <header className="sticky top-0 z-20 flex h-[68px] items-center justify-between border-b border-border/80 bg-background/90 px-4 backdrop-blur-xl sm:px-7 lg:px-9">
        <div className="flex items-center gap-3"><Button variant="ghost" size="icon" aria-label="Open navigation" onClick={() => setMobileOpen(true)} className="lg:hidden"><Menu size={19}/></Button><div><div className="text-[10px] font-semibold uppercase tracking-[.15em] text-muted-foreground">Chronos <span className="px-1 text-border">/</span> Operations</div><div className="mt-0.5 text-sm font-semibold">{title}</div></div></div>
        <div className="flex items-center gap-1 sm:gap-2"><div className="hidden items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-1.5 text-[10px] text-muted-foreground sm:flex"><Radio size={12}/> API session</div><ThemeToggle/><span className="mx-1 h-6 w-px bg-border"/><Button variant="ghost" size="icon" aria-label="Open control-plane API documentation" onClick={() => window.open("/docs", "_blank", "noopener,noreferrer")}><CircleHelp size={16}/></Button><Button variant="ghost" size="icon" aria-label="Disconnect API token" onClick={disconnect} title="Disconnect"><KeyRound size={15}/></Button><div className="ml-1 grid size-8 place-items-center rounded-full border border-border bg-muted text-[10px] text-muted-foreground"><KeyRound size={13} aria-hidden="true"/></div></div>
      </header>
      <main id="main-content" className="mx-auto w-full max-w-[1600px] px-4 pb-12 pt-7 sm:px-7 lg:px-9 lg:pt-9">{children}</main>
    </div>
  </div>;
}

export function AuthenticatedLayout({ children }: { children: React.ReactNode }) {
  return <AuthProvider><a href="#main-content" className="focus-ring sr-only fixed left-4 top-4 z-[60] rounded-lg bg-card px-3 py-2 text-sm shadow focus:not-sr-only">Skip to main content</a><Suspense fallback={<div className="grid min-h-screen place-items-center text-sm text-muted-foreground" role="status">Loading Chronos…</div>}><AppShell>{children}</AppShell></Suspense></AuthProvider>;
}
