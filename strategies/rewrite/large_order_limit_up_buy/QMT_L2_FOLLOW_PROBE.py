# coding: gbk
"""One-shot live probe for limit-up L2 follower validation."""

import copy
import datetime as _dt
import io
import json
import os
import time


ENABLE_LIVE_ORDERS = True
ACCOUNT_ID = "32011276"
ACCOUNT_TYPE = "stock"
TARGET_CODE = "603278.SH"
PROBE_QTY = 100
STRATEGY_NAME = "qmt_l2_follow_probe_603278"
PASSORDER_OPERATION = 23
ORDER_TYPE_BUY = 1101
PRICE_TYPE_LIMIT = 11
QUICK_TRADE = 2

VERIFY_WINDOW_SECONDS = 1.0
VERIFY_MAX_EVENTS = 150
VERIFY_LARGE_AMOUNT = 1500000.0
VERIFY_REQUIRED_LARGE_COUNT = 2
MAX_QUOTE_AGE_MS = 1000.0
PREFLIGHT_WINDOW_SECONDS = 5.0
ORDER_LOOKUP_INTERVAL_SECONDS = 0.1
ORDER_LOOKUP_MAX_ATTEMPTS = 10

QMT_HOME = u"C:/\u5149\u5927\u8bc1\u5238\u91d1\u9633\u5149QMT\u5b9e\u76d8"

_C = None
_LOG = None
_LOG_PATH = ""
_STATE_PATH = ""
_SUB_ID = None
_QUOTE_SUB_ID = None
_PREFLIGHT_TIMER = None
_PREFLIGHT_ACTIVE = False
_SNAPSHOT_NEEDS_REFRESH = False
_TOKEN = ""
_LIMIT_UP = None
_ORDER_ID = ""
_ORDER_STATUS = ""
_ORDER_QTY = PROBE_QTY
_ORDER_FILLED = 0.0
_SEEN_ORDER_IDS = set()
_SEEN_DEAL_IDS = set()
_VALIDATION_ACTIVE = False
_VALIDATION_RESULT = ""
_VALIDATION_STARTED = None
_VALIDATION_CUTOFF = None
_VALIDATION_TIMER = None
_VERIFY_COUNT = 0
_VERIFY_LARGE_COUNT = 0
_VERIFY_LARGE_IDS = set()
_STALE_COUNT = 0
_ORDER_LOOKUP_TIMER = None
_ORDER_LOOKUP_ATTEMPTS = 0
_ORDER_LOOKUP_ERROR_LOGGED = False
_CANCEL_DUE = False
_CANCEL_DUE_REASON = ""
_CANCEL_REQUESTED = False
_CANCEL_CONFIRMED = False
_CANCEL_FAILED = False
_ORDER_TERMINAL = False
_ATTEMPTED = False
_STATE = {}


def _log(message):
    line = "%s %s" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], message)
    try:
        print(line)
    except Exception:
        pass
    if _LOG is not None:
        try:
            _LOG.write(line + "\n")
            _LOG.flush()
        except Exception:
            pass


def _open_log():
    global _LOG, _LOG_PATH, _STATE_PATH
    log_dir = os.path.join(QMT_HOME, "userdata", "log")
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)
    day = _dt.datetime.now().strftime("%Y%m%d")
    _LOG_PATH = os.path.join(log_dir, "qmt_l2_follow_probe_%s.log" % day)
    _STATE_PATH = os.path.join(log_dir, "qmt_l2_follow_probe_603278_state.json")
    _LOG = io.open(_LOG_PATH, "a", encoding="utf-8")


def _field(source, *names):
    if isinstance(source, dict):
        for name in names:
            value = source.get(name)
            if value is not None and value != "":
                return value
    else:
        for name in names:
            try:
                value = getattr(source, name)
            except Exception:
                continue
            if value is not None and value != "":
                return value
    return None


def _number(value):
    try:
        number = float(value)
        if number == number and number > 0:
            return number
    except Exception:
        pass
    return None


def _first_number(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return _number(value)


def _normalize_code(value):
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 6:
        return digits + (".SH" if digits.startswith(("5", "6", "9")) else ".SZ")
    return text


def _payload(data):
    value = data
    if isinstance(data, dict):
        if TARGET_CODE in data:
            value = data.get(TARGET_CODE)
        else:
            for key, item in data.items():
                if _normalize_code(key) == TARGET_CODE:
                    value = item
                    break
    try:
        return copy.deepcopy(value) if isinstance(value, dict) else {}
    except Exception:
        return dict(value) if isinstance(value, dict) else {}


def _event_datetime(value):
    if value is None:
        return None
    text = str(value).strip()
    integer, dot, fraction = text.partition(".")
    digits = "".join(ch for ch in integer if ch.isdigit())
    fraction = "".join(ch for ch in fraction if ch.isdigit()) if dot else ""
    try:
        if len(digits) == 10:
            return _dt.datetime.fromtimestamp(int(digits) + (float("0." + fraction) if fraction else 0.0))
        if len(digits) == 13:
            return _dt.datetime.fromtimestamp(int(digits) / 1000.0)
        if len(digits) >= 14:
            stamp = _dt.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
            extra = digits[14:] + fraction
        elif len(digits) >= 9:
            stamp = _dt.datetime.combine(
                _dt.datetime.now().date(), _dt.datetime.strptime(digits[:6], "%H%M%S").time())
            extra = digits[6:] + fraction
        elif len(digits) >= 6:
            stamp = _dt.datetime.combine(
                _dt.datetime.now().date(), _dt.datetime.strptime(digits[-6:], "%H%M%S").time())
            extra = fraction
        else:
            return None
        if extra:
            micros = int((extra + "000000")[:6])
            stamp += _dt.timedelta(microseconds=micros)
        return stamp
    except Exception:
        return None


def _event_seconds(value):
    stamp = _event_datetime(value)
    if stamp is None:
        return None
    return stamp.hour * 3600.0 + stamp.minute * 60.0 + stamp.second + stamp.microsecond / 1000000.0


def _wall_seconds_since_midnight():
    now = _dt.datetime.now()
    return now.hour * 3600.0 + now.minute * 60.0 + now.second + now.microsecond / 1000000.0


def _continuous_session(value):
    seconds = _event_seconds(value)
    if seconds is None:
        return False
    return (9 * 3600 + 30 * 60 <= seconds <= 11 * 3600 + 30 * 60 or
            13 * 3600 <= seconds <= 15 * 3600)


def _quote_delay_ms(value):
    event_at = _event_datetime(value)
    if event_at is None:
        return None
    return (time.time() - time.mktime(event_at.timetuple()) - event_at.microsecond / 1000000.0) * 1000.0


def _read_existing_state():
    if not os.path.isfile(_STATE_PATH):
        return False
    try:
        with io.open(_STATE_PATH, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception as exc:
        _log("[STATE_READ_FAILED] path=%s error=%r; fail closed" % (_STATE_PATH, exc))
        return True
    today = _dt.datetime.now().strftime("%Y%m%d")
    if state.get("trade_day") == today and state.get("attempted"):
        _log("[ABORT] a probe submission was already reserved today token=%s phase=%s" % (
            state.get("token", ""), state.get("phase", "unknown")))
        return True
    return False


def _save_state():
    if not _STATE_PATH:
        return False
    temp_path = _STATE_PATH + ".tmp"
    try:
        with io.open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(_STATE, handle, sort_keys=True)
            handle.flush()
        os.replace(temp_path, _STATE_PATH)
        return True
    except Exception as exc:
        _log("[STATE_WRITE_FAILED] path=%s error=%r" % (_STATE_PATH, exc))
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return False


def _set_phase(phase):
    _STATE["phase"] = phase
    _STATE["updated_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    _save_state()


def _snapshot(C):
    global _LIMIT_UP, _SNAPSHOT_NEEDS_REFRESH
    try:
        detail = C.get_instrument_detail(TARGET_CODE)
        full = C.get_full_tick([TARGET_CODE])
    except Exception as exc:
        _log("[PREFLIGHT_FAILED] market query error=%r" % (exc,))
        return False
    quote = _payload(full)
    _LIMIT_UP = (_number(_field(detail, "UpStopPrice", "upLimit", "limitUp", "upperLimit")) or
                 _number(_field(quote, "upLimit", "up_limit", "upperLimit", "limitUp", "UpStopPrice")))
    last = _number(_field(quote, "lastPrice", "last", "price"))
    bid = _first_number(_field(quote, "bidPrice", "bid", "bid1", "bidPrice1"))
    bid_volume = _first_number(_field(quote, "bidVol", "bidVolume", "bid1_volume", "bidVolume1"))
    event_raw = _field(quote, "time", "stime", "timestamp")
    session_raw = _field(quote, "stime", "time", "timestamp")
    delay_ms = _quote_delay_ms(event_raw)
    _log("[SNAPSHOT] code=%s limit_up=%s last=%s bid1=%s bid1_volume=%s "
         "event=%s delay_ms=%s" % (
             TARGET_CODE, _LIMIT_UP, last, bid, bid_volume, event_raw,
             "%.0f" % delay_ms if delay_ms is not None else "unknown"))
    if not _LIMIT_UP or not last or not bid or not bid_volume:
        _log("[ABORT] snapshot is missing upper-limit, last price, or bid queue")
        return False
    if abs(last - _LIMIT_UP) >= 1e-8 or abs(bid - _LIMIT_UP) >= 1e-8:
        _log("[ABORT] latest snapshot is not sealed at the upper limit")
        return False
    if not _continuous_session(session_raw):
        _log("[ABORT] snapshot time is outside continuous trading")
        return False
    _SNAPSHOT_NEEDS_REFRESH = (
        delay_ms is None or delay_ms < -1000.0 or delay_ms > MAX_QUOTE_AGE_MS)
    if _SNAPSHOT_NEEDS_REFRESH:
        _log("[SNAPSHOT_STALE] quote_age_ms=%s max_ms=%.0f; waiting for fresh l2quote" % (
            "unknown" if delay_ms is None else "%.0f" % delay_ms,
            MAX_QUOTE_AGE_MS))
    else:
        _log("[PREFLIGHT_OK] sealed_limit_up=true quote_age_ms=%.0f" % delay_ms)
    return True


def _subscribe_l2():
    global _SUB_ID
    try:
        _SUB_ID = _C.subscribe_quote(
            TARGET_CODE, period="l2order", dividend_type="follow",
            result_type="dict", callback=_l2order_callback)
    except Exception as exc:
        _log("[SUBSCRIBE_FAILED] code=%s period=l2order error=%r" % (TARGET_CODE, exc))
        return False
    try:
        valid = int(_SUB_ID) > 0
    except Exception:
        valid = bool(_SUB_ID)
    if not valid:
        _log("[SUBSCRIBE_FAILED] code=%s period=l2order return=%r" % (TARGET_CODE, _SUB_ID))
        _SUB_ID = None
        return False
    _log("[SUBSCRIBED] code=%s period=l2order sub_id=%s" % (TARGET_CODE, _SUB_ID))
    return True


def _subscribe_preflight_l2quote():
    global _QUOTE_SUB_ID
    try:
        _QUOTE_SUB_ID = _C.subscribe_quote(
            TARGET_CODE, period="l2quote", dividend_type="follow",
            result_type="dict", callback=_preflight_l2quote_callback)
    except Exception as exc:
        _log("[SUBSCRIBE_FAILED] code=%s period=l2quote error=%r" % (TARGET_CODE, exc))
        return False
    try:
        valid = int(_QUOTE_SUB_ID) > 0
    except Exception:
        valid = bool(_QUOTE_SUB_ID)
    if not valid:
        _log("[SUBSCRIBE_FAILED] code=%s period=l2quote return=%r" % (
            TARGET_CODE, _QUOTE_SUB_ID))
        _QUOTE_SUB_ID = None
        return False
    _log("[SUBSCRIBED] code=%s period=l2quote sub_id=%s" % (
        TARGET_CODE, _QUOTE_SUB_ID))
    return True


def _schedule(delay, callback, name):
    if _C is None:
        return None
    try:
        return _C.schedule_run(
            lambda ContextInfo, cb=callback: cb(),
            _dt.datetime.now() + _dt.timedelta(seconds=delay),
            repeat_times=0, name=name)
    except Exception as exc:
        _log("[SCHEDULE_FAILED] name=%s error=%r" % (name, exc))
        return None


def _cancel_timer(name):
    global _VALIDATION_TIMER, _ORDER_LOOKUP_TIMER, _PREFLIGHT_TIMER
    if name == "validation":
        timer = _VALIDATION_TIMER
    elif name == "lookup":
        timer = _ORDER_LOOKUP_TIMER
    else:
        timer = _PREFLIGHT_TIMER
    if timer is not None and _C is not None:
        try:
            _C.cancel_schedule_run(timer)
        except Exception as exc:
            _log("[TIMER_CANCEL_FAILED] name=%s error=%r" % (name, exc))
    if name == "validation":
        _VALIDATION_TIMER = None
    elif name == "lookup":
        _ORDER_LOOKUP_TIMER = None
    else:
        _PREFLIGHT_TIMER = None


def _unsubscribe_preflight_l2quote():
    global _QUOTE_SUB_ID
    if _QUOTE_SUB_ID is None or _C is None:
        return
    try:
        result = _C.unsubscribe_quote(_QUOTE_SUB_ID)
        _log("[UNSUBSCRIBED] period=l2quote sub_id=%s return=%r" % (
            _QUOTE_SUB_ID, result))
    except Exception as exc:
        _log("[UNSUBSCRIBE_FAILED] period=l2quote sub_id=%s error=%r" % (
            _QUOTE_SUB_ID, exc))
    _QUOTE_SUB_ID = None


def _abort_preflight(reason):
    global _PREFLIGHT_ACTIVE
    _PREFLIGHT_ACTIVE = False
    _cancel_timer("preflight")
    _log("[ABORT] %s; no order sent" % reason)
    _unsubscribe_l2()


def _preflight_timeout():
    global _PREFLIGHT_TIMER
    if not _PREFLIGHT_ACTIVE:
        return
    _PREFLIGHT_TIMER = None
    _abort_preflight("no fresh sealed l2quote within %.1fs" % PREFLIGHT_WINDOW_SECONDS)


def _preflight_l2quote_callback(payload):
    global _PREFLIGHT_ACTIVE
    if not _PREFLIGHT_ACTIVE or _ATTEMPTED:
        return
    data = _payload(payload)
    data_code = _field(data, "stockCode", "stock_code", "code", "instrumentID")
    if data_code and _normalize_code(data_code) != TARGET_CODE:
        return
    event_raw = _field(data, "time", "stime", "timestamp")
    session_raw = _field(data, "stime", "time", "timestamp")
    delay_ms = _quote_delay_ms(event_raw)
    last = _number(_field(data, "lastPrice", "last", "price", "close"))
    bid = _first_number(_field(data, "bidPrice", "bid", "bid1", "bidPrice1"))
    bid_volume = _first_number(_field(data, "bidVol", "bidVolume", "bid1_volume", "bidVolume1"))
    _log("[PREFLIGHT_L2QUOTE] code=%s event=%s last=%s bid1=%s bid1_volume=%s delay_ms=%s" % (
        TARGET_CODE, event_raw, last, bid, bid_volume,
        "%.0f" % delay_ms if delay_ms is not None else "unknown"))
    if not _continuous_session(session_raw):
        return
    if not last or not bid or not bid_volume or not _LIMIT_UP:
        return
    if delay_ms is None or delay_ms < -1000.0 or delay_ms > MAX_QUOTE_AGE_MS:
        return
    if abs(last - _LIMIT_UP) >= 1e-8 or abs(bid - _LIMIT_UP) >= 1e-8:
        _abort_preflight("fresh l2quote shows the limit-up seal has opened")
        return
    _PREFLIGHT_ACTIVE = False
    _cancel_timer("preflight")
    _log("[PREFLIGHT_L2QUOTE_OK] sealed_limit_up=true quote_age_ms=%.0f" % delay_ms)
    _unsubscribe_preflight_l2quote()
    _submit_probe()


def _start_validation():
    global _VALIDATION_ACTIVE, _VALIDATION_STARTED, _VALIDATION_CUTOFF
    global _VALIDATION_TIMER, _VALIDATION_RESULT, _VERIFY_COUNT, _VERIFY_LARGE_COUNT
    global _STALE_COUNT
    _VALIDATION_STARTED = time.monotonic()
    _VALIDATION_CUTOFF = _wall_seconds_since_midnight()
    _VALIDATION_ACTIVE = True
    _VALIDATION_RESULT = ""
    _VERIFY_COUNT = 0
    _VERIFY_LARGE_COUNT = 0
    _VERIFY_LARGE_IDS.clear()
    _STALE_COUNT = 0
    _VALIDATION_TIMER = _schedule(
        VERIFY_WINDOW_SECONDS, lambda: _validation_timer(_TOKEN),
        "qmt_l2_probe_validation")
    if _VALIDATION_TIMER is None:
        _VALIDATION_ACTIVE = False
        _log("[ABORT] validation timer unavailable; no order sent")
        return False
    _log("[FOLLOW_VERIFY_START] code=%s token=%s start=before_passorder window_ms=%.0f "
         "max_events=%d required_large=%d min_amount=%.0f" % (
             TARGET_CODE, _TOKEN, VERIFY_WINDOW_SECONDS * 1000.0,
             VERIFY_MAX_EVENTS, VERIFY_REQUIRED_LARGE_COUNT, VERIFY_LARGE_AMOUNT))
    return True


def _stop_validation(reason):
    global _VALIDATION_ACTIVE
    _VALIDATION_ACTIVE = False
    _cancel_timer("validation")
    _log("[FOLLOW_VERIFY_STOP] token=%s reason=%s" % (_TOKEN, reason))


def _schedule_order_lookup():
    global _ORDER_LOOKUP_TIMER
    if (_C is None or _ORDER_ID or _ORDER_TERMINAL or
            _ORDER_LOOKUP_TIMER is not None or
            _ORDER_LOOKUP_ATTEMPTS >= ORDER_LOOKUP_MAX_ATTEMPTS):
        return
    _ORDER_LOOKUP_TIMER = _schedule(
        ORDER_LOOKUP_INTERVAL_SECONDS, lambda: _resolve_order_id(_TOKEN),
        "qmt_l2_probe_order_lookup")


def _order_fields(obj):
    code = str(_field(obj, "m_strInstrumentID", "stockCode", "stock_code") or "").strip().upper()
    exchange = str(_field(obj, "m_strExchangeID", "exchangeID", "exchange_id") or "").strip().upper()
    if code and "." not in code and exchange:
        exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange, exchange)
        code = "%s.%s" % (code, exchange)
    return {
        "id": str(_field(obj, "m_strOrderSysID", "order_sysid", "m_strOrderID",
                         "m_nOrderID", "orderID", "order_id", "orderId") or ""),
        "code": code,
        "remark": str(_field(obj, "m_strRemark", "m_strOrderRemark", "m_strUserOrderID",
                              "m_strUserOrderId", "userOrderId", "user_order_id",
                              "order_remark", "remark") or ""),
        "strategy": str(_field(obj, "m_strStrategyName", "strategyName",
                                "strategy_name", "strategy") or ""),
        "status": _field(obj, "m_nOrderStatus", "m_strOrderStatus", "order_status", "status"),
        "total": _number(_field(obj, "m_nVolumeTotalOriginal", "m_nVolumeTotal",
                                 "orderVolume", "order_volume", "totalVolume")) or 0.0,
        "traded": _number(_field(obj, "m_nVolumeTraded", "tradedVolume",
                                  "traded_volume", "volumeTraded")) or 0.0,
        "deal_id": str(_field(obj, "m_strTradeID", "m_strDealID", "tradeID",
                               "dealID", "tradedID", "traded_id") or ""),
        "deal_volume": _number(_field(obj, "m_nVolume", "m_nTradeVolume", "deal_volume",
                                       "traded_volume", "volume")) or 0.0,
        "price": _number(_field(obj, "m_dPrice", "price", "order_price")) or 0.0,
    }


def _matches_order(item):
    if item.get("code") and _normalize_code(item["code"]) != TARGET_CODE:
        return False
    return (item.get("remark") == _TOKEN or
            bool(_TOKEN and _TOKEN in str(item.get("strategy") or "")))


def _bind_order_id(order_id):
    global _ORDER_ID
    value = str(order_id or "")
    if not value or value in ("0", "-1"):
        return False
    if _ORDER_ID and _ORDER_ID != value:
        _log("[ORDER_ID_CONFLICT] existing=%s incoming=%s; keep existing" % (_ORDER_ID, value))
        return False
    _ORDER_ID = value
    _STATE["order_id"] = value
    if _VALIDATION_ACTIVE or _VALIDATION_RESULT:
        _log("[ORDER_ID_BOUND] code=%s token=%s order_id=%s" % (TARGET_CODE, _TOKEN, value))
    _save_state()
    return True


def _query_orders():
    global _ORDER_LOOKUP_ERROR_LOGGED
    getter = globals().get("get_trade_detail_data")
    if not callable(getter):
        if not _ORDER_LOOKUP_ERROR_LOGGED:
            _log("[ORDER_QUERY_UNAVAILABLE] api_missing")
            _ORDER_LOOKUP_ERROR_LOGGED = True
        return None
    try:
        data = getter(ACCOUNT_ID, ACCOUNT_TYPE, "order")
    except Exception as exc:
        if not _ORDER_LOOKUP_ERROR_LOGGED:
            _log("[ORDER_QUERY_FAILED] error=%r" % (exc,))
            _ORDER_LOOKUP_ERROR_LOGGED = True
        return None
    if data is None:
        return None
    _ORDER_LOOKUP_ERROR_LOGGED = False
    return list(data) if isinstance(data, (list, tuple)) else [data]


def _resolve_order_id(token):
    global _ORDER_LOOKUP_TIMER, _ORDER_LOOKUP_ATTEMPTS, _ORDER_LOOKUP_ERROR_LOGGED
    if token != _TOKEN or _ORDER_ID or _ORDER_TERMINAL:
        return
    _ORDER_LOOKUP_TIMER = None
    _ORDER_LOOKUP_ATTEMPTS += 1
    records = _query_orders()
    matches = {}
    if records is not None:
        for record in records:
            item = _order_fields(record)
            if item["id"] and _matches_order(item):
                matches[item["id"]] = item
    if len(matches) == 1:
        order_id, item = next(iter(matches.items()))
        if _bind_order_id(order_id):
            _ORDER_LOOKUP_ERROR_LOGGED = False
            _apply_order_update(item)
            if _CANCEL_DUE and not _ORDER_TERMINAL:
                _request_cancel(_CANCEL_DUE_REASON or "validation_failed")
            return
    elif len(matches) > 1:
        _log("[ORDER_ID_AMBIGUOUS] token=%s ids=%s attempt=%d" % (
            token, ",".join(sorted(matches)), _ORDER_LOOKUP_ATTEMPTS))
    if _ORDER_LOOKUP_ATTEMPTS < ORDER_LOOKUP_MAX_ATTEMPTS:
        _schedule_order_lookup()
    else:
        _log("[ORDER_ID_UNRESOLVED] token=%s attempts=%d cancel_due=%s" % (
            token, _ORDER_LOOKUP_ATTEMPTS, _CANCEL_DUE))


def _schedule_validation_after_abort(reason):
    _STATE["phase"] = reason
    _save_state()
    _unsubscribe_l2()


def _cancel_order(reason):
    global _CANCEL_DUE, _CANCEL_DUE_REASON, _CANCEL_REQUESTED, _CANCEL_FAILED
    if _ORDER_TERMINAL or _CANCEL_REQUESTED:
        return
    _CANCEL_DUE = True
    _CANCEL_DUE_REASON = reason
    if not _ORDER_ID:
        _log("[CANCEL_WAIT_ORDER_ID] token=%s reason=%s" % (_TOKEN, reason))
        _schedule_order_lookup()
        _set_phase("cancel_wait_order_id")
        return
    _CANCEL_DUE = False
    _CANCEL_REQUESTED = True
    _set_phase("cancel_requested")
    try:
        result = cancel(_ORDER_ID, ACCOUNT_ID, ACCOUNT_TYPE, _C)
        if result is False or str(result) == "-1":
            _CANCEL_FAILED = True
            _log("[CANCEL_API_REJECTED] order_id=%s reason=%s return=%r" % (
                _ORDER_ID, reason, result))
            _set_phase("cancel_api_rejected")
        else:
            _log("[CANCEL_REQUESTED] code=%s order_id=%s reason=%s return=%r" % (
                TARGET_CODE, _ORDER_ID, reason, result))
    except Exception as exc:
        _CANCEL_FAILED = True
        _log("[CANCEL_FAILED] code=%s order_id=%s reason=%s error=%r" % (
            TARGET_CODE, _ORDER_ID, reason, exc))
        _set_phase("cancel_failed")


def _finish_validation(result, reason):
    global _VALIDATION_RESULT
    _stop_validation("result_%s" % result.lower())
    _VALIDATION_RESULT = result
    _STATE["validation_result"] = result
    _STATE["verify_count"] = _VERIFY_COUNT
    _STATE["verify_large_count"] = _VERIFY_LARGE_COUNT
    _STATE["stale_count"] = _STALE_COUNT
    _save_state()
    _log("[PROBE_VALIDATION_%s] code=%s order_id=%s events=%d/%d "
         "large=%d/%d elapsed_ms=%.0f reason=%s" % (
             result, TARGET_CODE, _ORDER_ID or "pending", _VERIFY_COUNT,
             VERIFY_MAX_EVENTS, _VERIFY_LARGE_COUNT, VERIFY_REQUIRED_LARGE_COUNT,
             (time.monotonic() - _VALIDATION_STARTED) * 1000.0, reason))
    cancel_reason = reason if result == "FAIL" else "probe_cleanup_after_pass"
    _cancel_order(cancel_reason)


def _check_validation(force_timeout=False):
    if not _VALIDATION_ACTIVE:
        return
    elapsed = time.monotonic() - (_VALIDATION_STARTED or time.monotonic())
    if _VERIFY_LARGE_COUNT >= VERIFY_REQUIRED_LARGE_COUNT:
        _finish_validation("PASS", "large_followers_found")
        return
    if _VERIFY_COUNT >= VERIFY_MAX_EVENTS:
        _finish_validation("FAIL", "event_limit")
        return
    if force_timeout or elapsed >= VERIFY_WINDOW_SECONDS:
        _finish_validation("FAIL", "timeout")


def _validation_timer(token):
    global _VALIDATION_TIMER
    if token != _TOKEN or not _VALIDATION_ACTIVE:
        return
    _VALIDATION_TIMER = None
    _check_validation(force_timeout=True)


def _l2order_callback(payload):
    global _VERIFY_COUNT, _VERIFY_LARGE_COUNT, _STALE_COUNT
    if not _VALIDATION_ACTIVE or _ORDER_TERMINAL:
        return
    data = _payload(payload)
    data_code = _field(data, "stockCode", "stock_code", "code", "instrumentID")
    if data_code and _normalize_code(data_code) != TARGET_CODE:
        return
    event_raw = _field(data, "time", "stime", "timestamp")
    event_seconds = _event_seconds(event_raw)
    if event_seconds is None or (_VALIDATION_CUTOFF is not None and event_seconds <= _VALIDATION_CUTOFF):
        _STALE_COUNT += 1
        if _STALE_COUNT == 1:
            _log("[FOLLOW_VERIFY_STALE_EVENT] event=%s cutoff=%s" % (event_raw, _VALIDATION_CUTOFF))
        _check_validation()
        return
    if not _continuous_session(_field(data, "stime", "time", "timestamp")):
        return
    price = _number(_field(data, "price"))
    volume = _number(_field(data, "volume"))
    direction = str(_field(data, "entrustDirection", "direction", "side") or "").upper()
    order_id = str(_field(data, "entrustNo", "orderNo", "order_id") or "")
    if not price or not volume or not _LIMIT_UP or abs(price - _LIMIT_UP) >= 1e-8:
        return
    if direction not in ("B", "BUY", "1", u"\u4e70", u"\u4e70\u5165"):
        return
    if order_id and order_id == _ORDER_ID:
        _log("[FOLLOW_VERIFY_SELF_ORDER_EXCLUDED] order_id=%s" % order_id)
        return
    event_key = order_id or "event:%s:%s:%s:%s" % (event_raw, price, volume, direction)
    if event_key in _SEEN_ORDER_IDS:
        return
    _SEEN_ORDER_IDS.add(event_key)
    amount = _number(_field(data, "amount")) or price * volume
    _VERIFY_COUNT += 1
    is_large = amount >= VERIFY_LARGE_AMOUNT
    if is_large:
        _VERIFY_LARGE_IDS.add(event_key)
        _VERIFY_LARGE_COUNT = len(_VERIFY_LARGE_IDS)
    delay_ms = _quote_delay_ms(event_raw)
    if _VERIFY_COUNT == 1 or _VERIFY_COUNT % 25 == 0 or is_large:
        _log("[FOLLOW_VERIFY_PROGRESS] events=%d/%d large=%d/%d amount=%.0f "
             "delay_ms=%s event=%s" % (
                 _VERIFY_COUNT, VERIFY_MAX_EVENTS, _VERIFY_LARGE_COUNT,
                 VERIFY_REQUIRED_LARGE_COUNT, amount,
                 "%.0f" % delay_ms if delay_ms is not None else "unknown", event_raw))
    _STATE["verify_count"] = _VERIFY_COUNT
    _STATE["verify_large_count"] = _VERIFY_LARGE_COUNT
    _save_state()
    _check_validation()


def _unsubscribe_l2():
    global _SUB_ID
    if _SUB_ID is not None and _C is not None:
        try:
            result = _C.unsubscribe_quote(_SUB_ID)
            _log("[UNSUBSCRIBED] period=l2order sub_id=%s return=%r" % (_SUB_ID, result))
        except Exception as exc:
            _log("[UNSUBSCRIBE_FAILED] period=l2order sub_id=%s error=%r" % (
                _SUB_ID, exc))
        _SUB_ID = None
    _unsubscribe_preflight_l2quote()


def _finish_order(phase):
    global _ORDER_TERMINAL, _VALIDATION_ACTIVE, _CANCEL_CONFIRMED
    _ORDER_TERMINAL = True
    _VALIDATION_ACTIVE = False
    if phase == "cancel_confirmed":
        _CANCEL_CONFIRMED = True
    _cancel_timer("validation")
    _cancel_timer("lookup")
    _set_phase(phase)
    _log("[ORDER_TERMINAL] code=%s order_id=%s phase=%s filled=%s/%s "
         "cancel_confirmed=%s cancel_failed=%s" % (
             TARGET_CODE, _ORDER_ID or "unknown", phase, _ORDER_FILLED,
             _ORDER_QTY, _CANCEL_CONFIRMED, _CANCEL_FAILED))
    _unsubscribe_l2()


def _apply_order_update(item):
    global _ORDER_STATUS, _ORDER_FILLED
    _ORDER_STATUS = str(item.get("status") or "")
    _ORDER_FILLED = max(_ORDER_FILLED, item.get("traded", 0.0))
    status = _ORDER_STATUS.lower()
    if status == "56":
        _ORDER_FILLED = max(_ORDER_FILLED, _ORDER_QTY)
    if (_ORDER_QTY > 0 and _ORDER_FILLED >= _ORDER_QTY) or status == "56":
        _finish_order("filled")
    elif status in ("5", "6", "53", "54", "canceled", "cancelled", "\u64a4\u5355"):
        _finish_order("cancel_confirmed")
    elif status in ("57", "rejected", "\u5e9f\u5355"):
        _finish_order("rejected")
    elif _CANCEL_DUE:
        _cancel_order(_CANCEL_DUE_REASON or "validation_failed")
    _STATE["order_status"] = _ORDER_STATUS
    _STATE["filled"] = _ORDER_FILLED
    _save_state()


def order_callback(ContextInfo, orderInfo):
    item = _order_fields(orderInfo)
    if item["id"] and item["id"] == _ORDER_ID:
        _apply_order_update(item)
        _log("[ORDER_CALLBACK] id=%s status=%s total=%s traded=%s" % (
            item["id"], item["status"], item["total"], item["traded"]))
        return
    if _matches_order(item) and item["id"] and _bind_order_id(item["id"]):
        _apply_order_update(item)
        _log("[ORDER_CALLBACK_ASSOCIATED] id=%s token=%s status=%s total=%s traded=%s" % (
            item["id"], _TOKEN, item["status"], item["total"], item["traded"]))
        if _CANCEL_DUE and not _ORDER_TERMINAL:
            _cancel_order(_CANCEL_DUE_REASON or "validation_failed")
        return
    _log("[ORDER_CALLBACK_UNMATCHED] id=%s code=%s remark=%s status=%s" % (
        item["id"], item["code"], item["remark"], item["status"]))


def deal_callback(ContextInfo, dealInfo):
    global _ORDER_FILLED
    item = _order_fields(dealInfo)
    if item["deal_id"] and item["deal_id"] in _SEEN_DEAL_IDS:
        return
    if not item["id"] or item["id"] != _ORDER_ID:
        _log("[DEAL_CALLBACK_UNMATCHED] id=%s code=%s deal_id=%s volume=%s" % (
            item["id"], item["code"], item["deal_id"], item["deal_volume"]))
        return
    if item["deal_id"]:
        _SEEN_DEAL_IDS.add(item["deal_id"])
    _ORDER_FILLED += item["deal_volume"]
    _log("[DEAL_CALLBACK] id=%s deal_id=%s volume=%s cum_volume=%s price=%s" % (
        item["id"], item["deal_id"], item["deal_volume"], _ORDER_FILLED, item["price"]))
    if _ORDER_QTY > 0 and _ORDER_FILLED >= _ORDER_QTY:
        _finish_order("filled")
    else:
        _STATE["filled"] = _ORDER_FILLED
        _save_state()


def _submit_probe():
    global _TOKEN, _ATTEMPTED, _STATE, _CANCEL_DUE, _CANCEL_DUE_REASON
    global _ORDER_TERMINAL
    _TOKEN = "%s_%s_%s" % (
        TARGET_CODE.replace(".", ""), _dt.datetime.now().strftime("%Y%m%d%H%M%S"),
        int(time.time() * 1000) % 1000000)
    _STATE = {
        "trade_day": _dt.datetime.now().strftime("%Y%m%d"),
        "attempted": True,
        "token": _TOKEN,
        "code": TARGET_CODE,
        "quantity": PROBE_QTY,
        "limit_up": _LIMIT_UP,
        "phase": "reserved",
        "validation_result": "",
        "verify_count": 0,
        "verify_large_count": 0,
        "order_id": "",
        "filled": 0,
    }
    if not _save_state():
        _log("[ABORT] cannot reserve one-shot state; no order sent")
        _unsubscribe_l2()
        return
    _ATTEMPTED = True
    if not _start_validation():
        _schedule_validation_after_abort("validation_timer_failed_no_order")
        return
    price = _LIMIT_UP
    _log("[PROBE_ARMED] LIVE=%s account=%s code=%s price=%.3f qty=%d "
         "notional=%.2f token=%s" % (
             ENABLE_LIVE_ORDERS, ACCOUNT_ID, TARGET_CODE, price, PROBE_QTY,
             price * PROBE_QTY, _TOKEN))
    if not ENABLE_LIVE_ORDERS:
        _stop_validation("live_orders_disabled")
        _schedule_validation_after_abort("live_orders_disabled_no_order")
        return
    started = time.monotonic()
    try:
        result = passorder(
            PASSORDER_OPERATION, ORDER_TYPE_BUY, ACCOUNT_ID, TARGET_CODE,
            PRICE_TYPE_LIMIT, price, PROBE_QTY, STRATEGY_NAME, QUICK_TRADE, _TOKEN, _C)
    except Exception as exc:
        _log("[SUBMIT_EXCEPTION] token=%s error=%r; submission outcome unknown" % (_TOKEN, exc))
        _CANCEL_DUE = True
        _CANCEL_DUE_REASON = "submit_exception_cleanup"
        _schedule_order_lookup()
        _set_phase("submit_outcome_unknown")
        return
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if result is False or str(result) == "-1":
        _stop_validation("submit_rejected")
        _ORDER_TERMINAL = True
        _CANCEL_DUE = False
        _set_phase("submit_rejected")
        _log("[SUBMIT_REJECTED] code=%s return=%r passorder_ms=%.1f" % (
            TARGET_CODE, result, elapsed_ms))
        return
    if isinstance(result, (str, int)) and not isinstance(result, bool) and str(result).strip() not in ("", "0", "-1"):
        _bind_order_id(result)
    if not _ORDER_ID:
        _schedule_order_lookup()
    _set_phase("submitted")
    _log("[SUBMIT_RETURN] code=%s result=%r passorder_ms=%.1f order_id=%s "
         "validation_started_before_call=true" % (
             TARGET_CODE, result, elapsed_ms, _ORDER_ID or "pending"))


def init(C):
    global _C, _ORDER_QTY, _PREFLIGHT_ACTIVE, _PREFLIGHT_TIMER
    _C = C
    _ORDER_QTY = PROBE_QTY
    try:
        _open_log()
    except Exception as exc:
        _log("[LOG_OPEN_FAILED] error=%r; console logging remains active" % (exc,))
    _log("[INIT] live=%s account=%s code=%s qty=%d cwd=%s" % (
        ENABLE_LIVE_ORDERS, ACCOUNT_ID, TARGET_CODE, PROBE_QTY, os.getcwd()))
    if _read_existing_state():
        return
    if not ENABLE_LIVE_ORDERS:
        _log("[ABORT] ENABLE_LIVE_ORDERS is false")
        return
    try:
        C.set_account(ACCOUNT_ID, ACCOUNT_TYPE)
    except Exception as exc:
        _log("[ABORT] set_account failed error=%r" % (exc,))
        return
    if not _snapshot(C):
        return
    if not _subscribe_l2():
        _log("[ABORT] L2 subscription is unavailable; no order sent")
        return
    if _SNAPSHOT_NEEDS_REFRESH:
        _PREFLIGHT_ACTIVE = True
        _PREFLIGHT_TIMER = _schedule(
            PREFLIGHT_WINDOW_SECONDS, _preflight_timeout,
            "qmt_l2_probe_preflight")
        if _PREFLIGHT_TIMER is None:
            _abort_preflight("preflight timer unavailable")
            return
        if not _subscribe_preflight_l2quote():
            _abort_preflight("fresh l2quote subscription unavailable")
            return
        _log("[PREFLIGHT_WAIT_L2QUOTE] window_seconds=%.1f max_quote_age_ms=%.0f" % (
            PREFLIGHT_WINDOW_SECONDS, MAX_QUOTE_AGE_MS))
        return
    _submit_probe()


def handlebar(C):
    try:
        if C.is_last_bar():
            _check_validation()
    except Exception as exc:
        _log("[HANDLEBAR_FAILED] error=%r" % (exc,))


def stop(C):
    if _ATTEMPTED and not _ORDER_TERMINAL:
        _cancel_order("manual_stop_cleanup")
        if not _ORDER_ID:
            _log("[STOP_WARNING] order id is still unknown; cancellation cannot be verified")
    _cancel_timer("validation")
    _cancel_timer("lookup")
    _cancel_timer("preflight")
    _unsubscribe_l2()
    _log("[STOP] phase=%s order_id=%s cancel_confirmed=%s filled=%s/%s" % (
        _STATE.get("phase", "not_submitted"), _ORDER_ID or "unknown",
        _CANCEL_CONFIRMED, _ORDER_FILLED, _ORDER_QTY))
    if _LOG is not None:
        try:
            _LOG.close()
        except Exception:
            pass
