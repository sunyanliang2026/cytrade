# LargeOrderLimitUpBuy

The unimplemented reseal early-signal discussion is in
[`docs/reseal_early_signal_idea.md`](docs/reseal_early_signal_idea.md).

## Next-day L2 latency review

Each stock writes `<code>_<instance>.l2_latency.csv` under `data/runtime_records/YYYY-MM-DD/`.
For each L2 quote it records exchange event time, xtdata callback entry time,
strategy entry time, and completion of the raw JSONL write. The three millisecond
columns separate source-to-callback age, callback-to-strategy wait, and quote
raw-write cost. The order/transaction/queue columns summarize raw-write counts,
total time, and worst time since the previous quote. Missing event or callback
timestamps remain blank; do not treat them as zero latency.
L2 quote event times are sampled on the feed's roughly three-second cadence;
millisecond values show local timing stages, not millisecond exchange quote precision.

During 09:29-09:35, `logs/system.*.log` also contains one `L2 callback 10s
profile` about every ten seconds. Its per-kind tuple is callback count, total
execution milliseconds, and maximum single-callback milliseconds. Compare this
with the per-stock CSV before attributing a late quote to QMT or local processing.
`source_to_callback_ms` includes any backlog inside xtdata before Python callback
entry, so it cannot alone separate QMT/network delay from that internal backlog.
The diagnostic files do not participate in entry or order decisions.
For an actual buy, the existing `buy_submitted` trade-log event also includes
`trigger.callback_time` and `trigger.strategy_time`; compare these with the
trigger's exchange `event_time` and the order submission log time.

## Current Entry Rules

- First-seal entries require `bid1 == exact limit-up price`, a bid-one seal above 60 million yuan, then more than 10 million yuan cumulatively across limit-up BUY `l2order` records of at least 2 million yuan each. The opening-dip requirement remains in effect.
- 回封先卡位再验证：下单后累计出现两笔不低于150万元的大单即通过。大单不足时，必须同时观察至少150笔（配置值）且本地下单后至少1秒，才撤销未成交单；不足1秒时继续统计后续大单，超过1秒但不足150笔时继续等笔数。`reseal_validation_min_seconds` 默认1秒，单调时钟和独立定时器负责到时检查，无新行情也会检查。失败后沿用5秒补充观察及撤单确认后最多追随一次的规则；成交、停止和14:57后不会因该定时器撤单。
- After either entry, the next 30 seconds of 1.5 million yuan limit-up buy orders are written to a Chinese, one-order-per-row CSV with order, fill, cancel, remaining amount, and status.
- A reseal is eligible only after the prior sealed period was observed above 100 million yuan and longer than 10 seconds. The reopened period must last at least 3 seconds; no reopened-low-price condition applies.
- Planned rule: when a pool stock's opening gain is below 3.5%, its monitoring will stop for that trading day. This rule is documented but not implemented yet.
- Any partial or full entry fill disables further entries for that stock instance for the remainder of the run.
- `bid1 == exact limit-up price` is required for sealed/reopened state detection and first-seal entry eligibility. A sell-one quote at limit-up is not a sealed board and does not trigger an entry.

独立的 Level2 大单打板策略。人工股票池位于 `data/manual_pool.csv`，设计文档位于 `docs/design.md`。

当前入口为：

```bat
strategies\large_order_limit_up_buy\scripts\run_market_only.bat
```

该入口只运行本策略的 market-only dry-run，会订阅人工股票池的 Level2 数据并记录原始事件、我方订单前方队列和相邻委托。它不连接交易账户，不发送真实订单。

实盘验证入口为：

```bat
strategies\large_order_limit_up_buy\scripts\run_live.bat
```

先编辑 `run_live.bat` 顶部的参数。默认 `RUN_LIVE=false`，仍只运行 dry-run。实盘验证时必须：

1. 在 `data/live_test_pool.csv` 中填写一行或多行 `code,plan_amount`。
2. 将 `RUN_LIVE=true`。
3. 设置不小于任一单只计划金额的 `MAX_ORDER_AMOUNT`，以及不小于 CSV 合计计划金额的 `MAX_TOTAL_AMOUNT`。
4. 双击运行后，确认控制台显示的 CSV 股票池，再输入 `1` 作为第二次确认。

实盘模式会依次检查 CSV 股票池、每只和总计划金额上限、交易日、QMT 账户连接、账户资金和交易就绪状态。任一检查失败时退出，不进入 Level2 监听。
