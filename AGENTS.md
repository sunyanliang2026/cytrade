# AGENTS.md

## Project context

This repository is a QMT / xtquant-based trading system. The current active workflow is `MainSealFollow` Level2 dry-run monitoring:

1. Generate the stock pool before the open.
2. Run the market-only runtime in dry-run mode.
3. Observe ordinary tick and Level2 subscriptions.
4. Record enough `MONITOR_SESSION`, `Runtime heartbeat`, `MSF_EVENT`, and mock-trade logs for replay.

The current phase is monitoring and validation. It is not a live-trading rollout.

## Hard safety rules

- Never enable real trading automatically.
- Never change `CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN` from true to false.
- Never modify account credentials, QMT local paths, webhook secrets, `.env` files, or `config/local_runtime.json`.
- Never place real orders from an agent-generated change.
- Never bypass a failing safety gate.
- Strategy threshold changes, account changes, order-routing changes, or position-sizing changes require explicit human approval.

## Preferred first changes

The first self-improving loop should improve the system around the strategy, not the live trading behavior itself:

- Improve observability.
- Improve log parsing.
- Improve dry-run replay.
- Improve tests.
- Improve documentation.
- Keep patches small and reviewable.

## Validation commands

Run focused checks first:

```bash
python -m py_compile scripts/run/run_main_seal_follow_monitor_session.py scripts/run/run_main_seal_follow_market_only.py strategy/main_seal_follow_strategy.py
python -m py_compile agent/sensors/parse_monitor_logs.py agent/loops/post_morning_review.py agent/loops/generate_improvement_tasks.py agent/gates/quality_gate.py agent/tools/codex_cli_runner.py
python -m pytest tests/test_agent_monitor_review.py
python -m pytest tests/test_collect_main_seal_pool.py tests/test_import_iwencai_pool.py tests/test_run_main_seal_follow_monitor_session.py tests/test_main_seal_follow_strategy.py
```

## Review expectations

Every patch must explain:

- What runtime symptom it addresses.
- What files changed.
- What tests were run.
- Whether it touches trading execution or only dry-run / observability.
- Whether it changes any safety boundary.

## Agent workflow

The expected loop is:

1. Parse morning logs with `python -m agent.loops.post_morning_review`.
2. Generate `agent/memory/runs/YYYY-MM-DD_morning.md` and `agent/memory/improvement_tasks.yaml`.
3. Pick one low-risk task.
4. Ask Codex CLI to implement only that task.
5. Run `python -m agent.gates.quality_gate`.
6. Human reviews the diff, test output, and safety notes before merge.
7. Update `agent/memory/project_brain.md`, `agent/memory/known_failures.yaml`, and this file when a lesson repeats.
