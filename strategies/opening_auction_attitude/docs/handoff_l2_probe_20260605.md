# 早盘竞价 Level2 探针交接文档

日期：2026-06-05

## 1. 当前目标

当前优先任务是先解决 `OpeningAuctionL2Probe`：

```text
记录从集合竞价开始到开盘后 5 分钟的 Level2 数据情况，
为后续“早盘竞价主力态度策略”提供真实数据基础。
```

现阶段只做数据采集、覆盖验证、字段留痕和后续分析准备，不做交易。

## 2. 已完成内容

### 2.1 新增探针脚本

文件：

```text
strategies/opening_auction_attitude/scripts/probe_l2.py
```

作用：

```text
订阅指定股票的 l2quote / l2order / l2transaction / l2orderqueue
从 09:15:00 开始采集
到 09:35:00 停止
原样记录 Level2 事件
生成汇总 CSV 和字段 schema
```

默认采集窗口：

```text
capture_start = 09:15:00
capture_end   = 09:35:00
```

重点分析窗口：

```text
集合竞价全段：09:15:00 - 09:25:00
最后 10 秒：  09:24:50 - 09:25:00
开盘 5 分钟： 09:30:00 - 09:35:00
```

### 2.2 新增一键运行 bat

文件：

```text
strategies/opening_auction_attitude/scripts/run_l2_probe.bat
```

作用：

```text
直接双击或 PowerShell 执行即可启动探针
默认读取当前股票池
自动生成输出目录
自动生成日志文件
运行结束后打印日志尾部
```

bat 当前使用的 Python：

```text
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
```

原因：

```text
系统 python 指向 WindowsApps stub，不能正常执行。
QMT 自带 pythonw.exe 是 Python 3.6，不能运行当前项目的 Python 3.10+ 代码。
cytrade311 环境可用，并且已包含 xtquant。
```

### 2.3 输出目录已忽略

`.gitignore` 已新增：

```text
data/probe/
```

原因：

```text
Level2 原始采集文件可能很大，不应该提交 Git。
```

## 3. 输出文件说明

默认输出目录：

```text
data/probe/opening_auction_l2/YYYYMMDD_HHMMSS/
```

每次运行生成三个文件：

```text
opening_l2_raw.jsonl
opening_l2_summary.csv
opening_l2_schema.json
```

### 3.1 opening_l2_raw.jsonl

一行一条 Level2 事件，用于后续复盘和重新计算。

主要字段：

```text
recv_time              程序收到事件的时间
event_time             行情事件时间
stock                  股票代码
kind                   l2quote / l2order / l2transaction / l2orderqueue
subscribe_mode         early / delayed / unknown
in_capture_window      是否在 09:15:00-09:35:00
in_auction             是否在 09:15:00-09:25:00
in_final_10s           是否在 09:24:50-09:25:00
in_open_5m             是否在 09:30:00-09:35:00
phase                  所属阶段
normalized             项目标准化后的字段
raw                    xtdata 原始字段
```

`phase` 当前可能值：

```text
auction_before_final_10s
auction_final_10s
pre_open_gap
open_first_5m
capture_window_other
outside_capture_window
unknown
```

### 3.2 opening_l2_summary.csv

按股票和订阅模式聚合，方便快速判断数据覆盖情况。

关键字段：

```text
has_l2_capture
has_l2_auction
has_l2_2450_2500
has_l2_open_5m
```

每类 L2 都有分段计数：

```text
l2quote_count_total
l2quote_count_capture
l2quote_count_auction
l2quote_count_10s
l2quote_count_open_5m

l2order_count_total
l2order_count_capture
l2order_count_auction
l2order_count_10s
l2order_count_open_5m

l2transaction_count_total
l2transaction_count_capture
l2transaction_count_auction
l2transaction_count_10s
l2transaction_count_open_5m

l2orderqueue_count_total
l2orderqueue_count_capture
l2orderqueue_count_auction
l2orderqueue_count_10s
l2orderqueue_count_open_5m
```

最后 10 秒大单相关字段仍保留：

```text
big_trade_amount_10w
big_trade_amount_30w
big_trade_amount_50w
big_trade_amount_100w
big_trade_amount_300w
big_buy_amount_10s
big_sell_amount_10s
big_trade_imbalance_10s
big_buy_order_amount_10s
big_sell_order_amount_10s
big_order_imbalance_10s
cancel_buy_order_amount_10s
cancel_sell_order_amount_10s
```

### 3.3 opening_l2_schema.json

记录每种 L2 数据里实际出现过哪些原始字段，以及出现次数。

用途：

```text
确认集合竞价阶段和开盘阶段的字段是否稳定
确认 l2order / l2transaction / l2orderqueue 是否有真实数据
后续再决定如何解释 tradeFlag / entrustDirection 等字段
```

## 4. 如何运行

### 4.1 默认一键运行

直接运行：

```powershell
C:\Users\ysun\workspace\cytrade\strategies\opening_auction_attitude\scripts\run_l2_probe.bat
```

默认读取：

```text
data/stock_pools/current/main_seal_follow_pool.csv
```

默认行为：

```text
09:15:00 订阅 Level2
09:35:00 停止
```

### 4.2 指定股票运行

```powershell
C:\Users\ysun\workspace\cytrade\strategies\opening_auction_attitude\scripts\run_l2_probe.bat --early-codes 000001,600000
```

### 4.3 立即 smoke test

不等待交易时间，只验证程序能否启动、连接 xtdata、订阅并生成文件：

```powershell
C:\Users\ysun\workspace\cytrade\strategies\opening_auction_attitude\scripts\run_l2_probe.bat --early-codes 000001 --immediate --seconds 0
```

## 5. 已验证结果

### 5.1 单元测试

使用命令：

```powershell
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe -m pytest tests\test_opening_auction_l2_probe.py -q
```

结果：

```text
3 passed
```

### 5.2 bat smoke test

使用命令：

```powershell
cmd /d /c "echo. | C:\Users\ysun\workspace\cytrade\strategies\opening_auction_attitude\scripts\run_l2_probe.bat --early-codes 000001 --immediate --seconds 0"
```

结果：

```text
连接 xtdata 成功
订阅 l2quote / l2order / l2transaction / l2orderqueue 成功
生成 opening_l2_raw.jsonl
生成 opening_l2_summary.csv
生成 opening_l2_schema.json
退出码 rc=0
```

示例输出目录：

```text
data/probe/opening_auction_l2/20260605_072020/
```

## 6. 重要注意事项

### 6.1 不要用系统 python

当前 `python` 指向：

```text
C:\Users\ysun\AppData\Local\Microsoft\WindowsApps\python.exe
```

这个是 WindowsApps stub，不能用于项目运行。

### 6.2 不要用 QMT 自带 pythonw.exe 跑项目脚本

QMT 自带：

```text
C:\光大证券金阳光QMT实盘\bin.x64\pythonw.exe
```

它是 Python 3.6，当前项目代码使用 Python 3.10+ 语法，不能兼容。

### 6.3 当前有效 Python

```text
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
```

bat 已经默认使用这个环境。

### 6.4 输出数据不要提交 Git

以下目录是运行数据：

```text
data/probe/
logs/opening_auction_l2/
```

不应提交。

## 7. 当前工作树状态提醒

当前相关新增/修改文件包括：

```text
.gitignore
strategies/opening_auction_attitude/scripts/probe_l2.py
strategies/opening_auction_attitude/scripts/run_l2_probe.bat
strategies/opening_auction_attitude/tests/test_l2_probe.py
strategies/opening_auction_attitude/docs/handoff_l2_probe_20260605.md
```

工作树里还存在其他未跟踪项，需要新会话继续判断是否提交：

```text
data/db/
strategies/opening_auction_attitude/docs/strategy_v1.md
docs/启动简易说明.txt
```

其中：

```text
data/db/ 是本地运行数据库，不建议提交。
strategies/opening_auction_attitude/docs/strategy_v1.md 是策略方案文档。
docs/启动简易说明.txt 是中文启动说明，之前确认不是乱码。
```

## 8. 下一步建议

### 8.1 交易时间实测

在交易日 `09:14` 左右启动 bat，确保 `09:15:00` 前程序已经运行。

重点看：

```text
opening_l2_summary.csv
```

确认：

```text
has_l2_auction 是否为 True
has_l2_2450_2500 是否为 True
has_l2_open_5m 是否为 True
l2order_count_10s 是否有数据
l2transaction_count_10s 是否有数据
l2orderqueue_count_10s 是否有数据
```

### 8.2 分析 raw 数据

如果有数据，下一步应写分析脚本读取：

```text
opening_l2_raw.jsonl
```

输出：

```text
竞价阶段各类 L2 数据覆盖情况
最后 10 秒是否有 l2order / l2transaction
开盘 5 分钟 l2transaction 是否稳定
tradeFlag / entrustDirection / cancel 字段实际分布
```

### 8.3 再决定策略实现

只有确认数据稳定后，再进入：

```text
AuctionSpeedScanner
OpeningAuctionAttitudeStrategy
09:30 后真假抢筹验证
```

当前不要直接写交易逻辑。
