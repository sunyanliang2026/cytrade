# MainSealFollow lessons

Use this file to keep durable lessons from morning dry-run reviews.

## 2026-05-24 seed

The immediate unknowns are live trading-session validation points: strategy activation after 09:30, tick subscription growth, Level2 detail subscription growth, and a complete dry-run trigger chain. Prefer observability and replay improvements before strategy-parameter changes.

## 2026-05-25 invalid monitor session

Morning dry-run review for `2026-05-25` showed that the main session was `logs/system.11652.log` plus `logs/trade.11652.log`.

What happened:

- pool generation succeeded with `total=52`
- the market-only runtime started
- 52 strategy instances were created
- the session ran until `12:00`
- `real_order_sent=false`

Why the run is invalid for strategy evaluation:

- every heartbeat kept `connected=False`
- every heartbeat kept `tick_subscriptions=0`
- `latest_data_time` stayed empty
- no `MSF_EVENT` trigger chain appeared
- trade logs contained strategy init/start/pause only, with no order or mock-trade markers

Operational rule:

- Do not treat this pattern as "the strategy saw the market and found no opportunity".
- Treat it as "market data never became usable".
- Future morning review logic should emit an explicit invalid-monitor reason when this signature appears.
