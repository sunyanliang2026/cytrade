# Next-Trading-Day Monitoring

Use the dedicated wrapper when you want a dry-run monitoring session for the
next trading day:

```bash
python scripts\run\run_main_seal_follow_monitor_session.py
```

You can also use the local launcher:

```bash
scripts\ops\start_main_seal_follow_monitor.bat
```

Default behavior:

- Wait until `08:50`, then generate `config/main_seal_follow_pool.csv`.
- By default, start `MainSealFollow` in `market-only` mode with `dry_run=true`
  immediately after pool generation.
- Stop automatically at `10:00`.
- Keep console output in summary mode, but still print `MSF_EVENT`,
  `[ORDER]`, `[TRADE]`, and runtime heartbeat lines.

Useful switches:

- `--pool-time 08:50`: adjust stock-pool generation time.
- `--strategy-start-time 09:15`: delay runtime start until after pool generation.
- `--stop-time 10:00`: adjust session stop time.
- `--pool-source combined`: choose pool source.
- `--full-console`: disable summary mode and print all console logs.
- `--strict-sources`: fail the run if any configured stock-pool source fails.

Windows scheduled task helper:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\register_main_seal_follow_monitor_task.ps1
```

Defaults:

- Task name: `Cytrade MainSealFollow Monitor`
- Trigger time / pool time: `08:50`
- Strategy start time: `09:15`
- Stop time: `10:00`
- Action: run `scripts\ops\start_main_seal_follow_monitor.bat --pool-time 08:50 --strategy-start-time 09:15 --stop-time 10:00`

Optional overrides:

- `-StartTime 08:49`
- `-StrategyStartTime 09:20`
- `-StopTime 10:30`
- `-TaskName "Cytrade MainSealFollow Monitor Test"`

To separate stock selection and monitoring runtime, for example `08:50`
selection plus `09:15` strategy start:

```powershell
python scripts\run\run_main_seal_follow_monitor_session.py --pool-time 08:50 --strategy-start-time 09:15 --stop-time 10:00
```

Key replay markers in logs:

- `MONITOR_SESSION pool_collect_start` / `pool_generated`
- `MONITOR_SESSION monitor_start`
- `Runtime heartbeat ...`
- `MSF_EVENT ...`
- `MSF_EVENT {"event":"dry_run_probe_trade_recorded", ...}`
- `[ORDER] [TRADE] [MOCK] observation filled ...`
- `MONITOR_SESSION session_stop ...`

For the first real trading-day run, see
`docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md`.
