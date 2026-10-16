# LargeOrderLimitUpBuy

## 自动延迟排查

下次通过现有 `run_live.bat` 或 `run_market_only.bat` 启动时，会自动开启延迟诊断，无需额外运行探针。已在运行的进程不会自动加载新代码。

- 逐笔记录行情时间、SDK最早Python入口、解析完成、策略锁等待、处理与写盘耗时；每笔买入额外记录下单接口起止和柜台返回的原始订单时间。
- 独立进程只订阅同一股票池的四类L2行情，不连接交易账户；使用 `count=0`，每秒读取一次QMT缓存进度，采集QMT/主进程CPU、线程、磁盘和时钟情况。它会增加一组行情订阅及采集开销，原策略的订阅参数保持不变。
- 发现连续竞价行情年龄达到3秒、处理或锁等待达到200毫秒，以及触发下单时，保存前120秒、后90秒的逐笔诊断窗口。环形缓冲有数量上限；高流量覆盖、采集丢包、探针故障和缺失证据都会显示在报告中。
- 报价、队列、每秒峰值、订阅和订单时间链持续保留；异常时保存Python线程栈，长时间等锁或处理会在阻塞尚未结束时采样。原有全量行情文件仍按原逻辑保存。
- 订阅前已有的历史记录单独统计，不能用其行情年龄认定实时延迟；历史补发的解析与处理负担仍参与诊断。

输出在本策略的 `data/latency_diagnostics/YYYY-MM-DD/HHMMSS_PID/`。正常结束时自动生成中文 `analysis.md` 和详细 `analysis.json`，每笔下单都有独立时间链。运行中或异常退出后可手动分析已有记录：

```powershell
python -m strategies.large_order_limit_up_buy.scripts.analyze_runtime_latency "strategies/large_order_limit_up_buy/data/latency_diagnostics/日期/会话目录"
```

控制台只显示采集启动、探针就绪、覆盖不足、分析完成和限频异常提示。若没有看到“独立探针已就绪”，应检查该会话的 `reference_startup.log`、`health.json`。故障后最多启动三次探针，每次追加保留原数据，不改变策略交易行为。

诊断用于区分本地解析、锁、处理、写盘、SDK分发和时钟问题。QMT缓存时间是进度证据，不是同一委托的接收时间；若QMT没有逐条网络接收证据，仍可能无法区分券商上游与QMT内部接收处理，报告会明确保留这个缺口。只有秒精度的柜台时间也不能证明毫秒级先后。

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

- 连续竞价开始后，当日最低价严格低于昨收价（最低涨幅低于0%）时，首封和回封都停止后续买入，并在订阅同步时取消该实例的 tick/L2 行情需求。最低价等于昨收价不剔除；最低价或昨收价缺失时暂不判断。已有挂单不撤销，成交回报仍正常处理；若其他策略需要同一股票，保留其订阅。已有未成交买单显示为“已下单，等待成交”。
- 为降低开盘前 30 秒的 L2 高峰负荷，`opening_l2order_delay_seconds` 默认设为 30 秒。09:30:00 至 09:30:30 不订阅、不解析、不逐条落盘 `l2order`，也放弃这段时间的交易信号；`tick` 与 `l2quote` 保持运行。保护窗口结束后动态订阅 `l2order`，订阅参数使用 `count=0`，只接收新委托。
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
