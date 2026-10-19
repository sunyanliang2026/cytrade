# QMT 持仓查询上下文探针

探针用于确认 `get_trade_detail_data(account, "stock", "POSITION")` 在 QMT 原生模型的不同执行位置是否可用。它只读取持仓，并订阅 `002343.SZ` 的普通 `tick` 行情；不调用下单、撤单或账户设置接口。

## 运行前

1. 打开 `QMT_POSITION_QUERY_PROBE.py`，把 `ACCOUNT_ID` 填成当前卖出模型使用的账号 ID。该值只传给持仓查询，不写入日志。
2. 将探针作为一个独立的 QMT 原生 Python 模型运行。探针每个执行位置最多查询一次：`init`、首次 `handlebar(C)`、首条 `tick` 订阅回调。
3. 在交易时段运行，确保能收到目标股票的 `tick`。不需要停止或修改原卖出模型的参数。

日志写入 `C:\光大证券金阳光QMT实盘\userdata\log\qmt_position_probe_YYYYMMDD.log`。

## 结果判断

- `init` 或 `handlebar` 成功、`quote_callback` 报 `request_id` 异常：持仓查询依赖 QMT 主模型上下文，不能直接放在行情订阅回调里。
- `handlebar` 成功、`init` 失败：查询在模型初始化时尚不可用，后续应从主执行回调查询。
- 三处都报相同错误：再核对 QMT 内置模型的账号 ID、账号类型及账号授权/API 用法；当前异常不能说明账户没有持仓。
- `QUERY_RESULT` 的 `matched=1` 且 `can_sell` 有数值：查询正常；`matched=0` 只表示查询结果没有匹配到目标股票。

探针不会改变卖出策略。确认上下文后，再单独评估是否需要把持仓查询移到主执行回调，并避免多个卖出条件在同一行情事件中重复查询。
