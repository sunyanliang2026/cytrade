import csv
import json
import threading

from strategies.large_order_limit_up_buy.scripts.probe_l2_latency import (
    Recorder, callback_row, event_timestamp, normalize_codes, timed_wrapper_factory,
)


def test_missing_timestamp_is_not_reported_as_zero_delay():
    row = callback_row("l2order", "002733.SZ", {"entrustNo": 1}, 100, 101, 0.2, 0.3)
    assert row[3] == ""
    assert row[6] == ""
    assert event_timestamp(float("nan")) is None
    assert event_timestamp(1791509472470) == 1791509472.47


def test_sdk_entry_captured_before_decode(monkeypatch):
    from strategies.large_order_limit_up_buy.scripts import probe_l2_latency as probe
    wall = iter([100, 101])
    clock = iter([50, 50.005])
    monkeypatch.setattr(probe.time, "time", lambda: next(wall))
    monkeypatch.setattr(probe.time, "perf_counter", lambda: next(clock))
    context = threading.local()
    records = []
    def original(callback):
        def decode(payload):
            assert context.entered == 100
            callback({"decoded": payload})
        return decode
    callback = timed_wrapper_factory(original, context)(lambda data: records.append(data))
    callback(b"bson")
    assert records == [{"decoded": b"bson"}]
    assert round(context.decode_ms, 6) == 5


def test_recorder_drains_envelopes_and_preserves_order_identity(tmp_path):
    recorder = Recorder(tmp_path / "probe")
    recorder.context.entered = 1791509472.5
    recorder.context.decode_ms = 0.01
    recorder.callback("l2order")({"002733.SZ": [{"time": 1791509472470, "entrustNo": 1718927}]})
    recorder.close()
    with (recorder.directory / "callbacks.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["entrust_no"] == "1718927"
    assert float(rows[0]["source_to_sdk_ms"]) == 30
    assert recorder.error == ""


def test_codes_are_deduplicated():
    assert normalize_codes("002733,002733.SZ,600488") == ["002733.SZ", "600488.SH"]


def test_comparison_matches_same_order_and_finds_cache_lead(tmp_path):
    from strategies.large_order_limit_up_buy.scripts.compare_l2_latency import compare
    from strategies.large_order_limit_up_buy.scripts.probe_l2_latency import CACHE_HEADERS, iso
    recorder = Recorder(tmp_path / "probe")
    recorder.context.entered = 1791509475.5
    recorder.context.decode_ms = 0.01
    raw = {"time": 1791509472470, "entrustNo": 1718927}
    recorder.callback("l2order")({"002733.SZ": [raw]})
    recorder.close()
    with (recorder.directory / "cache.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CACHE_HEADERS)
        writer.writerow((iso(1791509473), "l2order", "002733.SZ", iso(1791509472.47), 530, 1))
    records = tmp_path / "records"
    records.mkdir()
    row = {"kind": "l2order", "code": "002733", "event_time": iso(1791509472.47),
           "recv_time": iso(1791509485.5), "raw": raw}
    (records / "002733_test.l2.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = compare(recorder.directory, records)
    assert result["matched_records"] == 1
    assert result["groups"][0]["probe_ahead_median_sec"] == 10
    assert result["groups"][0]["cache_ahead_of_sdk_records"] == 1


def test_empty_probe_does_not_make_a_latency_claim(tmp_path):
    from strategies.large_order_limit_up_buy.scripts.compare_l2_latency import compare
    from strategies.large_order_limit_up_buy.scripts.probe_l2_latency import CACHE_HEADERS
    recorder = Recorder(tmp_path / "probe")
    recorder.close()
    with (recorder.directory / "cache.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(CACHE_HEADERS)
    result = compare(recorder.directory, tmp_path / "nonexistent")
    assert result["matched_records"] == 0
    assert result["groups"] == []


def test_scheduled_launcher_keeps_failure_before_probe_records(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from strategies.large_order_limit_up_buy.scripts import run_scheduled_l2_probe as launcher
    script = tmp_path / "strategies" / "large_order_limit_up_buy" / "scripts" / "run_scheduled_l2_probe.py"
    monkeypatch.setattr(launcher, "__file__", str(script))
    monkeypatch.setattr(launcher.sys, "argv", [str(script), "--stocks", "2733,2242"])
    def fail_before_recording(command, **kwargs):
        assert kwargs["cwd"] == tmp_path
        assert command[-2:] == ["--stocks", "2733,2242"]
        kwargs["stdout"].write("stock validation failed before recording\n")
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(launcher.subprocess, "run", fail_before_recording)
    assert launcher.main() == 2
    log = next(tmp_path.glob("strategies/*/data/latency_probe/launcher/*/*.log"))
    contents = log.read_text(encoding="utf-8")
    assert "launcher_start" in contents
    assert "stock validation failed before recording" in contents
    assert '"exit_code": 2' in contents
