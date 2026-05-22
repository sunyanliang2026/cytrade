# 主封单跟随策略使用说明

## 股票池

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
