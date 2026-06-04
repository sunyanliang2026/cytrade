"""Parse MainSealFollow morning-monitor logs into replayable summaries.

This module is intentionally dependency-free. It accepts both JSON log lines
written by ``monitor.logger`` and console-style lines copied from a terminal.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

SESSION_PREFIX = "MONITOR_SESSION"
MSF_PREFIX = "MSF_EVENT "
MOCK_TRADE_MARKER = "[ORDER] [TRADE] [MOCK]"
SAFE_ORDER_MARKERS = (
    "[MOCK]",
    "[DRY_RUN]",
    "dry_run",
    "virtual",
    "simulated",
    "observation filled",
    "注册订单",
    "订单状态变更",
    "忽略重复成交",
    "忽略无策略归属成交",
    "下单拦截",
    "\u5a09\u3125\u553d\u7481\u3220\u5d1f",
    "\u7481\u3220\u5d1f\u9418\u8235\u20ac\u4f78\u5f49\u93c7?",
    "\u8e47\u754c\u6690\u95b2\u5d85\ue632\u93b4\u612a\u6c26",
    "\u8e47\u754c\u6690\u93c3\u72b5\u74e5\u9423\u30e5\u7d8a\u705e\u70b4\u579a\u6d5c?",
    "\u6d93\u5b2a\u5d1f\u93b7\ufe3d\u57c5",
    "注册订单",
    "订单状态变更",
    "忽略重复成交",
    "忽略无策略归属成交",
    "下单拦截",
)
SUSPICIOUS_ORDER_MARKERS = (
    "LIVE preflight passed",
    "下单提交",
    "撤单提交",
    "[ORDER] [TRADE] 成交",
    "下单提交",
    "撤单提交",
    "[ORDER] [TRADE] 成交",
    "live order submitted",
)

_KEY_VALUE_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^\s,]+)")
_INT_FIELDS = {
    "total",
    "strategies",
    "strategy_count",
    "tick_subscriptions",
    "l2_stocks",
    "l2_kinds",
    "traded_shares",
    "threshold",
}
_FLOAT_FIELDS = {"amount", "data_delay_ms", "process_ms", "price"}
_BOOL_FIELDS = {"dry_run", "connected", "account_connected", "real_order_sent"}
MSF_CHAIN_EVENTS = {
    "entry_signal_accepted",
    "entry_signal_blocked",
    "main_keep_decision",
    "main_cancel_decision",
    "dry_run_probe_trade_recorded",
}
MARKDOWN_STOCK_CHAIN_DETAIL_LIMIT = 20
MARKDOWN_STOCK_EVENT_DETAIL_LIMIT = 8


@dataclass(slots=True)
class ParsedLogEvent:
    """A single relevant event extracted from a log line."""

    type: str
    message: str
    source: str = ""
    event: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


def _coerce_scalar(key: str, value: str) -> Any:
    text = str(value).strip().strip('"')
    lower = text.lower()
    if key in _BOOL_FIELDS:
        if lower in {"1", "true", "yes", "on"}:
            return True
        if lower in {"0", "false", "no", "off"}:
            return False
    if key in _INT_FIELDS:
        try:
            return int(float(text))
        except ValueError:
            return text
    if key in _FLOAT_FIELDS:
        try:
            return float(text)
        except ValueError:
            return text
    return text


def parse_key_values(text: str) -> dict[str, Any]:
    """Parse simple ``key=value`` fields from a log message."""

    fields: dict[str, Any] = {}
    for match in _KEY_VALUE_RE.finditer(text):
        key = match.group("key")
        fields[key] = _coerce_scalar(key, match.group("value"))
    return fields


def _split_csv_values(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def extract_log_message(line: str) -> str:
    """Return the human message from JSON or console log lines."""

    text = str(line or "").strip()
    if not text:
        return ""

    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key in ("message", "msg"):
                value = payload.get(key)
                if value is not None:
                    return str(value)

    # Console format from monitor.logger: ``[09:30:00] INFO cytrade.system | message``.
    if " | " in text:
        return text.split(" | ", 1)[1].strip()
    return text


def extract_log_timestamp(line: str) -> str:
    """Return the source timestamp from JSON logs when available."""

    text = str(line or "").strip()
    if not text or not text.startswith("{"):
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("asctime")
    return str(value).strip() if value is not None else ""


def parse_log_line(line: str, *, source: str = "") -> ParsedLogEvent | None:
    """Parse one relevant MainSealFollow log line.

    Unknown lines return ``None`` so callers can stream large log files without
    retaining unrelated output.
    """

    raw = str(line or "").rstrip("\n")
    message = extract_log_message(raw)
    timestamp = extract_log_timestamp(raw)
    if not message:
        return None

    if MSF_PREFIX in message:
        before, payload_text = message.split(MSF_PREFIX, 1)
        payload_text = payload_text.strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = {"_parse_error": payload_text}
        event_name = str(payload.get("event", "")) if isinstance(payload, dict) else ""
        return ParsedLogEvent(
            type="msf_event",
            message=message,
            source=source,
            event=event_name,
            fields={**parse_key_values(before), "_logged_at": timestamp},
            payload=payload if isinstance(payload, dict) else {},
            raw=raw,
        )

    if message.startswith(SESSION_PREFIX):
        rest = message[len(SESSION_PREFIX) :].strip()
        parts = rest.split(None, 1)
        event_name = parts[0] if parts else ""
        field_text = parts[1] if len(parts) > 1 else ""
        return ParsedLogEvent(
            type="monitor_session",
            message=message,
            source=source,
            event=event_name,
            fields={**parse_key_values(field_text), "_logged_at": timestamp},
            raw=raw,
        )

    if "Runtime heartbeat" in message:
        return ParsedLogEvent(
            type="runtime_heartbeat",
            message=message,
            source=source,
            event="heartbeat",
            fields={**parse_key_values(message), "_logged_at": timestamp},
            raw=raw,
        )

    if message.startswith("STRATEGY_SELECTION"):
        fields = {**parse_key_values(message), "_logged_at": timestamp}
        return ParsedLogEvent(
            type="strategy_selection",
            message=message,
            source=source,
            event="stock_selection",
            fields=fields,
            raw=raw,
        )

    if "MainSealFollow" in message and "stock=" in message:
        fields = {**parse_key_values(message), "_logged_at": timestamp}
        if fields.get("stock"):
            return ParsedLogEvent(
                type="strategy_instance",
                message=message,
                source=source,
                event="stock_initialized",
                fields=fields,
                raw=raw,
            )

    if MOCK_TRADE_MARKER in message:
        return ParsedLogEvent(
            type="mock_trade",
            message=message,
            source=source,
            event="mock_trade",
            fields={**parse_key_values(message), "_logged_at": timestamp},
            raw=raw,
        )

    if "[ORDER]" in message or "[TRADE]" in message:
        if any(marker in message for marker in SAFE_ORDER_MARKERS):
            return ParsedLogEvent(
                type="order_log",
                message=message,
                source=source,
                event="non_live_order_log",
                fields={**parse_key_values(message), "_logged_at": timestamp},
                raw=raw,
            )

        if any(marker in message for marker in SUSPICIOUS_ORDER_MARKERS):
            return ParsedLogEvent(
                type="order_or_trade",
                message=message,
                source=source,
                event="possible_real_order_or_trade",
                fields={**parse_key_values(message), "_logged_at": timestamp},
                raw=raw,
            )

    if ("[ORDER]" in message or "[TRADE]" in message) and "[MOCK]" not in message:
        return ParsedLogEvent(
            type="order_or_trade",
            message=message,
            source=source,
            event="possible_real_order_or_trade",
            fields={**parse_key_values(message), "_logged_at": timestamp},
            raw=raw,
        )

    return None


def iter_log_lines(paths: Iterable[str | Path]) -> Iterator[tuple[str, str]]:
    """Yield ``(source, line)`` pairs from existing paths and glob patterns."""

    seen: set[Path] = set()
    for item in paths:
        text = str(item)
        matches = [Path(path) for path in glob.glob(text)] if any(ch in text for ch in "*?[]") else [Path(text)]
        matches.sort(key=lambda path: str(path))
        for path in matches:
            if path in seen or not path.exists() or not path.is_file():
                continue
            seen.add(path)
            with path.open("r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    yield str(path), line


def parse_log_files(paths: Iterable[str | Path]) -> list[ParsedLogEvent]:
    """Parse all relevant events from the supplied log files."""

    events: list[ParsedLogEvent] = []
    for source, line in iter_log_lines(paths):
        event = parse_log_line(line, source=source)
        if event is not None:
            events.append(event)
    return events


def _max_int(values: Iterable[Any]) -> int:
    best = 0
    for value in values:
        try:
            best = max(best, int(value))
        except (TypeError, ValueError):
            continue
    return best


def _latest_event(events: Iterable[ParsedLogEvent], event_type: str) -> ParsedLogEvent | None:
    found: ParsedLogEvent | None = None
    for event in events:
        if event.type == event_type:
            found = event
    return found


def _has_non_empty_value(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text)


def _event_logged_at(event: ParsedLogEvent) -> str:
    return str(event.fields.get("_logged_at") or "").strip()


def _msf_stock_name(event: ParsedLogEvent) -> tuple[str, str]:
    stock = ""
    name = ""
    if isinstance(event.payload, dict):
        stock = str(event.payload.get("stock") or "").strip()
        name = str(event.payload.get("name") or "").strip()
    if not stock:
        stock = str(event.fields.get("stock") or "").strip()
    if not name:
        name = str(event.fields.get("name") or "").strip()
    return stock, name


def _metric_float(metrics: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(metrics.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _metric_int(metrics: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(float(metrics.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def _format_amount_cn(value: Any) -> str:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        return str(value)
    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.2f}亿"
    if abs(amount) >= 10_000:
        return f"{amount / 10_000:.1f}万"
    return f"{amount:.0f}"


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


_EVENT_CN = {
    "entry_signal_accepted": "触发排板",
    "entry_signal_blocked": "排板被阻断",
    "dry_run_probe_trade_recorded": "dry-run 观察单模拟成交",
    "main_keep_decision": "观察单成交后保留主单",
    "main_cancel_decision": "观察单成交后撤主单",
    "queue_cancel_decision": "排队期间撤单判断",
}

_REASON_CN = {
    "limit_price_or_queue_missing": "缺少涨停价或买一队列，无法判断排板位置",
    "main_seal_not_ok": "主封单条件不足",
    "state_not_wait_signal": "策略状态已不是等待信号",
    "cooldown_after_cancel": "刚撤过单，仍在冷却期",
    "simulated_probe_fill": "按 dry-run 规则模拟观察单成交",
    "probe_filled_market_ok": "观察单成交后市场指标仍可接受",
    "market_still_strong_after_probe": "观察单成交后封单仍强",
    "confirmed_limit_buy_cancel_gt_add": "涨停价买单撤单金额大于新增金额",
    "position_danger_and_back_big_empty": "排队位置风险高且后排大单承接不足",
    "queue_timeout_and_back_big_empty": "排队超时且后排大单承接不足",
    "front_big_weak_and_back_big_empty": "前排大单弱且后排大单承接不足",
}


def _describe_msf_event(event_name: str, reason: str, metrics: dict[str, Any]) -> str:
    parts: list[str] = []
    event_cn = _EVENT_CN.get(event_name, event_name or "策略事件")
    reason_cn = _REASON_CN.get(reason, reason or "无原因")
    parts.append(f"{event_cn}：{reason_cn}")

    if event_name in {"entry_signal_accepted", "entry_signal_blocked"}:
        bid1_lot = _metric_int(metrics, "current_bid1_volume_lot")
        front50_lot = _metric_int(metrics, "current_front50_depth_lot")
        queue_count = _metric_int(metrics, "current_queue_count")
        reported_count = _metric_int(metrics, "current_queue_reported_count")
        if bid1_lot or front50_lot or queue_count or reported_count:
            parts.append(
                "队列：买一封单%s手，前50笔合计%s手，队列条数%s/%s"
                % (bid1_lot, front50_lot, queue_count, reported_count)
            )
        if "detected_main_seal" in metrics:
            parts.append(f"主封单识别={_yes_no(metrics.get('detected_main_seal'))}")
        if "recent_big_limit_buy_ok" in metrics:
            parts.append(
                "近期涨停价大买单=%s，数量=%s"
                % (_yes_no(metrics.get("recent_big_limit_buy_ok")), _metric_int(metrics, "recent_big_limit_buy_count"))
            )
        if "recent_big_limit_cancel_blocked" in metrics:
            parts.append(
                "近期大撤买阻断=%s，数量=%s"
                % (
                    _yes_no(metrics.get("recent_big_limit_cancel_blocked")),
                    _metric_int(metrics, "recent_big_limit_cancel_count"),
                )
            )
        if "existing_limit_observed_ms" in metrics:
            parts.append(
                "已封板观察=%sms/阈值%sms"
                % (_metric_int(metrics, "existing_limit_observed_ms"), _metric_int(metrics, "existing_limit_observe_ms"))
            )

    if event_name == "dry_run_probe_trade_recorded":
        parts.append(
            "成交：价格%s，数量%s股，金额%s"
            % (
                metrics.get("fill_price", ""),
                _metric_int(metrics, "fill_qty"),
                _format_amount_cn(metrics.get("fill_amount")),
            )
        )
        parts.append(
            "成交依据：提交后成交%s股，阈值%s股，提交时买一%s手，当前买一%s手"
            % (
                _metric_int(metrics, "traded_shares_after_submit"),
                _metric_int(metrics, "fill_threshold_shares"),
                _metric_int(metrics, "submit_bid1_volume_lot"),
                _metric_int(metrics, "current_bid1_volume_lot"),
            )
        )

    if event_name in {"main_keep_decision", "main_cancel_decision", "queue_cancel_decision"}:
        if "limit_buy_add_amount" in metrics or "limit_buy_cancel_amount" in metrics:
            parts.append(
                "涨停价买单：新增%s，撤单%s，净额%s，撤单/新增比%s"
                % (
                    _format_amount_cn(metrics.get("limit_buy_add_amount")),
                    _format_amount_cn(metrics.get("limit_buy_cancel_amount")),
                    _format_amount_cn(metrics.get("limit_buy_net_amount")),
                    metrics.get("limit_buy_cancel_ratio", ""),
                )
            )
        if "back_big_amount" in metrics or "front_big_amount" in metrics:
            parts.append(
                "大单承接：前排大单%s，后排大单%s，后排大单数%s"
                % (
                    _format_amount_cn(metrics.get("front_big_amount")),
                    _format_amount_cn(metrics.get("back_big_amount")),
                    _metric_int(metrics, "back_big_count"),
                )
            )
        if "probe_fill_ms" in metrics:
            parts.append(f"观察单成交后已等待={_metric_int(metrics, 'probe_fill_ms')}ms")
        if "elapsed_ms" in metrics:
            parts.append(f"排队耗时={_metric_int(metrics, 'elapsed_ms')}ms")

    return "；".join(part for part in parts if part)


def _compact_event_payload(event: ParsedLogEvent) -> dict[str, Any]:
    metrics = event.payload.get("metrics") if isinstance(event.payload, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    reason = str(event.payload.get("reason") or "").strip() if isinstance(event.payload, dict) else ""
    return {
        "time": _event_logged_at(event),
        "event": event.event,
        "state": str(event.payload.get("state") or "").strip() if isinstance(event.payload, dict) else "",
        "reason": reason,
        "source": str(event.payload.get("source") or "").strip() if isinstance(event.payload, dict) else "",
        "dry_run": bool(event.payload.get("dry_run")) if isinstance(event.payload, dict) else False,
        "summary_cn": _describe_msf_event(event.event, reason, metrics),
        "metrics": metrics,
    }


def _summarize_msf_chains(events: list[ParsedLogEvent]) -> list[dict[str, Any]]:
    chains: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for event in events:
        if event.type != "msf_event":
            continue
        stock, name = _msf_stock_name(event)
        if not stock:
            continue
        chain = chains.get(stock)
        if chain is None:
            chain = {
                "stock": stock,
                "name": name,
                "first_seen": _event_logged_at(event),
                "last_seen": _event_logged_at(event),
                "latest_state": str(event.payload.get("state") or "").strip() if isinstance(event.payload, dict) else "",
                "latest_reason": str(event.payload.get("reason") or "").strip() if isinstance(event.payload, dict) else "",
                "event_count": 0,
                "event_counts": Counter(),
                "reason_counts": Counter(),
                "source_counts": Counter(),
                "events": [],
                "entry_signal_accepted_count": 0,
                "entry_signal_blocked_count": 0,
                "main_keep_count": 0,
                "main_cancel_count": 0,
                "dry_run_probe_trade_count": 0,
            }
            chains[stock] = chain
            order.append(stock)
        if not chain["name"] and name:
            chain["name"] = name
        logged_at = _event_logged_at(event)
        if not chain["first_seen"] and logged_at:
            chain["first_seen"] = logged_at
        if logged_at:
            chain["last_seen"] = logged_at
        state = str(event.payload.get("state") or "").strip() if isinstance(event.payload, dict) else ""
        reason = str(event.payload.get("reason") or "").strip() if isinstance(event.payload, dict) else ""
        source = str(event.payload.get("source") or "").strip() if isinstance(event.payload, dict) else ""
        chain["latest_state"] = state or chain["latest_state"]
        chain["latest_reason"] = reason or chain["latest_reason"]
        chain["event_count"] += 1
        chain["event_counts"][event.event] += 1
        if reason:
            chain["reason_counts"][reason] += 1
        if source:
            chain["source_counts"][source] += 1
        chain["events"].append(_compact_event_payload(event))
        if event.event == "entry_signal_accepted":
            chain["entry_signal_accepted_count"] += 1
        elif event.event == "entry_signal_blocked":
            chain["entry_signal_blocked_count"] += 1
        elif event.event == "main_keep_decision":
            chain["main_keep_count"] += 1
        elif event.event == "main_cancel_decision":
            chain["main_cancel_count"] += 1
        elif event.event == "dry_run_probe_trade_recorded":
            chain["dry_run_probe_trade_count"] += 1

    ordered_chains = [chains[stock] for stock in order]
    for chain in ordered_chains:
        chain["event_counts"] = dict(sorted(chain["event_counts"].items()))
        chain["reason_counts"] = dict(chain["reason_counts"].most_common())
        chain["source_counts"] = dict(chain["source_counts"].most_common())
    return ordered_chains


def _format_chain_summary_rows(chains: list[dict[str, Any]]) -> list[str]:
    if not chains:
        return ["- No stock-level `MSF_EVENT` chains found."]
    lines = [
        "| stock | name | events | accepted | blocked | keep | cancel | probe fill | latest state | latest reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for chain in chains:
        lines.append(
            "| {stock} | {name} | {event_count} | {accepted} | {blocked} | {keep} | {cancel} | {probe} | {state} | {reason} |".format(
                stock=chain.get("stock", ""),
                name=chain.get("name", ""),
                event_count=chain.get("event_count", 0),
                accepted=chain.get("entry_signal_accepted_count", 0),
                blocked=chain.get("entry_signal_blocked_count", 0),
                keep=chain.get("main_keep_count", 0),
                cancel=chain.get("main_cancel_count", 0),
                probe=chain.get("dry_run_probe_trade_count", 0),
                state=chain.get("latest_state", ""),
                reason=chain.get("latest_reason", ""),
            )
        )
    return lines


def _stock_ref(chain: dict[str, Any]) -> str:
    stock = str(chain.get("stock") or "").strip()
    name = str(chain.get("name") or "").strip()
    return f"{stock} {name}".strip()


def _summarize_stock_chain_groups(chains: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {
        "entry_accepted": [],
        "blocked_only": [],
        "probe_trade_recorded": [],
        "main_keep_decision": [],
        "main_cancel_decision": [],
    }
    for chain in chains:
        ref = _stock_ref(chain)
        if not ref:
            continue
        accepted = int(chain.get("entry_signal_accepted_count", 0) or 0)
        blocked = int(chain.get("entry_signal_blocked_count", 0) or 0)
        probe = int(chain.get("dry_run_probe_trade_count", 0) or 0)
        keep = int(chain.get("main_keep_count", 0) or 0)
        cancel = int(chain.get("main_cancel_count", 0) or 0)
        if accepted > 0:
            groups["entry_accepted"].append(ref)
        if blocked > 0 and accepted == 0 and probe == 0 and keep == 0 and cancel == 0:
            groups["blocked_only"].append(ref)
        if probe > 0:
            groups["probe_trade_recorded"].append(ref)
        if keep > 0:
            groups["main_keep_decision"].append(ref)
        if cancel > 0:
            groups["main_cancel_decision"].append(ref)
    return groups


def summarize_events(events: Iterable[ParsedLogEvent]) -> dict[str, Any]:
    """Build a deterministic morning-run summary from parsed events."""

    event_list = list(events)
    by_type = Counter(event.type for event in event_list)
    session_events = Counter(event.event for event in event_list if event.type == "monitor_session")
    msf_events = Counter(event.event for event in event_list if event.type == "msf_event")
    blocked_reasons = Counter(
        str(event.payload.get("reason") or "")
        for event in event_list
        if event.type == "msf_event" and event.event == "entry_signal_blocked"
    )
    blocked_reasons.pop("", None)

    heartbeats = [event for event in event_list if event.type == "runtime_heartbeat"]
    session_stop = [event for event in event_list if event.type == "monitor_session" and event.event in {"session_stop", "stopped"}]
    pool_generated = [event for event in event_list if event.type == "monitor_session" and event.event == "pool_generated"]
    strategy_selection = [event for event in event_list if event.type == "strategy_selection"]
    strategy_instances = [event for event in event_list if event.type == "strategy_instance"]

    max_strategies = _max_int(
        [event.fields.get("strategies") for event in heartbeats]
        + [event.fields.get("strategy_count") for event in session_stop]
    )
    max_tick_subscriptions = _max_int(event.fields.get("tick_subscriptions") for event in heartbeats + session_stop)
    max_l2_stocks = _max_int(event.fields.get("l2_stocks") for event in heartbeats + session_stop)
    max_l2_kinds = _max_int(event.fields.get("l2_kinds") for event in heartbeats)
    any_connected = any(event.fields.get("connected") is True for event in heartbeats)
    any_latest_data = any(_has_non_empty_value(event.fields.get("latest_data_time")) for event in heartbeats)

    real_order_suspected = by_type.get("order_or_trade", 0) > 0
    for event in event_list:
        if event.type == "monitor_session" and event.event == "stopped":
            if event.fields.get("real_order_sent") is True:
                real_order_suspected = True

    pool_total = 0
    if pool_generated:
        pool_total = _max_int(event.fields.get("total") for event in pool_generated)
    if pool_total <= 0 and strategy_selection:
        pool_total = _max_int(event.fields.get("total") for event in strategy_selection)

    selected_stocks: list[str] = []
    seen_selected_stocks: set[str] = set()
    for event in strategy_selection:
        for stock in _split_csv_values(event.fields.get("stocks")):
            if stock not in seen_selected_stocks:
                seen_selected_stocks.add(stock)
                selected_stocks.append(stock)
    if not selected_stocks:
        for event in strategy_instances:
            stock = str(event.fields.get("stock") or "").strip()
            if stock and stock not in seen_selected_stocks:
                seen_selected_stocks.add(stock)
                selected_stocks.append(stock)

    stock_chains = _summarize_msf_chains(event_list)
    stock_chain_groups = _summarize_stock_chain_groups(stock_chains)
    stock_chain_counts = Counter(
        chain["event_count"]
        for chain in stock_chains
        if int(chain.get("event_count", 0) or 0) > 0
    )

    invalid_monitor_reason = ""
    if (
        max_strategies > 0
        and heartbeats
        and not any_connected
        and max_tick_subscriptions == 0
        and not any_latest_data
        and sum(msf_events.values()) == 0
    ):
        invalid_monitor_reason = "market_data_not_connected"
    elif not (session_events.get("monitor_start", 0) > 0 or session_events.get("session_start", 0) > 0):
        invalid_monitor_reason = "monitor_session_not_found"

    checks = {
        "pool_generated": (bool(pool_generated) or bool(strategy_selection)) and pool_total > 0,
        "monitor_started": session_events.get("monitor_start", 0) > 0 or session_events.get("session_start", 0) > 0,
        "heartbeat_seen": bool(heartbeats),
        "strategies_after_activation": max_strategies > 0,
        "market_data_active": any_connected and max_tick_subscriptions > 0 and any_latest_data,
        "no_real_order_suspected": not real_order_suspected,
        "session_stopped": bool(session_stop),
        "entry_signal_seen": msf_events.get("entry_signal_accepted", 0) > 0,
        "dry_run_probe_trade_seen": msf_events.get("dry_run_probe_trade_recorded", 0) > 0 or by_type.get("mock_trade", 0) > 0,
        "l2_detail_seen": max_l2_stocks > 0,
        "invalid_monitor_session": bool(invalid_monitor_reason),
    }
    checks["minimum_acceptance"] = all(
        checks[name]
        for name in (
            "pool_generated",
            "monitor_started",
            "heartbeat_seen",
            "strategies_after_activation",
            "market_data_active",
            "no_real_order_suspected",
        )
    )
    review_verdict = "accepted"
    if checks["invalid_monitor_session"]:
        review_verdict = "invalid_monitor_session"
    elif not checks["minimum_acceptance"]:
        review_verdict = "needs_review"

    latest_heartbeat = _latest_event(event_list, "runtime_heartbeat")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "event_counts": dict(sorted(by_type.items())),
        "session_events": dict(sorted(session_events.items())),
        "msf_events": dict(sorted(msf_events.items())),
        "blocked_reasons": dict(blocked_reasons.most_common()),
        "stock_chains": stock_chains,
        "stock_chain_groups": stock_chain_groups,
        "stock_chain_count": len(stock_chains),
        "stocks_with_msf_events": len(stock_chains),
        "stock_chain_event_counts": dict(sorted(stock_chain_counts.items())),
        "pool_total": pool_total,
        "selected_stocks": selected_stocks,
        "heartbeat_count": len(heartbeats),
        "max_strategies": max_strategies,
        "max_tick_subscriptions": max_tick_subscriptions,
        "max_l2_stocks": max_l2_stocks,
        "max_l2_kinds": max_l2_kinds,
        "invalid_monitor_reason": invalid_monitor_reason,
        "review_verdict": review_verdict,
        "mock_trade_count": by_type.get("mock_trade", 0),
        "possible_real_order_count": by_type.get("order_or_trade", 0),
        "real_order_suspected": real_order_suspected,
        "latest_heartbeat": latest_heartbeat.fields if latest_heartbeat else {},
        "checks": checks,
    }


def _status(value: bool) -> str:
    return "PASS" if value else "CHECK"


def format_markdown(summary: dict[str, Any], *, title: str = "MainSealFollow morning review") -> str:
    """Render a Chinese markdown report for human review."""

    checks = summary.get("checks", {})
    lines = [
        f"# {title}",
        "",
        f"Generated at: `{summary.get('generated_at', '')}`",
        "",
        "## Session verdict",
        "",
        f"- Verdict: `{summary.get('review_verdict', 'needs_review')}`",
        f"- Invalid monitor session: `{_status(bool(checks.get('invalid_monitor_session')))}` reason=`{summary.get('invalid_monitor_reason') or 'none'}`",
        "",
        "## Minimum acceptance",
        "",
        f"- Overall: **{_status(bool(checks.get('minimum_acceptance')))}**",
        f"- Pool generated: `{_status(bool(checks.get('pool_generated')))}` total=`{summary.get('pool_total', 0)}`",
        f"- Monitor started: `{_status(bool(checks.get('monitor_started')))}`",
        f"- Heartbeat seen: `{_status(bool(checks.get('heartbeat_seen')))}` count=`{summary.get('heartbeat_count', 0)}`",
        f"- Strategies after activation: `{_status(bool(checks.get('strategies_after_activation')))}` max=`{summary.get('max_strategies', 0)}`",
        f"- Market data active: `{_status(bool(checks.get('market_data_active')))}` tick_subscriptions_max=`{summary.get('max_tick_subscriptions', 0)}` latest_data_time_present=`{bool(summary.get('latest_heartbeat', {}).get('latest_data_time'))}`",
        f"- No real order suspected: `{_status(bool(checks.get('no_real_order_suspected')))}` possible_real_order_lines=`{summary.get('possible_real_order_count', 0)}`",
        "",
        "## Dry-run signal chain",
        "",
        f"- Entry signal accepted: `{_status(bool(checks.get('entry_signal_seen')))}` count=`{summary.get('msf_events', {}).get('entry_signal_accepted', 0)}`",
        f"- Dry-run probe trade recorded: `{_status(bool(checks.get('dry_run_probe_trade_seen')))}` count=`{summary.get('msf_events', {}).get('dry_run_probe_trade_recorded', 0)}` mock_trade_lines=`{summary.get('mock_trade_count', 0)}`",
        f"- Level2 detail seen: `{_status(bool(checks.get('l2_detail_seen')))}` l2_stocks=`{summary.get('max_l2_stocks', 0)}` l2_kinds=`{summary.get('max_l2_kinds', 0)}`",
        f"- Tick subscriptions max: `{summary.get('max_tick_subscriptions', 0)}`",
        f"- Invalid monitor reason: `{summary.get('invalid_monitor_reason') or 'none'}`",
        "",
    ]

    l2_calibration = summary.get("l2_calibration", {})
    lines.extend(["## L2 calibration coverage", ""])
    if l2_calibration:
        missing = list(l2_calibration.get("missing_stocks", []) or [])
        missing_text = ", ".join(missing[:30]) if missing else "none"
        if len(missing) > 30:
            missing_text = f"{missing_text}, ... +{len(missing) - 30} more"
        lines.extend(
            [
                f"- Complete: `{_status(bool(l2_calibration.get('complete')))}`",
                f"- Expected stocks: `{l2_calibration.get('expected_count', 0)}`",
                f"- Files found: `{l2_calibration.get('file_count', 0)}`",
                f"- Matched stocks: `{l2_calibration.get('matched_count', 0)}`",
                f"- Missing stocks: `{len(missing)}` {missing_text}",
                f"- Directory: `{l2_calibration.get('dir', '')}`",
                "",
            ]
        )
    else:
        lines.extend(["- No L2 calibration coverage data available.", ""])

    lines.extend(
        [
            "## Event counters",
            "",
            "### Session events",
        ]
    )

    session_events = summary.get("session_events", {})
    if session_events:
        for key, value in session_events.items():
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- No `MONITOR_SESSION` events found.")

    lines.extend(["", "### MSF events"])
    msf_events = summary.get("msf_events", {})
    if msf_events:
        for key, value in msf_events.items():
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- No `MSF_EVENT` events found.")

    blocked_reasons = summary.get("blocked_reasons", {})
    lines.extend(["", "### Top blocked reasons"])
    if blocked_reasons:
        for key, value in blocked_reasons.items():
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- No blocked reasons found.")

    stock_chains = summary.get("stock_chains", [])
    stock_chain_groups = summary.get("stock_chain_groups", {})
    lines.extend(["", "## Stock outcome groups", ""])
    group_labels = [
        ("entry_accepted", "Triggered entry"),
        ("blocked_only", "Blocked only"),
        ("probe_trade_recorded", "Dry-run probe filled"),
        ("main_keep_decision", "Main keep decision"),
        ("main_cancel_decision", "Main cancel decision"),
    ]
    if stock_chain_groups:
        for key, label in group_labels:
            stocks = list(stock_chain_groups.get(key, []) or [])
            value = ", ".join(stocks[:20]) if stocks else "none"
            if len(stocks) > 20:
                value = f"{value}, ... +{len(stocks) - 20} more"
            lines.append(f"- {label}: `{len(stocks)}` {value}")
    else:
        lines.append("- No stock outcome groups found.")

    lines.extend(["", "## Stock event chains", ""])
    lines.extend(_format_chain_summary_rows(stock_chains))

    if stock_chains:
        detail_limit = MARKDOWN_STOCK_CHAIN_DETAIL_LIMIT
        event_limit = MARKDOWN_STOCK_EVENT_DETAIL_LIMIT
        lines.extend(
            [
                "",
                "### Stock chain details",
                "",
                f"- Detail limit: first `{min(len(stock_chains), detail_limit)}` stocks, first `{event_limit}` events per stock. Full chain is in summary JSON.",
            ]
        )
        for chain in stock_chains[:detail_limit]:
            lines.append("")
            lines.append(f"- `{chain.get('stock', '')}` {chain.get('name', '')} events={chain.get('event_count', 0)}")
            lines.append(
                f"  - latest_state=`{chain.get('latest_state', '')}` latest_reason=`{chain.get('latest_reason', '')}` first_seen=`{chain.get('first_seen', '')}` last_seen=`{chain.get('last_seen', '')}`"
            )
            if chain.get("reason_counts"):
                reasons = ", ".join(f"{key}={value}" for key, value in chain["reason_counts"].items())
                lines.append(f"  - reasons: {reasons}")
            events = list(chain.get("events", []) or [])
            for item in events[:event_limit]:
                metrics = item.get("metrics", {})
                metric_keys = ", ".join(sorted(metrics)) if isinstance(metrics, dict) and metrics else ""
                lines.append(
                    "  - {time} `{event}` state=`{state}` reason=`{reason}` source=`{source}` metrics_keys=`{metrics_keys}`".format(
                        time=item.get("time", ""),
                        event=item.get("event", ""),
                        state=item.get("state", ""),
                        reason=item.get("reason", ""),
                        source=item.get("source", ""),
                        metrics_keys=metric_keys or "none",
                    )
                )
                summary_cn = str(item.get("summary_cn") or "").strip()
                if summary_cn:
                    lines.append(f"    中文摘要：{summary_cn}")
            omitted_events = len(events) - event_limit
            if omitted_events > 0:
                lines.append(f"  - ... omitted `{omitted_events}` more events for this stock; see summary JSON.")
        omitted_chains = len(stock_chains) - detail_limit
        if omitted_chains > 0:
            lines.append("")
            lines.append(f"- ... omitted `{omitted_chains}` more stock chains; see summary JSON.")

    latest_heartbeat = summary.get("latest_heartbeat", {})
    lines.extend(["", "## Latest heartbeat snapshot", ""])
    if latest_heartbeat:
        for key in sorted(latest_heartbeat):
            lines.append(f"- `{key}`: `{latest_heartbeat[key]}`")
    else:
        lines.append("- No heartbeat snapshot available.")

    lines.extend(
        [
            "",
            "## Review notes",
            "",
            "- 这份报告只判断 dry-run 监测链路，不判断策略收益。",
            "- 如果 verdict=`invalid_monitor_session`，先修复监测会话、行情连接或订阅问题，再做策略调参。",
            "- `CHECK` 不一定代表失败；例如没有涨停机会时，`entry_signal_accepted` 可能自然为 0。",
            "- 如果出现 possible real order lines，必须先人工确认日志来源，再继续任何自动修复。",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse MainSealFollow monitor logs and produce a replay summary.")
    parser.add_argument("logs", nargs="*", help="Log paths or glob patterns, e.g. logs/system.*.log logs/trade.*.log")
    parser.add_argument("--output", help="Write markdown summary to this path.")
    parser.add_argument("--json-output", help="Write machine-readable summary JSON to this path.")
    parser.add_argument("--title", default="MainSealFollow morning review")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs = args.logs or ["logs/system.*.log", "logs/trade.*.log", "logs/system.log", "logs/trade.log"]
    events = parse_log_files(logs)
    summary = summarize_events(events)
    markdown = format_markdown(summary, title=args.title)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    else:
        print(markdown)

    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    return 0 if summary.get("checks", {}).get("no_real_order_suspected", False) else 2


if __name__ == "__main__":
    raise SystemExit(main())
