"""Analyze raw OpeningAuctionL2Probe JSONL output.

This script is intentionally offline-only: it reads probe artifacts and writes
review summaries, without connecting to QMT or touching execution settings.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_RAW_NAME = "opening_l2_raw.jsonl"
DEFAULT_JSON_NAME = "opening_l2_analysis.json"
DEFAULT_MD_NAME = "opening_l2_analysis.md"


def parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for candidate in (text, text.replace("T", " ")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def exchange_for(stock: str) -> str:
    text = str(stock or "").strip()
    if text.startswith("6"):
        return "SH"
    if text.startswith(("0", "2", "3")):
        return "SZ"
    return "UNKNOWN"


def counter_to_dict(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): int(value) for key, value in sorted(counter.items(), key=lambda item: str(item[0]))}


def _inc_nested(root: dict[str, Any], keys: tuple[str, ...], amount: int = 1) -> None:
    node = root
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = int(node.get(keys[-1], 0)) + amount


def _update_time_bounds(bucket: dict[str, Any], event_time: datetime | None) -> None:
    if event_time is None:
        return
    value = event_time.isoformat(sep=" ", timespec="milliseconds")
    if not bucket.get("first_event_time") or value < bucket["first_event_time"]:
        bucket["first_event_time"] = value
    if not bucket.get("last_event_time") or value > bucket["last_event_time"]:
        bucket["last_event_time"] = value


def _field_value(row: dict[str, Any], field: str) -> object:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    normalized = row.get("normalized") if isinstance(row.get("normalized"), dict) else {}
    if field in normalized:
        return normalized.get(field)
    return raw.get(field)


def analyze_raw_jsonl(raw_path: Path, *, max_lines: int = 0) -> dict[str, Any]:
    totals = {
        "events": 0,
        "malformed_lines": 0,
        "by_kind": Counter(),
        "by_phase": Counter(),
        "by_subscribe_mode": Counter(),
        "by_exchange": Counter(),
        "by_exchange_kind": {},
        "by_phase_kind": {},
    }
    stock_modes: dict[tuple[str, str], dict[str, Any]] = {}
    field_distributions = {
        "l2transaction.tradeFlag": Counter(),
        "l2transaction.tradeType": Counter(),
        "l2transaction.side": Counter(),
        "l2order.entrustDirection": Counter(),
        "l2order.entrustType": Counter(),
        "l2order.side": Counter(),
    }

    with raw_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if max_lines and line_no > max_lines:
                break
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                totals["malformed_lines"] += 1
                continue

            stock = str(row.get("stock") or "").strip()
            mode = str(row.get("subscribe_mode") or "unknown").strip() or "unknown"
            kind = str(row.get("kind") or "unknown").strip() or "unknown"
            phase = str(row.get("phase") or "unknown").strip() or "unknown"
            exchange = exchange_for(stock)
            event_time = parse_dt(row.get("event_time"))

            totals["events"] += 1
            totals["by_kind"][kind] += 1
            totals["by_phase"][phase] += 1
            totals["by_subscribe_mode"][mode] += 1
            totals["by_exchange"][exchange] += 1
            _inc_nested(totals["by_exchange_kind"], (exchange, kind))
            _inc_nested(totals["by_phase_kind"], (phase, kind))

            key = (stock, mode)
            bucket = stock_modes.setdefault(
                key,
                {
                    "stock": stock,
                    "exchange": exchange,
                    "subscribe_mode": mode,
                    "events": 0,
                    "by_kind": Counter(),
                    "by_phase": Counter(),
                    "final_10s_by_kind": Counter(),
                    "open_5m_by_kind": Counter(),
                    "auction_by_kind": Counter(),
                    "first_event_time": "",
                    "last_event_time": "",
                    "has_l2_auction": False,
                    "has_l2_2450_2500": False,
                    "has_l2_open_5m": False,
                },
            )
            bucket["events"] += 1
            bucket["by_kind"][kind] += 1
            bucket["by_phase"][phase] += 1
            _update_time_bounds(bucket, event_time)
            if bool(row.get("in_auction")):
                bucket["has_l2_auction"] = True
                bucket["auction_by_kind"][kind] += 1
            if bool(row.get("in_final_10s")):
                bucket["has_l2_2450_2500"] = True
                bucket["final_10s_by_kind"][kind] += 1
            if bool(row.get("in_open_5m")):
                bucket["has_l2_open_5m"] = True
                bucket["open_5m_by_kind"][kind] += 1

            if kind == "l2transaction":
                field_distributions["l2transaction.tradeFlag"][_field_value(row, "trade_flag") or _field_value(row, "tradeFlag")] += 1
                field_distributions["l2transaction.tradeType"][_field_value(row, "trade_type") or _field_value(row, "tradeType")] += 1
                field_distributions["l2transaction.side"][_field_value(row, "side") or ""] += 1
            elif kind == "l2order":
                field_distributions["l2order.entrustDirection"][
                    _field_value(row, "entrust_direction") or _field_value(row, "entrustDirection")
                ] += 1
                field_distributions["l2order.entrustType"][_field_value(row, "entrust_type") or _field_value(row, "entrustType")] += 1
                field_distributions["l2order.side"][_field_value(row, "side") or ""] += 1

    stock_rows = []
    for bucket in stock_modes.values():
        row = dict(bucket)
        for key in ("by_kind", "by_phase", "final_10s_by_kind", "open_5m_by_kind", "auction_by_kind"):
            row[key] = counter_to_dict(row[key])
        stock_rows.append(row)

    stock_rows.sort(key=lambda item: (item["subscribe_mode"], item["stock"]))
    covered_10s = sum(1 for item in stock_rows if item["has_l2_2450_2500"])
    covered_open = sum(1 for item in stock_rows if item["has_l2_open_5m"])

    return {
        "raw_path": str(raw_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "max_lines": max_lines,
        "totals": {
            "events": totals["events"],
            "malformed_lines": totals["malformed_lines"],
            "stock_mode_rows": len(stock_rows),
            "covered_2450_2500_rows": covered_10s,
            "covered_open_5m_rows": covered_open,
            "by_kind": counter_to_dict(totals["by_kind"]),
            "by_phase": counter_to_dict(totals["by_phase"]),
            "by_subscribe_mode": counter_to_dict(totals["by_subscribe_mode"]),
            "by_exchange": counter_to_dict(totals["by_exchange"]),
            "by_exchange_kind": totals["by_exchange_kind"],
            "by_phase_kind": totals["by_phase_kind"],
        },
        "field_distributions": {key: counter_to_dict(counter) for key, counter in field_distributions.items()},
        "stock_modes": stock_rows,
    }


def render_markdown(analysis: dict[str, Any]) -> str:
    totals = analysis["totals"]
    lines = [
        "# Opening Auction L2 Probe Analysis",
        "",
        f"- raw_path: `{analysis['raw_path']}`",
        f"- generated_at: `{analysis['generated_at']}`",
        f"- events: `{totals['events']}`",
        f"- stock_mode_rows: `{totals['stock_mode_rows']}`",
        f"- covered_2450_2500_rows: `{totals['covered_2450_2500_rows']}`",
        f"- covered_open_5m_rows: `{totals['covered_open_5m_rows']}`",
        f"- malformed_lines: `{totals['malformed_lines']}`",
        "",
        "## Totals",
        "",
        "### By Kind",
        "",
    ]
    lines.extend(_markdown_kv_table(totals["by_kind"], "kind", "count"))
    lines.extend(["", "### By Phase", ""])
    lines.extend(_markdown_kv_table(totals["by_phase"], "phase", "count"))
    lines.extend(["", "### Field Distributions", ""])
    for name, values in analysis["field_distributions"].items():
        lines.extend([f"#### {name}", ""])
        lines.extend(_markdown_kv_table(values, "value", "count"))
        lines.append("")

    lines.extend(["## Stock Coverage", "", "| stock | mode | exchange | final_10s | open_5m | final_10s_by_kind | open_5m_by_kind |", "| --- | --- | --- | --- | --- | --- | --- |"])
    for row in analysis["stock_modes"]:
        lines.append(
            "| {stock} | {subscribe_mode} | {exchange} | {final_10s} | {open_5m} | `{final_kinds}` | `{open_kinds}` |".format(
                stock=row["stock"],
                subscribe_mode=row["subscribe_mode"],
                exchange=row["exchange"],
                final_10s=row["has_l2_2450_2500"],
                open_5m=row["has_l2_open_5m"],
                final_kinds=json.dumps(row["final_10s_by_kind"], ensure_ascii=False, sort_keys=True),
                open_kinds=json.dumps(row["open_5m_by_kind"], ensure_ascii=False, sort_keys=True),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _markdown_kv_table(values: dict[str, Any], key_name: str, value_name: str) -> list[str]:
    lines = [f"| {key_name} | {value_name} |", "| --- | ---: |"]
    for key, value in values.items():
        lines.append(f"| `{key}` | {value} |")
    return lines


def resolve_raw_path(input_dir: str, raw_path: str) -> Path:
    if raw_path:
        return Path(raw_path)
    if not input_dir:
        raise SystemExit("Use --input-dir or --raw.")
    return Path(input_dir) / DEFAULT_RAW_NAME


def run_analysis(args: argparse.Namespace) -> dict[str, Path]:
    raw_path = resolve_raw_path(args.input_dir, args.raw)
    if not raw_path.exists():
        raise SystemExit(f"Raw JSONL not found: {raw_path}")
    output_dir = Path(args.output_dir) if args.output_dir else raw_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = Path(args.output_json) if args.output_json else output_dir / DEFAULT_JSON_NAME
    md_path = Path(args.output_md) if args.output_md else output_dir / DEFAULT_MD_NAME

    analysis = analyze_raw_jsonl(raw_path, max_lines=args.max_lines)
    json_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(analysis), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze OpeningAuctionL2Probe raw JSONL output.")
    parser.add_argument("--input-dir", default="", help="Probe output directory containing opening_l2_raw.jsonl.")
    parser.add_argument("--raw", default="", help="Explicit opening_l2_raw.jsonl path.")
    parser.add_argument("--output-dir", default="", help="Directory for analysis outputs. Defaults to raw file directory.")
    parser.add_argument("--output-json", default="", help="Explicit JSON output path.")
    parser.add_argument("--output-md", default="", help="Explicit markdown output path.")
    parser.add_argument("--max-lines", type=int, default=0, help="Optional debug limit; 0 means all lines.")
    return parser


def main() -> None:
    parser = build_parser()
    paths = run_analysis(parser.parse_args())
    print(f"JSON_PATH {paths['json']}")
    print(f"MARKDOWN_PATH {paths['markdown']}")


if __name__ == "__main__":
    main()
