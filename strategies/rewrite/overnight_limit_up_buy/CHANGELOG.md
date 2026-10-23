# Change Log

## 2026-10-23

- Replaced CSV input with `PLAN_TRADE_DATE` and `ORDER_PLAN` constants in the native model; both default empty, so an unconfigured daily plan stops before scheduling.
- Removed strategy state-file reads and writes. Attempt counts, callback mappings, and reservations now exist only for one model session; restart retry recovery is no longer supported.
- Added a live-start check using native QMT order records. The model stops on query failure, missing strategy identity, or an order tagged with this strategy. The installed QMT build must be checked to confirm the query only returns current-day orders and exposes strategy names; the implementation scans every returned record without date filtering.
- Updated the existing native-model fixtures and deployment notes. Synced only `QMT_OVERNIGHT_LIMIT_UP_BUY.py` to QMT `python/OVERNIGHT_LIMIT_UP_BUY.py`; both SHA-256 values are `f9365265f116cfc5b03bbc756f081e5fb2c09de60cf8104aad68b071b41d21a0`. Tests and QMT Python compilation were not run; the model must be recompiled/restarted, and live QMT query/order behavior remains unverified.

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
