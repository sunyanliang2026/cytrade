"""Quality and safety gate for agent-generated changes.

The gate intentionally focuses on the current self-improving loop: dry-run
observability, tests, and reviewability. It does not prove trading correctness;
it blocks obvious safety regressions and records validation output.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_PY_COMPILE_TARGETS = [
    "agent/sensors/parse_monitor_logs.py",
    "agent/sensors/parse_pytest_output.py",
    "agent/sensors/parse_git_diff.py",
    "agent/loops/post_morning_review.py",
    "agent/loops/generate_improvement_tasks.py",
    "agent/gates/quality_gate.py",
    "agent/tools/codex_cli_runner.py",
]

RISKY_PATTERNS: list[tuple[str, str]] = [
    (r"CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN\s*[:=]\s*(False|false|0)", "dry-run safety flag is being disabled"),
    (r"real_order_sent\s*[:=]\s*(True|true|1)", "real order marker appears to be enabled"),
    (r"ACCOUNT_PASSWORD", "account password field touched"),
    (r"DINGTALK_SECRET", "webhook secret field touched"),
    (r"QMT_PATH\s*[:=]", "local QMT path touched"),
]

RISKY_PATH_PATTERNS: list[tuple[str, str]] = [
    (r"(^|/)\.env(\.|$|/)?", ".env files must not be modified by agents"),
    (r"config/local_runtime\.json", "local runtime secrets/config must not be modified by agents"),
]


@dataclass(slots=True)
class GateResult:
    name: str
    ok: bool
    detail: str


def run_command(command: list[str], *, cwd: Path | None = None, timeout: int = 120) -> GateResult:
    """Run a validation command and return a compact result."""

    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return GateResult(" ".join(command), False, f"command not found: {exc}")
    except subprocess.TimeoutExpired as exc:
        return GateResult(" ".join(command), False, f"timed out after {timeout}s: {exc}")

    output = (proc.stdout or "").strip()
    tail = "\n".join(output.splitlines()[-40:])
    return GateResult(" ".join(command), proc.returncode == 0, tail or f"exit={proc.returncode}")


def get_git_diff(cwd: Path) -> str:
    """Return git diff text, or an empty string outside a git worktree."""

    proc = subprocess.run(
        ["git", "diff", "--", "."],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _changed_paths_from_diff(diff_text: str) -> list[str]:
    paths: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                if path.startswith("b/"):
                    path = path[2:]
                paths.append(path)
    return paths


def _is_policy_or_test_path(path: str) -> bool:
    """Return true for files that may mention risky strings as policy/test text."""

    return (
        path == "AGENTS.md"
        or path.startswith("docs/")
        or path.startswith("tests/")
        or path.startswith("agent/gates/")
        or path.startswith("agent/policies/")
        or path.startswith("agent/prompts/")
        or path == "agent/sensors/parse_git_diff.py"
    )


def _iter_added_lines_by_path(diff_text: str) -> Iterable[tuple[str, str]]:
    current_path = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current_path = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            yield current_path, line[1:]


def scan_diff_for_safety(diff_text: str) -> list[str]:
    """Return safety findings in a unified diff.

    The scanner checks changed file paths globally, but scans risky content only
    on added runtime-code lines. Policy, documentation, tests, and the gate
    implementation itself are allowed to mention risky strings as text.
    """

    findings: list[str] = []
    for path in _changed_paths_from_diff(diff_text):
        for pattern, message in RISKY_PATH_PATTERNS:
            if re.search(pattern, path):
                findings.append(f"{message}: {path}")

    for path, line in _iter_added_lines_by_path(diff_text):
        if _is_policy_or_test_path(path):
            continue
        for pattern, message in RISKY_PATTERNS:
            if re.search(pattern, line):
                findings.append(f"{message}: {path}")
    return sorted(set(findings))


def run_py_compile(targets: Iterable[str], *, cwd: Path) -> GateResult:
    existing = [target for target in targets if (cwd / target).exists()]
    if not existing:
        return GateResult("py_compile", True, "no compile targets found")
    return run_command([sys.executable, "-m", "py_compile", *existing], cwd=cwd)


def run_quality_gate(
    *,
    cwd: Path,
    diff_file: Path | None = None,
    py_compile_targets: list[str] | None = None,
    pytest_targets: list[str] | None = None,
    skip_py_compile: bool = False,
) -> list[GateResult]:
    results: list[GateResult] = []

    diff_text = ""
    if diff_file:
        diff_text = diff_file.read_text(encoding="utf-8", errors="replace")
    else:
        diff_text = get_git_diff(cwd)

    findings = scan_diff_for_safety(diff_text) if diff_text else []
    if findings:
        results.append(GateResult("safety_scan", False, "\n".join(f"- {item}" for item in findings)))
    else:
        detail = "no risky diff patterns found" if diff_text else "no diff text available; safety scan had no content"
        results.append(GateResult("safety_scan", True, detail))

    if not skip_py_compile:
        results.append(run_py_compile(py_compile_targets or DEFAULT_PY_COMPILE_TARGETS, cwd=cwd))

    for target in pytest_targets or []:
        parts = target.split()
        if parts[:3] == ["python", "-m", "pytest"]:
            command = [sys.executable, "-m", "pytest", *parts[3:]]
        elif parts[:2] == ["pytest"]:
            command = [sys.executable, "-m", "pytest", *parts[1:]]
        else:
            command = [sys.executable, "-m", "pytest", target]
        results.append(run_command(command, cwd=cwd, timeout=240))

    return results


def format_results(results: list[GateResult]) -> str:
    lines = ["# Agent quality gate", ""]
    overall = all(result.ok for result in results)
    lines.append(f"Overall: **{'PASS' if overall else 'FAIL'}**")
    lines.append("")
    for result in results:
        lines.append(f"## {'PASS' if result.ok else 'FAIL'} - {result.name}")
        lines.append("")
        lines.append("```text")
        lines.append(result.detail)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run safety and quality checks for agent-generated changes.")
    parser.add_argument("--cwd", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--diff-file", help="Unified diff file to scan. Defaults to git diff when available.")
    parser.add_argument("--skip-py-compile", action="store_true")
    parser.add_argument("--py-compile", action="append", default=[], help="Additional file to include in py_compile.")
    parser.add_argument("--pytest", action="append", default=[], help="Pytest target or command. Can be repeated.")
    parser.add_argument("--output", help="Optional markdown report path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cwd = Path(args.cwd).resolve()
    diff_file = Path(args.diff_file).resolve() if args.diff_file else None
    targets = DEFAULT_PY_COMPILE_TARGETS + list(args.py_compile)
    results = run_quality_gate(
        cwd=cwd,
        diff_file=diff_file,
        py_compile_targets=targets,
        pytest_targets=list(args.pytest),
        skip_py_compile=args.skip_py_compile,
    )
    report = format_results(results)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
    else:
        print(report)
    return 0 if all(result.ok for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
