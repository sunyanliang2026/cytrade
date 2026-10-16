# coding: gbk
"""Native QMT model for overnight limit-up buy orders.

This file is self-contained for the QMT embedded Python runtime. Live orders
are disabled until an operator reviews and changes the constants below.
"""

import csv
import datetime
import decimal
import json
import os
import time
import uuid


# ------------------------- operator configuration -------------------------
ENABLE_LIVE_ORDERS = False
ACCOUNT_ID = ""
ACCOUNT_TYPE = "stock"
STRATEGY_NAME = "overnight_limit_up_buy_native"
ORDER_TYPE_BUY = 1101
PRICE_TYPE_LIMIT = 11
QUICK_TRADE = 2

QMT_HOME = u"C:/\u5149\u5927\u8bc1\u5238\u91d1\u9633\u5149QMT\u5b9e\u76d8"
ORDER_CSV_PATH = os.path.join(QMT_HOME, "python", "overnight_orders.csv")
STATE_FILE_PATH = os.path.join(
    QMT_HOME, "userdata", "log", "qmt_overnight_limit_up_buy_state.json"
)

SUBMIT_HOUR = 8
SUBMIT_MINUTE = 30
LATEST_SUBMIT_HOUR = 15
LATEST_SUBMIT_MINUTE = 0
MAX_PREOPEN_RETRIES = 2
RETRY_DELAY_MS = 100
PREFLIGHT_ROWS_PER_TURN = 5
SUBMIT_ROWS_PER_TURN = 5
SCHEDULER_INTERVAL_MS = 100
MAX_ORDERS = 500
SCHEDULE_NAME = "qmt_overnight_limit_up_buy_drive"
UNMATCHED_CALLBACK_LOG_INTERVAL_SECONDS = 5


# ------------------------------ model state -------------------------------
_CTX = None
_SESSION = {}
_STATE = {"trade_days": {}}


def _log(message):
    line = "[overnight] %s" % message
    try:
        print(line)
    except Exception:
        pass


def _field(source, *names):
    if isinstance(source, dict):
        for name in names:
            if name in source and source.get(name) is not None:
                return source.get(name)
    for name in names:
        try:
            value = getattr(source, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _float_value(value):
    try:
        number = float(value)
        if number > 0 and number < float("inf"):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return 0.0


def _normalize_code(value):
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) != 6:
        return ""
    if digits.startswith(("5", "6", "9", "11")):
        market = "SH"
    elif digits.startswith(("8", "4", "92")):
        market = "BJ"
    else:
        market = "SZ"
    return digits + "." + market


def _parse_amount(value, row_no):
    text = str(value or "").strip().replace(",", "")
    try:
        amount = decimal.Decimal(text)
    except (decimal.InvalidOperation, ValueError):
        raise ValueError("row %s invalid amount" % row_no)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("row %s amount must be positive" % row_no)
    return amount


def _load_orders(path):
    if not os.path.isfile(path):
        raise ValueError("order CSV not found: %s" % path)
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = [str(item or "").strip() for item in (reader.fieldnames or [])]
        if columns != ["stock_code", "amount"]:
            raise ValueError("CSV header must be exactly stock_code,amount")
        for row_no, row in enumerate(reader, 2):
            code = _normalize_code(row.get("stock_code"))
            if not code:
                raise ValueError("row %s invalid stock_code" % row_no)
            amount = _parse_amount(row.get("amount"), row_no)
            rows.append({
                "row_no": row_no,
                "stock_code": code,
                "amount": amount,
            })
            if len(rows) > MAX_ORDERS:
                raise ValueError("CSV exceeds MAX_ORDERS=%s" % MAX_ORDERS)
    if not rows:
        raise ValueError("order CSV contains no orders")
    return rows


def _normalize_day(value):
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, datetime.date):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4:5] == "-":
        text = text[:10].replace("-", "")
    if len(text) == 8 and text.isdigit():
        return text
    if len(text) >= 14 and text[:14].isdigit():
        return text[:8]
    try:
        number = float(text)
        if number > 1000000000000:
            number /= 1000.0
        if number > 1000000000:
            return datetime.datetime.fromtimestamp(number).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    if len(text) >= 8 and text[:8].isdigit():
        return text[:8]
    return ""


def _state_load(path):
    if not os.path.isfile(path):
        return {"trade_days": {}}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("trade_days"), dict):
        raise ValueError("state file has an unsupported format: %s" % path)
    return data


def _state_save():
    path = STATE_FILE_PATH
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(_STATE, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except Exception:
            pass
    os.replace(temp_path, path)


def _cancel_scheduler(C=None):
    context = C or _CTX
    cancel = getattr(context, "cancel_schedule_run", None) if context is not None else None
    if callable(cancel):
        task_id = _SESSION.get("task_id")
        try:
            cancel(task_id if task_id is not None else SCHEDULE_NAME)
        except Exception:
            try:
                cancel(SCHEDULE_NAME)
            except Exception:
                pass
    _SESSION["task_id"] = None


def _abort(reason):
    _SESSION["phase"] = "ABORTED"
    _SESSION["reason"] = str(reason)
    _cancel_scheduler()
    _log("ABORT reason=%s; no further orders will be sent" % reason)


def _detail_for(C, stock_code):
    getter = getattr(C, "get_instrument_detail", None)
    if not callable(getter):
        raise ValueError("ContextInfo.get_instrument_detail is unavailable")
    detail = getter(stock_code)
    if not isinstance(detail, dict):
        raise ValueError("instrument detail is not a dictionary for %s" % stock_code)
    return detail


def _limit_up_price(detail, trade_day, stock_code):
    detail_day = _normalize_day(_field(
        detail, "TradingDay", "tradingDay", "m_strTradingDay", "tradeDate"
    ))
    # The native model is scheduled by clock time.  Some QMT builds expose
    # the previous trading day in metadata around the pre-open transition;
    # that metadata must not block the timed submission path.  UpStopPrice is
    # still required and remains the only price value accepted for planning.
    if detail_day and detail_day != trade_day:
        _log("PRICE_DAY_INFO code=%s expected=%s got=%s; ignored for scheduling" % (
            stock_code, trade_day, detail_day
        ))
    price = _float_value(_field(
        detail, "UpStopPrice", "upStopPrice", "upLimit", "up_limit",
        "upperLimit", "limitUp", "upLimitPrice"
    ))
    if price <= 0:
        raise ValueError("verified UpStopPrice missing for %s" % stock_code)
    return price


def _quantity_for(amount, price):
    amount_value = decimal.Decimal(str(amount))
    price_value = decimal.Decimal(str(price))
    shares = (amount_value / price_value / decimal.Decimal("100"))
    lots = int(shares.to_integral_value(rounding=decimal.ROUND_FLOOR))
    return lots * 100


def _available_cash(C):
    getter = globals().get("get_trade_detail_data")
    if not callable(getter):
        raise ValueError("get_trade_detail_data is unavailable")
    data = getter(ACCOUNT_ID, ACCOUNT_TYPE, "account")
    if isinstance(data, (list, tuple)):
        if not data:
            raise ValueError("account detail is empty")
        data = data[0]
    cash = _float_value(_field(data, "m_dAvailable", "available", "available_cash"))
    if cash <= 0:
        raise ValueError("available cash is missing or not positive")
    return cash


def _query_order_records():
    """Read broker orders when deciding whether an old reservation is stale."""
    getter = globals().get("get_trade_detail_data")
    if not callable(getter):
        return None
    try:
        data = getter(ACCOUNT_ID, ACCOUNT_TYPE, "order")
    except Exception as exc:
        _log("STATE_ORDER_QUERY_FAILED reason=%r" % (exc,))
        return None
    if data is None:
        return None
    if isinstance(data, (list, tuple)):
        return list(data)
    return [data]


def _order_record_matches_plan(record, plan):
    code = _normalize_code(_field(
        record, "m_strInstrumentID", "m_strOrderCode", "orderCode",
        "stock_code", "code"
    ))
    if code != plan.get("stock_code"):
        return False
    strategy = str(_field(
        record, "m_strStrategyName", "strategyName", "strategy_name"
    ) or "").strip()
    if strategy and strategy != STRATEGY_NAME:
        return False
    token = str(_field(
        record, "m_strRemark", "m_strOrderRemark", "m_strUserOrderID",
        "m_strUserOrderId", "userOrderId", "user_order_id", "remark"
    ) or "").strip()
    if token and token == str(plan.get("last_token") or ""):
        return True
    quantity = _float_value(_field(
        record, "m_nVolumeTotalOriginal", "m_nVolume", "volume",
        "quantity", "order_volume"
    ))
    price = _float_value(_field(
        record, "m_dPrice", "price", "order_price", "m_dOrderPrice"
    ))
    if quantity <= 0 or price <= 0:
        return bool(strategy == STRATEGY_NAME)
    return (
        int(quantity) == int(plan.get("quantity") or 0)
        and abs(price - float(plan.get("limit_up_price") or 0.0)) < 0.0001
    )


def _reservation_has_broker_order(batch):
    orders = batch.get("orders", {}) if isinstance(batch, dict) else {}
    for saved in orders.values():
        if saved.get("order_id"):
            return True
    records = _query_order_records()
    if records is None:
        return True
    for saved in orders.values():
        plan = {
            "stock_code": saved.get("stock_code"),
            "quantity": saved.get("quantity"),
            "limit_up_price": saved.get("limit_up_price"),
            "last_token": saved.get("last_token", ""),
        }
        if any(_order_record_matches_plan(record, plan) for record in records):
            return True
    return False


def _format_plan():
    total = sum(item["estimated_amount"] for item in _SESSION["plans"])
    _log("PREFLIGHT_OK day=%s rows=%s estimate=%.2f live=%s" % (
        _SESSION["trade_day"], len(_SESSION["plans"]), total, ENABLE_LIVE_ORDERS
    ))
    if ENABLE_LIVE_ORDERS:
        _log("FUNDS_OK available=%.2f required=%.2f" % (
            _SESSION["available_cash"], total
        ))
    else:
        _log("FUNDS_CHECK_SKIPPED reason=live_orders_disabled")


def _preflight_step(C):
    if not _SESSION.get("state_checked"):
        day = _SESSION["trade_day"]
        batch = _SESSION["state"].get("trade_days", {}).get(day)
        if batch:
            if ENABLE_LIVE_ORDERS and not _reservation_has_broker_order(batch):
                _log("STATE_STALE_NO_BROKER_ORDER day=%s; releasing prior reservation" % day)
                del _SESSION["state"]["trade_days"][day]
                try:
                    _state_save()
                except Exception as exc:
                    _abort("could not release stale reservation: %r" % (exc,))
                    return
            else:
                _abort("a submission reservation already exists for trade day %s" % day)
                return
        _SESSION["state_checked"] = True
        _log("TRADING_DAY_CHECK_SKIPPED day=%s; clock schedule controls submission" % day)

    rows = _SESSION["orders"]
    start = _SESSION["preflight_cursor"]
    end = min(len(rows), start + PREFLIGHT_ROWS_PER_TURN)
    for index in range(start, end):
        order = rows[index]
        code = order["stock_code"]
        try:
            if code not in _SESSION["price_cache"]:
                detail = _detail_for(C, code)
                _SESSION["price_cache"][code] = _limit_up_price(
                    detail, _SESSION["trade_day"], code
                )
            price = _SESSION["price_cache"][code]
            quantity = _quantity_for(order["amount"], price)
            if quantity < 100:
                raise ValueError("budget is below one board lot for %s" % code)
            amount = float(order["amount"])
            estimate = round(price * quantity, 2)
            _SESSION["plans"].append({
                "row_no": order["row_no"],
                "stock_code": code,
                "amount": amount,
                "limit_up_price": price,
                "quantity": quantity,
                "estimated_amount": estimate,
                "attempts": 0,
                "status": "planned",
                "last_token": "",
                "order_id": "",
            })
            plan = _SESSION["plans"][-1]
            _SESSION["plan_by_row"][order["row_no"]] = plan
            _log("PLAN row=%s code=%s budget=%.2f limit_up=%.3f qty=%s estimate=%.2f" % (
                plan["row_no"], plan["stock_code"], plan["amount"],
                plan["limit_up_price"], plan["quantity"], plan["estimated_amount"]
            ))
        except Exception as exc:
            _SESSION["failures"].append(order["row_no"])
            _log("PREFLIGHT_ROW_FAILED row=%s code=%s reason=%s" % (
                order["row_no"], code, exc
            ))
    _SESSION["preflight_cursor"] = end

    if end < len(rows):
        _log("PREFLIGHT_PROGRESS checked=%s total=%s" % (end, len(rows)))
        return
    if _SESSION["failures"]:
        _abort("preflight failed rows=%s" % len(_SESSION["failures"]))
        return

    required = sum(item["estimated_amount"] for item in _SESSION["plans"])
    if ENABLE_LIVE_ORDERS:
        try:
            available = _available_cash(C)
        except Exception as exc:
            _abort("funding preflight failed: %r" % (exc,))
            return
        _SESSION["available_cash"] = available
        if available + 0.0001 < required:
            _abort("insufficient available cash: available=%.2f required=%.2f" % (
                available, required
            ))
            return

    _format_plan()
    _SESSION["phase"] = "WAITING_FOR_SUBMIT_TIME"


def _reserve_batch():
    if not ENABLE_LIVE_ORDERS:
        return True
    day = _SESSION["trade_day"]
    if _STATE.get("trade_days", {}).get(day):
        _abort("submission reservation appeared during preflight for %s" % day)
        return False
    batch = {
        "status": "reserved",
        "batch_id": uuid.uuid4().hex,
        "csv_path": ORDER_CSV_PATH,
        "reserved_at": datetime.datetime.now().isoformat(),
        "orders": {},
    }
    for item in _SESSION["plans"]:
        batch["orders"][str(item["row_no"])] = {
            "stock_code": item["stock_code"],
            "amount": item["amount"],
            "limit_up_price": item["limit_up_price"],
            "quantity": item["quantity"],
            "status": "reserved",
            "attempts": 0,
        }
    _STATE.setdefault("trade_days", {})[day] = batch
    try:
        _state_save()
    except Exception as exc:
        del _STATE["trade_days"][day]
        _abort("could not persist duplicate-protection reservation: %r" % (exc,))
        return False
    _SESSION["reserved"] = True
    _log("STATE_RESERVED path=%s batch_id=%s" % (
        STATE_FILE_PATH, batch["batch_id"]
    ))
    return True


def _update_reserved_order(plan):
    if not _SESSION.get("reserved"):
        return
    day = _SESSION["trade_day"]
    batch = _STATE.get("trade_days", {}).get(day, {})
    saved = batch.get("orders", {}).get(str(plan["row_no"]))
    if saved is not None:
        saved["status"] = plan["status"]
        saved["attempts"] = plan["attempts"]
        saved["last_token"] = plan["last_token"]
        saved["order_id"] = plan["order_id"]


def _persist_outcomes(batch_status):
    if not _SESSION.get("reserved"):
        return
    batch = _STATE.get("trade_days", {}).get(_SESSION["trade_day"], {})
    batch["status"] = batch_status
    batch["updated_at"] = datetime.datetime.now().isoformat()
    for plan in _SESSION["plans"]:
        _update_reserved_order(plan)
    try:
        _state_save()
    except Exception as exc:
        _log("STATE_UPDATE_FAILED reason=%r; original reservation remains" % (exc,))


def _is_preopen_rejection(message):
    text = str(message or "").lower()
    markers = (
        "\u672a\u5230\u5f00\u5e02", "\u5c1a\u672a\u5f00\u5e02", "\u672a\u5f00\u5e02",
        "\u4ea4\u6613\u65f6\u95f4\u672a\u5230", "market is not open", "not open yet",
    )
    return any(marker in text for marker in markers)


def _submit_plan(C, plan):
    plan["attempts"] += 1
    token = "%s_%s_%s_%s" % (
        _SESSION["trade_day"], plan["row_no"], plan["attempts"], uuid.uuid4().hex[:8]
    )
    plan["last_token"] = token
    plan["status"] = "sending"
    _update_reserved_order(plan)
    _SESSION["token_to_plan"][token] = plan
    sender = globals().get("passorder")
    if not callable(sender):
        plan["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _persist_outcomes("partial_unknown")
        _log("SEND_UNKNOWN reason=passorder_unavailable row=%s; remaining rows halted" %
             plan["row_no"])
        return False
    try:
        result = sender(
            23, ORDER_TYPE_BUY, ACCOUNT_ID, plan["stock_code"], PRICE_TYPE_LIMIT,
            plan["limit_up_price"], plan["quantity"], STRATEGY_NAME, QUICK_TRADE,
            token, C
        )
    except Exception as exc:
        plan["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _persist_outcomes("partial_unknown")
        _log("SEND_UNKNOWN row=%s token=%s error=%r; remaining rows halted" % (
            plan["row_no"], token, exc
        ))
        return False

    if result is False or (isinstance(result, (int, float)) and result < 0) or str(result) == "-1":
        plan["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _persist_outcomes("partial_unknown")
        _log("SEND_UNKNOWN row=%s token=%s return=%r; remaining rows halted" % (
            plan["row_no"], token, result
        ))
        return False

    if result == 0:
        plan["status"] = "accepted_no_id"
        _update_reserved_order(plan)
        _log("ORDER_ACCEPTED_NO_ID row=%s code=%s price=%.3f qty=%s attempt=%s token=%s; "
             "waiting for broker callback" % (
                 plan["row_no"], plan["stock_code"], plan["limit_up_price"],
                 plan["quantity"], plan["attempts"], token
             ))
        return True

    plan["status"] = "sent"
    if isinstance(result, (str, int)) and str(result) not in ("", "0", "-1"):
        plan["order_id"] = str(result)
        _SESSION["order_id_to_plan"][str(result)] = plan
    _update_reserved_order(plan)
    _log("ORDER_SENT row=%s code=%s price=%.3f qty=%s attempt=%s token=%s return=%r" % (
        plan["row_no"], plan["stock_code"], plan["limit_up_price"],
        plan["quantity"], plan["attempts"], token, result
    ))
    return True


def _dispatch_step(C, now):
    due_rows = sorted(_SESSION["retry_due"].keys())
    for row_no in due_rows:
        if _SESSION["retry_due"][row_no] <= time.monotonic():
            del _SESSION["retry_due"][row_no]
            plan = _SESSION["plan_by_row"][row_no]
            if not _submit_plan(C, plan):
                return
            return

    sent_this_turn = 0
    while (_SESSION["dispatch_cursor"] < len(_SESSION["plans"])
           and sent_this_turn < SUBMIT_ROWS_PER_TURN):
        plan = _SESSION["plans"][_SESSION["dispatch_cursor"]]
        _SESSION["dispatch_cursor"] += 1
        if ENABLE_LIVE_ORDERS:
            if not _submit_plan(C, plan):
                return
        else:
            plan["status"] = "dry_run"
            _log("DRY_RUN row=%s code=%s price=%.3f qty=%s estimate=%.2f" % (
                plan["row_no"], plan["stock_code"], plan["limit_up_price"],
                plan["quantity"], plan["estimated_amount"]
            ))
        sent_this_turn += 1

    if (_SESSION["dispatch_cursor"] >= len(_SESSION["plans"])
            and not _SESSION["retry_due"]):
        if ENABLE_LIVE_ORDERS:
            if _SESSION["phase"] != "MONITORING":
                _SESSION["phase"] = "MONITORING"
                _persist_outcomes("monitoring")
                _log("DISPATCHED_MONITORING rows=%s; waiting for asynchronous order callbacks" %
                     len(_SESSION["plans"]))
        else:
            _SESSION["phase"] = "COMPLETE"
            _persist_outcomes("dispatched")
            _log("DISPATCH_COMPLETE rows=%s live=%s" % (
                len(_SESSION["plans"]), ENABLE_LIVE_ORDERS
            ))


def _drive(C, now=None):
    if not _SESSION or _SESSION.get("phase") in (
            "ABORTED", "PARTIAL_STOP", "COMPLETE", "STOPPED"):
        return
    now = now or datetime.datetime.now()
    phase = _SESSION.get("phase")
    if phase == "PREFLIGHT":
        _preflight_step(C)
        if _SESSION.get("phase") in ("ABORTED", "PARTIAL_STOP", "COMPLETE"):
            _cancel_scheduler(C)
            return
        # If the final preflight slice completed at or after 08:30, continue
        # through the submit phase in this same scheduler callback.
        phase = _SESSION.get("phase")
        if phase == "PREFLIGHT":
            return
    if phase == "MONITORING":
        if (now.hour, now.minute) >= (LATEST_SUBMIT_HOUR, LATEST_SUBMIT_MINUTE):
            _SESSION["retry_due"].clear()
            _SESSION["phase"] = "COMPLETE"
            _persist_outcomes("monitoring_closed")
            _cancel_scheduler(C)
            _log("MONITORING_COMPLETE reason=submission_window_closed")
            return
        _dispatch_step(C, now)
        if _SESSION.get("phase") in ("ABORTED", "PARTIAL_STOP", "COMPLETE"):
            _cancel_scheduler(C)
        return
    if phase == "WAITING_FOR_SUBMIT_TIME":
        if (now.hour, now.minute) >= (LATEST_SUBMIT_HOUR, LATEST_SUBMIT_MINUTE):
            _abort("latest submission time 15:00 has passed")
            return
        if (now.hour, now.minute) < (SUBMIT_HOUR, SUBMIT_MINUTE):
            return
        if not _reserve_batch():
            return
        _SESSION["phase"] = "DISPATCHING"
        _log("SUBMIT_WINDOW_OPEN now=%s; dispatch starts after completed preflight" %
             now.strftime("%Y-%m-%d %H:%M:%S"))
        phase = "DISPATCHING"
    if phase == "DISPATCHING":
        if (now.hour, now.minute) >= (LATEST_SUBMIT_HOUR, LATEST_SUBMIT_MINUTE):
            _SESSION["retry_due"].clear()
            _SESSION["phase"] = "COMPLETE"
            _persist_outcomes("submission_window_closed_partial")
            _cancel_scheduler(C)
            _log("DISPATCH_STOPPED reason=submission_window_closed sent=%s total=%s" % (
                _SESSION["dispatch_cursor"], len(_SESSION["plans"])
            ))
            return
        _dispatch_step(C, now)
        if _SESSION.get("phase") in ("ABORTED", "PARTIAL_STOP", "COMPLETE"):
            _cancel_scheduler(C)


def _scheduled_drive(C):
    try:
        _drive(C)
    except Exception as exc:
        _abort("unhandled scheduler error: %r" % (exc,))


def init(C):
    global _CTX, _SESSION, _STATE
    _CTX = C
    today = datetime.date.today().strftime("%Y%m%d")
    _SESSION = {
        "phase": "PREFLIGHT",
        "trade_day": today,
        "orders": [],
        "plans": [],
        "plan_by_row": {},
        "preflight_cursor": 0,
        "dispatch_cursor": 0,
        "price_cache": {},
        "failures": [],
        "state_checked": False,
        "available_cash": 0.0,
        "retry_due": {},
        "token_to_plan": {},
        "order_id_to_plan": {},
        "handled_callbacks": set(),
        "unmatched_callback_count": 0,
        "unmatched_callback_last_log": 0.0,
        "reserved": False,
        "task_id": None,
    }
    script_file = globals().get("__file__", "<absent>")
    _log("INIT native Python live=%s account_configured=%s cwd=%s script=%s" % (
        ENABLE_LIVE_ORDERS, bool(ACCOUNT_ID), os.getcwd(), script_file
    ))
    _log("PATHS qmt_home=%s csv=%s state=%s" % (
        QMT_HOME, ORDER_CSV_PATH, STATE_FILE_PATH
    ))
    if ENABLE_LIVE_ORDERS and not ACCOUNT_ID:
        _abort("ENABLE_LIVE_ORDERS is true but ACCOUNT_ID is empty")
        return
    if ENABLE_LIVE_ORDERS:
        setter = getattr(C, "set_account", None)
        if not callable(setter):
            _abort("ContextInfo.set_account is unavailable")
            return
        try:
            setter(ACCOUNT_ID, ACCOUNT_TYPE)
        except Exception as exc:
            _abort("account registration failed: %r" % (exc,))
            return
    try:
        _SESSION["orders"] = _load_orders(ORDER_CSV_PATH)
        _STATE = _state_load(STATE_FILE_PATH)
    except Exception as exc:
        _abort("initialization failed: %r" % (exc,))
        return
    _SESSION["state"] = _STATE
    _SESSION["plans"] = []
    for order in _SESSION["orders"]:
        _SESSION["plan_by_row"][order["row_no"]] = None
    _log("CSV_LOADED rows=%s path=%s" % (len(_SESSION["orders"]), ORDER_CSV_PATH))
    scheduler = getattr(C, "schedule_run", None)
    if not callable(scheduler):
        _abort("ContextInfo.schedule_run is unavailable")
        return
    try:
        first_run = datetime.datetime.now() + datetime.timedelta(
            milliseconds=SCHEDULER_INTERVAL_MS
        )
        _SESSION["task_id"] = scheduler(
            _scheduled_drive,
            first_run,
            -1,
            datetime.timedelta(milliseconds=SCHEDULER_INTERVAL_MS),
            SCHEDULE_NAME,
        )
    except Exception as exc:
        _abort("could not start non-blocking scheduler: %r" % (exc,))
        return
    _log("RUNNING phase=PREFLIGHT schedule_id=%r preflight_slice=%s submit_slice=%s" % (
        _SESSION["task_id"], PREFLIGHT_ROWS_PER_TURN, SUBMIT_ROWS_PER_TURN
    ))


def handlebar(C):
    # The timer drives work in small slices; handlebar intentionally stays light.
    return None


def _callback_identity(info):
    token = str(_field(
        info, "m_strRemark", "m_strOrderRemark", "m_strUserOrderID",
        "m_strUserOrderId", "userOrderId", "user_order_id", "remark", "Remark"
    ) or "")
    order_id = str(_field(
        info, "m_strOrderSysID", "m_strOrderID", "m_nOrderID",
        "orderID", "order_id", "orderId"
    ) or "")
    return token, order_id


def _find_callback_plan(info):
    token, order_id = _callback_identity(info)
    plan = _SESSION.get("token_to_plan", {}).get(token)
    if plan is None and order_id:
        plan = _SESSION.get("order_id_to_plan", {}).get(order_id)
    if plan is not None:
        return plan, token, order_id, False

    code = _normalize_code(_field(
        info, "m_strInstrumentID", "m_strOrderCode", "orderCode",
        "stock_code", "code"
    ))
    quantity = _float_value(_field(
        info, "m_nVolumeTotalOriginal", "m_nVolume", "volume",
        "quantity", "order_volume"
    ))
    price = _float_value(_field(
        info, "m_dPrice", "price", "order_price", "m_dOrderPrice"
    ))
    candidates = []
    for candidate in _SESSION.get("plans", []):
        if candidate.get("stock_code") != code:
            continue
        if quantity > 0 and int(quantity) != int(candidate.get("quantity") or 0):
            continue
        if price > 0 and abs(price - float(candidate.get("limit_up_price") or 0.0)) >= 0.0001:
            continue
        candidates.append(candidate)
    if len(candidates) == 1:
        return candidates[0], token, order_id, True
    return None, token, order_id, False


def order_callback(ContextInfo, orderInfo):
    plan, token, order_id, fallback = _find_callback_plan(orderInfo)
    if plan is None:
        _SESSION["unmatched_callback_count"] = (
            _SESSION.get("unmatched_callback_count", 0) + 1
        )
        now = time.monotonic()
        last_log = _SESSION.get("unmatched_callback_last_log", 0.0)
        if (not last_log or
                now - last_log >= UNMATCHED_CALLBACK_LOG_INTERVAL_SECONDS):
            _SESSION["unmatched_callback_last_log"] = now
            _log("ORDER_CALLBACK_UNMATCHED count=%s id=%s token=%s" % (
                _SESSION["unmatched_callback_count"], order_id, token
            ))
        return
    if fallback:
        _log("ORDER_CALLBACK_ASSOCIATED_BY_FIELDS id=%s code=%s" % (
            order_id, plan["stock_code"]
        ))
    message = _field(
        orderInfo, "m_strStatusMsg", "m_strErrorMsg", "m_strOrderStatusMsg",
        "status_msg", "error_msg", "statusMessage", "message"
    )
    if order_id:
        plan["order_id"] = order_id
        _SESSION["order_id_to_plan"][order_id] = plan
    callback_key = token or order_id
    if callback_key and callback_key in _SESSION["handled_callbacks"]:
        return
    if _is_preopen_rejection(message):
        if callback_key:
            _SESSION["handled_callbacks"].add(callback_key)
        if plan["attempts"] <= MAX_PREOPEN_RETRIES:
            plan["status"] = "retry_wait"
            _SESSION["retry_due"][plan["row_no"]] = (
                time.monotonic() + max(0, RETRY_DELAY_MS) / 1000.0
            )
            _log("PREOPEN_REJECT row=%s attempt=%s retry_at_delay_ms=%s message=%s" % (
                plan["row_no"], plan["attempts"], RETRY_DELAY_MS, message
            ))
        else:
            plan["status"] = "rejected_preopen_max_retries"
            _log("PREOPEN_REJECT_FINAL row=%s attempts=%s message=%s" % (
                plan["row_no"], plan["attempts"], message
            ))
    else:
        plan["status"] = "callback_received"
        _log("ORDER_CALLBACK row=%s id=%s status=%s message=%s; no automatic retry" % (
            plan["row_no"], order_id,
            _field(orderInfo, "m_nOrderStatus", "status", "order_status"), message
        ))
    _update_reserved_order(plan)


def deal_callback(ContextInfo, dealInfo):
    plan, token, order_id, fallback = _find_callback_plan(dealInfo)
    if plan is None:
        _log("DEAL_CALLBACK_UNMATCHED id=%s token=%s" % (order_id, token))
        return
    if fallback:
        _log("DEAL_CALLBACK_ASSOCIATED_BY_FIELDS id=%s code=%s" % (
            order_id, plan["stock_code"]
        ))
    plan["status"] = "filled"
    if order_id:
        plan["order_id"] = order_id
    _update_reserved_order(plan)
    _log("DEAL_CALLBACK row=%s id=%s qty=%s" % (
        plan["row_no"], order_id,
        _field(dealInfo, "m_nVolume", "volume", "deal_volume")
    ))


def stop(C):
    _cancel_scheduler(C)
    if _SESSION.get("reserved"):
        phase = _SESSION.get("phase", "stopped")
        _persist_outcomes("stopped_%s" % phase.lower())
    _SESSION["phase"] = "STOPPED"
    _log("STOP")
