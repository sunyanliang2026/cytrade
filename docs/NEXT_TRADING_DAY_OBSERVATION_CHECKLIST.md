# Next-Trading-Day Observation Checklist

This checklist is for the first real dry-run monitoring session on the next
trading day.

## Before 08:50

- Confirm the Windows scheduled task `Cytrade MainSealFollow Monitor` is in
  `Ready` state.
- Confirm QMT is logged in and `userdata_mini` is available.
- Confirm `config/main_seal_pool_sources.json` is the intended stock-pool
  configuration.

## 08:50-08:55

Expected log markers:

- `MONITOR_SESSION pool_collect_start`
- `SET ...`
- `SET_SUMMARY ...`
- `FINAL expression_result=...`
- `MONITOR_SESSION pool_generated`
- `MONITOR_SESSION monitor_start`

Acceptance:

- Stock pool generation succeeds without uncaught exceptions.
- `config/main_seal_follow_pool.csv` is updated.
- If Jiuyangongshe is skipped, the warning must explain why.

## 09:30-09:35

Expected log markers:

- `MONITOR_SESSION session_start`
- `Runtime startup mode=market-only-monitor dry_run=True ...`
- `StrategyRunner: 已启动 ... 个策略`
- `Runtime heartbeat ... strategies=... tick_subscriptions=...`

Acceptance:

- `strategies` is greater than `0`.
- `tick_subscriptions` is greater than `0`.
- If a stock approaches limit-up, `l2_stocks` should grow from `0`.

## During Trading

Watch these event chains:

- `MSF_EVENT {"event":"entry_signal_accepted", ...}`
- `MSF_EVENT {"event":"entry_plan_created", ...}`
- `MSF_EVENT {"event":"dry_run_entry_submitted", ...}`
- `MSF_EVENT {"event":"dry_run_probe_trade_recorded", ...}`
- `[ORDER] [TRADE] [MOCK] observation filled ...`
- `MSF_EVENT {"event":"main_keep_decision", ...}`
- `MSF_EVENT {"event":"main_cancel_decision", ...}`

Acceptance:

- At least one candidate stock reaches the trigger path if market conditions
  permit.
- For any simulated probe fill, the log contains stock, price, quantity,
  trigger reason, and decision metrics.
- Heartbeat continues every 30 seconds; long silence means the process needs
  investigation.

## 12:00 Stop

Expected log markers:

- `MONITOR_SESSION session_stop reason=noon_stop ...`
- `MONITOR_SESSION stopped system_log=... trade_log=... dry_run=True real_order_sent=false`

Acceptance:

- Process exits automatically without manual intervention.
- No real order is sent.
- Log files can be used to replay the morning session.

## If Something Looks Wrong

- `strategies=0`: check whether the session started on a trading day and
  whether the CSV was generated correctly.
- `tick_subscriptions=0`: check whether strategies loaded from
  `config/main_seal_follow_pool.csv`.
- `l2_stocks=0` all morning: likely no stock got near limit-up, or the trigger
  conditions are too strict.
- No `MSF_EVENT`: check summary mode logs and confirm the strategy actually
  subscribed to candidate stocks.
