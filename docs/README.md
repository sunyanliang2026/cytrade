# cytrade Documentation Index

Top-level `docs/` is for project-wide documents. Strategy-specific design,
usage, replay notes, handoff files, scripts, tests, data, and outputs should
live under the matching `strategies/<strategy_name>/` package.

## Project Docs

- `STOCK_POOL_LOGIC.md`: shared stock-pool generation logic.
- `STOCK_POOL_DIRECTORY_STRUCTURE.md`: stock-pool directory structure.
- `NEXT_TRADING_DAY_MONITORING.md`: next trading-day monitoring flow.
- `NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md`: observation checklist.
- `SELF_IMPROVING_AGENT_SYSTEM.md`: dry-run review and agent loop.
- `LEVEL2_VALIDATION.md`: Level2 validation notes.
- `PROJECT_STATUS_20260524.md`: project status snapshot.
- `STRATEGY_DIRECTORY_LAYOUT.md`: canonical strategy directory layout.

## Strategy Docs

### MainSealFollow

- `strategies/main_seal_follow/docs/design.md`
- `strategies/main_seal_follow/docs/trigger_params.md`
- `strategies/main_seal_follow/docs/usage.md`

### OpeningAuctionAttitude

- `strategies/opening_auction_attitude/docs/strategy_v1.md`
- `strategies/opening_auction_attitude/docs/replay_notes_20260605.md`
- `strategies/opening_auction_attitude/docs/handoff_l2_probe_20260605.md`
- `strategies/opening_auction_attitude/docs/handoff_current_project_20260623.md`

## Archive

- `archive/`: historical outputs, old guides, and packaging leftovers.
- `project/`: project-level changelog, contribution, security, and release docs.
- `screenshots/`: screenshots used by README.

## Removed Duplicates

Canonical strategy files now live under `strategies/<strategy_name>/`.
The old root-level strategy wrappers and duplicate strategy launchers under
`scripts/run`, `scripts/probe`, `scripts/replay`, and `scripts/ops` have been
removed.
