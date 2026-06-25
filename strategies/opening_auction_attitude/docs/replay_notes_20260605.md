# Opening Auction Attitude Replay Notes 2026-06-05

本文记录 `OpeningAuctionL2Probe` 与 `OpeningAuctionAttitude` replay 当前采用的数据语义。当前阶段只用于离线复盘和 dry-run 观测，不用于实盘下单。

## 1. 竞价盘口字段解释

在集合竞价阶段，若出现：

```text
bidPrice[0] == askPrice[0]
```

则将该盘口解释为竞价同价撮合盘口：

```text
auction_price = bidPrice[0] = askPrice[0]
matched_volume = min(bidVol[0], askVol[0])
unmatched_buy_volume = bidVol[1]
unmatched_sell_volume = askVol[1]
```

当前按 A 股常见行情单位处理：

```text
quote_volume_unit = 100
matched_amount = auction_price * matched_volume * quote_volume_unit
unmatched_buy_amount = auction_price * unmatched_buy_volume * quote_volume_unit
unmatched_sell_amount = auction_price * unmatched_sell_volume * quote_volume_unit
```

示例：

```text
bidPrice: [15.38, 0, ...]
askPrice: [15.38, 0, ...]
bidVol:   [978, 0, ...]
askVol:   [978, 301, ...]
```

解释为：

```text
当前竞价撮合价 = 15.38
当前匹配量 = 978 手
未匹配买量 = 0 手
未匹配卖量 = 301 手
```

## 2. 非同价盘口不按竞价撮合盘口解释

若出现：

```text
bidPrice[0] != askPrice[0]
```

则认为盘口已经不再是竞价同价撮合盘口，不能继续把 `bidVol[0] / askVol[0]` 当作竞价匹配量。

此时 replay 改用最终成交字段：

```text
raw.amount
raw.pvolume
raw.volume
```

作为累计成交金额和成交量来源，并将：

```text
amount_source_at_final = raw_amount
```

## 3. 金额来源语义

当前 replay 输出中：

```text
amount_source_at_low
amount_source_at_final
amount_is_cumulative
```

用于说明金额字段来源。

当前有效来源：

```text
auction_book
    集合竞价同价盘口推导出的当前匹配金额。

raw_amount
    行情 raw.amount / pvolume / volume，对 09:25 撮合结束后的最终成交数据更可靠。

tick_amount
    TickData.amount，后续 live observe-only 使用。
```

## 4. 低点到最终点的资金指标

replay 使用：

```text
auction_low_price
auction_low_time
auction_final_price
auction_final_time
low_to_final_lift_pct
amount_at_low
amount_at_final
low_to_final_amount_delta
low_to_final_amount_ratio
```

其中：

```text
low_to_final_amount_delta = amount_at_final - amount_at_low
low_to_final_amount_ratio = max(delta, 0) / max(amount_at_final, 1)
```

注意：只有当数据源能稳定表达累计匹配/成交金额时，该比例才可解释为“低点后新增金额占最终金额的比例”。

## 5. 当前 replay 输出位置

当前离线复盘输出：

```text
data/replay/opening_auction_attitude_YYYYMMDD*.csv
data/replay/opening_auction_attitude_YYYYMMDD*.md
```

这些文件属于本地复盘产物，已通过 `.gitignore` 忽略，不应提交。
