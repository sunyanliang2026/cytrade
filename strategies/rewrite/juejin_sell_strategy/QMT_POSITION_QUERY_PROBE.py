# coding: utf-8
"""Read-only QMT probe for position-query callback context."""

import datetime as _dt
import io
import os


QMT_HOME = u"C:/\u5149\u5927\u8bc1\u5238\u91d1\u9633\u5149QMT\u5b9e\u76d8"
ACCOUNT_ID = u""
ACCOUNT_TYPE = "stock"
TARGET_CODE = "002343.SZ"
QUOTE_PERIOD = "tick"

_C = None
_LOG = None
_LOG_PATH = ""
_SUB_ID = None
_PROBED_SITES = set()
_QUOTE_SEEN = False


def _log(message):
    line = "%s %s" % (
        _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], message
    )
    try:
        print(line)
    except Exception:
        pass
    if _LOG:
        try:
            _LOG.write(line + "\n")
            _LOG.flush()
        except Exception:
            pass


def _field(source, *names):
    if isinstance(source, dict):
        for name in names:
            value = source.get(name)
            if value not in (None, ""):
                return value
        return None
    for name in names:
        try:
            value = getattr(source, name)
        except Exception:
            continue
        if value not in (None, ""):
            return value
    return None


def _stock_code(value):
    text = str(value or "").strip().upper()
    suffix = text.split(".", 1)[1] if "." in text else ""
    digits = "".join(ch for ch in text.split(".", 1)[0] if ch.isdigit())
    if len(digits) != 6:
        digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return ""
    if suffix in ("SH", "SZ", "BJ"):
        market = suffix
    elif digits.startswith(("5", "6", "9")):
        market = "SH"
    elif digits.startswith(("8", "4", "92")):
        market = "BJ"
    else:
        market = "SZ"
    return digits + "." + market


def _records(result):
    if result is None:
        return []
    if isinstance(result, dict):
        return list(result.values())
    if isinstance(result, (list, tuple)):
        return list(result)
    return [result]


def _probe_position(site):
    if site in _PROBED_SITES:
        return
    _PROBED_SITES.add(site)

    if not str(ACCOUNT_ID or "").strip():
        _log("[QUERY_SKIP] site=%s reason=ACCOUNT_ID_not_configured" % site)
        return

    getter = globals().get("get_trade_detail_data")
    if not callable(getter):
        _log("[QUERY_UNAVAILABLE] site=%s reason=get_trade_detail_data_missing" % site)
        return

    try:
        result = getter(ACCOUNT_ID, ACCOUNT_TYPE, "POSITION")
    except Exception as exc:
        error = str(exc)
        account_id = str(ACCOUNT_ID or "")
        if account_id:
            error = error.replace(account_id, "<redacted>")
        _log("[QUERY_EXCEPTION] site=%s type=%s error=%s" % (
            site, type(exc).__name__, error
        ))
        return

    rows = _records(result)
    matches = []
    for row in rows:
        code = _stock_code(_field(
            row, "m_strInstrumentID", "m_strOrderCode", "stockCode", "stock_code", "code"
        ))
        if code == TARGET_CODE:
            matches.append(row)

    if not matches:
        _log("[QUERY_RESULT] site=%s result_type=%s positions=%d target=%s matched=0" % (
            site, type(result).__name__, len(rows), TARGET_CODE
        ))
        return

    volumes = []
    for row in matches:
        available = _field(
            row, "m_nCanUseVolume", "can_use_volume", "canUseVolume",
            "available_quantity", "m_nVolumeAvailable", "m_nAvailable"
        )
        try:
            volumes.append(int(float(available)))
        except (TypeError, ValueError, OverflowError):
            volumes.append("unknown")
    _log("[QUERY_RESULT] site=%s result_type=%s positions=%d target=%s matched=%d can_sell=%s" % (
        site, type(result).__name__, len(rows), TARGET_CODE, len(matches), volumes
    ))


def _on_quote(data):
    global _QUOTE_SEEN
    if not _QUOTE_SEEN:
        _QUOTE_SEEN = True
        event_time = _field(data, "stime", "time", "timestamp")
        _log("[QUOTE_FIRST] code=%s period=%s event_time=%s" % (
            TARGET_CODE, QUOTE_PERIOD, event_time
        ))
        _probe_position("quote_callback")


def init(C):
    global _C, _LOG, _LOG_PATH, _SUB_ID
    _C = C

    log_dir = os.path.join(QMT_HOME, "userdata", "log")
    try:
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir)
        day = _dt.datetime.now().strftime("%Y%m%d")
        _LOG_PATH = os.path.join(log_dir, "qmt_position_probe_%s.log" % day)
        _LOG = io.open(_LOG_PATH, "a", encoding="utf-8")
    except Exception:
        _LOG = None
        _LOG_PATH = ""

    _log("[INIT] read_only=True target=%s period=%s account_configured=%s log=%s" % (
        TARGET_CODE, QUOTE_PERIOD, bool(str(ACCOUNT_ID or "").strip()),
        _LOG_PATH or "<console>"
    ))
    _probe_position("init")

    try:
        _SUB_ID = C.subscribe_quote(
            TARGET_CODE, period=QUOTE_PERIOD, dividend_type="follow",
            result_type="dict", callback=_on_quote
        )
        _log("[SUBSCRIBE] code=%s period=%s id=%s" % (TARGET_CODE, QUOTE_PERIOD, _SUB_ID))
    except Exception as exc:
        _log("[SUBSCRIBE_FAILED] code=%s period=%s type=%s error=%s" % (
            TARGET_CODE, QUOTE_PERIOD, type(exc).__name__, str(exc)
        ))


def handlebar(C):
    _probe_position("handlebar")


def stop(C):
    global _SUB_ID
    if _SUB_ID:
        try:
            C.unsubscribe_quote(_SUB_ID)
            _log("[UNSUBSCRIBE] id=%s" % _SUB_ID)
        except Exception as exc:
            _log("[UNSUBSCRIBE_FAILED] type=%s error=%s" % (
                type(exc).__name__, str(exc)
            ))
        _SUB_ID = None
    _log("[STOP] quote_received=%s probed_sites=%s" % (
        _QUOTE_SEEN, ",".join(sorted(_PROBED_SITES))
    ))
    if _LOG:
        try:
            _LOG.close()
        except Exception:
            pass


__all__ = ["init", "handlebar", "stop"]
