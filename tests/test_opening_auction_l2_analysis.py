import json
from datetime import datetime

from scripts.probe.analyze_opening_auction_l2_probe import analyze_raw_jsonl, render_markdown, run_analysis, build_parser


def _row(stock, kind, phase, event_time, **extra):
    row = {
        "recv_time": event_time,
        "event_time": event_time,
        "stock": stock,
        "kind": kind,
        "subscribe_mode": "early",
        "in_capture_window": True,
        "in_auction": phase.startswith("auction"),
        "in_final_10s": phase == "auction_final_10s",
        "in_open_5m": phase == "open_first_5m",
        "phase": phase,
        "normalized": {},
        "raw": {},
    }
    row.update(extra)
    return row


def test_analyze_raw_jsonl_summarizes_coverage_and_fields(tmp_path):
    raw_path = tmp_path / "opening_l2_raw.jsonl"
    event_time = datetime(2026, 6, 5, 9, 24, 58).isoformat(sep=" ")
    open_time = datetime(2026, 6, 5, 9, 30, 1).isoformat(sep=" ")
    rows = [
        _row("000001", "l2quote", "auction_final_10s", event_time),
        _row(
            "000001",
            "l2transaction",
            "auction_final_10s",
            event_time,
            normalized={"trade_flag": 1, "trade_type": 0, "side": "BUY"},
            raw={"tradeFlag": 1, "tradeType": 0},
        ),
        _row(
            "600000",
            "l2order",
            "auction_final_10s",
            event_time,
            normalized={"entrust_direction": 2, "entrust_type": 1, "side": "SELL"},
            raw={"entrustDirection": 2, "entrustType": 1},
        ),
        _row("600000", "l2quote", "open_first_5m", open_time),
    ]
    raw_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    analysis = analyze_raw_jsonl(raw_path)

    assert analysis["totals"]["events"] == 4
    assert analysis["totals"]["covered_2450_2500_rows"] == 2
    assert analysis["totals"]["covered_open_5m_rows"] == 1
    assert analysis["totals"]["by_kind"] == {"l2order": 1, "l2quote": 2, "l2transaction": 1}
    assert analysis["totals"]["by_exchange"] == {"SH": 2, "SZ": 2}
    assert analysis["field_distributions"]["l2transaction.tradeFlag"] == {"1": 1}
    assert analysis["field_distributions"]["l2order.entrustDirection"] == {"2": 1}

    markdown = render_markdown(analysis)
    assert "Opening Auction L2 Probe Analysis" in markdown
    assert "| 000001 | early | SZ | True | False |" in markdown


def test_run_analysis_writes_json_and_markdown(tmp_path):
    raw_path = tmp_path / "opening_l2_raw.jsonl"
    event_time = datetime(2026, 6, 5, 9, 24, 58).isoformat(sep=" ")
    raw_path.write_text(json.dumps(_row("000001", "l2quote", "auction_final_10s", event_time)), encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(["--input-dir", str(tmp_path)])
    paths = run_analysis(args)

    assert paths["json"].exists()
    assert paths["markdown"].exists()
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["totals"]["events"] == 1
