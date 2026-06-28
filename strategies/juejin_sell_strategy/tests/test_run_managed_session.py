from pathlib import Path

from config.settings import Settings
from strategies.juejin_sell_strategy.scripts.run_managed_session import (
    build_managed_settings,
    build_parser,
    run_managed_session,
)


def test_juejin_managed_settings_use_csv_without_forcing_dry_run(tmp_path: Path):
    csv_path = tmp_path / "sell_10.csv"
    csv_path.write_text("symbol,exp,sellvol,nick\nSZSE.000977,0,200,浪潮信息\n", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "--csv",
            str(csv_path),
            "--strategy-start-time",
            "09:15",
            "--stop-time",
            "15:05",
            "--heartbeat-interval-sec",
            "20",
        ]
    )

    baseline_dry_run = Settings().CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN
    settings = build_managed_settings(args)

    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH == str(csv_path.resolve())
    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN is baseline_dry_run
    assert settings.SESSION_START_TIME == "09:15"
    assert settings.SESSION_EXIT_TIME == "15:05"
    assert settings.RUNTIME_HEARTBEAT_INTERVAL_SEC == 20
    assert settings.LOAD_PREVIOUS_STATE_ON_START is False


def test_juejin_managed_session_skips_missing_csv(tmp_path: Path):
    args = build_parser().parse_args(["--csv", str(tmp_path / "missing.csv")])

    assert run_managed_session(args) == "skipped_missing_csv"
