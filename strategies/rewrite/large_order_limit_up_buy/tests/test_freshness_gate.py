import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_LARGE_ORDER_LIMIT_UP_BUY.py"
SPEC = importlib.util.spec_from_file_location("qmt_freshness_gate", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class FreshnessGateTests(unittest.TestCase):
    def setUp(self):
        self.old_log = QMT._log
        self.logs = []
        QMT._log = self.logs.append
        QMT._RAW_BUFFER.clear()
        QMT._LATEST_EVENT_EPOCH.clear()
        QMT._STARTUP_EVENT_WATERMARK.clear()
        QMT._FRESHNESS_DROPS.clear()
        QMT._FRESHNESS_LAST_LOG.clear()

    def tearDown(self):
        QMT._log = self.old_log
        QMT._RAW_BUFFER.clear()
        QMT._INSTANCES.clear()

    def test_stale_l2order_is_rejected_before_payload_copy_and_trigger(self):
        received_at = QMT._dt.datetime(2026, 10, 21, 9, 30, 3).timestamp()
        event_at = received_at - 2.0
        payload = {
            "time": str(int(event_at * 1000)),
            "stime": "20261021093001.000",
            "price": 10.0,
            "volume": 900001,
            "entrustDirection": "B",
            "entrustNo": "stale-large",
        }

        accepted, _, delay_ms = QMT._freshness_gate(
            "000001.SZ", "l2order", payload, received_at)

        self.assertFalse(accepted)
        self.assertAlmostEqual(delay_ms, 2000.0, places=3)
        self.assertEqual(len(QMT._RAW_BUFFER), 0)
        self.assertEqual(QMT._FRESHNESS_DROPS["l2order"]["count"], 1)

    def test_freshness_gate_rejects_out_of_order_absolute_event(self):
        received_at = QMT._dt.datetime(2026, 10, 21, 9, 30, 3).timestamp()
        first = str(int((received_at - 0.2) * 1000))
        older = str(int((received_at - 0.4) * 1000))

        accepted, _, _ = QMT._freshness_gate(
            "000001.SZ", "l2quote", {"time": first}, received_at)
        accepted_older, _, _ = QMT._freshness_gate(
            "000001.SZ", "l2quote", {"time": older}, received_at)

        self.assertTrue(accepted)
        self.assertFalse(accepted_older)
        self.assertEqual(QMT._FRESHNESS_DROPS["l2quote"]["last_reason"], "out_of_order")

    def test_handler_keeps_unmeasured_hhmmss_compatibility(self):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst.update({"state": QMT.STATE_READY, "limit_up": 10.0,
                     "open_price": 10.0, "day_low": 9.8})
        QMT._INSTANCES[inst["code"]] = inst
        payload = {
            "stime": "100000",
            "price": 10.0,
            "volume": 1000,
            "direction": "B",
            "entrustNo": "fresh-no-date",
        }

        with mock.patch.object(QMT, "time") as clock:
            clock.time.return_value = 1792575000.0
            clock.monotonic.return_value = 10.0
            QMT._handle_l2order(inst["code"], payload)

        self.assertEqual(len(QMT._RAW_BUFFER), 1)
        self.assertEqual(inst["last_signal_period"], "l2order")


if __name__ == "__main__":
    unittest.main()
