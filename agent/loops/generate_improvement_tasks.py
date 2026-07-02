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
    risk_cn = {"low": "低", "medium": "中", "high": "高"}.get(risk, risk)
    type_cn = {
        "safety": "安全",
        "observability": "可观测性",
        "diagnostics": "诊断",
        "replay": "回放",
        "learning": "复盘学习",
    }.get(task_type, task_type)
    return {
        "id": task_id,
        "title": title,
        "标题": title,
        "risk": risk,
        "风险": risk_cn,
        "type": task_type,
        "类型": type_cn,
        "reason": reason,
        "原因": reason,
        "allowed_files": allowed_files,
        "允许修改文件": allowed_files,
        "validation": validation,
        "验收命令": validation,
        "human_required": human_required,
        "需要人工确认": human_required,
    }


def generate_tasks(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate a small, reviewable task list from parsed monitor results."""

    checks = summary.get("checks", {}) or {}
    tasks: list[dict[str, Any]] = []

    if summary.get("real_order_suspected"):
        tasks.append(
            _task(
                "investigate-possible-real-order-line",
                title="排查疑似非模拟订单或成交日志",
                risk="high",
                task_type="safety",
                reason="日志解析发现没有 [MOCK] 标记的 [ORDER] 或 [TRADE] 行，必须先确认是否存在真实下单风险。",
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
                title="完善股票池生成诊断",
                risk="low",
                task_type="observability",
                reason="上午运行没有出现 total > 0 的 MONITOR_SESSION pool_generated 事件，说明股票池生成或复用统计不清楚。",
                allowed_files=[
                    "scripts/pool/collect_main_seal_pool.py",
                    "strategies/main_seal_follow/scripts/run_monitor_session.py",
                    "tests/test_collect_main_seal_pool.py",
                    "strategies/main_seal_follow/tests/test_run_main_seal_follow_monitor_session.py",
                    "docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md",
                ],
                validation=[
                    "python -m pytest tests/test_collect_main_seal_pool.py strategies/main_seal_follow/tests/test_run_main_seal_follow_monitor_session.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if not checks.get("monitor_started"):
        tasks.append(
            _task(
                "improve-monitor-start-diagnostics",
                title="完善监测会话启动诊断",
                risk="low",
                task_type="observability",
                reason="上午运行没有出现 monitor_start 或 session_start 标记，无法确认监测会话是否真正启动。",
                allowed_files=[
                    "strategies/main_seal_follow/scripts/run_monitor_session.py",
                    "strategies/main_seal_follow/scripts/run_market_only.py",
                    "strategies/main_seal_follow/tests/test_run_main_seal_follow_monitor_session.py",
                ],
                validation=[
                    "python -m pytest strategies/main_seal_follow/tests/test_run_main_seal_follow_monitor_session.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if not checks.get("heartbeat_seen"):
        tasks.append(
            _task(
                "improve-runtime-heartbeat-visibility",
                title="完善运行心跳可见性",
                risk="low",
                task_type="observability",
                reason="没有发现 Runtime heartbeat 日志，无法区分市场安静和程序卡死。",
                allowed_files=["main.py", "strategies/main_seal_follow/scripts/run_market_only.py", "tests/"],
                validation=[
                    "python -m py_compile main.py strategies/main_seal_follow/scripts/run_market_only.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if checks.get("heartbeat_seen") and not checks.get("strategies_after_activation"):
        tasks.append(
            _task(
                "explain-zero-strategy-heartbeats",
                title="解释激活后策略数为零的问题",
                risk="low",
                task_type="diagnostics",
                reason="心跳存在，但最大策略数量一直是 0，需要说明股票池、选股或策略初始化哪个环节失败。",
                allowed_files=[
                    "strategy/runner.py",
                    "strategies/main_seal_follow/scripts/run_market_only.py",
                    "docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md",
                    "tests/",
                ],
                validation=[
                    "python -m pytest strategies/main_seal_follow/tests/test_run_main_seal_follow_monitor_session.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if summary.get("invalid_monitor_reason") == "market_data_not_connected":
        tasks.append(
            _task(
                "capture-account-login-diagnostics",
                title="记录账户和行情登录诊断",
                risk="low",
                task_type="diagnostics",
                reason="会话已被标记为无效；在改策略前，先暴露本地账户或行情登录状态是否缺失。",
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
                title="汇总 Level2 订阅状态",
                risk="low",
                task_type="observability",
                reason="策略已经激活，但复盘没有观察到 Level2 明细订阅，需要在报告中明确订阅状态。",
                allowed_files=[
                    "core/data_subscription.py",
                    "strategies/main_seal_follow/strategy.py",
                    "strategies/main_seal_follow/tests/test_main_seal_follow_strategy.py",
                    "docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md",
                ],
                validation=[
                    "python -m pytest strategies/main_seal_follow/tests/test_main_seal_follow_strategy.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if checks.get("strategies_after_activation") and not checks.get("entry_signal_seen"):
        tasks.append(
            _task(
                "add-top-blocked-reason-summary",
                title="增加排板阻断原因汇总",
                risk="low",
                task_type="observability",
                reason="策略已经激活，但没有 entry_signal_accepted 事件；需要汇总阻断原因，让下一次复盘能直接定位问题。",
                allowed_files=[
                    "strategies/main_seal_follow/strategy.py",
                    "agent/sensors/parse_monitor_logs.py",
                    "strategies/main_seal_follow/tests/test_main_seal_follow_strategy.py",
                    "tests/test_agent_monitor_review.py",
                ],
                validation=[
                    "python -m pytest strategies/main_seal_follow/tests/test_main_seal_follow_strategy.py tests/test_agent_monitor_review.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if checks.get("entry_signal_seen") and not checks.get("dry_run_probe_trade_seen"):
        tasks.append(
            _task(
                "improve-dry-run-probe-fill-replay",
                title="完善 dry-run 观察单成交回放诊断",
                risk="low",
                task_type="replay",
                reason="出现 entry_signal_accepted，但没有 dry_run_probe_trade_recorded 或模拟成交日志，需要补充回放诊断。",
                allowed_files=[
                    "strategies/main_seal_follow/strategy.py",
                    "strategies/main_seal_follow/tests/test_main_seal_follow_strategy.py",
                    "agent/sensors/parse_monitor_logs.py",
                ],
                validation=[
                    "python -m pytest strategies/main_seal_follow/tests/test_main_seal_follow_strategy.py tests/test_agent_monitor_review.py",
                    "python -m agent.gates.quality_gate",
                ],
            )
        )

    if not tasks:
        tasks.append(
            _task(
                "tighten-morning-review-report",
                title="完善上午复盘报告和经验记录",
                risk="low",
                task_type="learning",
                reason="最低验收已通过；需要记录本次学到的内容，并优化下一次复盘检查清单。",
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
        "# 由 agent.loops.generate_improvement_tasks 自动生成",
        f"generated_at: {_yaml_scalar(datetime.now().isoformat(timespec='seconds'))}",
        f"generated_from: {_yaml_scalar(generated_from)}",
        "tasks:",
    ]
    for task in tasks:
        lines.append(f"  - id: {_yaml_scalar(task['id'])}")
        for key in ("标题", "风险", "类型", "原因"):
            lines.append(f"    {key}: {_yaml_scalar(task[key])}")
        lines.append(f"    需要人工确认: {_yaml_scalar(task.get('需要人工确认', False))}")
        lines.append("    允许修改文件:")
        for item in task.get("允许修改文件", []):
            lines.append(f"      - {_yaml_scalar(item)}")
        lines.append("    验收命令:")
        for item in task.get("验收命令", []):
            lines.append(f"      - {_yaml_scalar(item)}")
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
