"""Persist launcher output even when the independent probe fails before recording."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    now = datetime.now()
    directory = Path(__file__).resolve().parents[1] / "data" / "latency_probe" / "launcher" / now.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now:%H%M%S}_{os.getpid()}.log"
    command = [sys.executable, "-u", "-m", "strategies.large_order_limit_up_buy.scripts.probe_l2_latency", *sys.argv[1:]]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    code = 1
    with path.open("w", encoding="utf-8", buffering=1) as handle:
        handle.write(json.dumps({"event": "launcher_start", "time": now.isoformat(),
                                 "pid": os.getpid(), "command": command}, ensure_ascii=False) + "\n")
        handle.flush()
        try:
            result = subprocess.run(command, cwd=root, env=env, stdout=handle, stderr=subprocess.STDOUT, check=False)
            code = result.returncode
        except Exception as exc:
            handle.write(f"launcher_error: {type(exc).__name__}: {exc}\n")
        handle.write(json.dumps({"event": "launcher_finish", "time": datetime.now().isoformat(), "exit_code": code}) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
