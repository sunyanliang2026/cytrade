# Project Status

Date: `2026-05-24`

## Current Goal

Current focus is the `MainSealFollow` Level2 dry-run monitoring workflow:

- Generate the stock pool automatically before the open.
- Monitor candidate stocks with Level2 data during the morning session.
- Do not send real orders.
- Keep console and log output sufficient for intraday replay and review.

This is a monitoring and validation phase, not a live trading phase.

## Current Baseline

Latest relevant commit:

```text
f238d0f Add stock-pool sources and dry-run monitor session
```

Current git working tree:

- Code and docs are committed.
- The only local uncommitted file is `config/main_seal_follow_pool.csv`.
- That CSV is a runtime output generated during rehearsal and is intentionally
  not committed.

## What Is Implemented

### 1. Stock-pool generation

Implemented files:

- `scripts/collect_main_seal_pool.py`
- `scripts/collect_iwencai_pool.py`
- `scripts/collect_jiuyangongshe_pool.py`
- `config/main_seal_pool_sources.json`

Current design:

- Stock-pool sources are split by source.
- The final pool is assembled by a central script.
- Source sets support named result sets plus set expressions.
- `iwencai` and `jiuyangongshe` can be combined by `union` and `intersect`.
- Final common filtering is handled in the main collector.

Current default final pool logic:

- Direct inclusion from a main `iwencai` result set.
- Additional candidates from Jiuyangongshe nodes gated by a stronger `iwencai`
  base set.

### 2. MainSealFollow dry-run monitoring

Implemented files:

- `scripts/run_main_seal_follow_market_only.py`
- `scripts/run_main_seal_follow_monitor_session.py`
- `scripts/start_main_seal_follow_monitor.bat`
- `scripts/register_main_seal_follow_monitor_task.ps1`

Current behavior:

- `08:50` generate stock pool.
- Start `market-only` runtime with `dry_run=true`.
- Do not connect the trading account for actual order placement.
- Stop automatically at `12:00`.
- Emit runtime heartbeat every 30 seconds by default.

Windows scheduled task status:

- Task name: `Cytrade MainSealFollow Monitor`
- Trigger: weekdays at `08:50`
- Action: run `scripts/start_main_seal_follow_monitor.bat`

### 3. Strategy event logging

Implemented file:

- `strategy/main_seal_follow_strategy.py`

Current logging coverage includes:

- `entry_signal_accepted`
- `entry_signal_blocked`
- `entry_plan_created`
- `dry_run_entry_submitted`
- `dry_run_probe_filled`
- `dry_run_probe_trade_recorded`
- `main_keep_decision`
- `main_cancel_decision`
- `dry_run_orders_finalized`
- `[ORDER] [TRADE] [MOCK] observation filled ...`

This means a dry-run observation-order fill is now visible both as structured
`MSF_EVENT` data and as a grep-friendly mock trade line.

### 4. Operational docs

Implemented files:

- `docs/STOCK_POOL_LOGIC.md`
- `docs/NEXT_TRADING_DAY_MONITORING.md`
- `docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md`

Purpose:

- Explain stock-pool construction.
- Explain the dry-run morning monitoring entry point.
- Provide a trading-day observation checklist.

## What Has Been Validated

### Rehearsal

A non-trading-day rehearsal was completed on `2026-05-24`:

- Waited until a configured test time.
- Generated the stock pool successfully.
- Started the dry-run market-only runtime.
- Emitted heartbeats.
- Stopped automatically at the configured stop time.

Observed result:

- Session orchestration is working.
- On a non-trading day, `StrategyRunner` correctly skips trading-day
  activation, so strategy count remained `0`.

### Tests

Validated commands:

```text
python -m py_compile ...
python -m pytest tests\test_collect_main_seal_pool.py tests\test_import_iwencai_pool.py tests\test_run_main_seal_follow_monitor_session.py tests\test_main_seal_follow_strategy.py
```

Latest result:

```text
50 passed, 1 warning
```

## What Is Not Yet Proven In Real Trading Time

The following items are implemented but still need real trading-session
validation on `2026-05-25`:

1. `08:50` scheduled stock-pool generation under live morning conditions.
2. Strategy activation after `09:30` with `strategies > 0`.
3. Normal ordinary-tick subscription growth from the generated CSV pool.
4. Level2 detail subscription growth when stocks get near limit-up.
5. A full dry-run trigger chain during live market data:
   `entry_signal_accepted -> dry_run_entry_submitted -> dry_run_probe_trade_recorded -> main_keep_decision/main_cancel_decision`

## Known Constraints

1. This workflow is dry-run only.
   Real trading is intentionally not enabled in this monitoring phase.

2. Jiuyangongshe latest-article logic has a date guard.
   If the latest article is not from the same day, the source is skipped unless
   historical article testing is explicitly requested.

3. `config/main_seal_follow_pool.csv` is a generated artifact.
   It changes during rehearsals and normal runs and should be treated as runtime
   output rather than source code.

## Next Trading Day Checklist

Primary log markers to watch on `2026-05-25`:

- `MONITOR_SESSION pool_collect_start`
- `MONITOR_SESSION pool_generated`
- `MONITOR_SESSION monitor_start`
- `Runtime heartbeat ... strategies=... tick_subscriptions=... l2_stocks=...`
- `MSF_EVENT {"event":"entry_signal_accepted", ...}`
- `MSF_EVENT {"event":"dry_run_probe_trade_recorded", ...}`
- `[ORDER] [TRADE] [MOCK] observation filled ...`
- `MONITOR_SESSION session_stop ...`

Minimum acceptance for the morning run:

- Pool generation succeeds.
- `strategies > 0` after trading-day activation.
- Heartbeats continue normally.
- No real order is sent.
- Logs are sufficient to reconstruct the morning session.

## Recommended Next Steps

1. Let the scheduled task run on `2026-05-25` and capture the morning logs.
2. Compare the observed event chain with
   `docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md`.
3. After the first real dry-run morning, summarize observed issues and decide
   whether to tighten thresholds, relax thresholds, or improve observability
   further.
