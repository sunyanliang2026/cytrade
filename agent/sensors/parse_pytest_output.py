"""Parse pytest output into a compact quality-signal summary."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_RESULT_RE = re.compile(r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed|warnings?)")
_DURATION_RE = re.compile(r"in\s+(?P<seconds>\d+(?:\.\d+)?)s")


def parse_pytest_output(text: str) -> dict[str, Any]:
    """Return counts and headline failure lines from pytest output."""

    counts: dict[str, int] = {}
    lines = str(text or "").splitlines()
    for line in reversed(lines[-50:]):
        if " passed" in line or " failed" in line or " error" in line or " warning" in line:
            for match in _RESULT_RE.finditer(line):
                kind = match.group("kind")
                if kind == "error":
                    kind = "errors"
                if kind == "warning":
                    kind = "warnings"
                counts[kind] = int(match.group("count"))
            duration = _DURATION_RE.search(line)
            if duration:
                counts["duration_seconds"] = float(duration.group("seconds"))
            if counts:
                break

    failure_lines = [
        line.strip()
        for line in lines
        if line.startswith("FAILED ") or line.startswith("ERROR ") or "E   " in line[:8]
    ][:20]

    return {
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0),
        "errors": counts.get("errors", 0),
        "warnings": counts.get("warnings", 0),
        "skipped": counts.get("skipped", 0),
        "duration_seconds": counts.get("duration_seconds"),
        "success": counts.get("failed", 0) == 0 and counts.get("errors", 0) == 0 and bool(counts),
        "failure_lines": failure_lines,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse pytest output into JSON.")
    parser.add_argument("path", help="Text file containing pytest output.")
    parser.add_argument("--output", help="Optional JSON output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = parse_pytest_output(Path(args.path).read_text(encoding="utf-8", errors="replace"))
    data = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(data, encoding="utf-8")
    else:
        print(data)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
