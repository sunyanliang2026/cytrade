from pathlib import Path

from strategies.main_seal_follow.scripts.run_managed_session import build_managed_settings, build_parser


def test_managed_session_build_settings_uses_separate_strategy_start_time(tmp_path: Path):
    args = build_parser().parse_args(
        [
            "--pool-output",
            str(tmp_path / "pool.csv"),
            "--strategy-start-time",
            "09:15",
            "--stop-time",
            "10:00",
            "--heartbeat-interval-sec",
            "20",
        ]
    )

    settings = build_managed_settings(args)

    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH == str((tmp_path / "pool.csv").resolve())
    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN is True
    assert settings.SESSION_START_TIME == "09:15"
    assert settings.SESSION_EXIT_TIME == "10:00"
    assert settings.RUNTIME_HEARTBEAT_INTERVAL_SEC == 20
    assert settings.LOAD_PREVIOUS_STATE_ON_START is False
