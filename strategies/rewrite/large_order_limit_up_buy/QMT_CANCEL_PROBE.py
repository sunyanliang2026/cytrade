# coding: gbk
"""One-shot QMT native probe for a small manual order and cancellation.

This entry point deliberately uses console logging only.  It does not read or
write files and does not use an external client.  Set the manual values below,
then set ENABLE_LIVE_ORDERS to True immediately before the controlled test.
"""

import datetime as _dt
import time


# Edit these values before a controlled test.  The probe is disarmed by default.
ENABLE_LIVE_ORDERS = True
ACCOUNT_ID = "32011276"
ACCOUNT_TYPE = "stock"
TARGET_CODE = "603081.SH"
ORDER_PRICE = 10.17
ORDER_QTY = 100

STRATEGY_NAME = "qmt_cancel_probe"
PASSORDER_OPERATION = 23
ORDER_TYPE_BUY = 1101
PRICE_TYPE_LIMIT = 11
QUICK_TRADE = 2

SUBMIT_DELAY_SECONDS = 5.0
CANCEL_DELAY_SECONDS = 1.0
CANCEL_RETRY_INTERVAL_SECONDS = 0.2
CANCEL_CONFIRM_TIMEOUT_SECONDS = 2.0
CANCEL_MAX_ATTEMPTS = 10
ORDER_LOOKUP_INTERVAL_SECONDS = 0.1
ORDER_LOOKUP_MAX_ATTEMPTS = 20
STATUS_POLL_INTERVAL_SECONDS = 0.5


_C = None
_SUBMIT_TIMER = None
_CANCEL_TIMER = None
_LOOKUP_TIMER = None
_STATUS_TIMER = None
_SUBMITTED = False
_SUBMIT_TOKEN = ""
_ORDER_ID = ""
_ORDER_STATUS = ""
_ORDER_FILLED = 0.0
_ORDER_TOTAL = 0.0
_ORDER_TERMINAL = False
_CANCEL_DUE = False
_CANCEL_REASON = ""
_CANCEL_REQUESTED = False
_CANCEL_CONFIRMED = False
_CANCEL_FAILED = False
_CANCEL_ATTEMPTS = 0
_CANCEL_CONFIRM_DEADLINE = None
_LOOKUP_ATTEMPTS = 0
_QUERY_UNAVAILABLE_LOGGED = False
_PHASE = "CREATED"
_SEEN_DEAL_IDS = set()


def _log(tag, message):
    line = "%s [CANCEL_PROBE] [%s] %s" % (
        _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        tag, message)
    try:
        print(line)
    except Exception:
        pass


def _field(source, *names):
    if isinstance(source, dict):
        for name in names:
            value = source.get(name)
            if value is not None and value != "":
                return value
        return None
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
        if number == number:
            return number
    except Exception:
        pass
    return None


def _normalize_code(value, exchange=""):
    text = str(value or "").strip().upper()
    if "." in text:
        digits, suffix = text.split(".", 1)
        digits = "".join(ch for ch in digits if ch.isdigit())
        return digits + "." + suffix if len(digits) == 6 else text
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return text
    suffix = str(exchange or "").strip().upper()
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(suffix, suffix)
    if suffix not in ("SH", "SZ", "BJ"):
        suffix = "SH" if digits.startswith(("5", "6", "9")) else "SZ"
    return digits + "." + suffix


def _context_detail(context):
    if context is None:
        return "context=None"
    try:
        request_id = getattr(context, "request_id", None)
    except Exception as exc:
        request_id = "<error:%s>" % type(exc).__name__
    return "context_type=%s context_id=%s request_id=%r" % (
        type(context).__name__, id(context), request_id)


def _set_phase(phase, reason=""):
    global _PHASE
    if _PHASE == phase and not reason:
        return
    old = _PHASE
    _PHASE = phase
    _log("STATE", "%s -> %s reason=%s" % (old, phase, reason or "-"))


def _reset_runtime(C):
    global _C, _SUBMIT_TIMER, _CANCEL_TIMER, _LOOKUP_TIMER, _STATUS_TIMER
    global _SUBMITTED, _SUBMIT_TOKEN, _ORDER_ID, _ORDER_STATUS
    global _ORDER_FILLED, _ORDER_TOTAL, _ORDER_TERMINAL, _CANCEL_DUE
    global _CANCEL_REASON, _CANCEL_REQUESTED, _CANCEL_CONFIRMED
    global _CANCEL_FAILED, _CANCEL_ATTEMPTS, _CANCEL_CONFIRM_DEADLINE
    global _LOOKUP_ATTEMPTS, _QUERY_UNAVAILABLE_LOGGED, _PHASE
    _C = C
    _SUBMIT_TIMER = None
    _CANCEL_TIMER = None
    _LOOKUP_TIMER = None
    _STATUS_TIMER = None
    _SUBMITTED = False
    _SUBMIT_TOKEN = ""
    _ORDER_ID = ""
    _ORDER_STATUS = ""
    _ORDER_FILLED = 0.0
    _ORDER_TOTAL = 0.0
    _ORDER_TERMINAL = False
    _CANCEL_DUE = False
    _CANCEL_REASON = ""
    _CANCEL_REQUESTED = False
    _CANCEL_CONFIRMED = False
    _CANCEL_FAILED = False
    _CANCEL_ATTEMPTS = 0
    _CANCEL_CONFIRM_DEADLINE = None
    _LOOKUP_ATTEMPTS = 0
    _QUERY_UNAVAILABLE_LOGGED = False
    _PHASE = "CREATED"
    _SEEN_DEAL_IDS.clear()


def _cancel_timer(name):
    global _SUBMIT_TIMER, _CANCEL_TIMER, _LOOKUP_TIMER, _STATUS_TIMER
    timer = {
        "submit": _SUBMIT_TIMER,
        "cancel": _CANCEL_TIMER,
        "lookup": _LOOKUP_TIMER,
        "status": _STATUS_TIMER,
    }.get(name)
    if timer is not None and _C is not None:
        try:
            _C.cancel_schedule_run(timer)
        except Exception as exc:
            _log("TIMER_CANCEL_ERROR", "name=%s error=%r" % (name, exc))
    if name == "submit":
        _SUBMIT_TIMER = None
    elif name == "cancel":
        _CANCEL_TIMER = None
    elif name == "lookup":
        _LOOKUP_TIMER = None
    elif name == "status":
        _STATUS_TIMER = None


def _schedule(delay, callback, name):
    if _C is None:
        _log("SCHEDULE_ERROR", "name=%s reason=context_missing" % name)
        return None
    try:
        when = _dt.datetime.now() + _dt.timedelta(
            seconds=max(0.05, float(delay)))
        return _C.schedule_run(
            callback, when, repeat_times=0, name=name)
    except Exception as exc:
        _log("SCHEDULE_ERROR", "name=%s error=%r" % (name, exc))
        return None


def _valid_order_status(status):
    return str(status or "").strip().lower()


def _is_filled(status):
    return _valid_order_status(status) == "56" or (
        _ORDER_TOTAL > 0 and _ORDER_FILLED >= _ORDER_TOTAL)


def _is_cancelled(status):
    return _valid_order_status(status) in (
        "53", "54", "canceled", "cancelled")


def _is_rejected(status):
    return _valid_order_status(status) in ("57", "rejected")


def _order_fields(obj):
    code = _field(obj, "m_strInstrumentID", "stockCode", "stock_code", "code")
    exchange = _field(obj, "m_strExchangeID", "exchangeID", "exchange_id")
    return {
        "id": str(_field(
            obj, "m_strOrderSysID", "order_sysid", "m_strOrderID",
            "m_nOrderID", "orderID", "order_id", "orderId") or ""),
        "code": _normalize_code(code, exchange),
        "remark": str(_field(
            obj, "m_strRemark", "m_strOrderRemark", "m_strUserOrderID",
            "m_strUserOrderId", "userOrderId", "user_order_id",
            "order_remark", "remark") or ""),
        "strategy": str(_field(
            obj, "m_strStrategyName", "strategyName", "strategy_name",
            "strategy") or ""),
        "status": _field(
            obj, "m_nOrderStatus", "m_strOrderStatus", "order_status",
            "status"),
        "total": _number(_field(
            obj, "m_nVolumeTotalOriginal", "m_nVolumeTotal", "orderVolume",
            "order_volume", "totalVolume")) or 0.0,
        "traded": _number(_field(
            obj, "m_nVolumeTraded", "tradedVolume", "traded_volume",
            "volumeTraded")) or 0.0,
        "deal_id": str(_field(
            obj, "m_strTradeID", "m_strDealID", "tradeID", "dealID",
            "tradedID", "traded_id") or ""),
        "deal_volume": _number(_field(
            obj, "m_nVolume", "m_nTradeVolume", "deal_volume",
            "traded_volume", "volume")) or 0.0,
        "price": _number(_field(
            obj, "m_dPrice", "price", "order_price", "traded_price")) or 0.0,
    }


def _matches(item):
    if item.get("code") and item["code"] != TARGET_CODE:
        return False
    token = _SUBMIT_TOKEN
    return bool(token and (
        item.get("remark") == token or token in item.get("strategy", "")))


def _bind_order_id(order_id):
    global _ORDER_ID
    value = str(order_id or "").strip()
    if not value or value in ("0", "-1"):
        return False
    if _ORDER_ID and _ORDER_ID != value:
        _log("ORDER_ID_CONFLICT", "existing=%s incoming=%s; keep existing" % (
            _ORDER_ID, value))
        return False
    if not _ORDER_ID:
        _ORDER_ID = value
        _log("ORDER_ID_BOUND", "order_id=%s token=%s" % (value, _SUBMIT_TOKEN))
    return True


def _query_orders():
    global _QUERY_UNAVAILABLE_LOGGED
    getter = globals().get("get_trade_detail_data")
    if not callable(getter):
        if not _QUERY_UNAVAILABLE_LOGGED:
            _log("ORDER_QUERY_UNAVAILABLE", "api=get_trade_detail_data")
            _QUERY_UNAVAILABLE_LOGGED = True
        return None
    try:
        result = getter(ACCOUNT_ID, ACCOUNT_TYPE, "order")
    except Exception as exc:
        _log("ORDER_QUERY_ERROR", "error=%r" % (exc,))
        return None
    _QUERY_UNAVAILABLE_LOGGED = False
    if result is None:
        return []
    if isinstance(result, (list, tuple)):
        return list(result)
    return [result]


def _find_matching_order():
    records = _query_orders()
    if records is None:
        return None
    matches = []
    for record in records:
        item = _order_fields(record)
        if _ORDER_ID and item.get("id") == _ORDER_ID:
            return item
        if item.get("id") and _matches(item):
            matches.append(item)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _log("ORDER_ID_AMBIGUOUS", "ids=%s" % ",".join(
            sorted(item["id"] for item in matches)))
    return None


def _schedule_lookup():
    global _LOOKUP_TIMER
    if (_ORDER_ID or _ORDER_TERMINAL or _LOOKUP_TIMER is not None or
            _LOOKUP_ATTEMPTS >= ORDER_LOOKUP_MAX_ATTEMPTS):
        return
    timer = _schedule(
        ORDER_LOOKUP_INTERVAL_SECONDS, _on_lookup_timer,
        "qmt_cancel_probe_order_lookup")
    if timer is not None:
        _LOOKUP_TIMER = timer


def _on_lookup_timer(ContextInfo):
    global _LOOKUP_TIMER, _LOOKUP_ATTEMPTS
    _LOOKUP_TIMER = None
    if _ORDER_TERMINAL or _ORDER_ID:
        return
    _LOOKUP_ATTEMPTS += 1
    item = _find_matching_order()
    if item is not None and _bind_order_id(item.get("id")):
        _apply_order_update(item, ContextInfo)
        if _CANCEL_DUE and not _ORDER_TERMINAL and not _CANCEL_REQUESTED:
            _schedule_cancel_attempt(0.0)
        return
    if _LOOKUP_ATTEMPTS < ORDER_LOOKUP_MAX_ATTEMPTS:
        _schedule_lookup()
    else:
        _log("ORDER_ID_UNRESOLVED", "attempts=%d cancel_due=%s" % (
            _LOOKUP_ATTEMPTS, _CANCEL_DUE))


def _schedule_cancel_due(delay):
    global _CANCEL_TIMER
    if _ORDER_TERMINAL or _CANCEL_REQUESTED or _CANCEL_TIMER is not None:
        return
    timer = _schedule(
        delay, _on_cancel_due, "qmt_cancel_probe_cancel")
    if timer is not None:
        _CANCEL_TIMER = timer


def _schedule_cancel_attempt(delay):
    _schedule_cancel_due(delay)


def _on_cancel_due(ContextInfo):
    global _CANCEL_TIMER, _CANCEL_DUE
    _CANCEL_TIMER = None
    if _ORDER_TERMINAL or _CANCEL_REQUESTED:
        return
    _CANCEL_DUE = True
    if not _CANCEL_REASON:
        _set_cancel_reason("scheduled_cancel")
    _attempt_cancel(ContextInfo)


def _set_cancel_reason(reason):
    global _CANCEL_REASON
    _CANCEL_REASON = reason or "unspecified"


def _queue_cancel(reason):
    global _CANCEL_DUE
    if _ORDER_TERMINAL or _CANCEL_CONFIRMED:
        return
    _CANCEL_DUE = True
    _set_cancel_reason(reason)
    _set_phase("CANCEL_DUE", _CANCEL_REASON)
    if _ORDER_ID:
        _schedule_cancel_attempt(0.0)
    else:
        _schedule_lookup()
        _log("CANCEL_WAIT_ORDER_ID", "reason=%s" % _CANCEL_REASON)


def _schedule_status_poll(delay):
    global _STATUS_TIMER
    if _ORDER_TERMINAL or _STATUS_TIMER is not None:
        return
    timer = _schedule(
        delay, _on_status_timer, "qmt_cancel_probe_status")
    if timer is not None:
        _STATUS_TIMER = timer


def _on_status_timer(ContextInfo):
    global _STATUS_TIMER, _CANCEL_REQUESTED, _CANCEL_DUE
    _STATUS_TIMER = None
    if _ORDER_TERMINAL:
        return
    item = _find_matching_order()
    if item is not None:
        _apply_order_update(item, ContextInfo)
    if _ORDER_TERMINAL:
        return
    if (_CANCEL_REQUESTED and _CANCEL_CONFIRM_DEADLINE is not None and
            time.monotonic() >= _CANCEL_CONFIRM_DEADLINE):
        _CANCEL_REQUESTED = False
        _CANCEL_DUE = True
        _log("CANCEL_CONFIRM_TIMEOUT", "order_id=%s; retrying" % _ORDER_ID)
        _schedule_cancel_attempt(0.0)
    elif _CANCEL_REQUESTED:
        _schedule_status_poll(STATUS_POLL_INTERVAL_SECONDS)
    elif _CANCEL_DUE:
        _attempt_cancel(ContextInfo)


def _attempt_cancel(ContextInfo):
    global _CANCEL_DUE, _CANCEL_REQUESTED, _CANCEL_FAILED
    global _CANCEL_ATTEMPTS, _CANCEL_CONFIRM_DEADLINE
    if _ORDER_TERMINAL or _CANCEL_CONFIRMED or _CANCEL_REQUESTED:
        return
    if not _ORDER_ID:
        _schedule_lookup()
        return
    if _is_filled(_ORDER_STATUS):
        _finish("FILLED", "already_filled_before_cancel")
        return
    if _is_cancelled(_ORDER_STATUS) or _is_rejected(_ORDER_STATUS):
        _apply_order_update({
            "id": _ORDER_ID, "code": TARGET_CODE, "status": _ORDER_STATUS,
            "total": _ORDER_TOTAL, "traded": _ORDER_FILLED,
        }, ContextInfo)
        return
    if _CANCEL_ATTEMPTS >= CANCEL_MAX_ATTEMPTS:
        _CANCEL_DUE = False
        _set_phase("CANCEL_UNKNOWN", "max_attempts")
        _log("CANCEL_UNKNOWN", "order_id=%s attempts=%d" % (
            _ORDER_ID, _CANCEL_ATTEMPTS))
        return

    _CANCEL_ATTEMPTS += 1

    can_cancel = globals().get("can_cancel_order")
    if callable(can_cancel):
        try:
            can_result = can_cancel(_ORDER_ID, ACCOUNT_ID, ACCOUNT_TYPE)
            _log("CAN_CANCEL", "order_id=%s result=%r" % (
                _ORDER_ID, can_result))
            if not can_result:
                _set_phase("CANCEL_WAITABLE", "can_cancel_false")
                _schedule_cancel_attempt(CANCEL_RETRY_INTERVAL_SECONDS)
                return
        except Exception as exc:
            _log("CAN_CANCEL_ERROR", "order_id=%s error=%r; trying cancel" % (
                _ORDER_ID, exc))
    else:
        _log("CAN_CANCEL_UNAVAILABLE", "trying cancel directly")

    _log("CANCEL_CALL", "order_id=%s attempt=%d reason=%s %s" % (
        _ORDER_ID, _CANCEL_ATTEMPTS, _CANCEL_REASON or "unspecified",
        _context_detail(ContextInfo)))
    try:
        result = cancel(_ORDER_ID, ACCOUNT_ID, ACCOUNT_TYPE, ContextInfo)
    except Exception as exc:
        _CANCEL_FAILED = True
        _CANCEL_DUE = True
        _set_phase("CANCEL_EXCEPTION", type(exc).__name__)
        _log("CANCEL_EXCEPTION", "order_id=%s error=%r" % (_ORDER_ID, exc))
        _schedule_cancel_attempt(CANCEL_RETRY_INTERVAL_SECONDS)
        return
    if result is False or result == 0 or str(result).strip() == "-1":
        _CANCEL_FAILED = True
        _CANCEL_DUE = True
        _set_phase("CANCEL_REJECTED", "return=%r" % (result,))
        _log("CANCEL_REJECTED", "order_id=%s return=%r" % (
            _ORDER_ID, result))
        _schedule_cancel_attempt(CANCEL_RETRY_INTERVAL_SECONDS)
        return
    _CANCEL_DUE = False
    _CANCEL_REQUESTED = True
    _CANCEL_CONFIRM_DEADLINE = (
        time.monotonic() + CANCEL_CONFIRM_TIMEOUT_SECONDS)
    _set_phase("CANCEL_REQUESTED", "return=%r" % (result,))
    _log("CANCEL_REQUESTED", "order_id=%s return=%r %s" % (
        _ORDER_ID, result, _context_detail(ContextInfo)))
    _schedule_status_poll(STATUS_POLL_INTERVAL_SECONDS)


def _finish(phase, reason):
    global _ORDER_TERMINAL, _CANCEL_DUE
    _ORDER_TERMINAL = True
    _CANCEL_DUE = False
    _cancel_timer("submit")
    _cancel_timer("cancel")
    _cancel_timer("lookup")
    _cancel_timer("status")
    _set_phase(phase, reason)
    _log("TERMINAL", "phase=%s order_id=%s status=%s filled=%.0f/%.0f "
         "cancel_confirmed=%s cancel_failed=%s" % (
             phase, _ORDER_ID or "unknown", _ORDER_STATUS,
             _ORDER_FILLED, _ORDER_TOTAL, _CANCEL_CONFIRMED, _CANCEL_FAILED))


def _apply_order_update(item, ContextInfo=None):
    global _ORDER_STATUS, _ORDER_FILLED, _ORDER_TOTAL, _CANCEL_CONFIRMED
    if item.get("id"):
        _bind_order_id(item.get("id"))
    _ORDER_STATUS = str(item.get("status") or "")
    _ORDER_TOTAL = max(_ORDER_TOTAL, item.get("total", 0.0))
    _ORDER_FILLED = max(_ORDER_FILLED, item.get("traded", 0.0))
    _log("ORDER_UPDATE", "id=%s status=%s total=%.0f traded=%.0f" % (
        _ORDER_ID or "unknown", _ORDER_STATUS, _ORDER_TOTAL, _ORDER_FILLED))
    if _is_filled(_ORDER_STATUS):
        _finish("FILLED", "order_callback")
    elif _is_cancelled(_ORDER_STATUS):
        _CANCEL_CONFIRMED = True
        _finish("CANCEL_CONFIRMED", "order_callback")
    elif _is_rejected(_ORDER_STATUS):
        _finish("REJECTED", "order_callback")
    elif _CANCEL_DUE and not _CANCEL_REQUESTED:
        _schedule_cancel_attempt(0.0)


def _submit(ContextInfo):
    global _SUBMIT_TIMER, _SUBMITTED, _SUBMIT_TOKEN, _ORDER_TOTAL
    _SUBMIT_TIMER = None
    if _SUBMITTED or _ORDER_TERMINAL:
        return
    _SUBMITTED = True
    _ORDER_TOTAL = float(ORDER_QTY)
    _SUBMIT_TOKEN = "%s_%s" % (
        TARGET_CODE.replace(".", ""),
        _dt.datetime.now().strftime("%Y%m%d%H%M%S%f"))
    _set_phase("SUBMITTING", "timer")
    _log("SUBMIT_CALL", "code=%s price=%.4f qty=%d token=%s %s" % (
        TARGET_CODE, ORDER_PRICE, ORDER_QTY, _SUBMIT_TOKEN,
        _context_detail(ContextInfo)))
    started = time.monotonic()
    try:
        result = passorder(
            PASSORDER_OPERATION, ORDER_TYPE_BUY, ACCOUNT_ID, TARGET_CODE,
            PRICE_TYPE_LIMIT, ORDER_PRICE, ORDER_QTY, STRATEGY_NAME,
            QUICK_TRADE, _SUBMIT_TOKEN, ContextInfo)
    except Exception as exc:
        _set_phase("SUBMIT_OUTCOME_UNKNOWN", type(exc).__name__)
        _log("SUBMIT_EXCEPTION", "error=%r; keep lookup/cancel guard active" % (
            exc,))
        _schedule_lookup()
        _schedule_cancel_due(CANCEL_DELAY_SECONDS)
        return
    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log("SUBMIT_RETURN", "return=%r passorder_ms=%.1f" % (
        result, elapsed_ms))
    if result is False or str(result).strip() == "-1":
        _finish("SUBMIT_REJECTED", "return=%r" % (result,))
        return
    if (isinstance(result, (str, int)) and not isinstance(result, bool) and
            str(result).strip() not in ("", "0", "-1")):
        _bind_order_id(result)
    _set_phase("SUBMITTED", "passorder_accepted")
    if not _ORDER_ID:
        _schedule_lookup()
    _schedule_cancel_due(CANCEL_DELAY_SECONDS)


def _validate_config():
    global TARGET_CODE, ORDER_PRICE, ORDER_QTY
    TARGET_CODE = _normalize_code(TARGET_CODE)
    ORDER_PRICE = _number(ORDER_PRICE)
    try:
        ORDER_QTY = int(ORDER_QTY)
    except Exception:
        ORDER_QTY = 0
    if len(TARGET_CODE) < 9 or "." not in TARGET_CODE:
        return False, "invalid TARGET_CODE"
    if ORDER_PRICE is None or ORDER_PRICE <= 0:
        return False, "invalid ORDER_PRICE"
    if ORDER_QTY < 100 or ORDER_QTY % 100 != 0:
        return False, "ORDER_QTY must be a positive multiple of 100"
    return True, "ok"


def init(C):
    global _SUBMIT_TIMER
    _reset_runtime(C)
    _log("INIT", "live=%s account=%s code=%s price=%s qty=%s" % (
        ENABLE_LIVE_ORDERS, ACCOUNT_ID, TARGET_CODE, ORDER_PRICE, ORDER_QTY))
    ok, reason = _validate_config()
    if not ok:
        _set_phase("CONFIG_ERROR", reason)
        _log("ABORT", reason)
        return
    if not ENABLE_LIVE_ORDERS:
        _set_phase("DISARMED", "set ENABLE_LIVE_ORDERS=True for test")
        _log("ABORT", "no order sent; edit manual values and explicitly arm")
        return
    try:
        C.set_account(ACCOUNT_ID, ACCOUNT_TYPE)
    except Exception as exc:
        _set_phase("ACCOUNT_ERROR", type(exc).__name__)
        _log("ABORT", "set_account error=%r" % (exc,))
        return
    _submit_timer = _schedule(
        SUBMIT_DELAY_SECONDS, _submit, "qmt_cancel_probe_submit")
    if _submit_timer is None:
        _set_phase("SCHEDULE_ERROR", "submit_timer")
        _log("ABORT", "submit timer unavailable; no order sent")
        return
    _SUBMIT_TIMER = _submit_timer
    _set_phase("ARMED", "submit_in=%.1fs" % SUBMIT_DELAY_SECONDS)
    _log("ARMED", "one order only; cancel_after=%.1fs" % (
        CANCEL_DELAY_SECONDS,))


def handlebar(C):
    if (_CANCEL_DUE and _ORDER_ID and not _ORDER_TERMINAL and
            not _CANCEL_REQUESTED and _CANCEL_TIMER is None):
        _log("HANDLEBAR_CANCEL_FALLBACK", "using current handlebar context")
        _attempt_cancel(C)


def order_callback(ContextInfo, orderInfo):
    item = _order_fields(orderInfo)
    if _ORDER_ID and item.get("id") == _ORDER_ID:
        _apply_order_update(item, ContextInfo)
        return
    if item.get("id") and _matches(item) and _bind_order_id(item["id"]):
        _apply_order_update(item, ContextInfo)
        return
    _log("ORDER_CALLBACK_UNMATCHED", "id=%s code=%s remark=%s status=%s" % (
        item.get("id", ""), item.get("code", ""), item.get("remark", ""),
        item.get("status", "")))


def deal_callback(ContextInfo, dealInfo):
    global _ORDER_FILLED
    item = _order_fields(dealInfo)
    if item.get("deal_id") and item["deal_id"] in _SEEN_DEAL_IDS:
        return
    if _ORDER_ID and item.get("id") != _ORDER_ID:
        _log("DEAL_CALLBACK_UNMATCHED", "id=%s deal_id=%s" % (
            item.get("id", ""), item.get("deal_id", "")))
        return
    if not _ORDER_ID and item.get("id") and _matches(item):
        _bind_order_id(item["id"])
    if item.get("deal_id"):
        _SEEN_DEAL_IDS.add(item["deal_id"])
    _ORDER_FILLED += item.get("deal_volume", 0.0)
    _log("DEAL_CALLBACK", "id=%s deal_id=%s volume=%.0f filled=%.0f/%.0f" % (
        _ORDER_ID or item.get("id", ""), item.get("deal_id", ""),
        item.get("deal_volume", 0.0), _ORDER_FILLED, _ORDER_TOTAL))
    if _is_filled(_ORDER_STATUS):
        _finish("FILLED", "deal_callback")
    elif _ORDER_TOTAL > 0 and _ORDER_FILLED >= _ORDER_TOTAL:
        _finish("FILLED", "deal_callback")


def stop(C):
    if (_ORDER_ID and not _ORDER_TERMINAL and not _CANCEL_REQUESTED and
            not _CANCEL_CONFIRMED):
        _queue_cancel("model_stop")
        _attempt_cancel(C)
    _cancel_timer("submit")
    _cancel_timer("cancel")
    _cancel_timer("lookup")
    _cancel_timer("status")
    _log("STOP", "phase=%s order_id=%s status=%s filled=%.0f/%.0f "
         "cancel_confirmed=%s" % (
             _PHASE, _ORDER_ID or "unknown", _ORDER_STATUS,
             _ORDER_FILLED, _ORDER_TOTAL, _CANCEL_CONFIRMED))


__all__ = ["init", "handlebar", "stop", "order_callback", "deal_callback"]
