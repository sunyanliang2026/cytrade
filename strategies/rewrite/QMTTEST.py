# coding: gbk
"""大QMT内置 Python 大单打板模型。

只使用 QMT 模型上下文、subscribe_quote、行情函数和内置交易函数。
ENABLE_LIVE_ORDERS 默认为 False，先用于盘中数据与状态验证。
"""

import csv
import copy
import datetime as _dt
import json
import os
import time


# ------------------------- operator configuration -------------------------
ENABLE_LIVE_ORDERS = False
ACCOUNT_ID = ""
ACCOUNT_TYPE = "stock"
STRATEGY_NAME = "large_order_limit_up_buy"
ORDER_TYPE_BUY = 1101
PRICE_TYPE_LIMIT = 11

FIRST_SEAL_MIN_AMOUNT = 60000000.0
FIRST_LARGE_ORDER_MIN_AMOUNT = 2000000.0
FIRST_LARGE_ORDER_TOTAL_AMOUNT = 10000000.0
RESEAL_MIN_SEAL_SECONDS = 10.0
RESEAL_MIN_PEAK_AMOUNT = 100000000.0
RESEAL_BREAK_SECONDS = 3.0
RESEAL_VERIFY_ORDER_COUNT = 150
RESEAL_VERIFY_LARGE_AMOUNT = 1500000.0
RESEAL_VERIFY_LARGE_COUNT = 2
RESEAL_VALIDATION_MIN_SECONDS = 1.0
RESEAL_VALIDATION_CONTINUE_ON_FAILURE = True
RESEAL_LATE_WINDOW_SECONDS = 5.0

PERIODS = ("tick", "l2quote", "l2order")
DEBUG_SUMMARY_INTERVAL_SECONDS = 60
DEBUG_LATENCY_WARN_THRESHOLD_MS = 1000.0
DEBUG_LATENCY_WARN_INTERVAL_SECONDS = 10.0
L2_PERIODS = set(PERIODS[1:])
STATE_WAIT_INITIAL = "WAIT_INITIAL_QUOTE"
STATE_READY = "READY"
STATE_WAIT_REOPEN = "WAIT_REOPEN"
STATE_WAIT_RESEAL = "WAIT_RESEAL"
STATE_DONE = "DONE"
STATE_SKIPPED = "SKIPPED_OPENING_GAIN"

class _NoopLock(object):
    """QMT native model callbacks are serialized; never wait in the model thread."""
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


_LOCK = _NoopLock()
_C = None
_INSTANCES = {}
_SUBSCRIPTIONS = []
_SUB_SUCCESS = 0
_SUB_FAILED = 0
_LOG = None
_RAW_LOG = None
_LOG_PATH = ""
_RAW_LOG_PATH = ""
_DAY = ""
_LAST_SUMMARY = 0.0
_COUNTERS = {"orders": 0, "deals": 0, "cancels": 0, "l2": 0, "quotes": 0}
_PERIOD_COUNTS = {}
_FIRST_CALLBACKS = {}
_LAST_EVENT_TIMES = {}
_LATENCY_STATS = {}
_LATENCY_LAST_WARN = {}
_LAST_RECEIVED_AT = {}
_GAP_STATS = {}
_LIMIT_UP_FIRSTS = {}
_RAW_BUFFER = []
_RAW_FLUSH_TIMER = None
_RAW_DROPPED = 0
_AUCTION_RETRY_TIMER = None
_POOL_PATH = ""


def _now():
    return time.monotonic()


def _safe_float(value):
    try:
        value = float(value)
        return value if value == value and value > 0 else None
    except Exception:
        return None


def _field(obj, *names):
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] not in (None, ""):
                return obj[name]
    else:
        for name in names:
            try:
                value = getattr(obj, name)
                if value not in (None, ""):
                    return value
            except Exception:
                pass
    return None


def _stock_code(code):
    code = str(code or "").strip().upper()
    if "." in code:
        return code
    if code.isdigit() and len(code) == 6:
        return code + (".SH" if code.startswith(("5", "6", "9")) else ".SZ")
    return code


def _event_time(value=None):
    """Return HHMMSS from QMT time/stime values; never use it for elapsed time."""
    if value is None:
        return ""
    text = str(value).strip()
    if "." in text:
        integer = text.split(".", 1)[0]
        if integer.isdigit() and len(integer) >= 14:
            return integer[8:14]
        if integer.isdigit() and len(integer) <= 6:
            return integer.zfill(6)
    if text.isdigit():
        if len(text) >= 14 and len(text) <= 18:
            return text[8:14]
        if len(text) == 13:
            try:
                return _dt.datetime.fromtimestamp(int(text) / 1000.0).strftime("%H%M%S")
            except Exception:
                return ""
        if 7 <= len(text) <= 9:
            if text.startswith("0"):
                return text[:6]
            return text.zfill(9)[:6]
        if len(text) >= 8:
            return text[:6]
        if len(text) <= 6:
            return text.zfill(6)
    return text.replace(":", "")[-6:]


def _event_seconds(value=None):
    """Return seconds since midnight, retaining milliseconds when available."""
    if value is None:
        return None
    text = str(value).strip()
    integer, dot, fraction = text.partition(".")
    digits = "".join(ch for ch in integer if ch.isdigit())
    fraction = "".join(ch for ch in fraction if ch.isdigit()) if dot else ""
    if len(digits) == 13:
        try:
            stamp = _dt.datetime.fromtimestamp(int(digits) / 1000.0)
            return (stamp.hour * 3600.0 + stamp.minute * 60.0 + stamp.second +
                    stamp.microsecond / 1000000.0)
        except Exception:
            return None
    if len(digits) >= 14:
        hhmmss = digits[8:14]
        extra = digits[14:] + fraction
    elif len(digits) >= 9:
        hhmmss = digits[:6]
        extra = digits[6:] + fraction
    elif len(digits) >= 6:
        hhmmss = digits[-6:]
        extra = fraction
    else:
        return None
    try:
        hour, minute, second = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
        if hour > 23 or minute > 59 or second > 59:
            return None
        milliseconds = int((extra + "000")[:3]) if extra else 0
        return hour * 3600.0 + minute * 60.0 + second + milliseconds / 1000.0
    except Exception:
        return None


def _event_has_milliseconds(value=None):
    if value is None:
        return False
    text = str(value).strip()
    integer, dot, fraction = text.partition(".")
    digits = "".join(ch for ch in integer if ch.isdigit())
    if dot and any(ch.isdigit() for ch in fraction):
        return True
    if len(digits) == 13 or len(digits) >= 17:
        return True
    return 9 <= len(digits) <= 12


def _latency_ms(event_value, received_at):
    if not _event_has_milliseconds(event_value):
        return None
    event_seconds = _event_seconds(event_value)
    if event_seconds is None:
        return None
    received = _dt.datetime.fromtimestamp(received_at)
    received_seconds = (received.hour * 3600.0 + received.minute * 60.0 +
                        received.second + received.microsecond / 1000000.0)
    delay_ms = (received_seconds - event_seconds) * 1000.0
    # A negative value means the two clocks are not directly comparable. Do not
    # turn clock skew into a false low-latency result or a warning.
    return delay_ms if delay_ms >= 0 else None


def _in_continuous(event_time):
    return "093000" <= event_time < "145700"


def _in_auction(event_time):
    return "091500" <= event_time < "093000"


def _log(message, raw=False):
    line = "%s %s" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], message)
    try:
        print(line)
    except Exception:
        pass
    handle = _RAW_LOG if raw else _LOG
    if handle:
        try:
            handle.write(line + "\n")
            handle.flush()
        except Exception:
            pass


def _json_default(value):
    try:
        return value.__dict__
    except Exception:
        return repr(value)


def _write_raw(code, period, payload):
    global _RAW_DROPPED
    received_at = time.time()
    event_value = _field(payload, "stime", "time")
    latency_value = _field(payload, "time", "stime")
    event_time = _event_time(event_value)
    latency_ms = _latency_ms(latency_value, received_at)
    _COUNTERS["l2" if period in L2_PERIODS else "quotes"] += 1
    _PERIOD_COUNTS[period] = _PERIOD_COUNTS.get(period, 0) + 1
    _LAST_EVENT_TIMES[period] = event_time or ""
    stats = _LATENCY_STATS.setdefault(period, {
        "samples": 0, "valid": 0, "over_threshold": 0,
        "sum_ms": 0.0, "max_ms": None, "last_ms": None,
    })
    stats["samples"] += 1
    gap_key = (period, code)
    previous_received_at = _LAST_RECEIVED_AT.get(gap_key)
    if previous_received_at is not None:
        gap_ms = max(0.0, (received_at - previous_received_at) * 1000.0)
        gap_stats = _GAP_STATS.setdefault(period, {"count": 0, "sum_ms": 0.0, "max_ms": 0.0})
        gap_stats["count"] += 1
        gap_stats["sum_ms"] += gap_ms
        gap_stats["max_ms"] = max(gap_stats["max_ms"], gap_ms)
    _LAST_RECEIVED_AT[gap_key] = received_at
    if latency_ms is not None:
        stats["valid"] += 1
        stats["sum_ms"] += latency_ms
        stats["last_ms"] = latency_ms
        stats["max_ms"] = latency_ms if stats["max_ms"] is None else max(stats["max_ms"], latency_ms)
        if latency_ms > DEBUG_LATENCY_WARN_THRESHOLD_MS:
            stats["over_threshold"] += 1
            now = _now()
            if now - _LATENCY_LAST_WARN.get(period, 0.0) >= DEBUG_LATENCY_WARN_INTERVAL_SECONDS:
                _LATENCY_LAST_WARN[period] = now
                _log("[延时警告] period=%s code=%s delay=%.0fms event_time=%s received_at=%.3f" %
                     (period, code, latency_ms, event_time or "", received_at))
    if period not in _FIRST_CALLBACKS:
        _FIRST_CALLBACKS[period] = {
            "code": code, "event_time": event_time, "received_at": received_at,
            "latency_ms": latency_ms,
        }
        _log("[回调首条] period=%s code=%s event_time=%s received_at=%.3f" %
             (period, code, event_time or "", received_at))
    record = {
        "received_at": received_at, "event_time": event_time,
        "event_time_raw": str(event_value) if event_value is not None else "",
        "latency_time_raw": str(latency_value) if latency_value is not None else "",
        "latency_ms": latency_ms,
        "code": code, "period": period, "data": payload,
    }
    try:
        line = json.dumps(record, ensure_ascii=True, default=_json_default) + "\n"
        if len(_RAW_BUFFER) >= 20000:
            _RAW_BUFFER.pop(0)
            _RAW_DROPPED += 1
        _RAW_BUFFER.append(line)
    except Exception:
        pass
    return received_at


def _observe_first_limit_up(code, period, event_time, event_raw, received_at, limit_up, bid):
    if period not in ("tick", "l2quote") or not _in_continuous(event_time):
        return
    if not limit_up or not bid or abs(bid - limit_up) >= 1e-8:
        return
    observed = _LIMIT_UP_FIRSTS.setdefault(code, {})
    if period in observed:
        return
    observed[period] = {
        "received_at": received_at,
        "event_time": event_time,
        "event_raw": str(event_raw) if event_raw is not None else "",
    }
    _log("[LIMIT_UP_FIRST] code=%s period=%s bid1=%.3f limit_up=%.3f event_time=%s event_raw=%s received_at=%.3f" %
         (code, period, bid, limit_up, event_time, observed[period]["event_raw"], received_at))
    if "tick" not in observed or "l2quote" not in observed or observed.get("compared"):
        return
    tick_at = observed["tick"]["received_at"]
    l2quote_at = observed["l2quote"]["received_at"]
    delta_ms = (tick_at - l2quote_at) * 1000.0
    winner = "tick" if delta_ms < 0 else "l2quote" if delta_ms > 0 else "tie"
    observed["compared"] = True
    observed["winner"] = winner
    observed["gap_ms"] = abs(delta_ms)
    _log("[LIMIT_UP_COMPARE] code=%s winner=%s gap_ms=%.3f tick_event=%s l2quote_event=%s tick_received_at=%.3f l2quote_received_at=%.3f" %
         (code, winner, abs(delta_ms), observed["tick"]["event_raw"],
          observed["l2quote"]["event_raw"], tick_at, l2quote_at))


def _flush_raw(max_records=1000):
    if not _RAW_LOG or not _RAW_BUFFER:
        return
    records = _RAW_BUFFER[:max_records]
    del _RAW_BUFFER[:len(records)]
    try:
        _RAW_LOG.write("".join(records))
        _RAW_LOG.flush()
    except Exception:
        _RAW_BUFFER[0:0] = records


def _raw_flush_timer(ContextInfo):
    _flush_raw(200)


def _schedule_raw_flush():
    global _RAW_FLUSH_TIMER
    if _C is None:
        return
    try:
        _RAW_FLUSH_TIMER = _C.schedule_run(
            _raw_flush_timer, _dt.datetime.now() + _dt.timedelta(seconds=1),
            repeat_times=-1, interval=_dt.timedelta(seconds=1), name="qmt_raw_flush")
    except Exception as exc:
        _log("[日志] 原始数据定时落盘创建失败 %r" % (exc,))


def _auction_retry_timer(ContextInfo):
    global _AUCTION_RETRY_TIMER
    now = _dt.datetime.now().strftime("%H%M%S")
    if now > "092505":
        try:
            _C.cancel_schedule_run("qmt_auction_retry")
        except Exception:
            pass
        _AUCTION_RETRY_TIMER = None
        return
    if now < "092500":
        return
    pending = [inst for inst in _INSTANCES.values() if inst["state"] == STATE_WAIT_INITIAL]
    if pending:
        _initial_quote_batch(_C, pending)


def _schedule_auction_retry():
    global _AUCTION_RETRY_TIMER
    if _C is None:
        return
    try:
        _AUCTION_RETRY_TIMER = _C.schedule_run(
            _auction_retry_timer, _dt.datetime.now() + _dt.timedelta(seconds=1),
            repeat_times=-1, interval=_dt.timedelta(seconds=1), name="qmt_auction_retry")
    except Exception as exc:
        _log("[竞价] 重试定时器创建失败 %r" % (exc,))


def _script_root():
    """Return the script directory when running as a file, else an empty path."""
    script_file = globals().get("__file__")
    if script_file:
        try:
            return os.path.dirname(os.path.abspath(script_file))
        except Exception:
            pass
    return ""


def _paths():
    roots = []
    script_root = _script_root()
    if script_root:
        roots.append(script_root)
    roots.extend([os.getcwd(), os.path.dirname(os.getcwd()), r"C:\光大证券金阳光QMT实盘"])
    seen = set()
    for root in roots:
        if not root or root in seen:
            continue
        seen.add(root)
        yield os.path.join(root, "data", "manual_pool.csv")
        yield os.path.join(root, "manual_pool.csv")


def _load_pool():
    global _POOL_PATH
    candidates = list(_paths())
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if not path:
        _POOL_PATH = ""
        _log("[股票池] 未找到 data/manual_pool.csv，已搜索=%s" % " | ".join(candidates))
        return []
    _POOL_PATH = os.path.abspath(path)
    result = []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                code = _stock_code(row.get("code"))
                amount = _safe_float(row.get("plan_amount"))
                if code and amount and amount > 0:
                    result.append({"code": code, "plan_amount": amount})
                else:
                    _log("[初始化] 忽略无效CSV记录 %r" % row)
    except Exception as exc:
        _log("[初始化] CSV读取失败 %r" % (exc,))
    _log("[股票池] path=%s count=%d codes=%s" %
         (_POOL_PATH, len(result), ",".join(x["code"] for x in result)))
    return result


def _new_instance(code, amount):
    return {
        "code": code, "name": "未命名", "plan_amount": amount, "state": STATE_WAIT_INITIAL,
        "limit_up": None, "last_close": None, "open_price": None, "day_low": None,
        "last_event_time": "", "last_tick": {}, "auction_checked": False,
        "initial_sealed": False, "seal_started": None, "seal_peak": 0.0,
        "break_started": None, "reseal_ready": False, "first_large_total": 0.0,
        "first_large_ids": set(), "seen_order_ids": set(), "verify_count": 0,
        "verify_large_count": 0, "verify_large_ids": set(), "validation_started": None,
        "validation_timer": None, "validation_order_id": None, "cancel_started": None,
        "cancel_confirmed": False, "cancel_failed": False, "late_large_count": 0,
        "late_large_ids": set(), "follow_sent": False, "our_order_id": None,
        "our_order_qty": 0, "our_traded_qty": 0, "our_order_status": "",
        "order_reported_traded_qty": 0, "deal_traded_qty": 0,
        "seen_deal_ids": set(), "pending_deal_infos": [],
        "previous_order_ids": set(), "validation_passed": False,
        "submission_pending": False, "submit_token": None, "submit_reason": None,
        "order_lookup_timer": None, "order_lookup_attempts": 0,
        "pending_order_info": None,
        "sub_ids": [], "last_latency_ms": None, "last_log_reason": "",
    }


def _unwrap(code, data):
    if isinstance(data, dict) and code in data:
        return data.get(code) or {}
    return data if isinstance(data, dict) else {}


def _owned_payload(code, data):
    """Detach callback data from QMT's rollback/reuse object."""
    try:
        return copy.deepcopy(_unwrap(code, data))
    except Exception:
        value = _unwrap(code, data)
        return dict(value) if isinstance(value, dict) else {}


def _limit_from(data, tick=None):
    sources = [data or {}, tick or {}]
    for source in sources:
        value = _field(source, "upLimit", "up_limit", "upperLimit", "limitUp", "涨停价", "UpStopPrice")
        value = _safe_float(value)
        if value:
            return value
    return None


def _quote_values(inst, data, track_day=True):
    tick = data or {}
    raw_bid = _field(tick, "bidPrice", "bid", "bid1", "bidPrice1")
    raw_vol = _field(tick, "bidVol", "bidVolume", "bid1_volume", "bidVolume1")
    bid = _safe_float(raw_bid[0] if isinstance(raw_bid, (list, tuple)) else raw_bid)
    bid_vol = _safe_float(raw_vol[0] if isinstance(raw_vol, (list, tuple)) else raw_vol)
    ask = _safe_float(_field(tick, "askPrice", "ask", "ask1", "askPrice1"))
    last = _safe_float(_field(tick, "lastPrice", "last", "price", "close"))
    low = _safe_float(_field(tick, "low", "lowest", "lowPrice"))
    opening = _safe_float(_field(tick, "open", "openPrice"))
    if opening and not inst["open_price"]:
        inst["open_price"] = opening
    if track_day:
        if low:
            inst["day_low"] = low if inst["day_low"] is None else min(inst["day_low"], low)
        if last:
            inst["day_low"] = last if inst["day_low"] is None else min(inst["day_low"], last)
    return bid, bid_vol, ask, last, low


def _amount(price, volume):
    price = _safe_float(price)
    volume = _safe_float(volume)
    return (price * volume * 100.0) if price and volume else 0.0


def _limit_price(inst, quote=None):
    if inst["limit_up"]:
        return inst["limit_up"]
    value = _limit_from(quote or {}, inst.get("last_tick"))
    if value:
        inst["limit_up"] = value
    return value


def _set_state(inst, state, reason=""):
    if inst["state"] != state:
        inst["state"] = state
        inst["last_log_reason"] = reason
        _log("[状态] %s %s -> %s %s" % (inst["code"], inst.get("name", "未命名"), state, reason))


def _skip_low_gain(inst):
    if inst["state"] in (STATE_DONE, STATE_SKIPPED) or not inst["last_close"]:
        return False
    if inst["day_low"] is not None and inst["day_low"] < inst["last_close"]:
        _cancel_timer(inst)
        inst["cancel_started"] = None
        _set_state(inst, STATE_SKIPPED, "当日最低价低于昨收")
        return True
    return False


def _cancel_timer(inst):
    key = inst.get("validation_timer")
    if key is not None and _C is not None:
        try:
            _C.cancel_schedule_run(key)
        except Exception:
            pass
    inst["validation_timer"] = None


def _cancel_order_lookup(inst):
    key = inst.get("order_lookup_timer")
    if key is not None and _C is not None:
        try:
            _C.cancel_schedule_run(key)
        except Exception:
            pass
    inst["order_lookup_timer"] = None


def _schedule_validation(inst):
    if _C is None or not inst.get("validation_order_id"):
        return
    try:
        when = _dt.datetime.now() + _dt.timedelta(seconds=RESEAL_VALIDATION_MIN_SECONDS)
        code = inst["code"]
        order_id = inst["validation_order_id"]
        inst["validation_timer"] = _C.schedule_run(
            lambda ContextInfo, c=code, oid=order_id: _validation_timer(c, oid),
            when, repeat_times=0, name="reseal_%s" % code)
    except Exception as exc:
        _log("[验证] %s 定时器创建失败 %r，将由handlebar兜底" % (inst["code"], exc))


def _bind_order_id(inst, order_id):
    order_id = str(order_id or "")
    if not order_id or order_id in ("-1", "0") or order_id in inst["previous_order_ids"]:
        return False
    if order_id == str(inst.get("our_order_id") or ""):
        return True
    old_order_id = inst.get("our_order_id")
    if old_order_id:
        inst["previous_order_ids"].add(str(old_order_id))
    inst["our_order_id"] = order_id
    inst["submission_pending"] = False
    _cancel_order_lookup(inst)
    inst["our_order_status"] = "submitted"
    _COUNTERS["orders"] += 1
    _log("[下单] %s %s 委托=%s" % (inst["code"], inst.get("submit_reason", ""), order_id))
    if inst.get("submit_reason") == "回封":
        inst["validation_order_id"] = order_id
        inst["validation_started"] = _now()
        _schedule_validation(inst)
    pending = inst.pop("pending_order_info", None)
    if pending:
        _apply_order_update(inst, pending)
    for deal in inst.pop("pending_deal_infos", []):
        _apply_deal_update(inst, deal)
    return True


def _schedule_order_lookup(inst):
    if _C is None:
        return
    try:
        code = inst["code"]
        token = inst["submit_token"]
        inst["order_lookup_timer"] = _C.schedule_run(
            lambda ContextInfo, c=code, t=token: _resolve_order_id(c, t),
            _dt.datetime.now() + _dt.timedelta(milliseconds=200),
            repeat_times=0, name="qmt_order_lookup_%s" % code)
    except Exception as exc:
        _log("[下单] %s 委托号查询定时器失败 %r" % (inst["code"], exc))


def _resolve_order_id(code, token):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if not inst or inst.get("submit_token") != token or not inst.get("submission_pending"):
            return
        inst["order_lookup_attempts"] += 1
        try:
            order_id = get_last_order_id(ACCOUNT_ID, ACCOUNT_TYPE, "order", STRATEGY_NAME)
        except TypeError:
            order_id = get_last_order_id(ACCOUNT_ID, ACCOUNT_TYPE, "order")
        except Exception as exc:
            _log("[下单] %s 查询委托号失败 %r" % (code, exc))
            order_id = ""
        if _bind_order_id(inst, order_id):
            return
        if inst["order_lookup_attempts"] < 10:
            _schedule_order_lookup(inst)
        else:
            inst["submission_pending"] = False
            _log("[下单失败] %s 在限定次数内未取得委托号" % code)
            _set_state(inst, STATE_DONE, "委托号未知")


def _order_fields(obj):
    code = str(_field(obj, "m_strInstrumentID", "stockCode", "stock_code") or "").strip().upper()
    exchange = str(_field(obj, "m_strExchangeID", "exchangeID", "exchange_id") or "").strip().upper()
    if code and "." not in code and exchange:
        exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange, exchange)
        code = "%s.%s" % (code, exchange)
    return {
        "id": str(_field(obj, "m_strOrderID", "m_nOrderID", "orderID", "order_id") or ""),
        "code": code,
        "deal_id": str(_field(obj, "m_strTradeID", "m_strDealID", "tradeID", "dealID", "tradedID") or ""),
        "user_order_id": str(_field(obj, "m_strRemark", "m_strUserOrderID", "userOrderId", "user_order_id", "remark") or ""),
        "status": _field(obj, "m_nOrderStatus", "m_strOrderStatus", "status"),
        "total": _safe_float(_field(obj, "m_nVolumeTotalOriginal", "m_nVolumeTotal", "orderVolume", "totalVolume")) or 0,
        "traded": _safe_float(_field(obj, "m_nVolumeTraded", "tradedVolume", "volumeTraded")) or 0,
        "deal_volume": _safe_float(_field(obj, "m_nVolume", "m_nTradeVolume", "deal_volume", "m_nVolumeTraded", "tradedVolume", "volume")) or 0,
        "price": _safe_float(_field(obj, "m_dPrice", "price")) or 0,
    }


def _same_stock_code(left, right):
    left = str(left or "").upper()
    right = str(right or "").upper()
    if left == right:
        return True
    return bool(left and right and "." not in left and left == right.split(".", 1)[0])


def _pending_order_matches(inst, item):
    if not inst.get("submission_pending"):
        return False
    if item.get("id") and item["id"] in inst.get("previous_order_ids", set()):
        return False
    token = item.get("user_order_id") or ""
    if token:
        return token == str(inst.get("submit_token") or "")
    if not _same_stock_code(item.get("code"), inst.get("code")):
        return False
    price = item.get("price") or 0
    expected_price = inst.get("limit_up") or 0
    if price and expected_price and abs(price - expected_price) > 0.001:
        return False
    matched = bool(price)
    total = item.get("total") or 0
    expected_total = inst.get("our_order_qty") or 0
    if total and expected_total and total != expected_total:
        return False
    return matched or bool(total)


def _submit(inst, reason):
    is_follow = reason == "撤单后跟随"
    if inst["state"] in (STATE_DONE, STATE_SKIPPED) or inst.get("submission_pending") or (inst.get("our_order_id") and not is_follow):
        return False
    if not ACCOUNT_ID:
        _log("[下单判断] %s 未配置 ACCOUNT_ID，跳过真实下单" % inst["code"])
        _set_state(inst, STATE_DONE, "缺少资金账号")
        return False
    price = inst.get("limit_up")
    qty = int(inst["plan_amount"] / price / 100) * 100 if price else 0
    if qty < 100:
        _log("[下单判断] %s 计划金额不足一手" % inst["code"])
        _set_state(inst, STATE_DONE, "计划金额不足")
        return False
    if not ENABLE_LIVE_ORDERS:
        _log("[下单判断] %s 触发%s，ENABLE_LIVE_ORDERS=False" % (inst["code"], reason))
        _set_state(inst, STATE_DONE, "纸面模式")
        return False
    try:
        inst["submission_pending"] = True
        inst["submit_token"] = "%s_%s" % (inst["code"], int(time.time() * 1000))
        inst["submit_reason"] = reason
        inst["order_lookup_attempts"] = 0
        inst["our_order_qty"] = qty
        inst["our_traded_qty"] = 0
        inst["order_reported_traded_qty"] = 0
        inst["deal_traded_qty"] = 0
        inst["seen_deal_ids"].clear()
        inst["pending_deal_infos"] = []
        order_result = passorder(23, ORDER_TYPE_BUY, ACCOUNT_ID, inst["code"], PRICE_TYPE_LIMIT,
                                 price, qty, STRATEGY_NAME, 1, inst["submit_token"], _C)
        if isinstance(order_result, (str, int)) and not isinstance(order_result, bool) and str(order_result).strip() not in ("", "0", "-1"):
            _bind_order_id(inst, order_result)
        elif inst["submission_pending"]:
            _schedule_order_lookup(inst)
        _log("[下单提交] %s %s 价格=%.3f 数量=%d 返回=%r" % (inst["code"], reason, price, qty, order_result))
        return True
    except Exception as exc:
        inst["submission_pending"] = False
        _log("[下单失败] %s %r" % (inst["code"], exc))
        _set_state(inst, STATE_DONE, "下单异常")
        return False


def _request_cancel(inst):
    if not inst.get("validation_order_id") or inst.get("cancel_started") is not None:
        return
    if _event_time(inst.get("last_event_time")) >= "145700":
        return
    inst["cancel_started"] = _now()
    _COUNTERS["cancels"] += 1
    if not ENABLE_LIVE_ORDERS:
        inst["cancel_confirmed"] = True
        _log("[撤单] %s 纸面模式，模拟完全撤单" % inst["code"])
        return
    try:
        result = cancel(inst["validation_order_id"], ACCOUNT_ID, ACCOUNT_TYPE, _C)
        _log("[撤单] %s 委托=%s 返回=%r" % (inst["code"], inst["validation_order_id"], result))
    except Exception as exc:
        inst["cancel_failed"] = True
        _log("[撤单失败] %s %r" % (inst["code"], exc))
        _set_state(inst, STATE_DONE, "撤单异常")


def _validation_timer(code, order_id):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if not inst or inst.get("validation_order_id") != order_id or inst["state"] in (STATE_DONE, STATE_SKIPPED):
            return
        if inst["verify_count"] >= RESEAL_VERIFY_ORDER_COUNT and inst["verify_large_count"] < RESEAL_VERIFY_LARGE_COUNT:
            _request_cancel(inst)


def _check_validation(inst):
    if not inst.get("validation_order_id") or inst["state"] in (STATE_DONE, STATE_SKIPPED):
        return
    if inst["verify_large_count"] >= RESEAL_VERIFY_LARGE_COUNT:
        _cancel_timer(inst)
        if not inst.get("validation_passed"):
            inst["validation_passed"] = True
            _log("[验证] %s 通过，大单=%d，保留订单" % (inst["code"], inst["verify_large_count"]))
        return
    elapsed = _now() - (inst["validation_started"] or _now())
    if inst["verify_count"] >= RESEAL_VERIFY_ORDER_COUNT and elapsed >= RESEAL_VALIDATION_MIN_SECONDS:
        _request_cancel(inst)
    if inst.get("cancel_started") and not inst["cancel_confirmed"] and _now() - inst["cancel_started"] >= RESEAL_LATE_WINDOW_SECONDS:
        _set_state(inst, STATE_DONE, "撤单状态未知或补充窗口结束")
    if inst.get("cancel_confirmed") and not inst.get("follow_sent") and _now() - inst["cancel_started"] < RESEAL_LATE_WINDOW_SECONDS:
        if inst["late_large_count"] >= RESEAL_VERIFY_LARGE_COUNT and inst["last_tick"].get("bid_at_limit"):
            inst["follow_sent"] = True
            _submit(inst, "撤单后跟随")


def _handle_quote(code, period, payload):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if not inst:
            return
        data = _owned_payload(code, payload)
        is_l2quote = period == "l2quote"
        if is_l2quote:
            inst["last_tick"] = data
        event_time = _event_time(_field(data, "stime", "time", "timestamp"))
        if event_time:
            inst["last_event_time"] = event_time
        received_at = _write_raw(code, period, data)
        limit_up = _limit_price(inst, data)
        is_continuous = bool(event_time and _in_continuous(event_time))
        bid, bid_vol, ask, last, low = _quote_values(inst, data, track_day=is_continuous)
        _observe_first_limit_up(
            code, period, event_time, _field(data, "time", "stime", "timestamp"),
            received_at, limit_up, bid)
        if inst["last_close"] is None:
            inst["last_close"] = _safe_float(_field(data, "lastClose", "preClose", "pre_close"))
        if not is_continuous:
            if is_l2quote:
                _check_validation(inst)
            return
        if _skip_low_gain(inst):
            return
        if not is_l2quote:
            return
        if not limit_up or not bid:
            return
        bid_at_limit = abs(bid - limit_up) < 1e-8
        seal_amount = _amount(bid, bid_vol)
        inst["last_tick"]["bid_at_limit"] = bid_at_limit
        if inst["state"] == STATE_WAIT_INITIAL:
            if bid_at_limit:
                _set_state(inst, STATE_WAIT_REOPEN, "盘中启动时已在涨停价，等待回封")
            else:
                _set_state(inst, STATE_READY, "收到连续竞价有效行情")
        if inst["state"] == STATE_READY:
            if bid_at_limit:
                if inst["seal_started"] is None:
                    inst["seal_started"] = _now()
                inst["seal_peak"] = max(inst["seal_peak"], seal_amount)
                if seal_amount > FIRST_SEAL_MIN_AMOUNT:
                    inst["initial_sealed"] = True
            else:
                inst["seal_started"] = None
                inst["seal_peak"] = 0.0
                inst["initial_sealed"] = False
                inst["first_large_total"] = 0.0
                inst["first_large_ids"].clear()
            return
        if inst["state"] == STATE_WAIT_REOPEN:
            if bid_at_limit:
                if inst["seal_started"] is None:
                    inst["seal_started"] = _now()
                inst["seal_peak"] = max(inst["seal_peak"], seal_amount)
            else:
                if inst["seal_started"] is not None:
                    inst["break_started"] = _now()
                    if _now() - inst["seal_started"] > RESEAL_MIN_SEAL_SECONDS and inst["seal_peak"] >= RESEAL_MIN_PEAK_AMOUNT:
                        inst["reseal_ready"] = True
                        _set_state(inst, STATE_WAIT_RESEAL, "合格封板开板")
                inst["seal_started"] = None
                inst["seal_peak"] = 0.0
            return
        if inst["state"] == STATE_WAIT_RESEAL:
            if not bid_at_limit:
                if inst["break_started"] is None:
                    inst["break_started"] = _now()
                return
            if inst["break_started"] is not None and _now() - inst["break_started"] >= RESEAL_BREAK_SECONDS:
                _submit(inst, "回封")
            return
        _check_validation(inst)


def _handle_l2order(code, payload):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if not inst:
            return
        data = _owned_payload(code, payload)
        _write_raw(code, "l2order", data)
        event_time = _event_time(_field(data, "stime", "time"))
        if event_time:
            inst["last_event_time"] = event_time
        if not _in_continuous(event_time) or inst["state"] in (STATE_DONE, STATE_SKIPPED):
            return
        price = _safe_float(_field(data, "price"))
        volume = _safe_float(_field(data, "volume"))
        direction = str(_field(data, "entrustDirection", "direction", "side") or "").upper()
        order_id = str(_field(data, "entrustNo", "orderNo", "order_id") or "")
        if not price or not volume or not inst.get("limit_up") or abs(price - inst["limit_up"]) >= 1e-8:
            return
        if direction not in ("B", "BUY", "1", "买", "买入"):
            return
        if order_id and order_id in inst["seen_order_ids"]:
            return
        if order_id:
            inst["seen_order_ids"].add(order_id)
        amount = _amount(price, volume)
        if inst["state"] == STATE_READY and inst["initial_sealed"]:
            if amount >= FIRST_LARGE_ORDER_MIN_AMOUNT:
                if order_id not in inst["first_large_ids"]:
                    inst["first_large_ids"].add(order_id)
                    inst["first_large_total"] += amount
            if inst["first_large_total"] > FIRST_LARGE_ORDER_TOTAL_AMOUNT and inst["open_price"] and inst["day_low"] is not None and inst["day_low"] < inst["open_price"] * 0.985:
                _submit(inst, "首封")
        elif inst.get("validation_order_id"):
            if order_id == str(inst["validation_order_id"]):
                return
            inst["verify_count"] += 1
            if amount >= RESEAL_VERIFY_LARGE_AMOUNT and order_id not in inst["verify_large_ids"]:
                inst["verify_large_ids"].add(order_id)
                inst["verify_large_count"] += 1
            if inst.get("cancel_started") and not inst.get("cancel_confirmed"):
                if amount >= RESEAL_VERIFY_LARGE_AMOUNT and order_id not in inst["late_large_ids"]:
                    inst["late_large_ids"].add(order_id)
                    inst["late_large_count"] += 1
            _check_validation(inst)


def _subscribe(C, code, period):
    global _SUB_SUCCESS, _SUB_FAILED
    callback = (lambda data, c=code: _handle_l2order(c, data)) if period == "l2order" else (lambda data, c=code, p=period: _handle_quote(c, p, data))
    try:
        sub_id = C.subscribe_quote(code, period=period, dividend_type="follow", result_type="dict", callback=callback)
        if sub_id and sub_id > 0:
            _SUB_SUCCESS += 1
            _SUBSCRIPTIONS.append(sub_id)
            _INSTANCES[code]["sub_ids"].append(sub_id)
            _log("[订阅] code=%s period=%s sub_id=%s" % (code, period, sub_id))
        else:
            _SUB_FAILED += 1
            _log("[订阅失败] %s %s 返回=%r" % (code, period, sub_id))
    except Exception as exc:
        _SUB_FAILED += 1
        _log("[订阅失败] %s %s %r" % (code, period, exc))


def _initial_quote(C, inst, full_ticks=None):
    try:
        if not inst["limit_up"] or not inst["last_close"] or inst["name"] == "未命名":
            try:
                detail = C.get_instrument_detail(inst["code"])
                inst["limit_up"] = inst["limit_up"] or _safe_float(_field(detail, "UpStopPrice", "upLimit", "limitUp"))
                inst["last_close"] = inst["last_close"] or _safe_float(_field(detail, "PreClose", "lastClose", "preClose"))
                inst["name"] = str(_field(detail, "InstrumentName", "stockName") or "未命名")
            except Exception:
                pass
        full = full_ticks if full_ticks is not None else C.get_full_tick([inst["code"]])
        data = _unwrap(inst["code"], full)
        inst["last_tick"] = data
        inst["limit_up"] = inst["limit_up"] or _limit_from(data)
        inst["last_close"] = inst["last_close"] or _safe_float(_field(data, "lastClose", "preClose", "pre_close"))
        inst["open_price"] = _safe_float(_field(data, "open", "openPrice"))
        # Continuous-session low starts with the first valid quote after 09:30.
        inst["day_low"] = None
        stime = _event_time(_field(data, "stime", "time"))
        raw_bid = _field(data, "bidPrice", "bid1")
        bid = _safe_float(raw_bid[0] if isinstance(raw_bid, (list, tuple)) else raw_bid)
        if stime and "092500" <= stime <= "092505" and inst["limit_up"] and bid:
            if abs(bid - inst["limit_up"]) < 1e-8:
                _set_state(inst, STATE_WAIT_REOPEN, "竞价已封板")
            else:
                _set_state(inst, STATE_READY, "竞价未封板")
        else:
            _log("[竞价] %s 快照时间或字段无效，等待连续竞价" % inst["code"])
    except Exception as exc:
        _log("[竞价] %s 快照失败 %r" % (inst["code"], exc))


def _initial_quote_batch(C, instances):
    codes = [inst["code"] for inst in instances]
    try:
        full_ticks = C.get_full_tick(codes) if codes else {}
    except Exception as exc:
        full_ticks = {}
        _log("[竞价] 批量快照失败 %r，继续等待订阅行情" % (exc,))
    for inst in instances:
        _initial_quote(C, inst, full_ticks)


def _limit_up_first_summary():
    tick_seen = sum(1 for value in _LIMIT_UP_FIRSTS.values() if "tick" in value)
    l2quote_seen = sum(1 for value in _LIMIT_UP_FIRSTS.values() if "l2quote" in value)
    paired = [value for value in _LIMIT_UP_FIRSTS.values() if value.get("compared")]
    tick_first = sum(1 for value in paired if value["winner"] == "tick")
    l2quote_first = sum(1 for value in paired if value["winner"] == "l2quote")
    ties = sum(1 for value in paired if value["winner"] == "tie")
    gaps = [value["gap_ms"] for value in paired]
    average = sum(gaps) / len(gaps) if gaps else None
    minimum = min(gaps) if gaps else None
    maximum = max(gaps) if gaps else None
    tick_only = sum(1 for value in _LIMIT_UP_FIRSTS.values()
                    if "tick" in value and "l2quote" not in value)
    l2quote_only = sum(1 for value in _LIMIT_UP_FIRSTS.values()
                       if "l2quote" in value and "tick" not in value)
    unseen = max(0, len(_INSTANCES) - len(_LIMIT_UP_FIRSTS))
    return ("stocks=%d tick_seen=%d l2quote_seen=%d paired=%d tick_first=%d "
            "l2quote_first=%d tie=%d gap_avg/min/max_ms=%s/%s/%s "
            "tick_only=%d l2quote_only=%d unseen=%d" %
            (len(_INSTANCES), tick_seen, l2quote_seen, len(paired), tick_first,
             l2quote_first, ties,
             "%.1f" % average if average is not None else "n/a",
             "%.1f" % minimum if minimum is not None else "n/a",
             "%.1f" % maximum if maximum is not None else "n/a",
             tick_only, l2quote_only, unseen))


def _summary():
    by_state = {}
    for inst in _INSTANCES.values():
        display_state = inst["state"]
        if inst.get("our_order_id") and display_state not in (STATE_DONE, STATE_SKIPPED):
            display_state = "已下单，等待成交"
        by_state.setdefault(display_state, []).append(inst["code"])
    period_counts = ",".join("%s:%d" % (period, _PERIOD_COUNTS.get(period, 0)) for period in PERIODS)
    first_callbacks = ",".join(sorted(_FIRST_CALLBACKS)) or "无"
    last_events = ",".join("%s:%s" % (period, _LAST_EVENT_TIMES.get(period, "")) for period in PERIODS)
    latency_parts = []
    for period in PERIODS:
        stats = _LATENCY_STATS.get(period, {})
        samples = stats.get("samples", 0)
        valid = stats.get("valid", 0)
        average = stats.get("sum_ms", 0.0) / valid if valid else None
        maximum = stats.get("max_ms")
        latest = stats.get("last_ms")
        latency_parts.append("%s:%d/%d,>1s=%d,avg=%s,max=%s,last=%s" %
                             (period, valid, samples, stats.get("over_threshold", 0),
                              "%.0fms" % average if average is not None else "n/a",
                              "%.0fms" % maximum if maximum is not None else "n/a",
                              "%.0fms" % latest if latest is not None else "n/a"))
        gaps = _GAP_STATS.get(period, {})
        gap_count = gaps.get("count", 0)
        gap_average = gaps.get("sum_ms", 0.0) / gap_count if gap_count else None
        latency_parts[-1] += ",gap=%s/%s,codes=%d" % (
            "%.0fms" % gap_average if gap_average is not None else "n/a",
            "%.0fms" % gaps.get("max_ms") if gap_count else "n/a",
            len([key for key in _LAST_RECEIVED_AT if key[0] == period]))
    _log("[汇总] 监控=%d 下单=%d 成交=%d 撤单=%d L2=%d 行情=%d 订阅=%d成功/%d失败 "
         "回调=%s 首条=%s 延时=%s 缓冲=%d 丢弃=%d 最近事件=%s 状态=%s" %
         (len(_INSTANCES), _COUNTERS["orders"], _COUNTERS["deals"], _COUNTERS["cancels"],
          _COUNTERS["l2"], _COUNTERS["quotes"], _SUB_SUCCESS, _SUB_FAILED,
          period_counts, "%d/%d[%s]" % (len(_FIRST_CALLBACKS), len(PERIODS), first_callbacks),
          ";".join(latency_parts), len(_RAW_BUFFER), _RAW_DROPPED, last_events,
          ";".join("%s:%s" % (k, ",".join(v)) for k, v in sorted(by_state.items()))))
    _log("[涨停首报汇总] %s" % _limit_up_first_summary())


def init(C):
    global _C, _LOG, _RAW_LOG, _LOG_PATH, _RAW_LOG_PATH, _DAY, _LAST_SUMMARY
    global _RAW_FLUSH_TIMER, _RAW_DROPPED, _AUCTION_RETRY_TIMER, _SUB_SUCCESS, _SUB_FAILED
    with _LOCK:
        _C = C
        if ACCOUNT_ID:
            try:
                C.set_account(ACCOUNT_ID, ACCOUNT_TYPE)
            except Exception as exc:
                _log("[初始化] 交易账户注册失败 %r" % (exc,))
        _DAY = _dt.datetime.now().strftime("%Y%m%d")
        script_root = _script_root()
        script_file = globals().get("__file__")
        log_dir = (os.path.join(script_root, "..", "userdata", "log")
                   if script_root else "")
        if not log_dir or not os.path.isdir(log_dir):
            log_dir = os.path.join(os.getcwd(), "userdata", "log")
        _LOG_PATH = ""
        _RAW_LOG_PATH = ""
        try:
            if not os.path.isdir(log_dir):
                os.makedirs(log_dir)
            _LOG_PATH = os.path.abspath(os.path.join(log_dir, "qmt_strategy_%s.log" % _DAY))
            _RAW_LOG_PATH = os.path.abspath(os.path.join(log_dir, "qmt_strategy_raw_%s.jsonl" % _DAY))
            _LOG = open(_LOG_PATH, "a")
            _RAW_LOG = open(_RAW_LOG_PATH, "a")
        except Exception:
            _LOG = None
            _RAW_LOG = None
        _log("[运行] script_root=%s" % (script_root or "<empty>"))
        _log("[运行] cwd=%s" % os.getcwd())
        _log("[运行] __file__=%s" % (os.path.abspath(script_file) if script_file else "absent"))
        _log("[运行] log_path=%s" % (_LOG_PATH or "<unavailable>"))
        _log("[运行] raw_log_path=%s" % (_RAW_LOG_PATH or "<unavailable>"))
        _log("[运行] ENABLE_LIVE_ORDERS=%s PERIODS=%s" %
             (ENABLE_LIVE_ORDERS, ",".join(PERIODS)))
        _log("[运行] latency_warning_threshold=%.0fms warn_interval=%ss" %
             (DEBUG_LATENCY_WARN_THRESHOLD_MS, DEBUG_LATENCY_WARN_INTERVAL_SECONDS))
        _COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0, "l2": 0, "quotes": 0})
        _PERIOD_COUNTS.clear()
        _FIRST_CALLBACKS.clear()
        _LAST_EVENT_TIMES.clear()
        _LATENCY_STATS.clear()
        _LATENCY_LAST_WARN.clear()
        _LAST_RECEIVED_AT.clear()
        _GAP_STATS.clear()
        _LIMIT_UP_FIRSTS.clear()
        del _RAW_BUFFER[:]
        _RAW_FLUSH_TIMER = None
        _RAW_DROPPED = 0
        _AUCTION_RETRY_TIMER = None
        _SUB_SUCCESS = 0
        _SUB_FAILED = 0
        _INSTANCES.clear()
        _SUBSCRIPTIONS[:] = []
        for item in _load_pool():
            _INSTANCES[item["code"]] = _new_instance(item["code"], item["plan_amount"])
        _initial_quote_batch(C, list(_INSTANCES.values()))
        for inst in _INSTANCES.values():
            for period in PERIODS:
                _subscribe(C, inst["code"], period)
        _log("[运行] subscriptions=%d success=%d failed=%d expected=%d" %
             (len(_SUBSCRIPTIONS), _SUB_SUCCESS, _SUB_FAILED,
              len(_INSTANCES) * len(PERIODS)))
        _schedule_raw_flush()
        _schedule_auction_retry()
        _LAST_SUMMARY = _now()
        _log("[运行] 原生模型启动完成，股票池=%d，调试汇总间隔=%ss" %
             (len(_INSTANCES), DEBUG_SUMMARY_INTERVAL_SECONDS))


def handlebar(C):
    global _LAST_SUMMARY
    with _LOCK:
        for inst in _INSTANCES.values():
            _check_validation(inst)
        if _now() - _LAST_SUMMARY >= DEBUG_SUMMARY_INTERVAL_SECONDS:
            _summary()
            _LAST_SUMMARY = _now()


def _apply_order_update(inst, item):
    inst["our_order_status"] = item["status"]
    inst["order_reported_traded_qty"] = max(
        inst.get("order_reported_traded_qty", 0), item["traded"])
    inst["our_traded_qty"] = max(
        inst.get("deal_traded_qty", 0), inst["order_reported_traded_qty"])
    if item["traded"] > 0:
        _cancel_timer(inst)
        _set_state(inst, STATE_DONE, "订单已成交")
    elif str(item["status"]).lower() in ("5", "6", "canceled", "cancelled", "撤单", "废单"):
        if inst.get("validation_order_id") == item["id"]:
            inst["cancel_confirmed"] = True
            if item["traded"] == 0:
                _log("[撤单] %s 已完全撤单" % inst["code"])
        else:
            _set_state(inst, STATE_DONE, "订单结束")


def _apply_deal_update(inst, item):
    deal_id = item.get("deal_id") or ""
    if deal_id and deal_id in inst["seen_deal_ids"]:
        return
    volume = item.get("deal_volume") or 0
    if volume <= 0:
        _log("[DEAL_CALLBACK_INVALID] id=%s code=%s deal_id=%s volume=%s" % (
            item.get("id", ""), item.get("code", ""), deal_id, volume))
        return
    if deal_id:
        inst["seen_deal_ids"].add(deal_id)
    inst["deal_traded_qty"] = inst.get("deal_traded_qty", 0) + volume
    inst["our_traded_qty"] = max(
        inst["deal_traded_qty"], inst.get("order_reported_traded_qty", 0))
    _COUNTERS["deals"] += 1
    _cancel_timer(inst)
    _set_state(inst, STATE_DONE, "成交回报")
    _log("[DEAL_CALLBACK] id=%s code=%s deal_id=%s volume=%s cum_volume=%s price=%s" % (
        item.get("id", ""), item.get("code", ""), deal_id, volume,
        inst["our_traded_qty"], item.get("price", 0)))


def order_callback(ContextInfo, orderInfo):
    with _LOCK:
        item = _order_fields(orderInfo)
        for inst in _INSTANCES.values():
            if item["id"] and item["id"] == str(inst.get("our_order_id")):
                _apply_order_update(inst, item)
                _log("[ORDER_CALLBACK] id=%s code=%s token=%s status=%s total=%s traded=%s price=%s" % (
                    item["id"], item["code"], item["user_order_id"], item["status"],
                    item["total"], item["traded"], item["price"]))
                return
        candidates = [inst for inst in _INSTANCES.values() if _pending_order_matches(inst, item)]
        if len(candidates) == 1:
            inst = candidates[0]
            if item["id"] and _bind_order_id(inst, item["id"]):
                _apply_order_update(inst, item)
                event = "ORDER_CALLBACK_ASSOCIATED"
            else:
                inst["pending_order_info"] = item
                event = "ORDER_CALLBACK_PENDING"
            _log("[%s] id=%s code=%s token=%s status=%s total=%s traded=%s price=%s" % (
                event,
                item["id"], item["code"], item["user_order_id"], item["status"],
                item["total"], item["traded"], item["price"]))
            return
        _log("[ORDER_CALLBACK_UNMATCHED] id=%s code=%s token=%s status=%s total=%s traded=%s price=%s" % (
            item["id"], item["code"], item["user_order_id"], item["status"],
            item["total"], item["traded"], item["price"]))


def deal_callback(ContextInfo, dealInfo):
    with _LOCK:
        item = _order_fields(dealInfo)
        for inst in _INSTANCES.values():
            if item["id"] and item["id"] == str(inst.get("our_order_id")):
                _apply_deal_update(inst, item)
                return
        candidates = [inst for inst in _INSTANCES.values() if _pending_order_matches(inst, item)]
        if len(candidates) == 1:
            inst = candidates[0]
            inst["pending_deal_infos"].append(item)
            if item["id"]:
                _bind_order_id(inst, item["id"])
            event = "DEAL_CALLBACK_ASSOCIATED" if item["id"] else "DEAL_CALLBACK_PENDING"
            _log("[%s] id=%s code=%s token=%s deal_id=%s volume=%s" % (
                event,
                item["id"], item["code"], item["user_order_id"],
                item["deal_id"], item["deal_volume"]))
            return
        _log("[DEAL_CALLBACK_UNMATCHED] id=%s code=%s token=%s deal_id=%s volume=%s" % (
            item["id"], item["code"], item["user_order_id"],
            item["deal_id"], item["deal_volume"]))


def stop(C):
    with _LOCK:
        global _RAW_FLUSH_TIMER, _AUCTION_RETRY_TIMER
        if _RAW_FLUSH_TIMER is not None:
            try:
                C.cancel_schedule_run(_RAW_FLUSH_TIMER)
            except Exception:
                pass
            _RAW_FLUSH_TIMER = None
        if _AUCTION_RETRY_TIMER is not None:
            try:
                C.cancel_schedule_run("qmt_auction_retry")
            except Exception:
                pass
            _AUCTION_RETRY_TIMER = None
        for inst in _INSTANCES.values():
            _cancel_timer(inst)
            _cancel_order_lookup(inst)
        _flush_raw(20000)
        for sub_id in list(_SUBSCRIPTIONS):
            try:
                C.unsubscribe_quote(sub_id)
            except Exception:
                pass
        _summary()
        for handle in (_LOG, _RAW_LOG):
            try:
                if handle:
                    handle.close()
            except Exception:
                pass
