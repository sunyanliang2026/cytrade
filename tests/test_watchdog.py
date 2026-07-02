from datetime import datetime

from monitor.watchdog import Watchdog


def _fake_datetime_at(clock: str):
    hour, minute = [int(part) for part in clock.split(":")]

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 2, hour, minute)

    return _FakeDateTime


def test_watchdog_data_subscription_window_excludes_call_auction_gap_and_lunch(monkeypatch):
    monkeypatch.setattr("monitor.watchdog.datetime", _fake_datetime_at("09:27"))
    assert Watchdog._is_trading_time() is False

    monkeypatch.setattr("monitor.watchdog.datetime", _fake_datetime_at("11:35"))
    assert Watchdog._is_trading_time() is False

    monkeypatch.setattr("monitor.watchdog.datetime", _fake_datetime_at("09:30"))
    assert Watchdog._is_trading_time() is True

    monkeypatch.setattr("monitor.watchdog.datetime", _fake_datetime_at("13:00"))
    assert Watchdog._is_trading_time() is True
