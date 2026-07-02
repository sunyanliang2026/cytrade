# 当前项目交接总结 2026-06-23

## 1. 项目当前主线

当前仓库是一个基于 QMT / xtquant 的交易系统。现阶段主线不是实盘交易，而是围绕早盘集合竞价做观察、采集、打分和复盘。

当前重点策略是：

```text
OpeningAuctionAttitude
```

目标是观察 9:15 到 9:25 集合竞价阶段，尤其是 9:24:30 之后到 9:25:05 的最后阶段，判断是否存在资金真实抬价、虚假抢筹、开盘后是否承接。

安全边界：

- 目前只做 dry-run / market-only。
- 不连接交易账户。
- 不发真实订单。
- 不应修改 `CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN` 为 false。
- 不应修改账号、QMT 路径、webhook secret、`.env`、`config/local_runtime.json`。

## 2. 已实现的核心流程

一键入口：

```bat
scripts\run\run_opening_auction_attitude_morning.bat
```

这个 bat 做 4 步：

```text
1. collect_main_seal_pool
   生成主封跟随股票池，并写入 source cache。

2. build_opening_auction_universe
   从当天 source cache 里把问财和韭研公社来源取并集，生成更大的竞价观察池。

3. probe_opening_auction_l2
   后台启动 Level2 原始数据采集，保存 9:15-9:35 的 L2 原始行情。

4. run_opening_auction_attitude_market_only
   前台运行动态 scanner，9:15-9:24:30 轮询快照，发现涨停价竞价股票后动态加入候选并订阅 tick/L2，9:35 输出决策。
```

默认 Python：

```text
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
```

## 3. 股票池逻辑现状

股票池相关代码：

```text
scripts\pool\collect_main_seal_pool.py
scripts\pool\build_opening_auction_universe.py
config\main_seal_pool_sources.json
```

当前规则：

- 问财只在 9:00 前采集，因为问财查询的是昨日数据。
- 韭研公社 8:30 后才尝试采集，因为最新文章一般 8:30 后出现。
- 如果 9:00 后重跑，当天不再重新采集问财，但可以继续采集韭研公社，并与已有问财 cache 合并。
- `main_seal_follow_pool.csv` 是主封跟随策略的最终小池。
- `opening_auction_universe.csv` 是开盘竞价观察用大池，来自当天 source cache 的所有问财/韭研股票并集。

最新一次股票池产物：

```text
data\stock_pools\current\main_seal_follow_pool.csv
data\stock_pools\current\opening_auction_universe.csv
data\stock_pools\runs\2026-06-16\072944\
data\stock_pools\source_cache\2026-06-16\
```

2026-06-16 当天结果：

```text
main_seal_follow_pool.csv: 10 只
opening_auction_universe.csv: 68 只
source_cache:
  iwencai.base_strong.csv: 58 只
  iwencai.limitup_direct.csv: 10 只
  jiuyangongshe.*: 0 只
```

注意：6 月 16 日韭研公社三个来源都是 0，所以当天主封池主要来自问财。

## 4. OpeningAuctionAttitude 当前实现

策略代码目录：

```text
strategy\opening_auction_attitude\
  __init__.py
  models.py
  score.py
  strategy.py
```

运行脚本：

```text
scripts\run\run_opening_auction_attitude_market_only.py
```

当前不是全市场订阅。实现方式是：

```text
已有大池 opening_auction_universe.csv
  -> 9:15-9:24:30 每 2 秒 get_full_tick 快照轮询
  -> 如果报价命中涨停价附近，加入候选
  -> 对候选动态安装 OpeningAuctionAttitudeStrategy
  -> 订阅候选 tick + Level2
  -> 9:24:30 冻结候选
  -> 9:35 输出 MSF_AUCTION_ATTITUDE 决策
```

关键运行参数：

```text
scan_start_time: 09:15:00
candidate_freeze_time: 09:24:30
snapshot_interval_sec: 2
limit_up_tolerance: 0.01
stop_time: 09:35:00
```

保留数据：

```text
snapshot_scan.jsonl
opening_l2_raw.jsonl
opening_l2_summary.csv
opening_l2_schema.json
probe_console.log
logs\system.<pid>.log
logs\trade.<pid>.log
```

## 5. 策略判断口径

用户已经确认的核心逻辑：

1. 候选股票来自 9:24:30 前曾经出现涨停价竞价的股票。
2. 从 9:24:30 开始重点观察候选。
3. 竞价结束时间允许到 9:25:05，因为实际行情可能出现 9:25:01 到 9:25:02 的记录。
4. 不只看价格，更重视量能和金额。
5. 重点看竞价阶段从价格低点到最终竞价结束价之间，累计匹配金额增加了多少。
6. `low_to_final_amount_ratio` 表示低点后新增匹配金额占最终匹配金额的比例。
7. 集合竞价撮合阶段匹配量理论上只能增加，因此金额差值可以用于衡量是否真金白银拉升。

当前标签：

```text
AUCTION_NO_SIGNAL
AUCTION_SPEED_ONLY
AUCTION_MONEY_LIFT
AUCTION_BIG_ORDER_CONFIRMED
AUCTION_BIG_TRADE_CONFIRMED
AUCTION_STRONG_CONFIRMED
AUCTION_FAKE_RISK
```

开盘验证路径：

```text
DIRECT_PULL
WASH_THEN_PULL
FAKE_BREAKDOWN
NO_FOLLOW_THROUGH
```

## 6. 最近运行情况

最新完整运行日期：

```text
2026-06-16
```

最新输出目录：

```text
data\probe\opening_auction_l2\20260616_072942
```

主要文件：

```text
opening_l2_raw.jsonl      约 518 MB
snapshot_scan.jsonl       约 10 MB
opening_l2_summary.csv
opening_l2_schema.json
probe_console.log
```

对应主策略日志：

```text
logs\system.7768.log
```

运行结果：

```text
session_start: 2026-06-16 07:29:54
scanner_start universe=68
candidate_freeze candidates=5 universe=68 installed=5
session_stop: 09:35:00 scheduled_stop
decisions_emitted=5
dry_run=True
real_order_sent=false
```

5 个候选：

```text
000032 深桑达A
001257 盛龙股份
002106 莱宝高科
002815 崇达技术
603267 鸿远电子
```

决策分布：

```text
AUCTION_NO_SIGNAL: 5
NO_FOLLOW_THROUGH: 5
```

L2 覆盖：

```text
opening_l2_summary.csv: 68 只
has_l2_2450_2500=True 且 has_l2_open_5m=True: 68 只
```

前一次较有参考价值的完整运行：

```text
2026-06-12
data\probe\opening_auction_l2\20260612_090810
logs\system.8484.log
universe=88
candidates=25
decisions_emitted=25
AUCTION_BIG_ORDER_CONFIRMED: 1
AUCTION_FAKE_RISK: 1
NO_SIGNAL: 23
```

2026-06-12 的强信号股票：

```text
002654 万润科技
label=AUCTION_BIG_ORDER_CONFIRMED
open_path=NO_FOLLOW_THROUGH
```

## 7. 已知问题

### 7.1 probe 停止后仍收到 L2 回调

6 月 12 日和 6 月 16 日都出现过：

```text
DataSubscription: _on_l2_order_data failed: I/O operation on closed file.
```

原因：

```text
probe 在 09:35 写出 summary 并 close raw handle 后，DataSubscriptionManager 仍可能收到少量延迟 L2 回调。
回调继续调用 recorder.record_event，导致写已关闭文件。
```

影响：

- 主策略决策已经正常产出。
- 原始数据文件和 summary 已生成。
- 但日志里会出现 ERROR，后续应修。

建议修复：

```text
OpeningAuctionL2Recorder 增加 closed 标记。
close() 内设置 closed=True。
record_many/record_event 如果 closed=True 直接 return。
或者停止订阅/清空 callback 后再 close recorder。
```

### 7.2 文档和配置在部分 PowerShell 输出中有编码显示问题

部分中文文档、JSON 配置在当前控制台显示为乱码，但代码运行使用 `utf-8-sig`/UTF-8 读写。后续如果要维护文档，建议统一检查并固定 UTF-8 编码。

### 7.3 当前打分仍是观察版

当前策略可以跑通、可以输出标签，但阈值还没有经过足够样本校准。尤其是：

- `low_to_final_amount_ratio` 在某些样本里容易为 1.0。
- 这是因为价格低点时累计匹配金额可能为 0 或很低。
- 这个指标现在更接近“低点后新增匹配金额 / 最终匹配金额”。
- 用户已接受这个定义，但后续需要更多真实样本验证阈值。

## 8. 后续建议推进顺序

优先级 1：修 probe 收尾错误。

```text
目标：09:35 停止后不再出现 I/O operation on closed file。
验证：跑 probe smoke test 和相关单测。
```

优先级 2：增加运行后汇总脚本。

```text
输入：logs\system.<pid>.log + data\probe\opening_auction_l2\<run_id>\
输出：候选列表、标签分布、L2 覆盖、异常摘要、Top 分数股票。
目的：每天早上跑完不用手动解析日志。
```

优先级 3：复盘 6 月 12 日和 6 月 16 日样本。

```text
重点看：
  snapshot_scan.jsonl 是否准确记录候选形成过程
  opening_l2_raw.jsonl 中竞价撮合金额是否符合预期
  low_to_final_amount_ratio / low_to_final_lift_pct 是否稳定
  AUCTION_BIG_ORDER_CONFIRMED 是否过严或过松
```

优先级 4：补充更多测试。

```text
重点覆盖：
  9:25:01-9:25:05 尾部容忍
  matched amount 单调增加
  低点后金额占比
  动态候选 freeze 后不再新增
  关闭 recorder 后忽略迟到回调
```

优先级 5：只在 dry-run 下考虑策略阈值调优。

```text
任何阈值变化都应先记录样本依据。
不要接入真实下单。
```

## 9. 常用命令

一键早盘运行：

```bat
scripts\run\run_opening_auction_attitude_morning.bat
```

只跑 market-only scanner：

```powershell
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe scripts\run\run_opening_auction_attitude_market_only.py --pool data\stock_pools\current\opening_auction_universe.csv --scan-start-time 09:15:00 --candidate-freeze-time 09:24:30 --snapshot-interval-sec 2 --stop-time 09:35:00 --heartbeat-interval-sec 10
```

构建开盘竞价大池：

```powershell
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe -m scripts.pool.build_opening_auction_universe --strict
```

相关测试：

```powershell
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe -m pytest tests\test_opening_auction_attitude_score.py tests\test_opening_auction_attitude_strategy.py tests\test_replay_opening_auction_attitude.py tests\test_run_opening_auction_attitude_market_only.py tests\test_build_opening_auction_universe.py tests\test_opening_auction_l2_probe.py
```

AGENTS.md 推荐的基础检查：

```powershell
python -m py_compile strategies/main_seal_follow/scripts/run_monitor_session.py strategies/main_seal_follow/scripts/run_market_only.py strategies/main_seal_follow/strategy.py
python -m py_compile agent/sensors/parse_monitor_logs.py agent/loops/post_morning_review.py agent/loops/generate_improvement_tasks.py agent/gates/quality_gate.py agent/tools/codex_cli_runner.py
python -m pytest tests/test_agent_monitor_review.py
python -m pytest tests/test_collect_main_seal_pool.py tests/test_import_iwencai_pool.py strategies/main_seal_follow/tests/test_run_main_seal_follow_monitor_session.py strategies/main_seal_follow/tests/test_main_seal_follow_strategy.py
```

注意：实际环境里直接 `python` 可能指向 WindowsApps stub，优先使用 cytrade311 的完整路径。

## 10. Git 状态备注

当前分支：

```text
main
```

最近与本主线相关的提交：

```text
8de156c Add opening auction dynamic scanner retention
d1b3278 Add opening auction attitude observe stack
005fdb8 Move main seal follow into strategy package
9c60a85 Respect source timing for main seal pool
8651783 Guard main seal pool collection timeout
```

当前工作区有未提交变更，主要是另一个策略方向：

```text
 M strategy/__init__.py
?? docs/启动简易说明.txt
?? docs/掘金/
?? strategies/juejin_sell_strategy/
?? strategies/juejin_sell_strategy/tests/test_juejin_sell_strategy.py
```

这些看起来不是 OpeningAuctionAttitude 主线的一部分。接手人处理开盘竞价策略时，不要误改或回滚这些未提交内容，除非用户明确要求。

