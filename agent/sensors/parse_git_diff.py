"""Parse git unified diffs into changed files and coarse risk hints."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+)$")

HIGH_RISK_PATH_HINTS = (
    ".env",
    "config/local_runtime.json",
    "config/local_runtime.example.json",
)

HIGH_RISK_CONTENT_HINTS = (
    "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=False",
    "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=false",
    "real_order_sent=true",
    "ACCOUNT_PASSWORD",
    "DINGTALK_SECRET",
)


def parse_git_diff(text: str) -> dict[str, Any]:
    files: list[str] = []
    for line in str(text or "").splitlines():
        match = _DIFF_HEADER_RE.match(line)
        if match:
            files.append(match.group("new"))

    risky_paths = [path for path in files if any(hint in path for hint in HIGH_RISK_PATH_HINTS)]
    risky_content = [hint for hint in HIGH_RISK_CONTENT_HINTS if hint in text]
    return {
        "changed_files": files,
        "changed_file_count": len(files),
        "risky_paths": risky_paths,
        "risky_content_hints": risky_content,
        "has_high_risk_hint": bool(risky_paths or risky_content),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse a unified diff into a JSON summary.")
    parser.add_argument("path", help="Diff file path.")
    parser.add_argument("--output", help="Optional JSON output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = parse_git_diff(Path(args.path).read_text(encoding="utf-8", errors="replace"))
    data = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(data, encoding="utf-8")
    else:
        print(data)
    return 0 if not result.get("has_high_risk_hint") else 2


if __name__ == "__main__":
    raise SystemExit(main())
