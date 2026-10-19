# Juejin Sell Native Design

## 运行边界

模型使用 QMT 原生 `init(ContextInfo)`、`handlebar(ContextInfo)`、`stop(ContextInfo)` 入口，并通过 `subscribe_quote` 接收 `tick` 和 `l2quote`。行情回调、订单回调和成交回调共享脚本全局实例容器；不创建线程，不在回调中等待外部结果。

第一版不做跨重启恢复。运行期间的状态和订单生命周期保存在内存中；重启模型前由操作人员确认没有需要接管的历史卖单。后续如果需要自动恢复，再单独增加状态文件和启动前委托核对。

## 状态分层

`flag` 保持现有策略含义：

```text
0   普通价格逻辑
1   竞价严重不及预期
-3  弱势
5   弱势反弹
7   强势未涨停
10  涨停封单确认
99  跌停清仓已触发
-9  本轮卖出信号已触发
```

订单生命周期独立于 `flag`。每个实例可以同时保留多个订单记录，以支持竞价或弱势反弹分支的多笔委托；需要撤单后再卖的分支通过 `pending_after_cancel` 等待所有活动订单收到撤单回报。

## 行情处理

- `tick`：更新开盘价、日内最高价、日内最低价和普通行情辅助字段，不直接触发卖出。
- `l2quote`：读取买一、买一量、卖一、卖一量并执行卖出状态机。
- 封单金额按 `price * bid_volume * 100` 计算，QMT 的盘口量按手处理。
- 开板和撤单等待使用 `time.monotonic()`；行情时间只用于交易时段判断。
- 盘中启动时，如果初始快照已经处于涨停且封单金额超过确认阈值，直接进入 `flag=10` 观察状态，等待后续开板或封单减弱。

## 订单处理

实盘在 `init`、最新 K 线的 `handlebar` 及每 2 秒一次的原生定时回调中调用 `get_trade_detail_data(account, account_type, "POSITION")`，将原生 `XtPosition.stock_code` 和 `XtPosition.can_use_volume` 写入全局缓存。日线 `handlebar` 可能长时间不再触发，因此定时刷新维持缓存时效。订阅回调只读取 6 秒内的缓存；缓存失效时将信号暂存最多 5 秒，刷新后用最新 L2 报价重新核对条件及价格。定时回调中的持仓查询上下文仍需在 QMT 实机验证；查询失败或真实可卖量为零时不下单，日志分别记录原因。委托数量还受本模型已成交量与 `sellvol` 的差额约束。

下单使用 11 参数形式：`passorder(24, 1101, account, code, 11, price, quantity, strategy, 2, user_order_id, ContextInfo)`。其中本机大 QMT `xtconstant.py` 对应 `STOCK_SELL=24`、限价委托类型 `1101`、`FIX_PRICE=11`。委托回报解析 `XtOrder.order_id`、`order_volume`、`traded_volume`、`order_status` 和 `order_remark`；成交回报解析 `XtTrade.order_id`、`traded_id`、`traded_volume` 和 `traded_price`。订单回报按 QMT 状态码区分撤单处理中、撤单完成、成交和废单；成交回报按交易 ID 去重，订单号异步绑定期间暂存的成交在绑定后回放。没有委托号的订单保持待确认并阻止同股重复发单，不盲绑账户最近委托号。

卖出动作使用 action key 去重。一次实盘下单已经发起但返回未知时，本次运行不自动重试相同 action，避免 QMT 返回异常时重复卖出。撤单请求没有得到确认前，不提交依赖撤单完成的下一笔卖单。

## 关键验证

- 盘中启动时已涨停，随后开板能触发第一笔。
- 第一笔后跌破 `+3%`，必须先完成撤单再提交第二笔。
- 开板超过 5 分钟不回封，只回到普通价格逻辑。
- `flag=5` 只能从 `-3` 进入。
- `flag=7` 高点回落 `2.5%` 能提交一笔。
- 实盘可卖数量小于 `sellvol` 时按可卖数量下单。
- QMT 返回无订单号或异步回报时，订单不会被误判为成交。
