import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_LARGE_ORDER_LIMIT_UP_BUY.py"
SPEC = importlib.util.spec_from_file_location("qmt_rewrite_order_callbacks", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class FakeContext:
    def __init__(self):
        self.last_bar = True

    def is_last_bar(self):
        return self.last_bar

    def cancel_schedule_run(self, _timer):
        pass


class QMTOrderCallbackTests(unittest.TestCase):
    def setUp(self):
        self.original_log = QMT._log
        self.original_account = QMT.ACCOUNT_ID
        self.original_live_orders = QMT.ENABLE_LIVE_ORDERS
        self.original_continue_on_failure = QMT.RESEAL_VALIDATION_CONTINUE_ON_FAILURE
        self.logs = []
        self.context = FakeContext()
        QMT._log = self.logs.append
        QMT.ACCOUNT_ID = "TEST_ACCOUNT"
        QMT.ENABLE_LIVE_ORDERS = True
        QMT._C = self.context
        QMT._INSTANCES.clear()
        QMT._COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0})
        self.inst = QMT._new_instance("000001.SZ", 10000)
        self.inst["state"] = QMT.STATE_READY
        self.inst["limit_up"] = 10.0
        QMT._INSTANCES[self.inst["code"]] = self.inst
        QMT._schedule_order_lookup = lambda _inst: None

    def tearDown(self):
        QMT._log = self.original_log
        QMT.ACCOUNT_ID = self.original_account
        QMT.ENABLE_LIVE_ORDERS = self.original_live_orders
        QMT.RESEAL_VALIDATION_CONTINUE_ON_FAILURE = self.original_continue_on_failure
        QMT._C = None
        QMT._INSTANCES.clear()

    def test_native_passorder_signature_and_async_order_and_partial_fill_callbacks(self):
        calls = []
        QMT.passorder = lambda *args: calls.append(args) or 0

        self.assertTrue(QMT._submit(self.inst, "test"))
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual(len(args), 11)
        self.assertEqual(args[0:2], (23, QMT.ORDER_TYPE_BUY))
        self.assertEqual(args[3:8], (
            "000001.SZ", QMT.PRICE_TYPE_LIMIT, 10.0, 1000, QMT.STRATEGY_NAME))
        self.assertEqual(args[8], 2)
        self.assertEqual(args[9], self.inst["submit_token"])
        self.assertIs(args[10], self.context)
        self.assertTrue(self.inst["submission_pending"])

        QMT.order_callback(None, {
            "m_nOrderID": 501,
            "m_strInstrumentID": "000001",
            "m_strExchangeID": "SZ",
            "m_nOrderStatus": 50,
            "m_nVolumeTotalOriginal": 1000,
            "m_dPrice": 10.0,
        })
        self.assertEqual(self.inst["our_order_id"], "501")
        self.assertFalse(self.inst["submission_pending"])

        QMT.order_callback(None, {
            "m_nOrderID": 501,
            "m_strInstrumentID": "000001",
            "m_strExchangeID": "SZ",
            "m_nOrderStatus": 55,
            "m_nVolumeTotalOriginal": 1000,
            "m_nVolumeTraded": 300,
            "m_dPrice": 10.0,
        })
        QMT.deal_callback(None, {
            "m_nOrderID": 501,
            "m_strInstrumentID": "000001",
            "m_strExchangeID": "SZ",
            "m_strTradeID": "trade-1",
            "m_nVolume": 200,
            "m_dPrice": 10.0,
        })
        QMT.deal_callback(None, {
            "m_nOrderID": 501,
            "m_strInstrumentID": "000001",
            "m_strExchangeID": "SZ",
            "m_strTradeID": "trade-2",
            "m_nVolume": 100,
            "m_dPrice": 10.0,
        })

        self.assertEqual(self.inst["our_traded_qty"], 300)
        self.assertNotEqual(self.inst["state"], QMT.STATE_DONE)
        self.assertEqual(QMT._COUNTERS["deals"], 2)
        self.assertTrue(any("ORDER_CALLBACK_ASSOCIATED" in line for line in self.logs))
        self.assertTrue(any("DEAL_CALLBACK" in line and "cum_volume=300" in line
                            for line in self.logs))

    def test_unknown_order_id_waits_for_matching_callback(self):
        QMT.passorder = lambda *args: 0
        QMT.get_last_order_id = lambda *args: self.fail("blind order lookup")

        self.assertTrue(QMT._submit(self.inst, "test"))
        QMT._resolve_order_id(self.inst["code"], self.inst["submit_token"])
        self.assertTrue(self.inst["submission_pending"])
        self.assertIsNone(self.inst["our_order_id"])

        QMT.order_callback(None, {
            "order_id": "501", "stock_code": "000001.SZ",
            "order_remark": self.inst["submit_token"],
            "order_volume": 1000, "order_price": 10.0,
        })
        self.assertEqual(self.inst["our_order_id"], "501")

    def test_partial_reseal_fill_keeps_remainder_under_validation(self):
        calls = []
        QMT.passorder = lambda *args: calls.append(args) or "501"
        QMT.cancel = lambda *args: True

        self.assertTrue(QMT._submit(self.inst, "回封"))
        QMT.order_callback(None, {
            "order_id": "501", "stock_code": "000001.SZ",
            "order_volume": 1000, "traded_volume": 300,
            "order_status": 55, "order_price": 10.0,
        })
        self.assertNotEqual(self.inst["state"], QMT.STATE_DONE)
        self.assertEqual(self.inst["validation_order_id"], "501")

        self.inst["verify_count"] = QMT.RESEAL_VERIFY_ORDER_COUNT
        self.inst["validation_started"] = QMT._now() - 2
        QMT._check_validation(self.inst)
        QMT.order_callback(None, {
            "order_id": "501", "stock_code": "000001.SZ",
            "order_volume": 1000, "traded_volume": 300,
            "order_status": 53, "order_price": 10.0,
        })
        QMT._check_validation(self.inst)
        self.assertEqual(self.inst["state"], QMT.STATE_DONE)
        self.assertEqual(len(calls), 1)

    def test_cancelled_validation_does_not_follow_by_default(self):
        self.assertFalse(QMT.RESEAL_VALIDATION_CONTINUE_ON_FAILURE)
        self.inst.update({
            "validation_order_id": "501",
            "validation_started": QMT._now(),
            "cancel_started": QMT._now(),
            "cancel_confirmed": True,
            "late_large_count": QMT.RESEAL_VERIFY_LARGE_COUNT,
            "last_tick": {"bid_at_limit": True},
        })
        submitted = []
        old_submit = QMT._submit
        QMT._submit = lambda _inst, reason: submitted.append(reason) or True
        try:
            QMT._check_validation(self.inst)
        finally:
            QMT._submit = old_submit

        self.assertEqual(submitted, [])
        self.assertEqual(self.inst["state"], QMT.STATE_DONE)

    def test_cancelled_validation_can_follow_when_explicitly_enabled(self):
        QMT.RESEAL_VALIDATION_CONTINUE_ON_FAILURE = True
        self.inst.update({
            "validation_order_id": "501",
            "validation_started": QMT._now(),
            "cancel_started": QMT._now(),
            "cancel_confirmed": True,
            "late_large_count": QMT.RESEAL_VERIFY_LARGE_COUNT,
            "last_tick": {"bid_at_limit": True},
        })
        submitted = []
        old_submit = QMT._submit
        QMT._submit = lambda _inst, reason: submitted.append(reason) or True
        try:
            QMT._check_validation(self.inst)
        finally:
            QMT._submit = old_submit

        self.assertEqual(submitted, ["撤单后跟随"])
        self.assertTrue(self.inst["follow_sent"])

    def test_first_seal_order_uses_shared_cancel_validation(self):
        cancel_calls = []
        old_passorder = getattr(QMT, "passorder", None)
        had_passorder = hasattr(QMT, "passorder")
        old_cancel = getattr(QMT, "cancel", None)
        had_cancel = hasattr(QMT, "cancel")
        old_schedule_validation = QMT._schedule_validation
        QMT.passorder = lambda *args: "502"
        QMT.cancel = lambda *args: cancel_calls.append(args) or True
        QMT._schedule_validation = lambda _inst: None
        try:
            self.inst["last_event_time"] = "100000"
            self.assertTrue(QMT._submit(self.inst, "首封"))
            self.assertEqual(self.inst["validation_order_id"], "502")

            self.inst["verify_count"] = QMT.RESEAL_VERIFY_ORDER_COUNT
            self.inst["validation_started"] = QMT._now() - 2
            QMT._check_validation(self.inst)

            self.assertEqual(len(cancel_calls), 1)
            self.assertEqual(cancel_calls[0][0], "502")
        finally:
            if had_passorder:
                QMT.passorder = old_passorder
            else:
                del QMT.passorder
            QMT._schedule_validation = old_schedule_validation
            if had_cancel:
                QMT.cancel = old_cancel
            else:
                del QMT.cancel

    def test_handlebar_skips_historical_bars(self):
        self.context.last_bar = False
        QMT._LAST_SUMMARY = 0.0
        QMT.handlebar(self.context)
        self.assertEqual(QMT._LAST_SUMMARY, 0.0)


if __name__ == "__main__":
    unittest.main()
