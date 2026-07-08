import csv
import json
from datetime import datetime

from core.l2_models import L2OrderEvent, L2QuoteEvent, L2TransactionEvent
from strategies.opening_auction_attitude.scripts.probe_l2 import OpeningAuctionL2Recorder, load_codes, record_with_modes


def test_opening_auction_l2_recorder_writes_raw_summary_and_schema(tmp_path):
    recorder = OpeningAuctionL2Recorder(tmp_path, capture_start="09:15:00", capture_end="09:35:00")
    event_time = datetime(2026, 6, 4, 9, 24, 58, 120000)
    open_time = datetime(2026, 6, 4, 9, 30, 30)

    recorder.record_many(
        "l2quote",
        "early",
        {
            "000001": L2QuoteEvent(
                stock_code="000001",
                last_price=10.1,
                pre_close=10.0,
                event_time=event_time,
                recv_time=event_time,
                raw_xt_fields={"time": 1770000000000, "lastPrice": 10.1},
            )
        },
    )
    recorder.record_many(
        "l2transaction",
        "early",
        {
            "000001": [
                L2TransactionEvent(
                    stock_code="000001",
                    price=10.1,
                    volume=20_000,
                    amount=202_000,
                    side="BUY",
                    event_time=event_time,
                    recv_time=event_time,
                    raw_xt_fields={"tradeFlag": 1, "amount": 202_000},
                )
            ]
        },
    )
    recorder.record_many(
        "l2order",
        "early",
        {
            "000001": [
                L2OrderEvent(
                    stock_code="000001",
                    price=10.1,
                    volume=30_000,
                    amount=303_000,
                    side="BUY",
                    entrust_direction=1,
                    event_time=event_time,
                    recv_time=event_time,
                    raw_xt_fields={"entrustDirection": 1, "amount": 303_000},
                )
            ]
        },
    )
    recorder.record_many(
        "l2transaction",
        "early",
        {
            "000001": [
                L2TransactionEvent(
                    stock_code="000001",
                    price=10.2,
                    volume=10_000,
                    amount=102_000,
                    side="SELL",
                    event_time=open_time,
                    recv_time=open_time,
                    raw_xt_fields={"tradeFlag": 2, "amount": 102_000},
                )
            ]
        },
    )

    recorder.write_outputs()
    recorder.close()

    raw_lines = recorder.raw_path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 4
    assert json.loads(raw_lines[0])["in_capture_window"] is True
    assert json.loads(raw_lines[0])["in_auction"] is True
    assert json.loads(raw_lines[0])["in_final_10s"] is True
    assert json.loads(raw_lines[-1])["phase"] == "open_first_5m"

    with recorder.summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["stock"] == "000001"
    assert rows[0]["l2_subscribe_mode"] == "early"
    assert rows[0]["has_l2_capture"] == "True"
    assert rows[0]["has_l2_auction"] == "True"
    assert rows[0]["has_l2_2450_2500"] == "True"
    assert rows[0]["has_l2_open_5m"] == "True"
    assert rows[0]["l2quote_count_capture"] == "1"
    assert rows[0]["l2quote_count_auction"] == "1"
    assert rows[0]["l2quote_count_10s"] == "1"
    assert rows[0]["l2transaction_count_capture"] == "2"
    assert rows[0]["l2transaction_count_auction"] == "1"
    assert rows[0]["l2transaction_count_10s"] == "1"
    assert rows[0]["l2transaction_count_open_5m"] == "1"
    assert rows[0]["l2order_count_10s"] == "1"
    assert rows[0]["big_trade_amount_10w"] == "202000.0"
    assert rows[0]["big_buy_amount_10s"] == "202000.0"
    assert rows[0]["big_buy_order_amount_10s"] == "303000.0"

    schema = json.loads(recorder.schema_path.read_text(encoding="utf-8"))
    assert schema["l2quote"][0]["field"] in {"time", "lastPrice"}


def test_opening_auction_l2_record_with_modes_splits_early_and_delayed(tmp_path):
    recorder = OpeningAuctionL2Recorder(tmp_path)
    event_time = datetime(2026, 6, 4, 9, 24, 59)

    record_with_modes(
        recorder,
        "l2quote",
        {
            "000001": L2QuoteEvent(stock_code="000001", event_time=event_time, recv_time=event_time),
            "000002": L2QuoteEvent(stock_code="000002", event_time=event_time, recv_time=event_time),
        },
        lambda code: "early" if code == "000001" else "delayed",
    )
    rows = recorder.build_summary_rows()
    recorder.close()

    assert {(row["stock"], row["l2_subscribe_mode"]) for row in rows} == {
        ("000001", "early"),
        ("000002", "delayed"),
    }


def test_opening_auction_l2_recorder_writes_health_for_expected_codes(tmp_path):
    recorder = OpeningAuctionL2Recorder(tmp_path)
    event_time = datetime(2026, 6, 4, 9, 24, 59)
    recorder.set_expected_codes(["000001", "000002"])
    recorder.set_subscription_diagnostics(
        [
            {"stock": "000001", "kind": "l2order", "sub_id": 101, "status": "SUBSCRIBED"},
            {"stock": "000001", "kind": "l2transaction", "sub_id": 102, "status": "SUBSCRIBED"},
            {"stock": "000002", "kind": "l2order", "sub_id": 201, "status": "SUBSCRIBED"},
            {"stock": "000002", "kind": "l2transaction", "sub_id": 202, "status": "SUBSCRIBED"},
        ]
    )
    recorder.record_many(
        "l2order",
        "dynamic_small_pool",
        {
            "000001": [
                L2OrderEvent(
                    stock_code="000001",
                    price=10.1,
                    volume=1000,
                    amount=10_100,
                    side="BUY",
                    event_time=event_time,
                    recv_time=event_time,
                )
            ]
        },
    )

    recorder.write_outputs()
    recorder.close()
    recorder.record_many(
        "l2order",
        "dynamic_small_pool",
        {"000002": [L2OrderEvent(stock_code="000002", event_time=event_time, recv_time=event_time)]},
    )

    with recorder.summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        summary_rows = list(csv.DictReader(handle))
    assert {row["stock"] for row in summary_rows} == {"000001", "000002"}
    assert next(row for row in summary_rows if row["stock"] == "000002")["l2order_count_total"] == "0"

    with recorder.health_path.open("r", encoding="utf-8-sig", newline="") as handle:
        health_rows = list(csv.DictReader(handle))
    by_stock = {row["stock"]: row for row in health_rows}
    assert by_stock["000001"]["status"] == "SUBSCRIBED_WITH_EVENTS"
    assert by_stock["000002"]["status"] == "SUBSCRIBED_NO_EVENTS"
    assert by_stock["000002"]["l2order_sub_id"] == "201"


def test_opening_auction_l2_load_codes_from_text_and_csv(tmp_path):
    csv_path = tmp_path / "pool.csv"
    csv_path.write_text("股票代码,名称\n600000.SH,浦发银行\n000001,平安银行\n", encoding="utf-8-sig")

    codes = load_codes(csv_path=str(csv_path), codes_text="001259.SZ, 600604")

    assert codes == ["000001", "001259", "600000", "600604"]
