"""Compatibility wrapper around the current agent safety scan."""

from __future__ import annotations

from agent.gates.quality_gate import scan_diff_for_safety

__all__ = ["scan_diff_for_safety"]
