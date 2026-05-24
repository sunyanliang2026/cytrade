# 主封单跟随策略使用说明

## 股票池

完整股票池生成逻辑见：`docs/STOCK_POOL_LOGIC.md`。

默认股票池文件：

```text
config/main_seal_follow_pool.csv
```

CSV 表头：

```csv
股票代码,名称,计划买入金额
001259,利仁科技,10000
600604,市北高新,10000
```

- 股票代码：支持 `001259`、`001259.SZ`、`600604`、`600604.SH`，策略内部会归一成 6 位代码。
- 名称：只用于日志和诊断，不参与交易判断。
- 计划买入金额：每只股票本次计划买入的总金额，支持纯数字，也支持 `2万` 这种写法。
- 金额小于等于 0 的行会被跳过，可用于保留样例但不启用。

## 股票池自动生成

默认通过总入口汇总多个来源生成股票池。当前默认 `combined` 来源会按 `base/direct/gated` 类型调用问财，并叠加韭研公社，最后统一去重、主板过滤、非 ST 过滤：

```powershell
set IWENCAI_COOKIE=你的问财登录cookie
python scripts\collect_main_seal_pool.py --source combined --schedule-time 08:45 --amount 1000
```

来源参数从统一配置 `config/main_seal_pool_sources.json` 读取，其中包含问财条件和韭研公社参数：

当前配置采用“命名结果集 + 集合表达式”：

- `sets`：每条问财查询、每个韭研公社节点都会生成一个可引用结果集。
- `final.union`：定义最终股票池的并集组成。
- `final.intersect`：可以对任意两个或多个结果集取交集。

- `base`：初步筛选池，只做准入门槛。
- `direct`：一次性结果筛选池，直接进入最终股票池。
- `gated`：候选筛选池，必须同时命中 `base` 才进入最终股票池。

韭研公社候选也按 `gated` 处理，必须同时命中 `base`。单独调试来源脚本：

韭研公社自动取最新文章时默认要求文章日期等于当天，避免误用旧文章；手工传 `--article-url` 用于历史文章测试时不做这个自动最新日期保护。

```powershell
python scripts\collect_iwencai_pool.py
python scripts\collect_jiuyangongshe_pool.py
```

`combined` 默认容错：韭研公社/QMT 名称转代码不可用时会打印 `WARNING`，并继续用问财结果生成股票池。需要任一来源失败即退出时，加 `--strict-sources`。

也可以把 cookie 放到本机私有配置文件 `config/local_runtime.json`，该文件已被 `.gitignore` 忽略，不能提交：

```json
{
  "IWENCAI_COOKIE": "你的问财登录cookie"
}
```

读取优先级：`--iwencai-cookie` > 环境变量 `IWENCAI_COOKIE` > `config/local_runtime.json`。

默认筛选条件：

- 查询语句：`涨停，实际流通市值大于19亿,30日最大振幅小于50%，非st，主板`
- 调用方式：`pywencai.get(query=..., query_type="stock", loop=True, cookie=...)`
- 输出前仍会做代码归一、去重、主板过滤和非 ST 过滤
- 输出：`config/main_seal_follow_pool.csv`

单次立即生成：

```powershell
python scripts\collect_main_seal_pool.py --once --amount 1000
```

常用参数：

- `--iwencai-cookie "..."`：直接传入问财登录 cookie；不传则读取环境变量 `IWENCAI_COOKIE`，再读取 `config/local_runtime.json`。
- `--iwencai-query "..."`：调整问财自然语言查询。
- `--no-iwencai-loop`：不自动翻页。
- `--max-count 50`：最多输出 50 只。
- `--output path.csv`：输出到指定文件。
- `--no-backup`：覆盖前不备份旧股票池。
- `--no-market-day-check`：非交易日也执行。

`pywencai` 需要本机安装 Node.js，并需要有效的问财登录 cookie。如果 cookie 失效，重新从浏览器登录问财后复制新的 cookie。

### QMT 本地近似生成

也可以用 QMT 本地日线和证券资料生成近似股票池：

```powershell
python scripts\collect_main_seal_pool.py --source qmt --once --amount 1000
```

QMT 来源筛选条件：

- QMT 板块：`沪深A股`
- 主板代码：`000/001/002/003/600/601/603/605`
- 非 ST、非退市名称
- 最近一个交易日收盘价等于涨停价
- 本地流通市值：大于 `19亿`，口径为 QMT `FloatVolume * 最近收盘价`，不是问财“实际流通市值/自由流通市值”的严格口径
- 30 日最大振幅：小于 `50%`，计算口径为 `30日最高价最大值 / 30日最低价最小值 - 1`
- 输出：`config/main_seal_follow_pool.csv`

QMT 常用参数：

- `--min-float-market-value 1900000000`：调整最小实际流通市值。
- `--max-amplitude-30d 50`：调整 30 日最大振幅上限。
- `--history-count 31`：读取日线数量，默认需要最近 31 根日线，其中最后 30 根用于振幅，倒数第 2 根用于计算涨停价。
- `--no-download-history`：不先增量下载日线，只读取本地已有数据。默认会先对股票池候选范围批量下载 `1d` 日线。

每次覆盖正式股票池前，默认会把旧文件备份为：

```text
config/main_seal_follow_pool.backup_YYYYMMDD_HHMMSS.csv
```

股票池生成工具不连接交易账户，不会下单。

### 韭研公社文章生成

也可以从韭研公社用户页的最新文章生成股票池，用于每天盘前把热点事件、公告和涨停事件涉及的股票写入同一个 CSV：

```powershell
python scripts\collect_main_seal_pool.py --source jiuyangongshe --schedule-time 08:45 --amount 1000
```

单次调试指定文章：

```powershell
python scripts\collect_main_seal_pool.py --source jiuyangongshe --once --article-url https://www.jiuyangongshe.com/a/205ec428s8y --amount 1000
```

当前只解析这些节点：

- `No.1 盘前热点事件`
- `No.2 公告精选 -> 一、日常公告`
- `No.4 连板梯队和涨停事件 -> 三、涨停事件`

正式输出会通过 QMT `沪深A股` 名称表把股票名称解析成 6 位代码，并且只保留主板代码：`000/001/002/003/600/601/603/605`。所以需要本地 QMT/xtdata 服务可用。未解析代码时不能直接给策略实盘使用；如只想调试网页解析，可显式加：

```powershell
python scripts\collect_main_seal_pool.py --source jiuyangongshe --once --article-url https://www.jiuyangongshe.com/a/205ec428s8y --no-resolve-codes --allow-name-only-output --output data\tmp_jiuyangongshe_pool.csv --no-market-day-check
```

调试模式会输出名称作为代码，可能包含非股票短语，只用于检查文章结构和解析范围。

## 本地配置

`config/local_runtime.json` 已配置：

```json
"CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH": "C:\\Users\\ysun\\workspace\\cytrade\\config\\main_seal_follow_pool.csv",
"CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN": true
```

- `CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH`：股票池路径。
- `CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN`：是否只模拟下单。`true` 只打日志不真实委托；`false` 才会走真实交易执行器。

## 启用方式

可以直接运行专用入口：

```powershell
python run_main_seal_follow.py
```

该入口只注册 `MainSealFollowStrategy`。等价代码如下：

```python
from main import run_scheduler_service
from strategy.main_seal_follow_strategy import MainSealFollowStrategy

run_scheduler_service(strategy_classes=[MainSealFollowStrategy])
```

启动后，系统会在选股阶段读取股票池，每一行有效股票生成一个独立策略实例。

Level2 订阅采用两层模式：

- 全股票池默认只订阅 `l2quote`，用于轻量扫描是否接近涨停。
- 当某只股票的最新价或买一价进入 `quote_trigger_near_limit_ticks` 范围内，才动态打开 `l2transaction`、`l2order`、`l2orderqueue`。
- 已经打开详细 Level2 的股票，本轮运行中会保持订阅，避免频繁订阅/退订造成抖动。

## 安全检查

首次使用建议保持：

```json
"CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN": true
```

验收标准：

- 日志里能看到股票池中正金额股票被加载。
- 日志里出现 `[DRY_RUN]`，说明只模拟下单。
- Level2 校准目录继续输出该股票的行情诊断文件。

确认信号、队列、撤单指标都符合预期后，再把 `CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN` 改为 `false` 做真实委托。

## 相关文档

- `docs/MAIN_SEAL_FOLLOW_DESIGN.md`
- `docs/MAIN_SEAL_FOLLOW_TRIGGER_PARAMS.md`

## Runtime Diagnostics

Live startup now writes a structured startup line with:

- `mode`: `managed`, `live`, or `market-only`.
- `dry_run`: whether the strategy is allowed to simulate only.
- `qmt_path`: active QMT `userdata_mini` / `userdata` path.
- `account_id`: active trading account id.
- `account_type`: account type, normally `STOCK`.

If account subscription fails, the log writes `Runtime startup blocked` with
`stage/account_id/account_type/qmt_path/return_code`, and live trading is not
allowed to continue.

Market-only dry-run should be started with:

```powershell
python scripts\run_main_seal_follow_market_only.py
```

This script has a normal `if __name__ == "__main__"` guard and is intended for
market data / Level2 / signal validation only. It does not connect the trading
account.

Every runtime writes a `Runtime heartbeat` line every 30 seconds by default.
The heartbeat includes current mode, dry-run flag, connection status, strategy
count, tick/L2 subscription counts, latest market-data time, latest receive
time, latest strategy event, and last strategy processing cost.

After the first limit-up price is known, each stock is prechecked with:

```text
planned_amount >= limit_up_price * 100
```

If the planned amount cannot buy one lot, the strategy marks that stock as
`entry_disabled reason=planned_amount_below_one_lot`, keeps only lightweight
`l2quote`, and skips trigger/order logic. Repeated warnings for the same reason
are throttled.

## Live Trading Guard

`CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=false` only means策略允许走实盘路径；真实下单还必须同时满足执行层硬保护：

- `TradeExecutor.live_trading_enabled=true`，即执行器被显式装配为实盘模式。
- `xtquant` 可用，不能用缺失柜台模块时的 mock 通道替代。
- `ConnectionManager.is_trading_ready()` 为 `true`：底层 trader 在线、账户对象存在、账户订阅成功、最近连接错误为空。
- 委托数量必须大于 0；买入数量必须是 100 股整数倍；限价类委托价格必须大于 0。

如果任一条件不满足，下单会被登记为 `JUNK`，日志包含
`live_trading_not_ready` 或具体校验原因，不会生成 `[MOCK]` 活动委托。
撤单同样要求交易就绪；未就绪时直接返回失败，不会把订单误标为已撤。

`dry_run=false` 时，启动阶段还会执行 `Live trading preflight`：

- 执行器必须是 live enabled。
- `xtquant`、trader、account、账户订阅状态必须全部就绪。
- 能查询到账户资产，并能识别可用资金字段。

真实买入每次发柜台前还会重新做资金校验：

```text
required_amount = order.price * order.quantity
available_cash >= required_amount
```

资金不足、资产查询失败、无法识别可用资金、非法代码、非法价格或非法数量时，
订单会被登记为 `JUNK` 并写清 `available_cash / required_amount / reason`。
校验通过后，日志会先打印 `[ORDER] LIVE preflight passed`，再调用真实
`order_stock_async`。

## Strategy Event Logs

MainSealFollow 会为关键逻辑节点输出统一事件日志，前缀为：

```text
MSF_EVENT
```

事件正文是 JSON，核心字段包括：

- `event`：事件类型，例如 `entry_signal_accepted`、`entry_signal_blocked`、`entry_plan_created`、`dry_run_entry_submitted`、`live_entry_submitted`、`probe_filled`、`main_keep_decision`、`main_cancel_decision`、`entry_cancel_submitted`。
- `stock/name/state/dry_run`：标的、名称、当前状态和是否 dry-run。
- `reason/source`：触发原因或阻断原因，以及来自哪类 L2 数据。
- `metrics`：当时的关键指标，例如封单前排、撤买金额、观测单成交耗时、主单保留/撤单判断依据。

阻断类事件按同一股票同一原因限频，避免每笔行情都刷屏；下单、成交、撤单和决策类事件实时记录。
即使开启 `LOG_SUMMARY_MODE=true`，`MSF_EVENT` 也会保留输出，便于实盘时只看关键逻辑事件。
