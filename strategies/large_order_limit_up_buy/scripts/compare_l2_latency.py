"""Compare independent probe observations with matching strategy raw records."""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median


def record_key(kind, code, timestamp, entrust_no="", trade_index=""):
    identity = entrust_no if kind == "l2order" else trade_index if kind == "l2transaction" else ""
    return kind, code.split(".")[0], datetime.fromisoformat(timestamp), str(identity)


def compare(probe_dir: Path, records_dir: Path) -> dict:
    observations = {}
    with (probe_dir / "callbacks.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row["event_time"]:
                continue
            key = record_key(row["kind"], row["code"], row["event_time"], row["entrust_no"], row["trade_index"])
            observations.setdefault(key, row)
    matched = set()
    groups = defaultdict(list)
    output_headers = ("股票", "行情类型", "行情时间", "委托或成交号", "探针SDK收到", "策略记录时间",
                      "探针领先策略秒", "探针行情年龄秒", "SDK解码毫秒", "较早缓存采样时间", "缓存领先SDK秒")
    cache = defaultdict(list)
    with (probe_dir / "cache.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["event_time"]:
                cache[(row["kind"], row["code"].split(".")[0])].append(
                    (datetime.fromisoformat(row["event_time"]), datetime.fromisoformat(row["observed_time"])))
    # Windows directory metadata can remain stale while another process holds a file open.
    # Stream its actual contents rather than skipping files with stat().st_size == 0.
    with (probe_dir / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(output_headers)
        codes = {key[1] for key in observations}
        for code in sorted(codes):
            for path in records_dir.glob(f"{code}_*.l2.jsonl"):
                with path.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            record = json.loads(line)
                            raw = record.get("raw", {})
                            key = record_key(record["kind"], record["code"], record["event_time"],
                                             raw.get("entrustNo", ""), raw.get("tradeIndex", ""))
                        except (ValueError, KeyError, TypeError):
                            continue
                        if key not in observations or key in matched:
                            continue
                        matched.add(key)
                        probe = observations[key]
                        probe_time = datetime.fromisoformat(probe["sdk_entry_time"])
                        strategy_time = datetime.fromisoformat(record["recv_time"])
                        ahead = (strategy_time - probe_time).total_seconds()
                        source_age = (probe_time - key[2]).total_seconds()
                        earlier_cache = [observed for progress, observed in cache[(key[0], code)]
                                         if progress >= key[2] and key[2] <= observed < probe_time]
                        earliest = min(earlier_cache) if earlier_cache else None
                        cache_ahead = (probe_time - earliest).total_seconds() if earliest else None
                        writer.writerow((code, key[0], key[2].isoformat(), key[3], probe_time.isoformat(),
                                         strategy_time.isoformat(), round(ahead, 6), round(source_age, 6),
                                         probe["sdk_decode_ms"], earliest.isoformat() if earliest else "",
                                         round(cache_ahead, 6) if cache_ahead is not None else ""))
                        groups[(code, key[0])].append((ahead, source_age, cache_ahead))
        handle.flush()
    result = {
        "probe_records_with_timestamp": len(observations), "matched_records": len(matched),
        "note": "策略时间取原始落盘recv_time，略晚于策略回调入口；未匹配不代表策略丢包。",
        "interpretation": [
            "探针明显先收到同一条行情：排查策略进程内SDK分发、GIL、回调或写盘积压。",
            "探针也晚到，且QMT缓存进度更早已超过该条行情：排查QMT缓存到SDK的分发链路。",
            "探针和缓存都晚：仍需QMT原始网络接收时间，不能直接归因网络或券商。",
            "午休、收盘、无新委托时行情年龄增长正常，不代表传输延迟。",
        ],
        "groups": [{"code": code, "kind": kind, "matched": len(values),
                    "probe_ahead_median_sec": round(median(v[0] for v in values), 6),
                    "probe_source_age_median_sec": round(median(v[1] for v in values), 6),
                    "cache_ahead_of_sdk_records": sum(v[2] is not None for v in values)}
                   for (code, kind), values in sorted(groups.items())],
    }
    (probe_dir / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description="按同一行情逐笔比较探针与策略接收时间")
    parser.add_argument("probe_dir", type=Path)
    parser.add_argument("--records-dir", type=Path)
    args = parser.parse_args()
    records = args.records_dir or Path(__file__).resolve().parents[1] / "data" / "runtime_records" / args.probe_dir.parent.name
    result = compare(args.probe_dir, records)
    print(f"[对照] 行情={result['probe_records_with_timestamp']}条；匹配策略={result['matched_records']}条")
    for group in result["groups"]:
        print(f"[对照] {group['code']} {group['kind']} {group['matched']}条；"
              f"探针领先中位={group['probe_ahead_median_sec']:.3f}秒；"
              f"探针行情年龄中位={group['probe_source_age_median_sec']:.3f}秒")
    print(f"[对照] 逐笔结果：{args.probe_dir / 'comparison.csv'}")


if __name__ == "__main__":
    main()
