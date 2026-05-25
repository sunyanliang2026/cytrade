"""Post-morning review loop for MainSealFollow dry-run sessions.

The script converts raw logs into:

- a human-readable morning report under ``agent/memory/runs/``;
- a machine-readable JSON summary;
- a small list of low-risk improvement tasks for Codex CLI or human review.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from agent.loops.generate_improvement_tasks import dump_tasks_yaml, generate_tasks
from agent.sensors.parse_monitor_logs import ParsedLogEvent, format_markdown, parse_log_files, summarize_events


DEFAULT_LOG_PATTERNS = [
    "logs/system.*.log",
    "logs/trade.*.log",
    "logs/system.log",
    "logs/trade.log",
]


def _normalize_log_path(path_text: str, *, repo_root: Path) -> str:
    text = str(path_text or "").strip()
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        path = repo_root / path
    return str(path.resolve())


def select_run_session_events(events: list[ParsedLogEvent], *, repo_root: Path, run_id: str | None = None) -> list[ParsedLogEvent]:
    """Keep only the latest stopped session's concrete system/trade logs."""

    stopped_events = [
        event
        for event in events
        if event.type == "monitor_session" and event.event == "stopped"
    ]
    if run_id:
        run_day_events = [
            event
            for event in stopped_events
            if str(event.fields.get("_logged_at", "")).startswith(run_id)
        ]
        if run_day_events:
            stopped_events = run_day_events
    if not stopped_events:
        return events

    selected = stopped_events[-1]
    selected_sources = {
        _normalize_log_path(selected.fields.get("system_log", ""), repo_root=repo_root),
        _normalize_log_path(selected.fields.get("trade_log", ""), repo_root=repo_root),
    }
    selected_sources.discard("")
    if not selected_sources:
        return events

    filtered: list[ParsedLogEvent] = []
    for event in events:
        source = _normalize_log_path(event.source, repo_root=repo_root) if event.source else ""
        if not source or source in selected_sources:
            filtered.append(event)
    return filtered


def build_default_run_id(value: str | None = None) -> str:
    if value:
        return value
    return date.today().isoformat()


def run_post_morning_review(
    *,
    logs: list[str] | None = None,
    run_id: str | None = None,
    report_path: str | None = None,
    summary_json_path: str | None = None,
    tasks_path: str | None = None,
) -> dict[str, Path]:
    """Run the review loop and return generated artifact paths."""

    resolved_run_id = build_default_run_id(run_id)
    log_patterns = logs or DEFAULT_LOG_PATTERNS
    report = Path(report_path or f"agent/memory/runs/{resolved_run_id}_morning.md")
    summary_json = Path(summary_json_path or f"agent/memory/runs/{resolved_run_id}_morning_summary.json")
    tasks_file = Path(tasks_path or "agent/memory/improvement_tasks.yaml")

    repo_root = Path.cwd()
    events = parse_log_files(log_patterns)
    events = select_run_session_events(events, repo_root=repo_root, run_id=resolved_run_id)
    summary = summarize_events(events)
    summary["run_id"] = resolved_run_id
    summary["log_patterns"] = list(log_patterns)
    summary["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
    summary["reviewed_sources"] = sorted(
        {
            _normalize_log_path(event.source, repo_root=repo_root)
            for event in events
            if event.source
        }
    )

    markdown = format_markdown(summary, title=f"MainSealFollow morning review - {resolved_run_id}")
    tasks = generate_tasks(summary)

    report.parent.mkdir(parents=True, exist_ok=True)
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.parent.mkdir(parents=True, exist_ok=True)

    report.write_text(markdown, encoding="utf-8")
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tasks_file.write_text(dump_tasks_yaml(tasks, generated_from=str(summary_json)), encoding="utf-8")

    return {
        "report": report,
        "summary_json": summary_json,
        "tasks": tasks_file,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the MainSealFollow post-morning review loop.")
    parser.add_argument("logs", nargs="*", help="Optional log paths or glob patterns. Defaults to logs/system*.log and logs/trade*.log.")
    parser.add_argument("--run-id", help="Run identifier, usually YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--report", help="Output markdown report path.")
    parser.add_argument("--summary-json", help="Output summary JSON path.")
    parser.add_argument("--tasks", help="Output improvement tasks YAML path.")
    parser.add_argument("--print-paths", action="store_true", help="Print generated artifact paths.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = run_post_morning_review(
        logs=args.logs or None,
        run_id=args.run_id,
        report_path=args.report,
        summary_json_path=args.summary_json,
        tasks_path=args.tasks,
    )
    if args.print_paths:
        for name, path in paths.items():
            print(f"{name}: {path}")
    else:
        print(f"wrote morning review to {paths['report']}")
        print(f"wrote improvement tasks to {paths['tasks']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
