"""Locate and expose a local ``xtquant`` package before runtime imports."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path


_CONFIG_DIR = Path(__file__).resolve().parent
_DEFAULT_LOCAL_RUNTIME_CONFIG_PATH = _CONFIG_DIR / "local_runtime.json"


def _load_local_runtime_config() -> dict:
    config_path = Path(
        os.getenv("CYTRADE_LOCAL_SETTINGS_PATH", str(_DEFAULT_LOCAL_RUNTIME_CONFIG_PATH))
    )
    if not config_path.exists():
        return {}

    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _explicit_roots(raw_path: str) -> list[Path]:
    if not raw_path:
        return []

    path = Path(raw_path).expanduser()
    if path.is_file():
        return [path.parent]
    if path.name.lower() == "xtquant":
        return [path.parent]
    return [path]


def _qmt_candidate_roots(raw_qmt_path: str) -> list[Path]:
    if not raw_qmt_path:
        return []

    qmt_path = Path(raw_qmt_path).expanduser()
    bases: list[Path]
    if qmt_path.suffix.lower() == ".exe":
        bases = [qmt_path.parent, qmt_path.parent.parent]
    else:
        bases = [qmt_path, qmt_path.parent, qmt_path.parent.parent]

    roots: list[Path] = []
    for base in bases:
        if not str(base) or str(base) == ".":
            continue
        roots.extend(
            [
                base,
                base.parent,
                base / "bin.x64",
                base / "Lib" / "site-packages",
                base / "bin.x64" / "Lib" / "site-packages",
            ]
        )
    return roots


def _project_candidate_roots() -> list[Path]:
    project_root = _CONFIG_DIR.parent
    return [
        project_root,
        project_root / "vendor",
    ]


def _iter_candidate_roots(qmt_path: str, xtquant_path: str) -> list[str]:
    candidates = (
        _explicit_roots(xtquant_path)
        + _qmt_candidate_roots(qmt_path)
        + _project_candidate_roots()
    )

    roots: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            normalized = str(candidate.resolve())
        except Exception:
            normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        xtquant_dir = Path(normalized) / "xtquant"
        if xtquant_dir.is_dir():
            roots.append(normalized)
    return roots


def _current_xtquant_root() -> str:
    spec = importlib.util.find_spec("xtquant")
    if spec is None or not spec.submodule_search_locations:
        return ""

    package_dir = Path(next(iter(spec.submodule_search_locations)))
    try:
        return str(package_dir.parent.resolve())
    except Exception:
        return str(package_dir.parent)


def bootstrap_xtquant_sys_path(qmt_path: str = "", xtquant_path: str = "") -> str:
    """Ensure a local ``xtquant`` package root is present in ``sys.path``."""
    current_root = _current_xtquant_root()
    if current_root:
        return current_root

    local_runtime = _load_local_runtime_config()
    resolved_qmt_path = qmt_path or os.getenv("QMT_PATH", "") or str(local_runtime.get("QMT_PATH", "") or "")
    resolved_xtquant_path = (
        xtquant_path
        or os.getenv("XTQUANT_PATH", "")
        or str(local_runtime.get("XTQUANT_PATH", "") or "")
    )

    for root in _iter_candidate_roots(resolved_qmt_path, resolved_xtquant_path):
        if root not in sys.path:
            # Keep the project root ahead of external vendor roots to avoid
            # shadowing local packages such as ``strategy`` or ``config``.
            insert_at = 1 if sys.path else 0
            sys.path.insert(insert_at, root)
            importlib.invalidate_caches()
        current_root = _current_xtquant_root()
        if current_root:
            return current_root

    return ""
