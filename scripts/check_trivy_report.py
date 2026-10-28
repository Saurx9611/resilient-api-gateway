#!/usr/bin/env python3
"""Block critical findings and fixable high findings; report unfixed high findings."""

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_trivy_report.py REPORT.json", file=sys.stderr)
        return 2
    try:
        report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read Trivy report ({type(exc).__name__})", file=sys.stderr)
        return 2

    blockers: list[str] = []
    unresolved: dict[tuple[str, str], set[str]] = {}
    for result in report.get("Results", []):
        target = result.get("Target", "unknown target")
        for finding in result.get("Vulnerabilities") or []:
            severity = finding.get("Severity")
            name = finding.get("PkgName", "unknown package")
            version = finding.get("InstalledVersion", "unknown version")
            vulnerability_id = finding.get("VulnerabilityID", "unknown advisory")
            fixed = finding.get("FixedVersion")
            record = f"{target}: {name}@{version} {vulnerability_id}"
            if severity == "CRITICAL" or (severity == "HIGH" and fixed):
                blockers.append(f"{severity}: {record} (fixed: {fixed or 'not available'})")
            elif severity == "HIGH":
                unresolved.setdefault((target, name), set()).add(vulnerability_id)

    for (target, name), identifiers in sorted(unresolved.items()):
        print(f"REVIEW: unfixed HIGH finding(s): {target}: {name} {', '.join(sorted(identifiers))}")
    if blockers:
        print("blocking Trivy findings:", file=sys.stderr)
        print("\n".join(sorted(blockers)), file=sys.stderr)
        return 1
    print(f"Trivy gate passed; {len(unresolved)} unfixed HIGH advisory(s) reported for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
