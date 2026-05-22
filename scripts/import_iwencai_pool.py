"""Import an iWenCai export file into MainSealFollow stock-pool CSV.

Usage:
    python scripts/import_iwencai_pool.py input.csv
    python scripts/import_iwencai_pool.py input.xlsx --amount 20000
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import pandas as pd


DEFAULT_OUTPUT = Path("config/main_seal_follow_pool.csv")
OUTPUT_HEADERS = ["股票代码", "名称", "计划买入金额"]

CODE_COLUMNS = (
    "股票代码",
    "证券代码",
    "代码",
    "code",
    "stock_code",
)
NAME_COLUMNS = (
    "股票简称",
    "股票名称",
    "证券简称",
    "名称",
    "name",
    "stock_name",
)


def _normalize_column_name(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    return text


def _find_column(columns: list[str], candidates: tuple[str, ...]) -> str:
    normalized_map = {_normalize_column_name(column): column for column in columns}
    for candidate in candidates:
        column = normalized_map.get(_normalize_column_name(candidate))
        if column:
            return column
    for column in columns:
        normalized = _normalize_column_name(column).lower()
        if any(_normalize_column_name(candidate).lower() in normalized for candidate in candidates):
            return column
    return ""


def _normalize_stock_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text or text == "NAN":
        return ""
    match = re.search(r"(\d{6})", text)
    if not match:
        return ""
    return match.group(1)


def _read_input(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        try:
            return pd.read_excel(path)
        except ImportError as exc:
            raise SystemExit(
                "读取 Excel 失败：当前环境缺少 Excel engine。请先导出 CSV，或安装 openpyxl/xlrd。"
            ) from exc
    return pd.read_csv(path, encoding="utf-8-sig")


def import_pool(input_path: Path, output_path: Path, amount: float) -> int:
    if not input_path.is_file():
        raise SystemExit(f"输入文件不存在: {input_path}")

    df = _read_input(input_path)
    columns = [str(column) for column in df.columns]
    code_column = _find_column(columns, CODE_COLUMNS)
    name_column = _find_column(columns, NAME_COLUMNS)

    if not code_column:
        raise SystemExit(f"没有识别到股票代码列。当前列: {', '.join(columns)}")

    seen: set[str] = set()
    rows: list[list[object]] = []
    for _, item in df.iterrows():
        code = _normalize_stock_code(item.get(code_column))
        if not code or code in seen:
            continue
        seen.add(code)
        name = str(item.get(name_column, "") or "").strip() if name_column else ""
        if name.lower() == "nan":
            name = ""
        rows.append([code, name, float(amount)])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(OUTPUT_HEADERS)
        writer.writerows(rows)

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="导入问财导出文件，生成 MainSealFollow 股票池。")
    parser.add_argument("input", help="问财导出的 CSV/XLSX 文件路径")
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"输出股票池路径，默认 {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=10_000.0,
        help="每只股票计划买入金额，默认 10000",
    )
    args = parser.parse_args()

    count = import_pool(Path(args.input), Path(args.output), args.amount)
    print(f"已生成股票池: {args.output}，股票数: {count}，计划买入金额: {args.amount:g}")


if __name__ == "__main__":
    main()
