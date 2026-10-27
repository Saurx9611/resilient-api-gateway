# ADR-003: Minimal Phase 0 foundation and v1 boundaries

Status: Accepted 2026-10-27.

Problem/evidence: audit found only AGENTS.md, PLAN.md and CODEX_PROMPTS.md,
all untracked, no remote, Go or Docker on PATH, and no code/tests/ADRs.
Options: leave planning only; build all phases; create a dependency-free CLI
scaffold and test/CI foundation. Necessity: MUST establish executable evidence;
DEFER networking and services to their phases.
Decision: standard library only, Go 1.27.3 pinned in go.mod and CI. Official
release metadata at https://go.dev/dl/?mode=json and release history at
https://go.dev/doc/devel/release identify this stable patch on the audit date.
Use gofmt, go vet, unit and race tests, build and CLI smoke. No empty packages,
frameworks, third-party linter or fake health server. Tests only establish the
scaffold contract and output error handling, not resilience properties.
Trade-offs: a placeholder module path must change once the repository URL is
known; Go security updates require deliberate pin updates. GitHub action major
tags are mutable; commit-SHA pinning is future supply-chain hardening. CI Linux
does not establish all-platform support. No Compose without services to run.
Verification: local commands and recorded failures/skips in PLAN.md; CI workflow
review and actual remote run only after a repository remote exists.

V1 decisions: per-instance breaker with consecutive-failure threshold and bounded
half-open probes (MUST, deterministic and understandable, no fleet coordination);
immutable atomic reload (SHOULD, retain last-good snapshot); native logs and
bounded Prometheus dimensions (MUST). These remain future implementations.
Defer retries, custom pools, exact limiter, admin API and frontend until justified.
Reject Kubernetes, discovery, custom sockets and gRPC for v1: they add operational
scope without testing the required HTTP admission and failure-handling behavior.
