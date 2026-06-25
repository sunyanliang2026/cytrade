import csv
import json
from datetime import datetime

from strategies.opening_auction_attitude.scripts.replay_session import (
    build_score_config_from_args,
    load_stock_names,
    load_stock_names_from_tree,
    replay_raw_jsonl,
    run_replay,
    build_parser,
)
from strategies.opening_auction_attitude import AUCTION_STRONG_CONFIRMED, OPEN_DIRECT_PULL


def _row(stock, kind, clock, normalized=None, raw=None):
    return {
        "recv_time": f"2026-06-05 {clock}.000",
        "event_time": f"2026-06-05 {clock}.000",
        "stock": stock,
        "kind": kind,
        "subscribe_mode": "early",
        "normalized": normalized or {},
        "raw": raw or {},
    }


def test_replay_raw_jsonl_outputs_decision_rows(tmp_path):
    raw_path = tmp_path / "opening_l2_raw.jsonl"
    rows = [
        _row(
            "000001",
            "l2quote",
            "09:24:50",
            normalized={"stock_code": "000001", "last_price": 10.0, "pre_close": 10.0},
            raw={"amount": 1_000_000, "lastPrice": 10.0, "lastClose": 10.0},
        ),
        _row(
            "000001",
            "l2quote",
            "09:25:05",
            normalized={"stock_code": "000001", "last_price": 10.3, "pre_close": 10.0},
            raw={"amount": 3_000_000, "lastPrice": 10.3, "lastClose": 10.0},
        ),
        _row(
            "000001",
            "l2order",
            "09:25:00",
            normalized={"stock_code": "000001", "amount": 2_000_000, "side": "BUY"},
        ),
        _row(
            "000001",
            "l2transaction",
            "09:25:00",
            normalized={"stock_code": "000001", "amount": 900_000, "trade_flag": 1},
        ),
        _row(
            "000001",
            "l2order",
            "09:25:06",
            normalized={"stock_code": "000001", "amount": 8_000_000, "side": "SELL"},
        ),
    ]
    raw_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    decisions, summary = replay_raw_jsonl(raw_path)

    assert summary["rows_seen"] == 5
    assert summary["rows_in_window"] == 4
    assert len(decisions) == 1
    assert decisions[0]["stock"] == "000001"
    assert decisions[0]["auction_label"] == AUCTION_STRONG_CONFIRMED
    assert decisions[0]["low_to_final_amount_ratio"] == 2 / 3
    assert decisions[0]["l2order_count"] == 1
    assert decisions[0]["l2transaction_count"] == 1


def test_replay_outputs_open_verify_path_from_open_5m_rows(tmp_path):
    raw_path = tmp_path / "opening_l2_raw.jsonl"
    rows = [
        _row(
            "000001",
            "l2quote",
            "09:24:50",
            normalized={"stock_code": "000001", "last_price": 10.0, "pre_close": 10.0},
            raw={"amount": 1_000_000, "lastPrice": 10.0, "lastClose": 10.0},
        ),
        _row(
            "000001",
            "l2quote",
            "09:25:05",
            normalized={"stock_code": "000001", "last_price": 10.3, "pre_close": 10.0},
            raw={"amount": 3_000_000, "lastPrice": 10.3, "lastClose": 10.0},
        ),
        _row(
            "000001",
            "l2order",
            "09:25:00",
            normalized={"stock_code": "000001", "amount": 2_000_000, "side": "BUY"},
        ),
        _row(
            "000001",
            "l2quote",
            "09:30:00",
            normalized={"stock_code": "000001", "last_price": 10.30, "amount": 4_000_000, "volume": 400_000},
            raw={"lastPrice": 10.30, "amount": 4_000_000, "pvolume": 400_000},
        ),
        _row(
            "000001",
            "l2quote",
            "09:30:10",
            normalized={"stock_code": "000001", "last_price": 10.38, "amount": 7_000_000, "volume": 700_000},
            raw={"lastPrice": 10.38, "amount": 7_000_000, "pvolume": 700_000},
        ),
        _row(
            "000001",
            "l2transaction",
            "09:30:10",
            normalized={"stock_code": "000001", "price": 10.38, "volume": 10_000, "amount": 800_000, "trade_flag": 1},
        ),
        _row(
            "000001",
            "l2transaction",
            "09:30:20",
            normalized={"stock_code": "000001", "price": 10.36, "volume": 10_000, "amount": 100_000, "trade_flag": 2},
        ),
    ]
    raw_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    decisions, summary = replay_raw_jsonl(raw_path)

    assert summary["rows_seen"] == 7
    assert summary["rows_in_window"] == 7
    row = decisions[0]
    assert row["auction_label"] == AUCTION_STRONG_CONFIRMED
    assert row["open_verify_path"] == OPEN_DIRECT_PULL
    assert row["open_verify_reason"] == "direct_pull_confirmed"
    assert row["open_point_count"] == 2
    assert row["open_l2transaction_count"] == 2
    assert row["open_buy_trade_amount"] == 800_000
    assert row["open_sell_trade_amount"] == 100_000


def test_run_replay_writes_csv_and_markdown(tmp_path):
    raw_path = tmp_path / "opening_l2_raw.jsonl"
    raw_path.write_text(
        "\n".join(
            [
                json.dumps(
                    _row(
                        "000001",
                        "l2quote",
                        "09:24:50",
                        normalized={"stock_code": "000001", "last_price": 10.0, "pre_close": 10.0},
                        raw={"amount": 1_000_000, "lastPrice": 10.0, "lastClose": 10.0},
                    )
                ),
                json.dumps(
                    _row(
                        "000001",
                        "l2quote",
                        "09:25:05",
                        normalized={"stock_code": "000001", "last_price": 10.3, "pre_close": 10.0},
                        raw={"amount": 3_000_000, "lastPrice": 10.3, "lastClose": 10.0},
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )

    pool_path = tmp_path / "pool.csv"
    pool_path.write_text("股票代码,名称\n000001,平安银行\n", encoding="utf-8-sig")

    parser = build_parser()
    args = parser.parse_args(
        ["--raw", str(raw_path), "--output-dir", str(tmp_path), "--date", "20260605", "--name-pool", str(pool_path)]
    )
    paths = run_replay(args)

    assert paths["csv"].exists()
    assert paths["markdown"].exists()
    with paths["csv"].open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["stock"] == "000001"
    assert rows[0]["exchange"] == "SZ"
    assert rows[0]["stock_name"] == "平安银行"
    assert "Opening Auction Attitude Replay" in paths["markdown"].read_text(encoding="utf-8")


def test_load_stock_names_supports_chinese_pool_columns(tmp_path):
    pool_path = tmp_path / "pool.csv"
    pool_path.write_text("股票代码,名称,计划买入金额\n000700,模塑科技,50000\n600487.SH,亨通光电,50000\n", encoding="utf-8-sig")

    assert load_stock_names(str(pool_path)) == {
        "000700": "模塑科技",
        "600487": "亨通光电",
    }


def test_load_stock_names_from_tree_merges_csv_pool_names(tmp_path):
    current = tmp_path / "current"
    runs = tmp_path / "runs" / "2026-06-05"
    current.mkdir()
    runs.mkdir(parents=True)
    (current / "pool.csv").write_text("股票代码,名称\n002787,华源控股\n", encoding="utf-8-sig")
    (runs / "backup.csv").write_text("股票代码,名称\n000700,模塑科技\n600487,亨通光电\n", encoding="utf-8-sig")

    assert load_stock_names_from_tree(str(tmp_path)) == {
        "002787": "华源控股",
        "000700": "模塑科技",
        "600487": "亨通光电",
    }


def test_replay_prefers_auction_book_amount_over_raw_amount(tmp_path):
    raw_path = tmp_path / "opening_l2_raw.jsonl"
    raw_path.write_text(
        "\n".join(
            [
                json.dumps(
                    _row(
                        "000001",
                        "l2quote",
                        "09:24:50",
                        normalized={"stock_code": "000001", "last_price": 15.0, "pre_close": 15.0},
                        raw={
                            "lastPrice": 15.0,
                            "lastClose": 15.0,
                            "amount": 0.0,
                            "bidPrice": [15.0, 0.0],
                            "askPrice": [15.0, 0.0],
                            "bidVol": [100, 0],
                            "askVol": [100, 0],
                        },
                    )
                ),
                json.dumps(
                    _row(
                        "000001",
                        "l2quote",
                        "09:25:05",
                        normalized={"stock_code": "000001", "last_price": 15.38, "pre_close": 15.0},
                        raw={
                            "lastPrice": 15.38,
                            "lastClose": 15.0,
                            "amount": 0.0,
                            "bidPrice": [15.38, 0.0],
                            "askPrice": [15.38, 0.0],
                            "bidVol": [978, 0],
                            "askVol": [978, 301],
                        },
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )

    decisions, _summary = replay_raw_jsonl(raw_path)

    row = decisions[0]
    assert row["amount_source_at_final"] == "auction_book"
    assert row["amount_is_cumulative"] is True
    assert row["matched_volume_at_final"] == 978
    assert row["unmatched_sell_volume_at_final"] == 301
    assert row["amount_at_final"] == 15.38 * 978 * 100
    assert row["unmatched_sell_amount_at_final"] == 15.38 * 301 * 100
    assert row["low_to_final_amount_ratio"] > 0


def test_replay_does_not_treat_uncrossed_book_as_auction_match(tmp_path):
    raw_path = tmp_path / "opening_l2_raw.jsonl"
    raw_path.write_text(
        "\n".join(
            [
                json.dumps(
                    _row(
                        "000001",
                        "l2quote",
                        "09:24:57",
                        normalized={"stock_code": "000001", "last_price": 17.44, "pre_close": 17.8},
                        raw={
                            "lastPrice": 17.44,
                            "lastClose": 17.8,
                            "amount": 0.0,
                            "bidPrice": [17.44, 0.0],
                            "askPrice": [17.44, 0.0],
                            "bidVol": [26013, 874],
                            "askVol": [26013, 0],
                        },
                    )
                ),
                json.dumps(
                    _row(
                        "000001",
                        "l2quote",
                        "09:25:00",
                        normalized={"stock_code": "000001", "last_price": 18.27, "pre_close": 17.8},
                        raw={
                            "lastPrice": 18.27,
                            "lastClose": 17.8,
                            "amount": 81_261_306.0,
                            "pvolume": 4_447_800,
                            "volume": 44_478,
                            "bidPrice": [18.27, 18.26],
                            "askPrice": [18.28, 18.29],
                            "bidVol": [1677, 34],
                            "askVol": [76, 14],
                        },
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )

    decisions, _summary = replay_raw_jsonl(raw_path)

    row = decisions[0]
    assert row["amount_source_at_final"] == "raw_amount"
    assert row["matched_volume_at_final"] == 4_447_800
    assert row["unmatched_buy_volume_at_final"] == 0.0
    assert row["unmatched_sell_volume_at_final"] == 0.0
    assert row["amount_at_final"] == 81_261_306.0


def test_build_score_config_from_args_applies_threshold_overrides():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--raw",
            "dummy.jsonl",
            "--min-final-gap-pct",
            "0.01",
            "--min-low-to-final-lift-pct",
            "0.004",
            "--min-money-lift-ratio",
            "0.2",
            "--strong-money-lift-ratio",
            "0.4",
        ]
    )

    config = build_score_config_from_args(args)

    assert config.min_final_gap_pct == 0.01
    assert config.min_low_to_final_lift_pct == 0.004
    assert config.min_money_lift_ratio == 0.2
    assert config.strong_money_lift_ratio == 0.4
