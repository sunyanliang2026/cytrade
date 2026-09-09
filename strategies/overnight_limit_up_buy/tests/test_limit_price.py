from strategies.overnight_limit_up_buy import limit_price
from datetime import date, datetime, timedelta

import pandas as pd

from strategies.overnight_limit_up_buy.limit_price import LimitUpPriceProvider, extract_limit_up_price, to_xt_code


class _FakeXtData:
    def __init__(self):
        self.connected = False
        self.detail = {}
        self.tick = {}

    def connect(self):
        self.connected = True

    def get_instrument_detail(self, xt_code, **kwargs):
        return self.detail.get(xt_code, {})

    def get_full_tick(self, codes):
        return {code: self.tick.get(code, {}) for code in codes}

    def get_market_data_ex(self, **kwargs):
        code = kwargs["stock_list"][0]
        close = self.detail.get(code, {}).get("TestClose")
        if close is None:
            close = self.tick.get(code, {}).get("TestClose")
        return {code: pd.DataFrame({"close": [close]}, index=[date.today() - timedelta(days=1)])} if close else {}


def test_extract_limit_up_price_supports_xtdata_detail_key():
    assert extract_limit_up_price({"UpStopPrice": 11.03}) == 11.03


def test_to_xt_code_adds_market_suffix():
    assert to_xt_code("603005") == "603005.SH"
    assert to_xt_code("002185") == "002185.SZ"


def test_provider_uses_instrument_detail(monkeypatch):
    fake = _FakeXtData()
    fake.detail["603005.SH"] = {
        "UpStopPrice": 12.34,
        "TestClose": 11.2181818182,
        "TradingDay": date.today().strftime("%Y%m%d"),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    provider = LimitUpPriceProvider()

    assert provider.get_limit_up_price("603005") == 12.34
    assert fake.connected is True


def test_provider_exact_uses_current_instrument_detail_without_inference(monkeypatch):
    fake = _FakeXtData()
    fake.detail["002579.SZ"] = {
        "UpStopPrice": 17.06,
        "TestClose": 15.51,
        "TradingDay": date.today().strftime("%Y%m%d"),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    provider = LimitUpPriceProvider()

    assert provider.get_exact_limit_up_price("002579") == 17.06


def test_provider_exact_returns_zero_when_explicit_price_is_missing(monkeypatch):
    fake = _FakeXtData()
    fake.detail["002579.SZ"] = {
        "TestClose": 15.51,
        "TradingDay": date.today().strftime("%Y%m%d"),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    assert LimitUpPriceProvider().get_exact_limit_up_price("002579") == 0.0


def test_provider_falls_back_to_full_tick(monkeypatch):
    fake = _FakeXtData()
    fake.tick["002185.SZ"] = {
        "upLimitPrice": 9.87,
        "TestClose": 8.9727272727,
        "time": int(datetime.now().timestamp() * 1000),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    assert LimitUpPriceProvider().get_limit_up_price("002185") == 9.87


def test_provider_prefers_current_snapshot_over_stale_detail(monkeypatch):
    fake = _FakeXtData()
    fake.detail["600108.SH"] = {
        "UpStopPrice": 5.50,
        "TradingDay": date.today().strftime("%Y%m%d"),
    }
    fake.tick["600108.SH"] = {
        "upperLimit": 5.83,
        "TestClose": 5.3,
        "time": int(datetime.now().timestamp() * 1000),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    assert LimitUpPriceProvider().get_limit_up_price("600108") == 5.83


def test_provider_rejects_detail_from_previous_day(monkeypatch):
    fake = _FakeXtData()
    fake.detail["600108.SH"] = {
        "UpStopPrice": 5.50,
        "TradingDay": (date.today() - timedelta(days=1)).strftime("%Y%m%d"),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    assert LimitUpPriceProvider().get_limit_up_price("600108") == 0.0


def test_provider_ignores_snapshot_from_previous_day(monkeypatch):
    fake = _FakeXtData()
    fake.tick["600108.SH"] = {
        "upperLimit": 5.50,
        "TestClose": 5.3,
        "time": int((datetime.now() - timedelta(days=1)).timestamp() * 1000),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    assert LimitUpPriceProvider().get_limit_up_price("600108") == 5.83


def test_provider_uses_detail_preclose_when_daily_history_is_stale(monkeypatch):
    fake = _FakeXtData()
    fake.detail["603999.SH"] = {
        "PreClose": 6.63,
        "UpStopPrice": 7.29,
        "TradingDay": "20260908",
    }
    fake.tick["603999.SH"] = {
        "lastClose": 6.63,
        "lastPrice": 7.29,
        "time": int(datetime(2026, 9, 8, 15, 0).timestamp() * 1000),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    provider = LimitUpPriceProvider()

    assert provider.get_limit_up_quote("603999", date(2026, 9, 9)) == (8.02, 7.29)


def test_provider_rejects_tick_from_different_session(monkeypatch):
    fake = _FakeXtData()
    fake.detail["603999.SH"] = {"PreClose": 6.63, "TradingDay": "20260908"}
    fake.tick["603999.SH"] = {
        "lastClose": 6.63,
        "lastPrice": 6.63,
        "time": int(datetime(2026, 9, 7, 15, 0).timestamp() * 1000),
    }
    monkeypatch.setattr(limit_price, "xtdata", fake)
    monkeypatch.setattr(limit_price, "_XT_AVAILABLE", True)

    assert LimitUpPriceProvider().get_limit_up_price("603999", date(2026, 9, 9)) == 0.0
