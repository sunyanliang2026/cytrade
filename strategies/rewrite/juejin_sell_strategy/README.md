# Juejin Sell Native QMT Model

这是 `juejin_sell_strategy` 的 QMT 原生 Python 重写版本，独立于上级目录的大单涨停买入模型 `QMT_LARGE_ORDER_LIMIT_UP_BUY.py`。

## 当前范围

- 原生入口：`QMT_JUEJIN_SELL.py`。
- 行情：`l2quote` 作为卖出信号主行情，普通 `tick` 用于维护开盘价、日内高低点等辅助数据。
- 输入：UTF-8-SIG 编码的 `sell_10.csv`，列为 `symbol,exp,sellvol,nick`。
- 实盘默认开启：`ENABLE_LIVE_ORDERS = True`，账号为 `32011276`。
- 当前约定：单日连续运行，不做重启后的历史委托接管。中途重启前必须人工核对持仓和活动委托。

部署时将脚本复制到 QMT 原生 Python 模型目录的 `JUEJIN_SELL.py`，并将 CSV 放到脚本目录的 `data/sell_10.csv`，或放到 QMT 的 `python/sell_10.csv`。也可以通过环境变量 `CYTRADE_JUEJIN_SELL_CSV_PATH` 指定路径。每次更新后运行上级目录的 `sync_to_qmt.ps1` 同步源码。

## 运行顺序

每条有效 `l2quote` 按以下顺序处理：

1. 更新昨收、涨跌停价、买一/卖一、封单量、开盘价和日内高低点。
2. 处理集合竞价严重不及预期规则；`09:26` 前不执行普通盘中规则。
3. 处理上一个卖出信号等待撤单完成的挂起请求。
4. 确认涨停封单并进入 `flag=10`，再识别 `flag=-3`。
5. 依次处理 `-3` 反弹失败、跌停清仓、`flag=5` 和 `flag=7`。
6. 处理涨停开板首卖，以及首卖后跌破昨收 `+3%` 的第二笔。
7. 开板超过 5 分钟不回封时恢复 `flag=0`，不直接卖出。
8. 更新普通价格状态、上一笔买一和上一笔封单金额。

`flag` 只表示信号状态；订单另有 `SUBMITTING`、`ACTIVE`、`PARTIAL`、`CANCEL_REQUESTED`、`CANCELED`、`FILLED` 等生命周期状态。委托提交不等于成交。

## 卖出规则

规则数值与现有策略保持一致：

- `flag=10`：买一涨停且封单金额超过 `1.2` 亿元。开板或封单显著减少时卖第一笔，价格为买一乘 `0.982`。
- 开板首卖后，买一从昨收 `+3%` 以上跌破 `+3%` 时卖第二笔，价格为买一乘 `0.99`。
- `flag=5` 只能由 `flag=-3` 反弹进入。
- `flag=7` 支持日内高点回落 `2.5%`、冲高 `8.5%` 后跌破 `7%`、早盘跌破 `6.5%` 三条卖出规则。
- 跌停清仓使用当前全部可卖数量。

每笔卖出目标数量来自 CSV 的 `sellvol`。实盘在 `init`、最新 K 线的 `handlebar` 和每 2 秒一次的原生定时回调刷新全局可卖量缓存，行情回调不直接查询持仓。缓存超过 6 秒或查询失败时短暂保留信号；刷新后重新校验最新 L2 条件，失效或超过 5 秒则放弃。定时回调中的查询能力仍需在当前 QMT 实机验证。委托数量不超过缓存可卖量和 `sellvol` 扣除本模型已成交量后的余额。

## 实盘前检查

1. 核对默认开启的实盘开关、账号、股票池和当前活动委托，运行测试并观察盘中日志。
2. 下单使用 11 参数形式，订阅回调即时委托使用 `quickTrade=2`。已在本机大 QMT 的 `xtconstant.py` 核对卖出方向为 `24`、限价委托类型为 `1101`、指定价类型为 `11`；持仓查询使用 `get_trade_detail_data(account, account_type, "POSITION")`，原生字段为 `stock_code`、`can_use_volume`。委托和成交回报兼容 `XtOrder` 的 `order_id`、`order_volume`、`traded_volume`、`order_status`、`order_remark`，以及 `XtTrade` 的 `order_id`、`traded_id`、`traded_volume`、`traded_price`。
3. 确认账户、CSV 和日志路径无误，并在 QMT 中重新编译或启动已同步的策略。
4. 首次实盘只使用一只股票和较小的 `sellvol`，核对 `[SELL_SUBMIT]`、`[ORDER_UPDATE]` 和 `[DEAL]` 日志。

订单回报兼容 QMT 原生状态码：`51/52` 表示撤单请求处理中，`53/54` 表示撤单完成或部分撤单，`56` 表示成交，`57` 表示废单。成交回报按交易 ID 去重；订单号尚未绑定时先暂存成交回报，绑定后再计入订单，避免漏记成交。

模型不会在 `stop()` 中主动撤销已有卖单；停止模型前应人工确认活动委托处理方式。
