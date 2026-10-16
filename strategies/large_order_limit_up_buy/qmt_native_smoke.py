# coding: utf-8
"""Minimal read-only script for QMT's Native Python execution mode."""

import datetime
import os
import sys


def _log_path():
    if len(sys.argv) > 1:
        log_dir = os.path.join(sys.argv[1], "log")
        if os.path.isdir(log_dir):
            return os.path.join(log_dir, _log_name())
    return os.path.join(os.environ.get("TEMP", os.getcwd()), _log_name())


def _log_name():
    return "qmt_native_smoke_{}.log".format(
        datetime.datetime.now().strftime("%Y%m%d"))


def _log(message):
    line = "QMT_NATIVE_SMOKE {} time={}".format(
        message, datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3])
    if sys.stdout is not None:
        print(line)
        sys.stdout.flush()
    path = _log_path()
    try:
        with open(path, "a") as output:
            output.write(line + "\n")
    except OSError as exc:
        if sys.stderr is not None:
            print("QMT_NATIVE_SMOKE LOG_WRITE_FAILED {}".format(exc), file=sys.stderr)


if __name__ == "__main__":
    _log("SCRIPT_START python={} executable={} argv={}".format(
        sys.version.split()[0], sys.executable, len(sys.argv)))
    _log("SCRIPT_OK")
