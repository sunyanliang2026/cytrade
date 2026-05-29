"""Generate low-risk improvement tasks from a morning-review summary."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _task(
    task_id: str,
    *,
    title: str,
    risk: str,
    task_type: str,
    reason: str,
    allowed_files: list[str],
    validation: list[str],
    human_required: bool = False,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "risk": risk,
        "type": task_type,
        "reason": reason,
        "allowed_files": allowed_files,
        "validation": validation,
        "human_required": human_required,
    }


def generate_tasks(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate a small, reviewable task list from parsed monitor results."""

    checks = summary.get("checks", {}) or {}
    tasks: list[dict[str, Any]] = []

    if summary.get("real_order_suspected"):
        tasks.append(
            _task(
                "investigate-possible-real-order-line",
                title="Investigate possible non-mock order/trade lines",
                risk="high",
                task_type="safety",
                reason="The log parser found [ORDER] or [TRADE] lines without the [MOCK] marker.",
                allowed_files=[
                    "logs/",
                    "agent/sensors/parse_monitor_logs.py",
                    "agent/memory/runs/",
                ],
                validation=["python -m agent.gates.quality_gate"],
                human_required=True,
            )
        )
        return tasks

    if not checks.get("pool_generated"):
        tasks.append(
            _task(
                "improve-pool-generation-diagnostics",
                title="Improve stock-pool generation diagnostics",
                risk="low",
                task_type="observability",
                reason="The morning run did not show a successful MONITOR_SESSION pool_generated event with total > 0.",
                allowed_files=[
                    "scripts/collect_main_seal_pool.py",
                    "scripts/run_main_seal_follow_monitor_session.py",
                    "tests/test_collect_main_seal_pool.py",
                    "tests/test_run_main_seal_follow_monitor_session.py",
                    "docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md",
                ],
                validation=[
                    "python -m pytest tests/test_collect_main_seal_pool.py tests/test_run_main_seal_follow_monitor_session.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if not checks.get("monitor_started"):
        tasks.append(
            _task(
                "improve-monitor-start-diagnostics",
                title="Improve monitor session start diagnostics",
                risk="low",
                task_type="observability",
                reason="The morning run did not show monitor_start or session_start markers.",
                allowed_files=[
                    "scripts/run_main_seal_follow_monitor_session.py",
                    "scripts/run_main_seal_follow_market_only.py",
                    "tests/test_run_main_seal_follow_monitor_session.py",
                ],
                validation=[
                    "python -m pytest tests/test_run_main_seal_follow_monitor_session.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if not checks.get("heartbeat_seen"):
        tasks.append(
            _task(
                "improve-runtime-heartbeat-visibility",
                title="Improve runtime heartbeat visibility",
                risk="low",
                task_type="observability",
                reason="No Runtime heartbeat lines were found, so quiet markets cannot be distinguished from hangs.",
                allowed_files=["main.py", "scripts/run_main_seal_follow_market_only.py", "tests/"],
                validation=[
                    "python -m py_compile main.py scripts/run_main_seal_follow_market_only.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if checks.get("heartbeat_seen") and not checks.get("strategies_after_activation"):
        tasks.append(
            _task(
                "explain-zero-strategy-heartbeats",
                title="Explain zero-strategy heartbeats after activation",
                risk="low",
                task_type="diagnostics",
                reason="Heartbeats were present, but max strategy count stayed at 0.",
                allowed_files=[
                    "strategy/runner.py",
                    "scripts/run_main_seal_follow_market_only.py",
                    "docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md",
                    "tests/",
                ],
                validation=[
                    "python -m pytest tests/test_run_main_seal_follow_monitor_session.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if summary.get("invalid_monitor_reason") == "market_data_not_connected":
        tasks.append(
            _task(
                "capture-account-login-diagnostics",
                title="Capture account and market-login diagnostics for invalid sessions",
                risk="low",
                task_type="diagnostics",
                reason="The session is already flagged as invalid; the next low-risk step is to expose whether local account / market-data login state was missing before touching any strategy logic.",
                allowed_files=[
                    "agent/sensors/parse_monitor_logs.py",
                    "agent/loops/post_morning_review.py",
                    "docs/SELF_IMPROVING_AGENT_SYSTEM.md",
                    "tests/test_agent_monitor_review.py",
                ],
                validation=[
                    "python -m pytest tests/test_agent_monitor_review.py -q",
                    "python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py",
                ],
            )
        )
        return tasks[:3]

    if checks.get("strategies_after_activation") and not checks.get("l2_detail_seen"):
        tasks.append(
            _task(
                "summarize-l2-subscription-state",
                title="Summarize Level2 subscription state in review logs",
                risk="low",
                task_type="observability",
                reason="Strategies were active, but no Level2 detail subscriptions were observed.",
                allowed_files=[
                    "core/data_subscription.py",
                    "strategy/main_seal_follow_strategy.py",
                    "tests/test_main_seal_follow_strategy.py",
                    "docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md",
                ],
                validation=[
                    "python -m pytest tests/test_main_seal_follow_strategy.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if checks.get("strategies_after_activation") and not checks.get("entry_signal_seen"):
        tasks.append(
            _task(
                "add-top-blocked-reason-summary",
                title="Add top blocked-reason summary for MainSealFollow",
                risk="low",
                task_type="observability",
                reason="Strategies were active, but no entry_signal_accepted event was observed; summarize blocked reasons to make the next review actionable.",
                allowed_files=[
                    "strategy/main_seal_follow_strategy.py",
                    "agent/sensors/parse_monitor_logs.py",
                    "tests/test_main_seal_follow_strategy.py",
                    "tests/test_agent_monitor_review.py",
                ],
                validation=[
                    "python -m pytest tests/test_main_seal_follow_strategy.py tests/test_agent_monitor_review.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if checks.get("entry_signal_seen") and not checks.get("dry_run_probe_trade_seen"):
        tasks.append(
            _task(
                "improve-dry-run-probe-fill-replay",
                title="Improve dry-run probe fill replay diagnostics",
                risk="low",
                task_type="replay",
                reason="entry_signal_accepted appeared, but dry_run_probe_trade_recorded or mock-trade lines did not.",
                allowed_files=[
                    "strategy/main_seal_follow_strategy.py",
                    "tests/test_main_seal_follow_strategy.py",
                    "agent/sensors/parse_monitor_logs.py",
                ],
                validation=[
                    "python -m pytest tests/test_main_seal_follow_strategy.py tests/test_agent_monitor_review.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if not tasks:
        tasks.append(
            _task(
                "tighten-morning-review-report",
                title="Tighten morning review report and lessons",
                risk="low",
                task_type="learning",
                reason="Minimum acceptance passed; capture what was learned and improve the next review checklist.",
                allowed_files=[
                    "agent/memory/project_brain.md",
                    "agent/memory/known_failures.yaml",
                    "docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md",
                    "docs/SELF_IMPROVING_AGENT_SYSTEM.md",
                ],
                validation=["python -m agent.gates.quality_gate"],
            )
        )

    return tasks[:3]


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def dump_tasks_yaml(tasks: list[dict[str, Any]], *, generated_from: str = "morning_review") -> str:
    """Write enough YAML for humans and Codex prompts without requiring PyYAML."""

    lines = [
        "# Generated by agent.loops.generate_improvement_tasks",
        f"generated_at: {_yaml_scalar(datetime.now().isoformat(timespec='seconds'))}",
        f"generated_from: {_yaml_scalar(generated_from)}",
        "tasks:",
    ]
    for task in tasks:
        lines.append(f"  - id: {_yaml_scalar(task['id'])}")
        for key in ("title", "risk", "type", "reason"):
            lines.append(f"    {key}: {_yaml_scalar(task[key])}")
        lines.append(f"    human_required: {_yaml_scalar(task.get('human_required', False))}")
        lines.append("    allowed_files:")
        for item in task.get("allowed_files", []):
            lines.append(f"      - {_yaml_scalar(item)}")
        lines.append("    validation:")
        for item in task.get("validation", []):
            lines.append(f"      - {_yaml_scalar(item)}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate improvement tasks from a morning review summary JSON.")
    parser.add_argument("--summary-json", required=True, help="Path to summary JSON from parse_monitor_logs.")
    parser.add_argument("--output", default="agent/memory/improvement_tasks.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = json.loads(Path(args.summary_json).read_text(encoding="utf-8"))
    tasks = generate_tasks(summary)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dump_tasks_yaml(tasks, generated_from=args.summary_json), encoding="utf-8")
    print(f"wrote {len(tasks)} task(s) to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
