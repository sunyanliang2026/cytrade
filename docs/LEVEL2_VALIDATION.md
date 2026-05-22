# Level2 Validation Notes

This document records live QMT/xtquant observations that are used as the
implementation basis for `MainSealFollowStrategy`.

## Environment

- Date: 2026-05-21
- QMT data dir: `C:\光大证券金阳光QMT实盘\userdata_mini\datadir`
- xtdata service: `127.0.0.1:58610`
- Python env: `C:\Users\ysun\miniconda3\envs\cytrade311`
- Validated stock: `001259.SZ` / 利仁科技

## 001259.SZ Limit-Up State

Observed at 2026-05-21 13:41:11 to 13:44:11.

- Limit-up price from `get_instrument_detail(...).UpStopPrice`: `88.58`
- Before probe: `lastPrice=88.58`, `bid1=88.58`, `bid1Vol=3698 lot`, `ask1=0`
- After probe: `lastPrice=88.58`, `bid1=88.58`, `bid1Vol=3681 lot`, `ask1=0`

The stock was limit-up and sealed during the validation window.

## l2order Findings

Live probe duration: 180 seconds.

- `l2order` records: `113`
- `entrustType=1`: `113`
- `entrustDirection=1`: `73`
- `entrustDirection=2`: `40`
- Limit-price records at `88.58`: `88`
- Limit-price buy additions: `56`
- Limit-price sell additions: `32`

Implementation rule:

- `entrustDirection=1` means buy order addition.
- `entrustDirection=2` means sell order addition.
- For Shenzhen stocks in this sample, `l2order` did not emit
  `entrustDirection=3/4` for cancellations.

Example limit buy addition:

```json
{
  "time": 1779342217070,
  "price": 88.58,
  "volume": 800,
  "entrustNo": 43360644,
  "entrustType": 1,
  "entrustDirection": 1
}
```

## Shenzhen Cancellation Findings

Live probe duration: 180 seconds.

- `l2transaction` records: `120`
- `tradeFlag=3` cancellation records: `58`
- Matched to known `l2order.entrustNo`: `57`
- Confirmed limit-price buy cancellations: `45`
- Unmatched cancellations: `1`

Observed cancellation shape:

- `l2transaction.price=0.0`
- `l2transaction.amount=0.0`
- `l2transaction.tradeFlag=3`
- `buyNo` references original order number when cancelling a buy order.
- `sellNo` was `0` for observed buy cancellations.

Implementation rule:

- Shenzhen cancellation is detected from `l2transaction.tradeFlag=3`.
- Cancellation price must not be read from `l2transaction.price`.
- Cancellation direction and price must be inferred by joining
  `l2transaction.buyNo/sellNo` to a rolling index of `l2order.entrustNo`.
- If the referenced order is missing from the index, classify the cancellation
  as `UNKNOWN_CANCEL` and do not count it as limit-price buy cancellation.

Confirmed matched cancellation example:

```json
{
  "transaction": {
    "time": 1779341718330,
    "price": 0.0,
    "volume": 600,
    "amount": 0.0,
    "tradeIndex": 42120464,
    "buyNo": 41505515,
    "sellNo": 0,
    "tradeType": 0,
    "tradeFlag": 3
  },
  "matched_order": {
    "entrustNo": 41505515,
    "time": 1779341518570,
    "price": 88.58,
    "volume": 600,
    "entrustDirection": 1
  },
  "derived": {
    "cancel_side": "BUY",
    "cancel_price": 88.58,
    "cancel_volume": 600,
    "cancel_amount": 53148.0
  }
}
```

Unmatched example:

```json
{
  "time": 1779342210050,
  "price": 0.0,
  "volume": 10000,
  "amount": 0.0,
  "tradeIndex": 43347424,
  "buyNo": 412565,
  "sellNo": 0,
  "tradeType": 0,
  "tradeFlag": 3
}
```

The unmatched case proves the strategy needs a conservative fallback when the
original order is not in the local index.

## Threshold Statistics

During the 180-second probe:

| Threshold | Limit buy adds | Limit buy add amount | Limit buy cancels | Limit buy cancel amount |
|---:|---:|---:|---:|---:|
| 100,000 CNY | 2 | 779,504 | 2 | 611,202 |
| 500,000 CNY | 0 | 0 | 0 | 0 |
| 2,000,000 CNY | 0 | 0 | 0 | 0 |

Implication:

- The current default `big_amount_min=2,000,000` is too high for this specific
  observed window of `001259.SZ`.
- The strategy should keep `big_amount_min` configurable and record diagnostics
  at multiple thresholds during calibration.

## l2orderqueue Findings

Observed `l2orderqueue` snapshots confirm partial queue behavior:

- `bidLevelPrice=88.58`
- `observed_len=50`
- `bidLevelNumber` ranged from `715` down to `704`
- First snapshot observed sum: `128 lot` across 50 visible entries
- Last snapshot observed sum: `119 lot` across 50 visible entries

Implementation rule:

- `bidLevelVolume` is a visible front-window queue, not the complete queue.
- `bidLevelVolume` unit is lot for the observed QMT feed.
- `bidLevelNumber > len(bidLevelVolume)` means the queue is partial.
- Queue logic must expose `observed_queue_count`, `reported_total_order_count`,
  and `is_partial_queue`.

## Implementation Basis

The strategy can implement these market-state dimensions:

- Limit-price buy addition:
  `l2order.price == limit_up_price`
  and `entrustDirection == 1`.
- Limit-price sell addition:
  `l2order.price == limit_up_price`
  and `entrustDirection == 2`.
- Shenzhen limit-price buy cancellation:
  `l2transaction.tradeFlag == 3`
  and `buyNo` maps to a previous `l2order` whose
  `price == limit_up_price` and `entrustDirection == 1`.
- Cancellation amount:
  `matched_order.price * l2transaction.volume`.
- Unknown cancellation:
  `tradeFlag == 3` but neither `buyNo` nor `sellNo` maps to a known order.

Required code changes:

- Map `entrustDirection` in `DataSubscriptionManager._parse_l2_order_record`.
- Extend transaction parsing to preserve `tradeIndex`, `buyNo`, `sellNo`,
  `tradeType`, and `tradeFlag`.
- Maintain a rolling `entrustNo -> L2OrderEvent` index in strategy state.
- Convert matched Shenzhen cancellation events into derived cancel events that
  can feed `recent_big_limit_cancel_orders`.
- Treat unmatched cancellations as risk signals but not as confirmed limit-price
  buy cancellations.
- Record queue coverage fields for partial queue handling.

Acceptance criteria:

- For `001259.SZ`, realtime `l2order.entrustDirection=1/2` is normalized to
  `BUY` and `SELL`.
- For `001259.SZ`, realtime `l2transaction.tradeFlag=3` with `buyNo` matching a
  known limit-price buy order is normalized to `CANCEL_BUY`.
- The normalized cancel event has `price=88.58`, not `0.0`.
- The normalized cancel amount is calculated from matched order price and
  transaction cancel volume.
- Unknown Shenzhen cancellations are logged separately and do not pollute
  confirmed limit-price big-cancel metrics.
