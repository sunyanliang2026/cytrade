# Manual Managed Monitoring Checklist

This checklist is for the case where the account already has a real order or
position, and the system should load only the manual stock pool and continue
monitoring under the full managed runtime.

Default entrypoint:

```powershell
scripts\ops\start_main_seal_follow_manual_managed.bat
```

Default pool file:

```text
config/main_seal_follow_manual_pool.csv
```

## Before Start

- Confirm `config/main_seal_follow_manual_pool.csv` contains only the intended
  stocks.
- Confirm QMT is logged in and the target account is available.
- Confirm `CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=true` if the goal is only takeover
  monitoring, not new live orders.

Example single-stock pool:

```csv
code,name,plan_amount
600162,香江控股,50000
```

## Startup

Expected log markers:

- `Runtime startup mode=managed ...`
- `qmt_path=...`
- `account_id=...`
- `account_type=STOCK`

Acceptance:

- The runtime starts in `managed` mode, not `market-only`.
- No `Unable to connect QMT` or `Runtime startup blocked ...` line appears.
- No `启动前账户校验已跳过，交易连接未就绪` line appears.

## Stock Pool Load

Expected log markers:

- `StrategyRunner: 添加策略 MainSealFollow stock=600162`

Acceptance:

- Only stocks from `config/main_seal_follow_manual_pool.csv` are loaded.
- If the pool has only one row, only one strategy instance is created.

## Account Sync

Expected log markers:

- `StrategyRunner: 主动同步完成 reason=... trades=... orders=... recovered=...`

Acceptance:

- Account sync runs after startup.
- For a real pre-existing order or position, `orders` or `recovered` should
  normally be greater than `0`.

## Takeover State

Expected strategy outcome for the target stock:

- Real active buy order exists:
  - target state: `WAIT_PROBE_FILL`
- Real position already exists:
  - target state: `HAS_POSITION`

Expected log markers:

- `MSF_EVENT {... "stock":"600162", ... "state":"WAIT_PROBE_FILL" ...}`
- `MSF_EVENT {... "stock":"600162", ... "state":"HAS_POSITION" ...}`

Acceptance:

- The target stock reaches the expected takeover state.
- The state must match the real account situation:
  - active order -> `WAIT_PROBE_FILL`
  - existing position -> `HAS_POSITION`

## During Monitoring

Watch:

- `Runtime heartbeat ... connected=True strategies=...`
- `MSF_EVENT ... stock=600162 ...`
- account/order synchronization updates if they appear

Acceptance:

- Heartbeat continues normally.
- No unrelated stocks appear if the manual pool contains only one stock.
- The target stock keeps producing events consistent with its takeover state.

## Common Failure Signs

- `Runtime startup blocked ...`
  - QMT or account connection failed.
- `启动前账户校验已跳过，交易连接未就绪`
  - This is not a valid managed takeover run.
- `账户未查询到对应持仓`
  - Strategy/account position view is inconsistent.
- `策略持仓超过账户实际持仓`
  - Local restored state is larger than account reality.
- `策略可用持仓超过账户实际可用持仓`
  - Available quantity is inconsistent with the account.

## Minimum Acceptance

The run is acceptable only if all of the following hold:

- runtime mode is `managed`
- only the manual-pool stocks are loaded
- account connection succeeds
- account sync runs
- `600162` reaches `WAIT_PROBE_FILL` or `HAS_POSITION` as expected
