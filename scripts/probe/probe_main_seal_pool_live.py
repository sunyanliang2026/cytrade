"""Run a short dry-run live probe for MainSealFollow stock pool.

The probe validates dynamic Level2 subscription behavior:
- subscribe l2quote for the whole pool first
- open l2order/l2transaction/l2orderqueue only after a strategy requests detail L2
- print per-stock state summary
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from core.data_subscription import DataSubscriptionManager
from strategy.main_seal_follow_strategy import MainSealFollowStrategy
from strategy.models import StrategyConfig


DETAIL_KINDS = {"l2transaction", "l2order", "l2orderqueue"}


def build_strategies(pool_path: str) -> dict[str, MainSealFollowStrategy]:
    seed = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="",
            params={
                "csv_path": pool_path,
                "dry_run": True,
                "dry_run_replay_probe_logic": True,
                "l2_calibration_enabled": False,
            },
        )
    )
    strategies: dict[str, MainSealFollowStrategy] = {}
    for cfg in seed.select_stocks():
        params = dict(cfg.params or {})
        params["dry_run"] = True
        params["dry_run_replay_probe_logic"] = True
        params["l2_calibration_enabled"] = False
        cfg.params = params
        strategies[cfg.stock_code] = MainSealFollowStrategy(cfg)
    return strategies


def run_probe(pool_path: str, seconds: int) -> None:
    strategies = build_strategies(pool_path)
    codes = list(strategies)
    manager = DataSubscriptionManager()
    counts = {code: defaultdict(int) for code in codes}
    latest = {code: {} for code in codes}
    lock = threading.Lock()

    def sync_dynamic_subscriptions() -> None:
        current = {code: set(kinds) for code, kinds in manager.get_l2_subscription_map().items()}
        for code, strategy in strategies.items():
            desired = set(strategy.current_data_kinds())
            have = current.get(code, set())
            add = sorted(desired - have)
            remove = sorted(have - desired)
            if add:
                manager.subscribe_l2_stocks([code], kinds=add)
            if remove:
                manager.unsubscribe_l2_stocks([code], kinds=remove)

    def on_quote(events):
        for code, event in events.items():
            strategy = strategies.get(code)
            if not strategy:
                continue
            with lock:
                counts[code]["l2quote"] += 1
                latest[code].update(
                    {
                        "last": float(event.last_price or 0),
                        "pre": float(event.pre_close or 0),
                        "bid1": float(event.bid1 or 0),
                        "ask1": float(event.ask1 or 0),
                        "bid1_vol": int(event.bid1_volume or 0),
                        "limit_field": float(event.limit_up_price or 0),
                    }
                )
            try:
                strategy.on_l2_quote(event)
            except Exception as exc:
                print(f"ERR quote {code} {exc!r}", flush=True)
        sync_dynamic_subscriptions()

    def on_order(events_by_code):
        for code, events in events_by_code.items():
            strategy = strategies.get(code)
            if not strategy:
                continue
            with lock:
                counts[code]["l2order"] += len(events)
                if events:
                    event = events[-1]
                    latest[code].update(
                        {
                            "ord_price": float(event.price or 0),
                            "ord_vol": int(event.volume or 0),
                            "ord_side": str(event.side or ""),
                            "ord_cancel": bool(event.is_cancel),
                        }
                    )
            for event in events:
                try:
                    strategy.on_l2_order(event)
                except Exception as exc:
                    print(f"ERR order {code} {exc!r}", flush=True)

    def on_transaction(events_by_code):
        for code, events in events_by_code.items():
            strategy = strategies.get(code)
            if not strategy:
                continue
            with lock:
                counts[code]["l2transaction"] += len(events)
                if events:
                    event = events[-1]
                    latest[code].update(
                        {
                            "tx_price": float(event.price or 0),
                            "tx_vol": int(event.volume or 0),
                            "tx_flag": event.trade_flag,
                        }
                    )
            for event in events:
                try:
                    strategy.on_l2_transaction(event)
                except Exception as exc:
                    print(f"ERR transaction {code} {exc!r}", flush=True)

    def on_queue(events):
        for code, event in events.items():
            strategy = strategies.get(code)
            if not strategy:
                continue
            volumes = list(event.bid_level_volume or [])
            with lock:
                counts[code]["l2orderqueue"] += 1
                latest[code].update(
                    {
                        "q_price": float(event.price or 0),
                        "q_reported": int(event.reported_total_order_count or 0),
                        "q_front_lot": int(sum(volumes)),
                        "q_first5": volumes[:5],
                    }
                )
            try:
                strategy.on_l2_orderqueue(event)
            except Exception as exc:
                print(f"ERR queue {code} {exc!r}", flush=True)

    manager.set_l2_quote_callback(on_quote)
    manager.set_l2_order_callback(on_order)
    manager.set_l2_transaction_callback(on_transaction)
    manager.set_l2_orderqueue_callback(on_queue)

    print(f"POOL_STRATEGY_TEST start seconds={seconds} codes={len(codes)} codes={codes}", flush=True)
    manager.subscribe_l2_stocks(codes, kinds=["l2quote"])
    print(f"INITIAL_SUBS {manager.get_l2_subscription_map()}", flush=True)
    threading.Thread(target=manager.start, daemon=True).start()

    for elapsed in range(10, seconds + 1, 10):
        time.sleep(10)
        with lock:
            detail_codes = sorted(
                code
                for code, kinds in manager.get_l2_subscription_map().items()
                if DETAIL_KINDS & set(kinds)
            )
            entered = [
                (code, strategy._entry_state, list(strategy._entry_order_uuids))
                for code, strategy in strategies.items()
                if strategy._entry_state != strategy.STATE_WAIT_SIGNAL
            ]
            recv_codes = sum(1 for code in codes if counts[code].get("l2quote", 0) > 0)
            totals = defaultdict(int)
            for code in codes:
                for kind, value in counts[code].items():
                    totals[kind] += value
            print(
                f"PROGRESS t={elapsed}s recv_quote_codes={recv_codes}/{len(codes)} "
                f"detail_codes={detail_codes} totals={dict(totals)} entered={entered}",
                flush=True,
            )

    print("SUMMARY_BEGIN", flush=True)
    with lock:
        sub_map = manager.get_l2_subscription_map()
        for code in codes:
            strategy = strategies[code]
            print(
                "ROW "
                f"code={code} subs={sub_map.get(code, [])} counts={dict(counts[code])} "
                f"latest={latest[code]} limit={strategy._limit_up_price:.3f} "
                f"detail={strategy._l2_detail_enabled} state={strategy._entry_state} "
                f"target_lots={strategy._target_lots} orders={strategy._entry_order_uuids} "
                f"last_cancel={strategy._last_cancel_reason}",
                flush=True,
            )
    print("SUMMARY_END", flush=True)
    os._exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run live probe for MainSealFollow stock pool.")
    parser.add_argument("--pool", default=str(REPO_ROOT / "config" / "main_seal_follow_pool.csv"))
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    run_probe(args.pool, args.seconds)


if __name__ == "__main__":
    main()
