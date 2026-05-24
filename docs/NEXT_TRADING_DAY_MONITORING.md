# Next-Trading-Day Monitoring

Use the dedicated wrapper when you want a dry-run monitoring session for the
next trading day:

```bash
python scripts\run_main_seal_follow_monitor_session.py
```

You can also use the local launcher:

```bash
scripts\start_main_seal_follow_monitor.bat
```

Default behavior:

- Wait until `08:50`, then generate `config/main_seal_follow_pool.csv`.
- Start `MainSealFollow` in `market-only` mode with `dry_run=true`.
- Stop automatically at `12:00`.
- Keep console output in summary mode, but still print `MSF_EVENT`,
  `[ORDER]`, `[TRADE]`, and runtime heartbeat lines.

Useful switches:

- `--pool-time 08:50`: adjust stock-pool generation time.
- `--stop-time 12:00`: adjust session stop time.
- `--pool-source combined`: choose pool source.
- `--full-console`: disable summary mode and print all console logs.
- `--strict-sources`: fail the run if any configured stock-pool source fails.

Windows scheduled task helper:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_main_seal_follow_monitor_task.ps1
```

Defaults:

- Task name: `Cytrade MainSealFollow Monitor`
- Trigger time: `08:50`
- Action: run `scripts\start_main_seal_follow_monitor.bat`

Optional overrides:

- `-StartTime 08:49`
- `-TaskName "Cytrade MainSealFollow Monitor Test"`

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
