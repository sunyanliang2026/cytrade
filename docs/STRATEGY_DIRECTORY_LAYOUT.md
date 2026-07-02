# Strategy Directory Layout

Canonical strategy-owned files live under:

```text
strategies/<strategy_name>/
```

Each strategy package should keep its own files in these subdirectories:

- `strategy.py`, `models.py`, `score.py`: strategy implementation and local domain code.
- `scripts/`: runnable strategy entrypoints and one-command BAT files.
- `tests/`: strategy-specific regression tests.
- `docs/`: strategy design, handoff, replay notes, and usage docs.
- `data/`: strategy-owned static samples or default inputs.
- `output/`: strategy-owned generated reports or placeholders.

Current canonical entrypoints:

- MainSealFollow: `strategies/main_seal_follow/scripts/`
- OpeningAuctionAttitude morning: `strategies/opening_auction_attitude/scripts/run_morning.bat`
- OpeningAuctionAttitude L2 probe: `strategies/opening_auction_attitude/scripts/run_l2_probe.bat`
- JuejinSellStrategy: `strategies/juejin_sell_strategy/scripts/run_managed_session.bat`

Compatibility policy:

- Strategy-specific compatibility wrappers under `scripts/run`, `scripts/probe`, `scripts/ops`, and legacy `strategy.<strategy_name>` packages have been removed.
- New implementation code should not be added to old wrapper locations.
- New strategy-specific tests should live under `strategies/<strategy_name>/tests/`.
- Project-wide infrastructure tests remain under root `tests/`.
