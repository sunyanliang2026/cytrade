import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_LARGE_ORDER_LIMIT_UP_BUY.py"
SPEC = importlib.util.spec_from_file_location("qmt_rewrite_large_order_paths", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class LargeOrderTriggerPathTests(unittest.TestCase):
    def setUp(self):
        self.old_account = QMT.ACCOUNT_ID
        self.old_live = QMT.ENABLE_LIVE_ORDERS
        self.old_context = QMT._C
        self.old_log = QMT._log
        self.old_schedule_validation = QMT._schedule_validation
        self.old_schedule_lookup = QMT._schedule_order_lookup
        self.had_passorder = hasattr(QMT, "passorder")
        self.old_passorder = getattr(QMT, "passorder", None)
        self.logs = []
        self.calls = []

        QMT.ACCOUNT_ID = "TEST_ACCOUNT"
        QMT.ENABLE_LIVE_ORDERS = True
        QMT._C = object()
        QMT._log = self.logs.append
        QMT._schedule_validation = lambda _inst: None
        QMT._schedule_order_lookup = lambda _inst: None
        QMT.passorder = lambda *args: self.calls.append(args) or "501"
        QMT._INSTANCES.clear()
        QMT._RAW_BUFFER.clear()
        QMT._COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0})
        self.inst = self._instance()

    def tearDown(self):
        QMT.ACCOUNT_ID = self.old_account
        QMT.ENABLE_LIVE_ORDERS = self.old_live
        QMT._C = self.old_context
        QMT._log = self.old_log
        QMT._schedule_validation = self.old_schedule_validation
        QMT._schedule_order_lookup = self.old_schedule_lookup
        QMT._INSTANCES.clear()
        QMT._RAW_BUFFER.clear()
        if self.had_passorder:
            QMT.passorder = self.old_passorder
        else:
            del QMT.passorder

    def _instance(self, state=QMT.STATE_READY):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst["state"] = state
        inst["limit_up"] = 10.0
        QMT._INSTANCES[inst["code"]] = inst
        return inst

    def _order(self, order_id, *, price=10.0, volume=900001, side="B", amount=None):
        data = {
            "stime": "100000",
            "price": price,
            "volume": volume,
            "direction": side,
            "entrustNo": order_id,
        }
        if amount is not None:
            data["amount"] = amount
        QMT._handle_l2order(self.inst["code"], data)

    def test_large_order_triggers_when_share_or_amount_threshold_is_exceeded(self):
        self.inst["limit_up"] = 5.0
        self._order("share_only", price=5.0, volume=900001)
        self.assertEqual(len(self.calls), 1)

        self.calls[:] = []
        self.inst = self._instance()
        self.inst["limit_up"] = 10.0
        self._order("amount_only", price=10.0, volume=500000, amount=5000001.0)
        self.assertEqual(len(self.calls), 1)

        self.calls[:] = []
        self.inst = self._instance()
        self.inst["limit_up"] = 5.0
        self._order("exact_thresholds", price=5.0, volume=900000, amount=5000000.0)
        self.assertEqual(self.calls, [])

        self.assertIsNone(self.inst["submit_reason"])
        self.assertIsNone(self.inst["validation_order_id"])

    def test_l2quote_amount_keeps_lots_to_shares_conversion(self):
        self.assertEqual(QMT._amount(10.0, 10000), 10000000.0)

    def test_first_seal_original_cumulative_path_is_preserved_and_validated(self):
        self.inst["initial_sealed"] = True
        self.inst["open_price"] = 10.0
        self.inst["day_low"] = 9.8
        for index in range(6):
            self._order("cumulative_%d" % index, price=10.0, volume=200000)

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.inst["submit_reason"], "首封")
        self.assertEqual(self.inst["first_large_total"], 12000000.0)
        self.assertEqual(self.inst["validation_order_id"], "501")

    def test_reported_order_amount_is_used_as_currency_amount(self):
        self.inst["limit_up"] = 1.0
        self._order("reported_amount", price=1.0, volume=900001, amount=5000001.0)

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.inst["submit_reason"], "首封大单")

    def test_reseal_large_order_follows_only_after_qualified_open_duration(self):
        self.inst["state"] = QMT.STATE_WAIT_RESEAL
        self.inst["reseal_ready"] = True
        self.inst["break_started"] = QMT._now() - 2.0
        self._order("too_early")
        self.assertEqual(self.calls, [])

        self.inst["break_started"] = QMT._now() - QMT.RESEAL_BREAK_SECONDS - 0.1
        self._order("reseal_trigger")

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.inst["submit_reason"], "回封大单")
        self.assertEqual(self.inst["validation_order_id"], "501")

    def test_large_trigger_requires_limit_up_buy_order(self):
        self._order("sell", side="S")
        self._order("off_limit", price=9.99)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
