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

- Wait until `08:50`, then generate `data/stock_pools/current/main_seal_follow_pool.csv`.
- Start `MainSealFollow` in `market-only` mode with `dry_run=true` at `09:15`.
- Stop automatically at `11:00`.
- Generate the post-session morning review automatically after the runtime stops.
- Keep console output in summary mode, but still print `MSF_EVENT`,
  `[ORDER]`, `[TRADE]`, and runtime heartbeat lines.

Useful switches:

- `--pool-time 08:50`: adjust stock-pool generation time.
- `--strategy-start-time 09:15`: delay runtime start until after pool generation.
- `--stop-time 11:00`: adjust session stop time.
- `--pool-source combined`: choose pool source.
- `--full-console`: disable summary mode and print all console logs.
- `--strict-sources`: fail the run if any configured stock-pool source fails.
- `--no-post-review`: skip automatic post-session review generation.
- `--heartbeat-interval-sec 30`: heartbeat check interval.
- `--heartbeat-stable-repeat 10`: only repeat unchanged heartbeat every 10 checks,
  about 5 minutes with the default interval.

Windows scheduled task helper:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\register_main_seal_follow_monitor_task.ps1
```

Defaults:

- Task name: `Cytrade MainSealFollow Monitor`
- Trigger time / pool time: `08:50`
- Strategy start time: `09:15`
- Stop time: `11:00`
- Action: run `scripts\ops\start_main_seal_follow_monitor.bat --pool-time 08:50 --strategy-start-time 09:15 --stop-time 11:00`

Optional overrides:

- `-StartTime 08:49`
- `-StrategyStartTime 09:20`
- `-StopTime 10:30`
- `-TaskName "Cytrade MainSealFollow Monitor Test"`

To separate stock selection and monitoring runtime, for example `08:50`
selection plus `09:15` strategy start:

```powershell
python scripts\run\run_main_seal_follow_monitor_session.py --pool-time 08:50 --strategy-start-time 09:15 --stop-time 11:00
```

After the session stops, the wrapper runs:

```powershell
python -m agent.loops.post_morning_review --run-id YYYY-MM-DD
```

and writes:

- `agent/memory/runs/YYYY-MM-DD_morning.md`
- `agent/memory/runs/YYYY-MM-DD_morning_summary.json`
- `agent/memory/improvement_tasks.yaml`

Key replay markers in logs:

- `MONITOR_SESSION pool_collect_start` / `pool_generated`
- `MONITOR_SESSION monitor_start`
- `Runtime heartbeat ...`
- `MSF_EVENT ...`
- `MSF_EVENT {"event":"dry_run_probe_trade_recorded", ...}`
- `[ORDER] [TRADE] [MOCK] observation filled ...`
- `MONITOR_SESSION session_stop ...`

Runtime log noise policy:

- State changes still emit heartbeat immediately.
- Stable heartbeat defaults to about once every 5 minutes in the monitoring
  wrapper.
- `MSF_EVENT`, `[ORDER]`, and `[TRADE]` remain the primary replay markers.

For the first real trading-day run, see
`docs/NEXT_TRADING_DAY_OBSERVATION_CHECKLIST.md`.
