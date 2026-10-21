# 大 QMT 内置 Python 策略编写指南

本文整理 ThinkTrader 内置 API 文档与本机大 QMT 实测经验，供后续策略重写、维护和现场排查使用。内容分为官网约定、本机已观察行为和仍需实机验证三类；券商版本更新后应重新核对接口。

## 官方资料

- [内置 API 快速开始](https://dict.thinktrader.net/innerApi/start_now.html)：内置模型入口、调用方式和下单示例。
- [内置 API 代码示例](https://dict.thinktrader.net/innerApi/code_examples.html)：行情订阅、L2 周期、回调、定时任务和取消订阅。
- [内置 API 常见问题](https://dict.thinktrader.net/innerApi/question_answer.html)：`ContextInfo`、内置模型对象及下单和回报说明。

实现具体 API 时，先读官网对应章节，再查看当前券商 QMT 安装目录中的函数封装、常量和实际回报。不要把独立 XtQuant 客户端接口直接当作内置模型接口。

## 编写原则

### 运行与状态

- 模型入口按内置运行器组织：`init(ContextInfo)`、`handlebar(ContextInfo)`、`stop(ContextInfo)`。
- 按单线程事件循环设计。回调里不创建线程/线程池，不等待 `Future`，不调用 `sleep`，不做阻塞等待或长时间重试。
- 把批量工作拆成有限片段，由 `schedule_run` 或后续模型回调继续执行，并在 `stop` 取消定时任务。
- 回调参数不承载跨回调状态。回调参数可能有深拷贝或回滚语义；持续状态放入脚本全局字典、集合或其他自有容器。保留行情数据前先复制 payload。
- `handlebar` 可能遍历历史 K 线。检查当前运行模式和最新 bar 语义，避免历史数据触发实盘行为。

### 行情与数据量

- 只订阅策略实际需要的周期。大单买入模型以 `l2quote` 处理最新报价和涨停盘口，以 `l2order` 识别委托流信号，不订阅 tick；Juejin 卖出模型另用 `tick` 辅助普通行情并订阅 `l2quote` 计算卖出信号。其他 L2 周期只在诊断需要时订阅。
- 订阅返回正数只说明请求被登记，不代表行情已到达。还要确认首条回调、持续计数和 QMT 主日志。
- 延时分析分开记录事件时间、会话时间和本机接收时间。只有时间精度足够且主机时钟同步时才计算延时。
- 若需要比较 tick 与 l2quote 的涨停首报，应在独立探针中按股票和同一观察窗口配对。策略启动后首次收到不等于交易所当天首次；积累多个交易日样本再判断稳定性。
- 高频回调尽量只做新鲜度过滤、轻量复制、计数和状态更新。不要在回调中递归 `deepcopy`、JSON 序列化或批量写盘；原始数据放入有界缓冲区，由 `schedule_run` 按固定上限分片刷新。

### 订单与持仓

- 使用内置 Python 文档中的 `passorder` 调用形式，并按当前 QMT 版本核对参数顺序。当前项目在订阅或定时回调内即时委托使用 11 参数形式：`userOrderId` 后面紧跟 `ContextInfo`，`quickTrade=2`。
- 下单返回被接受不等于已获得订单号，更不等于成交。等待可唯一匹配的委托或成交回报；不能用账户最近委托号盲绑股票。
- 分开管理委托累计成交量和成交回报单笔量；通过成交 ID 去重，并对账避免重复计数。
- 未知、歧义或未收到回报时保留待确认状态，避免自动重发造成重复委托。
- 持仓查询上下文应按本机 QMT 版本实测。2026-10-19 的本机探针证明 `get_trade_detail_data(account, "stock", "POSITION")` 在 `init` 和 `handlebar` 返回正常，在 tick 订阅回调报 `request_id` 异常；定时器中的查询尚未验证。数据失败或过期时应阻止依赖持仓的实盘提交。

## 本机部署约定

- 当前 rewrite 中的打板与卖出策略默认 `ENABLE_LIVE_ORDERS = True`、`ACCOUNT_ID = "32011276"`。这是用户指定的部署默认值，不是 QMT 通用默认值；测试必须覆盖配置，避免触发真实交易路径。
- 打板入口编码为 GBK，池文件按 UTF-8-SIG 读取。其他模型按其 QMT 编辑器要求确认编码。
- 原生模型可能没有 `__file__`。路径解析应提供当前工作目录和 QMT 安装目录回退，并在启动日志中打印最终路径。
- 更新已部署策略时覆盖对应 QMT Python 文件并核对 SHA-256。当前映射为：
  - `large_order_limit_up_buy/QMT_LARGE_ORDER_LIMIT_UP_BUY.py` -> `C:\光大证券金阳光QMT实盘\python\QMT_LARGE_ORDER_LIMIT_UP_BUY.py`
  - `juejin_sell_strategy/QMT_JUEJIN_SELL.py` -> `C:\光大证券金阳光QMT实盘\python\JUEJIN_SELL.py`
- QMT 正在运行的模型需要重新编译或启动才能加载覆盖后的文件。同步前先确认脚本覆盖的目标文件。

## 编码前后检查

1. 阅读设计文档，逐条记录必须保留的交易规则、当前实现和待验证差异。
2. 阅读官方对应 API 章节，检查本机安装包的封装和常量；把官方规范、本机观测、推测分开记录。
3. 将规则实现为明确状态转移，状态放在脚本自有容器中；定义订单、撤单、部分成交、超时和重启行为。
4. 通过 QMT 支持的 Python 版本进行语法和目标测试；在 QMT 空跑确认入口、路径和订阅，交易时段确认回调和延迟。
5. 如果变更已部署策略，覆盖对应的 QMT 文件并核对哈希；记录是否需要重启模型。
6. 不把离线测试结果写成实盘委托验证结论。记录 QMT 版本、日志路径、验证时间和仍未覆盖的执行上下文。

完整的本项目重写记录见 [REWRITE_PLAYBOOK.md](REWRITE_PLAYBOOK.md)，当前接口注意事项见 [large_order_limit_up_buy/QMT_NATIVE_NOTES.md](large_order_limit_up_buy/QMT_NATIVE_NOTES.md)。
