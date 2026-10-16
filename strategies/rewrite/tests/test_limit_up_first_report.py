import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "QMTTEST.py"
SPEC = importlib.util.spec_from_file_location("qmt_rewrite", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)


class LimitUpFirstReportTests(unittest.TestCase):
    def setUp(self):
        QMT._LIMIT_UP_FIRSTS.clear()
        QMT._INSTANCES.clear()
        QMT._INSTANCES["000001.SZ"] = {}
        self.logs = []
        self.original_log = QMT._log
        QMT._log = self.logs.append

    def tearDown(self):
        QMT._log = self.original_log

    def test_first_limit_up_report_pairs_streams_by_receive_time(self):
        QMT._observe_first_limit_up("000001.SZ", "tick", "092959", "092959.900",
                                    1.0, 10.0, 10.0)
        QMT._observe_first_limit_up("000001.SZ", "tick", "100000", "100000.100",
                                    2.0, 10.0, 9.99)
        QMT._observe_first_limit_up("000001.SZ", "tick", "100000", "100000.200",
                                    3.0, 10.0, 10.0)
        QMT._observe_first_limit_up("000001.SZ", "l2quote", "100001", "100001.000",
                                    3.25, 10.0, 10.0)

        observed = QMT._LIMIT_UP_FIRSTS["000001.SZ"]
        self.assertEqual(observed["tick"]["received_at"], 3.0)
        self.assertEqual(observed["winner"], "tick")
        self.assertEqual(observed["gap_ms"], 250.0)
        self.assertEqual(sum("LIMIT_UP_COMPARE" in line for line in self.logs), 1)
        self.assertIn("paired=1 tick_first=1 l2quote_first=0", QMT._limit_up_first_summary())

    def test_first_limit_up_report_counts_l2quote_winner(self):
        QMT._observe_first_limit_up("000001.SZ", "l2quote", "100000", "100000.100",
                                    5.0, 10.0, 10.0)
        QMT._observe_first_limit_up("000001.SZ", "tick", "100001", "100001.000",
                                    6.5, 10.0, 10.0)

        self.assertEqual(QMT._LIMIT_UP_FIRSTS["000001.SZ"]["winner"], "l2quote")
        self.assertIn("paired=1 tick_first=0 l2quote_first=1", QMT._limit_up_first_summary())


if __name__ == "__main__":
    unittest.main()
