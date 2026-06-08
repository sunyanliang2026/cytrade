import json
from datetime import datetime
from pathlib import Path

from core.l2_models import L2OrderEvent
from core.models import TickData
from strategy.opening_auction_attitude import AUCTION_STRONG_CONFIRMED, OpeningAuctionAttitudeStrategy

from scripts.run.run_opening_auction_attitude_market_only import (
    DEFAULT_POOL,
    EVENT_NAME,
    PoolEntry,
    build_observe_settings,
    build_parser,
    build_session_time,
    build_strategy_configs,
    emit_auction_decisions,
    install_observe_strategies,
    parse_codes,
    parse_hhmmss,
    resolve_observe_entries,
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
