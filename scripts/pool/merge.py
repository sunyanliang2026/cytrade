from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from scripts.pool.common import (
    OUTPUT_HEADERS,
    PoolCandidate,
    format_plan_amount,
    is_main_board_code,
    is_non_st_name,
    normalize_stock_code,
)


def merge_candidates(candidates: Iterable[PoolCandidate]) -> list[PoolCandidate]:
    merged: list[PoolCandidate] = []
    seen: set[str] = set()
    for item in candidates:
        code = normalize_stock_code(item.code)
        if not code or code in seen:
            continue
        if not is_main_board_code(code):
            continue
        if not is_non_st_name(item.name):
            continue
        seen.add(code)
        merged.append(
            PoolCandidate(
                code=code,
                name=item.name,
                pct_change=item.pct_change,
                last_price=item.last_price,
                pre_close=item.pre_close,
                amount=item.amount,
                float_market_value=item.float_market_value,
                max_amplitude_30d=item.max_amplitude_30d,
            )
        )
    return merged


def candidate_code_set(candidates: Iterable[PoolCandidate]) -> set[str]:
    return {normalize_stock_code(item.code) for item in merge_candidates(candidates)}


def filter_candidates_by_base(candidates: Iterable[PoolCandidate], base_codes: set[str]) -> list[PoolCandidate]:
    if not base_codes:
        return []
    return [item for item in candidates if normalize_stock_code(item.code) in base_codes]


def union_candidate_sets(candidate_sets: Iterable[Iterable[PoolCandidate]]) -> list[PoolCandidate]:
    rows: list[PoolCandidate] = []
    for candidates in candidate_sets:
        rows.extend(candidates)
    return merge_candidates(rows)


def intersect_candidate_sets(candidate_sets: list[list[PoolCandidate]]) -> list[PoolCandidate]:
    if not candidate_sets:
        return []
    normalized_sets = [merge_candidates(candidates) for candidates in candidate_sets]
    code_sets = [{normalize_stock_code(item.code) for item in candidates} for candidates in normalized_sets]
    common_codes = set.intersection(*code_sets) if code_sets else set()
    return [item for item in normalized_sets[0] if normalize_stock_code(item.code) in common_codes]


def evaluate_candidate_expression(expression: object, named_sets: dict[str, list[PoolCandidate]]) -> list[PoolCandidate]:
    if isinstance(expression, str):
        return named_sets.get(expression, [])
    if isinstance(expression, list):
        return union_candidate_sets(evaluate_candidate_expression(item, named_sets) for item in expression)
    if not isinstance(expression, dict):
        raise RuntimeError(f"股票池 final 表达式格式错误: {expression!r}")
    if "union" in expression:
        items = expression.get("union")
        if not isinstance(items, list):
            raise RuntimeError("股票池 final.union 必须是数组")
        return union_candidate_sets(evaluate_candidate_expression(item, named_sets) for item in items)
    if "intersect" in expression:
        items = expression.get("intersect")
        if not isinstance(items, list):
            raise RuntimeError("股票池 final.intersect 必须是数组")
        return intersect_candidate_sets([evaluate_candidate_expression(item, named_sets) for item in items])
    raise RuntimeError(f"股票池 final 表达式不支持: {expression!r}")


def write_pool(
    candidates: list[PoolCandidate],
    output_path: Path,
    plan_amount: float,
    *,
    backup_existing: bool = True,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_existing and output_path.exists():
        backup_path = output_path.with_name(
            f"{output_path.stem}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{output_path.suffix}"
        )
        backup_path.write_bytes(output_path.read_bytes())

    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(OUTPUT_HEADERS)
        for item in candidates:
            writer.writerow([item.code, item.name, format_plan_amount(plan_amount)])
    temp_path.replace(output_path)
