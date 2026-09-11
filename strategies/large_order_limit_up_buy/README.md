# LargeOrderLimitUpBuy

## Current Entry Rules

- A large limit-up buy order is a single `l2order` worth at least 1.5 million yuan.
- First limit-up entries keep the opening-dip requirement and require that large order.
- Reseal entries submit on the first observed limit-up BUY `l2order`, then inspect the next 20 limit-up BUY orders. The order is retained only when at least two are large orders; otherwise the strategy requests cancellation of the unfilled order and disables further entries for that stock instance.
- A reseal is eligible only after the prior sealed period was observed above 100 million yuan and longer than 20 seconds. The reopened period must last at least 10 seconds and its low price must be below 98.5% of the limit-up price.
- Any partial or full entry fill disables further entries for that stock instance for the remainder of the run.
- `bid1 == exact limit-up price` is used only for sealed/reopened state detection. A sell-one quote at limit-up is not a sealed board and does not trigger an entry.

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
