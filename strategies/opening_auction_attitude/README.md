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
strategies\opening_auction_attitude\scripts\run_morning.bat
```

It runs the opening-auction observation pipeline end to end:

1. collect source caches from `config/main_seal_pool_sources.json`;
2. build strict all-candidate universe `data/stock_pools/current/opening_auction_universe.csv`;
3. run the full-pool snapshot scanner and dynamically select the small Level2 pool;
4. subscribe only small-pool `l2order,l2transaction` detail data;
5. archive run artifacts under `data/probe/opening_auction_l2/YYYYMMDD_HHMMSS/`:
   - `snapshot_full_pool.jsonl`
   - `auction_rankings.csv`
   - `auction_buy_plan.csv`
   - `auction_matched_candidates.csv`
   - `auction_matched_candidates.md`
   - `run_manifest.json`

The buy plan is analysis-only: `status=PLAN_ONLY`, `observe_only=True`, `real_order_sent=False`. It does not submit orders.

Strategy-specific files should stay in this package. Root-level compatibility wrappers for this strategy have been removed.
