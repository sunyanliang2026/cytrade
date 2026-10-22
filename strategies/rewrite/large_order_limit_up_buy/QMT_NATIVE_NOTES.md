# QMT 原生 Python 注意事项

## 入口和线程模型

原生模型由 QMT 调用 `init(ContextInfo)`、`handlebar(ContextInfo)` 和 `stop(ContextInfo)`。行情、订单和成交回调在 QMT 的模型调度上下文中串行到达；脚本不能假设可以创建后台线程，也不能在回调里等待外部结果。

因此当前脚本：

- 不导入 `threading`，不使用线程池、Future 或 `sleep`。
- 不在回调中做不可控的网络、磁盘批量写入或长时间重试；通过新鲜度闸门的记录和可解析但不能用于决策的原始时间记录进入有界缓冲区，JSON 序列化和落盘由 `schedule_run` 分片完成。
- 把需要稍后完成的工作拆成有限片段，由 `schedule_run` 或下一次 `handlebar` 继续执行。
- `_NoopLock` 只提供统一的上下文写法，不等待、不产生并发互斥；它不是线程安全机制。

## 回调参数和状态

QMT 回调传入的数据对象可能是复用对象，并具有深拷贝和回滚语义。回调结束后不能依赖对参数对象的修改。当前实现先用事件时间过滤回放、过期和乱序事件，再通过 `_owned_payload()` 复制顶层字段和盘口数组，放入 `_INSTANCES`、`_RAW_BUFFER` 等脚本自有容器；不在高频路径执行递归 `deepcopy` 或 JSON 序列化。跨回调字段必须从这些容器读取。

回调入口只接受带绝对日期且有亚秒精度的事件；`l2quote` 最多允许 3000ms，`l2order` 最多允许 2000ms，启动水位线之前的回放和同股票倒序事件也丢弃。通过时间闸门后，`l2quote` 只让连续竞价内能更新涨停盘口、负收益或开盘低点的报价继续执行；`l2order` 只让连续竞价内的涨停价买入、有效数量且未重复委托继续执行。仅有 `HHMMSS`、缺少日期或只有秒级精度的可解析时间仍写入原始记录并用于显示时段，但不改变行情/交易状态、初始化快照状态或触发下单，且不计算延时。延时告警仍分别使用 2000ms、1000ms，便于观察新鲜度窗口放宽后的数据。汇总中的 `freshness_drop` 按周期统计时间闸门拒绝数，`entry_drop` 按原因统计业务早筛拒绝数。

不要把以下状态写回 `ContextInfo` 或行情回调参数：

- 封板开始时间、封单峰值和大单去重集合。
- 验证笔数、验证定时器、撤单状态和跟随下单标记。
- 委托待报状态、原始数据缓冲区和统计计数器。

## 订阅和定时器

当前使用的原生调用形式是：

```python
C.subscribe_quote(code, period=period, dividend_type="follow",
                  result_type="dict", callback=callback)
C.schedule_run(callback, time_point, repeat_times, interval, name)
C.cancel_schedule_run(task_id_or_name)
```

打板模型启动时先订阅 `l2quote`；`l2order` 在 09:29:50 通过一次性 `schedule_run` 建立，盘中启动立即建立，收盘后启动预约下一日。这样竞价期间不会把策略不使用的逐笔委托回放压入单线程回调队列。

一次性任务使用 `repeat_times=0`，持续任务使用 `repeat_times=-1`。定时器回调显式接收 `ContextInfo`，闭包只捕获稳定的股票代码、订单号或提交令牌，防止旧任务误操作新订单。停止模型时必须取消所有定时器和订阅。

## 交易接口

账号注册使用 `C.set_account(account_id, account_type)`。完整下单形式为：

```python
passorder(23, ORDER_TYPE_BUY, ACCOUNT_ID, code,
          PRICE_TYPE_LIMIT, price, quantity,
          STRATEGY_NAME, 2, user_order_id, C)
```

`user_order_id` is the investment remark (`userOrderId`) used to associate order or deal callbacks; pass `ContextInfo` immediately after it.

大单路径使用 `l2order.volume` 的股数单位；9000 手按 100 股/手换算为 900000 股，触发条件为严格大于 900000 股或委托金额严格大于 500 万元。委托金额优先取 `amount`，缺失时按 `price * volume` 计算。`l2quote.bid1_volume` 才是手数，封单金额按 `bid1 * bid1_volume * 100` 计算。该单位映射与已实盘验证的原 Python 策略一致。

订阅回调中的即时委托使用 `quickTrade=2`。按官网问答说明，`passorder` 调用后立即发出委托并返回，不等待委托回报，也不阻塞 Python 线程；验证计时因此可在调用前启动。模型仍按单线程顺序处理回调，后续 L2 回调在当前信号回调返回后进入策略处理。回报回调签名为 `order_callback(ContextInfo, orderInfo)` 和 `deal_callback(ContextInfo, dealInfo)`；撤单使用 `cancel(orderId, accountId, accountType, ContextInfo)`。`passorder` 返回 0 且没有委托号时保持待报，以 100ms 间隔最多查询 10 次 `get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, "order")`。查单和回报仅按唯一备注令牌，或包含令牌的策略名，关联订单；不按账户最新委托、同代码或相似价格数量盲目关联。查单结束仍无唯一结果时保留待撤状态，等待后续回报，不自动重发。这些接口的字段名可能随 QMT 版本不同，仍需以当前券商版本的实际回报为准。

订单和成交回调需要分别确认 `m_strOrderSysID` / `order_sysid`、证券代码及市场、订单累计成交量和成交回报单笔数量。回调可能先于订单号查询到达；订单号未知时，仅使用唯一的备注令牌或策略名包含令牌进行关联。成交回报用 `m_nVolume` 等单笔成交量累计，并用 `m_nVolumeTraded` 作为订单侧累计量交叉核验。

## 编码、路径和日志

`QMT_LARGE_ORDER_LIMIT_UP_BUY.py` 保持 `# coding: gbk`，以兼容现有 QMT 原生 Python 编辑器。股票池用 UTF-8-SIG CSV 读取；日志和原始 JSONL 使用脚本运行目录、当前工作目录及 QMT 根目录逐级查找可用路径。实盘验证时应记录实际生成的文件路径，不要凭默认路径推断“没有回调”。原累计首封、单笔大单首封和两种回封入口在下单决策及本地前置检查通过后、调用 `passorder` 前开始观察后续涨停买入委托；接口拒单或调用抛出异常时停止该轮验证。150 笔内出现 2 笔不低于 150 万元即通过，否则满 150 笔或 1 秒窗口到期即撤单。触发事件和事件时间不晚于下单决策时刻的积压事件不计入；撤单后补充追随不再启动第二轮验证。提交日志记录触发行情延时和 `passorder` 调用耗时。下单前另保留每股最近 200 条涨停价买入 `l2order`，只纳入最近 2 秒且事件时间不晚于下单时刻的记录；合格大单严格指涨停价买入方向且单笔金额不低于 150 万元的委托。其中已有 2 笔合格大单时写入 `FOLLOW_VERIFY_PREPASS`，不再因下单后窗口不足而撤单。`l2quoteaux`、`l2transactioncount` 和 `l2orderqueue` 仍只作为独立诊断数据，不替代逐笔 `l2order`。

`QMT_L2_FOLLOW_PROBE.py` 是 603278.SH 的一次性实盘探针，固定 100 股，使用账号 `32011276`。只有最新价和买一价均在涨停价、买一队列非空、报价不超过 1 秒且 `l2order` 订阅和 1 秒验证计时器都成功时才提交。若 `get_full_tick` 快照已过期，先订阅 `l2quote`，最多等待 5 秒；只有收到新鲜且仍封板的报价才提交，否则不下单。验证沿用主策略的 150 笔、2 笔单笔金额至少 150 万元规则；失败时撤单，意外通过时也会以探针清理为由撤单。委托回报只按唯一备注令牌匹配；每天只保留一次提交预约。任何撤单前成交的数量仍会形成持仓，探针不会自动卖出。

## 验证重点

订阅接口返回成功只代表订阅请求被接受，不代表已经收到行情。交易时段应分别确认 `l2quote`、`l2order` 的首条回调，原始 JSONL 的持续增长，以及回调时间与 QMT 主日志时间的关系。大单策略不再订阅普通 tick；`get_full_tick` 仍只用于启动快照。盘后出现“无法连接行情服务”不能单独证明脚本写法错误。

调试阶段可从控制台或 `qmt_strategy_YYYYMMDD.log` 查找 `[运行]`、`[股票池]`、`[订阅]`、`[回调首条]` 和 `[汇总]`。`[运行] subscriptions=...` 只能证明订阅调用返回了正数；两个周期都出现 `[回调首条]` 且汇总计数持续增加，才证明实际行情回调正常。L2 JSONL 的 `received_time` 和 `event_time` 均为 `HH:MM:SS.mmm`，无法用于决策的时间记录保留 `latency_ms: null`；原始行情保存在 `data` 字段。不应在策略回调中做同步等待。

当前调试脚本对 `l2quote` 超过 2000ms 的事件时间延时输出 `[延时警告]`，同一周期最多每 10 秒输出一次；`l2order` 仍使用 1000ms 告警阈值。汇总的 `延时=` 字段分别统计 `l2quote`、`l2order` 的有效样本/总样本、对应告警阈值的超时次数、平均值、最大值和最近值，并附带逐股票聚合的回调间隔均值/最大值。计算延时优先用 QMT `time` 字段，交易时段状态仍看 `stime`。延时只对有绝对日期且具备亚秒精度的事件时间计算，结果依赖 QMT 行情时钟与运行主机时钟同步，负延时或无效时间戳会排除在统计之外。

大单策略使用 `l2quote` 处理最新价和涨停盘口状态，不再生成 tick 与 `l2quote` 的首封比较日志。若需再次对比两路行情，应在独立探针中订阅并测量，避免增加生产策略的回调负载。

策略可以盘中启动。若错过 09:25 竞价窗口，不应把盘中快照当作竞价结果；连续竞价中第一条有效 `l2quote` 若已在涨停价，模型从 `WAIT_REOPEN` 开始观察，等待启动后可验证的开板和回封。
