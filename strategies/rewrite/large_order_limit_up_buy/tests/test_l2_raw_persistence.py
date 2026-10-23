import ast
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_LARGE_ORDER_LIMIT_UP_BUY.py"
SPEC = importlib.util.spec_from_file_location("qmt_l2_console_diagnostics", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class NativeEntryNoFileIOTests(unittest.TestCase):
    def test_strategy_has_no_file_access_calls(self):
        tree = ast.parse(SCRIPT.read_text(encoding="gbk"))
        forbidden = {"open", "read", "write", "read_text", "read_bytes",
                     "write_text", "write_bytes", "isfile", "makedirs"}
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
            if name in forbidden:
                calls.append(name)
        self.assertEqual(calls, [])

    def test_pool_is_embedded_and_fails_closed_outside_its_trade_date(self):
        expected = [
            {"code": "600838.SH", "plan_amount": 25000.0},
            {"code": "603933.SH", "plan_amount": 21000.0},
            {"code": "603028.SH", "plan_amount": 21000.0},
            {"code": "603280.SH", "plan_amount": 21000.0},
            {"code": "002535.SZ", "plan_amount": 21000.0},
            {"code": "603089.SH", "plan_amount": 21000.0},
            {"code": "001298.SZ", "plan_amount": 21000.0},
        ]
        with mock.patch.object(QMT, "_log"), \
                mock.patch("builtins.open", side_effect=AssertionError("file I/O is disabled")) as file_open:
            self.assertEqual(QMT._load_pool("20261023"), expected)
            self.assertEqual(QMT._load_pool("20261024"), [])
            file_open.assert_not_called()

    def test_l2_diagnostics_keep_latency_counters_without_raw_records(self):
        QMT._FIRST_CALLBACKS.clear()
        QMT._PERIOD_COUNTS.clear()
        QMT._LAST_EVENT_TIMES.clear()
        QMT._LATENCY_STATS.clear()
        QMT._LATENCY_LAST_WARN.clear()
        QMT._LAST_RECEIVED_AT.clear()
        QMT._GAP_STATS.clear()
        QMT._COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0, "l2": 0})
        received_at = QMT._dt.datetime(2026, 10, 21, 10, 0, 1, 234000).timestamp()

        QMT._record_l2_ingress("000001.SZ", "l2order", {
            "stime": "100000",
            "time": "20261021100000.987",
            "price": 10.0,
            "volume": 100,
        }, received_at=received_at)

        self.assertEqual(QMT._COUNTERS["l2"], 1)
        self.assertEqual(QMT._PERIOD_COUNTS["l2order"], 1)
        self.assertAlmostEqual(QMT._LATENCY_STATS["l2order"]["last_ms"], 247.0, places=3)
        self.assertEqual(QMT._FIRST_CALLBACKS["l2order"]["event_time"], "100000")
        self.assertFalse(hasattr(QMT, "_RAW_BUFFER"))
        self.assertFalse(hasattr(QMT, "_RAW_LOG"))


if __name__ == "__main__":
    unittest.main()
