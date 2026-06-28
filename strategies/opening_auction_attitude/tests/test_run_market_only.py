import json
from datetime import datetime
from pathlib import Path

from core.l2_models import L2OrderEvent
from core.models import TickData
from strategies.opening_auction_attitude import AUCTION_STRONG_CONFIRMED, OpeningAuctionAttitudeStrategy
from strategy.models import StrategyConfig

from strategies.opening_auction_attitude.scripts.run_market_only import (
    BUY_PLAN_EVENT_NAME,
    DEFAULT_POOL,
    EVENT_NAME,
    RANKING_EVENT_NAME,
    FullPoolSnapshotRecorder,
    OpeningAuctionLimitUpScanner,
    PoolEntry,
    SnapshotTick,
    build_auction_rankings,
    build_buy_plan_rows,
    build_observe_settings,
    build_parser,
    build_session_time,
    build_strategy_configs,
    emit_auction_rankings_and_buy_plan,
    emit_auction_decisions,
    install_observe_strategies,
    parse_codes,
    parse_hhmmss,
    parse_snapshot_tick,
    resolve_observe_entries,
    write_auction_rankings,
    write_buy_plan,
)


def _ts(clock: str) -> datetime:
    hour, minute, second = [int(part) for part in clock.split(":")]
    return datetime(2026, 6, 5, hour, minute, second)


def test_parse_codes_accepts_commas_spaces_and_xt_suffixes():
    assert parse_codes(["000700.SZ, 600000", "700"]) == ["000700", "600000"]


def test_parse_hhmmss_and_build_session_time_accept_seconds():
    assert parse_hhmmss("09:25") == (9, 25, 0)
    assert parse_hhmmss("09:25:05") == (9, 25, 5)

    anchor = datetime(2026, 6, 5, 8, 30, 1)

    assert build_session_time(anchor, "09:25:05") == datetime(2026, 6, 5, 9, 25, 5)


def test_resolve_observe_entries_loads_names_from_pool_for_requested_codes(tmp_path: Path):
    pool = tmp_path / "pool.csv"
    pool.write_text(
        "stock_code,stock_name\n"
        "000700,\u6a21\u5851\u79d1\u6280\n"
        "600000,\u6d66\u53d1\u94f6\u884c\n",
        encoding="utf-8-sig",
    )

    entries = resolve_observe_entries(codes=["600000,000700"], pool_path=str(pool))

    assert entries == [
        PoolEntry("600000", "\u6d66\u53d1\u94f6\u884c"),
        PoolEntry("000700", "\u6a21\u5851\u79d1\u6280"),
    ]


def test_resolve_observe_entries_uses_pool_order_when_codes_empty(tmp_path: Path):
    pool = tmp_path / "pool.csv"
    pool.write_text(
        "\u80a1\u7968\u4ee3\u7801,\u80a1\u7968\u540d\u79f0\n"
        "700,\u6a21\u5851\u79d1\u6280\n"
        "600000,\u6d66\u53d1\u94f6\u884c\n",
        encoding="utf-8-sig",
    )

    entries = resolve_observe_entries(codes=[], pool_path=str(pool), max_count=1)

    assert entries == [PoolEntry("000700", "\u6a21\u5851\u79d1\u6280")]


def test_build_observe_settings_forces_dry_run_and_no_previous_state():
    args = build_parser().parse_args(
        [
            "--codes",
            "000700",
            "--stop-time",
            "09:25:05",
            "--heartbeat-interval-sec",
            "5",
        ]
    )

    settings = build_observe_settings(args)

    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN is True
    assert settings.LOAD_PREVIOUS_STATE_ON_START is False
    assert settings.LOG_SUMMARY_MODE is True
    assert settings.RUNTIME_HEARTBEAT_INTERVAL_SEC == 5
    assert settings.SESSION_EXIT_TIME == "09:25:05"


def test_runner_default_stop_time_covers_open_verify_window():
    args = build_parser().parse_args([])

    assert args.stop_time == "09:35:00"
    assert args.pool == DEFAULT_POOL
    assert args.pool.endswith("opening_auction_universe.csv")
    assert args.scan_start_time == "09:15:00"
    assert args.candidate_freeze_time == "09:24:30"
    assert args.snapshot_interval_sec == 2.0
    assert args.install_all is True


def test_runner_can_opt_into_dynamic_candidate_scanner():
    args = build_parser().parse_args(["--dynamic-candidates"])

    assert args.install_all is False


def test_morning_batch_uses_all_candidate_entry_and_run_artifact_paths():
    repo_root = Path(__file__).resolve().parents[3]
    text = (repo_root / "scripts" / "run" / "run_opening_auction_attitude_morning.bat").read_text(
        encoding="utf-8"
    )

    assert "--install-all" in text
    assert "--candidate-freeze-time" not in text
    assert "snapshot_full_pool.jsonl" in text
    assert "auction_rankings.csv" in text
    assert "auction_buy_plan.csv" in text
    assert "run_manifest.json" in text
    assert "real_order_sent=$false" in text


def test_parse_snapshot_tick_accepts_qmt_full_tick_fields():
    tick = parse_snapshot_tick(
        "000700.SZ",
        {"lastPrice": 11.0, "lastClose": 10.0, "amount": 1_200_000, "volume": 1000, "time": 1770000000000},
        recv_time=_ts("09:20:00"),
    )

    assert tick.stock_code == "000700"
    assert tick.last_price == 11.0
    assert tick.pre_close == 10.0
    assert tick.amount == 1_200_000
    assert tick.volume == 1000


def test_parse_snapshot_tick_uses_auction_book_price_when_last_price_missing():
    tick = parse_snapshot_tick(
        "000700",
        {
            "lastClose": 10.0,
            "bidPrice": [11.0, 0.0],
            "askPrice": [11.0, 0.0],
            "bidVol": [978, 0],
            "askVol": [978, 301],
        },
        recv_time=_ts("09:20:00"),
    )

    assert tick.last_price == 11.0
    assert tick.pre_close == 10.0


def test_limit_up_scanner_finds_snapshot_hit_once():
    calls = []

    def provider(codes):
        calls.append(list(codes))
        return {"000700": SnapshotTick("000700", last_price=11.0, pre_close=10.0, amount=2_000_000)}

    scanner = OpeningAuctionLimitUpScanner(
        [PoolEntry("000700", "\u6a21\u5851\u79d1\u6280")],
        snapshot_provider=provider,
        freeze_at=_ts("09:24:30"),
    )

    assert scanner.scan_once(_ts("09:20:00")) == [PoolEntry("000700", "\u6a21\u5851\u79d1\u6280")]
    assert scanner.scan_once(_ts("09:20:02")) == []
    assert calls == [["000700"]]
    assert scanner.candidate_count == 1


def test_limit_up_scanner_ignores_non_hit_snapshot():
    scanner = OpeningAuctionLimitUpScanner(
        [PoolEntry("000700", "\u6a21\u5851\u79d1\u6280")],
        snapshot_provider=lambda codes: {
            "000700": SnapshotTick("000700", last_price=10.98, pre_close=10.0, amount=2_000_000)
        },
        freeze_at=_ts("09:24:30"),
    )

    assert scanner.scan_once(_ts("09:20:00")) == []
    assert scanner.candidate_count == 0


def test_limit_up_scanner_does_not_add_after_freeze():
    scanner = OpeningAuctionLimitUpScanner(
        [PoolEntry("000700", "\u6a21\u5851\u79d1\u6280")],
        snapshot_provider=lambda codes: {
            "000700": SnapshotTick("000700", last_price=11.0, pre_close=10.0, amount=2_000_000)
        },
        freeze_at=_ts("09:24:30"),
    )

    assert scanner.scan_once(_ts("09:24:30")) == []
    assert scanner.scan_once(_ts("09:24:31")) == []
    assert scanner.candidate_count == 0


def test_limit_up_scanner_records_snapshot_jsonl(tmp_path: Path):
    snapshot_path = tmp_path / "snapshot_scan.jsonl"
    scanner = OpeningAuctionLimitUpScanner(
        [PoolEntry("000700", "\u6a21\u5851\u79d1\u6280")],
        snapshot_provider=lambda codes: {
            "000700": SnapshotTick(
                "000700",
                last_price=11.0,
                pre_close=10.0,
                amount=2_000_000,
                raw={"lastPrice": 11.0, "lastClose": 10.0},
            )
        },
        freeze_at=_ts("09:24:30"),
        snapshot_record_path=str(snapshot_path),
    )

    scanner.scan_once(_ts("09:20:00"))
    scanner.close()

    rows = [json.loads(line) for line in snapshot_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["stock"] == "000700"
    assert rows[0]["stock_name"] == "\u6a21\u5851\u79d1\u6280"
    assert rows[0]["last_price"] == 11.0
    assert rows[0]["limit_up_price"] == 11.0
    assert rows[0]["is_hit"] is True
    assert rows[0]["raw"]["lastPrice"] == 11.0


def test_full_pool_snapshot_recorder_records_all_returned_candidate_snapshots(tmp_path: Path):
    snapshot_path = tmp_path / "snapshot_scan.jsonl"
    calls = []

    def provider(codes):
        calls.append(list(codes))
        return {
            "000700": SnapshotTick(
                "000700",
                last_price=11.0,
                pre_close=10.0,
                amount=2_000_000,
                raw={"lastPrice": 11.0, "lastClose": 10.0},
            ),
            "600000": SnapshotTick(
                "600000",
                last_price=9.8,
                pre_close=10.0,
                amount=1_000_000,
                raw={"lastPrice": 9.8, "lastClose": 10.0},
            ),
        }

    recorder = FullPoolSnapshotRecorder(
        [PoolEntry("000700", "强势股"), PoolEntry("600000", "弱势股")],
        snapshot_provider=provider,
        snapshot_record_path=str(snapshot_path),
    )

    assert recorder.record_once(_ts("09:20:00")) == 2
    recorder.close()

    rows = [json.loads(line) for line in snapshot_path.read_text(encoding="utf-8").splitlines()]
    assert calls == [["000700", "600000"]]
    assert [row["stock"] for row in rows] == ["000700", "600000"]
    assert {row["record_mode"] for row in rows} == {"install_all_full_pool"}
    assert rows[0]["is_hit"] is True
    assert rows[1]["is_hit"] is False
    assert recorder.rows_written == 2


def test_install_observe_strategies_adds_opening_auction_strategy_instances():
    class FakeRunner:
        def __init__(self):
            self.strategies = []

        def add_strategy(self, strategy):
            self.strategies.append(strategy)

    configs = build_strategy_configs([PoolEntry("000700", "\u6a21\u5851\u79d1\u6280")])
    runner = FakeRunner()

    installed = install_observe_strategies(runner, configs)

    assert installed == 1
    assert isinstance(runner.strategies[0], OpeningAuctionAttitudeStrategy)
    assert runner.strategies[0].stock_code == "000700"
    assert runner.strategies[0].config.params["stock_name"] == "\u6a21\u5851\u79d1\u6280"


def test_emit_auction_decisions_logs_stock_name_and_observe_only_payload():
    class FakeRunner:
        def __init__(self, strategies):
            self._strategies = strategies

        def get_all_strategies(self):
            return list(self._strategies)

    class FakeLogger:
        def __init__(self):
            self.messages = []

        def info(self, template, *args):
            self.messages.append(template % args)

    strategy = install_and_get_strategy()
    logger = FakeLogger()

    emitted = emit_auction_decisions(FakeRunner([strategy]), logger)

    assert emitted == 1
    assert len(logger.messages) == 1
    prefix, payload_text = logger.messages[0].split(" ", 1)
    assert prefix == EVENT_NAME
    payload = json.loads(payload_text)
    assert payload["event_name"] == EVENT_NAME
    assert payload["stock_name"] == "\u6a21\u5851\u79d1\u6280"
    assert payload["observe_only"] is True
    assert payload["auction_label"] == AUCTION_STRONG_CONFIRMED


def test_build_auction_rankings_sorts_actionable_candidates_first():
    strong = install_and_get_strategy()
    weak = OpeningAuctionAttitudeStrategy(
        StrategyConfig(stock_code="600000", params={"stock_name": "弱势股", "instance_key": "600000"})
    )
    weak.on_tick(TickData(stock_code="600000", last_price=10.1, pre_close=10.0, amount=1_000_000, data_time=_ts("09:24:50")))
    weak.on_tick(TickData(stock_code="600000", last_price=10.1, pre_close=10.0, amount=1_100_000, data_time=_ts("09:25:05")))

    rankings = build_auction_rankings([weak, strong], min_plan_score=75.0)

    assert [row["stock_code"] for row in rankings] == ["000700", "600000"]
    assert rankings[0]["rank"] == 1
    assert rankings[0]["stock_name"] == "\u6a21\u5851\u79d1\u6280"
    assert rankings[0]["auction_label"] == AUCTION_STRONG_CONFIRMED
    assert rankings[0]["plan_eligible"] is True
    assert rankings[0]["has_order_confirmation"] is True
    assert rankings[1]["plan_eligible"] is False


def test_build_buy_plan_rows_is_plan_only_and_limited():
    rankings = [
        {"rank": 1, "stock_code": "000700", "stock_name": "强势股", "plan_eligible": True, "reference_price": 10.3, "auction_rank_score": 112.5, "auction_label": AUCTION_STRONG_CONFIRMED, "reason": "ok"},
        {"rank": 2, "stock_code": "600000", "stock_name": "次强股", "plan_eligible": True, "reference_price": 9.9, "auction_rank_score": 90.0, "auction_label": AUCTION_STRONG_CONFIRMED, "reason": "ok"},
    ]

    plan_rows = build_buy_plan_rows(rankings, top_n=1, plan_amount=50000)

    assert plan_rows == [
        {
            "rank": 1,
            "stock_code": "000700",
            "stock_name": "强势股",
            "plan_amount": 50000.0,
            "reference_price": 10.3,
            "auction_rank_score": 112.5,
            "auction_label": AUCTION_STRONG_CONFIRMED,
            "reason": "ok",
            "status": "PLAN_ONLY",
            "observe_only": True,
            "real_order_sent": False,
        }
    ]


def test_write_auction_rankings_and_buy_plan_csv(tmp_path: Path):
    ranking_path = tmp_path / "ranking.csv"
    plan_path = tmp_path / "plan.csv"
    rankings = build_auction_rankings([install_and_get_strategy()], min_plan_score=75.0)
    plan_rows = build_buy_plan_rows(rankings, top_n=1, plan_amount=0)

    assert write_auction_rankings(str(ranking_path), rankings) == str(ranking_path)
    assert write_buy_plan(str(plan_path), plan_rows) == str(plan_path)

    ranking_text = ranking_path.read_text(encoding="utf-8-sig")
    plan_text = plan_path.read_text(encoding="utf-8-sig")
    assert "auction_rank_score" in ranking_text
    assert "000700" in ranking_text
    assert "PLAN_ONLY" in plan_text
    assert "False" in plan_text


def test_emit_auction_rankings_and_buy_plan_logs_and_writes_outputs(tmp_path: Path):
    class FakeRunner:
        def __init__(self, strategies):
            self._strategies = strategies

        def get_all_strategies(self):
            return list(self._strategies)

    class FakeLogger:
        def __init__(self):
            self.messages = []

        def info(self, template, *args):
            self.messages.append(template % args)

    ranking_path = tmp_path / "ranking.csv"
    plan_path = tmp_path / "plan.csv"
    logger = FakeLogger()

    ranking_count, plan_count = emit_auction_rankings_and_buy_plan(
        FakeRunner([install_and_get_strategy()]),
        logger=logger,
        ranking_output_path=str(ranking_path),
        buy_plan_output_path=str(plan_path),
        buy_plan_top_n=1,
        buy_plan_min_score=75,
        buy_plan_amount=0,
    )

    assert ranking_count == 1
    assert plan_count == 1
    assert ranking_path.exists()
    assert plan_path.exists()
    assert logger.messages[0].startswith(RANKING_EVENT_NAME)
    assert logger.messages[1].startswith(BUY_PLAN_EVENT_NAME)
    _, plan_payload_text = logger.messages[1].split(" ", 1)
    plan_payload = json.loads(plan_payload_text)
    assert plan_payload["observe_only"] is True
    assert plan_payload["real_order_sent"] is False


def install_and_get_strategy() -> OpeningAuctionAttitudeStrategy:
    configs = build_strategy_configs([PoolEntry("000700", "\u6a21\u5851\u79d1\u6280")])

    class FakeRunner:
        def __init__(self):
            self.strategies = []

        def add_strategy(self, strategy):
            self.strategies.append(strategy)

    runner = FakeRunner()
    install_observe_strategies(runner, configs)
    strategy = runner.strategies[0]
    strategy.on_tick(TickData(stock_code="000700", last_price=10.0, pre_close=10.0, amount=1_000_000, data_time=_ts("09:24:50")))
    strategy.on_tick(TickData(stock_code="000700", last_price=10.3, pre_close=10.0, amount=3_000_000, data_time=_ts("09:25:05")))
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000700",
            price=10.3,
            volume=10_000,
            amount=2_000_000,
            side="BUY",
            event_time=_ts("09:25:00"),
        )
    )
    return strategy
