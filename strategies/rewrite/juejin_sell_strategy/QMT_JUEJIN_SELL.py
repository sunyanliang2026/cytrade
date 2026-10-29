# coding: gbk
"""Native QMT sell model for the Juejin tick strategy.

The model deliberately keeps the signal rules separate from QMT callbacks.
It uses l2quote as the canonical signal stream and ordinary tick data to keep
the session high/low/open values current. Live orders use the configured
QMT account by default.
"""

import copy
import datetime as _dt
import re
import time
import uuid


# ------------------------- operator configuration -------------------------
ENABLE_LIVE_ORDERS = True
ACCOUNT_ID = "32011276"
ACCOUNT_TYPE = "stock"
STRATEGY_NAME = "juejin_sell_native"
OP_TYPE_SELL = 24
ORDER_TYPE_LIMIT = 1101
PRICE_TYPE_LIMIT = 11
QUICK_TRADE = 2
POSITION_CACHE_MAX_AGE_SECONDS = 6.0
POSITION_REFRESH_INTERVAL_SECONDS = 2.0
PENDING_SIGNAL_MAX_AGE_SECONDS = 5.0

# This pool is the last QMT pool reviewed on 2026-10-23. Update both the
# date and rows before running the model on another trading day.
POOL_TRADE_DATE = "20261028"
SELL_POOL = (
    ("000810", 0, 2800),
    ("600715", 0, 13300),
    ("600826", 0, 2700),
    ("000678", 0, 1500),
    ("603390", 0, 2200),
)

LIMIT_UP_CONFIRM_AMOUNT = 120000000.0
LIMIT_UP_WEAK_AMOUNT = 100000000.0
LIMIT_UP_WEAK_REDUCE_AMOUNT = 25000000.0
LIMIT_UP_LARGE_AMOUNT = 160000000.0
LIMIT_UP_LARGE_REDUCE_AMOUNT = 80000000.0
LIMIT_DOWN_CLEAR_AMOUNT = 50000000.0
LIMIT_OPEN_GRACE_SECONDS = 60.0
LIMIT_OPEN_RECOVERY_SECONDS = 300.0
CANCEL_WAIT_SECONDS = 2.0
SUMMARY_INTERVAL_SECONDS = 300.0

FLAG_NORMAL = 0
FLAG_AUCTION_UNDER_EXPECTATION = 1
FLAG_WEAK = -3
FLAG_REBOUND = 5
FLAG_STRONG = 7
FLAG_LIMIT_UP = 10
FLAG_LIMIT_DOWN_CLEAR = 99
FLAG_SELL_DONE = -9
LIMIT_UP_FIRST_SELL_DONE = 99

ORDER_SUBMITTING = "SUBMITTING"
ORDER_ACTIVE = "ACTIVE"
ORDER_PARTIAL = "PARTIAL"
ORDER_CANCEL_REQUESTED = "CANCEL_REQUESTED"
ORDER_CANCELED = "CANCELED"
ORDER_FILLED = "FILLED"
ORDER_REJECTED = "REJECTED"
ORDER_UNKNOWN = "UNKNOWN"
ORDER_SIMULATED = "SIMULATED"

_FLAG_LABELS = {
    FLAG_NORMAL: "\u6b63\u5e38",
    FLAG_AUCTION_UNDER_EXPECTATION: "\u7ade\u4ef7\u4e0d\u53ca\u9884\u671f",
    FLAG_WEAK: "\u5f31\u52bf",
    FLAG_REBOUND: "\u53cd\u5f39",
    FLAG_STRONG: "\u5f3a\u52bf",
    FLAG_LIMIT_UP: "\u6da8\u505c",
    FLAG_LIMIT_DOWN_CLEAR: "\u8dcc\u505c\u6253\u5f00",
    FLAG_SELL_DONE: "\u5df2\u5356\u51fa",
}

_ORDER_STATUS_LABELS = {
    ORDER_SUBMITTING: "\u63d0\u4ea4\u4e2d",
    ORDER_ACTIVE: "\u6d3b\u52a8",
    ORDER_PARTIAL: "\u90e8\u5206\u6210\u4ea4",
    ORDER_CANCEL_REQUESTED: "\u64a4\u5355\u4e2d",
    ORDER_CANCELED: "\u5df2\u64a4\u5355",
    ORDER_FILLED: "\u5df2\u6210\u4ea4",
    ORDER_REJECTED: "\u5df2\u62d2\u7edd",
    ORDER_UNKNOWN: "\u672a\u77e5",
    ORDER_SIMULATED: "\u6a21\u62df",
}

_ACTION_LABELS = {
    "auction_under_expectation_primary": "\u7ade\u4ef7\u4e0d\u53ca\u9884\u671f",
    "early_under_expectation": "\u65e9\u76d8\u4e0d\u53ca\u9884\u671f",
    "weak_rebound_primary": "\u5f31\u52bf\u53cd\u5f39\u5931\u8d25",
    "limit_down_clear": "\u8dcc\u505c\u6253\u5f00",
    "flag7_break7_after_high": "\u5f3a\u52bf\u9ad8\u4f4d\u8dcc\u78347%",
    "flag7_high_pullback_25": "\u5f3a\u52bf\u9ad8\u70b9\u56de\u64a25%",
    "flag7_morning_take_profit": "\u5f3a\u52bf\u65e9\u76d8\u6b62\u76c8",
    "limit_open_first_sell": "\u6da8\u505c\u5f00\u677f\u9996\u6b21\u5356\u51fa",
    "limit_open_second_sell": "\u6da8\u505c\u5f00\u677f\u4e8c\u6b21\u5356\u51fa",
}

PERIODS = ("tick", "l2quote")


# ------------------------------- model state ------------------------------
class _NoopLock(object):
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
_LAST_SUMMARY = 0.0
_POSITION_CACHE = {"updated_at": 0.0, "available": {}}
_POSITION_REFRESH_TIMER = None


def _now():
    return time.monotonic()


def _safe_float(value):
    try:
        number = float(value)
        if number == number and number > 0 and number < float("inf"):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return 0.0


def _field(source, *names):
    if isinstance(source, dict):
        for name in names:
            if name in source and source.get(name) not in (None, ""):
                return source.get(name)
    else:
        for name in names:
            try:
                value = getattr(source, name)
            except Exception:
                continue
            if value not in (None, ""):
                return value
    return None


def _first_value(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _stock_code(value):
    text = str(value or "").strip().upper()
    if "." in text:
        left, right = text.split(".", 1)
        if len(left) == 6 and left.isdigit():
            digits = left
        elif len(right) == 6 and right.isdigit():
            digits = right
        else:
            digits = "".join(ch for ch in text if ch.isdigit())
    else:
        digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return ""
    if digits.startswith(("5", "6", "9")):
        market = "SH"
    elif digits.startswith(("8", "4", "92")):
        market = "BJ"
    else:
        market = "SZ"
    return digits + "." + market


def _event_seconds(value=None):
    if value is None:
        return None
    text = str(value).strip()
    integer, dot, fraction = text.partition(".")
    digits = "".join(ch for ch in integer if ch.isdigit())
    fraction = "".join(ch for ch in fraction if ch.isdigit()) if dot else ""
    if len(digits) == 13:
        try:
            stamp = _dt.datetime.fromtimestamp(int(digits) / 1000.0)
            return stamp.hour * 3600.0 + stamp.minute * 60.0 + stamp.second + stamp.microsecond / 1000000.0
        except (ValueError, OSError, OverflowError):
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
        hour = int(hhmmss[:2])
        minute = int(hhmmss[2:4])
        second = int(hhmmss[4:6])
        if hour > 23 or minute > 59 or second > 59:
            return None
        milliseconds = int((extra + "000")[:3]) if extra else 0
        return hour * 3600.0 + minute * 60.0 + second + milliseconds / 1000.0
    except (TypeError, ValueError):
        return None


def _event_time(value=None):
    seconds = _event_seconds(value)
    if seconds is None:
        return ""
    hour = int(seconds // 3600)
    minute = int((seconds - hour * 3600) // 60)
    second = int(seconds - hour * 3600 - minute * 60)
    return "%02d%02d%02d" % (hour, minute, second)


def _in_auction(seconds):
    return seconds is not None and 9 * 3600 + 15 * 60 <= seconds < 9 * 3600 + 25 * 60


def _in_regular_session(seconds):
    return seconds is not None and 9 * 3600 + 26 * 60 <= seconds <= 14 * 3600 + 57 * 60


_STOCK_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(\d{6}\.(?:SH|SZ|BJ))(?![A-Za-z0-9_])", re.IGNORECASE
)


def _stock_label(code, inst=None):
    normalized_code = _stock_code(code)
    if inst is None:
        inst = _INSTANCES.get(normalized_code, {})
    nick = str(inst.get("nick") or "").strip()
    if not nick or nick == normalized_code:
        nick = "\u672a\u77e5"
    return "%s(%s)" % (normalized_code, nick)


def _log(message):
    message = str(message)

    def add_stock_name(match):
        code = match.group(1)
        if message[match.end():].startswith("("):
            return code
        return _stock_label(code)

    message = _STOCK_CODE_RE.sub(add_stock_name, message)
    line = "%s %s" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], message)
    try:
        print(line)
    except Exception:
        pass


def _owned_payload(code, payload):
    try:
        if isinstance(payload, dict) and code in payload:
            payload = payload.get(code)
        return copy.deepcopy(payload if isinstance(payload, dict) else {})
    except Exception:
        return dict(payload) if isinstance(payload, dict) else {}


def _price_equal(left, right):
    return bool(left and right and abs(float(left) - float(right)) <= 0.005)


def _limit_ratio(code):
    digits = str(code or "").split(".", 1)[0]
    if digits.startswith(("300", "301", "688", "689")):
        return 0.20
    if digits.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def _limit_from(data, up=True):
    names = (
        ("upLimit", "up_limit", "upperLimit", "limitUp", "upLimitPrice",
         "upStopPrice", "UpStopPrice", "\u6da8\u505c\u4ef7")
        if up else
        ("downLimit", "down_limit", "lowerLimit", "limitDown", "downLimitPrice",
         "downStopPrice", "DownStopPrice", "\u8dcc\u505c\u4ef7")
    )
    return _safe_float(_first_value(_field(data, *names)))


def _quote_values(data):
    raw_bid = _first_value(_field(data, "bidPrice", "bid", "bid1", "bidPrice1"))
    raw_bid_volume = _first_value(_field(data, "bidVol", "bidVolume", "bid1_volume", "bidVolume1"))
    raw_ask = _first_value(_field(data, "askPrice", "ask", "ask1", "askPrice1"))
    raw_ask_volume = _first_value(_field(data, "askVol", "askVolume", "ask1_volume", "askVolume1"))
    return {
        "bid": _safe_float(raw_bid),
        "bid_volume": int(_safe_float(raw_bid_volume)),
        "ask": _safe_float(raw_ask),
        "ask_volume": int(_safe_float(raw_ask_volume)),
        "last": _safe_float(_first_value(_field(data, "lastPrice", "last", "price", "close"))),
        "open": _safe_float(_first_value(_field(data, "open", "openPrice", "Open"))),
        "high": _safe_float(_first_value(_field(data, "high", "highest", "highPrice", "High"))),
        "low": _safe_float(_first_value(_field(data, "low", "lowest", "lowPrice", "Low"))),
        "pre_close": _safe_float(_first_value(_field(
            data, "lastClose", "preClose", "pre_close", "PreClose", "LastClose"
        ))),
        "limit_up": _limit_from(data, up=True),
        "limit_down": _limit_from(data, up=False),
    }


def _new_instance(plan):
    return {
        "code": plan["code"],
        "nick": str(plan.get("nick") or plan["code"]),
        "exp": plan["exp"],
        "sell_quantity": plan["sell_quantity"],
        "flag": FLAG_NORMAL,
        "previous_flag": FLAG_NORMAL,
        "open_flag": 0,
        "up_sell": 0,
        "limit_open_second_sell_done": False,
        "pre_bid": 0.0,
        "pre_up_amount": 0.0,
        "limit_up_seen_at": None,
        "limit_open_started_at": None,
        "pre_close": 0.0,
        "limit_up": 0.0,
        "limit_down": 0.0,
        "open_price": 0.0,
        "day_high": 0.0,
        "day_low": 0.0,
        "last_event_time": "",
        "last_quote": {},
        "last_l2_quote": {},
        "last_l2_event_time": "",
        "last_l2_at": 0.0,
        "subscription_ids": {},
        "subscription_tokens": {},
        "disabled_subscription_periods": set(),
        "submitted_actions": set(),
        "orders": [],
        "pending_after_cancel": None,
        "flag7_condition_state": None,
        "seen_deal_ids": set(),
    }


def _set_flag(inst, flag, reason):
    if inst["flag"] == flag:
        return
    old = inst["flag"]
    inst["flag"] = flag
    _log("[JUEJIN_STATUS] code=%s nick=%s status=%s->%s reason=%s" % (
        inst["code"], inst["nick"], old, flag, reason
    ))


def _update_market(inst, data, event_time):
    values = _quote_values(data)
    if values["pre_close"] > 0:
        inst["pre_close"] = values["pre_close"]
    if values["limit_up"] > 0:
        inst["limit_up"] = values["limit_up"]
    if values["limit_down"] > 0:
        inst["limit_down"] = values["limit_down"]
    if inst["pre_close"] > 0 and inst["limit_up"] <= 0:
        inst["limit_up"] = round(inst["pre_close"] * (1.0 + _limit_ratio(inst["code"])), 2)
    if inst["pre_close"] > 0 and inst["limit_down"] <= 0:
        inst["limit_down"] = round(inst["pre_close"] * (1.0 - _limit_ratio(inst["code"])), 2)
    if values["open"] > 0 and inst["open_price"] <= 0:
        inst["open_price"] = values["open"]
    candidates_high = [values["high"], values["last"], values["bid"]]
    candidates_low = [values["low"], values["last"], values["bid"]]
    for value in candidates_high:
        if value > 0:
            inst["day_high"] = max(inst["day_high"], value)
    positive_lows = [value for value in candidates_low if value > 0]
    if positive_lows:
        inst["day_low"] = min(positive_lows) if inst["day_low"] <= 0 else min(inst["day_low"], min(positive_lows))
    if event_time:
        inst["last_event_time"] = event_time
    inst["last_quote"] = values
    return values


def _book_amount(price, volume):
    return float(price or 0.0) * float(volume or 0) * 100.0


def _is_order_active(order):
    return order.get("status") in (
        ORDER_SUBMITTING, ORDER_ACTIVE, ORDER_PARTIAL, ORDER_CANCEL_REQUESTED,
        ORDER_UNKNOWN,
    )


def _has_active_orders(inst):
    return any(_is_order_active(order) for order in inst["orders"])


def _action_known(inst, action):
    if action in inst["submitted_actions"]:
        return True
    pending = inst.get("pending_after_cancel") or {}
    return any(item.get("action") == action for item in pending.get("requests", []))


def _position_records():
    getter = globals().get("get_trade_detail_data")
    if not callable(getter):
        return None
    try:
        records = getter(ACCOUNT_ID, ACCOUNT_TYPE, "POSITION")
    except Exception as exc:
        _log("[POSITION_QUERY_FAILED] error=%r" % (exc,))
        return None
    if records is None:
        _log("[POSITION_QUERY_FAILED] error=empty_result")
        return None
    if isinstance(records, dict):
        return list(records.values())
    if isinstance(records, (list, tuple)):
        return list(records)
    return [records]


def _refresh_positions():
    if not ENABLE_LIVE_ORDERS:
        return True
    records = _position_records()
    if records is None:
        _POSITION_CACHE["updated_at"] = 0.0
        return False
    available_by_code = {}
    for record in records:
        code = _stock_code(_field(
            record, "m_strInstrumentID", "m_strOrderCode", "stockCode", "stock_code", "code"
        ))
        if code not in _INSTANCES:
            continue
        available = _field(
            record,
            "m_nCanUseVolume", "can_use_volume", "canUseVolume",
            "available_quantity", "m_nVolumeAvailable", "m_nAvailable",
        )
        if available is None:
            _log("[POSITION_QUERY_FAILED] code=%s reason=available_volume_missing" % code)
            _POSITION_CACHE["updated_at"] = 0.0
            return False
        available_by_code[code] = max(0, int(_safe_float(available)))
    if available_by_code != _POSITION_CACHE["available"]:
        _log("[POSITION_CACHE] available=%s" % available_by_code)
    _POSITION_CACHE["available"] = available_by_code
    _POSITION_CACHE["updated_at"] = _now()
    _sync_market_subscriptions(_C)
    return True


def _available_for_sell(inst):
    available = _account_available_for_sell(inst)
    if available is None:
        _log("[SELL_SKIP] code=%s reason=position_cache_unavailable" % inst["code"])
        return None
    return min(available, _remaining_target(inst))


def _remaining_target(inst):
    already_sold = sum(order.get("traded_quantity", 0) for order in inst["orders"])
    return max(0, inst["sell_quantity"] - already_sold)


def _account_available_for_sell(inst):
    if not ENABLE_LIVE_ORDERS:
        return _remaining_target(inst)
    if not _position_cache_valid():
        return None
    return max(0, _POSITION_CACHE["available"].get(inst["code"], 0))


def _position_cache_valid():
    return bool(_POSITION_CACHE["updated_at"]) and (
        _now() - _POSITION_CACHE["updated_at"] <= POSITION_CACHE_MAX_AGE_SECONDS
    )


def _order_record(inst, action, price, quantity, token):
    return {
        "action": action,
        "price": float(price),
        "quantity": int(quantity),
        "traded_quantity": 0,
        "order_reported_traded_quantity": 0,
        "deal_traded_quantity": 0,
        "token": token,
        "order_id": "",
        "status": ORDER_SUBMITTING,
        "status_raw": "",
        "created_at": _now(),
        "cancel_requested_at": None,
        "pending_update": None,
    }


def _order_matches(record, item):
    if item.get("code") and item["code"] != record.get("code"):
        return False
    if item.get("order_id") and record.get("order_id"):
        return item["order_id"] == record["order_id"]
    if item.get("token") and record.get("token"):
        return item["token"] == record["token"]
    if record.get("status") not in (ORDER_SUBMITTING, ORDER_ACTIVE, ORDER_PARTIAL, ORDER_CANCEL_REQUESTED, ORDER_UNKNOWN):
        return False
    if item.get("code") != record.get("code", item.get("code")):
        return False
    if item.get("price", 0) > 0 and abs(item["price"] - record["price"]) > 0.005:
        return False
    if item.get("quantity", 0) > 0 and int(item["quantity"]) != record["quantity"]:
        return False
    return True


def _callback_identity(info):
    token = str(_field(
        info, "m_strRemark", "m_strUserOrderID", "m_strUserOrderId", "userOrderId",
        "user_order_id", "order_remark", "orderRemark", "remark"
    ) or "").strip()
    order_id = str(_field(
        info, "m_strOrderID", "m_nOrderID", "m_strOrderSysID", "orderID", "order_id", "orderId"
    ) or "").strip()
    return token, order_id


def _order_fields(info):
    token, order_id = _callback_identity(info)
    code = str(_field(
        info, "m_strInstrumentID", "stockCode", "stock_code", "code"
    ) or "").strip().upper()
    exchange = str(_field(
        info, "m_strExchangeID", "exchangeID", "exchange_id"
    ) or "").strip().upper()
    if code and "." not in code and exchange:
        exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange, exchange)
        code = "%s.%s" % (code, exchange)
    return {
        "token": token,
        "order_id": order_id,
        "code": _stock_code(code),
        "deal_id": str(_field(
            info, "m_strTradeID", "m_strDealID", "tradeID", "dealID", "tradedID",
            "traded_id"
        ) or "").strip(),
        "status": _field(info, "m_nOrderStatus", "m_strOrderStatus", "status", "order_status"),
        "message": _field(info, "m_strStatusMsg", "m_strErrorMsg", "status_msg", "error_msg", "message"),
        "quantity": int(_safe_float(_field(
            info, "m_nVolumeTotalOriginal", "m_nVolumeTotal", "orderVolume",
            "order_volume", "totalVolume", "quantity"
        ))),
        "traded": int(_safe_float(_field(
            info, "m_nVolumeTraded", "tradedVolume", "traded_volume",
            "volumeTraded", "deal_volume"
        ))),
        "deal_volume": int(_safe_float(_field(
            info, "m_nVolume", "m_nTradeVolume", "deal_volume", "m_nVolumeTraded",
            "tradedVolume", "traded_volume", "volume"
        ))),
        "price": _safe_float(_field(
            info, "m_dPrice", "price", "order_price", "traded_price"
        )),
    }


def _find_order(inst, item):
    exact = [order for order in inst["orders"] if _order_matches(order, item)]
    if len(exact) == 1:
        return exact[0]
    return None


def _is_cancel_status(status, message):
    value = str(status or "").lower()
    text = str(message or "").lower()
    return value in (
        "5", "6", "53", "54", "canceled", "cancelled", "\u64a4\u5355", "\u5e9f\u5355"
    ) or any(
        marker in text for marker in ("\u64a4\u5355", "\u5e9f\u5355", "cancel", "cancelled")
    )


def _is_rejected_status(status, message):
    value = str(status or "").lower()
    text = str(message or "").lower()
    return value in ("57", "rejected", "reject", "junk", "invalid", "error") or any(
        marker in text for marker in ("\u62d2", "\u5e9f", "\u5931\u8d25", "\u4e0d\u8db3", "reject", "invalid", "error")
    )


def _is_cancel_pending_status(status):
    return str(status or "").lower() in (
        "51", "52", "reported_cancel", "partsucc_cancel", "cancel_pending"
    )


def _apply_order_update(inst, order, item):
    if item.get("order_id") and not order.get("order_id"):
        order["order_id"] = item["order_id"]
    if item.get("traded", 0) > order.get("order_reported_traded_quantity", 0):
        order["order_reported_traded_quantity"] = item["traded"]
    order["traded_quantity"] = max(
        order.get("order_reported_traded_quantity", 0),
        order.get("deal_traded_quantity", 0),
    )
    order["status_raw"] = item.get("status")
    status = str(item.get("status") or "").lower()
    if (
        order.get("traded_quantity", 0) >= order["quantity"] and order["quantity"] > 0
    ) or status in ("56", "succeeded", "filled"):
        order["status"] = ORDER_FILLED
    elif _is_rejected_status(item.get("status"), item.get("message")):
        order["status"] = ORDER_REJECTED
    elif _is_cancel_pending_status(item.get("status")):
        order["status"] = ORDER_CANCEL_REQUESTED
    elif _is_cancel_status(item.get("status"), item.get("message")):
        order["status"] = ORDER_CANCELED
    elif order.get("traded_quantity", 0) > 0:
        order["status"] = ORDER_PARTIAL
    else:
        order["status"] = ORDER_ACTIVE
    _log("[ORDER_UPDATE] code=%s action=%s id=%s status=%s traded=%s/%s message=%s" % (
        inst["code"], order["action"], order.get("order_id", ""), order["status"],
        order.get("traded_quantity", 0), order["quantity"], item.get("message", "")
    ))
    if order["status"] == ORDER_CANCELED:
        _drain_pending_after_cancel(inst)


def _bind_order_id(inst, order, order_id):
    order_id = str(order_id or "")
    if not order_id or order_id in ("0", "-1"):
        return False
    order["order_id"] = order_id
    if order["status"] == ORDER_SUBMITTING:
        order["status"] = ORDER_ACTIVE
    pending = order.pop("pending_update", None)
    if pending:
        _apply_order_update(inst, order, pending)
    pending_deals = order.pop("pending_deal_infos", [])
    for pending_deal in pending_deals:
        if pending_deal.get("token") == order.get("token") or not pending_deal.get("token"):
            _apply_deal_update(inst, order, pending_deal)
        else:
            inst.setdefault("pending_deal_infos", []).append(pending_deal)
    return True


def _apply_deal_update(inst, order, item):
    deal_id = str(item.get("deal_id") or "").strip()
    if deal_id and deal_id in inst["seen_deal_ids"]:
        _log("[DEAL_DUPLICATE] code=%s action=%s deal_id=%s" % (
            inst["code"], order["action"], deal_id
        ))
        return
    if deal_id:
        inst["seen_deal_ids"].add(deal_id)
    volume = int(item.get("deal_volume", 0) or 0)
    if volume <= 0:
        _log("[DEAL_INVALID] code=%s action=%s deal_id=%s volume=%s" % (
            inst["code"], order["action"], deal_id, volume
        ))
        return
    order["deal_traded_quantity"] = order.get("deal_traded_quantity", 0) + volume
    order["traded_quantity"] = max(
        order.get("order_reported_traded_quantity", 0),
        order.get("deal_traded_quantity", 0),
    )
    if order["traded_quantity"] >= order["quantity"]:
        order["status"] = ORDER_FILLED
    else:
        order["status"] = ORDER_PARTIAL
    _log("[DEAL] code=%s action=%s id=%s deal_id=%s traded=%s/%s" % (
        inst["code"], order["action"], order.get("order_id", ""), deal_id,
        order["traded_quantity"], order["quantity"]
    ))


def _schedule_order_lookup(inst, order):
    if _C is None:
        return
    scheduler = getattr(_C, "schedule_run", None)
    if not callable(scheduler):
        return
    token = order["token"]
    code = inst["code"]
    try:
        order["lookup_timer"] = scheduler(
            lambda ContextInfo, c=code, t=token: _resolve_order_id(c, t),
            _dt.datetime.now() + _dt.timedelta(seconds=8),
            repeat_times=0,
            name="juejin_sell_order_lookup_%s" % code,
        )
    except Exception as exc:
        _log("[ORDER_LOOKUP_FAILED] code=%s error=%r" % (code, exc))


def _resolve_order_id(code, token):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if not inst:
            return
        order = next((item for item in inst["orders"] if item.get("token") == token), None)
        if not order or order.get("status") != ORDER_SUBMITTING:
            return
        order["lookup_timer"] = None
        order["status"] = ORDER_UNKNOWN
        _log("[ORDER_UNKNOWN] code=%s action=%s token=%s reason=callback_not_received" % (
            code, order["action"], token
        ))


def _submit_sell(inst, quantity, price, reason, action, available_override=None,
                 allow_parallel=False):
    if _action_known(inst, action):
        return None
    if ENABLE_LIVE_ORDERS and _has_active_orders(inst) and not allow_parallel:
        _log("[SELL_SKIP] code=%s action=%s reason=existing_order_unresolved" % (inst["code"], action))
        return None
    available = (
        _available_for_sell(inst)
        if available_override is None
        else max(0, int(available_override))
    )
    if available is None:
        return None
    requested = available if quantity is None else max(0, int(quantity))
    sell_quantity = min(requested, available)
    if sell_quantity <= 0:
        inst["submitted_actions"].add(action)
        _log("[SELL_SKIP] code=%s action=%s reason=no_available_volume" % (inst["code"], action))
        return None
    if price <= 0:
        _log("[SELL_SKIP] code=%s action=%s reason=invalid_price" % (inst["code"], action))
        return None

    token = "%s_%s_%s" % (inst["code"], action, uuid.uuid4().hex[:8])
    order = _order_record(inst, action, price, sell_quantity, token)
    order["code"] = inst["code"]
    inst["orders"].append(order)
    # Once a live submission has been attempted, block the same action for
    # this session even when QMT returns an unknown result. Retrying blindly
    # could create a duplicate sell order.
    inst["submitted_actions"].add(action)
    sender = globals().get("passorder")
    if not ENABLE_LIVE_ORDERS:
        order["status"] = ORDER_SIMULATED
        _log("[SELL_DRY_RUN] code=%s action=%s qty=%d price=%.3f reason=%s" % (
            inst["code"], action, sell_quantity, price, reason
        ))
        return order
    if not ACCOUNT_ID or not callable(sender):
        order["status"] = ORDER_UNKNOWN
        _log("[SELL_UNKNOWN] code=%s action=%s reason=account_or_passorder_unavailable" % (
            inst["code"], action
        ))
        return None
    try:
        result = sender(
            OP_TYPE_SELL, ORDER_TYPE_LIMIT, ACCOUNT_ID, inst["code"], PRICE_TYPE_LIMIT,
            float(price), sell_quantity, STRATEGY_NAME, QUICK_TRADE, token, _C
        )
    except Exception as exc:
        order["status"] = ORDER_UNKNOWN
        _log("[SELL_UNKNOWN] code=%s action=%s error=%r" % (inst["code"], action, exc))
        return None
    if result is False or (isinstance(result, (int, float)) and result < 0) or str(result) == "-1":
        order["status"] = ORDER_REJECTED
        _log("[SELL_REJECTED] code=%s action=%s return=%r" % (inst["code"], action, result))
        return None
    if str(result) not in ("", "0"):
        _bind_order_id(inst, order, result)
    else:
        _schedule_order_lookup(inst, order)
    _log("[SELL_SUBMIT] code=%s action=%s qty=%d price=%.3f reason=%s return=%r" % (
        inst["code"], action, sell_quantity, price, reason, result
    ))
    return order


def _cancel_active_orders(inst, reason):
    cancel_fn = globals().get("cancel")
    canceled = 0
    for order in inst["orders"]:
        if not _is_order_active(order) or order["status"] == ORDER_CANCEL_REQUESTED:
            continue
        order["cancel_requested_at"] = _now()
        order["status"] = ORDER_CANCEL_REQUESTED
        if not ENABLE_LIVE_ORDERS:
            order["status"] = ORDER_CANCELED
            canceled += 1
            continue
        if not order.get("order_id") or not callable(cancel_fn):
            _log("[CANCEL_WAIT] code=%s action=%s reason=order_id_or_cancel_unavailable" % (
                inst["code"], order["action"]
            ))
            continue
        try:
            result = cancel_fn(order["order_id"], ACCOUNT_ID, ACCOUNT_TYPE, _C)
            canceled += 1
            _log("[CANCEL_REQUEST] code=%s action=%s id=%s reason=%s return=%r" % (
                inst["code"], order["action"], order["order_id"], reason, result
            ))
        except Exception as exc:
            _log("[CANCEL_FAILED] code=%s action=%s id=%s error=%r" % (
                inst["code"], order["action"], order.get("order_id", ""), exc
            ))
    if _has_active_orders(inst):
        pending = inst.get("pending_after_cancel")
        if pending:
            pending["deadline"] = _now() + CANCEL_WAIT_SECONDS
    return canceled


def _queue_after_cancel(inst, requests, reason, next_flag=None, next_reason="", set_exp=None):
    if not requests:
        return
    if any(_action_known(inst, item["action"]) for item in requests):
        return
    had_active_orders = _has_active_orders(inst)
    inst["pending_after_cancel"] = {
        "requests": list(requests),
        "reason": reason,
        "created_at": _now(),
        "expires_at": _now() + PENDING_SIGNAL_MAX_AGE_SECONDS,
        "deadline": _now() + CANCEL_WAIT_SECONDS if had_active_orders else None,
        "next_flag": next_flag,
        "next_reason": next_reason,
        "set_exp": set_exp,
    }
    _cancel_active_orders(inst, reason)
    _drain_pending_after_cancel(inst)


def _pending_request_valid(inst, request):
    quote = inst["last_l2_quote"]
    bid = quote.get("bid", 0)
    if bid <= 0 or _now() - inst["last_l2_at"] > PENDING_SIGNAL_MAX_AGE_SECONDS:
        return False
    pre_close = inst["pre_close"]
    action = request["action"]
    seconds = _event_seconds(inst["last_l2_event_time"])
    if action == "auction_under_expectation_primary":
        return (seconds is not None and 9 * 3600 + 24 * 60 + 56 <= seconds < 9 * 3600 + 25 * 60
                and inst["exp"] == 1 and pre_close * 0.94 < bid < pre_close * 0.995)
    if seconds is None or not _in_regular_session(seconds):
        return False
    if action == "early_under_expectation":
        return (inst["exp"] == 1 and seconds is not None and seconds < 9 * 3600 + 31 * 60
                and pre_close * 0.94 < bid < pre_close * 0.98)
    if action == "weak_rebound_primary":
        return inst["flag"] == FLAG_WEAK and bid < pre_close * 1.02 and inst["day_low"] < pre_close * 0.97
    if action == "limit_down_clear":
        return (_price_equal(quote.get("ask", 0), inst["limit_down"])
                and _book_amount(quote.get("ask", 0), quote.get("ask_volume", 0)) > LIMIT_DOWN_CLEAR_AMOUNT)
    if action == "flag5_high_pullback":
        return inst["flag"] == FLAG_REBOUND and bid < pre_close * 1.05 and inst["day_high"] > pre_close * 1.065
    if action == "flag5_break3":
        return inst["flag"] == FLAG_REBOUND and bid < pre_close * 1.03
    if action == "flag7_high_pullback_25":
        return inst["flag"] == FLAG_STRONG and bid < inst["day_high"] * 0.975
    if action == "flag7_break7_after_high":
        return inst["flag"] == FLAG_STRONG and inst["day_high"] > pre_close * 1.085 and bid < pre_close * 1.07
    if action == "flag7_morning_take_profit":
        return inst["flag"] == FLAG_STRONG and bid < pre_close * 1.065 and seconds is not None and seconds < 9 * 3600 + 31 * 60
    if action == "limit_open_first_sell":
        amount = _book_amount(bid, quote.get("bid_volume", 0))
        previous_amount = inst["pre_up_amount"]
        return (inst["flag"] == FLAG_LIMIT_UP and
                (bid < inst["limit_up"] or
                 (_price_equal(bid, inst["limit_up"]) and
                  ((amount < LIMIT_UP_WEAK_AMOUNT and previous_amount - amount > LIMIT_UP_WEAK_REDUCE_AMOUNT) or
                   (amount < LIMIT_UP_LARGE_AMOUNT and previous_amount - amount > LIMIT_UP_LARGE_REDUCE_AMOUNT)))))
    if action == "limit_open_second_sell":
        return inst["up_sell"] == LIMIT_UP_FIRST_SELL_DONE and bid < pre_close * 1.03
    return False


def _pending_price(inst, request):
    bid = inst["last_l2_quote"].get("bid", 0)
    action = request["action"]
    if action in ("auction_under_expectation_primary", "limit_down_clear"):
        return inst["limit_down"]
    if action == "flag7_break7_after_high":
        return round(bid * 0.985, 2) + 0.01
    if action == "limit_open_first_sell":
        return round(bid * 0.982, 2)
    return round(bid * 0.99, 2)


def _drain_pending_after_cancel(inst):
    pending = inst.get("pending_after_cancel")
    if not pending:
        return
    if _now() > pending["expires_at"]:
        _log("[PENDING_SELL_ABORTED] code=%s reason=signal_expired" % inst["code"])
        inst["pending_after_cancel"] = None
        return
    if _has_active_orders(inst):
        return
    if pending.get("deadline") and pending["deadline"] < _now():
        _log("[PENDING_SELL_ABORTED] code=%s reason=cancel_timeout" % inst["code"])
        inst["pending_after_cancel"] = None
        return
    if ENABLE_LIVE_ORDERS and not _position_cache_valid():
        return
    delayed = _now() - pending["created_at"] > 0.1
    if delayed and not all(_pending_request_valid(inst, request) for request in pending["requests"]):
        _log("[PENDING_SELL_ABORTED] code=%s reason=signal_no_longer_valid" % inst["code"])
        inst["pending_after_cancel"] = None
        return
    if _remaining_target(inst) <= 0:
        _log("[PENDING_SELL_ABORTED] code=%s reason=target_complete" % inst["code"])
        inst["pending_after_cancel"] = None
        return
    inst["pending_after_cancel"] = None
    accepted = False
    for request in pending["requests"]:
        if request.get("split_account_available"):
            account_available = _account_available_for_sell(inst)
            if account_available is None:
                inst["pending_after_cancel"] = pending
                return
            primary_quantity = min(_remaining_target(inst), account_available)
            remainder_quantity = max(0, account_available - primary_quantity)
            primary_order = _submit_sell(
                inst,
                primary_quantity,
                _pending_price(inst, request) if delayed else request["price"],
                request["reason"],
                request["action"],
                available_override=primary_quantity,
            )
            if primary_order is not None:
                accepted = True
            if remainder_quantity > 0:
                remainder_order = _submit_sell(
                    inst,
                    remainder_quantity,
                    request["remainder_price"],
                    "weak_rebound_failure_remainder",
                    "weak_rebound_remainder",
                    available_override=remainder_quantity,
                    allow_parallel=True,
                )
                if remainder_order is not None:
                    accepted = True
            else:
                inst["submitted_actions"].add("weak_rebound_remainder")
                _log("[WEAK_REBOUND_SPLIT] code=%s account_available=%d primary=%d remainder=0" % (
                    inst["code"], account_available, primary_quantity
                ))
            if remainder_quantity > 0:
                _log("[WEAK_REBOUND_SPLIT] code=%s account_available=%d primary=%d remainder=%d" % (
                    inst["code"], account_available, primary_quantity, remainder_quantity
                ))
            continue
        order = _submit_sell(
            inst,
            request.get("quantity"),
            _pending_price(inst, request) if delayed else request["price"],
            request["reason"],
            request["action"],
        )
        if order is not None:
            accepted = True
            if request.get("set_second"):
                inst["limit_open_second_sell_done"] = True
            if request.get("set_up_sell"):
                inst["up_sell"] = LIMIT_UP_FIRST_SELL_DONE
                _log("[LIMIT_OPEN_FIRST_SELL] code=%s" % inst["code"])
    if accepted and pending.get("set_exp") is not None:
        inst["exp"] = pending["set_exp"]
    if accepted and pending.get("next_flag") is not None:
        _set_flag(inst, pending["next_flag"], pending.get("next_reason", ""))


def _handle_auction(inst, quote, seconds):
    if not (
        seconds is not None
        and 9 * 3600 + 24 * 60 + 56 <= seconds < 9 * 3600 + 25 * 60
        and quote["bid"] > inst["pre_close"] * 0.94
        and quote["bid"] < inst["pre_close"] * 0.995
        and inst["exp"] == 1
        and inst["flag"] == FLAG_NORMAL
    ):
        return
    request = {
        "quantity": inst["sell_quantity"], "price": inst["limit_down"],
        "reason": "auction_under_expectation_primary", "action": "auction_under_expectation_primary",
    }
    _queue_after_cancel(inst, [request], "auction_under_expectation_cancel",
                        next_flag=FLAG_AUCTION_UNDER_EXPECTATION, next_reason="auction_under_expectation")


def _handle_early_under_expectation(inst, quote, seconds):
    if not (
        inst["exp"] == 1
        and quote["bid"] > inst["pre_close"] * 0.94
        and quote["bid"] < inst["pre_close"] * 0.98
        and seconds is not None
        and seconds < 9 * 3600 + 31 * 60
    ):
        return
    request = {
        "quantity": inst["sell_quantity"],
        "price": round(quote["bid"] * 0.99, 2),
        "reason": "early_under_expectation",
        "action": "early_under_expectation",
    }
    _queue_after_cancel(
        inst, [request], "early_under_expectation_cancel",
        set_exp=-1,
    )


def _confirm_limit_up(inst, quote):
    at_limit_up = _price_equal(quote["bid"], inst["limit_up"])
    can_confirm = inst["flag"] not in (
        FLAG_LIMIT_UP, FLAG_LIMIT_DOWN_CLEAR, FLAG_SELL_DONE
    )
    if at_limit_up and can_confirm:
        inst["limit_up_seen_at"] = _now()
    amount = _book_amount(quote["bid"], quote["bid_volume"])
    if (
        at_limit_up
        and amount > LIMIT_UP_CONFIRM_AMOUNT
        and can_confirm
        and _remaining_target(inst) > 0
    ):
        _set_flag(inst, FLAG_LIMIT_UP, "limit_up_sealed_amount=%.0f" % amount)
        inst["open_flag"] = 0
        _log("[LIMIT_UP_CONFIRMED] code=%s bid=%.3f bid_volume=%d amount=%.0f" % (
            inst["code"], quote["bid"], quote["bid_volume"], amount
        ))


def _mark_weak_state(inst, quote):
    if quote["bid"] < inst["pre_close"] * 0.975 and inst["flag"] not in (
        FLAG_LIMIT_DOWN_CLEAR, FLAG_WEAK, FLAG_SELL_DONE, 66
    ):
        _set_flag(inst, FLAG_WEAK, "bid_below_minus_2_5_percent")


def _handle_weak_rebound_failure(inst, quote, seconds):
    if not (
        inst["flag"] == FLAG_WEAK
        and inst["pre_bid"] >= inst["pre_close"] * 1.02
        and quote["bid"] < inst["pre_close"] * 1.02
        and inst["day_low"] < inst["pre_close"] * 0.97
        and seconds is not None
        and seconds > 9 * 3600 + 33 * 60
    ):
        return
    request = {
        "quantity": None,
        "price": round(quote["bid"] * 0.99, 2),
        "reason": "weak_rebound_failure_primary",
        "action": "weak_rebound_primary",
        "split_account_available": True,
        "remainder_price": round(inst["pre_close"] * 1.04, 2),
    }
    _queue_after_cancel(
        inst, [request], "weak_rebound_failure_cancel",
        next_flag=FLAG_NORMAL, next_reason="weak_rebound_failure",
    )


def _handle_limit_down_clear(inst, quote):
    if not (
        _price_equal(quote["ask"], inst["limit_down"])
        and inst["open_price"] > inst["pre_close"] * 0.91
        and inst["flag"] != FLAG_LIMIT_DOWN_CLEAR
        and _book_amount(quote["ask"], quote["ask_volume"]) > LIMIT_DOWN_CLEAR_AMOUNT
    ):
        return
    request = {
        "quantity": None,
        "price": inst["limit_down"],
        "reason": "limit_down_clear",
        "action": "limit_down_clear",
    }
    _queue_after_cancel(
        inst, [request], "limit_down_clear_cancel",
        next_flag=FLAG_LIMIT_DOWN_CLEAR, next_reason="limit_down_clear",
    )


def _handle_flag_five(inst, quote, seconds):
    if inst["flag"] != FLAG_REBOUND or quote["bid"] >= inst["pre_close"] * 1.05:
        return
    if seconds is not None and seconds < 9 * 3600 + 31 * 60 and inst["open_price"] < inst["pre_close"] * 1.05:
        _log("[FLAG5_WAIT] code=%s reason=first_minute_below_5_percent" % inst["code"])
        return
    if inst["day_high"] > inst["pre_close"] * 1.065:
        reason = "flag5_high_above_6_5_break_5"
        action = "flag5_high_pullback"
    elif quote["bid"] < inst["pre_close"] * 1.03:
        reason = "flag5_break_3"
        action = "flag5_break3"
    else:
        return
    request = {
        "quantity": inst["sell_quantity"],
        "price": round(quote["bid"] * 0.99, 2),
        "reason": reason,
        "action": action,
    }
    _queue_after_cancel(inst, [request], reason + "_cancel", next_flag=FLAG_SELL_DONE, next_reason=reason)


def _handle_flag_seven(inst, quote, seconds):
    if inst["flag"] != FLAG_STRONG:
        return
    if (
        inst["day_high"] > inst["pre_close"] * 1.085
        and quote["bid"] < inst["pre_close"] * 1.07
    ):
        request = {
            "quantity": inst["sell_quantity"],
            "price": round(quote["bid"] * 0.985, 2) + 0.01,
            "reason": "flag7_break_7_after_high_8_5",
            "action": "flag7_break7_after_high",
        }
        _queue_after_cancel(inst, [request], "flag7_break7_cancel", next_flag=FLAG_SELL_DONE, next_reason="flag7_break_7")
        return
    if inst["day_high"] > 0 and quote["bid"] < inst["day_high"] * 0.975:
        request = {
            "quantity": inst["sell_quantity"],
            "price": round(quote["bid"] * 0.99, 2),
            "reason": "flag7_high_pullback_2_5",
            "action": "flag7_high_pullback_25",
        }
        _queue_after_cancel(inst, [request], "flag7_high_pullback_cancel", next_flag=FLAG_SELL_DONE, next_reason="flag7_high_pullback_2_5")
        return
    if (
        inst["flag"] == FLAG_STRONG
        and quote["bid"] < inst["pre_close"] * 1.065
        and seconds is not None
        and seconds < 9 * 3600 + 31 * 60
    ):
        request = {
            "quantity": inst["sell_quantity"],
            "price": round(quote["bid"] * 0.99, 2),
            "reason": "flag7_morning_break_6_5",
            "action": "flag7_morning_take_profit",
        }
        _queue_after_cancel(inst, [request], "flag7_morning_cancel", next_flag=FLAG_SELL_DONE, next_reason="flag7_morning")


def _handle_limit_up_open(inst, quote, seconds):
    if inst["flag"] != FLAG_LIMIT_UP:
        return
    if inst["open_flag"] == FLAG_LIMIT_UP and inst["up_sell"] == LIMIT_UP_FIRST_SELL_DONE:
        return
    amount = _book_amount(quote["bid"], quote["bid_volume"])
    previous_amount = inst["pre_up_amount"]
    opened_or_weakened = (
        quote["bid"] < inst["limit_up"]
        or (
            _price_equal(quote["bid"], inst["limit_up"])
            and amount < LIMIT_UP_WEAK_AMOUNT
            and previous_amount - amount > LIMIT_UP_WEAK_REDUCE_AMOUNT
        )
        or (
            _price_equal(quote["bid"], inst["limit_up"])
            and amount < LIMIT_UP_LARGE_AMOUNT
            and previous_amount - amount > LIMIT_UP_LARGE_REDUCE_AMOUNT
        )
    )
    if opened_or_weakened:
        gap = _now() - (inst["limit_up_seen_at"] or _now())
        if gap < LIMIT_OPEN_GRACE_SECONDS and inst["open_price"] < inst["limit_up"] and seconds < 9 * 3600 + 40 * 60:
            _log("[LIMIT_OPEN_WAIT] code=%s reason=within_first_minute" % inst["code"])
        elif inst["up_sell"] != LIMIT_UP_FIRST_SELL_DONE:
            request = {
                "quantity": inst["sell_quantity"],
                "price": round(quote["bid"] * 0.982, 2),
                "reason": "limit_open_first_sell",
                "action": "limit_open_first_sell",
                "set_up_sell": True,
            }
            _queue_after_cancel(inst, [request], "limit_open_first_sell_cancel",
                                next_flag=FLAG_SELL_DONE, next_reason="limit_open_first_sell")
    if quote["bid"] < inst["limit_up"]:
        if inst["open_flag"] != FLAG_LIMIT_UP:
            inst["limit_open_started_at"] = _now()
            inst["open_flag"] = FLAG_LIMIT_UP
        if inst["up_sell"] == LIMIT_UP_FIRST_SELL_DONE:
            _set_flag(inst, FLAG_SELL_DONE, "limit_open_first_sell")


def _handle_limit_open_second_sell(inst, quote, first_sell_done_before_tick):
    if not (
        first_sell_done_before_tick
        and not inst["limit_open_second_sell_done"]
        and inst["pre_bid"] >= inst["pre_close"] * 1.03
        and quote["bid"] < inst["pre_close"] * 1.03
    ):
        return
    request = {
        "quantity": inst["sell_quantity"],
        "price": round(quote["bid"] * 0.99, 2),
        "reason": "limit_open_second_sell_break_3",
        "action": "limit_open_second_sell",
        "set_second": True,
    }
    _queue_after_cancel(
        inst, [request], "limit_open_second_sell_cancel",
        next_flag=FLAG_SELL_DONE, next_reason="limit_open_second_sell",
    )


def _recover_after_limit_open_timeout(inst, quote):
    if inst["open_flag"] == FLAG_LIMIT_UP and quote["bid"] < inst["limit_up"]:
        if _now() - (inst["limit_open_started_at"] or _now()) > LIMIT_OPEN_RECOVERY_SECONDS:
            inst["open_flag"] = 0
            inst["limit_open_started_at"] = _now()
            _set_flag(inst, FLAG_NORMAL, "limit_open_5_minutes_no_reseal")


def _update_price_state(inst, quote):
    if (
        quote["bid"] > inst["pre_close"] * 1.07
        and quote["bid"] < inst["limit_up"]
        and inst["flag"] >= 0
        and inst["flag"] != FLAG_LIMIT_UP
    ):
        _set_flag(inst, FLAG_STRONG, "bid_above_7_percent")
    if (
        quote["bid"] > inst["pre_close"] * 1.04
        and quote["bid"] < inst["pre_close"] * 1.07
        and inst["flag"] == FLAG_WEAK
    ):
        _set_flag(inst, FLAG_REBOUND, "weak_state_rebound_4_to_7_percent")


def _log_flag7_monitor(inst, quote):
    if inst["flag"] != FLAG_STRONG:
        inst["flag7_condition_state"] = None
        return
    high_reached = inst["day_high"] > inst["pre_close"] * 1.085
    pulled_back = quote["bid"] < inst["pre_close"] * 1.07
    state = (high_reached, pulled_back)
    if state != inst["flag7_condition_state"]:
        _log("[FLAG7_MONITOR] code=%s bid=%.3f high=%.3f high_8_5=%s back_7=%s pullback_2_5=%.3f" % (
            inst["code"], quote["bid"], inst["day_high"], high_reached, pulled_back,
            inst["day_high"] * 0.975
        ))
    inst["flag7_condition_state"] = state


def _finish_quote(inst, quote):
    inst["pre_bid"] = quote["bid"]
    if _price_equal(quote["bid"], inst["limit_up"]):
        inst["pre_up_amount"] = _book_amount(quote["bid"], quote["bid_volume"])


def _process_l2quote(inst, data, event_seconds):
    event_time = _event_time(_field(data, "stime", "time", "timestamp"))
    quote = _update_market(inst, data, event_time)
    inst["last_l2_quote"] = quote
    inst["last_l2_event_time"] = event_time
    inst["last_l2_at"] = _now()
    if quote["bid"] <= 0 or inst["pre_close"] <= 0 or inst["limit_up"] <= 0:
        return
    first_sell_done_before_tick = inst["up_sell"] == LIMIT_UP_FIRST_SELL_DONE
    _drain_pending_after_cancel(inst)
    if _remaining_target(inst) <= 0:
        _finish_quote(inst, quote)
        return
    _handle_auction(inst, quote, event_seconds)
    if not _in_regular_session(event_seconds):
        return
    _handle_early_under_expectation(inst, quote, event_seconds)
    _confirm_limit_up(inst, quote)
    _mark_weak_state(inst, quote)
    _handle_weak_rebound_failure(inst, quote, event_seconds)
    _handle_limit_down_clear(inst, quote)
    _handle_flag_five(inst, quote, event_seconds)
    _handle_flag_seven(inst, quote, event_seconds)
    _handle_limit_up_open(inst, quote, event_seconds)
    _handle_limit_open_second_sell(inst, quote, first_sell_done_before_tick)
    _recover_after_limit_open_timeout(inst, quote)
    _update_price_state(inst, quote)
    _log_flag7_monitor(inst, quote)
    _finish_quote(inst, quote)


def _handle_quote(code, period, payload, subscription_token=None):
    with _LOCK:
        inst = _INSTANCES.get(code)
        if not inst:
            return
        if subscription_token is not None and (
            inst["subscription_tokens"].get(period) is not subscription_token
            or period in inst["disabled_subscription_periods"]
        ):
            return
        data = _owned_payload(code, payload)
        event_seconds = _event_seconds(_field(data, "stime", "time", "timestamp"))
        if period == "tick":
            _update_market(inst, data, _event_time(_field(data, "stime", "time", "timestamp")))
            return
        if period == "l2quote":
            _process_l2quote(inst, data, event_seconds)


def _subscribe(C, code, period):
    global _SUB_SUCCESS, _SUB_FAILED
    inst = _INSTANCES.get(code)
    if inst is None:
        return
    if period in inst["subscription_ids"]:
        inst["disabled_subscription_periods"].discard(period)
        return
    token = object()
    inst["subscription_tokens"][period] = token
    inst["disabled_subscription_periods"].discard(period)
    callback = lambda data, c=code, p=period, t=token: _handle_quote(c, p, data, t)
    try:
        sub_id = C.subscribe_quote(
            code, period=period, dividend_type="follow", result_type="dict", callback=callback
        )
        if sub_id and sub_id > 0:
            _SUB_SUCCESS += 1
            inst["subscription_ids"][period] = sub_id
            _SUBSCRIPTIONS.append(sub_id)
            _log("[SUBSCRIBE] code=%s period=%s id=%s" % (code, period, sub_id))
        else:
            if inst["subscription_tokens"].get(period) is token:
                inst["subscription_tokens"].pop(period, None)
            _SUB_FAILED += 1
            _log("[SUBSCRIBE_FAILED] code=%s period=%s return=%r" % (code, period, sub_id))
    except Exception as exc:
        if inst["subscription_tokens"].get(period) is token:
            inst["subscription_tokens"].pop(period, None)
        _SUB_FAILED += 1
        _log("[SUBSCRIBE_FAILED] code=%s period=%s error=%r" % (code, period, exc))


def _unsubscribe_stock(C, inst, reason):
    unsubscriber = getattr(C, "unsubscribe_quote", None)
    for period, sub_id in list(inst["subscription_ids"].items()):
        inst["disabled_subscription_periods"].add(period)
        if not callable(unsubscriber):
            _log("[UNSUBSCRIBE_FAILED] code=%s period=%s id=%s reason=api_unavailable" % (
                inst["code"], period, sub_id
            ))
            continue
        try:
            result = unsubscriber(sub_id)
        except Exception as exc:
            _log("[UNSUBSCRIBE_FAILED] code=%s period=%s id=%s error=%r" % (
                inst["code"], period, sub_id, exc
            ))
            continue
        if result is False:
            _log("[UNSUBSCRIBE_FAILED] code=%s period=%s id=%s return=%r" % (
                inst["code"], period, sub_id, result
            ))
            continue
        inst["subscription_ids"].pop(period, None)
        inst["subscription_tokens"].pop(period, None)
        try:
            _SUBSCRIPTIONS.remove(sub_id)
        except ValueError:
            pass
        _log("[UNSUBSCRIBE] code=%s period=%s id=%s reason=%s" % (
            inst["code"], period, sub_id, reason
        ))


def _sync_market_subscriptions(C):
    if not ENABLE_LIVE_ORDERS or C is None or not _position_cache_valid():
        return
    for inst in _INSTANCES.values():
        available = max(0, _POSITION_CACHE["available"].get(inst["code"], 0))
        no_sellable_quantity = available <= 0 or _remaining_target(inst) <= 0
        if no_sellable_quantity:
            if _has_active_orders(inst) or inst.get("pending_after_cancel"):
                continue
            _unsubscribe_stock(C, inst, "no_sellable_quantity")
            continue
        for period in PERIODS:
            _subscribe(C, inst["code"], period)


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _pool_plans(trade_date=None):
    current_date = str(trade_date or _dt.datetime.now().strftime("%Y%m%d"))
    if current_date != POOL_TRADE_DATE:
        raise ValueError(
            "pool_date_mismatch configured=%s current=%s" % (POOL_TRADE_DATE, current_date)
        )
    if not SELL_POOL:
        raise ValueError("inline_pool_empty")
    result = []
    seen = set()
    for row_no, row in enumerate(SELL_POOL, 1):
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise ValueError("invalid inline pool row %s" % row_no)
        code = _stock_code(row[0])
        quantity = max(0, _to_int(row[2]))
        if not code or quantity <= 0:
            raise ValueError("invalid inline pool row %s" % row_no)
        if code in seen:
            raise ValueError("duplicate inline pool row %s: %s" % (row_no, code))
        seen.add(code)
        result.append({
            "row_no": row_no,
            "code": code,
            "exp": _to_int(row[1]),
            "sell_quantity": quantity,
        })
    if not result:
        raise ValueError("inline_pool_empty")
    return result


def _initial_quote(C, inst):
    try:
        detail_getter = getattr(C, "get_instrument_detail", None)
        if callable(detail_getter):
            detail = detail_getter(inst["code"])
            _update_market(inst, detail, "")
            instrument_name = _first_value(_field(detail, "InstrumentName"))
            if instrument_name:
                inst["nick"] = str(instrument_name).strip() or inst["code"]
            if inst["pre_close"] <= 0:
                inst["pre_close"] = _safe_float(_field(
                    detail, "PreClose", "lastClose", "preClose", "pre_close"
                ))
            if inst["limit_up"] <= 0:
                inst["limit_up"] = _safe_float(_field(
                    detail, "UpStopPrice", "upStopPrice", "upLimit", "upLimitPrice"
                ))
            if inst["limit_down"] <= 0:
                inst["limit_down"] = _safe_float(_field(
                    detail, "DownStopPrice", "downStopPrice", "downLimit", "downLimitPrice"
                ))
        full_getter = getattr(C, "get_full_tick", None)
        if not callable(full_getter):
            return
        payload = full_getter([inst["code"]])
        data = _owned_payload(inst["code"], payload)
        event_seconds = _event_seconds(_field(data, "stime", "time", "timestamp"))
        quote = _update_market(inst, data, _event_time(_field(data, "stime", "time", "timestamp")))
        if quote["bid"] > 0 and _price_equal(quote["bid"], inst["limit_up"]):
            amount = _book_amount(quote["bid"], quote["bid_volume"])
            if amount > LIMIT_UP_CONFIRM_AMOUNT:
                inst["limit_up_seen_at"] = _now()
                _set_flag(inst, FLAG_LIMIT_UP, "startup_observed_limit_up")
                inst["pre_up_amount"] = amount
                _log("[STARTUP_LIMIT_UP] code=%s amount=%.0f event=%s" % (
                    inst["code"], amount, _event_time(_field(data, "stime", "time", "timestamp"))
                ))
        inst["pre_bid"] = quote["bid"]
    except Exception as exc:
        _log("[INITIAL_QUOTE_FAILED] code=%s error=%r" % (inst["code"], exc))


def _summary_price(value):
    value = _safe_float(value)
    return "%.3f" % value if value > 0 else "-"


def _summary_stock_label(inst):
    return _stock_label(inst.get("code"), inst)


def _summary():
    states = {}
    active_stocks = 0
    active_orders = 0
    for inst in _INSTANCES.values():
        flag = inst.get("flag", FLAG_NORMAL)
        states[flag] = states.get(flag, 0) + 1
        if _has_active_orders(inst):
            active_stocks += 1
        active_orders += sum(1 for order in inst.get("orders", []) if _is_order_active(order))
    state_text = ",".join(
        "%s=%d" % (_FLAG_LABELS.get(flag, str(flag)), count)
        for flag, count in sorted(states.items())
    ) or "-"
    _log("[SUMMARY][\u6982\u89c8] \u65e5\u671f=%s \u80a1\u7968=%d \u8ba2\u9605=%d \u6210\u529f=%d \u5931\u8d25=%d \u6d3b\u8dc3\u80a1\u7968=%d \u6d3b\u8dc3\u59d4\u6258=%d \u72b6\u6001=%s" % (
        POOL_TRADE_DATE, len(_INSTANCES), len(_SUBSCRIPTIONS), _SUB_SUCCESS,
        _SUB_FAILED, active_stocks, active_orders, state_text
    ))

    for inst in sorted(_INSTANCES.values(), key=lambda item: item.get("code", "")):
        label = _summary_stock_label(inst)
        flag = inst.get("flag", FLAG_NORMAL)
        flag_label = _FLAG_LABELS.get(flag, "\u672a\u77e5")
        target = max(0, _to_int(inst.get("sell_quantity", 0)))
        traded = sum(max(0, _to_int(order.get("traded_quantity", 0))) for order in inst.get("orders", []))
        remaining = max(0, target - traded)
        if ENABLE_LIVE_ORDERS:
            available = _POSITION_CACHE["available"].get(inst.get("code"))
            available_text = str(max(0, _to_int(available))) if (
                available is not None and _position_cache_valid()
            ) else "-"
        else:
            available_text = str(remaining)
        _log("[SUMMARY][\u80a1\u7968] %s" % label)
        _log("[SUMMARY][\u72b6\u6001] %s flag=%s(%s) \u53ef\u5356=%s \u76ee\u6807=%d \u5df2\u6210\u4ea4=%d \u5269\u4f59=%d" % (
            label, flag, flag_label, available_text, target, traded, remaining
        ))

        quote = inst.get("last_l2_quote") or inst.get("last_quote") or {}
        event_time = inst.get("last_l2_event_time") or inst.get("last_event_time") or "-"
        _log("[SUMMARY][\u884c\u60c5] %s \u4e70\u4e00=%s \u6700\u65b0=%s \u5f00\u76d8=%s \u6700\u9ad8=%s \u6700\u4f4e=%s \u6628\u6536=%s \u6da8\u505c=%s \u8dcc\u505c=%s \u4e8b\u4ef6=%s" % (
            label,
            _summary_price(quote.get("bid")),
            _summary_price(quote.get("last")),
            _summary_price(inst.get("open_price")),
            _summary_price(inst.get("day_high")),
            _summary_price(inst.get("day_low")),
            _summary_price(inst.get("pre_close")),
            _summary_price(inst.get("limit_up")),
            _summary_price(inst.get("limit_down")),
            event_time,
        ))

        orders = inst.get("orders", [])
        if not orders:
            _log("[SUMMARY][\u59d4\u6258] %s \u65e0\u59d4\u6258" % label)
            continue
        for order in orders:
            action = str(order.get("action") or "-")
            action_label = _ACTION_LABELS.get(action, action)
            status = str(order.get("status") or "-")
            status_label = _ORDER_STATUS_LABELS.get(status, status)
            quantity = max(0, _to_int(order.get("quantity", 0)))
            traded_quantity = max(0, _to_int(order.get("traded_quantity", 0)))
            order_id = str(order.get("order_id") or "-")
            _log("[SUMMARY][\u59d4\u6258] %s \u52a8\u4f5c=%s(%s) \u72b6\u6001=%s(%s) \u59d4\u6258\u53f7=%s \u4ef7\u683c=%s \u6570\u91cf=%d \u6210\u4ea4=%d/%d" % (
                label, action, action_label, status, status_label, order_id,
                _summary_price(order.get("price")), quantity, traded_quantity, quantity
            ))


def _drain_all_pending():
    for inst in _INSTANCES.values():
        _drain_pending_after_cancel(inst)


def _position_refresh_timer(ContextInfo):
    with _LOCK:
        _refresh_positions()
        _drain_all_pending()


def _schedule_position_refresh(C):
    global _POSITION_REFRESH_TIMER
    if not ENABLE_LIVE_ORDERS:
        return
    scheduler = getattr(C, "schedule_run", None)
    if not callable(scheduler):
        _log("[POSITION_REFRESH_FAILED] reason=schedule_run_unavailable")
        return
    try:
        _POSITION_REFRESH_TIMER = scheduler(
            _position_refresh_timer,
            _dt.datetime.now() + _dt.timedelta(seconds=POSITION_REFRESH_INTERVAL_SECONDS),
            repeat_times=-1,
            interval=_dt.timedelta(seconds=POSITION_REFRESH_INTERVAL_SECONDS),
            name="juejin_sell_position_refresh",
        )
        _log("[POSITION_REFRESH_SCHEDULED] interval=%.1fs id=%s" % (
            POSITION_REFRESH_INTERVAL_SECONDS, _POSITION_REFRESH_TIMER
        ))
    except Exception as exc:
        _log("[POSITION_REFRESH_FAILED] error=%r" % (exc,))


def init(C):
    global _C, _SUB_SUCCESS, _SUB_FAILED, _LAST_SUMMARY
    with _LOCK:
        _C = C
        _SUBSCRIPTIONS[:] = []
        _SUB_SUCCESS = 0
        _SUB_FAILED = 0
        _INSTANCES.clear()
        _POSITION_CACHE["updated_at"] = 0.0
        _POSITION_CACHE["available"] = {}
        try:
            plans = _pool_plans()
        except Exception as exc:
            _log("[ABORT] inline_pool_invalid error=%r" % (exc,))
            return
        if ACCOUNT_ID:
            setter = getattr(C, "set_account", None)
            if callable(setter):
                setter(ACCOUNT_ID, ACCOUNT_TYPE)
        for plan in plans:
            inst = _new_instance(plan)
            _INSTANCES[inst["code"]] = inst
            _initial_quote(C, inst)
            _log("[JUEJIN_STATUS] code=%s nick=%s status=0 reason=initial" % (inst["code"], inst["nick"]))
        _POSITION_CACHE["updated_at"] = 0.0
        _POSITION_CACHE["available"] = {}
        refreshed = _refresh_positions()
        if not ENABLE_LIVE_ORDERS or not refreshed:
            for inst in _INSTANCES.values():
                for period in PERIODS:
                    _subscribe(C, inst["code"], period)
        _schedule_position_refresh(C)
        _LAST_SUMMARY = _now()
        _log("[INIT] pool_date=%s stocks=%d live=%s log=console" % (
            POOL_TRADE_DATE, len(_INSTANCES), ENABLE_LIVE_ORDERS
        ))


def handlebar(C):
    global _LAST_SUMMARY
    is_last_bar = getattr(C, "is_last_bar", None)
    if callable(is_last_bar) and not is_last_bar():
        return
    with _LOCK:
        _refresh_positions()
        _drain_all_pending()
        if _now() - _LAST_SUMMARY >= SUMMARY_INTERVAL_SECONDS:
            _summary()
            _LAST_SUMMARY = _now()


def order_callback(ContextInfo, orderInfo):
    with _LOCK:
        item = _order_fields(orderInfo)
        for inst in _INSTANCES.values():
            order = _find_order(inst, item)
            if order is not None:
                _apply_order_update(inst, order, item)
                return
        _log("[ORDER_CALLBACK_UNMATCHED] id=%s token=%s code=%s" % (
            item.get("order_id", ""), item.get("token", ""), item.get("code", "")
        ))


def deal_callback(ContextInfo, dealInfo):
    with _LOCK:
        item = _order_fields(dealInfo)
        for inst in _INSTANCES.values():
            order = _find_order(inst, item)
            if order is not None:
                _apply_deal_update(inst, order, item)
                return
        pending = [
            (inst, order)
            for inst in _INSTANCES.values()
            if item.get("code") == inst["code"]
            for order in inst["orders"]
            if order.get("status") == ORDER_SUBMITTING
        ]
        if len(pending) == 1:
            inst, order = pending[0]
            order.setdefault("pending_deal_infos", []).append(item)
            _log("[DEAL_PENDING] code=%s order_id=%s deal_id=%s reason=order_id_not_bound" % (
                inst["code"], item.get("order_id", ""), item.get("deal_id", "")
            ))
            return
        _log("[DEAL_UNMATCHED] id=%s token=%s code=%s" % (
            item.get("order_id", ""), item.get("token", ""), item.get("code", "")
        ))


def stop(C):
    global _POSITION_REFRESH_TIMER
    with _LOCK:
        if _POSITION_REFRESH_TIMER is not None:
            try:
                C.cancel_schedule_run(_POSITION_REFRESH_TIMER)
            except Exception:
                pass
            _POSITION_REFRESH_TIMER = None
        for inst in _INSTANCES.values():
            _unsubscribe_stock(C, inst, "stop")
        for sub_id in list(_SUBSCRIPTIONS):
            try:
                C.unsubscribe_quote(sub_id)
            except Exception:
                pass
        _SUBSCRIPTIONS[:] = []
        for inst in _INSTANCES.values():
            inst["subscription_ids"].clear()
            inst["subscription_tokens"].clear()
            inst["disabled_subscription_periods"].clear()
            _log("[STOP] code=%s flag=%s active_orders=%s" % (
                inst["code"], inst["flag"], _has_active_orders(inst)
            ))


__all__ = ["init", "handlebar", "stop", "order_callback", "deal_callback"]
