"""CSV loader for overnight limit-up buy orders."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path


class OrderCsvError(ValueError):
    """Raised when the order CSV does not match the required schema."""


@dataclass(frozen=True)
class OrderRequest:
    """One requested overnight buy order from CSV."""

    row_no: int
    stock_code: str
    amount: float


REQUIRED_COLUMNS = ("stock_code", "amount")


def load_order_requests(csv_path: str | Path) -> list[OrderRequest]:
    """Load stock_code/amount rows from a strict two-column CSV."""

    path = Path(csv_path)
    if not path.is_file():
        raise OrderCsvError(f"CSV file not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [str(name or "").strip() for name in (reader.fieldnames or [])]
        if tuple(fieldnames) != REQUIRED_COLUMNS:
            raise OrderCsvError(
                f"CSV must contain exactly these columns: {','.join(REQUIRED_COLUMNS)}"
            )

        rows: list[OrderRequest] = []
        for row_no, row in enumerate(reader, start=2):
            stock_code = normalize_stock_code(row.get("stock_code", ""))
            amount = parse_amount(row.get("amount", ""), row_no=row_no)
            if not stock_code:
                raise OrderCsvError(f"row {row_no}: invalid stock_code")
            rows.append(OrderRequest(row_no=row_no, stock_code=stock_code, amount=amount))
    return rows


def normalize_stock_code(value: object) -> str:
    """Normalize stock symbols to the internal 6-digit code format."""

    text = str(value or "").strip().upper()
    if not text:
        return ""
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if not match:
        return ""
    return match.group(1)


def parse_amount(value: object, *, row_no: int) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text:
        raise OrderCsvError(f"row {row_no}: amount is required")
    try:
        amount = float(text)
    except ValueError as exc:
        raise OrderCsvError(f"row {row_no}: invalid amount {value!r}") from exc
    if amount <= 0:
        raise OrderCsvError(f"row {row_no}: amount must be > 0")
    return amount
