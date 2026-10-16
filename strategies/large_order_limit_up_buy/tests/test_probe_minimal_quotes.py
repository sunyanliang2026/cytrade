from strategies.large_order_limit_up_buy.scripts.probe_minimal_quotes import Collector, event_seconds, normalize_codes, run


def test_collector_compares_three_streams(monkeypatch):
    from strategies.large_order_limit_up_buy.scripts import probe_minimal_quotes as probe
    clock = iter([100.2, 101.0, 104.2])
    monkeypatch.setattr(probe.time, "time", lambda: next(clock))
    collector = Collector()
    collector.callback("tick")({"600241.SH": {"time": 100}})
    collector.callback("l2quote")({"600241.SH": {"time": 100}})
    collector.callback("l2order")({"600241.SH": [{"time": 100}, {"time": 103}]})
    streams = {row["kind"]: row for row in collector.summary(["600241.SH"])}
    assert streams["tick"]["age_p50_ms"] == 200
    assert streams["l2quote"]["age_p50_ms"] == 1000
    assert streams["l2order"]["age_over_1s"] == 2
    assert event_seconds(float("nan")) is None
    assert normalize_codes("600241,000892,600241.SH") == ["600241.SH", "000892.SZ"]


def test_probe_only_subscribes_required_streams_and_unsubscribes(tmp_path):
    class FakeXtdata:
        def __init__(self):
            self.calls = []
            self.unsubscribed = []

        def connect(self):
            pass

        def subscribe_quote(self, code, *, period, count, callback):
            self.calls.append((code, period, count))
            return len(self.calls)

        def unsubscribe_quote(self, sub_id):
            self.unsubscribed.append(sub_id)

    fake = FakeXtdata()
    report = run(fake, ["600241.SH", "000892.SZ"], 0, tmp_path / "report.json")
    assert len(fake.calls) == 6
    assert {period for _, period, _ in fake.calls} == {"tick", "l2quote", "l2order"}
    assert all(count == 0 for _, _, count in fake.calls)
    assert fake.unsubscribed == list(range(1, 7))
    assert all(stream["records"] == 0 for stream in report["streams"])
