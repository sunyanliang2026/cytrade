# Change Log

## 2026-10-16

- Removed the trading-calendar gate so a stale pre-open calendar response cannot abort the clock-scheduled order path.
- Allowed a valid `UpStopPrice` to proceed when `TradingDay` metadata is missing or from the previous session; the mismatch is logged for diagnosis.
- Dispatches the first submission in the same scheduler callback that completes the final preflight slice after 08:30, and rate-limits unrelated order callback logs.
- Treats `passorder` return `0` as an accepted submission without an order ID, correlates blank-remark broker callbacks by code/price/quantity, and releases an old reservation when the broker has no matching order.

## 2026-10-15

- Added a standalone native QMT model for the overnight limit-up buy workflow.
- Added sliced preflight and submission scheduling, strict trading-day/limit-price checks, batch funding validation, duplicate protection, and callback-driven retries only for explicit pre-open rejections.
- Kept real orders disabled by default and added a separate CSV example, design notes, and offline tests.
- Verified the tests with the Python runtime bundled with QMT; no QMT account or live order was used.
