"""Run validation commands and save a small text report."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def run(command: list[str], *, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(command, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return proc.returncode, proc.stdout or ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one or more validation commands.")
    parser.add_argument("commands", nargs="*", help="Commands to run. Use quoted strings, e.g. 'python -m pytest tests/test_agent_monitor_review.py'.")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--output", default="agent/memory/last_validation.txt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = args.commands or [
        "python -m py_compile agent/sensors/parse_monitor_logs.py agent/loops/post_morning_review.py agent/gates/quality_gate.py",
        "python -m pytest tests/test_agent_monitor_review.py",
    ]
    cwd = Path(args.cwd).resolve()
    chunks = [f"Validation run at {datetime.now().isoformat(timespec='seconds')}", ""]
    overall = 0
    for command_text in commands:
        command = shlex.split(command_text)
        if command[:1] == ["python"]:
            command[0] = sys.executable
        code, output = run(command, cwd=cwd)
        overall = overall or code
        chunks.extend([f"$ {command_text}", output.rstrip(), f"exit={code}", ""])
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(chunks), encoding="utf-8")
    print(f"wrote validation report: {output_path}")
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
