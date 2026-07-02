from pathlib import Path
from types import SimpleNamespace

from config.settings import Settings
from strategies.juejin_sell_strategy.scripts import run_managed_session as managed_session
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
            "--heartbeat-stable-repeat",
            "8",
        ]
    )

    assert args.require_live is False
    assert args.confirm_live is False

    baseline_sell_dry_run = Settings().CYTRADE_JUEJIN_SELL_DRY_RUN
    settings = build_managed_settings(args)

    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH == str(csv_path.resolve())
    assert settings.CYTRADE_JUEJIN_SELL_DRY_RUN is baseline_sell_dry_run
    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN is baseline_sell_dry_run
    assert settings.SESSION_START_TIME == "09:15"
    assert settings.SESSION_EXIT_TIME == "15:05"
    assert settings.RUNTIME_HEARTBEAT_INTERVAL_SEC == 20
    assert settings.RUNTIME_HEARTBEAT_STABLE_REPEAT == 8
    assert settings.LOAD_PREVIOUS_STATE_ON_START is False


def test_juejin_managed_settings_bridge_sell_dry_run_to_shared_executor(tmp_path: Path, monkeypatch):
    class FakeSettings:
        def __init__(self, **overrides):
            self.CYTRADE_JUEJIN_SELL_DRY_RUN = False
            self.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN = True
            self.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH = ""
            self.LOG_SUMMARY_MODE = False
            self.SESSION_START_TIME = "09:25"
            self.SESSION_EXIT_TIME = "23:00"
            self.RUNTIME_HEARTBEAT_INTERVAL_SEC = 30
            self.RUNTIME_HEARTBEAT_STABLE_REPEAT = 4
            self.LOAD_PREVIOUS_STATE_ON_START = True
            for key, value in overrides.items():
                setattr(self, key, value)

    monkeypatch.setattr(managed_session, "Settings", FakeSettings)

    csv_path = tmp_path / "sell_10.csv"
    csv_path.write_text("symbol,exp,sellvol,nick\nSZSE.000977,0,200,test\n", encoding="utf-8")
    args = build_parser().parse_args(["--csv", str(csv_path)])

    settings = managed_session.build_managed_settings(args)

    assert settings.CYTRADE_JUEJIN_SELL_DRY_RUN is False
    assert settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN is False


def test_juejin_managed_session_skips_missing_csv(tmp_path: Path):
    args = build_parser().parse_args(["--csv", str(tmp_path / "missing.csv")])

    assert run_managed_session(args) == "skipped_missing_csv"


def test_juejin_managed_session_defaults_to_quiet_stable_heartbeat():
    args = build_parser().parse_args([])

    assert args.heartbeat_interval_sec == 30
    assert args.heartbeat_stable_repeat == 20


def test_juejin_managed_session_require_live_fails_when_config_is_dry_run(tmp_path: Path, monkeypatch):
    csv_path = tmp_path / "sell_10.csv"
    csv_path.write_text("symbol,exp,sellvol,nick\nSZSE.000977,0,200,test\n", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "--csv",
            str(csv_path),
            "--stop-time",
            "23:59",
            "--require-live",
            "--confirm-live",
        ]
    )

    monkeypatch.setattr(
        managed_session,
        "build_managed_settings",
        lambda _args: SimpleNamespace(
            CYTRADE_JUEJIN_SELL_DRY_RUN=True,
            CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=True,
            ACCOUNT_ID="test-account",
            ACCOUNT_TYPE="STOCK",
            QMT_PATH="C:/qmt",
            SESSION_START_TIME="09:15",
            SESSION_EXIT_TIME="23:59",
            LOG_SUMMARY_MODE=True,
        ),
    )

    assert managed_session.run_managed_session(args) == "skipped_require_live_dry_run"


def test_juejin_managed_session_live_config_requires_confirm_live(tmp_path: Path, monkeypatch):
    csv_path = tmp_path / "sell_10.csv"
    csv_path.write_text("symbol,exp,sellvol,nick\nSZSE.000977,0,200,test\n", encoding="utf-8")
    args = build_parser().parse_args(["--csv", str(csv_path), "--stop-time", "23:59"])

    monkeypatch.setattr(
        managed_session,
        "build_managed_settings",
        lambda _args: SimpleNamespace(
            CYTRADE_JUEJIN_SELL_DRY_RUN=False,
            CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=False,
            ACCOUNT_ID="test-account",
            ACCOUNT_TYPE="STOCK",
            QMT_PATH="C:/qmt",
            SESSION_START_TIME="09:15",
            SESSION_EXIT_TIME="23:59",
            LOG_SUMMARY_MODE=True,
        ),
    )

    assert managed_session.run_managed_session(args) == "skipped_live_not_confirmed"
