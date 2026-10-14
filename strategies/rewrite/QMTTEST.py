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

PERIODS = ("tick", "l2quote", "l2quoteaux", "l2transaction", "l2order", "l2transactioncount", "l2orderqueue")
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
_LOG = None
_RAW_LOG = None
_DAY = ""
_LAST_SUMMARY = 0.0
_COUNTERS = {"orders": 0, "deals": 0, "cancels": 0, "l2": 0, "quotes": 0}
_RAW_BUFFER = []
_RAW_FLUSH_TIMER = None
_RAW_DROPPED = 0
_AUCTION_RETRY_TIMER = None


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
    text = str(value)
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
    _COUNTERS["l2" if period in L2_PERIODS else "quotes"] += 1
    record = {"received_at": time.time(), "code": code, "period": period, "data": payload}
    try:
        line = json.dumps(record, ensure_ascii=True, default=_json_default) + "\n"
        if len(_RAW_BUFFER) >= 20000:
            _RAW_BUFFER.pop(0)
            _RAW_DROPPED += 1
        _RAW_BUFFER.append(line)
    except Exception:
        pass


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


def _paths():
    roots = []
    try:
        roots.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    roots.extend([os.getcwd(), os.path.dirname(os.getcwd()), r"C:\光大证券金阳光QMT实盘"])
    seen = set()
    for root in roots:
        if not root or root in seen:
            continue
        seen.add(root)
        yield os.path.join(root, "data", "manual_pool.csv")
        yield os.path.join(root, "manual_pool.csv")


def _load_pool():
    path = next((p for p in _paths() if os.path.isfile(p)), None)
    if not path:
        _log("[初始化] 未找到 data/manual_pool.csv")
        return []
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
    _log("[初始化] 股票池 %d 只: %s" % (len(result), ",".join(x["code"] for x in result)))
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
    old_order_id = inst.get("our_order_id")
    if old_order_id:
        inst["previous_order_ids"].add(str(old_order_id))
    inst["our_order_id"] = order_id
    inst["submission_pending"] = False
    inst["order_lookup_timer"] = None
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
    return {
        "id": str(_field(obj, "m_strOrderID", "m_nOrderID", "orderID", "order_id") or ""),
        "code": str(_field(obj, "m_strInstrumentID", "stockCode", "stock_code") or ""),
        "status": _field(obj, "m_nOrderStatus", "m_strOrderStatus", "status"),
        "total": _safe_float(_field(obj, "m_nVolumeTotalOriginal", "volume", "orderVolume")) or 0,
        "traded": _safe_float(_field(obj, "m_nVolumeTraded", "tradedVolume", "volumeTraded")) or 0,
        "price": _safe_float(_field(obj, "m_dPrice", "price")) or 0,
    }


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
        order_result = passorder(23, ORDER_TYPE_BUY, ACCOUNT_ID, inst["code"], PRICE_TYPE_LIMIT,
                                 price, qty, STRATEGY_NAME, 1, inst["submit_token"], {}, _C)
        inst["our_order_qty"] = qty
        if isinstance(order_result, str) and order_result not in ("", "-1"):
            _bind_order_id(inst, order_result)
        else:
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
        inst["last_tick"] = data
        event_time = _event_time(_field(data, "stime", "time", "timestamp"))
        if event_time:
            inst["last_event_time"] = event_time
        _write_raw(code, period, data)
        limit_up = _limit_price(inst, data)
        is_continuous = bool(event_time and _in_continuous(event_time))
        bid, bid_vol, ask, last, low = _quote_values(inst, data, track_day=is_continuous)
        if not limit_up or not bid:
            return
        if inst["last_close"] is None:
            inst["last_close"] = _safe_float(_field(data, "lastClose", "preClose", "pre_close"))
        if event_time and _in_auction(event_time):
            return
        if event_time and not _in_continuous(event_time):
            _check_validation(inst)
            return
        if _skip_low_gain(inst):
            return
        bid_at_limit = abs(bid - limit_up) < 1e-8
        seal_amount = _amount(bid, bid_vol)
        inst["last_tick"]["bid_at_limit"] = bid_at_limit
        if inst["state"] == STATE_WAIT_INITIAL:
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
    callback = (lambda data, c=code: _handle_l2order(c, data)) if period == "l2order" else (lambda data, c=code, p=period: _handle_quote(c, p, data))
    try:
        sub_id = C.subscribe_quote(code, period=period, dividend_type="follow", result_type="dict", callback=callback)
        if sub_id and sub_id > 0:
            _SUBSCRIPTIONS.append(sub_id)
            _INSTANCES[code]["sub_ids"].append(sub_id)
        else:
            _log("[订阅失败] %s %s 返回=%r" % (code, period, sub_id))
    except Exception as exc:
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


def _summary():
    by_state = {}
    for inst in _INSTANCES.values():
        display_state = inst["state"]
        if inst.get("our_order_id") and display_state not in (STATE_DONE, STATE_SKIPPED):
            display_state = "已下单，等待成交"
        by_state.setdefault(display_state, []).append(inst["code"])
    _log("[汇总] 监控=%d 下单=%d 成交=%d 撤单=%d L2=%d 行情=%d 状态=%s" %
         (len(_INSTANCES), _COUNTERS["orders"], _COUNTERS["deals"], _COUNTERS["cancels"],
          _COUNTERS["l2"], _COUNTERS["quotes"], ";".join("%s:%s" % (k, ",".join(v)) for k, v in sorted(by_state.items()))))


def init(C):
    global _C, _LOG, _RAW_LOG, _DAY, _LAST_SUMMARY, _RAW_FLUSH_TIMER, _RAW_DROPPED, _AUCTION_RETRY_TIMER
    with _LOCK:
        _C = C
        if ACCOUNT_ID:
            try:
                C.set_account(ACCOUNT_ID, ACCOUNT_TYPE)
            except Exception as exc:
                _log("[初始化] 交易账户注册失败 %r" % (exc,))
        _DAY = _dt.datetime.now().strftime("%Y%m%d")
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "userdata", "log")
        if not os.path.isdir(log_dir):
            log_dir = os.path.join(os.getcwd(), "userdata", "log")
        try:
            if not os.path.isdir(log_dir):
                os.makedirs(log_dir)
            _LOG = open(os.path.join(log_dir, "qmt_strategy_%s.log" % _DAY), "a")
            _RAW_LOG = open(os.path.join(log_dir, "qmt_strategy_raw_%s.jsonl" % _DAY), "a")
        except Exception:
            _LOG = None
            _RAW_LOG = None
        _COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0, "l2": 0, "quotes": 0})
        del _RAW_BUFFER[:]
        _RAW_FLUSH_TIMER = None
        _RAW_DROPPED = 0
        _AUCTION_RETRY_TIMER = None
        _INSTANCES.clear()
        _SUBSCRIPTIONS[:] = []
        for item in _load_pool():
            _INSTANCES[item["code"]] = _new_instance(item["code"], item["plan_amount"])
        _initial_quote_batch(C, list(_INSTANCES.values()))
        for inst in _INSTANCES.values():
            for period in PERIODS:
                _subscribe(C, inst["code"], period)
        _schedule_raw_flush()
        _schedule_auction_retry()
        _LAST_SUMMARY = _now()
        _log("[初始化] 原生模型启动，ENABLE_LIVE_ORDERS=%s" % ENABLE_LIVE_ORDERS)


def handlebar(C):
    global _LAST_SUMMARY
    with _LOCK:
        for inst in _INSTANCES.values():
            _check_validation(inst)
        if _now() - _LAST_SUMMARY >= 600:
            _summary()
            _LAST_SUMMARY = _now()


def _apply_order_update(inst, item):
    inst["our_order_status"] = item["status"]
    inst["our_traded_qty"] = item["traded"]
    if item["traded"] > 0:
        _COUNTERS["deals"] += 1
        _cancel_timer(inst)
        _set_state(inst, STATE_DONE, "订单已成交")
    elif str(item["status"]).lower() in ("5", "6", "canceled", "cancelled", "撤单", "废单"):
        if inst.get("validation_order_id") == item["id"]:
            inst["cancel_confirmed"] = True
            if item["traded"] == 0:
                _log("[撤单] %s 已完全撤单" % inst["code"])
        else:
            _set_state(inst, STATE_DONE, "订单结束")


def order_callback(ContextInfo, orderInfo):
    with _LOCK:
        item = _order_fields(orderInfo)
        for inst in _INSTANCES.values():
            if item["id"] and item["id"] == str(inst.get("our_order_id")):
                _apply_order_update(inst, item)
            elif inst.get("submission_pending") and item["code"] == inst["code"]:
                inst["pending_order_info"] = item


def deal_callback(ContextInfo, dealInfo):
    with _LOCK:
        item = _order_fields(dealInfo)
        for inst in _INSTANCES.values():
            if item["id"] and item["id"] == str(inst.get("our_order_id")):
                _COUNTERS["deals"] += 1
                inst["our_traded_qty"] += item["traded"] or 1
                _cancel_timer(inst)
                _set_state(inst, STATE_DONE, "成交回报")


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

