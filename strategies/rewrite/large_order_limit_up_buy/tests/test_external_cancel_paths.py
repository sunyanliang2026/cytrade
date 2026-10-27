import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_LARGE_ORDER_LIMIT_UP_BUY.py"
SPEC = importlib.util.spec_from_file_location("qmt_external_cancel_paths", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class CallbackContext:
    pass


class FakeContext:
    def __init__(self):
        self.scheduled = []
        self.cancelled = []

    def schedule_run(self, callback, target, **kwargs):
        timer_id = len(self.scheduled) + 1
        self.scheduled.append((timer_id, callback, target, kwargs))
        return timer_id

    def cancel_schedule_run(self, timer_id):
        self.cancelled.append(timer_id)


class ExternalCancelPathTests(unittest.TestCase):
    def setUp(self):
        self.context = FakeContext()
        self.logs = []
        self.cancel_calls = []
        self.old = {
            "C": QMT._C,
            "log": QMT._log,
            "freshness": QMT._freshness_gate,
            "passorder": getattr(QMT, "passorder", None),
            "cancel": getattr(QMT, "cancel", None),
            "account": QMT.ACCOUNT_ID,
            "live": QMT.ENABLE_LIVE_ORDERS,
        }
        QMT._C = self.context
        QMT._log = self.logs.append
        QMT._freshness_gate = lambda _code, _period, payload, _received_at, **_kwargs: (
            True, QMT._field(payload, "time", "stime", "timestamp"), None)
        QMT.ACCOUNT_ID = "TEST_ACCOUNT"
        QMT.ENABLE_LIVE_ORDERS = True
        QMT.passorder = lambda *_args: "OUR-1"
        QMT.cancel = lambda *args: self.cancel_calls.append(args) or True
        QMT._INSTANCES.clear()
        QMT._COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0, "l2": 0})
        QMT._ENTRY_DROPS.clear()
        QMT._CALLBACK_COUNTS.clear()

    def tearDown(self):
        QMT._C = self.old["C"]
        QMT._log = self.old["log"]
        QMT._freshness_gate = self.old["freshness"]
        if self.old["passorder"] is None:
            del QMT.passorder
        else:
            QMT.passorder = self.old["passorder"]
        if self.old["cancel"] is None:
            del QMT.cancel
        else:
            QMT.cancel = self.old["cancel"]
        QMT.ACCOUNT_ID = self.old["account"]
        QMT.ENABLE_LIVE_ORDERS = self.old["live"]
        QMT._INSTANCES.clear()

    def instance(self):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst.update({
            "state": QMT.STATE_READY,
            "limit_up": 10.0,
            "open_price": 10.0,
            "day_low": 9.8,
        })
        QMT._INSTANCES[inst["code"]] = inst
        return inst

    def submit_with_trigger(self, order_id="TRIGGER-1"):
        inst = self.instance()
        self.assertTrue(QMT._submit(
            inst, "首封大单", "event:%s" % order_id,
            {"order_id": order_id, "price": 10.0, "volume": 900001,
             "amount": 9000010.0, "direction": "B", "event_time": "100000"}))
        self.assertEqual(inst["our_order_id"], "OUR-1")
        return inst

    def cancel_timer(self):
        return next(item for item in self.context.scheduled
                    if item[3].get("name", "").startswith("qmt_cancel_due"))

    def test_shanghai_l2order_cancel_matches_trigger_and_uses_timer_context(self):
        inst = self.submit_with_trigger("SH-TRIGGER")
        QMT._handle_l2order(inst["code"], {
            "stime": "100001", "price": 10.0, "volume": 900001,
            "entrustDirection": 3, "entrustNo": "SH-TRIGGER",
        })

        self.assertTrue(inst["external_cancel_due"])
        _, callback, _, _ = self.cancel_timer()
        timer_context = CallbackContext()
        callback(timer_context)

        self.assertEqual(len(self.cancel_calls), 1)
        self.assertIs(self.cancel_calls[0][-1], timer_context)
        self.assertIsNot(self.cancel_calls[0][-1], QMT._C)
        QMT.order_callback(timer_context, {
            "order_id": "OUR-1", "stock_code": "000001.SZ",
            "order_volume": 1000, "traded_volume": 0,
            "order_status": 54, "order_price": 10.0,
        })
        self.assertEqual(inst["state"], QMT.STATE_DONE)
        self.assertTrue(inst["cancel_confirmed"])

    def test_shenzhen_l2transaction_cancel_matches_buy_no(self):
        inst = self.submit_with_trigger("SZ-TRIGGER")
        QMT._handle_l2transaction(inst["code"], {
            "stime": "100001", "price": 10.0, "volume": 900001,
            "tradeFlag": 3, "buyNo": "SZ-TRIGGER",
        })
        self.assertTrue(inst["external_cancel_due"])
        _, callback, _, _ = self.cancel_timer()
        callback(CallbackContext())
        self.assertEqual(len(self.cancel_calls), 1)
        self.assertEqual(self.cancel_calls[0][0], "OUR-1")

    def test_external_cancel_seen_before_trigger_is_replayed_after_submission(self):
        inst = self.instance()
        QMT._handle_l2order(inst["code"], {
            "stime": "100000", "price": 10.0, "volume": 900001,
            "entrustDirection": 3, "entrustNo": "EARLY-TRIGGER",
        })
        self.assertFalse(inst["external_cancel_due"])
        self.assertIn("EARLY-TRIGGER", inst["pending_external_cancels"])
        self.assertTrue(QMT._submit(
            inst, "首封大单", "event:EARLY-TRIGGER",
            {"order_id": "EARLY-TRIGGER", "price": 10.0,
             "volume": 900001, "amount": 9000010.0,
             "direction": "B", "event_time": "100001"}))
        self.assertTrue(inst["external_cancel_due"])
        _, callback, _, _ = self.cancel_timer()
        callback(CallbackContext())
        self.assertEqual(len(self.cancel_calls), 1)

    def test_unrelated_external_cancel_does_not_cancel_our_order(self):
        inst = self.submit_with_trigger("TRIGGER-1")
        QMT._handle_l2order(inst["code"], {
            "stime": "100001", "price": 10.0, "volume": 900001,
            "entrustDirection": 3, "entrustNo": "OTHER-1",
        })
        self.assertFalse(inst["external_cancel_due"])
        self.assertEqual(self.cancel_calls, [])
        self.assertFalse(any("CANCEL_TIMER_FAILED" in line for line in self.logs))

    def test_fully_filled_order_skips_external_cancel(self):
        inst = self.submit_with_trigger()
        QMT.order_callback(None, {
            "order_id": "OUR-1", "stock_code": "000001.SZ",
            "order_volume": inst["our_order_qty"],
            "traded_volume": inst["our_order_qty"],
            "order_status": 56, "order_price": 10.0,
        })
        QMT._handle_l2order(inst["code"], {
            "stime": "100001", "price": 10.0, "volume": 900001,
            "entrustDirection": 3, "entrustNo": "TRIGGER-1",
        })
        self.assertEqual(inst["state"], QMT.STATE_DONE)
        self.assertEqual(self.cancel_calls, [])

    def test_partial_fill_still_requests_cancel_for_remainder(self):
        inst = self.submit_with_trigger()
        QMT.order_callback(None, {
            "order_id": "OUR-1", "stock_code": "000001.SZ",
            "order_volume": inst["our_order_qty"], "traded_volume": 300,
            "order_status": 55, "order_price": 10.0,
        })
        QMT._handle_l2order(inst["code"], {
            "stime": "100001", "price": 10.0, "volume": 900001,
            "entrustDirection": 3, "entrustNo": "TRIGGER-1",
        })
        _, callback, _, _ = self.cancel_timer()
        callback(CallbackContext())
        self.assertEqual(len(self.cancel_calls), 1)
        self.assertEqual(inst["our_traded_qty"], 300)
        self.assertLess(inst["our_traded_qty"], inst["our_order_qty"])

    def test_cancel_exception_keeps_due_and_retries_with_new_context(self):
        inst = self.submit_with_trigger()
        attempts = []

        def fail_once(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise RuntimeError("request id unavailable")
            return True

        QMT.cancel = fail_once
        QMT._handle_l2order(inst["code"], {
            "stime": "100001", "price": 10.0, "volume": 900001,
            "entrustDirection": 3, "entrustNo": "TRIGGER-1",
        })
        _, callback, _, _ = self.cancel_timer()
        first_context = CallbackContext()
        callback(first_context)
        self.assertTrue(inst["cancel_due"])
        self.assertFalse(inst["cancel_requested"])
        _, retry_callback, _, retry_kwargs = next(
            item for item in self.context.scheduled
            if item[3].get("name", "").startswith("qmt_cancel_due") and
            item[0] != self.cancel_timer()[0]
        )
        self.assertEqual(retry_kwargs["name"], "qmt_cancel_due_000001.SZ")
        second_context = CallbackContext()
        retry_callback(second_context)
        self.assertEqual(len(attempts), 2)
        self.assertIs(attempts[0][-1], first_context)
        self.assertIs(attempts[1][-1], second_context)
        self.assertFalse(inst["cancel_due"])
        self.assertTrue(inst["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
