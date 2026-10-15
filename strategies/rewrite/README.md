# 大 QMT 原生 Python 重写

这个目录保存“大 QMT 内置原生 Python”版本的大单打板模型及其迭代记录。模型不依赖 `qmtmini`、`xtdata` 或外部 Python 进程，入口由 QMT 模型交易环境调用：

```text
init(ContextInfo)
handlebar(ContextInfo)
stop(ContextInfo)
```

## 文件

- `QMTTEST.py`：可复制到 QMT 原生 Python 目录运行的当前脚本，编码保持 `gbk`。
- `DESIGN.md`：设计文档到代码状态机和事件处理的映射，以及当前差异。
- `QMT_NATIVE_NOTES.md`：原生模型的线程、回调、订阅、定时器和交易接口约束。
- `CHANGELOG.md`：每次重写和后续迭代的记录入口。
- `data/manual_pool.csv.example`：股票池格式示例。

## 安全边界

- `ENABLE_LIVE_ORDERS = False` 是默认值，默认不会调用真实下单或撤单路径。
- `ACCOUNT_ID` 默认为空；没有明确配置前不会提交实盘订单。
- 本目录不会覆盖仓库内其他未提交文件，也不修改 QMT 账户、路径或交易配置。
- 使用实盘前必须由人工审阅代码、配置和当天日志，并单独确认开启实盘开关。

## 部署和运行

1. 在 QMT 的“模型研究”中创建 Python 策略，勾选“原生 Python”。
2. 将 `QMTTEST.py` 部署到：
   `C:\光大证券金阳光QMT实盘\python\QMTTEST.py`
3. 将股票池放到脚本可搜索的 `data\manual_pool.csv`，格式见示例。代码可写六位数字，脚本会补充市场后缀。
4. 在交易时段点击编译并运行。启动前确认 QMT 行情服务已登录且处于可用状态。
5. 观察 QMT 用户数据目录下的策略日志和原始 JSONL 日志。日志通常位于：
   `C:\光大证券金阳光QMT实盘\userdata\log\`
   如果 QMT 原生执行环境没有 `__file__` 且当前工作目录是 `bin.x64`，实际路径会回退为：
   `C:\光大证券金阳光QMT实盘\bin.x64\userdata\log\`

模型默认只订阅普通 `tick`、`l2quote` 和 `l2order`，使用 `schedule_run` 分片刷新原始数据，避免在回调中阻塞写盘。状态、计数器和跨回调数据都保存在脚本全局容器中。`l2quoteaux`、`l2transaction`、`l2transactioncount` 和 `l2orderqueue` 属于独立诊断数据，不在策略默认订阅中。

策略可以在交易日盘中任意时间启动。09:25 竞价快照只在时间窗口内可用；连续竞价中启动时，已处于涨停价的股票进入等待回封状态，避免把既有封板误判成首封。

## 迭代方式

每次修改按以下顺序记录：

1. 在 `CHANGELOG.md` 增加日期、问题证据、改动和验证结果。
2. 同步更新 `DESIGN.md` 或 `QMT_NATIVE_NOTES.md` 中受影响的约束。
3. 修改 `QMTTEST.py` 后先运行离线语法检查，再按原生入口验证。
4. 只有在日志证据支持时调整策略行为；行情延迟问题优先增加观测，不直接改变阈值或仓位。
