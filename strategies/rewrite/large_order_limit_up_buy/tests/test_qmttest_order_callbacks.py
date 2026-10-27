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
        self.original_wall_seconds = QMT._wall_seconds_since_midnight
        self.original_freshness_gate = QMT._freshness_gate
        self.had_order_query = hasattr(QMT, "get_trade_detail_data")
        self.old_order_query = getattr(QMT, "get_trade_detail_data", None)
        self.had_cancel = hasattr(QMT, "cancel")
        self.old_cancel = getattr(QMT, "cancel", None)
        self.had_passorder = hasattr(QMT, "passorder")
        self.old_passorder = getattr(QMT, "passorder", None)
        self.logs = []
        self.context = FakeContext()
        QMT._log = self.logs.append
        QMT.ACCOUNT_ID = "TEST_ACCOUNT"
        QMT.ENABLE_LIVE_ORDERS = True
        QMT._wall_seconds_since_midnight = lambda: 36001.5
        # These tests focus on order validation and callback state transitions.
        QMT._freshness_gate = lambda _code, _period, payload, _received_at, **_kwargs: (
            True, QMT._field(payload, "time", "stime", "timestamp"), None)
        QMT._C = self.context
        QMT._INSTANCES.clear()
        QMT._COUNTERS.update({"orders": 0, "deals": 0, "cancels": 0})
        self.inst = QMT._new_instance("000001.SZ", 10000)
        self.inst["state"] = QMT.STATE_READY
        self.inst["limit_up"] = 10.0
        self.inst["open_price"] = 10.0
        self.inst["day_low"] = 9.8
        QMT._INSTANCES[self.inst["code"]] = self.inst
        QMT._schedule_order_lookup = lambda _inst: None

    def tearDown(self):
        QMT._log = self.original_log
        QMT.ACCOUNT_ID = self.original_account
        QMT.ENABLE_LIVE_ORDERS = self.original_live_orders
        QMT.RESEAL_VALIDATION_CONTINUE_ON_FAILURE = self.original_continue_on_failure
        QMT._wall_seconds_since_midnight = self.original_wall_seconds
        QMT._freshness_gate = self.original_freshness_gate
        if self.had_order_query:
            QMT.get_trade_detail_data = self.old_order_query
        elif hasattr(QMT, "get_trade_detail_data"):
            del QMT.get_trade_detail_data
        if self.had_cancel:
            QMT.cancel = self.old_cancel
        elif hasattr(QMT, "cancel"):
            del QMT.cancel
        if self.had_passorder:
            QMT.passorder = self.old_passorder
        elif hasattr(QMT, "passorder"):
            del QMT.passorder
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
            "m_strRemark": self.inst["submit_token"],
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

    def test_native_system_order_id_alias_binds_exact_token_callback(self):
        self.assertEqual(
            QMT._order_fields({"order_sysid": "SYS-ALIAS"})["id"], "SYS-ALIAS")
        QMT.passorder = lambda *args: 0
        self.assertTrue(QMT._submit(self.inst, "首封大单"))
        token = self.inst["submit_token"]

        QMT.order_callback(None, {
            "m_strOrderSysID": "SYS-501",
            "m_strInstrumentID": "000001",
            "m_strExchangeID": "SZ",
            "m_strRemark": token,
            "m_nOrderStatus": 50,
            "m_nVolumeTotalOriginal": 1000,
            "m_dPrice": 10.0,
        })

        self.assertEqual(self.inst["our_order_id"], "SYS-501")
        self.assertEqual(self.inst["validation_order_id"], "SYS-501")

    def test_order_query_resolves_only_unique_exact_token(self):
        QMT.passorder = lambda *args: 0
        self.assertTrue(QMT._submit(self.inst, "首封大单"))
        token = self.inst["submit_token"]
        QMT.get_trade_detail_data = lambda *_args: [
            {
                "m_strOrderSysID": "OTHER-1",
                "m_strInstrumentID": "000001",
                "m_strExchangeID": "SZ",
                "m_strRemark": "unrelated-token",
                "m_dPrice": 10.0,
                "m_nVolumeTotalOriginal": 1000,
            },
            {
                "m_strOrderSysID": "SYS-502",
                "m_strInstrumentID": "000001",
                "m_strExchangeID": "SZ",
                "m_strRemark": token,
                "m_nOrderStatus": 50,
                "m_nVolumeTotalOriginal": 1000,
                "m_dPrice": 10.0,
            },
        ]

        QMT._resolve_order_id(self.inst["code"], token)

        self.assertEqual(self.inst["our_order_id"], "SYS-502")
        self.assertEqual(self.inst["validation_order_id"], "SYS-502")

    def test_order_query_does_not_bind_ambiguous_strategy_name_matches(self):
        QMT.passorder = lambda *args: 0
        self.assertTrue(QMT._submit(self.inst, "首封大单"))
        token = self.inst["submit_token"]
        QMT.get_trade_detail_data = lambda *_args: [
            {
                "m_strOrderSysID": "SYS-505",
                "m_strInstrumentID": "000001",
                "m_strExchangeID": "SZ",
                "m_strStrategyName": "large_order_%s" % token,
            },
            {
                "m_strOrderSysID": "SYS-506",
                "m_strInstrumentID": "000001",
                "m_strExchangeID": "SZ",
                "m_strStrategyName": "large_order_%s" % token,
            },
        ]

        QMT._resolve_order_id(self.inst["code"], token)

        self.assertIsNone(self.inst["our_order_id"])
        self.assertTrue(self.inst["submission_pending"])
        self.assertTrue(any("ORDER_ID_AMBIGUOUS" in line for line in self.logs))

    def test_validation_starts_immediately_without_order_id(self):
        def passorder_before_validation_returns(*args):
            self.assertTrue(self.inst["validation_active"])
            self.assertIsNotNone(self.inst["validation_started"])
            return 0

        QMT.passorder = passorder_before_validation_returns

        self.assertTrue(QMT._submit(self.inst, "首封大单"))

        self.assertTrue(self.inst["validation_active"])
        self.assertIsNotNone(self.inst["validation_started"])
        self.assertIsNone(self.inst["validation_order_id"])
        self.assertTrue(any("FOLLOW_VERIFY_START" in line for line in self.logs))

    def test_rejected_passorder_stops_prestarted_validation_without_cancel(self):
        cancelled_timers = []
        cancel_calls = []
        old_schedule_validation = QMT._schedule_validation
        self.context.cancel_schedule_run = cancelled_timers.append
        QMT._schedule_validation = lambda inst: inst.__setitem__("validation_timer", 77)
        QMT.cancel = lambda *args: cancel_calls.append(args) or True
        QMT.passorder = lambda *args: -1

        try:
            self.assertFalse(QMT._submit(self.inst, "首封大单"))
        finally:
            QMT._schedule_validation = old_schedule_validation

        self.assertFalse(self.inst["validation_active"])
        self.assertIsNone(self.inst["validation_timer"])
        self.assertEqual(cancelled_timers, [77])
        self.assertEqual(cancel_calls, [])
        self.assertFalse(self.inst["cancel_due"])
        self.assertTrue(any("reason=submit_rejected" in line for line in self.logs))

    def test_passorder_exception_stops_prestarted_validation_without_cancel(self):
        cancel_calls = []
        old_schedule_validation = QMT._schedule_validation
        QMT._schedule_validation = lambda _inst: None
        QMT.cancel = lambda *args: cancel_calls.append(args) or True

        def raise_passorder(*_args):
            raise RuntimeError("submission failed")

        QMT.passorder = raise_passorder
        try:
            self.assertFalse(QMT._submit(self.inst, "首封大单"))
        finally:
            QMT._schedule_validation = old_schedule_validation

        self.assertFalse(self.inst["validation_active"])
        self.assertFalse(self.inst["cancel_due"])
        self.assertEqual(cancel_calls, [])
        self.assertTrue(any("reason=passorder_exception" in line for line in self.logs))

    def test_validation_excludes_pre_submit_events_and_passes_on_two_followers(self):
        QMT.passorder = lambda *args: 0
        self.assertTrue(QMT._submit(self.inst, "首封大单"))
        self.inst["validation_event_cutoff_seconds"] = QMT._event_seconds("100000")

        self._send_order("stale", event_time="100000", volume=150000)
        self.assertEqual(self.inst["verify_count"], 0)
        self.assertEqual(self.inst["validation_stale_count"], 1)

        self._send_order("follow-1", event_time="100001", volume=150000)
        self._send_order("follow-2", event_time="100002", volume=150000)

        self.assertEqual(self.inst["verify_count"], 2)
        self.assertEqual(self.inst["verify_large_count"], 2)
        self.assertTrue(self.inst["validation_passed"])
        self.assertFalse(self.inst["validation_active"])

    def test_pre_submit_history_passes_without_waiting_or_cancel(self):
        QMT.passorder = lambda *args: 0

        self._send_order("pre-large-1", event_time="100000", volume=150000)
        self._send_order("pre-large-2", event_time="100001", volume=150000)
        self._send_order("trigger", event_time="100002", volume=900001)

        self.assertEqual(self.inst["validation_pre_count"], 2)
        self.assertEqual(self.inst["validation_pre_large_count"], 2)
        self.assertEqual(self.inst["verify_count"], 0)
        self.assertEqual(self.inst["verify_large_count"], 0)
        self.assertTrue(self.inst["validation_passed"])
        self.assertFalse(self.inst["validation_active"])
        self.assertTrue(any("FOLLOW_VERIFY_PREPASS" in line for line in self.logs))

    def test_single_pre_submit_large_does_not_count_as_post_observation(self):
        QMT.passorder = lambda *args: 0

        self._send_order("pre-large", event_time="100000", volume=150000)
        self._send_order("trigger", event_time="100001", volume=900001)

        self.assertEqual(self.inst["validation_pre_large_count"], 1)
        self.assertTrue(self.inst["validation_active"])
        self._send_order("post-large", event_time="100002", volume=150000)

        self.assertEqual(self.inst["verify_count"], 1)
        self.assertEqual(self.inst["verify_large_count"], 1)
        self.assertFalse(self.inst["validation_passed"])
        self.assertTrue(self.inst["validation_active"])

    def test_pre_submit_history_is_bounded_and_old_records_are_ignored(self):
        now = QMT._now()
        for index in range(QMT.RESEAL_PRE_SUBMIT_ORDER_MAX_COUNT + 20):
            QMT._remember_l2order(
                self.inst, "history-%d" % index, "history-%d" % index,
                "100000", "100000", 10.0, "B", 150000, 1500000.0,
                0.0, now)
        self.assertEqual(
            len(self.inst["l2order_history"]),
            QMT.RESEAL_PRE_SUBMIT_ORDER_MAX_COUNT,
        )

        self.inst["l2order_history"][-1]["received_mono"] = (
            now - QMT.RESEAL_PRE_SUBMIT_ORDER_MAX_AGE_SECONDS - 0.1)
        QMT.passorder = lambda *args: 0
        self._send_order("trigger", event_time="100001", volume=900001)
        self.assertEqual(self.inst["validation_pre_count"], 198)
        self.assertEqual(self.inst["validation_pre_large_count"], 198)

    def test_event_limit_failure_requests_cancel(self):
        cancel_calls = []
        QMT.passorder = lambda *args: "SYS-503"
        QMT.cancel = lambda *args: cancel_calls.append(args) or True
        self.assertTrue(QMT._submit(self.inst, "首封大单"))
        self.inst["validation_event_cutoff_seconds"] = QMT._event_seconds("100000")

        for index in range(QMT.RESEAL_VERIFY_ORDER_COUNT):
            self._send_order("small-%d" % index, event_time="100001", volume=1000)

        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0][0], "SYS-503")
        self.assertFalse(self.inst["validation_active"])
        self.assertTrue(any("FOLLOW_VERIFY_FAIL" in line for line in self.logs))

    def test_timeout_defers_cancel_until_unique_order_id_is_bound(self):
        cancel_calls = []
        QMT.passorder = lambda *args: 0
        QMT.cancel = lambda *args: cancel_calls.append(args) or True
        self.assertTrue(QMT._submit(self.inst, "首封大单"))
        self.inst["validation_started"] = QMT._now() - QMT.RESEAL_VALIDATION_WINDOW_SECONDS - 0.01

        QMT._validation_timer(self.inst["code"], self.inst["submit_token"])
        self.assertTrue(self.inst["cancel_due"])
        self.assertEqual(cancel_calls, [])

        QMT.order_callback(None, {
            "m_strOrderSysID": "SYS-504",
            "m_strInstrumentID": "000001",
            "m_strExchangeID": "SZ",
            "m_strRemark": self.inst["submit_token"],
            "m_nOrderStatus": 50,
            "m_nVolumeTotalOriginal": 1000,
            "m_dPrice": 10.0,
        })

        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0][0], "SYS-504")
        self.assertFalse(self.inst["cancel_due"])

    def _send_order(self, order_id, *, event_time, volume):
        QMT._handle_l2order(self.inst["code"], {
            "stime": event_time,
            "price": 10.0,
            "volume": volume,
            "direction": "B",
            "entrustNo": order_id,
        })

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
            "last_l2quote": {"bid_at_limit": True},
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
            "last_l2quote": {"bid_at_limit": True},
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
