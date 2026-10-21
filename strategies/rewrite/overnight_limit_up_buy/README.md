# 大 QMT 隔夜涨停买入模型

此目录存放与大单策略分开的 QMT 原生 Python 隔夜下单模型。

- `QMT_OVERNIGHT_LIMIT_UP_BUY.py`：独立原生模型，默认实盘开启，账户为 `32011276`。
- `docs/DESIGN.md`：运行时序、校验、安全状态和部署说明。
- `data/overnight_orders.csv.example`：订单 CSV 模板。
- `tests/`：离线接口与时序验证。

实盘默认启用，账户为 `32011276`。模型不依赖 qmtmini、xtdata 或外部 Python 进程。

模型按时钟驱动提交：不调用交易日历接口，08:30 前启动会等待到提交时刻，08:30 或之后启动则在分片预检完成后立即提交。`UpStopPrice` 必须有效；详情里的 `TradingDay` 不作为阻断条件。QMT `[quote]` 是仿真模式，不会代表真实委托；发送实盘订单时必须使用 `[trade]` 模式。
