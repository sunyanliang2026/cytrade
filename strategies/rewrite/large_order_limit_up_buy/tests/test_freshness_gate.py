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
        QMT._ENTRY_DROPS.clear()

    def tearDown(self):
        QMT._log = self.old_log
        QMT._RAW_BUFFER.clear()
        QMT._INSTANCES.clear()

    def test_stale_l2order_is_rejected_before_payload_copy_and_trigger(self):
        received_at = QMT._dt.datetime(2026, 10, 21, 9, 30, 3).timestamp()
        event_at = received_at - 2.001
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
        self.assertAlmostEqual(delay_ms, 2001.0, places=3)
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

    def test_freshness_gate_rejects_empty_zero_and_nonnumeric_event_times(self):
        received_at = QMT._dt.datetime(2026, 10, 21, 9, 30, 3).timestamp()
        invalid_values = (None, "", "   ", 0, "0", "not-a-time")

        for event_time in invalid_values:
            with self.subTest(event_time=event_time):
                accepted, _, delay_ms = QMT._freshness_gate(
                    "000001.SZ", "l2quote", {"time": event_time}, received_at)

                self.assertFalse(accepted)
                self.assertIsNone(delay_ms)
                expected_reason = (
                    "missing_event_time"
                    if event_time is None or
                    isinstance(event_time, str) and not event_time.strip()
                    else "invalid_event_time"
                )
                self.assertEqual(
                    QMT._FRESHNESS_DROPS["l2quote"]["last_reason"], expected_reason)

    def test_relaxed_freshness_limits_accept_recent_delayed_events(self):
        received_at = QMT._dt.datetime(2026, 10, 21, 9, 30, 3).timestamp()
        quote, _, quote_delay = QMT._freshness_gate(
            "000001.SZ", "l2quote",
            {"time": str(int((received_at - 2.5) * 1000))}, received_at)
        order, _, order_delay = QMT._freshness_gate(
            "000002.SZ", "l2order",
            {"time": str(int((received_at - 1.5) * 1000))}, received_at)

        self.assertTrue(quote)
        self.assertTrue(order)
        self.assertAlmostEqual(quote_delay, 2500.0, places=3)
        self.assertAlmostEqual(order_delay, 1500.0, places=3)

    def test_l2quote_event_beyond_relaxed_limit_is_rejected(self):
        received_at = QMT._dt.datetime(2026, 10, 21, 9, 30, 3).timestamp()
        accepted, _, delay_ms = QMT._freshness_gate(
            "000001.SZ", "l2quote",
            {"time": str(int((received_at - 3.001) * 1000))}, received_at)

        self.assertFalse(accepted)
        self.assertAlmostEqual(delay_ms, 3001.0, places=3)
        self.assertEqual(QMT._FRESHNESS_DROPS["l2quote"]["last_reason"], "stale_event")

    def test_freshness_gate_uses_fractional_epoch_seconds(self):
        received_at = QMT._dt.datetime(2026, 10, 21, 9, 30, 3).timestamp()
        event_at = received_at - 0.2
        event_time = "%.3f" % event_at

        accepted, _, delay_ms = QMT._freshness_gate(
            "000001.SZ", "l2order", {"time": event_time}, received_at)

        self.assertTrue(accepted)
        self.assertAlmostEqual(QMT._event_epoch_seconds(event_time), event_at, places=3)
        self.assertAlmostEqual(delay_ms, 200.0, places=2)
        self.assertAlmostEqual(QMT._latency_ms(event_time, received_at), 200.0, places=2)

    def test_second_resolution_and_session_only_times_cannot_drive_decisions(self):
        received_at = QMT._dt.datetime(2026, 10, 21, 9, 30, 3).timestamp()
        invalid_precision_values = (
            "100000.250",
            "20261021093001",
            str(int(received_at - 0.2)),
        )

        for event_time in invalid_precision_values:
            with self.subTest(event_time=event_time):
                accepted, _, delay_ms = QMT._freshness_gate(
                    "000001.SZ", "l2quote", {"time": event_time}, received_at)

                self.assertFalse(accepted)
                self.assertIsNone(delay_ms)
                self.assertEqual(
                    QMT._FRESHNESS_DROPS["l2quote"]["last_reason"],
                    "unverifiable_event_time")

    def test_l2order_entry_filters_irrelevant_and_duplicate_orders_before_copy(self):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst.update({"state": QMT.STATE_READY, "limit_up": 10.0,
                     "open_price": 10.0, "day_low": 9.8})
        QMT._INSTANCES[inst["code"]] = inst
        old_gate = QMT._freshness_gate
        QMT._freshness_gate = lambda _code, _period, payload, _received_at, **_kwargs: (
            True, QMT._field(payload, "time", "stime", "timestamp"), None)
        try:
            invalid_orders = (
                {"stime": "100000", "price": 10.0, "volume": 1000,
                 "direction": "S", "entrustNo": "sell"},
                {"stime": "100001", "price": 9.99, "volume": 1000,
                 "direction": "B", "entrustNo": "off-limit"},
                {"stime": "100002", "price": 10.0, "volume": 0,
                 "direction": "B", "entrustNo": "empty-volume"},
                {"stime": "092959", "price": 10.0, "volume": 1000,
                 "direction": "B", "entrustNo": "auction"},
            )
            with mock.patch.object(
                    QMT, "_owned_payload", wraps=QMT._owned_payload) as owned_payload:
                with mock.patch.object(QMT, "_write_raw", wraps=QMT._write_raw) as write_raw:
                    for payload in invalid_orders:
                        QMT._handle_l2order(inst["code"], payload)

                    valid = {
                        "stime": "100003", "price": 10.0, "volume": 1000,
                        "direction": "B", "entrustNo": "valid-buy",
                    }
                    QMT._handle_l2order(inst["code"], valid)
                    duplicate = dict(valid, volume=900001, stime="100004")
                    QMT._handle_l2order(inst["code"], duplicate)

                    self.assertEqual(owned_payload.call_count, 1)
                    self.assertEqual(write_raw.call_count, 1)
        finally:
            QMT._freshness_gate = old_gate

        self.assertEqual(len(QMT._RAW_BUFFER), 1)
        self.assertEqual(len(inst["l2order_history"]), 1)
        self.assertEqual(inst["last_signal_event_raw"], "100003")
        self.assertEqual(inst["seen_order_ids"], {"valid-buy"})
        self.assertEqual(QMT._ENTRY_DROPS["l2order"]["count"], 5)
        self.assertEqual(QMT._ENTRY_DROPS["l2order"]["reasons"]["duplicate"], 1)

    def test_quote_entry_skips_empty_quote_and_keeps_price_fields_that_seed_filter(self):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst.update({"state": QMT.STATE_READY, "limit_up": 11.0,
                     "last_close": 10.0})
        QMT._INSTANCES[inst["code"]] = inst
        old_gate = QMT._freshness_gate
        QMT._freshness_gate = lambda _code, _period, payload, _received_at, **_kwargs: (
            True, QMT._field(payload, "time", "stime", "timestamp"), None)
        try:
            with mock.patch.object(
                    QMT, "_owned_payload", wraps=QMT._owned_payload) as owned_payload:
                with mock.patch.object(QMT, "_write_raw", wraps=QMT._write_raw) as write_raw:
                    QMT._handle_quote(inst["code"], "l2quote", {
                        "stime": "100000", "upLimit": 11.0, "lastClose": 10.0,
                    })
                    self.assertEqual(owned_payload.call_count, 0)
                    self.assertEqual(write_raw.call_count, 0)

                    # The bid is absent, but these fresh fields seed the
                    # opening-low filter used by large-order decisions.
                    QMT._handle_quote(inst["code"], "l2quote", {
                        "stime": "100001", "upLimit": 11.0, "lastClose": 10.0,
                        "open": 10.0, "low": 9.8, "lastPrice": 10.2,
                    })
                    self.assertEqual(owned_payload.call_count, 1)
                    self.assertEqual(write_raw.call_count, 1)
        finally:
            QMT._freshness_gate = old_gate

        self.assertEqual(len(QMT._RAW_BUFFER), 1)
        self.assertEqual(inst["open_price"], 10.0)
        self.assertEqual(inst["day_low"], 9.8)
        self.assertEqual(inst["last_event_time"], "100001")
        self.assertEqual(QMT._ENTRY_DROPS["l2quote"]["reasons"]["missing_bid"], 1)

    def test_irrelevant_quote_does_not_advance_out_of_order_watermark(self):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst.update({"state": QMT.STATE_READY, "limit_up": 11.0,
                     "last_close": 10.0, "day_low": 9.0})
        QMT._INSTANCES[inst["code"]] = inst
        received_at = QMT._dt.datetime(2026, 10, 21, 10, 0, 2).timestamp()
        newer_irrelevant = received_at - 0.100
        older_useful = received_at - 0.150

        with mock.patch.object(QMT.time, "time", return_value=received_at):
            QMT._handle_quote(inst["code"], "l2quote", {
                "time": str(int(newer_irrelevant * 1000)),
                "stime": "20261021100001.900", "upLimit": 11.0,
            })
            self.assertNotIn(("l2quote", inst["code"]), QMT._LATEST_EVENT_EPOCH)

            QMT._handle_quote(inst["code"], "l2quote", {
                "time": str(int(older_useful * 1000)),
                "stime": "20261021100001.850", "upLimit": 11.0,
                "bidPrice": [11.0], "bidVol": [5000],
            })

        self.assertEqual(len(QMT._RAW_BUFFER), 1)
        self.assertAlmostEqual(
            QMT._LATEST_EVENT_EPOCH[("l2quote", inst["code"])], older_useful, places=3)
        self.assertEqual(QMT._ENTRY_DROPS["l2quote"]["reasons"]["missing_bid"], 1)

    def test_unverifiable_l2order_is_persisted_without_order_decision(self):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst.update({"state": QMT.STATE_READY, "limit_up": 10.0,
                     "open_price": 10.0, "day_low": 9.8})
        QMT._INSTANCES[inst["code"]] = inst
        payload = {
            "stime": "100000",
            "price": 10.0,
            "volume": 900001,
            "direction": "B",
            "entrustNo": "fresh-no-date",
        }

        with mock.patch.object(QMT, "time") as clock:
            clock.time.return_value = 1792575000.0
            clock.monotonic.return_value = 10.0
            with mock.patch.object(QMT, "_submit") as submit:
                QMT._handle_l2order(inst["code"], payload)
                submit.assert_not_called()

        self.assertEqual(len(QMT._RAW_BUFFER), 1)
        self.assertEqual(QMT._RAW_BUFFER[0]["event_time"], "10:00:00.000")
        self.assertIsNone(QMT._RAW_BUFFER[0]["latency_ms"])
        self.assertEqual(inst["last_signal_period"], "")
        self.assertEqual(inst["l2order_history"], [])
        self.assertEqual(inst["seen_order_ids"], set())
        self.assertEqual(inst["state"], QMT.STATE_READY)

    def test_unverifiable_l2quote_is_persisted_without_quote_state_change(self):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst.update({"state": QMT.STATE_READY, "limit_up": 10.0,
                     "last_close": 9.0, "open_price": 9.5})
        QMT._INSTANCES[inst["code"]] = inst
        payload = {
            "stime": "100000.250",
            "lastPrice": 10.0,
            "lastClose": 9.0,
            "upLimit": 10.0,
            "bidPrice": [10.0],
            "bidVol": [5000],
        }

        with mock.patch.object(QMT, "time") as clock:
            clock.time.return_value = 1792575000.0
            clock.monotonic.return_value = 10.0
            QMT._handle_quote(inst["code"], "l2quote", payload)

        self.assertEqual(len(QMT._RAW_BUFFER), 1)
        self.assertEqual(QMT._RAW_BUFFER[0]["event_time"], "10:00:00.250")
        self.assertIsNone(QMT._RAW_BUFFER[0]["latency_ms"])
        self.assertEqual(inst["last_l2quote"], {})
        self.assertEqual(inst["last_signal_period"], "")
        self.assertEqual(inst["last_event_time"], "")
        self.assertEqual(inst["state"], QMT.STATE_READY)

    def test_initial_snapshot_without_absolute_timestamp_does_not_seed_state(self):
        inst = QMT._new_instance("000001.SZ", 10000)
        inst.update({"state": QMT.STATE_WAIT_INITIAL, "name": "Test",
                     "limit_up": 10.0, "last_close": 9.0})
        snapshot = {
            inst["code"]: {
                "stime": "100000.250",
                "open": 10.0,
                "low": 9.7,
                "lastPrice": 10.0,
                "bidPrice": [10.0],
                "bidVol": [5000],
            }
        }

        with mock.patch.object(QMT, "time") as clock:
            clock.time.return_value = 1792575000.0
            clock.monotonic.return_value = 10.0
            QMT._initial_quote(object(), inst, snapshot)

        self.assertEqual(inst["last_l2quote"], {})
        self.assertIsNone(inst["open_price"])
        self.assertIsNone(inst["day_low"])
        self.assertEqual(inst["state"], QMT.STATE_WAIT_INITIAL)


if __name__ == "__main__":
    unittest.main()
