import csv
import json
import os
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.data_subscription import DataSubscriptionManager
from core.l2_models import L2OrderEvent
from strategies.large_order_limit_up_buy.diagnostic_system import quote_log_fields
from strategies.large_order_limit_up_buy.latency_diagnostics import LatencyDiagnostics, observe
from strategies.large_order_limit_up_buy.scripts.analyze_runtime_latency import build_report, diagnose
from strategies.large_order_limit_up_buy.scripts.probe_l2_latency import HEADERS, CACHE_HEADERS, Recorder, callback_row
from strategies.large_order_limit_up_buy.strategy import _serialized


def diagnostic(tmp_path):
    return LatencyDiagnostics(tmp_path / "diagnostic", {"002733": "雄韬股份"}, tmp_path / "runtime_records", datetime.now() + timedelta(minutes=1))


def test_optional_core_hook_preserves_event_time_and_dispatch(tmp_path):
    diag = diagnostic(tmp_path)
    manager = DataSubscriptionManager(print_latest_status=False)
    manager.set_l2_diagnostics(diag)
    received = []
    manager.set_l2_order_callback(lambda events: received.extend(events["002733"]))
    wrapped = diag.wrap_callback("l2order", manager._on_l2_order_data)
    wrapped({"002733.SZ": [{"time": 1791509472470, "price": 20.78, "volume": 100, "entrustNo": 1718927}]})
    diag.close()
    assert len(received) == 1
    assert received[0].event_time == datetime.fromtimestamp(1791509472.47)
    assert received[0].entrust_no == "1718927"
    rows = [json.loads(line) for line in (diag.directory / "pipeline.jsonl").read_text(encoding="utf-8").splitlines()]
    # Missing native SDK timing is an explicit coverage gap, never zero latency.
    assert build_report(diag.directory)["coverage_gaps"]


def test_diagnostic_failure_does_not_change_strategy_result_or_exception():
    class Broken:
        error = ""
        def strategy_begin(self, *args):
            raise OSError("diagnostic storage failed")
    class Strategy:
        _event_lock = threading.RLock()
        _latency_diagnostic = Broken()
        @_serialized
        def handler(self, fail=False):
            if fail:
                raise ValueError("original strategy failure")
            return "original result"
    strategy = Strategy()
    assert strategy.handler() == "original result"
    with pytest.raises(ValueError, match="original strategy failure"):
        strategy.handler(True)
    assert "diagnostic storage failed" in strategy._latency_diagnostic.error


@pytest.mark.parametrize("timings,age,ref_age,ahead,cache,expected", [
    ({"lock_wait_ms": 250}, 20, 20, 0, None, "策略事件锁"),
    ({"raw_write_ms": 500}, 20, 20, 0, None, "写盘"),
    ({"sdk_decode_ms": 1000}, 20, 20, 0, None, "解码"),
    ({}, 66000, 30, 65970, None, "主策略进程SDK"),
    ({}, 66000, 66000, 0, 60000, "缓存到SDK"),
    ({}, 66000, 66000, 0, None, "不能直接认定网络"),
    ({}, 66000, None, None, None, "证据覆盖不足"),
])
def test_report_attributes_only_observed_stages(timings, age, ref_age, ahead, cache, expected):
    assert expected in diagnose(age, timings, ref_age, ahead, cache)
    assert "时钟" in diagnose(age, timings, ref_age, ahead, cache, clock_step_ms=2000)


def test_reference_retains_before_trigger_and_resume_does_not_overwrite(tmp_path):
    signal = tmp_path / "signals.jsonl"
    recorder = Recorder(tmp_path / "reference", windowed=True, signal_file=signal)
    recorder.context.entered = time.time()
    recorder.context.decode_ms = 0.01
    recorder.callback("l2order")({"002733.SZ": [{"time": int(time.time() * 1000), "entrustNo": 100}]})
    signal.write_text(json.dumps({"time": time.time(), "reason": "order_trigger"}) + "\n", encoding="utf-8")
    recorder.close()
    resumed = Recorder(tmp_path / "reference", signal_file=signal, resume=True)
    resumed.context.entered = time.time()
    resumed.context.decode_ms = 0.01
    resumed.callback("l2order")({"002733.SZ": [{"time": int(time.time() * 1000), "entrustNo": 200}]})
    resumed.close()
    with (recorder.directory / "callbacks.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["entrust_no"] for row in rows] == ["100", "200"]
    assert not recorder.error and not resumed.error


def test_qmt_log_capture_is_allowlisted():
    line = '2026-10-09 09:31:12,600 data return, meta: SZ_002733_1801_0, datas:[20261009093112, 20261009093112, 10], net time: 3.2ms, password: secret-value, account_id: 12345'
    fields = quote_log_fields(line)
    assert fields["meta"] == "SZ_002733_1801_0"
    assert fields["net_ms"] == "3.2"
    assert "secret-value" not in json.dumps(fields)
    assert "12345" not in json.dumps(fields)


def test_reference_launch_uses_entire_pool_without_shell_conversion(tmp_path, monkeypatch):
    diag = diagnostic(tmp_path)
    diag.stocks["000001"] = "平安银行"
    captured = []
    class Child:
        pid = os.getpid()
        returncode = 0
        def wait(self, timeout=None):
            return 0
    def spawn(command, **kwargs):
        captured.append(command)
        assert not kwargs.get("shell")
        return Child()
    monkeypatch.setattr("strategies.large_order_limit_up_buy.latency_diagnostics.subprocess.Popen", spawn)
    monkeypatch.setattr(diag, "_watch", lambda: None)
    diag.start_reference()
    diag.close()
    assert captured[0][captured[0].index("--stocks") + 1] == "002733,000001"
    assert "--windowed" in captured[0]


def test_full_report_matches_delayed_order_against_independent_reference(tmp_path):
    directory = tmp_path / "diag"
    reference = directory / "reference"
    reference.mkdir(parents=True)
    source = 1791509472.47
    native = source + 66.64
    callback = {"type": "callback", "kind": "l2order", "code": "002733", "event_time": datetime.fromtimestamp(source).isoformat(),
                "entrust_no": 1718927, "source_to_sdk_ms": 66640,
                "sdk": {"wall": native, "mono": 100}, "begin": {"wall": native + .001, "mono": 100.001},
                "parsed": {"wall": native + .002, "mono": 100.002}, "end": {"wall": native + .01, "mono": 100.01}}
    (directory / "pipeline.jsonl").write_text(json.dumps(callback) + "\n", encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps({"stocks": {"002733": "雄韬股份"}}), encoding="utf-8")
    with (reference / "callbacks.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        writer.writerow(callback_row("l2order", "002733.SZ", {"time": int(source * 1000), "entrustNo": 1718927}, source + .03, source + .031, 1, 0))
    with (reference / "cache.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(CACHE_HEADERS)
    result = build_report(directory)
    case = result["representative_cases"][0]
    assert "主策略进程SDK" in case["finding"]
    assert case["reference_ahead_ms"] > 66000
    assert case["name"] == "雄韬股份"
    assert result["coverage_gaps"]
    assert "雄韬股份" in (directory / "analysis.md").read_text(encoding="utf-8")


def callback_record(source, delay, number, **fields):
    native = source + delay
    return {"type": "callback", "kind": "l2order", "code": "002733",
            "event_time": datetime.fromtimestamp(source).isoformat(), "entrust_no": number,
            "source_to_sdk_ms": delay * 1000, "sdk": {"wall": native, "mono": 100},
            "begin": {"wall": native + .001, "mono": 100.001},
            "parsed": {"wall": native + .002, "mono": 100.002},
            "end": {"wall": native + .01, "mono": 100.01}, **fields}


def test_startup_history_does_not_hide_later_realtime_latency(tmp_path):
    source = 1791509472.47
    directory = tmp_path / "diag"
    directory.mkdir()
    history = callback_record(source - 120, 1000, 1)
    delayed = callback_record(source, 66, 2)
    (directory / "manifest.json").write_text(json.dumps({"started_wall": source - 60}), encoding="utf-8")
    (directory / "pipeline.jsonl").write_text("\n".join(json.dumps(row) for row in (history, delayed)), encoding="utf-8")
    report = build_report(directory)
    assert report["pre_subscription_records"] == 1
    assert report["anomalous_callback_records"] == 1
    assert [case["number"] for case in report["representative_cases"]] == ["2"]


def test_history_processing_cost_is_still_diagnosed(tmp_path):
    directory = tmp_path / "diag"
    directory.mkdir()
    record = callback_record(1791509472.47, 1000, 1, pre_subscription=True)
    record["begin"]["mono"] = 100.5
    record["parsed"]["mono"] = 100.501
    record["end"]["mono"] = 100.51
    (directory / "pipeline.jsonl").write_text(json.dumps(record), encoding="utf-8")
    report = build_report(directory)
    assert report["pre_subscription_records"] == 1
    assert "订阅前" in report["representative_cases"][0]["finding"]
    assert "解码" in report["representative_cases"][0]["finding"]


def test_writer_separates_history_and_realtime_source_peak(tmp_path):
    diag = diagnostic(tmp_path)
    now = time.time()
    diag.started_wall = now - 30
    data = {"002733.SZ": [{"time": int((now - 1000) * 1000), "entrustNo": 1},
                          {"time": int((now - .1) * 1000), "entrustNo": 2}]}
    diag.context.sdk = {"wall": now, "mono": time.perf_counter()}
    diag.wrap_callback("l2order", lambda data: None)(data)
    diag.close()
    metrics = [json.loads(line) for line in (diag.directory / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    orders = [metric for metric in metrics if metric["kind"] == "l2order"]
    assert sum(metric["pre_subscription_count"] for metric in orders) == 1
    assert max(metric["max_source_ms"] for metric in orders if metric["max_source_ms"] is not None) < 200
    assert build_report(diag.directory)["pre_subscription_records"] == 1


def test_every_order_gets_exact_timing_chain_even_when_not_worst_sample(tmp_path):
    source = 1791509472.47
    directory = tmp_path / "diag"
    directory.mkdir()
    worst = callback_record(source, 66, 1)
    actual = callback_record(source + 100, .03, 2)
    trigger = {"type": "order_trigger", "code": "002733", "name": "雄韬股份", "order_uuid": "our-order",
               "trigger": {"event_time": actual["event_time"], "entrust_no": "2"}}
    submit = {"type": "submit_api", "order_uuid": "our-order", "begin": {"wall": source + 100.04},
              "end": {"wall": source + 100.045}}
    broker = {"type": "broker_order", "order_uuid": "our-order", "broker_order_time": 1791509572, "xt_order_id": 123}
    (directory / "pipeline.jsonl").write_text("\n".join(json.dumps(row) for row in (trigger, submit, broker, worst, actual)), encoding="utf-8")
    report = build_report(directory)
    order = report["orders"][0]
    assert order["timing_chain"]["number"] == "2"
    assert order["timing_chain"]["source_to_sdk_ms"] == 30
    assert order["broker_order_time_raw"] == 1791509572
    assert order["xt_order_id"] == 123
    assert "柜台" in (directory / "analysis.md").read_text(encoding="utf-8")


def test_sdk_wrapper_preserves_callback_and_restores_context(tmp_path):
    diag = diagnostic(tmp_path)
    sdk = SimpleNamespace(subscribe_callback_wrapper=lambda callback: lambda data: callback(data))
    original = sdk.subscribe_callback_wrapper
    diag.install_sdk_timing(sdk)
    received = []
    wrapped = sdk.subscribe_callback_wrapper(diag.wrap_callback("l2order", lambda data: received.append(data)))
    data = {"002733.SZ": [{"time": int(time.time() * 1000), "entrustNo": 1}]}
    wrapped(data)
    assert received == [data]
    assert diag.context.sdk is None
    failing = sdk.subscribe_callback_wrapper(lambda data: (_ for _ in ()).throw(ValueError("original")))
    with pytest.raises(ValueError, match="original"):
        failing(data)
    assert diag.context.sdk is None
    diag.close()
    assert sdk.subscribe_callback_wrapper is original


def test_watcher_failure_is_recorded_without_raising(tmp_path, monkeypatch):
    diag = diagnostic(tmp_path)
    def fail():
        raise OSError("health disk failed")
    monkeypatch.setattr(diag, "_watch_loop", fail)
    diag._watch()
    diag.close()
    assert "health disk failed" in diag.error
    assert "health_error" in (diag.directory / "pipeline.jsonl").read_text(encoding="utf-8")


def test_callback_diagnostic_failure_preserves_original_behavior(tmp_path):
    diag = diagnostic(tmp_path)
    assert diag.wrap_callback("l2order", lambda data: "original result")(None) == "original result"
    def fail(data):
        raise ValueError("original callback failure")
    with pytest.raises(ValueError, match="original callback failure"):
        diag.wrap_callback("l2order", fail)(None)
    diag.close()
    assert diag.error


def test_order_matches_its_receipt_instead_of_a_later_duplicate(tmp_path):
    source = 1791509472.47
    directory = tmp_path / "diag"
    directory.mkdir()
    actual = callback_record(source, .03, 2)
    duplicate = callback_record(source, 66, 2)
    duplicate["sdk"]["mono"] = 200
    trigger = {"type": "order_trigger", "code": "002733", "name": "雄韬股份", "order_uuid": "our-order",
               "trigger": {"event_time": actual["event_time"], "entrust_no": "2",
                           "callback_time": datetime.fromtimestamp(source + .031).isoformat()}}
    submit = {"type": "submit_api", "order_uuid": "our-order", "begin": {"wall": source + .04},
              "end": {"wall": source + .045}}
    (directory / "pipeline.jsonl").write_text("\n".join(json.dumps(row) for row in (trigger, submit, actual, duplicate)), encoding="utf-8")
    report = build_report(directory)
    assert report["orders"][0]["timing_chain"]["source_to_sdk_ms"] == 30
    assert report["representative_cases"][0]["source_to_sdk_ms"] == 66000


def test_callback_timing_includes_existing_profiler_logging(tmp_path, monkeypatch):
    diag = diagnostic(tmp_path)
    manager = DataSubscriptionManager(print_latest_status=False)
    manager.set_l2_diagnostics(diag)
    def profile(kind, callback):
        def measured(data):
            callback(data)
            time.sleep(.025)
        return measured
    monkeypatch.setattr(manager, "_profile_l2_callback", profile)
    diag.context.sdk = {"wall": time.time(), "mono": time.perf_counter()}
    manager._get_l2_callback("l2quote")({"002733.SZ": {"time": int(time.time() * 1000), "lastPrice": 20.78}})
    diag.close()
    rows = [json.loads(line) for line in (diag.directory / "pipeline.jsonl").read_text(encoding="utf-8").splitlines()]
    callback = next(row for row in rows if row["type"] == "callback")
    assert (callback["end"]["mono"] - callback["parsed"]["mono"]) * 1000 >= 20


def test_stalled_lock_and_handler_are_captured_before_they_finish(tmp_path):
    diag = diagnostic(tmp_path)
    strategy = SimpleNamespace(stock_code="002733", _stock_name="雄韬股份")
    began = diag.strategy_begin(strategy, "on_l2_order")
    now = {**began, "mono": began["mono"] + 1}
    assert diag.sample_inflight(now)[0]["stage"] == "lock_wait"
    diag.strategy_acquired(began, now)
    later = {**now, "mono": now["mono"] + 1}
    assert diag.sample_inflight(later)[0]["stage"] == "handler"
    diag.strategy_end(strategy, "on_l2_order", None, began, now)
    assert not diag.active_calls
    diag.close()
    rows = [json.loads(line) for line in (diag.directory / "pipeline.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row.get("reason") == "inflight_stall" and row.get("stacks") for row in rows)
