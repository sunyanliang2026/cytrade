# Change Log

## 2026-10-20

- Recognized QMT order status `57` as a junk order even when its rejection message is empty; scheduled at most two retries, 100 ms apart, without blocking the native event loop.
- Suppressed retries after any reported or uncertain fill and persisted attempt tokens, order IDs, status codes, and traded volumes before and after submission.
- Reconciled same-day reservations against broker order records. A changed CSV can proceed only after every previous order is confirmed terminal with zero fills; the same CSV resumes its saved retry count.
- Matched QMT `XtOrder.order_sysid` and its composed strategy-name format during reconciliation; added query and match counts while keeping missing or ambiguous records fail-closed.
- Set the requested live defaults to `ENABLE_LIVE_ORDERS = True` and `ACCOUNT_ID = "32011276"`.

## 2026-10-16

- Removed the trading-calendar gate so a stale pre-open calendar response cannot abort the clock-scheduled order path.
- Allowed a valid `UpStopPrice` to proceed when `TradingDay` metadata is missing or from the previous session; the mismatch is logged for diagnosis.
- Dispatches the first submission in the same scheduler callback that completes the final preflight slice after 08:30, and rate-limits unrelated order callback logs.
- Treats `passorder` return `0` as an accepted submission without an order ID and correlates blank-remark broker callbacks by code/price/quantity.

## 2026-10-15

- Added a standalone native QMT model for the overnight limit-up buy workflow.
- Added sliced preflight and submission scheduling, strict trading-day/limit-price checks, batch funding validation, duplicate protection, and callback-driven retries only for explicit pre-open rejections.
- Kept real orders disabled by default and added a separate CSV example, design notes, and offline tests.
- Verified the tests with the Python runtime bundled with QMT; no QMT account or live order was used.
