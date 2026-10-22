import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_LARGE_ORDER_LIMIT_UP_BUY.py"
SPEC = importlib.util.spec_from_file_location("qmt_negative_quote_exit", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class FakeContext:
    def __init__(self):
        self.unsubscribed = []
        self.subscribed = []
        self.cancelled_timers = []

    def unsubscribe_quote(self, sub_id):
        self.unsubscribed.append(sub_id)

    def subscribe_quote(self, *args, **kwargs):
        self.subscribed.append((args, kwargs))
        return 999

    def cancel_schedule_run(self, timer):
        self.cancelled_timers.append(timer)


class NegativeQuoteExitTests(unittest.TestCase):
    def setUp(self):
        self.old_log = QMT._log
        self.old_context = QMT._C
        self.old_cancel = getattr(QMT, "cancel", None)
        self.old_freshness_gate = QMT._freshness_gate
        self.had_cancel = hasattr(QMT, "cancel")
        self.logs = []
        self.cancel_calls = []
        self.context = FakeContext()
        QMT._log = self.logs.append
        # These tests cover quote decisions after the entry gate has accepted.
        QMT._freshness_gate = lambda _code, _period, payload, _received_at, **_kwargs: (
            True, QMT._field(payload, "time", "stime", "timestamp"), None)
        QMT._C = self.context
        QMT.cancel = lambda *args: self.cancel_calls.append(args)
        QMT._INSTANCES.clear()
        QMT._SUBSCRIPTIONS[:] = []
        QMT._RAW_BUFFER.clear()
        QMT._PERIOD_COUNTS.clear()
        QMT._FIRST_CALLBACKS.clear()
        QMT._LAST_EVENT_TIMES.clear()
        QMT._LATENCY_STATS.clear()
        QMT._LATENCY_LAST_WARN.clear()
        QMT._LAST_RECEIVED_AT.clear()
        QMT._GAP_STATS.clear()
        QMT._COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0, "l2": 0})
        self.inst = QMT._new_instance("000001.SZ", 10000)
        self.inst.update({
            "name": "Test",
            "state": QMT.STATE_READY,
            "limit_up": 11.0,
            "last_close": 10.0,
            "sub_ids": [102, 103],
            "sub_periods": {102: "l2quote", 103: "l2order"},
            "our_order_id": "501",
            "validation_order_id": "501",
            "our_order_qty": 1000,
            "validation_timer": "validate-1",
            "order_lookup_timer": "lookup-1",
        })
        QMT._INSTANCES[self.inst["code"]] = self.inst
        QMT._SUBSCRIPTIONS[:] = [102, 103]

    def tearDown(self):
        QMT._log = self.old_log
        QMT._C = self.old_context
        QMT._freshness_gate = self.old_freshness_gate
        QMT._INSTANCES.clear()
        QMT._SUBSCRIPTIONS[:] = []
        if self.had_cancel:
            QMT.cancel = self.old_cancel
        else:
            del QMT.cancel

    def test_negative_l2quote_unsubscribes_all_periods_and_ignores_existing_order(self):
        QMT._handle_quote(self.inst["code"], "l2quote", {
            "stime": "100000",
            "lastPrice": 9.99,
            "lastClose": 10.0,
            "low": 9.50,
        })

        self.assertEqual(self.inst["state"], QMT.STATE_SKIPPED)
        self.assertTrue(self.inst["subscriptions_stopped"])
        self.assertEqual(self.context.unsubscribed, [102, 103])
        self.assertEqual(self.inst["sub_ids"], [])
        self.assertEqual(QMT._SUBSCRIPTIONS, [])
        self.assertEqual(self.context.cancelled_timers, ["validate-1", "lookup-1"])
        self.assertEqual(self.inst["our_order_id"], "501")
        self.assertEqual(self.inst["validation_order_id"], "501")
        self.assertEqual(self.cancel_calls, [])

        raw_count = len(QMT._RAW_BUFFER)
        QMT._handle_l2order(self.inst["code"], {
            "stime": "100001", "price": 11.0, "volume": 1000000,
            "direction": "B", "entrustNo": "new-market-order",
        })
        QMT.order_callback(None, {
            "order_id": "501", "stock_code": "000001.SZ",
            "order_status": 56, "order_volume": 1000, "traded_volume": 1000,
        })
        QMT.deal_callback(None, {
            "order_id": "501", "stock_code": "000001.SZ",
            "deal_id": "deal-1", "deal_volume": 1000,
        })

        self.assertEqual(len(QMT._RAW_BUFFER), raw_count)
        self.assertEqual(self.inst["our_traded_qty"], 0)
        self.assertEqual(QMT._COUNTERS["deals"], 0)
        self.assertEqual(self.cancel_calls, [])
        self.assertTrue(any("change_pct=-0.1000" in line for line in self.logs))

    def test_nonnegative_l2quote_does_not_trigger_exit(self):
        QMT._handle_quote(self.inst["code"], "l2quote", {
            "stime": "100001", "lastPrice": 10.0, "lastClose": 10.0,
        })
        QMT._handle_quote(self.inst["code"], "l2quote", {
            "stime": "100002", "lastPrice": 10.01, "lastClose": 10.0, "low": 9.50,
        })

        self.assertEqual(self.inst["state"], QMT.STATE_READY)
        self.assertFalse(self.inst["subscriptions_stopped"])
        self.assertEqual(self.context.unsubscribed, [])
        self.assertEqual(self.cancel_calls, [])

    def test_stale_negative_l2quote_does_not_permanently_exit(self):
        with mock.patch.object(QMT, "_latency_ms", return_value=1500.0):
            QMT._handle_quote(self.inst["code"], "l2quote", {
                "stime": "100000", "time": "100000.500",
                "lastPrice": 9.99, "lastClose": 10.0,
            })

        self.assertEqual(self.inst["state"], QMT.STATE_READY)
        self.assertFalse(self.inst["subscriptions_stopped"])
        self.assertEqual(self.context.unsubscribed, [])
        self.assertTrue(any("NEGATIVE_RETURN_IGNORED_STALE" in line for line in self.logs))

    def test_summary_reports_only_l2_subscriptions(self):
        QMT._PERIOD_COUNTS.update({"l2quote": 1, "l2order": 2})
        QMT._COUNTERS["l2"] = 3

        QMT._summary()

        summary = self.logs[0]
        self.assertIn("L2=3", summary)
        self.assertIn("l2quote:1,l2order:2", summary)
        self.assertNotIn("tick", summary)
        self.assertNotIn("行情=", summary)

    def test_negative_continuous_session_snapshot_prevents_subscriptions(self):
        self.inst["sub_ids"] = []
        self.inst["sub_periods"] = {}
        QMT._SUBSCRIPTIONS[:] = []
        snapshot = {
            self.inst["code"]: {
                "stime": "100000", "lastPrice": 9.99, "lastClose": 10.0,
            }
        }
        QMT._initial_quote(self.context, self.inst, snapshot)
        QMT._subscribe(self.context, self.inst["code"], "l2quote")

        self.assertEqual(QMT.PERIODS, ("l2quote", "l2order"))
        self.assertEqual(self.inst["state"], QMT.STATE_SKIPPED)
        self.assertTrue(self.inst["subscriptions_stopped"])
        self.assertEqual(self.context.subscribed, [])
        self.assertEqual(self.context.unsubscribed, [])


if __name__ == "__main__":
    unittest.main()
