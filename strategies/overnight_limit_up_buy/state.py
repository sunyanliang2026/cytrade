"""Persistent idempotency state for the overnight limit-up buy strategy."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class SubmissionStateStore:
    """Track submitted CSV rows to avoid duplicate overnight orders."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data = self._load()

    def was_submitted(self, trade_day: str, request_key: str) -> bool:
        key = self._key(trade_day, request_key)
        return key in self._data.get("submitted", {})

    def record(self, trade_day: str, request_key: str, payload: dict[str, Any]) -> None:
        key = self._key(trade_day, request_key)
        submitted = self._data.setdefault("submitted", {})
        submitted[key] = {
            **dict(payload or {}),
            "trade_day": str(trade_day),
            "request_key": str(request_key),
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"submitted": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"submitted": {}}
        if not isinstance(data, dict):
            return {"submitted": {}}
        submitted = data.get("submitted")
        if not isinstance(submitted, dict):
            data["submitted"] = {}
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    @staticmethod
    def _key(trade_day: str, request_key: str) -> str:
        return f"{str(trade_day)}:{str(request_key)}"
