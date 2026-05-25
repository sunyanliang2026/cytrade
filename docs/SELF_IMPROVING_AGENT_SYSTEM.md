# Self-improving agent system

This repository now has a first, safe self-improving loop around the `MainSealFollow` dry-run workflow.

The design follows five layers:

1. **Sensor layer**: parse logs, pytest output, and diffs.
2. **Policy / decision layer**: decide what can be automated and what requires human review.
3. **Tool layer**: prepare constrained Codex CLI prompts and run validation commands.
4. **Quality gate**: scan diffs for safety regressions and run focused checks.
5. **Learning mechanism**: write reports, tasks, and durable lessons back into `agent/memory/`.

## What this loop can do

After the morning dry-run session, run:

```bash
python -m agent.loops.post_morning_review --run-id 2026-05-25 --print-paths
```

The command creates a report under `agent/memory/runs/`, a JSON summary, and `agent/memory/improvement_tasks.yaml`.

Morning review should distinguish between:

- a valid no-trade session, where market data was active but no trigger chain completed;
- an invalid monitor session, where strategies were created but market data never became usable.

The generated markdown and JSON now carry a top-level `review_verdict` so invalid sessions are surfaced before anyone treats them as strategy-quality evidence.

The current invalid-session signature is:

- `strategies > 0`
- all heartbeats keep `connected=False`
- `tick_subscriptions=0`
- `latest_data_time` stays empty
- no `MSF_EVENT` trigger chain appears

To prepare a task prompt for Codex CLI:

```bash
python -m agent.tools.codex_cli_runner --task-id add-top-blocked-reason-summary
```

By default this only writes a prompt file under `agent/memory/codex_prompts/`. It does not execute Codex. To execute, pass `--execute` and configure `--codex-command` for the local Codex CLI installation.

Before reviewing a Codex patch:

```bash
python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py
```

## What this loop must not do

This loop must not automatically enable live trading, change credentials, change QMT paths, change account settings, change strategy thresholds, change position sizing, or route real orders.

The first target is system improvement around observability and replay, not autonomous trading.

## Generated artifacts

- `agent/memory/runs/YYYY-MM-DD_morning.md`: human report.
- `agent/memory/runs/YYYY-MM-DD_morning_summary.json`: machine-readable summary.
- `agent/memory/improvement_tasks.yaml`: next low-risk tasks.
- `agent/memory/codex_prompts/*.md`: prompts prepared for Codex CLI.
- `agent/memory/known_failures.yaml`: durable failure patterns after human review.
