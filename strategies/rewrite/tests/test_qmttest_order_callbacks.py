import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "QMTTEST.py"
SPEC = importlib.util.spec_from_file_location("qmt_rewrite_order_callbacks", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class FakeContext:
    def cancel_schedule_run(self, _timer):
        pass


class QMTOrderCallbackTests(unittest.TestCase):
    def setUp(self):
        self.original_log = QMT._log
        self.original_account = QMT.ACCOUNT_ID
        self.original_live_orders = QMT.ENABLE_LIVE_ORDERS
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
        self.assertEqual(args[8], 1)
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
        self.assertEqual(QMT._COUNTERS["deals"], 2)
        self.assertTrue(any("ORDER_CALLBACK_ASSOCIATED" in line for line in self.logs))
        self.assertTrue(any("DEAL_CALLBACK" in line and "cum_volume=300" in line
                            for line in self.logs))


if __name__ == "__main__":
    unittest.main()
