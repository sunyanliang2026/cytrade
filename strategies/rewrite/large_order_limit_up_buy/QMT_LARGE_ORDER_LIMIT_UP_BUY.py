# coding: gbk
"""大QMT内置 Python 大单打板模型。

只使用 QMT 模型上下文、subscribe_quote、行情函数和内置交易函数。
ENABLE_LIVE_ORDERS 默认为 True，使用配置的 QMT 资金账号运行。
"""

from collections import deque
import datetime as _dt
import math
import time


# ------------------------- operator configuration -------------------------
ENABLE_LIVE_ORDERS = True
ACCOUNT_ID = "32011276"
ACCOUNT_TYPE = "stock"
STRATEGY_NAME = "large_order_limit_up_buy"
ORDER_TYPE_BUY = 1101
PRICE_TYPE_LIMIT = 11

POOL_TRADE_DATE = "20261027"

POOL_ITEMS = (
    ("002820", 25000.0),
    ("002093", 25000.0),
    ("001219", 25000.0),
    ("600558", 25000.0),
    ("603528", 25000.0),
    ("603028", 25000.0),
    ("603337", 25000.0),
)
FIRST_SEAL_MIN_AMOUNT = 60000000.0
FIRST_LARGE_ORDER_MIN_AMOUNT = 2000000.0
FIRST_LARGE_ORDER_TOTAL_AMOUNT = 10000000.0
LARGE_ORDER_MIN_VOLUME_SHARES = 900000
LARGE_ORDER_MIN_AMOUNT = 5000000.0
RESEAL_MIN_SEAL_SECONDS = 10.0
RESEAL_MIN_PEAK_AMOUNT = 100000000.0
RESEAL_BREAK_SECONDS = 3.0
RESEAL_VERIFY_ORDER_COUNT = 150
RESEAL_VERIFY_LARGE_AMOUNT = 1500000.0
RESEAL_VERIFY_LARGE_COUNT = 2
RESEAL_VALIDATION_WINDOW_SECONDS = 1.0
RESEAL_VALIDATION_CONTINUE_ON_FAILURE = False
RESEAL_LATE_WINDOW_SECONDS = 5.0
RESEAL_PRE_SUBMIT_ORDER_MAX_COUNT = 200
RESEAL_PRE_SUBMIT_ORDER_MAX_AGE_SECONDS = 2.0
ORDER_LOOKUP_RETRY_INTERVAL_SECONDS = 0.1
ORDER_LOOKUP_MAX_ATTEMPTS = 10
CANCEL_RETRY_INTERVAL_SECONDS = 0.1
CANCEL_MAX_ATTEMPTS = 3

PERIODS = ("l2quote", "l2order", "l2transaction")
DEBUG_SUMMARY_INTERVAL_SECONDS = 60
EXTERNAL_CANCEL_WAIT_LOG_INTERVAL_SECONDS = 60.0
DEBUG_LATENCY_WARN_THRESHOLD_MS = 2000.0
DEBUG_L2ORDER_LATENCY_WARN_THRESHOLD_MS = 1200.0
DEBUG_LATENCY_WARN_INTERVAL_SECONDS = 10.0
MAX_SIGNAL_QUOTE_DELAY_MS = 2000.0
FRESHNESS_MAX_DELAY_MS = {
    "l2quote": 2000.0,
    "l2order": 1200.0,
    "l2transaction": 1200.0,
}
FRESHNESS_FUTURE_TOLERANCE_MS = 1000.0
SEEN_ORDER_KEY_MAX_COUNT = 1000
L2ORDER_SUBSCRIBE_SECONDS = 9 * 60 * 60 + 30 * 60 + 30
L2ORDER_SUBSCRIBE_GAIN = 0.07
L2ORDER_SUBSCRIBE_RETRY_SECONDS = 1.0
L2ORDER_SUBSCRIBE_MAX_ATTEMPTS = 3
L2ORDER_SUBSCRIBE_BATCH = 10
L2_PERIODS = set(PERIODS)
VALIDATED_SUBMIT_REASONS = frozenset(("首封", "首封大单", "回封", "回封大单"))
STATE_WAIT_INITIAL = "WAIT_INITIAL_QUOTE"
STATE_READY = "READY"
STATE_WAIT_REOPEN = "WAIT_REOPEN"
STATE_WAIT_RESEAL = "WAIT_RESEAL"
STATE_DONE = "DONE"
STATE_SKIPPED = "SKIPPED_NEGATIVE_RETURN"

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
_DAY = ""
_LAST_SUMMARY = 0.0
_COUNTERS = {"orders": 0, "deals": 0, "cancels": 0, "l2": 0}
_PERIOD_COUNTS = {}
_CALLBACK_COUNTS = {}
_FIRST_CALLBACKS = {}
_LAST_EVENT_TIMES = {}
_LATENCY_STATS = {}
_LATENCY_LAST_WARN = {}
_LAST_RECEIVED_AT = {}
_GAP_STATS = {}
_LATEST_EVENT_EPOCH = {}
_STARTUP_EVENT_WATERMARK = {}
_FRESHNESS_DROPS = {}
_FRESHNESS_LAST_LOG = {}
_ENTRY_DROPS = {}
_AUCTION_RETRY_TIMER = None
_L2ORDER_SUBSCRIBE_TIMER = None


def _now():
    return time.monotonic()


def _wall_seconds_since_midnight(timestamp=None):
    stamp = _dt.datetime.fromtimestamp(time.time() if timestamp is None else timestamp)
    return (stamp.hour * 3600.0 + stamp.minute * 60.0 +
            stamp.second + stamp.microsecond / 1000000.0)


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
    if len(digits) == 10:
        try:
            fractional = float("0." + fraction) if fraction else 0.0
            stamp = _dt.datetime.fromtimestamp(int(digits) + fractional)
            return (stamp.hour * 3600.0 + stamp.minute * 60.0 + stamp.second +
                    stamp.microsecond / 1000000.0)
        except Exception:
            return None
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
    if isinstance(value, _dt.datetime):
        return True
    text = str(value).strip()
    integer, dot, fraction = text.partition(".")
    digits = "".join(ch for ch in integer if ch.isdigit())
    if dot and any(ch.isdigit() for ch in fraction):
        return True
    if len(digits) == 13 or len(digits) >= 15:
        return True
    return False


def _event_has_absolute_milliseconds(value=None):
    """Return whether the value has enough date/time precision for a gate."""
    if not _event_has_milliseconds(value):
        return False
    if isinstance(value, _dt.datetime):
        return True
    text = str(value).strip()
    integer, dot, fraction = text.partition(".")
    digits = "".join(ch for ch in integer if ch.isdigit())
    if len(digits) == 13:
        return True
    if len(digits) == 10:
        return bool(dot and any(ch.isdigit() for ch in fraction))
    # Full YYYYMMDD timestamps need at least one subsecond digit. A bare
    # HHMMSS.mmm value remains useful for session routing, but has no date.
    return len(digits) >= 14


def _event_epoch_seconds(value=None, reference_at=None):
    """Convert an absolute QMT event value to local epoch seconds."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        try:
            return value.timestamp()
        except Exception:
            return None
    text = str(value).strip()
    integer, dot, fraction = text.partition(".")
    digits = "".join(ch for ch in integer if ch.isdigit())
    fraction = "".join(ch for ch in fraction if ch.isdigit()) if dot else ""
    try:
        if len(digits) == 13:
            fractional_ms = float("0." + fraction) if fraction else 0.0
            return (float(int(digits)) + fractional_ms) / 1000.0
        if len(digits) == 10:
            fractional = float("0." + fraction) if fraction else 0.0
            return float(int(digits)) + fractional
        if len(digits) >= 14:
            stamp = _dt.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
            extra = digits[14:] + fraction
            micros = int((extra + "000000")[:6]) if extra else 0
            return stamp.timestamp() + micros / 1000000.0
        if len(digits) < 6:
            return None
        hhmmss = digits[:6]
        hour, minute, second = (int(hhmmss[:2]), int(hhmmss[2:4]),
                                int(hhmmss[4:6]))
        if hour > 23 or minute > 59 or second > 59:
            return None
        extra = digits[6:] + fraction
        micros = int((extra + "000000")[:6]) if extra else 0
        reference = _dt.datetime.fromtimestamp(
            time.time() if reference_at is None else reference_at)
        stamp = _dt.datetime.combine(
            reference.date(), _dt.time(hour, minute, second, micros))
        return stamp.timestamp()
    except Exception:
        return None


def _record_freshness_drop(code, period, reason, event_raw, delay_ms):
    stats = _FRESHNESS_DROPS.setdefault(period, {
        "count": 0, "last_reason": "", "last_delay_ms": None,
    })
    stats["count"] += 1
    stats["last_reason"] = reason
    stats["last_delay_ms"] = delay_ms
    key = (period, code)
    now = _now()
    if now - _FRESHNESS_LAST_LOG.get(key, 0.0) < DEBUG_LATENCY_WARN_INTERVAL_SECONDS:
        return
    _FRESHNESS_LAST_LOG[key] = now
    _log("[FRESHNESS_DROP] period=%s code=%s reason=%s delay_ms=%s event_time=%s" % (
        period, code, reason,
        "%.0f" % delay_ms if delay_ms is not None else "n/a",
        _format_event_timestamp(event_raw) or str(event_raw or "")))


def _record_entry_drop(period, reason):
    """Count cheap callback rejects without retaining their payloads."""
    stats = _ENTRY_DROPS.setdefault(period, {"count": 0, "reasons": {}})
    stats["count"] += 1
    reasons = stats["reasons"]
    reasons[reason] = reasons.get(reason, 0) + 1


def _freshness_gate(code, period, payload, received_at, record_latest=True):
    """Reject event times that cannot safely drive live strategy decisions."""
    event_raw = _field(payload, "time", "stime", "timestamp")
    event_text = "" if event_raw is None else str(event_raw).strip()
    if not event_text:
        _record_freshness_drop(code, period, "missing_event_time", event_raw, None)
        return False, event_raw, None
    digits = "".join(ch for ch in event_text if ch.isdigit())
    if not digits or not any(ch != "0" for ch in digits):
        _record_freshness_drop(code, period, "invalid_event_time", event_raw, None)
        return False, event_raw, None
    # Session-only or second-resolution timestamps can be logged, but cannot
    # establish event freshness closely enough to drive strategy state.
    if not _event_has_absolute_milliseconds(event_raw):
        if _event_seconds(event_raw) is None:
            _record_freshness_drop(code, period, "invalid_event_time", event_raw, None)
            return False, event_raw, None
        _record_freshness_drop(code, period, "unverifiable_event_time", event_raw, None)
        return False, event_raw, None
    event_epoch = _event_epoch_seconds(event_raw, received_at)
    if event_epoch is None:
        _record_freshness_drop(code, period, "invalid_event_time", event_raw, None)
        return False, event_raw, None
    delay_ms = (received_at - event_epoch) * 1000.0
    if delay_ms < -FRESHNESS_FUTURE_TOLERANCE_MS:
        _record_freshness_drop(code, period, "future_event", event_raw, delay_ms)
        return False, event_raw, delay_ms
    max_delay_ms = FRESHNESS_MAX_DELAY_MS.get(period)
    if max_delay_ms is not None and delay_ms > max_delay_ms:
        _record_freshness_drop(code, period, "stale_event", event_raw, delay_ms)
        return False, event_raw, delay_ms
    watermark = _STARTUP_EVENT_WATERMARK.get((period, code))
    if (watermark is not None and
            event_epoch < watermark - (max_delay_ms or 0.0) / 1000.0):
        _record_freshness_drop(code, period, "startup_replay", event_raw, delay_ms)
        return False, event_raw, delay_ms
    previous = _LATEST_EVENT_EPOCH.get((period, code))
    if previous is not None and event_epoch < previous - 0.001:
        _record_freshness_drop(code, period, "out_of_order", event_raw, delay_ms)
        return False, event_raw, delay_ms
    if record_latest:
        _record_latest_event(code, period, event_epoch)
    return True, event_raw, delay_ms


def _record_latest_event(code, period, event_epoch):
    key = (period, code)
    previous = _LATEST_EVENT_EPOCH.get(key)
    _LATEST_EVENT_EPOCH[key] = max(previous or event_epoch, event_epoch)


def _record_accepted_event(code, period, event_raw, received_at):
    if not _event_has_absolute_milliseconds(event_raw):
        return
    event_epoch = _event_epoch_seconds(event_raw, received_at)
    if event_epoch is not None:
        _record_latest_event(code, period, event_epoch)


def _format_local_timestamp(timestamp):
    return _dt.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3]


def _format_event_timestamp(value=None):
    """Format a QMT event timestamp as HH:MM:SS.mmm for diagnostics."""
    seconds = _event_seconds(value)
    if seconds is None:
        return ""
    whole_seconds = int(seconds)
    milliseconds = int(round((seconds - whole_seconds) * 1000.0))
    if milliseconds >= 1000:
        whole_seconds += 1
        milliseconds = 0
    whole_seconds %= 24 * 60 * 60
    hour, remainder = divmod(whole_seconds, 60 * 60)
    minute, second = divmod(remainder, 60)
    return "%02d:%02d:%02d.%03d" % (hour, minute, second, milliseconds)


def _latency_ms(event_value, received_at):
    if not _event_has_absolute_milliseconds(event_value):
        return None
    event_seconds = _event_epoch_seconds(event_value)
    if event_seconds is None:
        return None
    delay_ms = (received_at - event_seconds) * 1000.0
    # A negative value means the two clocks are not directly comparable. Do not
    # turn clock skew into a false low-latency result or a warning.
    return delay_ms if delay_ms >= 0 else None


def _latency_warn_threshold(period):
    if period in ("l2order", "l2transaction"):
        return DEBUG_L2ORDER_LATENCY_WARN_THRESHOLD_MS
    return DEBUG_LATENCY_WARN_THRESHOLD_MS


def _in_continuous(event_time):
    return "093000" <= event_time < "145700"


def _in_auction(event_time):
    return "091500" <= event_time < "093000"


def _log(message):
    line = "%s %s" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], message)
    try:
        print(line)
    except Exception:
        pass


def _record_l2_ingress(code, period, payload, received_at=None):
    received_at = time.time() if received_at is None else received_at
    received_time = _format_local_timestamp(received_at)
    event_value = _field(payload, "stime", "time")
    latency_value = _field(payload, "time", "stime")
    event_time = _event_time(event_value)
    latency_ms = _latency_ms(latency_value, received_at)
    _COUNTERS["l2"] += 1
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
        warn_threshold_ms = _latency_warn_threshold(period)
        if latency_ms > warn_threshold_ms:
            stats["over_threshold"] += 1
            now = _now()
            if now - _LATENCY_LAST_WARN.get(period, 0.0) >= DEBUG_LATENCY_WARN_INTERVAL_SECONDS:
                _LATENCY_LAST_WARN[period] = now
                _log("[延时警告] period=%s code=%s delay=%.0fms threshold=%.0fms "
                     "event_time=%s received_time=%s" %
                     (period, code, latency_ms, warn_threshold_ms,
                      event_time or "", received_time))
    if period not in _FIRST_CALLBACKS:
        _FIRST_CALLBACKS[period] = {
            "code": code, "event_time": event_time, "received_at": received_at,
            "received_time": received_time,
            "latency_ms": latency_ms,
        }
        _log("[回调首条] period=%s code=%s event_time=%s received_time=%s" %
             (period, code, event_time or "", received_time))
    return received_at


def _cache_l2order_quote(inst, source, event_raw, received_at):
    """Keep a separate price watermark before the business quote filter."""
    if (inst.get("subscriptions_stopped") or
            inst["state"] in (STATE_DONE, STATE_SKIPPED) or
            "l2order" in inst.get("sub_periods", {}).values() or
            not _event_has_absolute_milliseconds(event_raw)):
        return
    epoch = _event_epoch_seconds(event_raw, received_at)
    previous = inst.get("l2order_candidate")
    if epoch is None or (previous and epoch < previous[0]):
        return
    if not _in_continuous(_dt.datetime.fromtimestamp(epoch).strftime("%H%M%S")):
        return
    last = _safe_float(_field(source, "lastPrice", "last", "price", "close"))
    previous_close = (_safe_float(inst.get("last_close")) or
                      _safe_float(_field(source, "lastClose", "preClose", "pre_close")))
    # A newer incomplete price invalidates the previous candidate; never carry
    # an old above-threshold price across a newer quote with unknown prices.
    inst["l2order_candidate"] = (epoch, last, previous_close)


def _try_subscribe_l2order(C, inst, received_at=None):
    """Request order and transaction streams once above 7% and retain them."""
    if (C is None or inst.get("subscriptions_stopped") or
            inst["state"] in (STATE_DONE, STATE_SKIPPED) or
            ("l2order" in inst.get("sub_periods", {}).values() and
             "l2transaction" in inst.get("sub_periods", {}).values())):
        return False
    received_at = time.time() if received_at is None else received_at
    now = _dt.datetime.fromtimestamp(received_at)
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    session_time = now.strftime("%H%M%S")
    if (seconds < L2ORDER_SUBSCRIBE_SECONDS or
            not _in_continuous(session_time) or "113000" <= session_time < "130000"):
        return False
    candidate = inst.get("l2order_candidate")
    if not candidate:
        return False
    epoch, last, previous_close = candidate
    delay_ms = (received_at - epoch) * 1000.0
    if (delay_ms < -FRESHNESS_FUTURE_TOLERANCE_MS or
            delay_ms > FRESHNESS_MAX_DELAY_MS["l2quote"] or
            not last or not previous_close or
            not math.isfinite(last) or not math.isfinite(previous_close) or
            last - previous_close * (1.0 + L2ORDER_SUBSCRIBE_GAIN) <= 1e-8):
        return False
    attempts = inst.get("l2order_sub_attempts", 0)
    requested_at = inst.get("l2order_requested_at")
    if (attempts >= L2ORDER_SUBSCRIBE_MAX_ATTEMPTS or
            (requested_at is not None and
             received_at - requested_at < L2ORDER_SUBSCRIBE_RETRY_SECONDS)):
        return False
    inst["l2order_sub_attempts"] = attempts + 1
    inst["l2order_requested_at"] = received_at
    started = _now()
    if "l2order" not in inst.get("sub_periods", {}).values():
        _subscribe(C, inst["code"], "l2order")
    if "l2transaction" not in inst.get("sub_periods", {}).values():
        _subscribe(C, inst["code"], "l2transaction")
    registered = ("l2order" in inst.get("sub_periods", {}).values() and
                  "l2transaction" in inst.get("sub_periods", {}).values())
    _log("[L2ORDER_REQUEST] code=%s reason=gain_gt_7pct gain=%.2f%% "
         "event_time=%s quote_delay_ms=%.0f attempt=%d/%d "
         "registered=%s transaction_registered=%s call_ms=%.1f" % (
             inst["code"], (last / previous_close - 1.0) * 100.0,
             _format_local_timestamp(epoch), delay_ms, attempts + 1,
             L2ORDER_SUBSCRIBE_MAX_ATTEMPTS, registered,
             "l2transaction" in inst.get("sub_periods", {}).values(),
             (_now() - started) * 1000.0))
    return True


def _subscribe_l2order_now(C, pending=None):
    """At the opening boundary, check fresh candidates in bounded batches."""
    global _L2ORDER_SUBSCRIBE_TIMER
    _L2ORDER_SUBSCRIBE_TIMER = None
    codes = list(_INSTANCES) if pending is None else pending
    requested = 0
    for index, code in enumerate(codes):
        inst = _INSTANCES.get(code)
        if inst and _try_subscribe_l2order(C, inst):
            requested += 1
        if requested >= L2ORDER_SUBSCRIBE_BATCH and index + 1 < len(codes):
            remaining = codes[index + 1:]
            try:
                _L2ORDER_SUBSCRIBE_TIMER = C.schedule_run(
                    lambda ContextInfo, rest=remaining: _subscribe_l2order_now(ContextInfo, rest),
                    _dt.datetime.now() + _dt.timedelta(milliseconds=100),
                    repeat_times=0, name="qmt_l2order_subscribe")
            except Exception as exc:
                _log("[SUBSCRIBE] l2order batch timer failed %r; wait for fresh quotes" % (exc,))
            return


def _schedule_l2order_subscriptions(C):
    """Before 09:30:30 keep only quotes; never fall back to early fan-out."""
    global _L2ORDER_SUBSCRIBE_TIMER
    now = _dt.datetime.now()
    now_seconds = now.hour * 3600 + now.minute * 60 + now.second
    close_seconds = 14 * 60 * 60 + 57 * 60
    target = now.replace(hour=9, minute=30, second=30, microsecond=0)
    if L2ORDER_SUBSCRIBE_SECONDS <= now_seconds < close_seconds:
        _subscribe_l2order_now(C)
        return
    if now_seconds >= close_seconds:
        target += _dt.timedelta(days=1)
    try:
        _L2ORDER_SUBSCRIBE_TIMER = C.schedule_run(
            lambda ContextInfo: _subscribe_l2order_now(ContextInfo), target,
            repeat_times=0, name="qmt_l2order_subscribe")
        _log("[SUBSCRIBE] l2order eligible from %s, gain>7%%, retain after opening" %
             target.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as exc:
        _log("[SUBSCRIBE] l2order opening timer failed %r; wait for fresh quotes after 09:30:30" % (exc,))


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


def _load_pool(day=None):
    trade_date = str(day or _dt.datetime.now().strftime("%Y%m%d"))
    if trade_date != POOL_TRADE_DATE:
        _log("[股票池] 日期不匹配，当前日期=%s 配置日期=%s，股票池为空" %
             (trade_date, POOL_TRADE_DATE))
        return []
    result = []
    for raw_code, raw_amount in POOL_ITEMS:
        code = _stock_code(raw_code)
        amount = _safe_float(raw_amount)
        if code and amount:
            result.append({"code": code, "plan_amount": amount})
    _log("[股票池] date=%s count=%d codes=%s" %
         (trade_date, len(result), ",".join(x["code"] for x in result)))
    return result


def _new_instance(code, amount):
    return {
        "code": code, "name": "未命名", "plan_amount": amount, "state": STATE_WAIT_INITIAL,
        "limit_up": None, "last_close": None, "open_price": None, "day_low": None,
        "last_event_time": "", "last_l2quote": {}, "auction_checked": False,
        "initial_sealed": False, "seal_started": None, "seal_peak": 0.0,
        "break_started": None, "reseal_ready": False, "first_large_total": 0.0,
        "first_large_ids": set(), "seen_order_ids": set(), "seen_order_queue": deque(),
        "seen_cancel_event_keys": set(), "seen_cancel_event_queue": deque(),
        "l2order_history": [],
        "verify_count": 0,
        "verify_large_count": 0, "verify_large_ids": set(), "validation_started": None,
        "validation_pre_count": 0, "validation_pre_large_count": 0,
        "validation_active": False, "validation_deadline": None,
        "validation_event_cutoff_seconds": None, "validation_stale_count": 0,
        "validation_timer": None, "validation_order_id": None, "cancel_started": None,
        "cancel_confirmed": False, "cancel_failed": False, "cancel_due": False,
        "cancel_due_reason": "", "cancel_request_timer": None,
        "cancel_requested": False, "cancel_attempts": 0,
        "cancel_terminal": False, "external_cancel_due": False,
        "external_cancel_volume": 0, "external_cancel_event_keys": set(),
        "external_cancel_monitor_enabled": False,
        "external_cancel_wait_count": 0, "external_cancel_wait_last_log": 0.0,
        "trigger_order_id": None, "trigger_order_key": None,
        "trigger_order_price": None, "trigger_order_volume": 0,
        "trigger_order_amount": 0.0, "trigger_order_direction": "",
        "trigger_order_event_raw": None, "trigger_order_event_time": "",
        "pending_external_cancels": {},
        "late_large_count": 0,
        "late_large_ids": set(), "follow_sent": False, "our_order_id": None,
        "our_order_qty": 0, "our_traded_qty": 0, "our_order_status": "",
        "order_reported_traded_qty": 0, "deal_traded_qty": 0,
        "seen_deal_ids": set(), "pending_deal_infos": [],
        "previous_order_ids": set(), "validation_passed": False,
        "submission_pending": False, "submit_token": None, "submit_reason": None,
        "order_lookup_timer": None, "order_lookup_attempts": 0,
        "order_lookup_error_logged": False, "pending_order_info": None,
        "sub_ids": [], "sub_periods": {}, "subscriptions_stopped": False,
        "l2order_candidate": None, "l2order_sub_attempts": 0,
        "l2order_requested_at": None, "l2order_first_callback_at": None,
        "stale_negative_warned": False,
        "last_signal_period": "", "last_signal_event_raw": None,
        "last_signal_received_at": None,
        "last_latency_ms": None, "last_log_reason": "",
    }


def _unwrap(code, data):
    if isinstance(data, dict) and code in data:
        return data.get(code) or {}
    return data if isinstance(data, dict) else {}


def _owned_payload(code, data):
    """Detach the top-level callback fields without recursive deepcopy."""
    value = _unwrap(code, data)
    if not isinstance(value, dict):
        return {}
    detached = {}
    for key, item in value.items():
        if isinstance(item, list):
            detached[key] = list(item)
        elif isinstance(item, tuple):
            detached[key] = tuple(item)
        elif isinstance(item, dict):
            detached[key] = dict(item)
        else:
            detached[key] = item
    return detached


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


def _order_amount(price, volume, reported_amount=None):
    reported_amount = _safe_float(reported_amount)
    if reported_amount:
        return reported_amount
    price = _safe_float(price)
    volume = _safe_float(volume)
    return price * volume if price and volume else 0.0


def _is_large_order_trigger(volume, amount):
    volume = _safe_float(volume) or 0.0
    amount = _safe_float(amount) or 0.0
    return volume > LARGE_ORDER_MIN_VOLUME_SHARES or amount > LARGE_ORDER_MIN_AMOUNT


def _opening_low_filter_met(inst):
    opening = _safe_float(inst.get("open_price"))
    day_low = _safe_float(inst.get("day_low"))
    return bool(opening and day_low is not None and day_low < opening * 0.985)


def _limit_price(inst, quote=None):
    if inst["limit_up"]:
        return inst["limit_up"]
    value = _limit_from(quote or {}, inst.get("last_l2quote"))
    if value:
        inst["limit_up"] = value
    return value


def _set_state(inst, state, reason=""):
    if inst["state"] != state:
        inst["state"] = state
        inst["last_log_reason"] = reason
        _log("[状态] %s %s -> %s %s" % (inst["code"], inst.get("name", "未命名"), state, reason))


def _unsubscribe_instance(inst):
    if inst.get("subscriptions_stopped"):
        return 0, 0
    inst["subscriptions_stopped"] = True
    unsubscribed = 0
    failed = 0
    remaining = []
    for sub_id in list(inst.get("sub_ids", [])):
        period = inst.get("sub_periods", {}).get(sub_id, "unknown")
        if _C is None:
            failed += 1
            remaining.append(sub_id)
            _log("[UNSUBSCRIBE_FAIL] code=%s period=%s sub_id=%s reason=context_unavailable" %
                 (inst["code"], period, sub_id))
            continue
        try:
            result = _C.unsubscribe_quote(sub_id)
        except Exception as exc:
            failed += 1
            remaining.append(sub_id)
            _log("[UNSUBSCRIBE_FAIL] code=%s period=%s sub_id=%s error=%r" %
                 (inst["code"], period, sub_id, exc))
            continue
        unsubscribed += 1
        _SUBSCRIPTIONS[:] = [active_id for active_id in _SUBSCRIPTIONS
                             if active_id != sub_id]
        inst.get("sub_periods", {}).pop(sub_id, None)
        _log("[UNSUBSCRIBE] code=%s period=%s sub_id=%s result=%r" %
             (inst["code"], period, sub_id, result))
    inst["sub_ids"][:] = remaining
    return unsubscribed, failed


def _skip_negative_latest(inst, last, source, event_time=None, received_at=None):
    last = _safe_float(last)
    previous_close = _safe_float(inst.get("last_close"))
    if (inst["state"] in (STATE_DONE, STATE_SKIPPED) or
            inst.get("subscriptions_stopped") or not last or not previous_close or
            last >= previous_close):
        return False
    latency_ms = (_latency_ms(event_time, received_at)
                  if event_time is not None and received_at is not None else None)
    if latency_ms is not None and latency_ms > MAX_SIGNAL_QUOTE_DELAY_MS:
        if not inst.get("stale_negative_warned"):
            inst["stale_negative_warned"] = True
            _log("[NEGATIVE_RETURN_IGNORED_STALE] code=%s source=%s last=%.4f "
                 "prev_close=%.4f latency_ms=%.0f threshold_ms=%.0f" %
                 (inst["code"], source, last, previous_close, latency_ms,
                  MAX_SIGNAL_QUOTE_DELAY_MS))
        return False
    change_pct = (last / previous_close - 1.0) * 100.0
    _cancel_timer(inst)
    _cancel_order_lookup(inst)
    _set_state(inst, STATE_SKIPPED, "latest quote return < 0")
    unsubscribed, failed = _unsubscribe_instance(inst)
    _log("[NEGATIVE_RETURN_EXIT] code=%s source=%s last=%.4f prev_close=%.4f "
         "change_pct=%.4f unsubscribed=%d failed=%d active_subscriptions=%d "
         "existing_order_left_untouched=%s" %
         (inst["code"], source, last, previous_close, change_pct,
          unsubscribed, failed, len(_SUBSCRIPTIONS),
          inst.get("our_order_id") or ("pending" if inst.get("submission_pending") else "none")))
    return True


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


def _cancel_request_timer(inst):
    key = inst.get("cancel_request_timer")
    if key is not None and _C is not None:
        try:
            _C.cancel_schedule_run(key)
        except Exception:
            pass
    inst["cancel_request_timer"] = None


def _schedule_cancel_due(inst, delay=0.0):
    if (_C is None or inst.get("cancel_request_timer") is not None or
            not inst.get("cancel_due") or
            inst["state"] in (STATE_DONE, STATE_SKIPPED)):
        return False
    try:
        code = inst["code"]
        token = inst.get("submit_token")
        inst["cancel_request_timer"] = _C.schedule_run(
            lambda ContextInfo, c=code, t=token: _cancel_due_timer(ContextInfo, c, t),
            _dt.datetime.now() + _dt.timedelta(seconds=max(0.0, delay)),
            repeat_times=0, name="qmt_cancel_due_%s" % code)
        return True
    except Exception as exc:
        _log("[CANCEL_TIMER_FAILED] code=%s error=%r" % (inst["code"], exc))
        return False


def _cancel_due_timer(ContextInfo, code, token):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if (not inst or inst.get("submit_token") != token or
                not inst.get("cancel_due") or
                inst["state"] in (STATE_DONE, STATE_SKIPPED)):
            return
        inst["cancel_request_timer"] = None
        _request_cancel(inst, inst.get("cancel_due_reason") or "cancel_due", ContextInfo)


def _schedule_validation(inst):
    if _C is None or not inst.get("validation_active"):
        return
    try:
        remaining = max(0.0, (inst.get("validation_deadline") or _now()) - _now())
        when = _dt.datetime.now() + _dt.timedelta(seconds=remaining)
        code = inst["code"]
        token = inst.get("submit_token")
        inst["validation_timer"] = _C.schedule_run(
            lambda ContextInfo, c=code, t=token: _validation_timer(ContextInfo, c, t),
            when, repeat_times=0, name="order_validation_%s" % code)
    except Exception as exc:
        _log("[验证] %s 定时器创建失败 %r，将由handlebar兜底" % (inst["code"], exc))


def _remember_l2order(inst, event_key, order_id, event_raw, event_time,
                      price, direction, volume, amount, received_at, received_mono):
    # The caller has already restricted this record to an exact limit-up buy order.
    history = inst.setdefault("l2order_history", [])
    history.append({
        "event_key": event_key,
        "large_key": order_id or event_key,
        "order_id": order_id,
        "event_raw": str(event_raw) if event_raw is not None else "",
        "event_time": event_time or "",
        "event_seconds": _event_seconds(event_raw),
        "received_at": received_at,
        "received_mono": received_mono,
        "price": price,
        "direction": direction,
        "volume": volume,
        "amount": amount,
    })
    if len(history) > RESEAL_PRE_SUBMIT_ORDER_MAX_COUNT:
        del history[:-RESEAL_PRE_SUBMIT_ORDER_MAX_COUNT]


def _pre_submit_validation_records(inst, cutoff_seconds, now, trigger_order_key=None):
    records = []
    for record in inst.get("l2order_history", []):
        if trigger_order_key and record.get("event_key") == trigger_order_key:
            continue
        received_mono = record.get("received_mono")
        if received_mono is None:
            continue
        age = now - received_mono
        if age < 0.0 or age > RESEAL_PRE_SUBMIT_ORDER_MAX_AGE_SECONDS:
            continue
        event_seconds = record.get("event_seconds")
        if event_seconds is None or event_seconds > cutoff_seconds:
            continue
        records.append(record)
    return records


def _start_validation(inst, trigger_order_key=None, trigger_event_raw=None):
    if (inst.get("submit_reason") not in VALIDATED_SUBMIT_REASONS or
            inst["state"] in (STATE_DONE, STATE_SKIPPED)):
        return
    now = _now()
    cutoff_seconds = (_event_seconds(trigger_event_raw)
                      if trigger_event_raw is not None else None)
    cutoff_source = "trigger_l2order" if cutoff_seconds is not None else "local_decision"
    if cutoff_seconds is None:
        cutoff_seconds = _wall_seconds_since_midnight()
    pre_records = _pre_submit_validation_records(
        inst, cutoff_seconds, now, trigger_order_key)
    pre_large_ids = set(record.get("large_key") for record in pre_records
                         if record.get("amount", 0.0) >= RESEAL_VERIFY_LARGE_AMOUNT)
    inst["validation_started"] = now
    inst["validation_deadline"] = now + RESEAL_VALIDATION_WINDOW_SECONDS
    inst["validation_event_cutoff_seconds"] = cutoff_seconds
    inst["validation_stale_count"] = 0
    inst["validation_active"] = True
    inst["validation_order_id"] = inst.get("our_order_id")
    inst["validation_pre_count"] = len(pre_records)
    inst["validation_pre_large_count"] = len(pre_large_ids)
    # Pre-submit history is only a prepass check; observation counters start after submission.
    inst["verify_count"] = 0
    inst["verify_large_count"] = 0
    inst["verify_large_ids"] = set()
    inst["cancel_started"] = None
    inst["cancel_confirmed"] = False
    inst["cancel_failed"] = False
    inst["cancel_due"] = False
    inst["cancel_due_reason"] = ""
    inst["late_large_count"] = 0
    inst["late_large_ids"].clear()
    inst["follow_sent"] = False
    inst["validation_passed"] = False
    _log("[FOLLOW_VERIFY_START] code=%s token=%s order_id=%s window=%.0fms "
         "max_post_events=%d required_large=%d min_amount=%.0f pre_events=%d "
         "pre_large=%d history_max=%d history_age=%.1fs signal_period=%s "
         "signal_age_ms=%s boundary=%s boundary_seconds=%.3f" % (
             inst["code"], inst.get("submit_token"),
             inst.get("validation_order_id") or "pending",
             RESEAL_VALIDATION_WINDOW_SECONDS * 1000.0,
             RESEAL_VERIFY_ORDER_COUNT, RESEAL_VERIFY_LARGE_COUNT,
             RESEAL_VERIFY_LARGE_AMOUNT, inst["validation_pre_count"],
             inst["validation_pre_large_count"],
             RESEAL_PRE_SUBMIT_ORDER_MAX_COUNT,
             RESEAL_PRE_SUBMIT_ORDER_MAX_AGE_SECONDS,
             inst.get("last_signal_period") or "unknown",
             "%.0f" % inst["last_latency_ms"] if inst.get("last_latency_ms") is not None else "n/a",
             cutoff_source, cutoff_seconds))
    if inst["validation_pre_large_count"] >= RESEAL_VERIFY_LARGE_COUNT:
        inst["validation_active"] = False
        inst["validation_passed"] = True
        _log("[FOLLOW_VERIFY_PREPASS] code=%s pre_events=%d pre_large=%d/%d "
             "reason=pre_submit_history; order remains active" % (
                 inst["code"], inst["validation_pre_count"],
                 inst["validation_pre_large_count"], RESEAL_VERIFY_LARGE_COUNT))
    else:
        _schedule_validation(inst)


def _stop_validation(inst, reason):
    _cancel_timer(inst)
    inst["validation_active"] = False
    inst["cancel_due"] = False
    inst["cancel_due_reason"] = ""
    _log("[FOLLOW_VERIFY_STOP] code=%s token=%s reason=%s" % (
        inst["code"], inst.get("submit_token") or "", reason))


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
    if inst.get("submit_reason") in VALIDATED_SUBMIT_REASONS:
        inst["validation_order_id"] = order_id
    pending = inst.pop("pending_order_info", None)
    if pending:
        _apply_order_update(inst, pending)
    for deal in inst.pop("pending_deal_infos", []):
        _apply_deal_update(inst, deal)
    if inst.get("cancel_due") and inst["state"] not in (STATE_DONE, STATE_SKIPPED):
        _request_cancel(inst, inst.get("cancel_due_reason") or "cancel_due")
    return True


def _schedule_order_lookup(inst, delay=None):
    if (_C is None or not inst.get("submission_pending") or
            inst.get("order_lookup_timer") is not None or
            inst.get("order_lookup_attempts", 0) >= ORDER_LOOKUP_MAX_ATTEMPTS):
        return
    try:
        code = inst["code"]
        token = inst["submit_token"]
        delay = ORDER_LOOKUP_RETRY_INTERVAL_SECONDS if delay is None else max(0.0, delay)
        inst["order_lookup_timer"] = _C.schedule_run(
            lambda ContextInfo, c=code, t=token: _resolve_order_id(ContextInfo, c, t),
            _dt.datetime.now() + _dt.timedelta(seconds=delay),
            repeat_times=0, name="qmt_order_lookup_%s" % code)
    except Exception as exc:
        _log("[下单] %s 委托号查询定时器失败 %r" % (inst["code"], exc))


def _resolve_order_id(ContextInfo, code=None, token=None):
    # Keep the old two-argument form usable by offline tests and callers.
    if token is None:
        token = code
        code = ContextInfo
        ContextInfo = None
    with _LOCK:
        inst = _INSTANCES.get(code)
        if (not inst or inst["state"] in (STATE_DONE, STATE_SKIPPED) or
                inst.get("submit_token") != token or not inst.get("submission_pending")):
            return
        inst["order_lookup_timer"] = None
        if inst["order_lookup_attempts"] >= ORDER_LOOKUP_MAX_ATTEMPTS:
            return
        inst["order_lookup_attempts"] += 1
        records = _query_order_records(inst)
        matches = []
        if records is not None:
            for record in records:
                item = _order_fields(record)
                if item["id"] and _pending_order_matches(inst, item):
                    matches.append((item["id"], item))
        unique = {}
        for order_id, item in matches:
            unique[order_id] = item
        if len(unique) == 1:
            order_id, item = next(iter(unique.items()))
            if _bind_order_id(inst, order_id):
                _apply_order_update(inst, item)
                _log("[ORDER_ID_RESOLVED] code=%s token=%s order_id=%s source=order_query "
                     "attempt=%d" % (code, token, order_id, inst["order_lookup_attempts"]))
                if inst.get("cancel_due") and inst["state"] not in (STATE_DONE, STATE_SKIPPED):
                    _request_cancel(inst, inst.get("cancel_due_reason") or "cancel_due", ContextInfo)
                return
        elif len(unique) > 1:
            _log("[ORDER_ID_AMBIGUOUS] code=%s token=%s ids=%s attempt=%d" % (
                code, token, ",".join(sorted(unique)), inst["order_lookup_attempts"]))
        if inst["order_lookup_attempts"] < ORDER_LOOKUP_MAX_ATTEMPTS:
            _schedule_order_lookup(inst)
        else:
            _log("[ORDER_ID_UNRESOLVED] code=%s token=%s attempts=%d cancel_due=%s" % (
                code, token, inst["order_lookup_attempts"], inst.get("cancel_due", False)))


def _order_fields(obj):
    code = str(_field(obj, "m_strInstrumentID", "stockCode", "stock_code") or "").strip().upper()
    exchange = str(_field(obj, "m_strExchangeID", "exchangeID", "exchange_id") or "").strip().upper()
    if code and "." not in code and exchange:
        exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange, exchange)
        code = "%s.%s" % (code, exchange)
    return {
        "id": str(_field(obj, "m_strOrderSysID", "order_sysid", "m_strOrderID", "m_nOrderID", "orderID", "order_id", "orderId") or ""),
        "code": code,
        "deal_id": str(_field(obj, "m_strTradeID", "m_strDealID", "tradeID", "dealID", "tradedID", "traded_id") or ""),
        "user_order_id": str(_field(obj, "m_strRemark", "m_strOrderRemark", "m_strUserOrderID", "m_strUserOrderId", "userOrderId", "user_order_id", "order_remark", "remark") or ""),
        "strategy_name": str(_field(obj, "m_strStrategyName", "strategyName", "strategy_name", "strategy") or ""),
        "status": _field(obj, "m_nOrderStatus", "m_strOrderStatus", "order_status", "status"),
        "total": _safe_float(_field(obj, "m_nVolumeTotalOriginal", "m_nVolumeTotal", "orderVolume", "order_volume", "totalVolume")) or 0,
        "traded": _safe_float(_field(obj, "m_nVolumeTraded", "tradedVolume", "traded_volume", "volumeTraded")) or 0,
        "deal_volume": _safe_float(_field(obj, "m_nVolume", "m_nTradeVolume", "deal_volume", "traded_volume", "m_nVolumeTraded", "tradedVolume", "volume")) or 0,
        "price": _safe_float(_field(obj, "m_dPrice", "price", "order_price", "traded_price")) or 0,
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
    expected_token = str(inst.get("submit_token") or "")
    if item.get("code") and not _same_stock_code(item.get("code"), inst.get("code")):
        return False
    if token and token == expected_token:
        return True
    strategy_name = str(item.get("strategy_name") or "")
    return bool(expected_token and expected_token in strategy_name)


def _query_order_records(inst):
    getter = globals().get("get_trade_detail_data")
    if not callable(getter):
        if not inst.get("order_lookup_error_logged"):
            inst["order_lookup_error_logged"] = True
            _log("[ORDER_QUERY_UNAVAILABLE] code=%s reason=api_missing" % inst["code"])
        return None
    try:
        data = getter(ACCOUNT_ID, ACCOUNT_TYPE, "order")
    except Exception as exc:
        if not inst.get("order_lookup_error_logged"):
            inst["order_lookup_error_logged"] = True
            _log("[ORDER_QUERY_FAILED] code=%s error=%r" % (inst["code"], exc))
        return None
    if data is None:
        return None
    return list(data) if isinstance(data, (list, tuple)) else [data]


def _set_trigger_order(inst, trigger_order_key, trigger_order=None):
    if not trigger_order_key or not trigger_order:
        return
    order_id = str(trigger_order.get("order_id") or "")
    if not order_id or order_id in ("-1", "0"):
        return
    inst["trigger_order_key"] = trigger_order_key
    inst["trigger_order_id"], inst["external_cancel_monitor_enabled"] = order_id, True
    inst["trigger_order_price"] = _safe_float(trigger_order.get("price"))
    inst["trigger_order_volume"] = _safe_float(trigger_order.get("volume")) or 0
    inst["trigger_order_amount"] = _safe_float(trigger_order.get("amount")) or 0.0
    inst["trigger_order_direction"] = str(trigger_order.get("direction") or "").upper()
    inst["trigger_order_event_raw"] = trigger_order.get("event_raw")
    inst["trigger_order_event_time"] = str(trigger_order.get("event_time") or "")
    pending = inst.get("pending_external_cancels", {}).pop(order_id, [])
    for record in pending:
        _process_external_cancel(inst, record)


def _submit(inst, reason, trigger_order_key=None, trigger_order=None):
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
        _cancel_request_timer(inst)
        inst["cancel_started"] = None
        inst["cancel_confirmed"] = False
        inst["cancel_failed"] = False
        inst["cancel_due"] = False
        inst["cancel_due_reason"] = ""
        inst["cancel_requested"] = False
        inst["cancel_attempts"] = 0
        inst["cancel_terminal"] = False
        inst["external_cancel_due"], inst["external_cancel_monitor_enabled"] = False, False
        inst["external_cancel_volume"] = 0
        inst["external_cancel_event_keys"].clear()
        inst["trigger_order_key"] = None
        inst["trigger_order_id"] = None
        inst["trigger_order_price"] = None
        inst["trigger_order_volume"] = 0
        inst["trigger_order_amount"] = 0.0
        inst["trigger_order_direction"] = ""
        inst["trigger_order_event_raw"] = None
        inst["trigger_order_event_time"] = ""
        inst["pending_external_cancels"].clear()
        inst["external_cancel_wait_count"] = 0
        inst["external_cancel_wait_last_log"] = 0.0
        trigger_event_raw = (trigger_order.get("event_raw")
                             if trigger_order else None)
        inst["trigger_order_event_raw"] = trigger_event_raw
        signal_received_at = inst.get("last_signal_received_at")
        signal_event_raw = inst.get("last_signal_event_raw")
        signal_age_ms = (_latency_ms(signal_event_raw, time.time())
                         if signal_event_raw is not None else None)
        receive_to_submit_ms = (
            max(0.0, (time.time() - signal_received_at) * 1000.0)
            if signal_received_at is not None else None)
        if reason in VALIDATED_SUBMIT_REASONS:
            _start_validation(
                inst, trigger_order_key, trigger_event_raw)
        _set_trigger_order(inst, trigger_order_key, trigger_order)
        passorder_started = _now()
        try:
            order_result = passorder(23, ORDER_TYPE_BUY, ACCOUNT_ID, inst["code"], PRICE_TYPE_LIMIT,
                                     price, qty, STRATEGY_NAME, 2, inst["submit_token"], _C)
        except Exception:
            if reason in VALIDATED_SUBMIT_REASONS:
                _stop_validation(inst, "passorder_exception")
            raise
        if order_result is False or str(order_result) == "-1":
            inst["submission_pending"] = False
            if reason in VALIDATED_SUBMIT_REASONS:
                _stop_validation(inst, "submit_rejected")
            _log("[下单失败] %s 接口返回=%r" % (inst["code"], order_result))
            _set_state(inst, STATE_DONE, "下单接口拒绝")
            return False
        if isinstance(order_result, (str, int)) and not isinstance(order_result, bool) and str(order_result).strip() not in ("", "0", "-1"):
            _bind_order_id(inst, order_result)
        if inst["submission_pending"]:
            _schedule_order_lookup(inst)
        _log("[下单提交] %s %s 价格=%.3f 数量=%d 返回=%r "
             "signal_period=%s signal_age_ms=%s receive_to_submit_ms=%s passorder_ms=%.1f "
             "order_id=%s" % (
                 inst["code"], reason, price, qty, order_result,
                 inst.get("last_signal_period") or "unknown",
                 "%.0f" % signal_age_ms if signal_age_ms is not None else "n/a",
                 "%.1f" % receive_to_submit_ms if receive_to_submit_ms is not None else "n/a",
                 (_now() - passorder_started) * 1000.0,
                 inst.get("our_order_id") or "pending"))
        return True
    except Exception as exc:
        inst["submission_pending"] = False
        _log("[下单失败] %s %r" % (inst["code"], exc))
        _set_state(inst, STATE_DONE, "下单异常")
        return False


def _our_cancel_order_id(inst):
    return str(inst.get("our_order_id") or inst.get("validation_order_id") or "")


def _cancel_attempt_failed(inst, reason, error):
    inst["cancel_requested"] = False
    inst["cancel_failed"] = True
    inst["cancel_due"] = True
    inst["cancel_due_reason"] = reason
    _log("[CANCEL_FAILED] code=%s order_id=%s attempt=%d/%d error=%r" % (
        inst["code"], _our_cancel_order_id(inst), inst.get("cancel_attempts", 0),
        CANCEL_MAX_ATTEMPTS, error))
    if inst.get("cancel_attempts", 0) < CANCEL_MAX_ATTEMPTS:
        _schedule_cancel_due(inst, CANCEL_RETRY_INTERVAL_SECONDS)


def _confirm_cancel(inst, status=None):
    inst["cancel_requested"] = False
    inst["cancel_due"] = False
    inst["cancel_failed"] = False
    inst["cancel_confirmed"] = True
    _cancel_request_timer(inst)
    _log("[CANCEL_CONFIRMED] code=%s order_id=%s status=%s traded=%s/%s" % (
        inst["code"], _our_cancel_order_id(inst), status,
        inst.get("our_traded_qty", 0), inst.get("our_order_qty", 0)))
    if inst.get("external_cancel_due") or inst.get("cancel_terminal"):
        _set_state(inst, STATE_DONE, "external_trigger_cancelled")


def _request_cancel(inst, reason="validation_failed", ContextInfo=None):
    if inst["state"] in (STATE_DONE, STATE_SKIPPED):
        return False
    if inst.get("our_order_qty", 0) > 0 and inst.get("our_traded_qty", 0) >= inst["our_order_qty"]:
        inst["cancel_due"] = False
        inst["cancel_requested"] = False
        _cancel_request_timer(inst)
        _set_state(inst, STATE_DONE, "订单全部成交，跳过撤单")
        return False
    if inst.get("cancel_confirmed"):
        inst["cancel_due"] = False
        _log("[CANCEL_SKIPPED] code=%s order_id=%s reason=%s already_cancelled=true" % (
            inst["code"], _our_cancel_order_id(inst) or "unknown", reason))
        return False
    if inst.get("cancel_requested"):
        return False
    order_id = _our_cancel_order_id(inst)
    if not order_id:
        inst["cancel_due"] = True
        inst["cancel_due_reason"] = reason
        _log("[CANCEL_WAIT_ORDER_ID] code=%s token=%s reason=%s post_events=%d post_large=%d "
             "attempts=%d" % (
                 inst["code"], inst.get("submit_token"), reason,
                 inst.get("verify_count", 0), inst.get("verify_large_count", 0),
                 inst.get("order_lookup_attempts", 0)))
        _schedule_order_lookup(inst)
        return False
    if inst.get("cancel_attempts", 0) >= CANCEL_MAX_ATTEMPTS:
        inst["cancel_due"] = True
        inst["cancel_due_reason"] = reason
        _log("[CANCEL_RETRY_EXHAUSTED] code=%s order_id=%s reason=%s" % (
            inst["code"], order_id, reason))
        return False
    if ContextInfo is None:
        # A real QMT timer/callback always supplies ContextInfo.  The global
        # context is only a compatibility fallback for offline fake contexts
        # that cannot schedule a callback.
        if _C is not None and callable(getattr(_C, "schedule_run", None)):
            inst["cancel_due"] = True
            inst["cancel_due_reason"] = reason
            _schedule_cancel_due(inst, 0.0)
            return False
        ContextInfo = _C
    if ContextInfo is None:
        inst["cancel_due"] = True
        inst["cancel_due_reason"] = reason
        return False
    _cancel_request_timer(inst)
    inst["cancel_due"] = False
    inst["cancel_due_reason"] = reason
    if inst.get("cancel_started") is None:
        inst["cancel_started"] = _now()
    inst["cancel_attempts"] += 1
    inst["cancel_requested"] = True
    _COUNTERS["cancels"] += 1
    if not ENABLE_LIVE_ORDERS:
        _confirm_cancel(inst, "paper")
        return True
    try:
        result = cancel(order_id, ACCOUNT_ID, ACCOUNT_TYPE, ContextInfo)
        if result is False or str(result) == "-1":
            _cancel_attempt_failed(inst, reason, result)
            return False
        _log("[CANCEL_REQUESTED] code=%s order_id=%s reason=%s attempt=%d/%d return=%r context=%s" % (
            inst["code"], order_id, reason, inst["cancel_attempts"],
            CANCEL_MAX_ATTEMPTS, result, type(ContextInfo).__name__))
        return True
    except Exception as exc:
        _cancel_attempt_failed(inst, reason, exc)
        return False


def _validation_timer(ContextInfo, code=None, token=None):
    if token is None:
        token = code
        code = ContextInfo
        ContextInfo = None
    with _LOCK:
        inst = _INSTANCES.get(code)
        if (not inst or inst.get("submit_token") != token or
                not inst.get("validation_active") or
                inst["state"] in (STATE_DONE, STATE_SKIPPED)):
            return
        inst["validation_timer"] = None
        _check_validation(inst, ContextInfo)


def _check_validation(inst, ContextInfo=None):
    if inst["state"] in (STATE_DONE, STATE_SKIPPED):
        return
    if inst.get("validation_active"):
        if inst["verify_large_count"] >= RESEAL_VERIFY_LARGE_COUNT:
            inst["validation_active"] = False
            _cancel_timer(inst)
            if not inst.get("validation_passed"):
                inst["validation_passed"] = True
                _log("[FOLLOW_VERIFY_PASS] code=%s order_id=%s post_events=%d post_large=%d "
                     "elapsed_ms=%.0f; order remains active" % (
                         inst["code"], inst.get("validation_order_id") or "pending",
                         inst["verify_count"], inst["verify_large_count"],
                         (_now() - inst["validation_started"]) * 1000.0))
            return
        elapsed = _now() - (inst["validation_started"] or _now())
        reached_event_limit = inst["verify_count"] >= RESEAL_VERIFY_ORDER_COUNT
        reached_deadline = elapsed >= RESEAL_VALIDATION_WINDOW_SECONDS
        if reached_event_limit or reached_deadline:
            reason = "event_limit" if reached_event_limit else "timeout"
            inst["validation_active"] = False
            _cancel_timer(inst)
            _log("[FOLLOW_VERIFY_FAIL] code=%s order_id=%s reason=%s post_events=%d/%d "
                 "post_large=%d/%d elapsed_ms=%.0f stale_events=%d; cancel required" % (
                     inst["code"], inst.get("validation_order_id") or "pending", reason,
                     inst["verify_count"], RESEAL_VERIFY_ORDER_COUNT,
                     inst["verify_large_count"], RESEAL_VERIFY_LARGE_COUNT,
                     elapsed * 1000.0, inst.get("validation_stale_count", 0)))
            _request_cancel(inst, reason, ContextInfo)
    if (inst.get("external_cancel_due") and not inst.get("cancel_confirmed") and
            not inst.get("cancel_requested")):
        _request_cancel(inst, "external_trigger_cancel", ContextInfo)
    if (inst.get("cancel_started") and not inst["cancel_confirmed"] and
            inst.get("cancel_attempts", 0) >= CANCEL_MAX_ATTEMPTS and
            not inst.get("cancel_requested")):
        _log("[CANCEL_PENDING] code=%s order_id=%s retries_exhausted=true" % (
            inst["code"], _our_cancel_order_id(inst)))
        return
    if inst.get("external_cancel_due") and inst.get("cancel_confirmed"):
        _set_state(inst, STATE_DONE, "external_trigger_cancelled")
        return
    if inst.get("cancel_confirmed") and inst["our_traded_qty"] > 0:
        _set_state(inst, STATE_DONE, "部分成交后余量撤单")
        return
    if inst.get("cancel_confirmed") and not RESEAL_VALIDATION_CONTINUE_ON_FAILURE:
        _set_state(inst, STATE_DONE, "撤单后补单已关闭")
        return
    if (inst.get("cancel_confirmed") and not inst.get("follow_sent") and
            _now() - inst["cancel_started"] < RESEAL_LATE_WINDOW_SECONDS):
        if inst["late_large_count"] >= RESEAL_VERIFY_LARGE_COUNT and inst["last_l2quote"].get("bid_at_limit"):
            inst["follow_sent"] = True
            _submit(inst, "撤单后跟随")


def _quote_scalar(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return _safe_float(value)


def _is_cancel_direction(value):
    return str(value or "").strip().upper() in ("3", "C", "CANCEL", "CANCELLED")


def _is_buy_direction(value):
    return str(value or "").strip().upper() in ("B", "BUY", "1", "买", "买入")


def _external_cancel_watch_active(inst):
    """Only inspect external cancel events while our order can be affected."""
    if inst.get("submission_pending") and inst.get("external_cancel_monitor_enabled"):
        return True
    return bool(inst.get("external_cancel_monitor_enabled") and
        inst.get("our_order_id") and
        inst.get("state") not in (STATE_DONE, STATE_SKIPPED)
    )


def _remember_cancel_event_key(inst, event_key):
    if not event_key:
        return
    keys = inst.setdefault("seen_cancel_event_keys", set())
    queue = inst.setdefault("seen_cancel_event_queue", deque())
    keys.add(event_key)
    queue.append(event_key)
    while len(queue) > SEEN_ORDER_KEY_MAX_COUNT:
        keys.discard(queue.popleft())


def _external_cancel_matches(inst, record):
    trigger_id = str(inst.get("trigger_order_id") or "")
    order_id = str(record.get("order_id") or "")
    if not trigger_id or order_id != trigger_id:
        return False
    if inst.get("trigger_order_direction") and not _is_buy_direction(
            inst.get("trigger_order_direction")):
        return False
    trigger_price = _safe_float(inst.get("trigger_order_price"))
    cancel_price = _safe_float(record.get("price"))
    return not (trigger_price and cancel_price and
                abs(trigger_price - cancel_price) >= 1e-8)


def _remember_pending_external_cancel(inst, record):
    order_id = str(record.get("order_id") or "")
    if not order_id:
        return
    pending = inst.setdefault("pending_external_cancels", {})
    records = pending.setdefault(order_id, [])
    records.append(record)
    total = sum(len(items) for items in pending.values())
    while total > 64:
        first_key = next(iter(pending))
        items = pending[first_key]
        items.pop(0)
        total -= 1
        if not items:
            pending.pop(first_key, None)


def _process_external_cancel(inst, record):
    order_id = str(record.get("order_id") or "")
    if not order_id or not inst.get("external_cancel_monitor_enabled"):
        return False
    if not inst.get("trigger_order_id"):
        _remember_pending_external_cancel(inst, record)
        wait_count = int(inst.get("external_cancel_wait_count", 0) or 0) + 1
        inst["external_cancel_wait_count"] = wait_count
        now = _now()
        last_log = float(inst.get("external_cancel_wait_last_log", 0.0) or 0.0)
        if (wait_count == 1 or
                now - last_log >= EXTERNAL_CANCEL_WAIT_LOG_INTERVAL_SECONDS):
            inst["external_cancel_wait_last_log"] = now
            _log("[EXTERNAL_CANCEL_WAIT_TRIGGER] code=%s pending_ids=%d count=%d "
                 "last_order_id=%s source=%s" % (
                     inst["code"], len(inst.get("pending_external_cancels", {})),
                     wait_count, order_id, record.get("source") or "unknown"))
        return False
    if not _external_cancel_matches(inst, record):
        _log("[EXTERNAL_CANCEL_IGNORED] code=%s order_id=%s trigger=%s price=%s source=%s" % (
            inst["code"], order_id, inst.get("trigger_order_id"),
            record.get("price"), record.get("source") or "unknown"))
        return False
    event_key = str(record.get("event_key") or "")
    if event_key and event_key in inst.setdefault("external_cancel_event_keys", set()):
        return False
    if event_key:
        inst["external_cancel_event_keys"].add(event_key)
    inst["external_cancel_volume"] += _safe_float(record.get("volume")) or 0
    inst["external_cancel_due"] = True
    inst["cancel_terminal"] = True
    _log("[EXTERNAL_TRIGGER_CANCEL] code=%s trigger_order=%s volume=%s source=%s event=%s" % (
        inst["code"], order_id, record.get("volume") or 0,
        record.get("source") or "unknown", record.get("event_raw") or ""))
    _request_cancel(inst, "external_trigger_cancel")
    return True


def _quote_entry_gate(inst, source):
    """Return (accepted, event_time, reason) before copying a quote payload."""
    event_time = _event_time(_field(source, "stime", "time", "timestamp"))
    if not event_time or not _in_continuous(event_time):
        return False, event_time, "outside_continuous"
    if inst["state"] in (STATE_DONE, STATE_SKIPPED):
        return False, event_time, "terminal_state"

    last = _safe_float(_field(source, "lastPrice", "last", "price", "close"))
    opening = _safe_float(_field(source, "open", "openPrice"))
    low = _safe_float(_field(source, "low", "lowest", "lowPrice"))
    previous_close = (_safe_float(inst.get("last_close")) or
                      _safe_float(_field(source, "lastClose", "preClose", "pre_close")))
    # A valid negative return can trigger the existing unsubscribe path even
    # when the snapshot is missing a bid or upper-limit field.
    if last and previous_close and last < previous_close:
        return True, event_time, "negative_return"

    limit_up = (_safe_float(inst.get("limit_up")) or
                _limit_from(source, inst.get("last_l2quote")))
    bid = _quote_scalar(_field(source, "bidPrice", "bid", "bid1", "bidPrice1"))
    if limit_up and bid:
        bid_at_limit = abs(bid - limit_up) < 1e-8
        previous_quote = inst.get("last_l2quote", {})
        previous = previous_quote.get("bid_at_limit")
        current_bid_vol = _quote_scalar(
            _field(source, "bidVol", "bidVolume", "bid1_volume", "bidVolume1"))
        previous_bid_vol = _safe_float(previous_quote.get("bidVol"))
        queue_growth = bool(
            bid_at_limit and current_bid_vol and
            (previous_bid_vol is None or current_bid_vol > previous_bid_vol))
        if (previous is None or bool(previous) != bid_at_limit or queue_growth or
                inst["state"] == STATE_WAIT_INITIAL):
            reason = ("limit_up_queue_growth" if queue_growth else
                      "limit_up_book" if bid_at_limit else "seal_transition")
            return True, event_time, reason
    day_low = _safe_float(inst.get("day_low"))
    if ((opening and not inst.get("open_price")) or
            (low and (day_low is None or low < day_low)) or
            (last and (day_low is None or last < day_low)) or
            (previous_close and not inst.get("last_close"))):
        return True, event_time, "seed_price_state"
    if not limit_up:
        return False, event_time, "missing_limit_up"
    if not bid:
        return False, event_time, "missing_bid"
    return False, event_time, "unchanged_quote"


def _l2order_entry_gate(inst, source, event_raw):
    """Return (accepted, event_time, reason, event_key) for useful orders."""
    event_time = _event_time(_field(source, "stime", "time", "timestamp"))
    if not event_time or not _in_continuous(event_time):
        return False, event_time, "outside_continuous", None
    if inst["state"] in (STATE_DONE, STATE_SKIPPED):
        return False, event_time, "terminal_state", None

    volume = _safe_float(_field(source, "volume"))
    direction = str(_field(source, "entrustDirection", "direction", "side") or "").upper()
    order_id = str(_field(source, "entrustNo", "orderNo", "order_id") or "")
    if _is_cancel_direction(direction):
        if not order_id:
            return False, event_time, "cancel_missing_order_id", None
        if not _external_cancel_watch_active(inst):
            return False, event_time, "cancel_not_needed", None
        trigger_order_id = str(inst.get("trigger_order_id") or "")
        if trigger_order_id and order_id != trigger_order_id:
            return False, event_time, "cancel_not_trigger", None
        cancel_volume = volume or 0.0
        event_key = "cancel:%s:%s:%s" % (order_id, event_raw or event_time, cancel_volume)
        if event_key in inst.setdefault("seen_cancel_event_keys", set()):
            return False, event_time, "duplicate", event_key
        return True, event_time, "cancel", event_key
    price = _safe_float(_field(source, "price"))
    if not price:
        return False, event_time, "invalid_price", None
    if not volume:
        return False, event_time, "invalid_volume", None
    if not _is_buy_direction(direction):
        return False, event_time, "not_buy", None

    limit_up = _safe_float(inst.get("limit_up"))
    if not limit_up:
        return False, event_time, "missing_limit_up", None
    if abs(price - limit_up) >= 1e-8:
        return False, event_time, "off_limit_up", None

    event_key = order_id or "event:%s:%s:%s:%s" % (
        event_raw or event_time, price, volume, direction)
    if event_key in inst["seen_order_ids"]:
        return False, event_time, "duplicate", event_key
    return True, event_time, "limit_up_buy", event_key


def _snapshot_entry_gate(inst, source):
    event_time = _event_time(_field(source, "stime", "time", "timestamp"))
    if event_time and "092500" <= event_time <= "092505":
        limit_up = (_safe_float(inst.get("limit_up")) or
                    _limit_from(source, inst.get("last_l2quote")))
        bid = _quote_scalar(_field(source, "bidPrice", "bid", "bid1", "bidPrice1"))
        if limit_up and bid:
            return True, event_time, "auction_book"
        return False, event_time, "missing_auction_book"
    if event_time and _in_continuous(event_time):
        snapshot_price = (_safe_float(_field(source, "open", "openPrice")) or
                          _safe_float(_field(source, "low", "lowest", "lowPrice")) or
                          _safe_float(_field(source, "lastPrice", "last", "price", "close")))
        if snapshot_price:
            return True, event_time, "snapshot_price_state"
    return _quote_entry_gate(inst, source)


def _handle_quote(code, period, payload):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if not inst or inst.get("subscriptions_stopped") or inst["state"] == STATE_SKIPPED:
            return
        received_at = time.time()
        _CALLBACK_COUNTS[period] = _CALLBACK_COUNTS.get(period, 0) + 1
        source = _unwrap(code, payload)
        fresh, event_raw, _ = _freshness_gate(
            code, period, source, received_at, record_latest=False)
        if not fresh:
            return
        if period == "l2quote":
            _cache_l2order_quote(inst, source, event_raw, received_at)
            _try_subscribe_l2order(_C, inst, received_at)
        accepted, event_time, drop_reason = _quote_entry_gate(inst, source)
        if not accepted:
            _record_entry_drop(period, drop_reason)
            return
        _record_accepted_event(code, period, event_raw, received_at)
        data = _owned_payload(code, payload)
        is_l2quote = period == "l2quote"
        if event_time:
            inst["last_event_time"] = event_time
        received_at = _record_l2_ingress(code, period, data, received_at)
        if is_l2quote:
            inst["last_signal_period"] = period
            inst["last_signal_event_raw"] = event_raw
            inst["last_signal_received_at"] = received_at
            inst["last_latency_ms"] = _latency_ms(inst["last_signal_event_raw"], received_at)
        limit_up = _limit_price(inst, data)
        is_continuous = bool(event_time and _in_continuous(event_time))
        bid, bid_vol, ask, last, low = _quote_values(inst, data, track_day=is_continuous)
        if inst["last_close"] is None:
            inst["last_close"] = _safe_float(_field(data, "lastClose", "preClose", "pre_close"))
        if not is_continuous:
            if is_l2quote:
                _check_validation(inst)
            return
        if (is_l2quote and
                _skip_negative_latest(inst, last, "l2quote",
                                      _field(data, "time", "stime", "timestamp"),
                                      received_at)):
            return
        if not is_l2quote:
            return
        if not limit_up or not bid:
            return
        bid_at_limit = abs(bid - limit_up) < 1e-8
        seal_amount = _amount(bid, bid_vol)
        if is_l2quote:
            # Keep only the latest scalar quote state needed by the strategy.
            inst["last_l2quote"] = {
                "time": _field(data, "time"),
                "stime": _field(data, "stime"),
                "upLimit": limit_up,
                "bidPrice": bid,
                "bidVol": bid_vol,
                "lastPrice": last,
                "open": inst.get("open_price"),
                "low": low,
            }
        inst["last_l2quote"]["bid_at_limit"] = bid_at_limit
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
        if not inst or inst.get("subscriptions_stopped") or inst["state"] == STATE_SKIPPED:
            return
        received_at = time.time()
        _CALLBACK_COUNTS["l2order"] = _CALLBACK_COUNTS.get("l2order", 0) + 1
        source = _unwrap(code, payload)
        requested_at = inst.get("l2order_requested_at")
        if requested_at is not None and inst.get("l2order_first_callback_at") is None:
            inst["l2order_first_callback_at"] = received_at
            delay_ms = _latency_ms(_field(source, "time", "stime", "timestamp"), received_at)
            _log("[L2ORDER_FIRST_CALLBACK] code=%s request_to_callback_ms=%.0f "
                 "event_delay_ms=%s" % (
                     code, (received_at - requested_at) * 1000.0,
                     "%.0f" % delay_ms if delay_ms is not None else "n/a"))
        fresh, event_raw, _ = _freshness_gate(
            code, "l2order", source, received_at, record_latest=False)
        if not fresh:
            return
        accepted, event_time, drop_reason, event_key = _l2order_entry_gate(
            inst, source, event_raw)
        if not accepted:
            _record_entry_drop("l2order", drop_reason)
            return
        _record_accepted_event(code, "l2order", event_raw, received_at)
        data = _owned_payload(code, payload)
        received_at = _record_l2_ingress(code, "l2order", data, received_at)
        inst["last_signal_period"] = "l2order"
        inst["last_signal_event_raw"] = event_raw
        inst["last_signal_received_at"] = received_at
        inst["last_latency_ms"] = _latency_ms(event_raw, received_at)
        if event_time:
            inst["last_event_time"] = event_time
        if inst["state"] in (STATE_DONE, STATE_SKIPPED):
            return
        price = _safe_float(_field(data, "price"))
        volume = _safe_float(_field(data, "volume"))
        direction = str(_field(data, "entrustDirection", "direction", "side") or "").upper()
        order_id = str(_field(data, "entrustNo", "orderNo", "order_id") or "")
        if drop_reason == "cancel":
            _remember_cancel_event_key(inst, event_key)
            _process_external_cancel(inst, {
                "event_key": event_key,
                "event_raw": event_raw,
                "event_time": event_time,
                "order_id": order_id,
                "price": price,
                "volume": volume or 0,
                "source": "l2order",
            })
            return
        inst["seen_order_ids"].add(event_key)
        seen_queue = inst.setdefault("seen_order_queue", deque())
        seen_queue.append(event_key)
        while len(seen_queue) > SEEN_ORDER_KEY_MAX_COUNT:
            inst["seen_order_ids"].discard(seen_queue.popleft())
        amount = _order_amount(price, volume, _field(data, "amount"))
        _remember_l2order(
            inst, event_key, order_id, event_raw, event_time, price, direction,
            volume, amount, received_at, _now())

        # L2-triggered validation starts at the trigger event, not local submit time.
        if inst.get("validation_started") is not None and inst.get("submit_reason") in VALIDATED_SUBMIT_REASONS:
            if order_id and order_id in (
                    str(inst.get("validation_order_id") or ""),
                    str(inst.get("our_order_id") or "")):
                return
            if inst.get("validation_active"):
                event_seconds = _event_seconds(event_raw)
                cutoff = inst.get("validation_event_cutoff_seconds")
                if event_seconds is None:
                    inst["validation_stale_count"] += 1
                    if inst["validation_stale_count"] == 1:
                        _log("[FOLLOW_VERIFY_TIME_UNAVAILABLE] code=%s event=%s excluded" % (
                            inst["code"], event_raw))
                    _check_validation(inst)
                    return
                if event_seconds is not None and cutoff is not None and event_seconds <= cutoff:
                    inst["validation_stale_count"] += 1
                    if inst["validation_stale_count"] == 1:
                        _log("[FOLLOW_VERIFY_BEFORE_BOUNDARY] code=%s event=%s cutoff=%.3f" % (
                            inst["code"], event_raw, cutoff))
                    _check_validation(inst)
                    return
                inst["verify_count"] += 1
                large_key = order_id or event_key
                is_large = amount >= RESEAL_VERIFY_LARGE_AMOUNT and large_key not in inst["verify_large_ids"]
                if is_large:
                    inst["verify_large_ids"].add(large_key)
                    inst["verify_large_count"] += 1
                if (inst["verify_count"] == 1 or inst["verify_count"] % 25 == 0 or is_large):
                    _log("[FOLLOW_VERIFY_PROGRESS] code=%s post_events=%d/%d post_large=%d/%d "
                         "last_amount=%.0f event=%s" % (
                             inst["code"], inst["verify_count"], RESEAL_VERIFY_ORDER_COUNT,
                             inst["verify_large_count"], RESEAL_VERIFY_LARGE_COUNT,
                             amount, event_raw or event_time))
                _check_validation(inst)
            elif inst.get("cancel_started") and not inst.get("cancel_confirmed"):
                if amount >= RESEAL_VERIFY_LARGE_AMOUNT:
                    large_key = order_id or event_key
                    if large_key not in inst["late_large_ids"]:
                        inst["late_large_ids"].add(large_key)
                        inst["late_large_count"] += 1
            return
        if inst.get("submission_pending") or inst.get("validation_passed") or inst.get("cancel_due"):
            return

        trigger_reason = None
        if (inst["state"] == STATE_READY
                and _is_large_order_trigger(volume, amount)
                and _opening_low_filter_met(inst)):
            trigger_reason = "首封大单"
        elif (
            inst["state"] == STATE_WAIT_RESEAL
            and inst.get("reseal_ready")
            and inst.get("break_started") is not None
            and _now() - inst["break_started"] >= RESEAL_BREAK_SECONDS
            and _is_large_order_trigger(volume, amount)
        ):
            trigger_reason = "回封大单"
        if trigger_reason:
            if _submit(inst, trigger_reason, event_key, {
                    "order_id": order_id, "price": price, "volume": volume,
                    "amount": amount, "direction": direction,
                    "event_time": event_time, "event_raw": event_raw}):
                _log("[大单触发] %s 路径=%s 委托=%s 数量=%d股(%.2f手) 金额=%.2f" % (
                    inst["code"], trigger_reason, order_id or "无编号", int(volume),
                    volume / 100.0, amount))
            return

        if inst["state"] == STATE_READY and inst["initial_sealed"]:
            if amount >= FIRST_LARGE_ORDER_MIN_AMOUNT:
                if order_id not in inst["first_large_ids"]:
                    inst["first_large_ids"].add(order_id)
                    inst["first_large_total"] += amount
            if (inst["first_large_total"] > FIRST_LARGE_ORDER_TOTAL_AMOUNT
                    and _opening_low_filter_met(inst)):
                _submit(inst, "首封", event_key, {
                    "order_id": order_id, "price": price, "volume": volume,
                    "amount": amount, "direction": direction,
                    "event_time": event_time, "event_raw": event_raw})


def _handle_l2transaction(code, payload):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if not inst or inst.get("subscriptions_stopped") or inst["state"] == STATE_SKIPPED:
            return
        received_at = time.time()
        _CALLBACK_COUNTS["l2transaction"] = _CALLBACK_COUNTS.get("l2transaction", 0) + 1
        source = _unwrap(code, payload)
        fresh, event_raw, _ = _freshness_gate(
            code, "l2transaction", source, received_at, record_latest=False)
        if not fresh:
            return
        event_time = _event_time(_field(source, "stime", "time", "timestamp"))
        if not event_time or not _in_continuous(event_time):
            _record_entry_drop("l2transaction", "outside_continuous")
            return
        trade_flag = _field(source, "tradeFlag", "trade_flag", "flag")
        if not _is_cancel_direction(trade_flag):
            _record_entry_drop("l2transaction", "not_cancel")
            return
        order_id = str(_field(source, "buyNo", "buy_no") or "")
        if not order_id:
            _record_entry_drop("l2transaction", "cancel_missing_buy_no")
            return
        if not _external_cancel_watch_active(inst):
            _record_entry_drop("l2transaction", "cancel_not_needed")
            return
        trigger_order_id = str(inst.get("trigger_order_id") or "")
        if trigger_order_id and order_id != trigger_order_id:
            _record_entry_drop("l2transaction", "cancel_not_trigger")
            return
        volume = _safe_float(_field(source, "volume", "tradeVolume", "trade_volume")) or 0
        price = _safe_float(_field(source, "price"))
        event_key = "cancel:%s:%s:%s" % (order_id, event_raw or event_time, volume)
        if event_key in inst.setdefault("seen_cancel_event_keys", set()):
            _record_entry_drop("l2transaction", "duplicate")
            return
        _remember_cancel_event_key(inst, event_key)
        _record_accepted_event(code, "l2transaction", event_raw, received_at)
        data = _owned_payload(code, payload)
        received_at = _record_l2_ingress(code, "l2transaction", data, received_at)
        _process_external_cancel(inst, {
            "event_key": event_key,
            "event_raw": event_raw,
            "event_time": event_time,
            "order_id": order_id,
            "price": price,
            "volume": volume,
            "source": "l2transaction",
        })


def _subscribe(C, code, period):
    global _SUB_SUCCESS, _SUB_FAILED
    inst = _INSTANCES.get(code)
    if not inst or inst.get("subscriptions_stopped") or inst["state"] == STATE_SKIPPED:
        return
    if period == "l2order":
        callback = lambda data, c=code: _handle_l2order(c, data)
    elif period == "l2transaction":
        callback = lambda data, c=code: _handle_l2transaction(c, data)
    else:
        callback = lambda data, c=code, p=period: _handle_quote(c, p, data)
    try:
        sub_id = C.subscribe_quote(code, period=period, dividend_type="follow", result_type="dict", callback=callback)
        if sub_id and sub_id > 0:
            _SUB_SUCCESS += 1
            _SUBSCRIPTIONS.append(sub_id)
            inst["sub_ids"].append(sub_id)
            inst["sub_periods"][sub_id] = period
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
        snapshot_received_at = time.time()
        fresh, snapshot_event_raw, snapshot_delay_ms = _freshness_gate(
            inst["code"], "l2quote", data, snapshot_received_at,
            record_latest=False)
        if not fresh:
            _log("[初始快照未通过新鲜度校验] code=%s session_time=%s event_time=%s delay_ms=%s，等待带绝对时间的有效 l2quote" % (
                inst["code"], _event_time(_field(data, "stime", "time")) or "n/a",
                _format_event_timestamp(snapshot_event_raw) or
                str(snapshot_event_raw or ""),
                "%.0f" % snapshot_delay_ms if snapshot_delay_ms is not None else "n/a"))
            return
        _cache_l2order_quote(inst, data, snapshot_event_raw, snapshot_received_at)
        accepted, _snapshot_time, drop_reason = _snapshot_entry_gate(inst, data)
        if not accepted:
            _record_entry_drop("l2quote", "snapshot_%s" % drop_reason)
            return
        _record_accepted_event(
            inst["code"], "l2quote", snapshot_event_raw, snapshot_received_at)
        inst["last_l2quote"] = {
            "time": _field(data, "time"),
            "stime": _field(data, "stime"),
            "upLimit": _limit_from(data),
            "bidPrice": _field(data, "bidPrice", "bid1"),
            "bidVol": _field(data, "bidVol", "bidVolume", "bid1_volume"),
            "lastPrice": _field(data, "lastPrice", "last", "price"),
            "open": _field(data, "open", "openPrice"),
            "low": _field(data, "low", "lowest", "lowPrice"),
        }
        inst["limit_up"] = inst["limit_up"] or _limit_from(data)
        inst["last_close"] = inst["last_close"] or _safe_float(_field(data, "lastClose", "preClose", "pre_close"))
        inst["open_price"] = _safe_float(_field(data, "open", "openPrice"))
        # Do not carry auction or prior-session values into the continuous-session low.
        inst["day_low"] = None
        stime = _event_time(_field(data, "stime", "time"))
        if stime and _in_continuous(stime):
            # Seed the cached intraday low before the first l2order can trigger.
            _, _, _, last, _ = _quote_values(inst, data, track_day=True)
            if _skip_negative_latest(inst, last, "initial_snapshot",
                                     _field(data, "time", "stime", "timestamp"), time.time()):
                return
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
    freshness_drops = ",".join(
        "%s:%d" % (period, _FRESHNESS_DROPS.get(period, {}).get("count", 0))
        for period in PERIODS)
    entry_drops = ",".join(
        "%s:%d[%s]" % (
            period,
            _ENTRY_DROPS.get(period, {}).get("count", 0),
            ",".join("%s=%d" % item for item in sorted(
                _ENTRY_DROPS.get(period, {}).get("reasons", {}).items())) or "none")
        for period in PERIODS)
    latency_parts = []
    for period in PERIODS:
        stats = _LATENCY_STATS.get(period, {})
        samples = stats.get("samples", 0)
        valid = stats.get("valid", 0)
        average = stats.get("sum_ms", 0.0) / valid if valid else None
        maximum = stats.get("max_ms")
        latest = stats.get("last_ms")
        threshold_seconds = _latency_warn_threshold(period) / 1000.0
        latency_parts.append("%s:%d/%d,>%.0fs=%d,avg=%s,max=%s,last=%s" %
                             (period, valid, samples, threshold_seconds,
                              stats.get("over_threshold", 0),
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
    _log("[汇总] 监控=%d 下单=%d 成交=%d 撤单=%d L2=%d 订阅=%d成功/%d失败 "
         "回调=%s 首条=%s 延时=%s freshness_drop=%s "
         "entry_drop=%s 最近事件=%s 状态=%s" %
         (len(_INSTANCES), _COUNTERS["orders"], _COUNTERS["deals"], _COUNTERS["cancels"],
          _COUNTERS["l2"], _SUB_SUCCESS, _SUB_FAILED,
          period_counts, "%d/%d[%s]" % (len(_FIRST_CALLBACKS), len(PERIODS), first_callbacks),
          ";".join(latency_parts), freshness_drops,
          entry_drops,
          last_events,
          ";".join("%s:%s" % (k, ",".join(v)) for k, v in sorted(by_state.items()))))

    registered = sum(1 for inst in _INSTANCES.values()
                     if "l2order" in inst.get("sub_periods", {}).values())
    first_callback = sum(1 for inst in _INSTANCES.values()
                         if inst.get("l2order_first_callback_at") is not None)
    registered_transaction = sum(1 for inst in _INSTANCES.values()
                                 if "l2transaction" in inst.get("sub_periods", {}).values())
    _log("[L2ORDER_COVERAGE] pool=%d registered=%d transaction_registered=%d "
         "first_callback=%d ingress_quote=%d ingress_order=%d ingress_transaction=%d" % (
             len(_INSTANCES), registered, registered_transaction, first_callback,
             _CALLBACK_COUNTS.get("l2quote", 0), _CALLBACK_COUNTS.get("l2order", 0),
             _CALLBACK_COUNTS.get("l2transaction", 0)))


def init(C):
    global _C, _DAY, _LAST_SUMMARY, _AUCTION_RETRY_TIMER
    global _L2ORDER_SUBSCRIBE_TIMER, _SUB_SUCCESS, _SUB_FAILED
    with _LOCK:
        _C = C
        if ACCOUNT_ID:
            try:
                C.set_account(ACCOUNT_ID, ACCOUNT_TYPE)
            except Exception as exc:
                _log("[初始化] 交易账户注册失败 %r" % (exc,))
        _DAY = _dt.datetime.now().strftime("%Y%m%d")
        _log("[运行] log=console_only raw_capture=disabled")
        _log("[运行] ENABLE_LIVE_ORDERS=%s PERIODS=%s" %
             (ENABLE_LIVE_ORDERS, ",".join(PERIODS)))
        _log("[运行] latency_warning_thresholds=l2quote:%.0fms,l2order:%.0fms,l2transaction:%.0fms "
             "warn_interval=%ss" %
             (DEBUG_LATENCY_WARN_THRESHOLD_MS,
              DEBUG_L2ORDER_LATENCY_WARN_THRESHOLD_MS,
              DEBUG_L2ORDER_LATENCY_WARN_THRESHOLD_MS,
              DEBUG_LATENCY_WARN_INTERVAL_SECONDS))
        _COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0, "l2": 0})
        _PERIOD_COUNTS.clear()
        _CALLBACK_COUNTS.clear()
        _FIRST_CALLBACKS.clear()
        _LAST_EVENT_TIMES.clear()
        _LATENCY_STATS.clear()
        _LATENCY_LAST_WARN.clear()
        _LAST_RECEIVED_AT.clear()
        _GAP_STATS.clear()
        _LATEST_EVENT_EPOCH.clear()
        _STARTUP_EVENT_WATERMARK.clear()
        _FRESHNESS_DROPS.clear()
        _FRESHNESS_LAST_LOG.clear()
        _ENTRY_DROPS.clear()
        _AUCTION_RETRY_TIMER = None
        _L2ORDER_SUBSCRIBE_TIMER = None
        _SUB_SUCCESS = 0
        _SUB_FAILED = 0
        _INSTANCES.clear()
        _SUBSCRIPTIONS[:] = []
        for item in _load_pool(_DAY):
            _INSTANCES[item["code"]] = _new_instance(item["code"], item["plan_amount"])
        startup_received_at = time.time()
        for inst in _INSTANCES.values():
            for period in PERIODS:
                _STARTUP_EVENT_WATERMARK[(period, inst["code"])] = startup_received_at
        _initial_quote_batch(C, list(_INSTANCES.values()))
        for inst in _INSTANCES.values():
            _subscribe(C, inst["code"], "l2quote")
        _log("[运行] l2order_policy=start:09:30:30,gain>7%%,retain_after_opening; "
             "retry_interval=%.1fs,max_attempts=%d" % (
                 L2ORDER_SUBSCRIBE_RETRY_SECONDS, L2ORDER_SUBSCRIBE_MAX_ATTEMPTS))
        _schedule_l2order_subscriptions(C)
        expected_subscriptions = sum(
            sum(1 for period in PERIODS if period in inst.get("sub_periods", {}).values())
            for inst in _INSTANCES.values()
            if not inst.get("subscriptions_stopped"))
        pending_l2order = sum(
             1 for inst in _INSTANCES.values()
            if (not inst.get("subscriptions_stopped") and
                ("l2order" not in inst.get("sub_periods", {}).values() or
                 "l2transaction" not in inst.get("sub_periods", {}).values())))
        skipped_count = sum(1 for inst in _INSTANCES.values()
                            if inst["state"] == STATE_SKIPPED)
        _log("[运行] subscriptions=%d success=%d failed=%d expected=%d "
             "watching_l2order_candidates=%d skipped=%d" %
             (len(_SUBSCRIPTIONS), _SUB_SUCCESS, _SUB_FAILED,
              expected_subscriptions, pending_l2order, skipped_count))
        _schedule_auction_retry()
        _LAST_SUMMARY = _now()
        _log("[运行] 原生模型启动完成，股票池=%d，调试汇总间隔=%ss" %
             (len(_INSTANCES), DEBUG_SUMMARY_INTERVAL_SECONDS))


def handlebar(C):
    global _LAST_SUMMARY
    is_last_bar = getattr(C, "is_last_bar", None)
    if callable(is_last_bar) and not is_last_bar():
        return
    with _LOCK:
        for inst in _INSTANCES.values():
            _check_validation(inst, C)
        if _now() - _LAST_SUMMARY >= DEBUG_SUMMARY_INTERVAL_SECONDS:
            _summary()
            _LAST_SUMMARY = _now()


def _apply_order_update(inst, item):
    inst["our_order_status"] = item["status"]
    inst["order_reported_traded_qty"] = max(
        inst.get("order_reported_traded_qty", 0), item["traded"])
    inst["our_traded_qty"] = max(
        inst.get("deal_traded_qty", 0), inst["order_reported_traded_qty"])
    status = str(item["status"]).lower()
    if inst["our_traded_qty"] >= inst["our_order_qty"] > 0 or status == "56":
        _cancel_timer(inst)
        _cancel_request_timer(inst)
        inst["cancel_due"] = False
        inst["cancel_requested"] = False
        _set_state(inst, STATE_DONE, "订单全部成交")
    elif status in ("5", "6", "53", "54", "canceled", "cancelled", "撤单"):
        if _our_cancel_order_id(inst) == item["id"]:
            _confirm_cancel(inst, status)
            if item["traded"] == 0:
                _log("[撤单] %s 已完全撤单" % inst["code"])
        else:
            _set_state(inst, STATE_DONE, "订单结束")
    elif status in ("57", "rejected", "废单"):
        _cancel_timer(inst)
        _cancel_request_timer(inst)
        inst["cancel_requested"] = False
        _set_state(inst, STATE_DONE, "订单废单")


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
    if inst["our_traded_qty"] >= inst["our_order_qty"] > 0:
        _cancel_timer(inst)
        _cancel_request_timer(inst)
        inst["cancel_due"] = False
        inst["cancel_requested"] = False
        _set_state(inst, STATE_DONE, "订单全部成交")
    _log("[DEAL_CALLBACK] id=%s code=%s deal_id=%s volume=%s cum_volume=%s price=%s" % (
        item.get("id", ""), item.get("code", ""), deal_id, volume,
        inst["our_traded_qty"], item.get("price", 0)))


def _is_ignored_exited_order_callback(inst, item):
    if inst.get("state") != STATE_SKIPPED:
        return False
    order_id = str(item.get("id") or "")
    for field in ("our_order_id", "validation_order_id"):
        owned_id = str(inst.get(field) or "")
        if order_id and owned_id and order_id == owned_id:
            return True
    token = str(inst.get("submit_token") or "")
    return bool(token and str(item.get("user_order_id") or "") == token)


def order_callback(ContextInfo, orderInfo):
    with _LOCK:
        item = _order_fields(orderInfo)
        if any(_is_ignored_exited_order_callback(inst, item)
               for inst in _INSTANCES.values()):
            return
        for inst in _INSTANCES.values():
            if item["id"] and item["id"] == str(inst.get("our_order_id")):
                _apply_order_update(inst, item)
                if inst.get("cancel_due") and inst["state"] not in (STATE_DONE, STATE_SKIPPED):
                    _request_cancel(inst, inst.get("cancel_due_reason") or "cancel_due", ContextInfo)
                if inst.get("cancel_confirmed"):
                    _check_validation(inst, ContextInfo)
                _log("[ORDER_CALLBACK] id=%s code=%s token=%s status=%s total=%s traded=%s price=%s" % (
                    item["id"], item["code"], item["user_order_id"], item["status"],
                    item["total"], item["traded"], item["price"]))
                return
        candidates = [inst for inst in _INSTANCES.values()
                      if inst["state"] != STATE_SKIPPED and _pending_order_matches(inst, item)]
        if len(candidates) == 1:
            inst = candidates[0]
            if item["id"] and _bind_order_id(inst, item["id"]):
                _apply_order_update(inst, item)
                if inst.get("cancel_due") and inst["state"] not in (STATE_DONE, STATE_SKIPPED):
                    _request_cancel(inst, inst.get("cancel_due_reason") or "validation_failed", ContextInfo)
                if inst.get("cancel_confirmed"):
                    _check_validation(inst, ContextInfo)
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
        if any(_is_ignored_exited_order_callback(inst, item)
               for inst in _INSTANCES.values()):
            return
        for inst in _INSTANCES.values():
            if item["id"] and item["id"] == str(inst.get("our_order_id")):
                _apply_deal_update(inst, item)
                if inst.get("cancel_due") and inst["state"] not in (STATE_DONE, STATE_SKIPPED):
                    _request_cancel(inst, inst.get("cancel_due_reason") or "cancel_due", ContextInfo)
                return
        candidates = [inst for inst in _INSTANCES.values()
                      if inst["state"] != STATE_SKIPPED and _pending_order_matches(inst, item)]
        if len(candidates) == 1:
            inst = candidates[0]
            inst["pending_deal_infos"].append(item)
            if item["id"]:
                _bind_order_id(inst, item["id"])
                if inst.get("cancel_due") and inst["state"] not in (STATE_DONE, STATE_SKIPPED):
                    _request_cancel(inst, inst.get("cancel_due_reason") or "validation_failed", ContextInfo)
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
        global _AUCTION_RETRY_TIMER, _L2ORDER_SUBSCRIBE_TIMER
        if _AUCTION_RETRY_TIMER is not None:
            try:
                C.cancel_schedule_run("qmt_auction_retry")
            except Exception:
                pass
            _AUCTION_RETRY_TIMER = None
        if _L2ORDER_SUBSCRIBE_TIMER is not None:
            try:
                C.cancel_schedule_run(_L2ORDER_SUBSCRIBE_TIMER)
            except Exception:
                pass
            _L2ORDER_SUBSCRIBE_TIMER = None
        for inst in _INSTANCES.values():
            _cancel_timer(inst)
            _cancel_order_lookup(inst)
            _cancel_request_timer(inst)
        for sub_id in list(_SUBSCRIPTIONS):
            try:
                C.unsubscribe_quote(sub_id)
            except Exception:
                pass
        _summary()
