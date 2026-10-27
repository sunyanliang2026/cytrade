# coding: utf-8
"""Read-only native QMT L2 subscription replay and callback timing probe."""

import datetime as _dt
import io
import os
import time


TARGET_CODE = "002185.SH"
QMT_HOME = u"C:/\u5149\u5927\u8bc1\u5238\u91d1\u9633\u5149QMT\u5b9e\u76d8"

OPEN_TIMES = ((9, 30), (13, 0))
OBSERVE_AFTER_OPEN_SECONDS = 180.0
RESUBSCRIBE_DELAY_SECONDS = 0.30
POST_RESET_OBSERVE_SECONDS = 60.0
FINAL_UNSUBSCRIBE_SETTLE_SECONDS = 5.0
SUMMARY_INTERVAL_SECONDS = 5.0
IDLE_LOG_INTERVAL_SECONDS = 30.0
STALE_THRESHOLD_MS = 2000.0
SUBSCRIPTION_TIME_TOLERANCE_MS = 500.0
STALE_TRIGGER_CONSECUTIVE = 3
SAMPLE_LIMIT_PER_PHASE = 20
SAMPLE_FLUSH_PER_TICK = 5

_C = None
_LOG = None
_LOG_PATH = ""
_SUB_ID = None
_ACTIVE_PHASE = ""
_PHASES = {}
_SAMPLE_QUEUE = []
_CALLBACK_ERRORS = 0
_LAST_CALLBACK_ERROR = ""
_POST_UNSUBSCRIBE_CALLBACKS = 0
_CALLBACKS_DURING_UNSUBSCRIBE = 0
_POST_FINAL_UNSUBSCRIBE_CALLBACKS = 0
_LATE_OLD_SUBSCRIPTION_CALLBACKS = 0
_STALE_CALLBACK_STREAK = 0
_BACKLOG_RESET_SCHEDULED = False
_BACKLOG_RESET_TRIGGERED = False
_BACKLOG_DETECTED_EPOCH = None
_BACKLOG_FIRST_STALE_EPOCH = None
_BACKLOG_FIRST_STALE_DELAY_MS = None
_BACKLOG_FIRST_STALE_EVENT = ""
_BACKLOG_RESET_SCHEDULE_ERROR = ""
_UNSUBSCRIBE_RETURN_EPOCH = None
_UNSUBSCRIBE_STARTED_EPOCH = None
_FINAL_UNSUBSCRIBE_RETURN_EPOCH = None
_DONE = False
_TIMERS = {}
_TIMER_EXPECTED = {}
_LAST_IDLE_LOG_EPOCH = 0.0
_CYCLE_OPEN_EPOCH = None
_CYCLE_EPOCH = None


def _log(message):
    line = "%s %s" % (
        _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        message)
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


def _format_time(epoch):
    try:
        return _dt.datetime.fromtimestamp(float(epoch)).strftime("%H:%M:%S.%f")[:-3]
    except Exception:
        return "unknown"


def _format_delay(value):
    return "%.1f" % value if value is not None else "unknown"


def _open_log():
    global _LOG, _LOG_PATH
    log_dir = os.path.join(QMT_HOME, "userdata", "log")
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)
    day = _dt.datetime.now().strftime("%Y%m%d")
    code = "".join(ch for ch in TARGET_CODE if ch.isalnum())
    _LOG_PATH = os.path.join(
        log_dir, "qmt_l2_subscription_probe_%s_%s.log" % (day, code))
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


def _normalize_code(value):
    text = str(value or "").strip().upper()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return text
    suffix = ".SH" if digits.startswith(("5", "6", "9")) else ".SZ"
    return digits + suffix


def _unwrap(payload):
    if not isinstance(payload, dict):
        return payload
    if TARGET_CODE in payload and isinstance(payload.get(TARGET_CODE), dict):
        return payload[TARGET_CODE]
    for key, value in payload.items():
        if (_normalize_code(key) == TARGET_CODE and
                isinstance(value, dict)):
            return value
    return payload


def _event_epoch(value, reference_epoch=None):
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        try:
            return value.timestamp()
        except Exception:
            return None
    text = str(value).strip()
    whole, dot, fraction = text.partition(".")
    digits = "".join(ch for ch in whole if ch.isdigit())
    fraction = "".join(ch for ch in fraction if ch.isdigit()) if dot else ""
    if not digits:
        return None
    try:
        if len(digits) >= 14 and digits[:4].startswith(("19", "20")):
            stamp = _dt.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
            millis = (digits[14:] + fraction)[:3]
            return stamp.timestamp() + (int(millis) / 1000.0 if millis else 0.0)
    except Exception:
        return None
    try:
        numeric = float(value)
    except Exception:
        numeric = None
    if numeric is not None:
        if numeric >= 1000000000000.0:
            return numeric / 1000.0
        if numeric >= 1000000000.0:
            return numeric
    try:
        if len(digits) < 14:
            day = _dt.datetime.fromtimestamp(
                time.time() if reference_epoch is None else reference_epoch).date()
            hms = digits[:6].zfill(6)
            stamp = _dt.datetime.combine(
                day, _dt.datetime.strptime(hms, "%H%M%S").time())
            millis = (digits[6:] or fraction)[:3]
            return stamp.timestamp() + (int(millis) / 1000.0 if millis else 0.0)
    except Exception:
        return None
    return None


def _new_window(now):
    return {
        "started": now,
        "callbacks": 0,
        "before_boundary": 0,
        "stale": 0,
        "invalid_time": 0,
        "future": 0,
        "out_of_order": 0,
        "delay_count": 0,
        "delay_sum": 0.0,
        "delay_min": None,
        "delay_max": None,
        "gap_count": 0,
        "gap_sum": 0.0,
        "gap_max": None,
        "handler_sum": 0.0,
        "handler_max": 0.0,
        "handler_over_5ms": 0,
        "first_event": "",
        "last_event": "",
        "first_received": "",
        "last_received": "",
    }


def _new_phase(request_epoch):
    now = time.time()
    return {
        "request_epoch": request_epoch,
        "request_return_epoch": None,
        "subscribe_call_ms": None,
        "total_callbacks": 0,
        "before_boundary": 0,
        "stale": 0,
        "invalid_time": 0,
        "future": 0,
        "out_of_order": 0,
        "delay_count": 0,
        "delay_sum": 0.0,
        "delay_min": None,
        "delay_max": None,
        "last_delay_ms": None,
        "callback_handler_max": 0.0,
        "callback_handler_sum": 0.0,
        "callback_handler_over_5ms": 0,
        "gap_count": 0,
        "gap_sum": 0.0,
        "gap_max": None,
        "last_event_epoch": None,
        "last_received_epoch": None,
        "first_event": "",
        "first_received": "",
        "last_event": "",
        "last_received": "",
        "window": _new_window(now),
        "sample_count": 0,
    }


def _schedule_at(epoch, callback, name):
    if _C is None:
        return None
    old = _TIMERS.get(name)
    if old is not None:
        try:
            _C.cancel_schedule_run(old)
        except Exception:
            pass
    expected = float(epoch)

    def invoke(ContextInfo, cb=callback, timer_name=name, due=expected):
        _TIMERS.pop(timer_name, None)
        _TIMER_EXPECTED.pop(timer_name, None)
        try:
            cb(due)
        except Exception as exc:
            _log("[TIMER_CALLBACK_ERROR] name=%s error=%r" % (timer_name, exc))

    try:
        timer_id = _C.schedule_run(
            invoke, _dt.datetime.fromtimestamp(expected),
            repeat_times=0, name=name)
    except Exception as exc:
        _log("[SCHEDULE_FAILED] name=%s error=%r" % (name, exc))
        return None
    _TIMERS[name] = timer_id
    _TIMER_EXPECTED[name] = expected
    return timer_id


def _schedule_after(seconds, callback, name):
    return _schedule_at(time.time() + max(0.05, seconds), callback, name)


def _schedule_heartbeat():
    if _DONE:
        return
    _schedule_after(1.0, _heartbeat, "metrics")


def _choose_cycle(now):
    local_now = _dt.datetime.fromtimestamp(now)
    today = local_now.date()
    opens = []
    for hour, minute in OPEN_TIMES:
        opens.append(_dt.datetime.combine(
            today, _dt.time(hour=hour, minute=minute)).timestamp())
    for open_epoch in opens:
        cycle_epoch = open_epoch + OBSERVE_AFTER_OPEN_SECONDS
        if cycle_epoch > now + 10.0:
            return open_epoch, cycle_epoch
    return None, now + 60.0


def _subscribe_phase(phase_name):
    global _SUB_ID, _ACTIVE_PHASE
    request_epoch = time.time()
    started = time.time()
    _PHASES[phase_name] = _new_phase(request_epoch)
    _ACTIVE_PHASE = phase_name
    callback = lambda payload, sub_phase=phase_name, request=request_epoch: (
        _l2order_callback(payload, sub_phase, request))
    try:
        sub_id = _C.subscribe_quote(
            TARGET_CODE, period="l2order", dividend_type="follow",
            result_type="dict", callback=callback)
    except Exception as exc:
        _ACTIVE_PHASE = ""
        _log("[SUBSCRIBE_FAILED] phase=%s code=%s error=%r" % (
            phase_name, TARGET_CODE, exc))
        return False
    returned = time.time()
    metrics = _PHASES[phase_name]
    metrics["request_return_epoch"] = returned
    metrics["subscribe_call_ms"] = (returned - started) * 1000.0
    try:
        valid = int(sub_id) > 0
    except Exception:
        valid = bool(sub_id)
    if not valid:
        _ACTIVE_PHASE = ""
        _log("[SUBSCRIBE_FAILED] phase=%s code=%s return=%r call_ms=%.1f" % (
            phase_name, TARGET_CODE, sub_id, metrics["subscribe_call_ms"]))
        return False
    _SUB_ID = sub_id
    _log("[SUBSCRIBE_RETURN] phase=%s code=%s sub_id=%s request_time=%s " 
         "return_time=%s call_ms=%.1f period=l2order " 
         "args=stock_code,period,dividend_type,result_type,callback" % (
             phase_name, TARGET_CODE, sub_id, _format_time(request_epoch),
             _format_time(returned), metrics["subscribe_call_ms"]))
    return True


def _sample_callback(phase_name, data, received_epoch, event_raw, event_epoch,
                     delay_ms, before_boundary, request_epoch):
    metrics = _PHASES.get(phase_name)
    if metrics is None:
        metrics = _new_phase(request_epoch or received_epoch)
        _PHASES[phase_name] = metrics
    if metrics["sample_count"] >= SAMPLE_LIMIT_PER_PHASE:
        return None
    metrics["sample_count"] += 1
    return {
        "phase": phase_name,
        "sequence": metrics["total_callbacks"],
        "event_raw": str(event_raw or ""),
        "event_time": _format_time(event_epoch) if event_epoch is not None else "unknown",
        "received_time": _format_time(received_epoch),
        "delay_ms": delay_ms,
        "before_boundary": before_boundary,
        "received_epoch": received_epoch,
        "price": _field(data, "price", "entrustPrice", "orderPrice"),
        "volume": _field(data, "volume", "entrustVolume", "orderVolume"),
        "direction": _field(data, "entrustDirection", "direction", "side"),
        "request_to_callback_ms": (
            (received_epoch - request_epoch) * 1000.0
            if request_epoch is not None else None),
    }


def _observe_callback(payload, received_epoch, phase_name, boundary_epoch=None,
                      request_epoch=None):
    data = _unwrap(payload)
    event_raw = _field(data, "time", "timestamp", "stime")
    event_epoch = _event_epoch(event_raw, received_epoch)
    metrics = _PHASES.get(phase_name)
    if metrics is None:
        metrics = _new_phase(request_epoch or received_epoch)
        _PHASES[phase_name] = metrics
    window = metrics["window"]
    metrics["total_callbacks"] += 1
    window["callbacks"] += 1

    event_text = _format_time(event_epoch) if event_epoch is not None else "unknown"
    received_text = _format_time(received_epoch)
    delay_ms = ((received_epoch - event_epoch) * 1000.0
                if event_epoch is not None else None)
    metrics["last_delay_ms"] = delay_ms
    before_boundary = (
        boundary_epoch is not None and event_epoch is not None and
        event_epoch < boundary_epoch - SUBSCRIPTION_TIME_TOLERANCE_MS / 1000.0)

    if before_boundary:
        metrics["before_boundary"] += 1
        window["before_boundary"] += 1
    if event_epoch is None:
        metrics["invalid_time"] += 1
        window["invalid_time"] += 1
    else:
        previous = metrics["last_event_epoch"]
        if previous is not None and event_epoch < previous - 0.001:
            metrics["out_of_order"] += 1
            window["out_of_order"] += 1
        metrics["last_event_epoch"] = event_epoch
        metrics["last_event"] = event_text
        if not metrics["first_event"]:
            metrics["first_event"] = event_text
            metrics["first_received"] = received_text
        window["last_event"] = event_text
        if not window["first_event"]:
            window["first_event"] = event_text

    if delay_ms is not None:
        metrics["delay_count"] += 1
        metrics["delay_sum"] += delay_ms
        metrics["delay_min"] = (
            delay_ms if metrics["delay_min"] is None
            else min(metrics["delay_min"], delay_ms))
        metrics["delay_max"] = (
            delay_ms if metrics["delay_max"] is None
            else max(metrics["delay_max"], delay_ms))
        window["delay_count"] += 1
        window["delay_sum"] += delay_ms
        window["delay_min"] = (
            delay_ms if window["delay_min"] is None
            else min(window["delay_min"], delay_ms))
        window["delay_max"] = (
            delay_ms if window["delay_max"] is None
            else max(window["delay_max"], delay_ms))
        if delay_ms > STALE_THRESHOLD_MS:
            metrics["stale"] += 1
            window["stale"] += 1
        elif delay_ms < -1000.0:
            metrics["future"] += 1
            window["future"] += 1

    previous_received = metrics["last_received_epoch"]
    if previous_received is not None:
        gap_ms = max(0.0, (received_epoch - previous_received) * 1000.0)
        metrics["gap_count"] += 1
        metrics["gap_sum"] += gap_ms
        metrics["gap_max"] = (
            gap_ms if metrics["gap_max"] is None
            else max(metrics["gap_max"], gap_ms))
        window["gap_count"] += 1
        window["gap_sum"] += gap_ms
        window["gap_max"] = (
            gap_ms if window["gap_max"] is None
            else max(window["gap_max"], gap_ms))
    metrics["last_received_epoch"] = received_epoch
    metrics["last_received"] = received_text
    window["last_received"] = received_text
    if not window["first_received"]:
        window["first_received"] = received_text

    sample = _sample_callback(
        phase_name, data, received_epoch, event_raw, event_epoch,
        delay_ms, before_boundary, request_epoch)
    if sample is not None:
        _SAMPLE_QUEUE.append(sample)
    return metrics


def _l2order_callback(payload, subscription_phase, request_epoch):
    global _CALLBACK_ERRORS, _LAST_CALLBACK_ERROR
    global _POST_UNSUBSCRIBE_CALLBACKS, _CALLBACKS_DURING_UNSUBSCRIBE
    global _POST_FINAL_UNSUBSCRIBE_CALLBACKS
    global _LATE_OLD_SUBSCRIPTION_CALLBACKS
    global _STALE_CALLBACK_STREAK, _BACKLOG_RESET_SCHEDULED
    global _BACKLOG_RESET_TRIGGERED, _BACKLOG_DETECTED_EPOCH
    global _BACKLOG_FIRST_STALE_EPOCH, _BACKLOG_FIRST_STALE_DELAY_MS
    global _BACKLOG_FIRST_STALE_EVENT, _BACKLOG_RESET_SCHEDULE_ERROR
    global _BACKLOG_FIRST_STALE_EPOCH, _BACKLOG_FIRST_STALE_DELAY_MS
    global _BACKLOG_FIRST_STALE_EVENT, _BACKLOG_RESET_SCHEDULE_ERROR
    started = time.time()
    received_epoch = started
    metric_key = ""
    try:
        if _DONE:
            _POST_FINAL_UNSUBSCRIBE_CALLBACKS += 1
            return
        active_phase = _ACTIVE_PHASE
        if active_phase == "unsubscribing":
            _CALLBACKS_DURING_UNSUBSCRIBE += 1
            metric_key = "during_unsubscribe_" + subscription_phase
            _observe_callback(
                payload, received_epoch, metric_key, _UNSUBSCRIBE_STARTED_EPOCH,
                request_epoch)
        elif subscription_phase == "phase1":
            if active_phase == "phase1":
                metric_key = "phase1"
                boundary_epoch = request_epoch
            else:
                _POST_UNSUBSCRIBE_CALLBACKS += 1
                if (active_phase in ("phase2", "after_final_unsubscribe") and
                        "phase2" in _PHASES):
                    metric_key = "late_phase1"
                    _LATE_OLD_SUBSCRIPTION_CALLBACKS += 1
                elif active_phase == "after_final_unsubscribe":
                    metric_key = "after_final_unsubscribe_phase1"
                    _POST_FINAL_UNSUBSCRIBE_CALLBACKS += 1
                else:
                    metric_key = "after_unsubscribe_phase1"
                if active_phase == "after_final_unsubscribe" and "phase2" in _PHASES:
                    _POST_FINAL_UNSUBSCRIBE_CALLBACKS += 1
                boundary_epoch = _UNSUBSCRIBE_RETURN_EPOCH
            _observe_callback(
                payload, received_epoch, metric_key, boundary_epoch,
                request_epoch)
            if metric_key == "phase1":
                delay_ms = _PHASES["phase1"].get("last_delay_ms")
                event_epoch = _PHASES["phase1"].get("last_event_epoch")
                post_open_event = (
                    _CYCLE_OPEN_EPOCH is None or
                    (event_epoch is not None and
                     event_epoch >= _CYCLE_OPEN_EPOCH))
                if (post_open_event and delay_ms is not None and
                        delay_ms > STALE_THRESHOLD_MS):
                    _STALE_CALLBACK_STREAK += 1
                    if _STALE_CALLBACK_STREAK == 1:
                        _BACKLOG_FIRST_STALE_EPOCH = received_epoch
                        _BACKLOG_FIRST_STALE_DELAY_MS = delay_ms
                        _BACKLOG_FIRST_STALE_EVENT = _PHASES["phase1"].get(
                            "last_event", "")
                    if (_STALE_CALLBACK_STREAK >= STALE_TRIGGER_CONSECUTIVE and
                            not _BACKLOG_RESET_SCHEDULED and
                            not _BACKLOG_RESET_TRIGGERED):
                        _BACKLOG_RESET_SCHEDULED = True
                        _BACKLOG_DETECTED_EPOCH = received_epoch
                        timer_id = _schedule_after(
                            0.05, _begin_backlog_reset, "backlog_reset")
                        if timer_id is None:
                            _BACKLOG_RESET_SCHEDULED = False
                            _BACKLOG_RESET_SCHEDULE_ERROR = (
                                "unable to schedule backlog reset")
                else:
                    _STALE_CALLBACK_STREAK = 0
        elif subscription_phase == "phase2":
            if active_phase == "phase2":
                metric_key = "phase2"
                boundary_epoch = request_epoch
            elif active_phase == "after_final_unsubscribe":
                _POST_FINAL_UNSUBSCRIBE_CALLBACKS += 1
                metric_key = "after_final_unsubscribe_phase2"
                boundary_epoch = _FINAL_UNSUBSCRIBE_RETURN_EPOCH
            else:
                metric_key = "phase2_unexpected"
                boundary_epoch = request_epoch
            _observe_callback(
                payload, received_epoch, metric_key, boundary_epoch,
                request_epoch)
    except Exception as exc:
        _CALLBACK_ERRORS += 1
        _LAST_CALLBACK_ERROR = repr(exc)
    finally:
        elapsed_ms = max(0.0, (time.time() - started) * 1000.0)
        metrics = _PHASES.get(metric_key)
        if metrics is not None:
            metrics["callback_handler_sum"] += elapsed_ms
            metrics["callback_handler_max"] = max(
                metrics["callback_handler_max"], elapsed_ms)
            if elapsed_ms > 5.0:
                metrics["callback_handler_over_5ms"] += 1
            window = metrics["window"]
            window["handler_sum"] += elapsed_ms
            window["handler_max"] = max(window["handler_max"], elapsed_ms)
            if elapsed_ms > 5.0:
                window["handler_over_5ms"] += 1


def _flush_samples(limit=SAMPLE_FLUSH_PER_TICK):
    for _ in range(min(limit, len(_SAMPLE_QUEUE))):
        sample = _SAMPLE_QUEUE.pop(0)
        delay = _format_delay(sample["delay_ms"])
        _log("[CALLBACK_SAMPLE] phase=%s seq=%d event_raw=%s event_time=%s "
             "received_time=%s delay_ms=%s before_boundary=%s "
             "request_to_callback_ms=%s price=%s volume=%s direction=%s" % (
                 sample["phase"], sample["sequence"], sample["event_raw"],
                 sample["event_time"], sample["received_time"], delay,
                 sample["before_boundary"],
                 _format_delay(sample["request_to_callback_ms"]),
                 sample["price"], sample["volume"], sample["direction"]))


def _emit_window(phase_name, now, timer_lag_ms):
    metrics = _PHASES.get(phase_name)
    if metrics is None:
        return
    window = metrics["window"]
    elapsed = max(0.001, now - window["started"])
    if window["callbacks"] == 0:
        return
    avg_delay = (
        window["delay_sum"] / window["delay_count"]
        if window["delay_count"] else None)
    avg_gap = (
        window["gap_sum"] / window["gap_count"]
        if window["gap_count"] else None)
    avg_handler = window["handler_sum"] / window["callbacks"]
    _log("[CALLBACK_WINDOW] phase=%s duration_s=%.2f callbacks=%d rate_s=%.1f "
         "before_boundary=%d stale_gt_%.0fms=%d invalid_time=%d future=%d "
         "out_of_order=%d delay_avg_ms=%s delay_min_ms=%s delay_max_ms=%s "
         "gap_avg_ms=%s gap_max_ms=%s handler_avg_ms=%.2f handler_max_ms=%.2f "
         "handler_gt_5ms=%d "
         "event_first=%s event_last=%s received_first=%s received_last=%s "
         "timer_lag_ms=%.1f total_callbacks=%d total_before_boundary=%d" % (
             phase_name, elapsed, window["callbacks"],
             window["callbacks"] / elapsed, window["before_boundary"],
             STALE_THRESHOLD_MS, window["stale"], window["invalid_time"],
             window["future"], window["out_of_order"],
             _format_delay(avg_delay), _format_delay(window["delay_min"]),
             _format_delay(window["delay_max"]), _format_delay(avg_gap),
             _format_delay(window["gap_max"]), avg_handler, window["handler_max"],
             window["handler_over_5ms"], window["first_event"],
             window["last_event"], window["first_received"],
             window["last_received"], timer_lag_ms,
             metrics["total_callbacks"], metrics["before_boundary"]))
    metrics["window"] = _new_window(now)


def _heartbeat(expected_epoch):
    global _LAST_IDLE_LOG_EPOCH, _CALLBACK_ERRORS, _LAST_CALLBACK_ERROR
    global _BACKLOG_RESET_SCHEDULE_ERROR
    now = time.time()
    timer_lag_ms = max(0.0, (now - expected_epoch) * 1000.0)
    _flush_samples()
    phase_name = _ACTIVE_PHASE
    metrics = _PHASES.get(phase_name)
    if metrics is not None:
        window = metrics["window"]
        if now - window["started"] >= SUMMARY_INTERVAL_SECONDS:
            if window["callbacks"]:
                _emit_window(phase_name, now, timer_lag_ms)
            else:
                metrics["window"] = _new_window(now)
                if now - _LAST_IDLE_LOG_EPOCH >= IDLE_LOG_INTERVAL_SECONDS:
                    _LAST_IDLE_LOG_EPOCH = now
                    _log("[NO_CALLBACKS] phase=%s total_callbacks=%d "
                         "elapsed_since_subscribe_s=%.1f timer_lag_ms=%.1f" % (
                             phase_name, metrics["total_callbacks"],
                             now - metrics["request_epoch"], timer_lag_ms))
    elif phase_name in ("between_subscriptions", "after_final_unsubscribe"):
        if now - _LAST_IDLE_LOG_EPOCH >= IDLE_LOG_INTERVAL_SECONDS:
            _LAST_IDLE_LOG_EPOCH = now
            _log("[UNSUBSCRIBE_WINDOW] phase=%s callbacks_after_unsubscribe=%d "
                 "callbacks_during_unsubscribe=%d timer_lag_ms=%.1f" % (
                     phase_name, _POST_UNSUBSCRIBE_CALLBACKS,
                     _CALLBACKS_DURING_UNSUBSCRIBE, timer_lag_ms))
    if _CALLBACK_ERRORS:
        _log("[CALLBACK_ERRORS] count=%d last=%s" % (
            _CALLBACK_ERRORS, _LAST_CALLBACK_ERROR))
        _CALLBACK_ERRORS = 0
        _LAST_CALLBACK_ERROR = ""
    if _BACKLOG_RESET_SCHEDULE_ERROR:
        _log("[BACKLOG_RESET_SCHEDULE_FAILED] reason=%s" % (
            _BACKLOG_RESET_SCHEDULE_ERROR))
        _BACKLOG_RESET_SCHEDULE_ERROR = ""
    if not _DONE:
        _schedule_heartbeat()


def _cancel_timer(name):
    timer_id = _TIMERS.pop(name, None)
    _TIMER_EXPECTED.pop(name, None)
    if timer_id is not None and _C is not None:
        try:
            _C.cancel_schedule_run(timer_id)
        except Exception:
            pass


def _unsubscribe_active(reason):
    global _SUB_ID, _ACTIVE_PHASE, _UNSUBSCRIBE_RETURN_EPOCH
    global _UNSUBSCRIBE_STARTED_EPOCH, _FINAL_UNSUBSCRIBE_RETURN_EPOCH
    if _SUB_ID is None:
        _log("[UNSUBSCRIBE_SKIPPED] reason=%s no_active_subscription=true" % reason)
        return False
    old_id = _SUB_ID
    _ACTIVE_PHASE = "unsubscribing"
    _UNSUBSCRIBE_STARTED_EPOCH = time.time()
    final_unsubscribe = (
        reason in ("phase2_complete", "observation_window_complete") or
        (reason == "manual_stop" and "phase2" in _PHASES))
    try:
        result = _C.unsubscribe_quote(old_id)
        returned = time.time()
        if final_unsubscribe:
            _FINAL_UNSUBSCRIBE_RETURN_EPOCH = returned
        else:
            _UNSUBSCRIBE_RETURN_EPOCH = returned
        _log("[UNSUBSCRIBE_RETURN] reason=%s sub_id=%s result=%r "
             "call_ms=%.1f return_time=%s" % (
                 reason, old_id, result,
                 (returned - _UNSUBSCRIBE_STARTED_EPOCH) * 1000.0,
                 _format_time(returned)))
    except Exception as exc:
        returned = time.time()
        if final_unsubscribe:
            _FINAL_UNSUBSCRIBE_RETURN_EPOCH = returned
        else:
            _UNSUBSCRIBE_RETURN_EPOCH = returned
        _log("[UNSUBSCRIBE_ERROR] reason=%s sub_id=%s error=%r return_time=%s" % (
            reason, old_id, exc, _format_time(returned)))
    _SUB_ID = None
    return True


def _begin_backlog_reset(expected_epoch):
    global _ACTIVE_PHASE, _BACKLOG_RESET_SCHEDULED, _BACKLOG_RESET_TRIGGERED
    if _DONE or _BACKLOG_RESET_TRIGGERED:
        return
    _BACKLOG_RESET_SCHEDULED = False
    _BACKLOG_RESET_TRIGGERED = True
    now = time.time()
    _ACTIVE_PHASE = "unsubscribing"
    _flush_samples(limit=len(_SAMPLE_QUEUE))
    _emit_window("phase1", now, max(0.0, (now - expected_epoch) * 1000.0))
    _log("[STALE_CALLBACK] first_delay_ms=%s first_event_time=%s "
         "first_received_time=%s trigger=%d_consecutive" % (
             _format_delay(_BACKLOG_FIRST_STALE_DELAY_MS),
             _BACKLOG_FIRST_STALE_EVENT,
             _format_time(_BACKLOG_FIRST_STALE_EPOCH),
             STALE_TRIGGER_CONSECUTIVE))
    _log("[BACKLOG_RESET_BEGIN] stale_streak=%d detected_at=%s "
         "control_time=%s timer_lag_ms=%.1f resubscribe_delay_s=%.2f" % (
             _STALE_CALLBACK_STREAK,
             _format_time(_BACKLOG_DETECTED_EPOCH or now),
             _format_time(now), max(0.0, (now - expected_epoch) * 1000.0),
             RESUBSCRIBE_DELAY_SECONDS))
    if not _unsubscribe_active("sustained_stale_callbacks"):
        _log("[UNSUBSCRIBE_WINDOW_NOTE] no active subscription at backlog trigger")
    _ACTIVE_PHASE = "between_subscriptions"
    _log("[UNSUBSCRIBE_WINDOW_START] collect callbacks until resubscribe in %.2fs" % (
        RESUBSCRIBE_DELAY_SECONDS))
    if _schedule_after(
            RESUBSCRIBE_DELAY_SECONDS, _resubscribe, "resubscribe") is None:
        _log("[RESUBSCRIBE_SCHEDULE_FAILED] reset_triggered=true")
        _ACTIVE_PHASE = "after_final_unsubscribe"
        _schedule_after(
            FINAL_UNSUBSCRIBE_SETTLE_SECONDS, _complete, "complete")


def _end_initial_observation(expected_epoch):
    if _DONE or _BACKLOG_RESET_TRIGGERED:
        return
    if _BACKLOG_RESET_SCHEDULED:
        _schedule_after(0.1, _end_initial_observation, "observation_end")
        return
    now = time.time()
    _flush_samples(limit=len(_SAMPLE_QUEUE))
    _emit_window("phase1", now, max(0.0, (now - expected_epoch) * 1000.0))
    _log("[OPENING_OBSERVATION_CHECK] time=%s callbacks=%d stale=%d "
         "invalid_time=%d reset_triggered=false action=continue_monitoring "
         "until_stale_trigger_or_manual_stop" % (
             _format_time(now), _PHASES.get("phase1", {}).get("total_callbacks", 0),
             _PHASES.get("phase1", {}).get("stale", 0),
             _PHASES.get("phase1", {}).get("invalid_time", 0)))


def _resubscribe(expected_epoch):
    global _ACTIVE_PHASE, _POST_UNSUBSCRIBE_CALLBACKS
    _ACTIVE_PHASE = "between_subscriptions"
    _flush_samples(limit=len(_SAMPLE_QUEUE))
    for phase_name in (
            "during_unsubscribe_phase1", "after_unsubscribe_phase1"):
        _emit_window(phase_name, time.time(), 0.0)
    _log("[UNSUBSCRIBE_WINDOW_END] callbacks_after_unsubscribe=%d "
         "callbacks_during_unsubscribe=%d late_old_stream_callbacks=%d "
         "elapsed_s=%.2f" % (
             _POST_UNSUBSCRIBE_CALLBACKS, _CALLBACKS_DURING_UNSUBSCRIBE,
             _LATE_OLD_SUBSCRIPTION_CALLBACKS,
             max(0.0, time.time() - (_UNSUBSCRIBE_RETURN_EPOCH or time.time()))))
    _log("[RESUBSCRIBE_BEGIN] scheduled_time=%s actual_time=%s timer_lag_ms=%.1f" % (
        _format_time(expected_epoch), _format_time(time.time()),
        max(0.0, (time.time() - expected_epoch) * 1000.0)))
    if not _subscribe_phase("phase2"):
        _ACTIVE_PHASE = "after_final_unsubscribe"
        _schedule_after(
            FINAL_UNSUBSCRIBE_SETTLE_SECONDS, _complete, "complete")
        return
    _schedule_after(
        POST_RESET_OBSERVE_SECONDS, _finish_phase2, "finish_phase2")


def _finish_phase2(expected_epoch):
    global _ACTIVE_PHASE
    if _DONE:
        return
    _flush_samples()
    _emit_window(
        "phase2", time.time(), max(0.0, (time.time() - expected_epoch) * 1000.0))
    _emit_window("late_phase1", time.time(), 0.0)
    _log("[PHASE2_END] scheduled_time=%s actual_time=%s timer_lag_ms=%.1f" % (
        _format_time(expected_epoch), _format_time(time.time()),
        max(0.0, (time.time() - expected_epoch) * 1000.0)))
    _unsubscribe_active("phase2_complete")
    _ACTIVE_PHASE = "after_final_unsubscribe"
    _schedule_after(
        FINAL_UNSUBSCRIBE_SETTLE_SECONDS, _complete, "complete")


def _complete(expected_epoch):
    global _DONE, _ACTIVE_PHASE
    _flush_samples(limit=len(_SAMPLE_QUEUE))
    _emit_window("during_unsubscribe_phase2", time.time(), 0.0)
    _emit_window("after_final_unsubscribe_phase1", time.time(), 0.0)
    _emit_window("after_final_unsubscribe_phase2", time.time(), 0.0)
    _emit_window("late_phase1", time.time(), 0.0)
    _DONE = True
    _cancel_timer("metrics")
    _ACTIVE_PHASE = "done"
    _log("[PROBE_COMPLETE] actual_time=%s timer_lag_ms=%.1f reset_triggered=%s "
         "callbacks_after_first_unsubscribe=%d callbacks_during_unsubscribe=%d "
         "late_old_stream_callbacks=%d callbacks_after_final_unsubscribe=%d" % (
             _format_time(time.time()),
             max(0.0, (time.time() - expected_epoch) * 1000.0),
             _BACKLOG_RESET_TRIGGERED,
             _POST_UNSUBSCRIBE_CALLBACKS, _CALLBACKS_DURING_UNSUBSCRIBE,
             _LATE_OLD_SUBSCRIPTION_CALLBACKS,
             _POST_FINAL_UNSUBSCRIBE_CALLBACKS))


def _reset_state():
    global _SUB_ID, _ACTIVE_PHASE, _PHASES, _SAMPLE_QUEUE
    global _CALLBACK_ERRORS, _LAST_CALLBACK_ERROR
    global _POST_UNSUBSCRIBE_CALLBACKS, _CALLBACKS_DURING_UNSUBSCRIBE
    global _POST_FINAL_UNSUBSCRIBE_CALLBACKS, _UNSUBSCRIBE_RETURN_EPOCH
    global _LATE_OLD_SUBSCRIPTION_CALLBACKS
    global _STALE_CALLBACK_STREAK, _BACKLOG_RESET_SCHEDULED
    global _BACKLOG_RESET_TRIGGERED, _BACKLOG_DETECTED_EPOCH
    global _UNSUBSCRIBE_STARTED_EPOCH, _FINAL_UNSUBSCRIBE_RETURN_EPOCH
    global _DONE, _TIMERS, _TIMER_EXPECTED, _LAST_IDLE_LOG_EPOCH
    global _CYCLE_OPEN_EPOCH, _CYCLE_EPOCH
    _SUB_ID = None
    _ACTIVE_PHASE = ""
    _PHASES = {}
    _SAMPLE_QUEUE = []
    _CALLBACK_ERRORS = 0
    _LAST_CALLBACK_ERROR = ""
    _POST_UNSUBSCRIBE_CALLBACKS = 0
    _CALLBACKS_DURING_UNSUBSCRIBE = 0
    _POST_FINAL_UNSUBSCRIBE_CALLBACKS = 0
    _LATE_OLD_SUBSCRIPTION_CALLBACKS = 0
    _STALE_CALLBACK_STREAK = 0
    _BACKLOG_RESET_SCHEDULED = False
    _BACKLOG_RESET_TRIGGERED = False
    _BACKLOG_DETECTED_EPOCH = None
    _BACKLOG_FIRST_STALE_EPOCH = None
    _BACKLOG_FIRST_STALE_DELAY_MS = None
    _BACKLOG_FIRST_STALE_EVENT = ""
    _BACKLOG_RESET_SCHEDULE_ERROR = ""
    _UNSUBSCRIBE_RETURN_EPOCH = None
    _UNSUBSCRIBE_STARTED_EPOCH = None
    _FINAL_UNSUBSCRIBE_RETURN_EPOCH = None
    _DONE = False
    _TIMERS = {}
    _TIMER_EXPECTED = {}
    _LAST_IDLE_LOG_EPOCH = 0.0
    _CYCLE_OPEN_EPOCH = None
    _CYCLE_EPOCH = None


def init(C):
    global _C, _CYCLE_OPEN_EPOCH, _CYCLE_EPOCH
    _C = C
    _reset_state()
    try:
        _open_log()
    except Exception as exc:
        _log("[LOG_OPEN_FAILED] error=%r; console logging remains active" % (exc,))
    now = time.time()
    _CYCLE_OPEN_EPOCH, _CYCLE_EPOCH = _choose_cycle(now)
    _log("[INIT] mode=read_only code=%s period=l2order cwd=%s" % (
        TARGET_CODE, os.getcwd()))
    _log("[NO_TRADING] account_query=false passorder=false cancel=false")
    _log("[TIMING_CONFIG] stale_threshold_ms=%.0f stale_trigger_consecutive=%d "
         "pre_subscribe_tolerance_ms=%.0f summary_interval_s=%.0f "
         "observe_after_open_s=%.0f resubscribe_delay_s=%.2f "
         "post_reset_observe_s=%.0f" % (
             STALE_THRESHOLD_MS, STALE_TRIGGER_CONSECUTIVE,
             SUBSCRIPTION_TIME_TOLERANCE_MS, SUMMARY_INTERVAL_SECONDS,
             OBSERVE_AFTER_OPEN_SECONDS, RESUBSCRIBE_DELAY_SECONDS,
             POST_RESET_OBSERVE_SECONDS))
    if _CYCLE_OPEN_EPOCH is not None:
        _log("[FOCUS_WINDOW] session_open=%s observation_end=%s "
             "reset=only_after_sustained_stale_callbacks" % (
            _format_time(_CYCLE_OPEN_EPOCH), _format_time(_CYCLE_EPOCH)))
    else:
        _log("[FOCUS_WINDOW] no upcoming open; observation_end=%s "
             "reset=only_after_sustained_stale_callbacks" % (
            _format_time(_CYCLE_EPOCH)))
    if not _subscribe_phase("phase1"):
        _log("[ABORT] l2order subscription failed; no trade functions are present")
        return
    _schedule_at(_CYCLE_EPOCH, _end_initial_observation, "observation_end")
    _schedule_heartbeat()


def handlebar(C):
    return


def stop(C):
    global _DONE, _ACTIVE_PHASE
    if _DONE:
        _log("[STOP] probe already complete")
    else:
        for name in list(_TIMERS.keys()):
            _cancel_timer(name)
        _unsubscribe_active("manual_stop")
        _flush_samples(limit=len(_SAMPLE_QUEUE))
        _DONE = True
        _ACTIVE_PHASE = "stopped"
        _log("[STOP] callbacks_after_first_unsubscribe=%d "
             "callbacks_during_unsubscribe=%d late_old_stream_callbacks=%d "
             "callbacks_after_final_unsubscribe=%d" % (
                 _POST_UNSUBSCRIBE_CALLBACKS, _CALLBACKS_DURING_UNSUBSCRIBE,
                 _LATE_OLD_SUBSCRIPTION_CALLBACKS,
                 _POST_FINAL_UNSUBSCRIBE_CALLBACKS))
    if _LOG is not None:
        try:
            _LOG.close()
        except Exception:
            pass
