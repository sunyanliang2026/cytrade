# OpeningAuctionAttitude

Self-contained strategy package for opening-auction attitude observation.

Current contents:

- `strategy.py`, `models.py`, `score.py`: strategy implementation and scoring logic.
- `scripts/`: runnable market-only, L2 probe, analysis, and replay tools.
- `tests/`: strategy-specific tests.
- `data/`: strategy-owned input samples/fixtures placeholder.
- `output/`: strategy-owned run outputs/reports placeholder.

Current one-command morning flow:

```bat
scripts\run\run_opening_auction_attitude_morning.bat
```

It runs the opening-auction observation pipeline end to end:

1. collect source caches from `config/main_seal_pool_sources.json`;
2. build strict all-candidate universe `data/stock_pools/current/opening_auction_universe.csv`;
3. start the all-candidate Level2 recorder, writing raw artifacts under `data/probe/opening_auction_l2/YYYYMMDD_HHMMSS/`;
4. run `OpeningAuctionAttitudeStrategy` in default `--install-all` mode for the whole loaded candidate pool;
5. archive run artifacts in the same probe directory:
   - `opening_l2_raw.jsonl`
   - `opening_l2_summary.csv`
   - `opening_l2_schema.json`
   - `snapshot_full_pool.jsonl`
   - `auction_rankings.csv`
   - `auction_buy_plan.csv`
   - `run_manifest.json`

`--candidate-freeze-time` is only for the legacy opt-in `--dynamic-candidates` mode. It is not part of the default all-candidate morning flow.

The buy plan is analysis-only: `status=PLAN_ONLY`, `observe_only=True`, `real_order_sent=False`. It does not submit orders.

Compatibility wrappers remain under the old `strategy.opening_auction_attitude` and `scripts/...` paths during migration.
