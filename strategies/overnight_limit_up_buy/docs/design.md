# Overnight limit-up buy strategy

This strategy ports the Juejin overnight order idea into miniQMT with a narrow
business surface:

- CSV contains only `stock_code,amount`.
- Submission time is fixed by the runner default: `08:30:00`.
- Direction is always `BUY`.
- Price is today's limit-up price from QMT/xtdata.
- Quantity is `floor(amount / limit_up_price / 100) * 100`.
- Every CSV row is an independent order. Repeated stock codes are allowed.
- Any row that cannot verify a limit-up price, cannot buy one lot, is already
  submitted, or fails batch funding preflight aborts the entire batch before
  waiting for 08:30.
- Live mode dispatches at `submit_time + counter_offset_ms` and allows one retry
  only after an explicit counter rejection saying the market is not open yet.
  Unknown results and transport failures are never retried automatically.

The runtime script defaults to dry-run. Without `--live`, it sends orders through
`TradeExecutor` with `live_trading_enabled=False`, so it registers mock orders
and does not touch the live counter.

Live submission is available only through explicit confirmation.
The script does not modify local runtime config. In live mode it temporarily
builds runtime settings with dry-run disabled, connects QMT through the existing
runtime path, and freezes the complete batch before waiting. The frozen plan
shows CSV row, stock, budget, previous close, limit-up submission price,
quantity, estimated amount, and batch total. The user confirms that table.

```powershell
--live --confirm-live --require-plan-confirm
```

All costly validation, price calculation, and the single batch funding check
happen before waiting. At 08:30 the runner only issues the frozen asynchronous
orders in CSV order; detailed logging and state persistence occur after those
requests have been sent.

For manual use, edit the parameters at the top of:

```text
strategies/overnight_limit_up_buy/scripts/run_overnight_limit_up_buy.bat
```

The BAT reads `data/orders.csv`, displays it for confirmation, then displays the
frozen plan for final confirmation. It defaults to `RUN_LIVE=false`. Set
`RUN_LIVE=true` only when a real broker-counter submission is intended.

The state file records submitted `trade_day:row:<CSV row number>` keys, so
multiple rows for one stock remain independent while an unchanged CSV is not
sent twice on the same day.

The current BAT defaults are `COUNTER_OFFSET_MS=50`, `RETRY_DELAY_MS=100`,
`REJECTION_WAIT_MS=100`, and `MAX_RETRIES=2`. `MAX_RETRIES` means retries after
the first attempt, so the default allows up to three total attempts. A retry
uses a new order UUID and trace ID; only the accepted attempt is recorded as
submitted.
