# OpeningAuctionAttitude

Self-contained strategy package for opening-auction attitude observation.

Current contents:

- `strategy.py`, `models.py`, `score.py`: strategy implementation and scoring logic.
- `scripts/`: runnable market-only, L2 probe, analysis, and replay tools.
- `tests/`: strategy-specific tests will be migrated here in the next step.
- `data/`: strategy-owned input samples/fixtures placeholder.
- `output/`: strategy-owned run outputs/reports placeholder.

Compatibility wrappers remain under the old `strategy.opening_auction_attitude` and `scripts/...` paths during migration.
