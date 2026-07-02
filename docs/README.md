# cytrade 文档索引

顶层 `docs/` 只放跨策略、项目级和当前状态文档；具体策略自己的设计、用法、回放记录和 handoff 文档放在对应的 `strategies/<strategy_name>/docs/` 下。

## 当前项目级文档

- `STOCK_POOL_LOGIC.md`：主封单股票池逻辑。
- `STOCK_POOL_DIRECTORY_STRUCTURE.md`：股票池目录结构。
- `NEXT_TRADING_DAY_MONITORING.md`：下个交易日监控流程。
- `NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md`：观察检查清单。
- `SELF_IMPROVING_AGENT_SYSTEM.md`：dry-run 复盘和自改进 agent 流程。
- `LEVEL2_VALIDATION.md`：Level2 验证说明。
- `PROJECT_STATUS_20260524.md`：阶段状态记录。
- `HANDOFF_CURRENT_PROJECT_20260623.md`：当前项目 handoff。

## 策略专属文档

### MainSealFollow

- `strategies/main_seal_follow/docs/design.md`
- `strategies/main_seal_follow/docs/trigger_params.md`
- `strategies/main_seal_follow/docs/usage.md`

### OpeningAuctionAttitude

- `strategies/opening_auction_attitude/docs/strategy_v1.md`
- `strategies/opening_auction_attitude/docs/replay_notes_20260605.md`
- `strategies/opening_auction_attitude/docs/handoff_l2_probe_20260605.md`
- `strategies/opening_auction_attitude/docs/handoff_current_project_20260623.md`

## 归档

- `archive/`：历史输出、旧指南、发布/打包遗留材料。
- `project/`：项目级 changelog、贡献、安全和发布检查清单。
- `screenshots/`：README 使用的 Web 控制台截图。

## 去重说明

以下顶层重复文件已移除，canonical 版本保留在策略包内：

- `docs/MAIN_SEAL_FOLLOW_DESIGN.md` → `strategies/main_seal_follow/docs/design.md`
- `docs/MAIN_SEAL_FOLLOW_TRIGGER_PARAMS.md` → `strategies/main_seal_follow/docs/trigger_params.md`
- `docs/MAIN_SEAL_FOLLOW_USAGE.md` → `strategies/main_seal_follow/docs/usage.md`
- `docs/HANDOFF_OPENING_AUCTION_L2_PROBE_20260605.md` → `strategies/opening_auction_attitude/docs/handoff_l2_probe_20260605.md`
- `docs/opening_auction_attitude_replay_notes_20260605.md` → `strategies/opening_auction_attitude/docs/replay_notes_20260605.md`
- `docs/opening_auction_attitude_strategy_v1.md` → `strategies/opening_auction_attitude/docs/strategy_v1.md`
