# QMT 撤单原生探针

`QMT_CANCEL_PROBE.py` 是一次性小额买单撤单探针，使用 QMT 内置 Python 模型接口：

- `passorder`
- `can_cancel_order`
- `cancel`
- `get_trade_detail_data`
- `order_callback`
- `deal_callback`

探针只输出 QMT 控制台，不使用 `open`、`read`、`write`，不连接外部程序，也不依赖独立 `xtquant` 客户端。

## 明天测试前修改

编辑脚本顶部的以下参数：

```python
ENABLE_LIVE_ORDERS = True
TARGET_CODE = "603081.SH"
ORDER_PRICE = 11.72
ORDER_QTY = 100
```

价格应选择不容易立即成交的限价，否则订单可能在撤单前已经成交。`ORDER_QTY` 必须是 100 的整数倍。`CANCEL_DELAY_SECONDS` 默认是 1 秒，可按测试需要调整。

探针启动后等待 `SUBMIT_DELAY_SECONDS`（默认 5 秒）提交一笔订单，之后到达撤单时间时：

1. 先查询 `can_cancel_order`。
2. 使用当前定时器回调传入的 `ContextInfo` 调用 `cancel`。
3. 通过订单回报或状态查询确认撤单。
4. 记录 `ContextInfo.request_id`、委托号、可撤结果、撤单返回值和异常。

默认 `ENABLE_LIVE_ORDERS = False`，编译或启动不会下单。测试前必须明确修改为 `True`，测试结束后停止该模型。

## 预期日志

成功路径应包含：

```text
[SUBMIT_CALL]
[SUBMIT_RETURN]
[ORDER_ID_BOUND]
[CAN_CANCEL] result=True
[CANCEL_CALL] ... request_id=...
[CANCEL_REQUESTED]
[ORDER_UPDATE] ... status=5/6/53/54
[TERMINAL] phase=CANCEL_CONFIRMED
```

若再次出现 `NoneType` 的 `request_id`，保留完整的 `[CANCEL_CALL]`、`[CANCEL_EXCEPTION]` 和 QMT 主日志；这可以区分当前上下文传递问题与 QMT 运行时缺陷。若订单先成交，日志应以 `phase=FILLED` 结束，不应再判定为撤单成功。

## QMT 操作

1. 将 `QMT_CANCEL_PROBE.py` 作为新的原生 Python 模型入口载入。
2. 编译并确认默认配置为不下单；准备好人工参数后再改为 `ENABLE_LIVE_ORDERS = True` 并重新编译。
3. 交易时段启动模型，观察 QMT 控制台日志。
4. 撤单回报确认后停止模型，并核对账户委托与成交状态。
