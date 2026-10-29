# coding: gbk
"""Native QMT model for overnight limit-up buy orders."""

import datetime
import decimal
import time
import uuid


# ------------------------- operator configuration -------------------------
ENABLE_LIVE_ORDERS = True
ACCOUNT_ID = "32011276"
ACCOUNT_TYPE = "stock"
STRATEGY_NAME = "overnight_limit_up_buy_native"
ORDER_TYPE_BUY = 1101
PRICE_TYPE_LIMIT = 11
QUICK_TRADE = 2
ORDER_STATUS_PART_CANCEL = 53
ORDER_STATUS_CANCELED = 54
ORDER_STATUS_PART_SUCCEEDED = 55
ORDER_STATUS_SUCCEEDED = 56
ORDER_STATUS_JUNK = 57
TERMINAL_ORDER_STATUSES = (
    ORDER_STATUS_PART_CANCEL, ORDER_STATUS_CANCELED,
    ORDER_STATUS_PART_SUCCEEDED, ORDER_STATUS_SUCCEEDED, ORDER_STATUS_JUNK,
)


class _PriceNotReady(ValueError):
    pass

# Example only: replace the date, codes, and amounts before enabling a daily plan.
# PLAN_TRADE_DATE = "YYYYMMDD"
# ORDER_PLAN = (
#     ("603005", 26000),
#     ("000530", 26000),
# )
# Update the active values below before compiling the model for a trading day.
PLAN_TRADE_DATE = ""
ORDER_PLAN = ()

SUBMIT_HOUR = 8
SUBMIT_MINUTE = 30
LATEST_SUBMIT_HOUR = 15
LATEST_SUBMIT_MINUTE = 0
MAX_REJECT_RETRIES = 2
RETRY_DELAY_MS = 100
MAX_ORDERS = 500
PREFLIGHT_ROWS_PER_TURN = 5
SUBMIT_ROWS_PER_TURN = MAX_ORDERS
SCHEDULER_INTERVAL_MS = 100
PRICE_RETRY_INTERVAL_MS = 500
SUBMISSION_PREP_LEAD_MS = 2000
SCHEDULE_NAME = "qmt_overnight_limit_up_buy_drive"
UNMATCHED_CALLBACK_LOG_INTERVAL_SECONDS = 5


# ------------------------------ model state -------------------------------
_CTX = None
_SESSION = {}


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


def _int_value(value):
    try:
        number = float(value)
        if number == number and number not in (float("inf"), float("-inf")):
            return int(number)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _order_status_value(source):
    return _int_value(_field(
        source, "m_nOrderStatus", "order_status", "m_strOrderStatus", "status"
    ))


def _traded_volume(source):
    return max(0, int(_float_value(_field(
        source, "m_nVolumeTraded", "traded_volume", "tradedVolume",
        "volumeTraded", "m_nTradeVolume"
    ))))


def _is_terminal_order_status(status):
    return status in TERMINAL_ORDER_STATUSES


def _reject_callback_key(row_no, attempt):
    return "reject:%s:%s" % (row_no, attempt.get("attempt", ""))


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


def _parse_order_plan(trade_day):
    plan_day = str(PLAN_TRADE_DATE or "").strip()
    if len(plan_day) != 8 or not plan_day.isdigit():
        raise ValueError("PLAN_TRADE_DATE must be YYYYMMDD")
    if plan_day != trade_day:
        raise ValueError("plan date %s does not match today %s" % (
            plan_day, trade_day
        ))
    if not isinstance(ORDER_PLAN, (list, tuple)):
        raise ValueError("ORDER_PLAN must be a list or tuple")
    if not ORDER_PLAN:
        raise ValueError("ORDER_PLAN is empty")
    rows = []
    for row_no, entry in enumerate(ORDER_PLAN, 1):
        if isinstance(entry, dict):
            raw_code = entry.get("stock_code")
            raw_amount = entry.get("amount")
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            raw_code, raw_amount = entry
        else:
            raise ValueError("plan row %s must contain stock_code and amount" % row_no)
        code = _normalize_code(raw_code)
        if not code:
            raise ValueError("row %s invalid stock_code" % row_no)
        amount = _parse_amount(raw_amount, row_no)
        rows.append({
            "row_no": row_no,
            "stock_code": code,
            "amount": amount,
        })
        if len(rows) > MAX_ORDERS:
            raise ValueError("ORDER_PLAN exceeds MAX_ORDERS=%s" % MAX_ORDERS)
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
    if detail is None:
        raise _PriceNotReady("instrument detail is not ready for %s" % stock_code)
    if not isinstance(detail, dict):
        raise ValueError("instrument detail is not a dictionary for %s" % stock_code)
    return detail


def _limit_up_price(detail, trade_day, stock_code):
    detail_day = _normalize_day(_field(
        detail, "TradingDay", "tradingDay", "m_strTradingDay", "tradeDate"
    ))
    if not detail_day:
        raise _PriceNotReady("instrument detail TradingDay is missing for %s" % stock_code)
    if detail_day != trade_day:
        raise _PriceNotReady("instrument detail day mismatch for %s: expected=%s got=%s" % (
            stock_code, trade_day, detail_day
        ))
    price = _float_value(_field(
        detail, "UpStopPrice", "upStopPrice", "upLimit", "up_limit",
        "upperLimit", "limitUp", "upLimitPrice"
    ))
    if price <= 0:
        raise _PriceNotReady("UpStopPrice is not ready for %s" % stock_code)
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
    """Query native QMT orders before allowing a live run to submit."""
    getter = globals().get("get_trade_detail_data")
    if not callable(getter):
        _log("STARTUP_ORDER_QUERY unavailable=api_missing")
        return None
    try:
        data = getter(ACCOUNT_ID, ACCOUNT_TYPE, "order")
    except Exception as exc:
        _log("STARTUP_ORDER_QUERY_FAILED reason=%r" % (exc,))
        return None
    if data is None:
        _log("STARTUP_ORDER_QUERY unavailable=none")
        return None
    if isinstance(data, (list, tuple)):
        records = list(data)
    else:
        records = [data]
    _log("STARTUP_ORDER_QUERY count=%s" % len(records))
    return records


def _broker_strategy_name(record):
    return str(_field(
        record, "m_strStrategyName", "strategyName", "strategy_name"
    ) or "").strip()


def _check_startup_order_records(records):
    for row_no, record in enumerate(records, 1):
        strategy = _broker_strategy_name(record)
        if not strategy:
            return False, "order row %s has no strategy identity" % row_no
        if (strategy == STRATEGY_NAME
                or strategy.startswith(STRATEGY_NAME + "_&&&_")):
            return False, "strategy order found in query row %s" % row_no
    return True, "no prior strategy orders; all records are identified"


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


def _preflight_step(C, now=None):
    step_started_mono = time.monotonic()
    now = now or datetime.datetime.now()
    if not _SESSION.get("startup_order_check_done"):
        day = _SESSION["trade_day"]
        if ENABLE_LIVE_ORDERS:
            records = _query_order_records()
            if records is None:
                _abort("startup order query is unavailable")
                return
            safe_to_submit, reason = _check_startup_order_records(records)
            if not safe_to_submit:
                _abort("startup order check failed: %s" % reason)
                return
            _SESSION["startup_orders_clear"] = True
            _log("STARTUP_ORDER_CHECK_CLEAR day=%s records=%s" % (
                day, len(records)
            ))
        else:
            _SESSION["startup_orders_clear"] = True
            _log("STARTUP_ORDER_CHECK_SKIPPED reason=live_orders_disabled")
        _SESSION["startup_order_check_done"] = True
        _log("TRADING_DAY_CHECK_SKIPPED day=%s; clock schedule controls submission" % day)

    retry_due = _SESSION.get("preflight_retry_due", 0.0)
    if retry_due and time.monotonic() < retry_due:
        return

    rows = _SESSION["orders"]
    start = _SESSION["preflight_cursor"]
    end = min(len(rows), start + PREFLIGHT_ROWS_PER_TURN)
    prepared = []
    waiting = []
    failures = []
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
            prepared.append({
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
                "order_status": None,
                "traded_volume": 0,
                "fill_uncertain": False,
                "attempt_history": [],
            })
        except _PriceNotReady as exc:
            waiting.append((order["row_no"], code, str(exc)))
        except Exception as exc:
            failures.append((order["row_no"], code, str(exc)))

    for row_no, code, reason in failures:
        _SESSION["failures"].append(row_no)
        _log("PREFLIGHT_ROW_FAILED row=%s code=%s reason=%s" % (
            row_no, code, reason
        ))
    if failures:
        _abort("preflight failed rows=%s" % len(_SESSION["failures"]))
        return

    waiting_rows = [(row_no, code) for row_no, code, unused in waiting]
    previous_waiting = _SESSION.get("price_waiting_rows", [])
    if waiting:
        for row_no, code, reason in waiting:
            if (row_no, code) not in previous_waiting:
                _log("PRICE_NOT_READY row=%s code=%s reason=%s retry_ms=%s" % (
                    row_no, code, reason, PRICE_RETRY_INTERVAL_MS
                ))
        _SESSION["price_waiting_rows"] = waiting_rows
        _SESSION["preflight_retry_due"] = (
            time.monotonic() + max(1, PRICE_RETRY_INTERVAL_MS) / 1000.0
        )
        return

    _SESSION["price_waiting_rows"] = []
    _SESSION["preflight_retry_due"] = 0.0
    for plan in prepared:
        _SESSION["plans"].append(plan)
        _SESSION["plan_by_row"][plan["row_no"]] = plan
        _log("PLAN row=%s code=%s budget=%.2f limit_up=%.3f qty=%s estimate=%.2f" % (
            plan["row_no"], plan["stock_code"], plan["amount"],
            plan["limit_up_price"], plan["quantity"], plan["estimated_amount"]
        ))
    _SESSION["preflight_cursor"] = end

    if end < len(rows):
        _log("PREFLIGHT_PROGRESS checked=%s total=%s" % (end, len(rows)))
        return
    preparation_deadline = now.replace(
        hour=SUBMIT_HOUR, minute=SUBMIT_MINUTE, second=0, microsecond=0
    ) - datetime.timedelta(milliseconds=SUBMISSION_PREP_LEAD_MS)
    elapsed = max(0.0, time.monotonic() - step_started_mono)
    if now + datetime.timedelta(seconds=elapsed) >= preparation_deadline:
        _abort("price preflight missed the 08:29:58 preparation deadline")
        return
    _SESSION["phase"] = "WAITING_FOR_SUBMIT_TIME"


def _reserve_batch():
    if not _SESSION.get("startup_order_check_done"):
        _abort("startup order check has not completed")
        return False
    if ENABLE_LIVE_ORDERS and not _SESSION.get("startup_orders_clear"):
        _abort("startup order check did not authorize live submission")
        return False
    if _SESSION.get("reserved"):
        _abort("submission reservation already exists in this model session")
        return False
    _SESSION["reserved"] = True
    _log("RUNTIME_RESERVATION day=%s rows=%s" % (
        _SESSION["trade_day"], len(_SESSION["plans"])
    ))
    return True


def _prepare_initial_attempts(C):
    sender = globals().get("passorder")
    if ENABLE_LIVE_ORDERS and not callable(sender):
        _abort("passorder is unavailable before submission")
        return False
    for plan in _SESSION["plans"]:
        if not ENABLE_LIVE_ORDERS:
            continue
        attempt_no = plan["attempts"] + 1
        token = "%s_%s_%s_%s" % (
            _SESSION["trade_day"], plan["row_no"], attempt_no,
            uuid.uuid4().hex[:8]
        )
        attempt = {
            "attempt": attempt_no,
            "token": token,
            "order_id": "",
            "status": "prepared",
            "order_status": None,
            "traded_volume": 0,
            "fill_uncertain": False,
        }
        plan["prepared_attempt"] = attempt
        plan["prepared_sender"] = sender
        plan["prepared_args"] = (
            23, ORDER_TYPE_BUY, ACCOUNT_ID, plan["stock_code"], PRICE_TYPE_LIMIT,
            plan["limit_up_price"], plan["quantity"], STRATEGY_NAME,
            QUICK_TRADE, token, C
        )
        _SESSION["token_to_plan"][token] = plan
        _SESSION["token_to_attempt"][token] = attempt
    return True


def _prepare_submission(C, now):
    started_mono = time.monotonic()
    required = sum(item["estimated_amount"] for item in _SESSION["plans"])
    if ENABLE_LIVE_ORDERS:
        try:
            available = _available_cash(C)
        except Exception as exc:
            _abort("funding preflight failed: %r" % (exc,))
            return False
        _SESSION["available_cash"] = available
        if available + 0.0001 < required:
            _abort("insufficient available cash: available=%.2f required=%.2f" % (
                available, required
            ))
            return False
    if not _reserve_batch():
        return False
    if not _prepare_initial_attempts(C):
        return False
    elapsed = max(0.0, time.monotonic() - started_mono)
    finished = now + datetime.timedelta(seconds=elapsed)
    submit_at = now.replace(
        hour=SUBMIT_HOUR, minute=SUBMIT_MINUTE, second=0, microsecond=0
    )
    if finished >= submit_at:
        _abort("submission preparation crossed the 08:30 cutoff")
        return False
    _SESSION["submission_prepared"] = True
    _format_plan()
    _log("SUBMISSION_ARMED time=%02d:%02d rows=%s" % (
        SUBMIT_HOUR, SUBMIT_MINUTE, len(_SESSION["plans"])
    ))
    return True


def _is_preopen_rejection(message):
    text = str(message or "").lower()
    markers = (
        "\u672a\u5230\u5f00\u5e02", "\u5c1a\u672a\u5f00\u5e02", "\u672a\u5f00\u5e02",
        "\u4ea4\u6613\u65f6\u95f4\u672a\u5230", "market is not open", "not open yet",
    )
    return any(marker in text for marker in markers)


def _submit_plan(C, plan):
    attempt = plan.pop("prepared_attempt", None)
    prepared_args = plan.pop("prepared_args", None)
    sender = plan.pop("prepared_sender", None)
    if attempt is None:
        plan["attempts"] += 1
        token = "%s_%s_%s_%s" % (
            _SESSION["trade_day"], plan["row_no"], plan["attempts"], uuid.uuid4().hex[:8]
        )
        attempt = {
            "attempt": plan["attempts"],
            "token": token,
            "order_id": "",
            "status": "sending",
            "order_status": None,
            "traded_volume": 0,
            "fill_uncertain": False,
        }
        plan.setdefault("attempt_history", []).append(attempt)
        sender = globals().get("passorder")
    else:
        token = attempt["token"]
        plan["attempts"] = attempt["attempt"]
        plan.setdefault("attempt_history", []).append(attempt)
    plan["last_token"] = token
    plan["order_id"] = ""
    plan["order_status"] = None
    plan["traded_volume"] = 0
    plan["fill_uncertain"] = False
    plan["status"] = "sending"
    attempt["status"] = "sending"
    _SESSION["token_to_plan"][token] = plan
    _SESSION["token_to_attempt"][token] = attempt
    if not callable(sender):
        plan["status"] = "unknown"
        attempt["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _log("SEND_UNKNOWN reason=passorder_unavailable row=%s; remaining rows halted" %
             plan["row_no"])
        return False
    try:
        if prepared_args is None:
            prepared_args = (
                23, ORDER_TYPE_BUY, ACCOUNT_ID, plan["stock_code"], PRICE_TYPE_LIMIT,
                plan["limit_up_price"], plan["quantity"], STRATEGY_NAME,
                QUICK_TRADE, token, C
            )
        result = sender(*prepared_args)
    except Exception as exc:
        plan["status"] = "unknown"
        attempt["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _log("SEND_UNKNOWN row=%s token=%s error=%r; remaining rows halted" % (
            plan["row_no"], token, exc
        ))
        return False

    if result is False or (isinstance(result, (int, float)) and result < 0) or str(result) == "-1":
        plan["status"] = "unknown"
        attempt["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _log("SEND_UNKNOWN row=%s token=%s return=%r; remaining rows halted" % (
            plan["row_no"], token, result
        ))
        return False

    if result == 0:
        plan["status"] = "accepted_no_id"
        attempt["status"] = "accepted_no_id"
        _log("ORDER_ACCEPTED_NO_ID row=%s code=%s price=%.3f qty=%s attempt=%s token=%s; "
             "waiting for broker callback" % (
                 plan["row_no"], plan["stock_code"], plan["limit_up_price"],
                 plan["quantity"], plan["attempts"], token
             ))
        return True

    plan["status"] = "sent"
    attempt["status"] = "sent"
    if isinstance(result, (str, int)) and str(result) not in ("", "0", "-1"):
        plan["order_id"] = str(result)
        attempt["order_id"] = str(result)
        _SESSION["order_id_to_plan"][str(result)] = plan
        _SESSION["order_id_to_attempt"][str(result)] = attempt
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
            if plan.get("fill_uncertain") or plan.get("traded_volume", 0) > 0:
                plan["status"] = (
                    "partial_filled" if plan.get("traded_volume", 0) > 0
                    else "fill_unverified"
                )
                _log("RETRY_CANCELED_AFTER_FILL row=%s traded=%s fill_uncertain=%s" % (
                    row_no, plan.get("traded_volume", 0),
                    plan.get("fill_uncertain", False)
                ))
                continue
            if not _submit_plan(C, plan):
                return
            return

    sent_this_turn = 0
    while (_SESSION["dispatch_cursor"] < len(_SESSION["plans"])
           and sent_this_turn < SUBMIT_ROWS_PER_TURN):
        plan = _SESSION["plans"][_SESSION["dispatch_cursor"]]
        _SESSION["dispatch_cursor"] += 1
        if plan.get("status") == "retry_wait":
            sent_this_turn += 1
            continue
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
                _log("DISPATCHED_MONITORING rows=%s; waiting for asynchronous order callbacks" %
                     len(_SESSION["plans"]))
        else:
            _SESSION["phase"] = "COMPLETE"
            _log("DISPATCH_COMPLETE rows=%s live=%s" % (
                len(_SESSION["plans"]), ENABLE_LIVE_ORDERS
            ))


def _drive(C, now=None):
    if not _SESSION or _SESSION.get("phase") in (
            "ABORTED", "PARTIAL_STOP", "COMPLETE", "STOPPED"):
        return
    clock_is_injected = now is not None
    now = now or datetime.datetime.now()
    phase = _SESSION.get("phase")
    if phase == "PREFLIGHT":
        submit_at = now.replace(
            hour=SUBMIT_HOUR, minute=SUBMIT_MINUTE, second=0, microsecond=0
        )
        preparation_deadline = submit_at - datetime.timedelta(
            milliseconds=SUBMISSION_PREP_LEAD_MS
        )
        if now >= preparation_deadline:
            waiting = _SESSION.get("price_waiting_rows", [])
            _abort("price preflight missed the 08:29:58 preparation deadline; "
                   "waiting_rows=%s" % waiting)
            return
        _preflight_step(C, now=now)
        if _SESSION.get("phase") in ("ABORTED", "PARTIAL_STOP", "COMPLETE"):
            _cancel_scheduler(C)
            return
        if not clock_is_injected:
            now = datetime.datetime.now()
        phase = _SESSION.get("phase")
        if phase == "PREFLIGHT":
            return
    if phase == "MONITORING":
        if (now.hour, now.minute) >= (LATEST_SUBMIT_HOUR, LATEST_SUBMIT_MINUTE):
            _SESSION["retry_due"].clear()
            _SESSION["phase"] = "COMPLETE"
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
            arm_time = now.replace(
                hour=SUBMIT_HOUR, minute=SUBMIT_MINUTE, second=0, microsecond=0
            ) - datetime.timedelta(milliseconds=SUBMISSION_PREP_LEAD_MS)
            if (not _SESSION.get("submission_prepared") and now >= arm_time
                    and not _prepare_submission(C, now)):
                _cancel_scheduler(C)
            return
        if not _SESSION.get("submission_prepared"):
            _abort("submission preparation did not finish before 08:30")
            return
        _SESSION["phase"] = "DISPATCHING"
        phase = "DISPATCHING"
    if phase == "DISPATCHING":
        if (now.hour, now.minute) >= (LATEST_SUBMIT_HOUR, LATEST_SUBMIT_MINUTE):
            _SESSION["retry_due"].clear()
            _SESSION["phase"] = "COMPLETE"
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
    global _CTX, _SESSION
    _CTX = C
    today = datetime.date.today().strftime("%Y%m%d")
    _SESSION = {
        "phase": "PREFLIGHT",
        "trade_day": today,
        "orders": [],
        "plans": [],
        "plan_by_row": {},
        "preflight_cursor": 0,
        "price_waiting_rows": [],
        "preflight_retry_due": 0.0,
        "dispatch_cursor": 0,
        "price_cache": {},
        "failures": [],
        "startup_order_check_done": False,
        "available_cash": 0.0,
        "retry_due": {},
        "token_to_plan": {},
        "token_to_attempt": {},
        "order_id_to_plan": {},
        "order_id_to_attempt": {},
        "handled_callbacks": set(),
        "handled_deals": set(),
        "unmatched_callback_count": 0,
        "unmatched_callback_last_log": 0.0,
        "reserved": False,
        "submission_prepared": False,
        "task_id": None,
    }
    script_file = globals().get("__file__", "<absent>")
    _log("INIT native Python live=%s account_configured=%s script=%s" % (
        ENABLE_LIVE_ORDERS, bool(ACCOUNT_ID), script_file
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
        _SESSION["orders"] = _parse_order_plan(today)
    except Exception as exc:
        _abort("initialization failed: %r" % (exc,))
        return
    _log("ORDER_PLAN_LOADED rows=%s trade_day=%s" % (
        len(_SESSION["orders"]), today
    ))
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
        "m_strUserOrderId", "userOrderId", "user_order_id", "remark", "Remark",
        "order_remark"
    ) or "")
    order_id = str(_field(
        info, "m_strOrderSysID", "m_strOrderID", "m_nOrderID",
        "order_sysid", "orderID", "order_id", "orderId"
    ) or "")
    return token, order_id


def _find_callback_plan(info, is_deal=False):
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
    quantity = 0.0 if is_deal else _float_value(_field(
        info, "m_nVolumeTotalOriginal", "m_nVolumeTotal", "m_nVolume",
        "volume", "quantity", "order_volume"
    ))
    price = _float_value(_field(
        info, "m_dPrice", "price", "order_price", "m_dOrderPrice",
        "traded_price"
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


def _attempt_for_callback(plan, token, order_id):
    attempt = _SESSION.get("token_to_attempt", {}).get(token)
    if attempt is None and order_id:
        attempt = _SESSION.get("order_id_to_attempt", {}).get(order_id)
    if attempt is not None:
        return attempt
    history = plan.get("attempt_history", [])
    return history[-1] if history else None


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
    if (fallback and not token and not order_id
            and len(plan.get("attempt_history", [])) > 1):
        _log("ORDER_CALLBACK_AMBIGUOUS row=%s status=%s attempts=%s; ignored" % (
            plan["row_no"], _order_status_value(orderInfo), plan["attempts"]
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
    status = _order_status_value(orderInfo)
    traded = _traded_volume(orderInfo)
    if order_id:
        plan["order_id"] = order_id
        _SESSION["order_id_to_plan"][order_id] = plan
    attempt = _attempt_for_callback(plan, token, order_id)
    if attempt is None:
        attempt = {"attempt": plan.get("attempts", 0), "status": "unknown"}
        plan.setdefault("attempt_history", []).append(attempt)
    if order_id:
        attempt["order_id"] = order_id
        _SESSION["order_id_to_attempt"][order_id] = attempt
    if token:
        attempt["token"] = token
        _SESSION["token_to_attempt"][token] = attempt
    previous_status = _int_value(attempt.get("order_status"))
    previous_traded = _int_value(attempt.get("traded_volume")) or 0
    was_terminal = _is_terminal_order_status(previous_status)
    if was_terminal and traded <= previous_traded:
        return
    if status is not None and (not was_terminal or status == previous_status):
        attempt["order_status"] = status
        plan["order_status"] = status
    elif was_terminal:
        status = previous_status
    attempt["traded_volume"] = max(
        _int_value(attempt.get("traded_volume")) or 0, traded
    )
    plan["traded_volume"] = max(
        _int_value(plan.get("traded_volume")) or 0, traded
    )
    retryable_rejection = (
        status == ORDER_STATUS_JUNK
        or (status is None and _is_preopen_rejection(message))
    )
    if retryable_rejection:
        attempt["status"] = "rejected"
        reject_key = _reject_callback_key(plan["row_no"], attempt)
        if plan["traded_volume"] > 0 or plan.get("fill_uncertain"):
            _SESSION["handled_callbacks"].add(reject_key)
            _SESSION["retry_due"].pop(plan["row_no"], None)
            plan["status"] = "rejected_after_fill"
            _log("ORDER_JUNK_NO_RETRY row=%s attempt=%s traded=%s fill_uncertain=%s" % (
                plan["row_no"], plan["attempts"], plan["traded_volume"],
                plan.get("fill_uncertain", False)
            ))
        elif reject_key in _SESSION["handled_callbacks"]:
            return
        elif plan["attempts"] <= MAX_REJECT_RETRIES:
            _SESSION["handled_callbacks"].add(reject_key)
            plan["status"] = "retry_wait"
            _SESSION["retry_due"][plan["row_no"]] = (
                time.monotonic() + max(0, RETRY_DELAY_MS) / 1000.0
            )
            _log("ORDER_JUNK_RETRY_SCHEDULED row=%s attempt=%s retry=%s/%s "
                 "delay_ms=%s id=%s message=%s" % (
                plan["row_no"], plan["attempts"], plan["attempts"],
                MAX_REJECT_RETRIES, RETRY_DELAY_MS, order_id, message
            ))
        else:
            _SESSION["handled_callbacks"].add(reject_key)
            plan["status"] = "rejected_max_retries"
            _log("ORDER_JUNK_FINAL row=%s attempts=%s max_retries=%s id=%s message=%s" % (
                plan["row_no"], plan["attempts"], MAX_REJECT_RETRIES,
                order_id, message
            ))
    else:
        if status == ORDER_STATUS_SUCCEEDED or plan["traded_volume"] >= plan["quantity"]:
            plan["status"] = "filled"
            attempt["status"] = "filled"
        elif plan["traded_volume"] > 0 or status == ORDER_STATUS_PART_SUCCEEDED:
            plan["status"] = "partial_filled"
            attempt["status"] = "partial_filled"
        else:
            plan["status"] = "callback_received"
            if status is not None:
                attempt["status"] = (
                    "terminal" if _is_terminal_order_status(status) else "active"
                )
        _log("ORDER_CALLBACK row=%s id=%s status=%s traded=%s message=%s" % (
            plan["row_no"], order_id, status, plan["traded_volume"], message
        ))


def deal_callback(ContextInfo, dealInfo):
    plan, token, order_id, fallback = _find_callback_plan(dealInfo, is_deal=True)
    if plan is None:
        _log("DEAL_CALLBACK_UNMATCHED id=%s token=%s" % (order_id, token))
        return
    if fallback:
        _log("DEAL_CALLBACK_ASSOCIATED_BY_FIELDS id=%s code=%s" % (
            order_id, plan["stock_code"]
        ))
    deal_id = str(_field(
        dealInfo, "m_strTradeID", "m_strDealID", "tradeID", "dealID",
        "tradedID", "traded_id"
    ) or "").strip()
    attempt = _attempt_for_callback(plan, token, order_id)
    if not deal_id:
        plan["fill_uncertain"] = True
        plan["status"] = "fill_unverified"
        _SESSION["retry_due"].pop(plan["row_no"], None)
        if attempt is not None:
            attempt["fill_uncertain"] = True
        _log("DEAL_CALLBACK_UNVERIFIED row=%s id=%s; retries blocked" % (
            plan["row_no"], order_id
        ))
        return
    if deal_id in _SESSION["handled_deals"]:
        return
    _SESSION["handled_deals"].add(deal_id)
    deal_volume = max(0, _int_value(_field(
        dealInfo, "m_nVolume", "m_nTradeVolume", "deal_volume",
        "traded_volume", "tradedVolume", "volume"
    )) or 0)
    if deal_volume <= 0:
        plan["fill_uncertain"] = True
        plan["status"] = "fill_unverified"
    else:
        plan["traded_volume"] = (
            _int_value(plan.get("traded_volume")) or 0
        ) + deal_volume
        plan["status"] = (
            "filled" if plan["traded_volume"] >= plan["quantity"]
            else "partial_filled"
        )
    if plan.get("fill_uncertain") or deal_volume > 0:
        _SESSION["retry_due"].pop(plan["row_no"], None)
    if attempt is not None and deal_volume > 0:
        attempt["traded_volume"] = (
            _int_value(attempt.get("traded_volume")) or 0
        ) + deal_volume
    if order_id:
        plan["order_id"] = order_id
        _SESSION["order_id_to_plan"][order_id] = plan
        if attempt is not None:
            _SESSION["order_id_to_attempt"][order_id] = attempt
    _log("DEAL_CALLBACK row=%s id=%s qty=%s" % (
        plan["row_no"], order_id, deal_volume
    ))


def stop(C):
    _cancel_scheduler(C)
    _SESSION["phase"] = "STOPPED"
    _log("STOP")
