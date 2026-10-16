"""Build an evidence-based Chinese report without inferring unobserved network times."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


def records(path):
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except (ValueError, TypeError):
                    continue


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def key(record):
    source = record.get("event_time")
    if not source:
        return None
    number = record.get("entrust_no", "") if record.get("kind") == "l2order" else record.get("trade_index", "") if record.get("kind") == "l2transaction" else ""
    return record["kind"], record["code"].split(".")[0], datetime.fromisoformat(source), str(number)


def stages(callback, strategy=None):
    native, began, parsed, ended = (callback.get(name) for name in ("sdk", "begin", "parsed", "end"))
    result = {"sdk_decode_ms": (began["mono"] - native["mono"]) * 1000 if native else None,
              "parse_ms": (parsed["mono"] - began["mono"]) * 1000 if parsed else None,
              "dispatch_ms": (ended["mono"] - parsed["mono"]) * 1000 if parsed else None}
    for name in ("lock_wait_ms", "handler_ms", "raw_write_ms"):
        result[name] = strategy.get(name) if strategy else None
    return result


def pre_subscription(record, manifest):
    if "pre_subscription" in record:
        return record["pre_subscription"]
    started = record.get("subscription_started_wall") or manifest.get("started_wall")
    if not started and manifest.get("started"):
        started = datetime.fromisoformat(manifest["started"]).timestamp()
    return bool(started and record.get("event_time") and datetime.fromisoformat(record["event_time"]).timestamp() < started)


def trigger_key(record):
    trigger = record.get("trigger", {})
    return key({"kind": "l2order", "code": record["code"], "event_time": trigger.get("event_time"),
                "entrust_no": trigger.get("entrust_no", "")})


def receipt_key(record):
    base = key(record)
    sdk = record.get("sdk") or {}
    return (*base, sdk.get("mono", sdk.get("wall"))) if base else None


def broker_time_text(value):
    try:
        numeric = float(value)
        if 900_000_000 <= numeric <= 4_000_000_000:
            return datetime.fromtimestamp(numeric).isoformat()
        if 900_000_000_000 <= numeric <= 4_000_000_000_000:
            return datetime.fromtimestamp(numeric / 1000).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return str(value) if value else "缺失"


def diagnose(age_ms, timings, reference_age_ms=None, reference_ahead_ms=None, cache_ahead_ms=None, clock_step_ms=0):
    if abs(clock_step_ms) >= 500:
        return "检测到系统时钟跳变；先校验时钟，行情年龄不能直接视为延迟"
    if (timings.get("lock_wait_ms") or 0) >= 200:
        return "策略事件锁等待过长；对照线程现场定位持锁调用"
    if (timings.get("raw_write_ms") or 0) >= 200:
        return "策略原始行情写盘耗时过长"
    if (timings.get("sdk_decode_ms") or 0) >= 200:
        return "SDK入口后解码或Python调度耗时过长"
    if (timings.get("parse_ms") or 0) >= 200:
        return "项目行情解析耗时过长"
    if (timings.get("handler_ms") or 0) >= 200:
        return "策略处理耗时过长；对照线程现场和下单接口耗时"
    if (timings.get("dispatch_ms") or 0) >= 200:
        return "解析后分发耗时过长；检查状态锁、订阅同步及其他策略处理"
    if age_ms < 3000:
        return "此样本未检测到明显延迟"
    if reference_ahead_ms is not None and reference_ahead_ms >= 1000 and reference_age_ms is not None and reference_age_ms < 3000:
        return "主策略进程SDK回调晚于独立探针；定位主进程SDK分发、回放或调度"
    if cache_ahead_ms is not None and cache_ahead_ms >= 1000:
        return "QMT缓存进度已提前到达；优先核查缓存到SDK分发，同一条记录仍需核对"
    if reference_age_ms is None:
        return "独立探针缺少同一条记录，证据覆盖不足，不能判定上游原因"
    return "尚未证实该笔首次进入QMT缓存的时间；需核对QMT接收及分发日志，不能直接认定网络"


def build_report(directory: Path):
    manifest = load_json(directory / "manifest.json")
    finished = load_json(directory / "finished.json")
    reference_session = load_json(directory / "reference" / "session.json")
    candidates = {}
    triggers, submissions, broker_orders = {}, {}, {}
    clock_step = 0
    anomaly_count = 0
    missing_sdk = 0
    reference_restarts = 0
    history_count = 0
    history_ranges = {}
    subscription_events = []
    inflight_stalls = {}
    for record in records(directory / "pipeline.jsonl"):
        category = record.get("type")
        if category == "callback" and not record.get("sdk"):
            missing_sdk += 1
        if category == "reference_restarted":
            reference_restarts += 1
        if category == "callback" and record.get("sdk") and key(record):
            group = (record["code"], record["kind"])
            historical = pre_subscription(record, manifest)
            if historical:
                history_count += 1
            age = 0 if historical else record.get("source_to_sdk_ms", 0)
            timing = stages(record)
            anomaly_count += int(age >= 3000 or max(value or 0 for value in timing.values()) >= 200)
            severity = max(age, *(value or 0 for value in timing.values()))
            if group not in candidates or severity > candidates[group][0]:
                candidates[group] = (severity, record)
        elif category == "order_trigger":
            triggers[record["order_uuid"]] = record
        elif category == "submit_api":
            submissions[record["order_uuid"]] = record
        elif category == "broker_order":
            if record.get("broker_order_time"):
                broker_orders[record["order_uuid"]] = record
        elif category == "process":
            clock_step = max(clock_step, abs(record.get("clock_step_ms", 0)))
        elif category == "subscription":
            subscription_events.append(record)
        elif category == "inflight_stall":
            group = (record["code"], record["kind"], record["stage"], record["since"]["mono"])
            if group not in inflight_stalls or record["elapsed_ms"] > inflight_stalls[group]["elapsed_ms"]:
                inflight_stalls[group] = record
    # Metrics include all callbacks, even records not persisted outside capture windows.
    metric_history_count = 0
    for metric in records(directory / "metrics.jsonl"):
        count = metric.get("pre_subscription_count", 0)
        metric_history_count += count
        if count:
            group = f"{metric['code']}:{metric['kind']}"
            summary = history_ranges.setdefault(group, {"count": 0, "first": None, "last": None})
            summary["count"] += count
            first, last = metric.get("pre_subscription_first"), metric.get("pre_subscription_last")
            if first is not None:
                summary["first"] = min(summary["first"] or first, first)
            if last is not None:
                summary["last"] = max(summary["last"] or last, last)
    history_count = max(history_count, metric_history_count)
    callbacks = {receipt_key(record): record for _, record in candidates.values()}
    trigger_keys = {trigger_key(record) for record in triggers.values()} - {None}
    relevant_keys = {row_key[:4] for row_key in callbacks} | trigger_keys
    order_callbacks = {}
    strategy_spans = {}
    for record in records(directory / "pipeline.jsonl"):
        row_key = key(record)
        if record.get("type") == "callback" and record.get("sdk") and row_key in trigger_keys:
            for uuid, trigger in triggers.items():
                if trigger_key(trigger) != row_key:
                    continue
                target = trigger["trigger"].get("callback_time")
                target_wall = datetime.fromisoformat(target).timestamp() if target else (submissions.get(uuid, {}).get("begin") or {}).get("wall")
                if target_wall is None:
                    distance = record["sdk"]["wall"]
                elif target:
                    distance = abs(record["begin"]["wall"] - target_wall)
                else:
                    distance = target_wall - record["sdk"]["wall"]
                    if distance < 0:
                        continue
                if uuid not in order_callbacks or distance < order_callbacks[uuid][0]:
                    order_callbacks[uuid] = (distance, record)
        if record.get("type") == "strategy" and row_key in relevant_keys:
            occurrence = receipt_key(record)
            previous = strategy_spans.get(occurrence)
            if previous is None or record.get("handler_ms", 0) > previous.get("handler_ms", 0):
                strategy_spans[occurrence] = record
    for _, record in order_callbacks.values():
        callbacks[receipt_key(record)] = record
    callback_groups = {}
    for occurrence in callbacks:
        callback_groups.setdefault(occurrence[:4], []).append(occurrence)
    matched_reference = {}
    reference_path = directory / "reference" / "callbacks.csv"
    if reference_path.exists():
        with reference_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                row_key = key(row)
                for occurrence in callback_groups.get(row_key, []):
                    if occurrence not in matched_reference:
                        matched_reference[occurrence] = row
    cache_ahead = {}
    cache_path = directory / "reference" / "cache.csv"
    if cache_path.exists():
        with cache_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if not row["event_time"]:
                    continue
                progress = datetime.fromisoformat(row["event_time"])
                observed = datetime.fromisoformat(row["observed_time"]).timestamp()
                for row_key, callback in callbacks.items():
                    if row_key[:2] == (row["kind"], row["code"].split(".")[0]) and progress >= row_key[2]:
                        ahead = (callback["sdk"]["wall"] - observed) * 1000
                        if ahead > 0:
                            cache_ahead[row_key] = max(ahead, cache_ahead.get(row_key, 0))
    system_near = {}
    for record in records(directory / "reference" / "system.jsonl"):
        clock_step = max(clock_step, abs(record.get("clock_step_ms", 0)))
        for row_key, callback in callbacks.items():
            distance = abs(record.get("time", 0) - callback["sdk"]["wall"])
            if distance <= 5 and (row_key not in system_near or distance < system_near[row_key][0]):
                system_near[row_key] = (distance, {"time": record["time"], "processes": [
                    {name: process.get(name) for name in ("pid", "name", "cpu_percent", "rss", "context_switches", "io")}
                    for process in record.get("processes", [])]})
    clocks = list(records(directory / "reference" / "clock.jsonl"))
    ntp_offset_peak = max((abs(value * 1000) for record in clocks for value in record.get("ntp_offset_seconds", [])), default=None)
    clock_uncertainty = max(clock_step, ntp_offset_peak or 0)
    evidence = {}
    qmt_evidence_count = 0
    for record in records(directory / "reference" / "qmt_quote_evidence.jsonl"):
        qmt_evidence_count += 1
        meta = record.get("meta", "")
        for row_key, callback in callbacks.items():
            if f"_{row_key[1]}_" in meta and abs(record.get("observed_time", 0) - callback["sdk"]["wall"]) <= 10:
                samples = evidence.setdefault(row_key, [])
                if len(samples) < 20:
                    samples.append(record)
    details = {}
    cases = []
    for row_key, callback in callbacks.items():
        strategy_span = strategy_spans.get(row_key) or strategy_spans.get((*row_key[:4], None))
        timings = stages(callback, strategy_span)
        reference = matched_reference.get(row_key)
        ref_age = float(reference["source_to_sdk_ms"]) if reference and reference["source_to_sdk_ms"] else None
        ref_ahead = (callback["sdk"]["wall"] - datetime.fromisoformat(reference["sdk_entry_time"]).timestamp()) * 1000 if reference else None
        age = callback.get("source_to_sdk_ms", 0)
        historical = pre_subscription(callback, manifest)
        details[row_key] = {"code": row_key[1], "name": manifest.get("stocks", {}).get(row_key[1], ""),
                      "kind": row_key[0], "event_time": row_key[2].isoformat(), "number": row_key[3],
                      "sdk_entry": callback["sdk"], "callback_begin": callback["begin"], "pre_subscription": historical,
                      "source_to_sdk_ms": age, "reference_age_ms": ref_age, "reference_ahead_ms": ref_ahead,
                      "cache_ahead_ms": cache_ahead.get(row_key), "stages": timings,
                      "strategy_span": strategy_span,
                      "system_near_receive": system_near.get(row_key, (None, None))[1], "qmt_log_near_receive": evidence.get(row_key, []),
                      "finding": diagnose(0 if historical else age, timings, ref_age, ref_ahead, cache_ahead.get(row_key), clock_uncertainty)}
        if historical:
            details[row_key]["finding"] = "订阅前既有记录，不以行情年龄认定实时延迟；" + details[row_key]["finding"]
        if (not historical and age >= 3000) or max(value or 0 for value in timings.values()) >= 200:
            cases.append(details[row_key])
    gaps = []
    for label, path in (("独立探针就绪记录", directory / "reference" / "ready.json"),
                        ("独立系统/进程采样", directory / "reference" / "system.jsonl"),
                        ("时钟记录", directory / "reference" / "clock.jsonl")):
        if not path.exists():
            gaps.append(f"缺少{label}")
    if not reference_session:
        gaps.append("独立探针没有正常结束记录")
    if not finished:
        gaps.append("主进程没有正常结束记录，当前报告可能仍在采集中")
    elif finished.get("writer_alive"):
        gaps.append("主进程诊断写盘未排空，已保存记录可能不完整")
    if finished.get("dropped") or reference_session.get("dropped_envelopes"):
        gaps.append("诊断队列发生丢包，逐笔证据不完整")
    if finished.get("ring_overwritten") or reference_session.get("ring_overwritten"):
        gaps.append("高流量覆盖了部分前置环形缓冲，需检查具体委托是否匹配")
    if finished.get("error") or reference_session.get("errors") or (directory / "reference" / "system_error.json").exists():
        gaps.append("诊断存在异常，请检查finished.json、session.json和system_error.json")
    if cases and any(case["reference_age_ms"] is None for case in cases):
        gaps.append("部分异常样本没有独立探针的同一条行情")
    if missing_sdk:
        gaps.append(f"{missing_sdk}条行情未记录SDK最早入口时间")
    if reference_restarts:
        gaps.append(f"独立探针发生{reference_restarts}次重启，中断时段需结合启动日志检查")
    if any(session.get("dropped_envelopes") or session.get("ring_overwritten") or session.get("errors") for session in records(directory / "reference" / "sessions.jsonl")):
        gaps.append("独立探针某次运行有丢包、缓冲覆盖或异常，已保留全部运行历史")
    if ntp_offset_peak is None:
        gaps.append("没有成功的外部时钟偏差采样，固定时钟偏差尚未排除")
    if not qmt_evidence_count:
        gaps.append("没有采集到QMT行情日志字段；上游与QMT内部接收边界缺少证据")
    orders = []
    for uuid, trigger in triggers.items():
        row_key = receipt_key(order_callbacks[uuid][1]) if uuid in order_callbacks else None
        timing_chain = details.get(row_key)
        if not timing_chain:
            gaps.append(f"订单{uuid}缺少精确匹配的SDK回调，不能计算完整触发链路")
        elif timing_chain["reference_age_ms"] is None:
            gaps.append(f"订单{uuid}缺少独立探针的同一笔委托")
        if not trigger["trigger"].get("callback_time"):
            gaps.append(f"订单{uuid}缺少原始回调时刻，重复接收关联只能按提交前最近一次推定")
        submission = submissions.get(uuid, {})
        began, ended = submission.get("begin"), submission.get("end")
        if not began or not ended:
            gaps.append(f"订单{uuid}缺少完整下单接口起止记录")
        broker_time = broker_orders.get(uuid, {}).get("broker_order_time")
        if not broker_time and not trigger.get("dry_run"):
            gaps.append(f"订单{uuid}未取得柜台订单时间，不能核实实际委托时刻")
        sdk = (timing_chain or {}).get("sdk_entry", {})
        orders.append({"order_uuid": uuid, "code": trigger["code"], "name": trigger["name"], "trigger": trigger["trigger"],
                       "timing_chain": timing_chain,
                       "submit_api_begin": began, "submit_api_end": ended,
                       "sdk_to_submit_ms": (began["mono"] - sdk["mono"]) * 1000 if began and "mono" in began and sdk else None,
                       "submit_api_ms": (ended["mono"] - began["mono"]) * 1000 if began and ended and "mono" in began and "mono" in ended else None,
                       "broker_order_time_raw": broker_time, "broker_order_time_text": broker_time_text(broker_time),
                       "xt_order_id": broker_orders.get(uuid, {}).get("xt_order_id") or submission.get("xt_order_id")})
    result = {"directory": str(directory), "representative_cases": cases, "anomalous_callback_records": anomaly_count,
              "pre_subscription_records": history_count, "pre_subscription_ranges": history_ranges,
              "subscriptions": subscription_events, "clock_samples": clocks, "ntp_offset_peak_ms": ntp_offset_peak,
              "qmt_log_evidence_records": qmt_evidence_count,
              "inflight_stalls": list(inflight_stalls.values()),
              "clock_step_peak_ms": clock_step, "coverage_gaps": gaps, "orders": orders,
              "boundary": "QMT没有逐条网络接收日志时，上游与QMT内部接收处理仍可能无法区分；不能将缺失时间直接归因网络。"}
    (directory / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 行情延迟诊断", "", f"记录目录：`{directory}`", "",
             f"异常回调记录：{anomaly_count}条；代表样本：{len(cases)}条；触发下单：{len(orders)}笔。", ""]
    if history_count:
        lines += [f"订阅前既有记录：{history_count}条，单独统计，不将其行情年龄直接认定为实时延迟。其解析、分发和策略处理耗时仍参与诊断。", ""]
    if gaps:
        lines += ["## 证据覆盖", "", *[f"- {gap}" for gap in gaps], ""]
    if cases:
        lines += ["## 延迟定位", "", "| 股票 | 行情 | 行情时间 | 委托/成交号 | 行情至SDK | 定位 |", "|---|---|---|---|---:|---|"]
        for case in cases:
            lines.append(f"| {case['code']} {case['name']} | {case['kind']} | {case['event_time'][11:]} | {case['number']} | {case['source_to_sdk_ms']/1000:.3f}秒 | {case['finding']} |")
        lines += ["", "展示每个股票/行情类型的最大异常代表样本及实际触发委托，完整阶段耗时在analysis.json和pipeline.jsonl中。", ""]
    elif inflight_stalls:
        lines += ["已记录正在等待锁或处理中的阻塞现场，部分回调可能尚未完成，不能以缺少完成记录判断正常。", ""]
    else:
        lines += ["本次已保存样本未检测到明显延迟；这不代表缺失时段或证据不足时段正常。", ""]
    if inflight_stalls:
        lines += ["## 阻塞现场", "", "| 股票 | 行情 | 阶段 | 已观测阻塞时长 |", "|---|---|---|---:|"]
        for record in inflight_stalls.values():
            phase = "等待策略锁" if record["stage"] == "lock_wait" else "策略处理中"
            lines.append(f"| {record['code']} {manifest.get('stocks', {}).get(record['code'], '')} | {record['kind']} | {phase} | {record['elapsed_ms']/1000:.3f}秒 |")
        lines += ["", "以上时长是阻塞进行中采样的峰值，可能小于最终耗时；对应Python线程现场在pipeline.jsonl中。", ""]
    if orders:
        lines += ["## 触发与委托时间", "", "| 股票 | 触发委托号 | 行情时间 | SDK收到 | 本地调用下单 | 柜台原始时间 |", "|---|---|---|---|---|---|"]
        for order in orders:
            chain = order.get("timing_chain") or {}
            received = chain.get("sdk_entry", {}).get("wall")
            submitted = (order.get("submit_api_begin") or {}).get("wall")
            lines.append(f"| {order['code']} {order['name']} | {order['trigger'].get('entrust_no', '')} | {order['trigger'].get('event_time', '')} | {datetime.fromtimestamp(received).isoformat() if received else '缺失'} | {datetime.fromtimestamp(submitted).isoformat() if submitted else '缺失'} | {order['broker_order_time_text']} |")
        lines += ["", "独立探针对照、各阶段耗时和附近CPU/QMT日志在analysis.json的orders.timing_chain中。柜台时间保留接口原值，通常只有秒精度；时钟未核对时不能直接以本地时刻推断柜台迟到。", ""]
    lines += ["## 定位依据", "", "- pipeline.jsonl：SDK入口、解析、策略锁等待、处理、写盘、订阅和订单链路。",
              "- metrics.jsonl：按股票和数据类型的每秒峰值，新的正常行情不会抹掉之前的异常。",
              "- reference/system.jsonl与clock.jsonl：独立进程采集的CPU、线程、磁盘及系统时钟。",
              "- pipeline.jsonl中的stack_capture：异常时主进程的Python线程现场。",
              "- reference/qmt_quote_evidence.jsonl：仅保存白名单行情日志字段，可向券商提供。", "", result["boundary"], ""]
    (directory / "analysis.md").write_text("\n".join(lines), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成运行期行情延迟中文报告")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    report = build_report(args.directory)
    print(f"[诊断] 异常代表样本={len(report['representative_cases'])}；覆盖缺口={len(report['coverage_gaps'])}；报告={args.directory / 'analysis.md'}")
