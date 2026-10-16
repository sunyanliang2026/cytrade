"""Read-only, independent SDK/callback/cache timing probe; no trading imports."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime, time as dt_time
from pathlib import Path

KINDS = ("l2quote", "l2order", "l2transaction", "l2orderqueue")
HEADERS = (
    "kind", "code", "raw_time", "event_time", "sdk_entry_time", "callback_time",
    "source_to_sdk_ms", "sdk_decode_ms", "writer_wait_ms", "entrust_no", "trade_index",
    "buy_no", "sell_no", "price", "volume", "bid1", "ask1",
)
CACHE_HEADERS = ("observed_time", "kind", "code", "event_time", "cache_age_ms", "query_ms")


def event_timestamp(value) -> float | None:
    try:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            return None
        return result / 1000 if result > 10_000_000_000 else result
    except (TypeError, ValueError, OverflowError):
        return None


def iso(value: float | None) -> str:
    return datetime.fromtimestamp(value).isoformat(timespec="microseconds") if value is not None else ""


def session_label(now: datetime) -> str:
    if now.weekday() >= 5:
        return "非交易时段"
    if dt_time(11, 30) <= now.time() < dt_time(13):
        return "午间休市"
    if dt_time(9, 30) <= now.time() <= dt_time(11, 30) or dt_time(13) <= now.time() < dt_time(15):
        return "连续竞价"
    return "非连续竞价时段"


def normalize_codes(text: str) -> list[str]:
    codes = []
    for item in text.split(","):
        value = item.strip().upper()
        if re.fullmatch(r"\d{6}", value):
            value += ".SH" if value.startswith("6") else ".SZ"
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
            raise ValueError(f"股票代码格式错误：{item}")
        if value not in codes:
            codes.append(value)
    return codes


def callback_row(kind, code, raw, entered, callback_time, decode_ms, writer_wait_ms):
    source = event_timestamp(raw.get("time"))
    def first(field):
        value = raw.get(field)
        return value[0] if value is not None and len(value) else ""
    return (
        kind, code, raw.get("time", ""), iso(source), iso(entered), iso(callback_time),
        round((entered - source) * 1000, 3) if source is not None else "",
        round(decode_ms, 3), round(writer_wait_ms, 3), raw.get("entrustNo", ""),
        raw.get("tradeIndex", ""), raw.get("buyNo", ""), raw.get("sellNo", ""),
        raw.get("price", raw.get("lastPrice", "")), raw.get("volume", ""),
        first("bidPrice"), first("askPrice"),
    )


def timed_wrapper_factory(original_factory, context):
    """Measure earliest Python SDK entry separately from BSON decoding."""
    def factory(callback):
        def decoded(data):
            context.decode_ms = (time.perf_counter() - context.started) * 1000
            callback(data)
        wrapped = original_factory(decoded)
        def entry(data):
            context.entered = time.time()
            context.started = time.perf_counter()
            return wrapped(data)
        return entry
    return factory


class Recorder:
    def __init__(self, directory: Path, *, windowed=False, signal_file=None, resume=False):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=resume)
        self.resume = resume
        self.started_wall = time.time()
        self.queue = queue.Queue(maxsize=20000)
        self.context = threading.local()
        self.counts = Counter()
        self.dropped = 0
        self.error = ""
        self.windowed = windowed
        self.signal_file = signal_file
        self.ring_overwritten = 0
        self.saved = 0
        self.capture_until = 0.0
        self.signal_offset = 0
        self.thread = threading.Thread(target=self.write, name="probe-csv-writer")
        self.thread.start()

    def callback(self, kind):
        def receive(data):
            # Only enqueue the SDK envelope. Parsing and disk writes run elsewhere.
            received = time.time()
            item = (kind, self.context.entered, received, self.context.decode_ms, time.perf_counter(), data)
            try:
                self.queue.put_nowait(item)
            except queue.Full:
                self.dropped += 1
        return receive

    def write(self):
        try:
            with (self.directory / "callbacks.csv").open("a" if self.resume else "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                if handle.tell() == 0:
                    writer.writerow(HEADERS)
                last_flush = time.monotonic()
                ring = deque(maxlen=200000)
                def capture(now):
                    was_active = now < self.capture_until
                    self.capture_until = now + 90
                    if was_active:
                        return
                    for row in ring:
                        if not row[2]:
                            writer.writerow(row[1])
                            self.saved += 1
                            row[2] = True
                def signals(now):
                    if self.signal_file and self.signal_file.exists():
                        with self.signal_file.open(encoding="utf-8") as source:
                            source.seek(self.signal_offset)
                            while True:
                                position = source.tell()
                                line = source.readline()
                                if not line or not line.endswith("\n"):
                                    source.seek(position)
                                    break
                                try:
                                    signal = json.loads(line)
                                    if signal.get("time", time.time()) >= time.time() - 120:
                                        capture(now)
                                except ValueError:
                                    pass
                            self.signal_offset = source.tell()
                while True:
                    try:
                        item = self.queue.get(timeout=0.5)
                    except queue.Empty:
                        signals(time.monotonic())
                        handle.flush()
                        continue
                    if item is None:
                        signals(time.monotonic())
                        break
                    kind, entered, received, decode_ms, queued, data = item
                    wait_ms = (time.perf_counter() - queued) * 1000
                    for code, records in data.items():
                        records = [records] if isinstance(records, dict) else records
                        for raw in records:
                            row = callback_row(kind, code, raw, entered, received, decode_ms, wait_ms)
                            self.counts[(code, kind)] += 1
                            now = time.monotonic()
                            entry = [now, row, False]
                            if len(ring) == ring.maxlen and now - ring[0][0] <= 120 and not ring[0][2]:
                                self.ring_overwritten += 1
                            ring.append(entry)
                            while ring and now - ring[0][0] > 120:
                                ring.popleft()
                            source = event_timestamp(raw.get("time"))
                            anomaly = source is not None and source >= self.started_wall and (entered - source) * 1000 >= 3000 and datetime.fromtimestamp(source).date() == datetime.now().date() and session_label(datetime.fromtimestamp(source)) == "连续竞价"
                            if self.windowed and anomaly:
                                capture(now)
                            if not self.windowed or kind in ("l2quote", "l2orderqueue") or now < self.capture_until:
                                if not entry[2]:
                                    writer.writerow(row)
                                    self.saved += 1
                                    entry[2] = True
                    if time.monotonic() - last_flush >= 1:
                        signals(time.monotonic())
                        handle.flush()
                        last_flush = time.monotonic()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def close(self):
        while self.thread.is_alive():
            try:
                self.queue.put(None, timeout=0.5)
                break
            except queue.Full:
                continue
        self.thread.join()


def poll_cache(xtdata, codes, kinds, directory, stop, interval, errors, resume=False):
    try:
        with (directory / "cache.csv").open("a" if resume else "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if handle.tell() == 0:
                writer.writerow(CACHE_HEADERS)
            while not stop.is_set():
                for kind in kinds:
                    if stop.is_set():
                        break
                    started = time.perf_counter()
                    try:
                        frames = xtdata.get_market_data_ex([], codes, kind, count=1)
                        observed = time.time()
                        query_ms = (time.perf_counter() - started) * 1000
                        for code in codes:
                            frame = frames.get(code)
                            source = None
                            if frame is not None and len(frame) and "time" in frame.columns:
                                source = event_timestamp(frame.iloc[-1]["time"])
                            writer.writerow((iso(observed), kind, code, iso(source),
                                             round((observed - source) * 1000, 3) if source is not None else "",
                                             round(query_ms, 3)))
                    except Exception as exc:
                        errors.append(f"cache {kind}: {type(exc).__name__}: {exc}")
                handle.flush()
                stop.wait(interval)
    except Exception as exc:
        errors.append(f"cache writer: {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="独立L2延迟探针，只订行情，不连接交易账户")
    parser.add_argument("--stocks", default="002733,002242")
    parser.add_argument("--kinds", default=",".join(KINDS))
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--cache-interval", type=float, default=3)
    parser.add_argument("--compare-strategy", action="store_true", help="结束后自动对照当日策略原始记录")
    parser.add_argument("--compare-delay", type=float, default=0, help="停止采集后等待策略追上行情的秒数")
    parser.add_argument("--directory", type=Path, help="由策略启动时指定诊断目录")
    parser.add_argument("--windowed", action="store_true", help="逐笔仅保存异常/下单前后；报价持续保存")
    parser.add_argument("--signal-file", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--resume-directory", action="store_true", help="诊断进程重启时追加记录")
    args = parser.parse_args()
    if not all(math.isfinite(value) for value in (args.duration, args.cache_interval, args.compare_delay)):
        parser.error("时间参数必须是有限数值")
    if args.duration <= 0 or args.cache_interval <= 0 or args.compare_delay < 0:
        parser.error("时长和采样间隔必须大于0，对照等待时间不得为负数")
    try:
        codes = normalize_codes(args.stocks)
    except ValueError as exc:
        parser.error(str(exc))
    kinds = list(dict.fromkeys(args.kinds.split(",")))
    if not kinds or any(kind not in KINDS for kind in kinds):
        parser.error("行情类型必须是l2quote/l2order/l2transaction/l2orderqueue")
    from xtquant import xtdata
    xtdata.enable_hello = False
    xtdata.connect()
    now = datetime.now()
    directory = args.directory or Path(__file__).resolve().parents[1] / "data" / "latency_probe" / now.strftime("%Y-%m-%d") / f"{now:%H%M%S}_{os.getpid()}"
    recorder = Recorder(directory, windowed=args.windowed, signal_file=args.signal_file, resume=args.resume_directory)
    subscriptions = []
    subscription_timings = []
    errors = []
    stop = threading.Event()
    cache_thread = None
    system_thread = None
    original = xtdata.subscribe_callback_wrapper
    print(f"[探针] {session_label(now)}；股票={','.join(codes)}；count=0；时长={args.duration:g}秒", flush=True)
    print(f"[探针] 记录目录：{directory}", flush=True)
    try:
        xtdata.subscribe_callback_wrapper = timed_wrapper_factory(original, recorder.context)
        for code in codes:
            for kind in kinds:
                # Converted SDK periods use a different wrapper and cannot share this timing context.
                if xtdata._needconvert_period(kind)[0]:
                    raise RuntimeError(f"SDK对{kind}启用了转换包装，当前探针不支持此版本")
                began = time.perf_counter()
                sub_id = xtdata.subscribe_quote(code, period=kind, count=0, callback=recorder.callback(kind))
                subscription_timings.append({"code": code, "kind": kind, "duration_ms": round((time.perf_counter() - began) * 1000, 3)})
                if sub_id is None or int(sub_id) <= 0:
                    raise RuntimeError(f"订阅失败：{code} {kind} 返回{sub_id}")
                subscriptions.append(int(sub_id))
        xtdata.subscribe_callback_wrapper = original
        cache_thread = threading.Thread(target=poll_cache,
                                        args=(xtdata, codes, kinds, directory, stop, args.cache_interval, errors, args.resume_directory),
                                        name="probe-cache", daemon=True)
        cache_thread.start()
        (directory / "ready.json").write_text(json.dumps({"time": datetime.now().isoformat(), "pid": os.getpid(), "stocks": codes}), encoding="utf-8")
        if args.parent_pid:
            from strategies.large_order_limit_up_buy.diagnostic_system import sample_system
            system_thread = threading.Thread(target=sample_system, args=(directory, args.parent_pid, stop), name="probe-system", daemon=True)
            system_thread.start()
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline and not recorder.error:
            stop.wait(min(1, max(0, deadline - time.monotonic())))
            if args.stop_file and args.stop_file.exists():
                break
            if args.parent_pid:
                import psutil
                if not psutil.pid_exists(args.parent_pid):
                    break
    except KeyboardInterrupt:
        print("[探针] 手动结束", flush=True)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        xtdata.subscribe_callback_wrapper = original
        stop.set()
        for sub_id in subscriptions:
            try:
                xtdata.unsubscribe_quote(sub_id)
            except Exception as exc:
                errors.append(f"unsubscribe {sub_id}: {type(exc).__name__}: {exc}")
        if cache_thread:
            cache_thread.join(timeout=5)
            if cache_thread.is_alive():
                errors.append("cache query did not finish within 5 seconds")
        if system_thread:
            system_thread.join(timeout=5)
            if system_thread.is_alive():
                errors.append("system sampling did not finish within 5 seconds")
        recorder.close()
        if recorder.error:
            errors.append(recorder.error)
        result = {"stocks": codes, "kinds": kinds, "count": 0, "started": now.isoformat(),
                  "finished": datetime.now().isoformat(), "initial_session": session_label(now),
                  "records": {f"{code}:{kind}": n for (code, kind), n in recorder.counts.items()},
                  "subscriptions": subscription_timings,
                  "windowed": args.windowed, "saved_records": recorder.saved, "ring_overwritten": recorder.ring_overwritten,
                  "dropped_envelopes": recorder.dropped, "errors": errors}
        (directory / "session.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with (directory / "sessions.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(f"[探针] 结束；记录={sum(recorder.counts.values())}条；异常={len(errors)}；目录={directory}", flush=True)
        for error in errors:
            print(f"[探针] 异常：{error}", flush=True)
    if args.compare_strategy:
        from strategies.large_order_limit_up_buy.scripts.compare_l2_latency import compare
        deadline = time.monotonic() + args.compare_delay
        while time.monotonic() < deadline:
            time.sleep(min(30, max(0, deadline - time.monotonic())))
        records_dir = Path(__file__).resolve().parents[1] / "data" / "runtime_records" / now.strftime("%Y-%m-%d")
        report = compare(directory, records_dir)
        print(f"[对照] 匹配策略{report['matched_records']}条；逐笔结果：{directory / 'comparison.csv'}", flush=True)
    return 1 if errors or recorder.dropped else 0


if __name__ == "__main__":
    raise SystemExit(main())
