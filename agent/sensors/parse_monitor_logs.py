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
)
SUSPICIOUS_ORDER_MARKERS = (
    "LIVE preflight passed",
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

    checks = {
        "pool_generated": bool(pool_generated) and pool_total > 0,
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
        "pool_total": pool_total,
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
        "## Event counters",
        "",
        "### Session events",
    ]

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
            "- 这份报告只判断 dry-run 监控链路，不判断策略收益。",
            "- 如果 verdict=`invalid_monitor_session`，先修复行情连接或订阅问题，再做任何策略调参。",
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
