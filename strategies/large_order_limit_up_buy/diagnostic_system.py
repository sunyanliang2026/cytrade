"""Read-only OS/clock samples and allowlisted QMT quote-log evidence."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def quote_log_fields(line):
    """Never persist original QMT lines: some other QMT logs contain credentials."""
    if not any(word in line for word in ("data return", "sendRequest", "disconnect", "timeout", "subscribeQuote", "unsubscribeQuote")):
        return None
    timestamp = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,\.]\d+)", line)
    meta = re.search(r"meta:\s*([A-Z]+_\d{6}_\d+_\d+)", line)
    result = {"qmt_log_time": timestamp.group(1) if timestamp else "", "meta": meta.group(1) if meta else ""}
    for field, pattern in {
        "data_range": r"datas:\[([\d, ]+)\]", "function": r"func:(\w+)",
        "total_ms": r"total time: ([\d.]+)ms", "net_ms": r"net time: ([\d.]+)ms",
        "net_process_ms": r"net process time: ([\d.]+)ms", "decompress_ms": r"decompress time: ([\d.]+)ms",
        "count": r"count: (\d+)",
    }.items():
        match = re.search(pattern, line)
        if match:
            result[field] = match.group(1)
    return result


def clock_snapshot(directory):
    record = {"local_time": datetime.now().isoformat(), "utc_time": datetime.now(timezone.utc).isoformat(),
              "time_zone": time.tzname, "wall_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns()}
    if os.name == "nt":
        for key, command in (("w32tm_status", ["w32tm", "/query", "/status"]),
                             ("w32tm_ntp_sample", ["w32tm", "/stripchart", "/computer:time.windows.com", "/samples:1", "/dataonly"])):
            try:
                result = subprocess.run(command, capture_output=True, timeout=4, check=False)
                record[key] = {"exit_code": result.returncode, "text": result.stdout.decode("mbcs", errors="replace")}
                if key == "w32tm_ntp_sample":
                    offsets = re.findall(r"([+-]\d+\.\d+)s", record[key]["text"])
                    record["ntp_offset_seconds"] = [float(value) for value in offsets]
            except Exception as exc:
                record[key] = {"error": str(exc)}
    with (directory / "clock.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def sample_system(directory: Path, parent_pid: int, stop):
    import psutil
    from xtquant import xtdata
    processes = {}
    quote_file = None
    previous = (time.time(), time.perf_counter())
    last_scan = 0.0
    last_clock = 0.0
    last_signal = ""
    try:
        clock_snapshot(directory)
        last_clock = time.perf_counter()
        qmt_dir = Path(xtdata.get_data_dir()).parent
        quote_path = qmt_dir / "log" / f"XtMiniQuote_{datetime.now():%Y%m%d}.log"
        if quote_path.exists():
            quote_file = quote_path.open(encoding="utf-8", errors="replace")
            quote_file.seek(0, 2)
        with (directory / "system.jsonl").open("a", encoding="utf-8") as handle, \
             (directory / "qmt_quote_evidence.jsonl").open("a", encoding="utf-8") as quotes:
            while not stop.is_set():
                wall, mono = time.time(), time.perf_counter()
                if mono - last_scan >= 30:
                    last_scan = mono
                    for process in psutil.process_iter(["pid", "name"]):
                        if process.pid in (parent_pid, os.getpid()) or process.info["name"] in {"XtMiniQmt.exe", "XtMiniQuote.exe", "XtClient.exe"}:
                            if process.pid not in processes:
                                process.cpu_percent()
                                processes[process.pid] = process
                record = {"time": wall, "clock_step_ms": ((wall - previous[0]) - (mono - previous[1])) * 1000,
                          "total_disk_io": psutil.disk_io_counters()._asdict() if psutil.disk_io_counters() else {}, "processes": []}
                previous = (wall, mono)
                for pid, process in list(processes.items()):
                    try:
                        record["processes"].append({"pid": pid, "name": process.name(), "cpu_percent": process.cpu_percent(),
                                                    "rss": process.memory_info().rss, "threads": [t._asdict() for t in process.threads()],
                                                    "io": process.io_counters()._asdict(), "context_switches": process.num_ctx_switches()._asdict()})
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        processes.pop(pid, None)
                handle.write(json.dumps(record) + "\n")
                if quote_file:
                    budget = 128000
                    while budget > 0:
                        position = quote_file.tell()
                        line = quote_file.readline()
                        if not line or not line.endswith("\n"):
                            quote_file.seek(position)
                            break
                        budget -= len(line)
                        fields = quote_log_fields(line)
                        if fields:
                            quotes.write(json.dumps({"observed_time": wall, **fields}) + "\n")
                handle.flush()
                quotes.flush()
                signal_file = directory.parent / "signals.jsonl"
                signal_text = signal_file.read_text(encoding="utf-8") if signal_file.exists() else ""
                if signal_text != last_signal and mono - last_clock >= 60:
                    # Clock sampling has no authority to alter or resynchronize the clock.
                    last_clock = mono
                    last_signal = signal_text
                    clock_snapshot(directory)
                stop.wait(1)
    except Exception as exc:
        (directory / "system_error.json").write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), encoding="utf-8")
    finally:
        if quote_file:
            quote_file.close()
