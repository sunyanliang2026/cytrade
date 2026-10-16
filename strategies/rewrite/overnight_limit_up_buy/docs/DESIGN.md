# 大 QMT 隔夜涨停买入原生模型

## 行为

模型读取 `stock_code,amount` 两列 CSV，每行是一笔独立买单，允许同一股票重复出现。下单价取 QMT `ContextInfo.get_instrument_detail()` 返回的有效 `UpStopPrice`；股数按预算除以涨停价向下取整至 100 股。任何 CSV、价格、整手或可用资金检查失败，整批都不会发单。

启动后按最多 5 行为一片处理预检，提交也按最多 5 笔一片执行。计划行和失败原因随预检分片输出，末尾只打印汇总。模型不使用线程、线程池、`sleep` 或回调参数保存状态。`init`、定时器和交易回调间的状态保存在脚本全局容器。模型不调用交易日历接口，也不会因日历服务返回上一交易日而中止；详情中的 `TradingDay` 仅作提示，`UpStopPrice` 有效即可继续。模型在整批中止、模拟提交完成或收盘后取消定时任务；实盘提交后定时任务保留到收盘，以处理异步拒单和有限重试。

08:30 前启动时，模型可以先完成预检并等待到 08:30；08:30 或之后启动时，预检完成后立即开始提交。最后一片预检若已到提交时刻，会在同一调度回合进入首笔提交。为了避免收盘后补发，15:00 起停止提交。只在订单回调明确包含“未到开市”等柜台拒单文本时，才按 100ms 间隔最多重试两次；不明确的返回结果、异常和其他拒单不自动重试。

QMT 的 `[quote] start simulation mode` 只用于仿真，不代表实盘委托已送达；需要真实委托时必须在 QMT 的 `[trade]` 模式启动模型。

## 安全与状态

- `ENABLE_LIVE_ORDERS=False` 且 `ACCOUNT_ID=""` 是默认配置。默认只输出计划，不调用 `passorder`。
- 开启实盘需要人工审阅并修改脚本顶部两个配置值。实盘启动还会注册账户、检查可用资金，并在第一笔 `passorder` 前把当天整批计划原子写入状态文件。QMT `passorder` 返回 `0` 时只表示调用没有返回订单号，模型等待异步回报；原生回报备注为空时按股票、价格和数量关联到计划。
- 当天已有状态保留时，模型先查询 QMT 委托；若旧批次没有对应委托则自动释放状态，否则拒绝重复启动。若查询不可用或提交结果未知，状态保留以防重复发单；当天需要核对 QMT 委托后再决定如何处理状态文件。
- 状态文件写入 `C:\光大证券金阳光QMT实盘\userdata\log\qmt_overnight_limit_up_buy_state.json`。这只写入本策略专用状态，不改 QMT 账户或交易配置。
- 订单 CSV 默认路径为 `C:\光大证券金阳光QMT实盘\python\overnight_orders.csv`。把示例复制为该文件后编辑订单内容；模型不会覆盖 CSV。

## 部署

1. 在 QMT “模型研究”中创建独立的原生 Python 模型，不覆盖现有 `QMT_LARGE_ORDER_LIMIT_UP_BUY.py`。
2. 将 `QMT_OVERNIGHT_LIMIT_UP_BUY.py` 作为独立文件部署到 QMT 的 Python 模型目录。
3. 将 `data/overnight_orders.csv.example` 复制到上述 `overnight_orders.csv` 路径并填写当天订单。
4. 先保持实盘开关关闭，在 QMT `[quote]` 模式检查 `INIT`/`PATHS`、`TRADING_DAY_CHECK_SKIPPED`、`PREFLIGHT_OK`、逐行 `PLAN` 和 `DRY_RUN` 日志。
5. 实盘调用需切换到 QMT `[trade]` 模式，并验证 `UpStopPrice` 字段、账户可用资金字段、委托回报字段和拒单文本。默认开关不会发送订单。

## 离线验证

运行：

```text
python -m unittest discover -s strategies/rewrite/overnight_limit_up_buy/tests -v
```

测试只使用模拟 `ContextInfo` 和模拟下单函数，不连接 QMT 账户。
