from __future__ import annotations

import importlib
from collections.abc import Iterable


def to_strategy_spec(strategy_class_or_spec) -> str:
    if isinstance(strategy_class_or_spec, str):
        return strategy_class_or_spec

    module_name = getattr(strategy_class_or_spec, "__module__", "")
    qualname = getattr(strategy_class_or_spec, "__name__", "")
    if not module_name or not qualname:
        raise ValueError(f"Cannot serialize strategy definition: {strategy_class_or_spec!r}")
    return f"{module_name}:{qualname}"


def normalize_strategy_specs(strategy_classes=None) -> list[str]:
    return [to_strategy_spec(item) for item in (strategy_classes or [])]


def resolve_strategy_specs(strategy_specs: Iterable[str]) -> list[type]:
    resolved = []
    for spec in strategy_specs:
        module_name, class_name = str(spec).split(":", 1)
        module = importlib.import_module(module_name)
        resolved.append(getattr(module, class_name))
    return resolved
