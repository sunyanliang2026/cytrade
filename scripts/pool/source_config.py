from __future__ import annotations

import json
from pathlib import Path

from scripts.pool.common import (
    DEFAULT_JIUYANGONGSHE_USER_URL,
    DEFAULT_SOURCE_CONFIG,
    JiuyangongsheConfig,
    SourceSet,
)
from scripts.pool.iwencai_source import IwencaiQuery, parse_iwencai_query_items


def load_source_config(config_path: Path) -> dict:
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"股票池来源配置文件格式错误: {path}") from exc
    return value if isinstance(value, dict) else {}


def read_iwencai_queries_from_source_config(config_path: Path) -> list[IwencaiQuery]:
    config = load_source_config(config_path)
    iwencai_config = config.get("iwencai", {})
    if not isinstance(iwencai_config, dict):
        raise RuntimeError(f"iwencai 配置必须是对象: {config_path}")
    raw_queries = iwencai_config.get("queries", [])
    return parse_iwencai_query_items(raw_queries, source_name=str(config_path))


def read_source_sets_from_config(config_path: Path) -> tuple[dict[str, SourceSet], object]:
    config = load_source_config(config_path)
    raw_sets = config.get("sets")
    if not isinstance(raw_sets, dict):
        return {}, None
    sets: dict[str, SourceSet] = {}
    for name, raw in raw_sets.items():
        if not isinstance(raw, dict) or not raw.get("enabled", True):
            continue
        source = str(raw.get("source", "") or "").strip().lower()
        if source not in ("iwencai", "jiuyangongshe"):
            raise RuntimeError(f"集合 {name} 的 source 只能是 iwencai/jiuyangongshe")
        sets[str(name)] = SourceSet(
            name=str(name),
            source=source,
            query=str(raw.get("query", "") or "").strip(),
            node=str(raw.get("node", "") or "").strip(),
        )
    return sets, config.get("final")


def resolve_jiuyangongshe_config(args) -> JiuyangongsheConfig:
    source_config = Path(str(getattr(args, "source_config", DEFAULT_SOURCE_CONFIG) or DEFAULT_SOURCE_CONFIG))
    config = load_source_config(source_config)
    raw = config.get("jiuyangongshe", {})
    raw = raw if isinstance(raw, dict) else {}
    return JiuyangongsheConfig(
        enabled=bool(raw.get("enabled", True)),
        user_url=str(getattr(args, "jiuyangongshe_user_url", "") or raw.get("user_url", "") or DEFAULT_JIUYANGONGSHE_USER_URL),
        require_today=bool(raw.get("require_today", True)),
    )
