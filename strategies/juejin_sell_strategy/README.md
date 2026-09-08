# JuejinSellStrategy

Self-contained strategy package for the sell-side strategy converted from the original Juejin/GM tick strategy.

Contents:

- `strategy.py`: cytrade/QMT strategy implementation. Orders are routed through the shared `TradeExecutor` and existing execution gates.
- `data/sell_10.csv`: default stock/quantity input copied from the original Juejin strategy.
- `scripts/run_managed_session.py`: managed-session entry point for running only this strategy from CSV.
- `scripts/run_managed_session.bat`: one-command Windows entry point for this strategy.
- `docs/original_juejin_main.py`: original Juejin/GM source for reference only; do not run it inside cytrade.
- `tests/`: strategy-specific regression tests.
- `output/`: strategy-owned run artifacts placeholder.

Canonical one-command entry:

```bat
strategies\juejin_sell_strategy\scripts\run_managed_session.bat
```

Strategy-specific files should stay in this package. Root-level compatibility wrappers for this strategy have been removed.

Behavior notes:

- `sellvol` in CSV is treated as the strategy-side sellable quantity.
- The strategy does not require or mock live account holdings before emitting a sell attempt.
- If the real account has no holding, the sell order may be rejected by the execution/account layer; that is acceptable for verification and does not pause this strategy for account-position rejection messages.

## 卖出逻辑

策略从 `09:15` 起接收行情，主要卖出判断窗口为 `09:26-14:57`。所有卖出数量受 CSV 的 `sellvol` 限制；同一卖出动作有独立去重标记，不会因连续行情重复提交。

| 场景 | 条件 | 卖出动作 |
|---|---|---|
| 竞价严重不及预期 | 仅 `exp=1`；`09:24:56` 后买一处于昨收 `-6%` 至 `-0.5%` | 目标数量挂跌停价，剩余数量挂昨收 `+5%` |
| 早盘不及预期 | 仅 `exp=1`；`09:26-09:31` 买一低于昨收 `-2%` | 撤前单，目标数量按买一 `-1%` 卖出 |
| 弱势反弹失败 | 先跌破昨收 `-2.5%`，前一笔买一曾高于 `+2%` 后又跌破 `+2%`，日内低点低于 `-3%`，且晚于 `09:33` | 目标数量按买一 `-1%` 卖出，剩余数量挂昨收 `+4%` |
| 跌停止损 | 卖一等于跌停价，非一字跌停开盘，且跌停卖一封单金额超过 `5000 万` | 撤前单，按跌停价清仓 |
| `+4%` 至 `+7%` 冲高回落 | 曾进入该涨幅区间；最高价超过 `+6.5%` 后买一跌破 `+5%`，或买一跌破 `+3%` | 撤前单，目标数量按买一 `-1%` 卖出；`09:31` 前首次跌破 `+5%` 且开盘未达 `+5%` 时暂不卖 |
| 超过 `+7%` 后回落 | 曾高于 `+7%` 且未涨停；最高价超过 `+8.5%` 后买一跌破 `+7%`，或 `09:31` 前买一跌破 `+6.5%` | 撤前单，目标数量按买一约 `-1.5%` 或 `-1%` 卖出 |
| 涨停大封单开板 | 涨停价买一封单金额超过 `1.2 亿` 后开板，或涨停封单显著走弱 | 首次开板按买一约 `-1.8%` 卖出一笔；封板后 1 分钟内开板、开盘未涨停且早于 `09:40` 时暂不卖 |
| 开板 5 分钟未回封 | 已开板且连续 5 分钟买一仍低于涨停价 | 按当前买一价格卖出 |

涨停价和跌停价按股票所属市场的涨跌停幅度从昨收计算。实盘订单仍须通过统一的账户连接、可用持仓和交易就绪校验。

Safety notes:

- The managed BAT defaults to live mode when double-clicked. It sets the live switch only for that process and requires an interactive `1` confirmation before starting.
- Use `run_managed_session.bat dryrun` for an explicit dry-run session.
- Account credentials, QMT paths, `.env`, and `config/local_runtime.json` are outside this package and must not be changed by this strategy migration.
