"""Passive runtime timing and an independent, account-free reference process."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

from monitor.logger import get_logger

logger = get_logger("system")
KINDS = {"on_l2_quote": "l2quote", "on_l2_order": "l2order", "on_l2_transaction": "l2transaction", "on_l2_orderqueue": "l2orderqueue"}


def stamp():
    return {"wall": time.time(), "mono": time.perf_counter(), "thread": threading.get_ident()}


def identity(event):
    return {"code": getattr(event, "stock_code", ""),
            "event_time": getattr(event, "event_time", None),
            "entrust_no": getattr(event, "entrust_no", ""),
            "trade_index": getattr(event, "trade_index", "")}


def observe(diagnostic, method, *args):
    """A diagnostic failure must not suppress a trading callback or its exception."""
    if diagnostic is None:
        return None
    try:
        return getattr(diagnostic, method)(*args)
    except Exception as exc:
        if not getattr(diagnostic, "error", ""):
            diagnostic.error = f"{type(exc).__name__}: {exc}"
            logger.warning("[LARGE_ORDER] [诊断] 记录异常，覆盖不足：%s", diagnostic.error)
        return None


class LatencyDiagnostics:
    """All market-path operations enqueue only; diagnostics never route orders."""

    def __init__(self, directory: Path, stocks: dict[str, str], record_dir: Path, stop_at: datetime):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.started_wall = time.time()
        self.subscription_starts = {}
        self.stocks = stocks
        self.record_dir = record_dir
        self.stop_at = stop_at
        self.queue = queue.Queue(maxsize=20000)
        self.context = threading.local()
        self.active_calls = {}
        self.stop_event = threading.Event()
        self.dropped = 0
        self.error = ""
        self.counts = Counter()
        self.child = None
        self.child_log = None
        self.sdk = None
        self.original_factory = None
        self.factory = None
        self.last_warning = 0.0
        self.last_capture = 0.0
        self.ring_overwritten = 0
        self.writer = threading.Thread(target=self._write, name="l2-diagnostic-writer", daemon=True)
        self.writer.start()
        self.watcher = None
        self.reference_attempts = 0
        self.child_command = None
        self.thread_dump_handle = None

    def emit(self, record):
        try:
            if not self.stop_event.is_set():
                self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.dropped += 1

    def install_sdk_timing(self, sdk):
        self.sdk = sdk
        self.original_factory = sdk.subscribe_callback_wrapper
        original = self.original_factory
        def factory(callback):
            wrapped = original(callback)
            def entry(data):
                previous = getattr(self.context, "sdk", None)
                self.context.sdk = stamp()
                try:
                    return wrapped(data)
                finally:
                    self.context.sdk = previous
            return entry
        self.factory = factory
        sdk.subscribe_callback_wrapper = factory

    def wrap_callback(self, kind, callback):
        def measured(data):
            state = observe(self, "callback_begin", kind, data)
            try:
                return callback(data)
            finally:
                if state is not None:
                    observe(self, "callback_end", kind, state, data)
        return measured

    def callback_begin(self, kind, data):
        state = {"begin": stamp(), "sdk": getattr(self.context, "sdk", None),
                 "subscription_starts": {code: self.subscription_starts.get((code.split(".")[0], kind), self.started_wall) for code in data},
                 "previous_parsed": getattr(self.context, "parsed", None)}
        self.context.parsed = None
        return state

    def callback_end(self, kind, state, data):
        parsed = getattr(self.context, "parsed", None)
        self.context.parsed = state.pop("previous_parsed")
        self.emit({"type": "callback", "kind": kind, **state, "parsed": parsed, "end": stamp(), "data": data})

    def mark_parsed(self):
        self.context.parsed = stamp()

    def strategy_begin(self, strategy=None, method=""):
        previous = getattr(self.context, "raw_ms", 0.0)
        self.context.raw_ms = 0.0
        began = {**stamp(), "previous_raw_ms": previous}
        token = (began["thread"], began["mono"])
        self.active_calls[token] = {"code": getattr(strategy, "stock_code", ""), "kind": KINDS.get(method, method),
                                    "stage": "lock_wait", "since": began}
        return began

    def strategy_acquired(self, began, acquired):
        token = (began["thread"], began["mono"])
        if token in self.active_calls:
            self.active_calls[token] = {**self.active_calls[token], "stage": "handler", "since": acquired}

    def sample_inflight(self, now):
        stalls = []
        for call in list(self.active_calls.values()):
            elapsed = (now["mono"] - call["since"]["mono"]) * 1000
            if elapsed >= 200:
                stalls.append({"type": "inflight_stall", **call, "elapsed_ms": elapsed, "observed": now})
        if stalls and time.monotonic() - self.last_capture >= 10:
            self.last_capture = time.monotonic()
            self.emit({"type": "stack_capture", "observed": now, "reason": "inflight_stall",
                       "stacks": {str(tid): traceback.format_stack(frame) for tid, frame in sys._current_frames().items()}})
        for call in stalls:
            self.emit(call)
        return stalls

    def strategy_end(self, strategy, method, event, began, acquired):
        ended = stamp()
        self.active_calls.pop((began["thread"], began["mono"]), None)
        fields = identity(event)
        if method not in KINDS:
            fields = {"code": strategy.stock_code}
        self.emit({"type": "strategy", "kind": KINDS.get(method, method), **fields,
                   "sdk": getattr(self.context, "sdk", None),
                   "name": strategy._stock_name, "begin": began, "acquired": acquired, "end": ended,
                   "lock_wait_ms": (acquired["mono"] - began["mono"]) * 1000,
                   "handler_ms": (ended["mono"] - acquired["mono"]) * 1000,
                   "raw_write_ms": getattr(self.context, "raw_ms", 0.0)})
        self.context.raw_ms = began["previous_raw_ms"] + getattr(self.context, "raw_ms", 0.0)
        if method == "_on_order_update_hook":
            self.emit({"type": "broker_order", "code": strategy.stock_code,
                       "observed": ended, "order_uuid": getattr(event, "order_uuid", ""),
                       "xt_order_id": getattr(event, "xt_order_id", 0),
                       "broker_order_time": getattr(event, "order_time", 0),
                       "status": str(getattr(event, "status", "")),
                       "filled_quantity": getattr(event, "filled_quantity", 0)})

    def raw_write(self, elapsed_ms):
        self.context.raw_ms = getattr(self.context, "raw_ms", 0.0) + elapsed_ms

    def strategy_event(self, code, name, event, payload):
        if event != "buy_submitted":
            return
        trigger = dict(payload.get("trigger") or {})
        self.emit({"type": "order_trigger", "code": code, "name": name, "observed": stamp(),
                   "trigger": trigger, "order_uuid": payload.get("order_uuid", ""),
                   "quantity": payload.get("quantity", 0), "dry_run": payload.get("dry_run")})

    def subscription(self, code, kind, stage, sub_id):
        observed = stamp()
        if stage == "begin":
            self.subscription_starts[(code.split(".")[0], kind)] = observed["wall"]
        self.emit({"type": "subscription", "code": code, "kind": kind, "stage": stage, "sub_id": sub_id, "observed": observed})

    def submit_api(self, code, trigger, began, order):
        self.emit({"type": "submit_api", "code": code, "trigger": dict(trigger), "begin": began, "end": stamp(),
                   "order_uuid": getattr(order, "order_uuid", ""), "xt_order_id": getattr(order, "xt_order_id", 0)})

    def start_reference(self):
        from importlib.metadata import PackageNotFoundError, version
        try:
            xtquant_version = version("xtquant")
        except PackageNotFoundError:
            xtquant_version = "unknown"
        root = Path(__file__).resolve().parents[2]
        self.child_log = (self.directory / "reference_startup.log").open("w", encoding="utf-8", buffering=1)
        duration = max(1, (self.stop_at - datetime.now()).total_seconds())
        command = [sys.executable, "-u", "-m", "strategies.large_order_limit_up_buy.scripts.probe_l2_latency",
                   "--stocks", ",".join(self.stocks), "--duration", str(duration), "--directory", str(self.directory / "reference"),
                   "--windowed", "--signal-file", str(self.directory / "signals.jsonl"),
                   "--parent-pid", str(os.getpid()), "--cache-interval", "1",
                   "--stop-file", str(self.directory / "reference.stop")]
        (self.directory / "manifest.json").write_text(json.dumps({
            "started": datetime.fromtimestamp(self.started_wall).isoformat(), "started_wall": self.started_wall,
            "python_version": sys.version, "parent_pid": os.getpid(), "stocks": self.stocks,
            "xtquant_version": xtquant_version,
            "records_dir": str(self.record_dir.resolve()), "stop_at": self.stop_at.isoformat(),
            "command": command, "anomaly_ms": 3000, "retention": "120 seconds before / 90 seconds after; bounded ring",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.child = subprocess.Popen(command, cwd=root, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                      stdout=self.child_log, stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reference_attempts = 1
        self.child_command = command
        self.thread_dump_handle = (self.directory / "stack_watchdog.log").open("a", encoding="utf-8")
        self.emit({"type": "reference_started", "pid": self.child.pid, "observed": stamp()})
        self.watcher = threading.Thread(target=self._watch, name="l2-diagnostic-health", daemon=True)
        self.watcher.start()

    def _capture(self, signals, record, reason):
        signals.write(json.dumps({"time": time.time(), "reason": reason, "code": record.get("code", "")}) + "\n")
        signals.flush()
        now = time.monotonic()
        if now - self.last_capture >= 10:
            self.last_capture = now
            self.emit({"type": "stack_capture", "observed": stamp(), "reason": reason,
                       "stacks": {str(tid): traceback.format_stack(frame) for tid, frame in sys._current_frames().items()}})
        if reason != "order_trigger" and now - self.last_warning >= 60:
            self.last_warning = now
            logger.warning("[LARGE_ORDER] [诊断] 检测到行情或处理延迟，已保存现场：%s", self.directory)

    def _write(self):
        ring = deque(maxlen=200000)
        capture_until = 0.0
        try:
            with (self.directory / "pipeline.jsonl").open("w", encoding="utf-8") as output, \
                 (self.directory / "signals.jsonl").open("a", encoding="utf-8") as signals, \
                 (self.directory / "metrics.jsonl").open("w", encoding="utf-8") as metrics:
                stats = {}
                last_flush = time.monotonic()
                while True:
                    try:
                        record = self.queue.get(timeout=0.5)
                    except queue.Empty:
                        record = {}
                    if record is None:
                        break
                    now = time.monotonic()
                    expanded = []
                    if record.get("type") == "callback":
                        data = record.pop("data")
                        starts = record.pop("subscription_starts", {})
                        for code, rows in data.items():
                            rows = [rows] if isinstance(rows, dict) else rows
                            for raw in rows:
                                source = raw.get("time")
                                from strategies.large_order_limit_up_buy.scripts.probe_l2_latency import event_timestamp
                                timestamp = event_timestamp(source)
                                started = starts.get(code, self.started_wall)
                                expanded.append({**record, "code": code.split(".")[0], "raw_time": source,
                                                 "subscription_started_wall": started,
                                                 "pre_subscription": bool(timestamp and timestamp < started),
                                                 "event_time": datetime.fromtimestamp(timestamp).isoformat() if timestamp else "",
                                                 "entrust_no": raw.get("entrustNo", ""), "trade_index": raw.get("tradeIndex", "")})
                    elif record:
                        expanded.append(record)
                    for item in expanded:
                        self.counts[item["type"]] += 1
                        key = (item.get("code", ""), item.get("kind", item["type"]))
                        stat = stats.setdefault(key, {"count": 0, "pre_subscription_count": 0, "pre_subscription_first": None,
                                                    "pre_subscription_last": None, "max_source_ms": None, "max_lock_ms": 0,
                                                    "max_handler_ms": 0, "max_raw_ms": 0, "max_decode_ms": 0,
                                                    "max_parser_ms": 0, "max_dispatch_ms": 0})
                        stat["count"] += 1
                        anomaly = False
                        if item["type"] == "callback":
                            source = item.get("raw_time")
                            from strategies.large_order_limit_up_buy.scripts.probe_l2_latency import event_timestamp, session_label
                            timestamp = event_timestamp(source)
                            native = item.get("sdk")
                            if native and timestamp:
                                age = (native["wall"] - timestamp) * 1000
                                if item.get("pre_subscription"):
                                    stat["pre_subscription_count"] += 1
                                    stat["pre_subscription_first"] = min(stat["pre_subscription_first"] or timestamp, timestamp)
                                    stat["pre_subscription_last"] = max(stat["pre_subscription_last"] or timestamp, timestamp)
                                else:
                                    stat["max_source_ms"] = max(stat["max_source_ms"] if stat["max_source_ms"] is not None else age, age)
                                item["source_to_sdk_ms"] = age
                                source_date = datetime.fromtimestamp(timestamp)
                                anomaly = not item.get("pre_subscription") and age >= 3000 and source_date.date() == datetime.now().date() and session_label(source_date) == "连续竞价"
                            if native:
                                stat["max_decode_ms"] = max(stat["max_decode_ms"], (item["begin"]["mono"] - native["mono"]) * 1000)
                            if item.get("parsed"):
                                stat["max_parser_ms"] = max(stat["max_parser_ms"], (item["parsed"]["mono"] - item["begin"]["mono"]) * 1000)
                                stat["max_dispatch_ms"] = max(stat["max_dispatch_ms"], (item["end"]["mono"] - item["parsed"]["mono"]) * 1000)
                            anomaly = anomaly or max(stat["max_decode_ms"], stat["max_parser_ms"], stat["max_dispatch_ms"]) >= 200
                        elif item["type"] == "strategy":
                            for field, target in (("lock_wait_ms", "max_lock_ms"), ("handler_ms", "max_handler_ms"), ("raw_write_ms", "max_raw_ms")):
                                stat[target] = max(stat[target], item[field])
                            anomaly = item["lock_wait_ms"] >= 200 or item["handler_ms"] >= 200
                        elif item["type"] == "inflight_stall":
                            anomaly = True
                        keep = item["type"] not in {"callback", "strategy"} or item.get("kind") in {"l2quote", "l2orderqueue"}
                        entry = [now, item, False]
                        if len(ring) == ring.maxlen and now - ring[0][0] <= 120 and not ring[0][2]:
                            self.ring_overwritten += 1
                        ring.append(entry)
                        while ring and now - ring[0][0] > 120:
                            ring.popleft()
                        if anomaly or item["type"] == "order_trigger":
                            start_capture = now >= capture_until
                            if now >= capture_until or item["type"] == "order_trigger" or now - self.last_capture >= 10:
                                self._capture(signals, item, "order_trigger" if item["type"] == "order_trigger" else "latency")
                            capture_until = now + 90
                            if start_capture:
                                for buffered in ring:
                                    if not buffered[2]:
                                        output.write(json.dumps(buffered[1], ensure_ascii=False, default=str) + "\n")
                                        buffered[2] = True
                        if (keep or now < capture_until) and not entry[2]:
                            output.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                            entry[2] = True
                    if now - last_flush >= 1 or not record:
                        for (code, kind), stat in stats.items():
                            metrics.write(json.dumps({"time": time.time(), "code": code, "kind": kind, **stat, "dropped": self.dropped}) + "\n")
                        stats.clear()
                        output.flush()
                        metrics.flush()
                        last_flush = now
                for (code, kind), stat in stats.items():
                    metrics.write(json.dumps({"time": time.time(), "code": code, "kind": kind, **stat, "dropped": self.dropped}) + "\n")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            logger.error("[LARGE_ORDER] [诊断] 写记录失败：%s", self.error)

    def _watch(self):
        try:
            self._watch_loop()
        except Exception as exc:
            self.error = self.error or f"health watcher: {type(exc).__name__}: {exc}"
            self.emit({"type": "health_error", "observed": stamp(), "error": self.error})
            logger.warning("[LARGE_ORDER] [诊断] 健康采集失败，覆盖不足：%s", self.error)

    def _watch_loop(self):
        import psutil
        parent = psutil.Process()
        parent.cpu_percent()
        previous = stamp()
        deadline = time.monotonic() + 15
        ready_printed = False
        failed_printed = False
        last_restart = 0.0
        while not self.stop_event.wait(1):
            import faulthandler
            faulthandler.dump_traceback_later(5, repeat=True, file=self.thread_dump_handle)
            now = stamp()
            process = {"type": "process", "observed": now, "parent_pid": os.getpid(),
                       "cpu_percent": parent.cpu_percent(), "rss": parent.memory_info().rss,
                       "writer_backlog": self.queue.qsize(), "dropped": self.dropped,
                       "clock_step_ms": ((now["wall"] - previous["wall"]) - (now["mono"] - previous["mono"])) * 1000}
            previous = now
            self.emit(process)
            self.sample_inflight(now)
            ready = self.directory / "reference" / "ready.json"
            if not ready_printed and ready.exists():
                try:
                    ready_data = json.loads(ready.read_text(encoding="utf-8"))
                    if ready_data.get("pid") == self.child.pid:
                        ready_printed = True
                        logger.info("[LARGE_ORDER] [诊断] 独立探针已就绪，覆盖%d只；目录=%s", len(self.stocks), self.directory)
                except (OSError, ValueError):
                    pass
            exit_code = self.child.poll()
            if not failed_printed and (exit_code is not None or (not ready_printed and time.monotonic() > deadline) or self.error or self.dropped):
                failed_printed = True
                logger.warning("[LARGE_ORDER] [诊断] 采集覆盖不完整，请查看启动日志和health.json：%s", self.directory)
            if exit_code is not None and datetime.now() < self.stop_at and self.reference_attempts < 3 and time.monotonic() - last_restart >= 30:
                self.emit({"type": "reference_exit", "pid": self.child.pid, "exit_code": exit_code, "observed": now})
                last_restart = time.monotonic()
                self.reference_attempts += 1
                try:
                    self.child = subprocess.Popen([*self.child_command, "--resume-directory"], cwd=Path(__file__).resolve().parents[2],
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, stdout=self.child_log, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except OSError as exc:
                    self.error = f"reference restart: {exc}"
                    self.emit({"type": "reference_restart_failed", "error": self.error, "observed": now})
                    continue
                deadline = time.monotonic() + 15
                ready_printed = False
                self.emit({"type": "reference_restarted", "pid": self.child.pid, "observed": stamp()})
                logger.warning("[LARGE_ORDER] [诊断] 独立探针已重启，第%d次；原记录保留", self.reference_attempts)
            (self.directory / "health.json").write_text(json.dumps({
                "time": datetime.now().isoformat(), "parent_pid": os.getpid(), "reference_pid": self.child.pid,
                "reference_ready": ready_printed, "reference_exit_code": exit_code,
                "writer_alive": self.writer.is_alive(), "dropped": self.dropped, "error": self.error,
                "ring_overwritten": self.ring_overwritten,
            }, ensure_ascii=False), encoding="utf-8")

    def close(self):
        self.stop_event.set()
        if self.sdk and self.sdk.subscribe_callback_wrapper is self.factory:
            self.sdk.subscribe_callback_wrapper = self.original_factory
        try:
            (self.directory / "reference.stop").touch()
        except OSError as exc:
            self.error = self.error or f"reference stop marker: {exc}"
        if self.watcher:
            self.watcher.join(timeout=3)
        if self.thread_dump_handle:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
            self.thread_dump_handle.close()
        if self.child:
            try:
                self.child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.child.terminate()
                try:
                    self.child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=3)
                self.error = self.error or "reference terminated after stop timeout"
        drain_deadline = time.monotonic() + 10
        while self.writer.is_alive() and time.monotonic() < drain_deadline:
            try:
                self.queue.put(None, timeout=0.5)
                break
            except queue.Full:
                continue
        self.writer.join(timeout=10)
        if self.child_log:
            self.child_log.close()
        (self.directory / "finished.json").write_text(json.dumps({"time": datetime.now().isoformat(),
            "dropped": self.dropped, "error": self.error, "writer_alive": self.writer.is_alive(),
            "ring_overwritten": self.ring_overwritten,
            "reference_exit_code": self.child.returncode if self.child else None}, ensure_ascii=False), encoding="utf-8")
        try:
            from strategies.large_order_limit_up_buy.scripts.analyze_runtime_latency import build_report
            report = build_report(self.directory)
            logger.info("[LARGE_ORDER] [诊断] 分析完成：异常%d类，覆盖缺口%d项；报告=%s",
                        len(report["representative_cases"]), len(report["coverage_gaps"]), self.directory / "analysis.md")
        except Exception as exc:
            logger.warning("[LARGE_ORDER] [诊断] 自动分析失败，原始证据已保留：%s", exc)


def start_diagnostics(configs, data_sub, stop_at, record_dir):
    """Called only by this strategy's runners after their existing startup gates."""
    now = datetime.now()
    directory = Path(record_dir).parent / "latency_diagnostics" / now.strftime("%Y-%m-%d") / f"{now:%H%M%S}_{os.getpid()}"
    diagnostic = None
    try:
        from xtquant import xtdata
        stocks = {config.stock_code: str((config.params or {}).get("stock_name", "")) for config in configs}
        diagnostic = LatencyDiagnostics(directory, stocks, Path(record_dir), stop_at)
        diagnostic.install_sdk_timing(xtdata)
        data_sub.set_l2_diagnostics(diagnostic)
        diagnostic.start_reference()
        logger.info("[LARGE_ORDER] [诊断] 自动采集已启动；目录=%s", directory)
        return diagnostic
    except Exception as exc:
        logger.warning("[LARGE_ORDER] [诊断] 启动失败，诊断覆盖不足：%s", exc)
        if diagnostic:
            observe(diagnostic, "close")
        data_sub.set_l2_diagnostics(None)
        return None
