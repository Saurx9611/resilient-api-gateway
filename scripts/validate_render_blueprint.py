"""Validate the Chronos Render Blueprint against Render's published schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import urlopen

import yaml
from jsonschema import Draft202012Validator

SCHEMA_URL = "https://render.com/schema/render.yaml.json"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/validate_render_blueprint.py render.chronos.yaml", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        content = path.read_text(encoding="utf-8")
        blueprint = yaml.safe_load(content)
        with urlopen(SCHEMA_URL, timeout=10) as response:
            schema = json.load(response)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Could not read Blueprint or Render schema ({type(exc).__name__})", file=sys.stderr)
        return 2
    except Exception as exc:
        # urllib and JSON decoder failures have provider-specific subclasses;
        # do not echo response bodies or file contents in CI logs.
        print(f"Could not retrieve Render's Blueprint schema ({type(exc).__name__})", file=sys.stderr)
        return 2

    errors = sorted(
        Draft202012Validator(schema).iter_errors(blueprint),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            print(f"{path}:{location}: {error.message}", file=sys.stderr)
        return 1
    print(f"{path}: valid against Render's published Blueprint schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
