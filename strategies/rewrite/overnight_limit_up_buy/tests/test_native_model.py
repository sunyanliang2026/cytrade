import datetime
import importlib.util
import json
import os
import tempfile
import time
import unittest


MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "QMT_OVERNIGHT_LIMIT_UP_BUY.py"
)


class FakeContext(object):
    def __init__(self, day=None, details=None, calendar=None, cash=1000000):
        self.day = day or datetime.date.today().strftime("%Y%m%d")
        self.details = details or {}
        self.calendar = calendar if calendar is not None else [self.day]
        self.cash = cash
        self.scheduled = None
        self.account = None

    def get_trading_dates(self, stockcode, start_date, end_date, count, period):
        return self.calendar

    def get_instrument_detail(self, stock_code):
        return self.details.get(stock_code, {})

    def schedule_run(self, callback, time_point, repeat_times, interval, name):
        self.scheduled = (callback, time_point, repeat_times, interval, name)
        return name

    def cancel_schedule_run(self, task_id):
        self.scheduled = None

    def set_account(self, account_id, account_type):
        self.account = (account_id, account_type)


class NativeModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.day = datetime.date.today().strftime("%Y%m%d")
        self.model = self._load_model()
        self.default_qmt_home = self.model.QMT_HOME
        self.default_order_csv_path = self.model.ORDER_CSV_PATH
        self.default_live_orders = self.model.ENABLE_LIVE_ORDERS
        self.default_account_id = self.model.ACCOUNT_ID
        self.csv_path = os.path.join(self.temp.name, "orders.csv")
        self.state_path = os.path.join(self.temp.name, "state.json")
        self.model.ORDER_CSV_PATH = self.csv_path
        self.model.STATE_FILE_PATH = self.state_path
        self.model.ENABLE_LIVE_ORDERS = False
        self.model.ACCOUNT_ID = ""
        self._write_csv("stock_code,amount\n603005,26000\n")

    def tearDown(self):
        self.temp.cleanup()

    def _load_model(self):
        spec = importlib.util.spec_from_file_location("qmt_overnight_native_test", MODEL_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _write_csv(self, text):
        with open(self.csv_path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _write_state(self, state):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

    def _context(self, **kwargs):
        details = kwargs.pop("details", None)
        if details is None:
            details = {"603005.SH": {
                "TradingDay": self.day,
                "UpStopPrice": 13.0,
            }}
        return FakeContext(day=self.day, details=details, **kwargs)

    def test_default_paths_target_a_separate_qmt_model_and_csv(self):
        self.assertEqual(
            self.default_qmt_home,
            u"C:/\u5149\u5927\u8bc1\u5238\u91d1\u9633\u5149QMT\u5b9e\u76d8",
        )
        self.assertTrue(self.default_order_csv_path.endswith(
            os.path.join("python", "overnight_orders.csv")
        ))
        self.assertNotIn("QMTTEST.py", self.default_order_csv_path)
        self.assertTrue(self.default_live_orders)
        self.assertEqual(self.default_account_id, "32011276")

    def _run_preflight(self, context, now):
        for unused in range(20):
            if self.model._SESSION.get("phase") != "PREFLIGHT":
                break
            self.model._drive(context, now=now)

    def test_late_start_runs_all_checks_then_dry_runs_without_passorder(self):
        context = self._context()
        self.model.passorder = lambda *args: self.fail("dry run called passorder")
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))
        self.assertEqual(self.model._SESSION["phase"], "COMPLETE")
        self.assertEqual(self.model._SESSION["plans"][0]["quantity"], 2000)
        self.assertFalse(os.path.exists(self.state_path))
        self.assertIsNone(context.scheduled)

    def test_start_before_0830_waits_until_submit_time(self):
        context = self._context()
        self.model.init(context)
        before = datetime.datetime.now().replace(hour=8, minute=29)
        self._run_preflight(context, before)
        self.model._drive(context, now=before)
        self.assertEqual(self.model._SESSION["phase"], "WAITING_FOR_SUBMIT_TIME")
        self.model._drive(context, now=before.replace(minute=30))
        self.assertEqual(self.model._SESSION["phase"], "COMPLETE")
        
    def test_one_unverified_price_aborts_the_whole_batch(self):
        self._write_csv("stock_code,amount\n603005,26000\n000530,26000\n")
        context = self._context(details={
            "603005.SH": {"TradingDay": self.day, "UpStopPrice": 13.0},
            "000530.SZ": {"TradingDay": self.day},
        })
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))
        self.assertEqual(self.model._SESSION["phase"], "ABORTED")
        self.assertEqual(self.model._SESSION["dispatch_cursor"], 0)

    def test_calendar_api_is_not_required_for_scheduled_submission(self):
        context = self._context(calendar=[])
        context.get_trading_dates = None
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))
        self.assertEqual(self.model._SESSION["phase"], "COMPLETE")
        self.assertEqual(self.model._SESSION["preflight_cursor"], 1)

    def test_previous_detail_day_does_not_block_clock_scheduled_submission(self):
        previous_day = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
        context = self._context(details={
            "603005.SH": {"TradingDay": previous_day, "UpStopPrice": 13.0},
        })
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))
        self.assertEqual(self.model._SESSION["phase"], "COMPLETE")
        self.assertEqual(self.model._SESSION["plans"][0]["limit_up_price"], 13.0)

    def test_normalize_day_accepts_epoch_seconds(self):
        value = time.mktime(datetime.datetime(2026, 10, 15, 9, 0).timetuple())
        self.assertEqual(self.model._normalize_day(str(int(value))), "20261015")

    def test_existing_same_day_state_aborts_before_price_lookup(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({"trade_days": {self.day: {"status": "reserved"}}}, handle)
        context = self._context()
        self.model.init(context)
        self.model._drive(context, now=datetime.datetime.now())
        self.assertEqual(self.model._SESSION["phase"], "ABORTED")
        self.assertEqual(self.model._SESSION["preflight_cursor"], 0)

    def test_insufficient_live_cash_aborts_before_reservation_or_order(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        self.model.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_dAvailable": 100}
        ]
        self.model.passorder = lambda *args: self.fail("insufficient cash sent an order")
        context = self._context()
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))
        self.assertEqual(self.model._SESSION["phase"], "ABORTED")
        self.assertFalse(os.path.exists(self.state_path))

    def test_late_after_close_does_not_dispatch(self):
        context = self._context()
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=15, minute=0))
        self.assertEqual(self.model._SESSION["phase"], "ABORTED")
        self.assertEqual(self.model._SESSION["dispatch_cursor"], 0)

    def test_close_time_stops_remaining_dispatch_slices(self):
        self._write_csv("stock_code,amount\n603005,26000\n603005,26000\n")
        self.model.SUBMIT_ROWS_PER_TURN = 1
        context = self._context()
        self.model.init(context)
        before_close = datetime.datetime.now().replace(hour=14, minute=59)
        self._run_preflight(context, before_close)
        self.assertEqual(self.model._SESSION["dispatch_cursor"], 1)
        self.assertEqual(self.model._SESSION["phase"], "DISPATCHING")

        self.model._drive(context, now=before_close.replace(hour=15, minute=0))

        self.assertEqual(self.model._SESSION["dispatch_cursor"], 1)
        self.assertEqual(self.model._SESSION["phase"], "COMPLETE")

    def test_live_order_uses_native_signature_and_reserves_batch_first(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        context = self._context()
        calls = []
        self.model.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_dAvailable": 100000}
        ]

        def fake_passorder(*args):
            self.assertTrue(os.path.isfile(self.state_path))
            calls.append(args)
            return "123456"

        self.model.passorder = fake_passorder
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))
        self.model._drive(context, now=datetime.datetime.now().replace(hour=8, minute=31))

        self.assertEqual(context.account, ("TEST_ACCOUNT", "stock"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0:2], (23, 1101))
        self.assertEqual(calls[0][3:8], ("603005.SH", 11, 13.0, 2000, "overnight_limit_up_buy_native"))
        self.assertEqual(calls[0][8], 2)
        self.assertEqual(self.model._SESSION["phase"], "MONITORING")
        self.model.order_callback(context, {
            "orderID": "123456",
            "m_strStatusMsg": "other broker rejection",
        })
        self.assertEqual(self.model._SESSION["plans"][0]["status"], "callback_received")
        self.assertFalse(self.model._SESSION["retry_due"])

    def test_zero_return_and_blank_remark_callback_are_correlated_by_order_fields(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        context = self._context()

        def fake_trade_detail(account, account_type, kind):
            if kind == "account":
                return [{"m_dAvailable": 100000}]
            if kind == "order":
                return []
            return []

        self.model.get_trade_detail_data = fake_trade_detail
        self.model.passorder = lambda *args: 0
        self.model.init(context)
        now = datetime.datetime.now().replace(hour=8, minute=31)
        self._run_preflight(context, now)

        plan = self.model._SESSION["plans"][0]
        self.assertEqual(plan["status"], "accepted_no_id")
        self.model.order_callback(context, {
            "m_strOrderSysID": "9",
            "m_strInstrumentID": "603005.SH",
            "m_nVolumeTotalOriginal": 2000,
            "m_dPrice": 13.0,
            "m_strStatusMsg": "accepted",
        })
        self.assertEqual(plan["status"], "callback_received")
        self.assertEqual(plan["order_id"], "9")

    def test_restart_blocks_when_submitted_order_cannot_be_reconciled(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        context = self._context()
        calls = []

        def fake_trade_detail(account, account_type, kind):
            if kind == "account":
                return [{"m_dAvailable": 100000}]
            if kind == "order":
                return []
            return []

        self.model.get_trade_detail_data = fake_trade_detail
        self.model.passorder = lambda *args: calls.append(args) or 0
        now = datetime.datetime.now().replace(hour=8, minute=31)

        self.model.init(context)
        self._run_preflight(context, now)
        self.assertEqual(self.model._SESSION["phase"], "MONITORING")
        self.assertTrue(os.path.isfile(self.state_path))

        self.model.init(context)
        self._run_preflight(context, now)
        self.assertEqual(self.model._SESSION["phase"], "ABORTED")
        self.assertEqual(len(calls), 1)
        self.assertIn("query_records=0", self.model._SESSION["reason"])

    def test_junk_status_without_message_retries_twice(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        self.model.RETRY_DELAY_MS = 0
        context = self._context()
        calls = []
        self.model.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_dAvailable": 100000}
        ]
        self.model.passorder = lambda *args: calls.append(args) or str(len(calls))
        self.model.init(context)
        now = datetime.datetime.now().replace(hour=8, minute=31)
        self._run_preflight(context, now)
        self.model._drive(context, now=now)

        plan = self.model._SESSION["plans"][0]
        for expected_attempt in (1, 2, 3):
            token = plan["last_token"]
            self.model.order_callback(context, {
                "m_strRemark": token,
                "m_nOrderStatus": 57,
                "m_nVolumeTraded": 0,
                "m_strStatusMsg": "",
            })
            if expected_attempt < 3:
                self.model._drive(context, now=now)
                self.assertEqual(plan["attempts"], expected_attempt + 1)

        self.assertEqual(len(calls), 3)
        self.assertEqual(plan["status"], "rejected_max_retries")
        self.assertNotIn(plan["row_no"], self.model._SESSION["retry_due"])

    def test_field_only_junk_callback_after_retry_is_not_bound_to_latest_attempt(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        self.model.RETRY_DELAY_MS = 0
        context = self._context()
        calls = []
        self.model.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_dAvailable": 100000}
        ]
        self.model.passorder = lambda *args: calls.append(args) or str(len(calls))
        self.model.init(context)
        now = datetime.datetime.now().replace(hour=8, minute=31)
        self._run_preflight(context, now)
        plan = self.model._SESSION["plans"][0]

        field_only_junk = {
            "m_strInstrumentID": "603005.SH",
            "m_nVolumeTotalOriginal": 2000,
            "m_dPrice": 13.0,
            "m_nOrderStatus": 57,
        }
        self.model.order_callback(context, field_only_junk)
        self.model._drive(context, now=now)
        self.assertEqual(plan["attempts"], 2)
        self.assertEqual(len(calls), 2)

        self.model.order_callback(context, field_only_junk)
        self.assertFalse(self.model._SESSION["retry_due"])
        self.assertEqual(len(calls), 2)

    def test_junk_retry_waits_at_least_100ms(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        context = self._context()
        calls = []
        self.model.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_dAvailable": 100000}
        ]
        self.model.passorder = lambda *args: calls.append(args) or str(len(calls))
        self.model.init(context)
        now = datetime.datetime.now().replace(hour=8, minute=31)
        self._run_preflight(context, now)

        original_monotonic = self.model.time.monotonic
        clock = [10.0]
        self.model.time.monotonic = lambda: clock[0]
        try:
            plan = self.model._SESSION["plans"][0]
            self.model.order_callback(context, {
                "m_strRemark": plan["last_token"],
                "m_nOrderStatus": 57,
                "m_nVolumeTraded": 0,
            })
            self.assertAlmostEqual(self.model._SESSION["retry_due"][2], 10.1)
            clock[0] = 10.099
            self.model._drive(context, now=now)
            self.assertEqual(len(calls), 1)
            clock[0] = 10.1
            self.model._drive(context, now=now)
            self.assertEqual(len(calls), 2)
        finally:
            self.model.time.monotonic = original_monotonic

    def test_partial_fill_junk_does_not_retry(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        context = self._context()
        calls = []
        self.model.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_dAvailable": 100000}
        ]
        self.model.passorder = lambda *args: calls.append(args) or str(len(calls))
        self.model.init(context)
        now = datetime.datetime.now().replace(hour=8, minute=31)
        self._run_preflight(context, now)
        plan = self.model._SESSION["plans"][0]

        self.model.order_callback(context, {
            "m_strRemark": plan["last_token"],
            "m_nOrderStatus": 57,
            "m_nVolumeTraded": 100,
        })

        self.assertEqual(len(calls), 1)
        self.assertEqual(plan["status"], "rejected_after_fill")
        self.assertFalse(self.model._SESSION["retry_due"])

    def test_late_deal_callback_cancels_a_scheduled_junk_retry(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        context = self._context()
        calls = []
        self.model.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_dAvailable": 100000}
        ]
        self.model.passorder = lambda *args: calls.append(args) or str(len(calls))
        self.model.init(context)
        now = datetime.datetime.now().replace(hour=8, minute=31)
        self._run_preflight(context, now)
        plan = self.model._SESSION["plans"][0]
        order_id = plan["order_id"]

        self.model.order_callback(context, {
            "orderID": order_id,
            "m_nOrderStatus": 57,
            "m_nVolumeTraded": 0,
        })
        self.assertIn(plan["row_no"], self.model._SESSION["retry_due"])

        self.model.deal_callback(context, {
            "order_id": order_id,
            "traded_id": "deal-1",
            "stock_code": "603005.SH",
            "traded_volume": 100,
            "traded_price": 13.0,
        })
        self.assertNotIn(plan["row_no"], self.model._SESSION["retry_due"])

        self.model._drive(context, now=now)
        self.assertEqual(len(calls), 1)
        self.assertEqual(plan["status"], "partial_filled")

    def test_changed_csv_releases_only_a_verified_zero_fill_junk_order(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        self._write_csv("stock_code,amount\n603278,10000\n")
        self._write_state({"trade_days": {self.day: {
            "status": "stopped_monitoring",
            "orders": {"2": {
                "stock_code": "603005.SH",
                "amount": 26000.0,
                "limit_up_price": 13.0,
                "quantity": 2000,
                "attempts": 1,
                "last_token": "old-token",
                "order_id": "old-id",
                "status": "callback_received",
            }},
        }}})
        context = self._context(details={
            "603278.SH": {"TradingDay": self.day, "UpStopPrice": 13.0},
        })
        calls = []

        def fake_trade_detail(account, account_type, kind):
            if kind == "account":
                return [{"m_dAvailable": 100000}]
            if kind == "order":
                return [{
                    "order_id": "old-id",
                    "order_remark": "old-token",
                    "stock_code": "603005.SH",
                    "order_volume": 2000,
                    "price": 13.0,
                    "order_status": 57,
                    "traded_volume": 0,
                }]
            return []

        self.model.get_trade_detail_data = fake_trade_detail
        self.model.passorder = lambda *args: calls.append(args) or "new-id"
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))

        self.assertEqual(self.model._SESSION["phase"], "MONITORING")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], "603278.SH")

    def test_qmt_order_sysid_reconciles_legacy_rejected_order(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        self._write_csv("stock_code,amount\n603278,10000\n")
        self._write_state({"trade_days": {self.day: {
            "status": "stopped_monitoring",
            "orders": {"2": {
                "stock_code": "603197.SH",
                "amount": 10000.0,
                "limit_up_price": 32.33,
                "quantity": 300,
                "attempts": 1,
                "last_token": "old-token",
                "order_id": "xt1209008129",
                "status": "callback_received",
            }},
        }}})
        context = self._context(details={
            "603278.SH": {"TradingDay": self.day, "UpStopPrice": 12.5},
        })
        calls = []
        order = type("XtOrder", (), {
            "stock_code": "603197.SH",
            "order_sysid": "xt1209008129",
            "order_volume": 300,
            "price": 32.33,
            "order_status": 57,
            "traded_volume": 0,
            "strategy_name": "overnight_limit_up_buy_native_&&&_old-token_&&&_OVERNIGHT_LIMIT_UP_BUY",
        })()

        def fake_trade_detail(account, account_type, kind):
            if kind == "account":
                return [{"m_dAvailable": 100000}]
            if kind == "order":
                return [order]
            return []

        self.model.get_trade_detail_data = fake_trade_detail
        self.model.passorder = lambda *args: calls.append(args) or "new-id"
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))

        self.assertEqual(self.model._SESSION["phase"], "MONITORING")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], "603278.SH")

    def test_same_csv_restart_resumes_retry_count_after_verified_junk(self):
        self.model.ENABLE_LIVE_ORDERS = True
        self.model.ACCOUNT_ID = "TEST_ACCOUNT"
        self._write_state({"trade_days": {self.day: {
            "status": "stopped_monitoring",
            "orders": {"2": {
                "stock_code": "603005.SH",
                "amount": 26000.0,
                "limit_up_price": 13.0,
                "quantity": 2000,
                "attempts": 1,
                "last_token": "old-token",
                "order_id": "old-id",
                "status": "callback_received",
            }},
        }}})
        context = self._context()
        calls = []

        def fake_trade_detail(account, account_type, kind):
            if kind == "account":
                return [{"m_dAvailable": 100000}]
            if kind == "order":
                return [{
                    "order_id": "old-id",
                    "order_remark": "old-token",
                    "stock_code": "603005.SH",
                    "order_volume": 2000,
                    "price": 13.0,
                    "order_status": 57,
                    "traded_volume": 0,
                }]
            return []

        self.model.get_trade_detail_data = fake_trade_detail
        self.model.passorder = lambda *args: calls.append(args) or "new-id"
        self.model.init(context)
        self._run_preflight(context, datetime.datetime.now().replace(hour=8, minute=31))

        plan = self.model._SESSION["plans"][0]
        self.assertEqual(self.model._SESSION["phase"], "DISPATCHING")
        self.assertEqual(plan["attempts"], 1)
        self.assertEqual(plan["status"], "retry_wait")
        self.assertIn(plan["row_no"], self.model._SESSION["retry_due"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
