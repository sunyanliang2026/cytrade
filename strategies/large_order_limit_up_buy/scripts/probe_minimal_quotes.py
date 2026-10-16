"""Read-only latency comparison for tick, l2quote, and l2order."""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

KINDS = ("tick", "l2quote", "l2order")


def event_seconds(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return result / 1000 if result > 10_000_000_000 else result


def normalize_codes(value):
    codes = []
    for item in value.split(","):
        code = item.strip().upper()
        if len(code) == 6 and code.isdigit():
            code += ".SH" if code.startswith("6") else ".SZ"
        if len(code) != 9 or code[6:] not in (".SH", ".SZ", ".BJ") or not code[:6].isdigit():
            raise ValueError(f"invalid stock code: {item}")
        if code not in codes:
            codes.append(code)
    return codes


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples = defaultdict(list)
        self.gaps = defaultdict(list)
        self.worst_gap = {}
        self.last_callback = {}
        self.invalid_times = defaultdict(int)

    def callback(self, kind):
        def receive(data):
            now = time.time()
            if not isinstance(data, dict):
                return
            for code, payload in data.items():
                rows = payload if isinstance(payload, list) else [payload]
                ages = []
                invalid = 0
                for row in rows:
                    source = event_seconds(row.get("time")) if isinstance(row, dict) else None
                    if source is None:
                        invalid += 1
                    else:
                        ages.append((round((now - source) * 1000, 3), source, now))
                key = (code, kind)
                with self.lock:
                    previous = self.last_callback.get(key)
                    if previous is not None:
                        gap_ms = round((now - previous) * 1000, 3)
                        self.gaps[key].append(gap_ms)
                        if gap_ms > self.worst_gap.get(key, (0,))[0]:
                            self.worst_gap[key] = (gap_ms, previous, now)
                    self.last_callback[key] = now
                    self.samples[key].extend(ages)
                    self.invalid_times[key] += invalid
        return receive

    def summary(self, codes):
        with self.lock:
            result = []
            for code in codes:
                for kind in KINDS:
                    key = (code, kind)
                    samples = list(self.samples[key])
                    ages = [sample[0] for sample in samples]
                    gaps = list(self.gaps[key])
                    worst = sorted(samples, reverse=True)[:5]
                    worst_gap = self.worst_gap.get(key)
                    result.append({
                        "code": code, "kind": kind, "records": len(ages),
                        "invalid_times": self.invalid_times[key],
                        "age_p50_ms": percentile(ages, 0.50),
                        "age_p95_ms": percentile(ages, 0.95),
                        "age_p99_ms": percentile(ages, 0.99),
                        "age_max_ms": round(max(ages), 3) if ages else None,
                        "age_over_1s": sum(age > 1000 for age in ages),
                        "age_over_3s": sum(age > 3000 for age in ages),
                        "age_over_10s": sum(age > 10000 for age in ages),
                        "callback_gap_max_ms": round(max(gaps), 3) if gaps else None,
                        "callback_gap_max_window": {
                            "from": datetime.fromtimestamp(worst_gap[1]).isoformat(),
                            "to": datetime.fromtimestamp(worst_gap[2]).isoformat(),
                        } if worst_gap else None,
                        "callback_gaps_over_6s": sum(gap > 6000 for gap in gaps),
                        "worst": [{"age_ms": age, "event_time": datetime.fromtimestamp(source).isoformat(),
                                   "received_time": datetime.fromtimestamp(received).isoformat()}
                                  for age, source, received in worst],
                    })
            return result


def run(xtdata, codes, duration, output):
    collector = Collector()
    subscriptions = []
    clock_steps = []
    started = datetime.now().isoformat()
    try:
        xtdata.connect()
        for code in codes:
            for kind in KINDS:
                sub_id = xtdata.subscribe_quote(code, period=kind, count=0, callback=collector.callback(kind))
                if sub_id is None or int(sub_id) <= 0:
                    raise RuntimeError(f"subscription failed: {code} {kind} id={sub_id}")
                subscriptions.append(int(sub_id))
        deadline = time.monotonic() + duration
        previous_wall, previous_mono = time.time(), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            wall, mono = time.time(), time.monotonic()
            step_ms = ((wall - previous_wall) - (mono - previous_mono)) * 1000
            if abs(step_ms) > 200:
                clock_steps.append({"time": datetime.fromtimestamp(wall).isoformat(), "step_ms": round(step_ms, 3)})
            previous_wall, previous_mono = wall, mono
    finally:
        for sub_id in subscriptions:
            xtdata.unsubscribe_quote(sub_id)
    report = {"started": started, "finished": datetime.now().isoformat(), "stocks": codes,
              "kinds": KINDS, "count": 0, "clock_steps": clock_steps,
              "streams": collector.summary(codes)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for stream in report["streams"]:
        print(f"{stream['code']} {stream['kind']}: n={stream['records']} "
              f"p50={stream['age_p50_ms']}ms p95={stream['age_p95_ms']}ms "
              f">1s={stream['age_over_1s']} max={stream['age_max_ms']}ms "
              f"gap_max={stream['callback_gap_max_ms']}ms", flush=True)
    print(f"report={output} clock_steps={len(clock_steps)}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="Minimal read-only tick/L2 latency probe")
    parser.add_argument("--stocks", required=True)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("duration must be positive and finite")
    codes = normalize_codes(args.stocks)
    from xtquant import xtdata
    xtdata.enable_hello = False
    output = args.output or (Path(__file__).resolve().parents[1] / "data" / "minimal_latency_probe" /
                             datetime.now().strftime("%Y-%m-%d") / f"{datetime.now():%H%M%S}_{os.getpid()}.json")
    run(xtdata, codes, args.duration, output)


if __name__ == "__main__":
    main()
