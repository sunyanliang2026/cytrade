from datetime import datetime
from pathlib import Path

from scripts.run.run_main_seal_follow_monitor_session import (
    build_monitor_settings,
    build_parser,
    build_pool_args,
    build_session_time,
    parse_hhmm,
    resolve_review_run_id,
    resolve_runtime_start_time,
    should_collect_pool,
)


def test_monitor_session_parse_hhmm_and_build_session_time():
    assert parse_hhmm("08:50") == (8, 50)

    anchor = datetime(2026, 5, 25, 7, 30, 12)
    target = build_session_time(anchor, "10:00")

    assert target == datetime(2026, 5, 25, 10, 0, 0)


def test_monitor_session_build_monitor_settings_forces_dry_run(tmp_path: Path):
    args = build_parser().parse_args(
        [
            "--pool-output",
            str(tmp_path / "pool.csv"),
            "--stop-time",
            "10:00",
            "--heartbeat-interval-sec",
            "15",
        ]
    )

    settings = build_monitor_settings(args)

    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN is True
    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH == str((tmp_path / "pool.csv").resolve())
    assert settings.LOG_SUMMARY_MODE is True
    assert settings.SESSION_START_TIME == "09:15"
    assert settings.RUNTIME_HEARTBEAT_INTERVAL_SEC == 15
    assert settings.SESSION_EXIT_TIME == "10:00"
    assert settings.LOAD_PREVIOUS_STATE_ON_START is False


def test_monitor_session_defaults_to_separated_times():
    args = build_parser().parse_args([])

    assert args.pool_time == "08:50"
    assert args.strategy_start_time == "09:15"
    assert args.stop_time == "11:00"
    assert args.no_post_review is False


def test_monitor_session_build_pool_args_uses_wrapper_options(tmp_path: Path):
    output_path = tmp_path / "pool.csv"
    source_config = tmp_path / "sources.json"
    args = build_parser().parse_args(
        [
            "--pool-source",
            "iwencai",
            "--pool-output",
            str(output_path),
            "--pool-source-config",
            str(source_config),
            "--amount",
            "2500",
            "--max-count",
            "50",
            "--strict-sources",
            "--no-backup",
            "--no-market-day-check",
        ]
    )

    pool_args = build_pool_args(args)

    assert pool_args.source == "iwencai"
    assert Path(pool_args.output) == output_path
    assert Path(pool_args.source_config) == source_config
    assert pool_args.amount == 2500.0
    assert pool_args.max_count == 50
    assert pool_args.strict_sources is True
    assert pool_args.no_backup is True
    assert pool_args.market_day_only is False


def test_monitor_session_can_skip_pool_collection(tmp_path: Path):
    args = build_parser().parse_args(
        [
            "--pool-output",
            str(tmp_path / "manual_pool.csv"),
            "--skip-pool-collect",
        ]
    )

    assert should_collect_pool(args) is False


def test_monitor_session_runtime_start_time_defaults_to_pool_time():
    args = build_parser().parse_args(["--pool-time", "08:50", "--strategy-start-time", ""])

    assert resolve_runtime_start_time(args) == "08:50"


def test_monitor_session_runtime_start_time_can_be_overridden():
    args = build_parser().parse_args(["--pool-time", "08:50", "--strategy-start-time", "09:15"])

    assert resolve_runtime_start_time(args) == "09:15"


def test_monitor_session_review_run_id_defaults_to_today():
    args = build_parser().parse_args([])

    assert resolve_review_run_id(args, datetime(2026, 6, 2, 9, 0)) == "2026-06-02"


def test_monitor_session_review_run_id_can_be_overridden():
    args = build_parser().parse_args(["--review-run-id", "2026-05-25"])

    assert resolve_review_run_id(args, datetime(2026, 6, 2, 9, 0)) == "2026-05-25"
