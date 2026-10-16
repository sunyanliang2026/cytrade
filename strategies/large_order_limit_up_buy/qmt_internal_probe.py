# coding: utf-8
"""Read-only quote and Level2 probe for QMT Native Python mode."""

import datetime
import os
import sys
import time


STOCK_CODE = "000001.SZ"
SUMMARY_INTERVAL_SECONDS = 60
RUN_SECONDS = 30
PERIODS = ("tick", "l2quote", "l2order", "l2transaction", "l2orderqueue")
STATE = {}
LOG_PATH = None


def _resolve_log_path():
    candidates = []
    if len(sys.argv) > 1:
        candidates.append(sys.argv[1])
    candidates.append(os.getcwd())
    for base in candidates:
        if not base:
            continue
        log_dir = os.path.join(base, "log")
        if os.path.isdir(log_dir):
            return os.path.join(
                log_dir,
                "qmt_probe_{}.log".format(
                    datetime.datetime.now().strftime("%Y%m%d")))
    return os.path.join(
        os.environ.get("TEMP", os.getcwd()),
        "qmt_probe_{}.log".format(datetime.datetime.now().strftime("%Y%m%d")))


def _log(message):
    global LOG_PATH
    line = "QMT_PROBE {} time={}".format(
        message, datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3])
    if sys.stdout is not None:
        print(line)
        sys.stdout.flush()
    if LOG_PATH is None:
        LOG_PATH = _resolve_log_path()
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a") as output:
                output.write(line + "\n")
        except Exception:
            pass


_log("MODULE_LOADED")


def _error(exc):
    return "{}: {}".format(type(exc).__name__, str(exc)[:120])


def _payload(data):
    if isinstance(data, dict) and STOCK_CODE in data:
        return data[STOCK_CODE]
    return data


def _batch_size(payload):
    if isinstance(payload, (list, tuple)):
        return len(payload)
    if isinstance(payload, dict):
        values = payload.get("time")
        if values is None:
            values = next((value for value in payload.values() if isinstance(value, (list, tuple))), None)
        if values is not None and hasattr(values, "__len__") and not isinstance(values, (str, bytes)):
            return len(values)
        return 1 if payload else 0
    return 0 if payload is None else 1


def _sample(payload):
    if isinstance(payload, (list, tuple)):
        payload = payload[-1] if payload else None
    if not isinstance(payload, dict):
        return "type={}".format(type(payload).__name__)
    fields = ",".join(sorted(str(key) for key in payload.keys())[:12])
    event_time = payload.get("time", payload.get("stime", ""))
    if hasattr(event_time, "__len__") and not isinstance(event_time, (str, bytes, dict)):
        event_time = event_time[-1] if len(event_time) else ""
    return "time={} fields={}".format(str(event_time)[:30], fields)


def _summary(force=False):
    if not STATE:
        return
    now = time.monotonic()
    if not force and now - STATE["last_summary"] < SUMMARY_INTERVAL_SECONDS:
        return
    STATE["last_summary"] = now
    counts = " ".join("{}={}".format(kind, STATE["counts"][kind]) for kind in PERIODS)
    _log("SUMMARY stock={} {}".format(STOCK_CODE, counts))


def _on_data(kind, data):
    if not STATE:
        return
    payload = _payload(data)
    count = _batch_size(payload)
    STATE["counts"][kind] += count
    STATE["last_callback"][kind] = time.monotonic()
    if count and not STATE["first_seen"][kind]:
        STATE["first_seen"][kind] = True
        _log("CALLBACK_FIRST kind={} rows={} {}".format(
            kind, count, _sample(payload)))
    _summary()


def on_tick(data):
    _on_data("tick", data)


def on_l2quote(data):
    _on_data("l2quote", data)


def on_l2order(data):
    _on_data("l2order", data)


def on_l2transaction(data):
    _on_data("l2transaction", data)


def on_l2orderqueue(data):
    _on_data("l2orderqueue", data)


CALLBACKS = (on_tick, on_l2quote, on_l2order, on_l2transaction, on_l2orderqueue)


def _snapshot(xtdata):
    try:
        _log("SNAPSHOT_BEGIN stock={}".format(STOCK_CODE))
        tick = _payload(xtdata.get_full_tick([STOCK_CODE]))
        _log("SNAPSHOT stock={} available={} {}".format(
            STOCK_CODE, bool(tick), _sample(tick)))
    except Exception as exc:
        _log("SNAPSHOT failed {}".format(_error(exc)))


def main(xtdata=None):
    global LOG_PATH
    _log("SCRIPT_START mode=native_python stock={}".format(STOCK_CODE))
    STATE.clear()
    STATE.update({
        "counts": dict((kind, 0) for kind in PERIODS),
        "first_seen": dict((kind, False) for kind in PERIODS),
        "last_callback": dict((kind, None) for kind in PERIODS),
        "subscriptions": {},
        "last_summary": time.monotonic(),
    })
    LOG_PATH = _resolve_log_path()
    if xtdata is None:
        try:
            from xtquant import xtdata
            _log("IMPORT xtdata=ok")
        except Exception as exc:
            _log("IMPORT xtdata=failed {}".format(_error(exc)))
            STATE.clear()
            return 1
    else:
        _log("IMPORT xtdata=provided")

    try:
        _snapshot(xtdata)
        for kind, callback in zip(PERIODS, CALLBACKS):
            try:
                _log("SUBSCRIBE_BEGIN kind={} code={}".format(kind, STOCK_CODE))
                sub_id = xtdata.subscribe_quote(STOCK_CODE, period=kind, callback=callback)
                if not isinstance(sub_id, int) or sub_id <= 0:
                    _log("SUBSCRIBE kind={} failed id={}".format(kind, sub_id))
                    continue
                STATE["subscriptions"][kind] = sub_id
                _log("SUBSCRIBE kind={} ok id={}".format(kind, sub_id))
            except Exception as exc:
                _log("SUBSCRIBE kind={} failed {}".format(kind, _error(exc)))
        _log("SUBSCRIBE_END subscriptions={}".format(len(STATE["subscriptions"])))
        if not STATE["subscriptions"]:
            return 1
        deadline = time.monotonic() + RUN_SECONDS
        while time.monotonic() < deadline:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
            _summary()
        _snapshot(xtdata)
        return 0
    except KeyboardInterrupt:
        _log("INTERRUPTED")
        return 0
    except Exception as exc:
        _log("RUN failed {}".format(_error(exc)))
        return 1
    finally:
        _log("STOP_BEGIN subscriptions={}".format(len(STATE["subscriptions"])))
        _summary(force=True)
        for kind, sub_id in list(STATE["subscriptions"].items()):
            try:
                xtdata.unsubscribe_quote(sub_id)
                _log("UNSUBSCRIBE kind={} ok".format(kind))
            except Exception as exc:
                _log("UNSUBSCRIBE kind={} failed {}".format(kind, _error(exc)))
        _log("STOP_END")
        STATE.clear()


if __name__ == "__main__":
    sys.exit(main())
