# QMT 原生 Python 注意事项

## 入口和线程模型

原生模型由 QMT 调用 `init(ContextInfo)`、`handlebar(ContextInfo)` 和 `stop(ContextInfo)`。行情、订单和成交回调在 QMT 的模型调度上下文中串行到达；脚本不能假设可以创建后台线程，也不能在回调里等待外部结果。

因此当前脚本：

- 不导入 `threading`，不使用线程池、Future 或 `sleep`。
- 不在回调中做不可控的网络、磁盘批量写入或长时间重试。
- 把需要稍后完成的工作拆成有限片段，由 `schedule_run` 或下一次 `handlebar` 继续执行。
- `_NoopLock` 只提供统一的上下文写法，不等待、不产生并发互斥；它不是线程安全机制。

## 回调参数和状态

QMT 回调传入的数据对象可能是复用对象，并具有深拷贝和回滚语义。回调结束后不能依赖对参数对象的修改。当前实现通过 `_owned_payload()` 深拷贝数据，再放入 `_INSTANCES`、`_RAW_BUFFER` 等脚本自有容器。跨回调字段必须从这些容器读取。

不要把以下状态写回 `ContextInfo` 或行情回调参数：

- 封板开始时间、封单峰值和大单去重集合。
- 验证笔数、验证定时器、撤单状态和跟随下单标记。
- 订单号查询重试次数、原始数据缓冲区和统计计数器。

## 订阅和定时器

当前使用的原生调用形式是：

```python
C.subscribe_quote(code, period=period, dividend_type="follow",
                  result_type="dict", callback=callback)
C.schedule_run(callback, time_point, repeat_times, interval, name)
C.cancel_schedule_run(task_id_or_name)
```

一次性任务使用 `repeat_times=0`，持续任务使用 `repeat_times=-1`。定时器回调显式接收 `ContextInfo`，闭包只捕获稳定的股票代码、订单号或提交令牌，防止旧任务误操作新订单。停止模型时必须取消所有定时器和订阅。

## 交易接口

账号注册使用 `C.set_account(account_id, account_type)`。完整下单形式为：

```python
passorder(23, ORDER_TYPE_BUY, ACCOUNT_ID, code,
          PRICE_TYPE_LIMIT, price, quantity,
          STRATEGY_NAME, 1, user_order_id, {}, C)
```

回报回调签名为 `order_callback(ContextInfo, orderInfo)` 和 `deal_callback(ContextInfo, dealInfo)`。订单号异步查询使用 `get_last_order_id(accountID, strAccountType, strDatatype, strategyName)`；撤单使用 `cancel(orderId, accountId, accountType, ContextInfo)`。这些接口的字段名可能随 QMT 版本不同，脚本对常见字典键和对象属性做了兼容读取，但仍需以当前券商版本的实际回调为准。

## 编码、路径和日志

`QMTTEST.py` 保持 `# coding: gbk`，以兼容现有 QMT 原生 Python 编辑器。股票池用 UTF-8-SIG CSV 读取；日志和原始 JSONL 使用脚本运行目录、当前工作目录及 QMT 根目录逐级查找可用路径。实盘验证时应记录实际生成的文件路径，不要凭默认路径推断“没有回调”。

## 验证重点

订阅接口返回成功只代表订阅请求被接受，不代表已经收到行情。交易时段应分别确认：普通 tick、`l2quote`、`l2order` 的首条回调，原始 JSONL 的持续增长，以及回调时间与 QMT 主日志时间的关系。盘后出现“无法连接行情服务”不能单独证明脚本写法错误。

