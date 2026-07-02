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

Safety notes:

- This package must not enable live trading by itself.
- Account credentials, QMT paths, `.env`, and `config/local_runtime.json` are outside this package and must not be changed by this strategy migration.
