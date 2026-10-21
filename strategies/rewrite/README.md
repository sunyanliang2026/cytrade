# 大 QMT 原生 Python 重写

这个目录保存“大 QMT 内置原生 Python”版本的大单打板模型及其迭代记录。模型不依赖 `qmtmini`、`xtdata` 或外部 Python 进程，入口由 QMT 模型交易环境调用：

隔夜涨停买入模型和 Juejin 卖出模型作为独立子项目保存，不会覆盖本目录的大单策略模型。

```text
init(ContextInfo)
handlebar(ContextInfo)
stop(ContextInfo)
```

## 文件

- `large_order_limit_up_buy/QMT_LARGE_ORDER_LIMIT_UP_BUY.py`：可复制到 QMT 原生 Python 目录运行的当前脚本，编码保持 `gbk`。
- `juejin_sell_strategy/`：Juejin 卖出策略原生模型、规则说明和离线测试。
- `overnight_limit_up_buy/`：独立的隔夜涨停买入原生模型。
- `large_order_limit_up_buy/DESIGN.md`：设计文档到代码状态机和事件处理的映射，以及当前差异。
- `large_order_limit_up_buy/QMT_NATIVE_NOTES.md`：原生模型的线程、回调、订阅、定时器和交易接口约束。
- `CHANGELOG.md`：每次重写和后续迭代的记录入口。
- `QMT_NATIVE_GUIDE.md`：从官网内置 API 文档和本机实测总结的策略编写指南。
- `large_order_limit_up_buy/data/manual_pool.csv.example`：股票池格式示例。

## 安全边界

- 打板和卖出模型默认 `ENABLE_LIVE_ORDERS = True`，`ACCOUNT_ID = "32011276"`；满足信号条件时会尝试真实下单或撤单。
- 本目录不会覆盖仓库内其他未提交文件，也不修改 QMT 账户、路径或交易配置。
- 使用实盘前必须由人工审阅代码、配置和当天日志，并单独确认开启实盘开关。

## 部署和运行

1. 在 QMT 的“模型研究”中创建 Python 策略，勾选“原生 Python”。
2. 将 `large_order_limit_up_buy/QMT_LARGE_ORDER_LIMIT_UP_BUY.py` 部署到：
   `C:\光大证券金阳光QMT实盘\python\QMT_LARGE_ORDER_LIMIT_UP_BUY.py`
   Juejin 卖出模型使用独立的原生 Python 模型，将 `juejin_sell_strategy/QMT_JUEJIN_SELL.py` 部署为同一目录下的 `JUEJIN_SELL.py`。
3. 将股票池放到脚本可搜索的 `data\manual_pool.csv`，格式见示例。代码可写六位数字，脚本会补充市场后缀。
4. 在交易时段点击编译并运行。启动前确认 QMT 行情服务已登录且处于可用状态。
5. 观察 QMT 用户数据目录下的策略日志和原始 JSONL 日志。日志通常位于：
   `C:\光大证券金阳光QMT实盘\userdata\log\`
   如果 QMT 原生执行环境没有 `__file__` 且当前工作目录是 `bin.x64`，实际路径会回退为：
   `C:\光大证券金阳光QMT实盘\bin.x64\userdata\log\`

大单模型默认只订阅普通 `tick`、`l2quote` 和 `l2order`，使用 `schedule_run` 分片刷新原始数据，避免在回调中阻塞写盘。状态、计数器和跨回调数据都保存在脚本全局容器中。`l2quoteaux`、`l2transaction`、`l2transactioncount` 和 `l2orderqueue` 属于独立诊断数据，不在策略默认订阅中。Juejin 卖出模型单独订阅 `tick` 和 `l2quote`，不复用大单买入状态机。

当前处于交易时段验证阶段，控制台和策略日志会保留诊断信息：启动时打印脚本根目录、当前工作目录、`__file__` 是否存在、实际日志路径、股票池路径和订阅周期；每项订阅打印返回的订阅号；`tick`、`l2quote`、`l2order` 各自收到首条回调时打印代码、行情事件时间和接收时间；`tick`、`l2quote` 延时超过 2 秒，或 `l2order` 延时超过 1 秒时按周期限频提示；每 60 秒打印回调计数、订阅成功/失败、原始缓冲区、最近事件时间，以及各周期按阈值统计的有效样本数、超限次数、均值/最大值、按股票统计的回调间隔均值/最大值，可直接比较 `tick` 与 `l2quote`。延时优先使用 QMT 的 `time` 时间戳，时段判断使用 `stime`；数据依赖行情时间戳与本机时钟同步，缺失、无毫秒精度、不可解析或落后于接收时钟的样本不参与延时统计。确认连续交易日运行稳定后，再把 `DEBUG_SUMMARY_INTERVAL_SECONDS` 调大或关闭逐项订阅日志。

新增的 `[LIMIT_UP_FIRST]` 和 `[LIMIT_UP_COMPARE]` 只比较策略启动订阅后，每只股票首次在连续竞价回调中观察到 `bid1 == 涨停价` 的本机接收时刻；配对结果显示先到周期和相差毫秒数。`[涨停首报汇总]` 汇总两路覆盖数、配对数、先到次数、差值范围和未配对数。若启动前已经触板，这表示启动后首次观察到的涨停报价，不代表交易所当天的首次触板；至少积累多个交易日和多只股票的配对样本，再评估哪路更稳定。

策略可以在交易日盘中任意时间启动。09:25 竞价快照只在时间窗口内可用；连续竞价中启动时，已处于涨停价的股票进入等待回封状态，避免把既有封板误判成首封。

## 迭代方式

每次修改按以下顺序记录：

1. 在 `CHANGELOG.md` 增加日期、问题证据、改动和验证结果。
2. 同步更新 `large_order_limit_up_buy/DESIGN.md` 或 `large_order_limit_up_buy/QMT_NATIVE_NOTES.md` 中受影响的约束。
3. 修改 `large_order_limit_up_buy/QMT_LARGE_ORDER_LIMIT_UP_BUY.py` 后先运行离线语法检查，再按原生入口验证。
4. 每次修改打板或卖出策略并通过本地检查后，运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\sync_to_qmt.ps1 -QmtPythonDir "C:\光大证券金阳光QMT实盘\python"`，将两个入口文件覆盖同步到 QMT `python` 目录，并核对源与目标的 SHA-256。运行中的 QMT 模型需要重新编译或启动才能加载新版本。
5. 只有在日志证据支持时调整策略行为；行情延迟问题优先增加观测，不直接改变阈值或仓位。
## Layout

The native models are kept as sibling projects:

```text
large_order_limit_up_buy/
juejin_sell_strategy/
overnight_limit_up_buy/
```

The large order model entry point is `large_order_limit_up_buy/QMT_LARGE_ORDER_LIMIT_UP_BUY.py`.
本次重写的通用流程和检查清单见 [`REWRITE_PLAYBOOK.md`](REWRITE_PLAYBOOK.md)。
