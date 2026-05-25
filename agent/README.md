# Self-improving agent loop

This directory contains the first safe layer of the cytrade self-improving agent system. It does **not** trade. It reads MainSealFollow dry-run logs, writes a morning review, proposes small improvement tasks, and gives Codex CLI a constrained task prompt.

## Quick start

After a morning dry-run session:

```bash
python -m agent.loops.post_morning_review --run-id 2026-05-25 --print-paths
```

This writes:

- `agent/memory/runs/2026-05-25_morning.md`
- `agent/memory/runs/2026-05-25_morning_summary.json`
- `agent/memory/improvement_tasks.yaml`

Prepare a Codex prompt for one task:

```bash
python -m agent.tools.codex_cli_runner --task-id add-top-blocked-reason-summary
```

Run safety checks before any human review:

```bash
python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py
```

## Safety model

The first loop is limited to dry-run observability, log replay, tests, and documentation. It should not touch account credentials, QMT local runtime configuration, real order routing, strategy thresholds, or position sizing.
