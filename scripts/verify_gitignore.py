#!/usr/bin/env python3
"""Fail when required local secrets/build outputs lack explicit ignore rules."""

from pathlib import Path


REQUIRED = {
    ".env", ".env.*", "!.env.example", ".envrc", "secrets/",
    ".tools/", ".gocache/", ".gopath/", "bin/", "*.test", "coverage.out",
    ".venv/", "__pycache__/", "*.py[cod]", ".pytest_cache/",
    "web/node_modules/", "web/.next/", "worker_runtime/.venv/",
}


def main() -> int:
    path = Path(__file__).resolve().parents[1] / ".gitignore"
    entries = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.lstrip().startswith("#")}
    missing = sorted(REQUIRED - entries)
    if missing:
        print("missing required .gitignore entries: " + ", ".join(missing))
        return 1
    print(f"verified {len(REQUIRED)} required .gitignore entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
