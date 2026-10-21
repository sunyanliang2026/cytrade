import json
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_LARGE_ORDER_LIMIT_UP_BUY.py"
SPEC = importlib.util.spec_from_file_location("qmt_l2_raw_persistence", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class L2RawPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.old_log = QMT._log
        self.logs = []
        QMT._log = self.logs.append
        QMT._RAW_BUFFER.clear()
        QMT._FIRST_CALLBACKS.clear()
        QMT._PERIOD_COUNTS.clear()
        QMT._LAST_EVENT_TIMES.clear()
        QMT._LATENCY_STATS.clear()
        QMT._LATENCY_LAST_WARN.clear()
        QMT._LAST_RECEIVED_AT.clear()
        QMT._GAP_STATS.clear()
        QMT._COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0, "l2": 0})

    def tearDown(self):
        QMT._log = self.old_log
        QMT._RAW_BUFFER.clear()

    def test_l2_record_keeps_only_readable_time_fields(self):
        payload = {
            "stime": "100000",
            "time": "20261021100000.987",
            "price": 10.0,
            "volume": 100,
        }
        received_at = QMT._dt.datetime(2026, 10, 21, 10, 0, 1, 234000).timestamp()

        with mock.patch.object(QMT.time, "time", return_value=received_at):
            QMT._write_raw("000001.SZ", "l2order", payload)

        record = QMT._RAW_BUFFER[-1]
        self.assertEqual(record["received_time"], "10:00:01.234")
        self.assertEqual(record["event_time"], "10:00:00.987")
        self.assertAlmostEqual(record["latency_ms"], 247.0, places=3)
        self.assertEqual(record["data"], payload)
        for field in ("received_at", "event_time_raw", "latency_time_raw"):
            self.assertNotIn(field, record)

    def test_flush_serializes_records_outside_the_callback(self):
        class RawLog:
            def __init__(self):
                self.text = ""

            def write(self, value):
                self.text += value

            def flush(self):
                pass

        raw_log = RawLog()
        old_raw_log = QMT._RAW_LOG
        QMT._RAW_LOG = raw_log
        try:
            QMT._write_raw("000001.SZ", "l2order", {
                "stime": "100000", "price": 10.0, "volume": 100,
            }, received_at=QMT.time.time())
            self.assertIsInstance(QMT._RAW_BUFFER[-1], dict)
            QMT._flush_raw(1)
        finally:
            QMT._RAW_LOG = old_raw_log
        self.assertEqual(len(QMT._RAW_BUFFER), 0)
        self.assertEqual(json.loads(raw_log.text)["code"], "000001.SZ")

    def test_event_timestamp_falls_back_to_second_precision_stime(self):
        self.assertEqual(QMT._format_event_timestamp("100000"), "10:00:00.000")


if __name__ == "__main__":
    unittest.main()
