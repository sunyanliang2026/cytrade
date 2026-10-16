import ast
import sys
from pathlib import Path

from strategies.large_order_limit_up_buy import qmt_internal_probe as probe


class FakeXtData:
    def __init__(self, snapshot=None, fail_kind=None):
        self.snapshot = snapshot if snapshot is not None else {probe.STOCK_CODE: {"time": 1}}
        self.fail_kind = fail_kind
        self.callbacks = {}
        self.unsubscribed = []

    def subscribe_quote(self, code, period, callback):
        assert code == probe.STOCK_CODE
        if period == self.fail_kind:
            raise RuntimeError("unavailable")
        sub_id = len(self.callbacks) + 1
        self.callbacks[period] = (sub_id, callback)
        return sub_id

    def get_full_tick(self, codes):
        assert codes == [probe.STOCK_CODE]
        return self.snapshot

    def unsubscribe_quote(self, sub_id):
        self.unsubscribed.append(sub_id)


def test_native_entry_subscribes_reports_data_and_unsubscribes(capsys, monkeypatch):
    xtdata = FakeXtData()
    monkeypatch.setattr(probe, "RUN_SECONDS", 0)
    assert probe.main(xtdata) == 0
    assert set(xtdata.callbacks) == set(probe.PERIODS)
    assert len(xtdata.unsubscribed) == len(probe.PERIODS)
    assert not probe.STATE

    probe.STATE.update({
        "counts": dict((kind, 0) for kind in probe.PERIODS),
        "first_seen": dict((kind, False) for kind in probe.PERIODS),
        "last_callback": {},
        "last_summary": probe.time.monotonic(),
    })
    xtdata.callbacks["l2order"][1]({probe.STOCK_CODE: [
        {"time": 1, "price": 10.0, "volume": 100},
        {"time": 2, "price": 10.0, "volume": 200},
    ]})
    assert probe.STATE["counts"]["l2order"] == 2
    probe.STATE.clear()

    output = capsys.readouterr().out
    assert "QMT_PROBE SNAPSHOT stock=000001.SZ available=True" in output
    assert "SCRIPT_START mode=native_python" in output
    assert "CALLBACK_FIRST kind=l2order rows=2" in output
    assert "SUBSCRIBE_END subscriptions=5" in output
    assert "STOP_END" in output


def test_failed_subscription_does_not_prevent_other_read_only_feeds(capsys, monkeypatch):
    xtdata = FakeXtData(snapshot={}, fail_kind="l2orderqueue")
    monkeypatch.setattr(probe, "RUN_SECONDS", 0)
    assert probe.main(xtdata) == 0
    output = capsys.readouterr().out
    assert "SUBSCRIBE kind=l2orderqueue failed" in output
    assert "SNAPSHOT stock=000001.SZ available=False" in output
    assert len(xtdata.unsubscribed) == len(probe.PERIODS) - 1


def test_no_subscriptions_exits_with_failure(capsys, monkeypatch):
    xtdata = FakeXtData()
    monkeypatch.setattr(xtdata, "subscribe_quote", lambda *args, **kwargs: 0)
    assert probe.main(xtdata) == 1
    assert "SUBSCRIBE_END subscriptions=0" in capsys.readouterr().out
    assert not xtdata.unsubscribed


def test_probe_is_python36_syntax_and_has_no_trading_calls():
    source = Path(probe.__file__).read_text(encoding="ascii")
    tree = ast.parse(source, feature_version=(3, 6))
    calls = [node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert not {"passorder", "order_stock", "order_stock_async", "cancel_order_stock"}.intersection(calls)
    assert "XtQuantTrader(" not in source
    assert 'if __name__ == "__main__":' in source
    assert source.startswith("# coding: utf-8")


def test_probe_log_path_uses_qmt_userdata_argument(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    monkeypatch.setattr(sys, "argv", ["L2TEST.py", str(tmp_path), "timestamp"])
    monkeypatch.setattr(probe, "LOG_PATH", None)

    probe._log("PATH_CHECK")

    assert probe.LOG_PATH == probe._resolve_log_path()
    assert "QMT_PROBE PATH_CHECK" in Path(probe.LOG_PATH).read_text(encoding="ascii")
