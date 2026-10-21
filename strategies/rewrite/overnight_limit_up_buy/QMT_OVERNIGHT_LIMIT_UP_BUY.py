# coding: gbk
"""Native QMT model for overnight limit-up buy orders."""

import csv
import datetime
import decimal
import json
import os
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

QMT_HOME = u"C:/\u5149\u5927\u8bc1\u5238\u91d1\u9633\u5149QMT\u5b9e\u76d8"
ORDER_CSV_PATH = os.path.join(QMT_HOME, "python", "overnight_orders.csv")
STATE_FILE_PATH = os.path.join(
    QMT_HOME, "userdata", "log", "qmt_overnight_limit_up_buy_state.json"
)

SUBMIT_HOUR = 8
SUBMIT_MINUTE = 30
LATEST_SUBMIT_HOUR = 15
LATEST_SUBMIT_MINUTE = 0
MAX_REJECT_RETRIES = 2
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


def _is_terminal_zero_fill(status, traded_volume):
    return status in TERMINAL_ORDER_STATUSES and int(traded_volume or 0) == 0


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
        _log("STATE_ORDER_QUERY_RESULT unavailable=api_missing")
        return None
    try:
        data = getter(ACCOUNT_ID, ACCOUNT_TYPE, "order")
    except Exception as exc:
        _log("STATE_ORDER_QUERY_FAILED reason=%r" % (exc,))
        return None
    if data is None:
        _log("STATE_ORDER_QUERY_RESULT unavailable=none")
        return None
    if isinstance(data, (list, tuple)):
        records = list(data)
    else:
        records = [data]
    _log("STATE_ORDER_QUERY_RESULT count=%s" % len(records))
    return records


def _broker_order_id(record):
    return str(_field(
        record, "m_strOrderSysID", "order_sysid", "m_strOrderID",
        "m_nOrderID", "orderID", "order_id", "orderId"
    ) or "").strip()


def _broker_order_token(record):
    return str(_field(
        record, "m_strRemark", "m_strOrderRemark", "m_strUserOrderID",
        "m_strUserOrderId", "userOrderId", "user_order_id", "remark",
        "order_remark"
    ) or "").strip()


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
    token = _broker_order_token(record)
    expected_token = str(plan.get("last_token") or "").strip()
    strategy_matches = (
        not strategy
        or strategy == STRATEGY_NAME
        or (
            strategy.startswith(STRATEGY_NAME + "_&&&_")
            and (not expected_token or
                 strategy.startswith(STRATEGY_NAME + "_&&&_" + expected_token + "_&&&_"))
        )
    )
    if not strategy_matches:
        return False
    if token and token == expected_token:
        return True
    quantity = _float_value(_field(
        record, "m_nVolumeTotalOriginal", "m_nVolumeTotal", "m_nVolume",
        "volume", "quantity", "order_volume"
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


def _saved_attempt_history(saved):
    history = saved.get("attempt_history")
    if isinstance(history, list) and history:
        return [dict(item) for item in history if isinstance(item, dict)]
    attempts = _int_value(saved.get("attempts")) or 0
    if attempts <= 0 and (saved.get("order_id") or saved.get("last_token")):
        attempts = 1
    if attempts <= 0:
        return []
    return [{
        "attempt": attempts,
        "token": saved.get("last_token", ""),
        "order_id": saved.get("order_id", ""),
        "status": saved.get("status", ""),
        "order_status": saved.get("order_status"),
        "traded_volume": saved.get("traded_volume", 0),
        "fill_uncertain": saved.get("fill_uncertain", False),
    }]


def _find_broker_order_for_attempt(records, saved, attempt):
    order_id = str(attempt.get("order_id") or "").strip()
    if order_id:
        matches = [record for record in records
                   if _broker_order_id(record) == order_id]
        if len(matches) == 1:
            return matches[0]

    token = str(attempt.get("token") or "").strip()
    if token:
        matches = [record for record in records
                   if _broker_order_token(record) == token]
        if len(matches) == 1:
            return matches[0]

    plan = {
        "stock_code": saved.get("stock_code"),
        "quantity": saved.get("quantity"),
        "limit_up_price": saved.get("limit_up_price"),
        "last_token": token,
    }
    matches = [record for record in records
               if _order_record_matches_plan(record, plan)]
    return matches[0] if len(matches) == 1 else None


def _broker_order_match_counts(records, saved, attempt):
    order_id = str(attempt.get("order_id") or "").strip()
    token = str(attempt.get("token") or "").strip()
    plan = {
        "stock_code": saved.get("stock_code"),
        "quantity": saved.get("quantity"),
        "limit_up_price": saved.get("limit_up_price"),
        "last_token": token,
    }
    return (
        sum(1 for record in records if order_id and _broker_order_id(record) == order_id),
        sum(1 for record in records if token and _broker_order_token(record) == token),
        sum(1 for record in records if _order_record_matches_plan(record, plan)),
    )


def _reconcile_reservation(batch):
    orders = batch.get("orders", {}) if isinstance(batch, dict) else {}
    records = None
    histories = {}
    for row_key, saved in orders.items():
        if not isinstance(saved, dict):
            return False, {}, "invalid saved order row %s" % row_key
        if saved.get("fill_uncertain"):
            return False, {}, "fill state is uncertain for row %s" % row_key
        attempts = _int_value(saved.get("attempts")) or 0
        history = _saved_attempt_history(saved)
        if not history:
            histories[int(row_key)] = {"attempts": 0, "attempt_history": []}
            continue
        for attempt in history:
            if attempt.get("fill_uncertain"):
                return False, {}, "fill state is uncertain for row %s" % row_key
            status = _int_value(attempt.get("order_status"))
            traded = max(0, _int_value(attempt.get("traded_volume")) or 0)
            if not _is_terminal_zero_fill(status, traded):
                if records is None:
                    records = _query_order_records()
                if records is None:
                    return False, {}, "broker order query is unavailable"
                record = _find_broker_order_for_attempt(records, saved, attempt)
                if record is None:
                    id_count, token_count, plan_count = _broker_order_match_counts(
                        records, saved, attempt
                    )
                    return False, {}, (
                        "row %s order is not verifiably terminal "
                        "(query_records=%s id_matches=%s token_matches=%s "
                        "plan_matches=%s)" % (
                            row_key, len(records), id_count, token_count, plan_count
                        )
                    )
                record_status = _order_status_value(record)
                if record_status is not None:
                    status = record_status
                traded = max(traded, _traded_volume(record))
                attempt["order_status"] = status
                attempt["traded_volume"] = traded
                attempt["order_id"] = str(_field(
                    record, "m_strOrderSysID", "order_sysid", "m_strOrderID",
                    "m_nOrderID", "orderID", "order_id", "orderId"
                ) or attempt.get("order_id") or "")
                attempt["token"] = _broker_order_token(record) or attempt.get("token") or ""
            if not _is_terminal_zero_fill(status, traded):
                return False, {}, (
                    "row %s order status=%s traded=%s" % (row_key, status, traded)
                )
            attempt["status"] = (
                "rejected" if status == ORDER_STATUS_JUNK else "terminal"
            )
        histories[int(row_key)] = {
            "attempts": attempts,
            "attempt_history": history,
        }
        saved["attempts"] = attempts
        saved["attempt_history"] = history
        latest = history[-1]
        saved["order_status"] = latest.get("order_status")
        saved["traded_volume"] = latest.get("traded_volume", 0)
        saved["status"] = latest.get("status", saved.get("status", ""))
    return True, histories, "all submitted orders are terminal with zero fills"


def _current_csv_matches_batch(batch):
    saved_orders = batch.get("orders", {}) if isinstance(batch, dict) else {}
    saved_rows = sorted(saved_orders.items(), key=lambda item: int(item[0]))
    current_rows = _SESSION.get("orders", [])
    if len(saved_rows) != len(current_rows):
        return False
    for (saved_row, saved), current in zip(saved_rows, current_rows):
        if int(saved_row) != int(current["row_no"]):
            return False
        if saved.get("stock_code") != current.get("stock_code"):
            return False
        try:
            saved_amount = decimal.Decimal(str(saved.get("amount")))
        except (decimal.InvalidOperation, TypeError, ValueError):
            return False
        if saved_amount != current["amount"]:
            return False
    return True


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
            if not ENABLE_LIVE_ORDERS:
                _abort("a submission reservation already exists for trade day %s" % day)
                return
            safe_to_reopen, histories, reason = _reconcile_reservation(batch)
            if not safe_to_reopen:
                _abort("same-day reservation is still active, filled, or unknown: %s" % reason)
                return
            same_csv = _current_csv_matches_batch(batch)
            if same_csv:
                resume = {}
                for row_key, saved in batch.get("orders", {}).items():
                    row_no = int(row_key)
                    history_info = histories.get(row_no, {"attempts": 0,
                                                           "attempt_history": []})
                    attempts = history_info["attempts"]
                    history = history_info["attempt_history"]
                    if attempts <= 0:
                        continue
                    if not history or history[-1].get("order_status") != ORDER_STATUS_JUNK:
                        _abort("same-day row %s ended without a retryable junk order" % row_no)
                        return
                    if attempts >= 1 + MAX_REJECT_RETRIES:
                        _abort("same-day row %s already used the maximum %s retries" % (
                            row_no, MAX_REJECT_RETRIES
                        ))
                        return
                    resume[row_no] = {
                        "attempts": attempts,
                        "attempt_history": history,
                    }
                _SESSION["resume_attempts_by_row"] = resume
                if resume:
                    _log("STATE_RETRY_RESUME day=%s rows=%s verified=terminal_zero_fill" % (
                        day, len(resume)
                    ))
                else:
                    _log("STATE_RELEASED_UNSUBMITTED day=%s; no prior order was sent" % day)
            else:
                _SESSION["resume_attempts_by_row"] = {}
                _log("STATE_RELEASED_TERMINAL_ZERO_FILL day=%s csv_changed=true" % day)
            del _SESSION["state"]["trade_days"][day]
            try:
                _state_save()
            except Exception as exc:
                _abort("could not release terminal reservation: %r" % (exc,))
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
            resume = _SESSION.get("resume_attempts_by_row", {}).get(
                order["row_no"], {}
            )
            history = [dict(item) for item in resume.get("attempt_history", [])]
            _SESSION["plans"].append({
                "row_no": order["row_no"],
                "stock_code": code,
                "amount": amount,
                "limit_up_price": price,
                "quantity": quantity,
                "estimated_amount": estimate,
                "attempts": resume.get("attempts", 0),
                "status": "retry_wait" if history else "planned",
                "last_token": history[-1].get("token", "") if history else "",
                "order_id": history[-1].get("order_id", "") if history else "",
                "order_status": history[-1].get("order_status") if history else None,
                "traded_volume": 0,
                "fill_uncertain": False,
                "attempt_history": history,
            })
            plan = _SESSION["plans"][-1]
            _SESSION["plan_by_row"][order["row_no"]] = plan
            for attempt in history:
                token = str(attempt.get("token") or "")
                order_id = str(attempt.get("order_id") or "")
                if token:
                    _SESSION["token_to_plan"][token] = plan
                    _SESSION["token_to_attempt"][token] = attempt
                if order_id:
                    _SESSION["order_id_to_plan"][order_id] = plan
                    _SESSION["order_id_to_attempt"][order_id] = attempt
                if attempt.get("order_status") == ORDER_STATUS_JUNK:
                    _SESSION["handled_callbacks"].add(
                        _reject_callback_key(order["row_no"], attempt)
                    )
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

    now_mono = time.monotonic()
    for plan in _SESSION["plans"]:
        if plan.get("status") == "retry_wait":
            _SESSION["retry_due"][plan["row_no"]] = (
                now_mono + max(0, RETRY_DELAY_MS) / 1000.0
            )

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
            "status": item["status"],
            "attempts": item["attempts"],
            "last_token": item["last_token"],
            "order_id": item["order_id"],
            "order_status": item.get("order_status"),
            "traded_volume": item.get("traded_volume", 0),
            "fill_uncertain": item.get("fill_uncertain", False),
            "attempt_history": [dict(attempt)
                                for attempt in item.get("attempt_history", [])],
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
        saved["order_status"] = plan.get("order_status")
        saved["traded_volume"] = plan.get("traded_volume", 0)
        saved["fill_uncertain"] = plan.get("fill_uncertain", False)
        saved["attempt_history"] = [dict(attempt)
                                    for attempt in plan.get("attempt_history", [])]


def _persist_outcomes(batch_status):
    if not _SESSION.get("reserved"):
        return True
    batch = _STATE.get("trade_days", {}).get(_SESSION["trade_day"], {})
    batch["status"] = batch_status
    batch["updated_at"] = datetime.datetime.now().isoformat()
    for plan in _SESSION["plans"]:
        _update_reserved_order(plan)
    try:
        _state_save()
        return True
    except Exception as exc:
        _log("STATE_UPDATE_FAILED reason=%r; original reservation remains" % (exc,))
        return False


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
    plan["last_token"] = token
    plan["order_id"] = ""
    plan["order_status"] = None
    plan["traded_volume"] = 0
    plan["fill_uncertain"] = False
    plan["status"] = "sending"
    _update_reserved_order(plan)
    _SESSION["token_to_plan"][token] = plan
    _SESSION["token_to_attempt"][token] = attempt
    if not _persist_outcomes("dispatching"):
        plan["status"] = "unknown"
        attempt["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _log("SEND_UNKNOWN reason=attempt_state_not_persisted row=%s; passorder skipped" %
             plan["row_no"])
        return False
    sender = globals().get("passorder")
    if not callable(sender):
        plan["status"] = "unknown"
        attempt["status"] = "unknown"
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
        attempt["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _persist_outcomes("partial_unknown")
        _log("SEND_UNKNOWN row=%s token=%s error=%r; remaining rows halted" % (
            plan["row_no"], token, exc
        ))
        return False

    if result is False or (isinstance(result, (int, float)) and result < 0) or str(result) == "-1":
        plan["status"] = "unknown"
        attempt["status"] = "unknown"
        _SESSION["phase"] = "PARTIAL_STOP"
        _persist_outcomes("partial_unknown")
        _log("SEND_UNKNOWN row=%s token=%s return=%r; remaining rows halted" % (
            plan["row_no"], token, result
        ))
        return False

    if result == 0:
        plan["status"] = "accepted_no_id"
        attempt["status"] = "accepted_no_id"
        _update_reserved_order(plan)
        _persist_outcomes("monitoring")
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
    _update_reserved_order(plan)
    _persist_outcomes("monitoring")
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
                _update_reserved_order(plan)
                _persist_outcomes("monitoring")
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
        "token_to_attempt": {},
        "order_id_to_plan": {},
        "order_id_to_attempt": {},
        "handled_callbacks": set(),
        "handled_deals": set(),
        "unmatched_callback_count": 0,
        "unmatched_callback_last_log": 0.0,
        "reserved": False,
        "resume_attempts_by_row": {},
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
    _update_reserved_order(plan)
    _persist_outcomes("monitoring")


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
        _update_reserved_order(plan)
        _persist_outcomes("monitoring")
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
    _update_reserved_order(plan)
    _persist_outcomes("monitoring")
    _log("DEAL_CALLBACK row=%s id=%s qty=%s" % (
        plan["row_no"], order_id, deal_volume
    ))


def stop(C):
    _cancel_scheduler(C)
    if _SESSION.get("reserved"):
        phase = _SESSION.get("phase", "stopped")
        _persist_outcomes("stopped_%s" % phase.lower())
    _SESSION["phase"] = "STOPPED"
    _log("STOP")
