# MainSealFollow

Self-contained strategy package for the MainSealFollow workflow.

Contents:

- `strategy.py`: strategy implementation.
- `scripts/`: runnable market-only, monitor, managed session, and probe tools.
- `tests/`: strategy-specific tests.
- `docs/`: design, usage, trigger parameters.
- `data/` and `output/`: strategy-owned inputs and run artifacts.

Canonical one-command entries:

```bat
strategies\main_seal_follow\scripts\run_monitor_session.bat
strategies\main_seal_follow\scripts\run_managed_session.bat
strategies\main_seal_follow\scripts\run_manual_monitor.bat
strategies\main_seal_follow\scripts\run_manual_managed.bat
```

Strategy-specific files should stay in this package. The old strategy-specific compatibility wrappers under `strategy.*`, `scripts/run`, and `scripts/ops` have been removed.
