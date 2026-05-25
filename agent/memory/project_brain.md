# Cytrade project brain

Last seeded: 2026-05-24

## Current operating phase

The active workflow is `MainSealFollow` Level2 dry-run monitoring. The system should generate the stock pool before the open, start the market-only dry-run runtime, observe tick and Level2 subscriptions, and stop around noon. This phase is for validation and monitoring, not live trading.

## Core log markers

Use these markers to reconstruct a morning session:

- `MONITOR_SESSION pool_collect_start`
- `MONITOR_SESSION pool_generated`
- `MONITOR_SESSION monitor_start`
- `MONITOR_SESSION session_start`
- `Runtime heartbeat ... strategies=... tick_subscriptions=... l2_stocks=...`
- `MSF_EVENT {"event":"entry_signal_accepted", ...}`
- `MSF_EVENT {"event":"dry_run_probe_trade_recorded", ...}`
- `[ORDER] [TRADE] [MOCK] observation filled ...`
- `MONITOR_SESSION session_stop`
- `MONITOR_SESSION stopped ... real_order_sent=false`

## Minimum morning acceptance

- Stock pool generation succeeds and has `total > 0`.
- Runtime monitor starts.
- At least one heartbeat is emitted.
- After trading activation, `strategies > 0`.
- After trading activation, the market-data path is actually alive: `connected=True`, `tick_subscriptions > 0`, and `latest_data_time` is non-empty.
- No non-mock order/trade line appears.
- Logs are sufficient for replay.

## Current known failure signature

Treat a morning run as an invalid monitor session, not merely a weak strategy day, when all of the following hold after 09:30:

- `strategies > 0`
- `connected=False`
- `tick_subscriptions=0`
- `latest_data_time` is empty
- no `MSF_EVENT` trigger chain appears

This means the framework started and strategies were created, but tick / Level2 market data never became usable, so no in-session strategy judgment actually happened.

## First self-improving loop

The first loop should make the system easier to observe, test, and review:

1. Parse morning logs.
2. Generate a markdown report and JSON summary.
3. Propose 1-3 low-risk improvement tasks.
4. Give Codex CLI a constrained task prompt.
5. Run safety and quality gates.
6. Human reviews and approves changes.
7. Lessons are written back here and to `known_failures.yaml`.

## Boundaries

Agents may propose patches, but they must not automatically merge, deploy, enable real trading, alter credentials, alter account paths, or widen risk limits.
