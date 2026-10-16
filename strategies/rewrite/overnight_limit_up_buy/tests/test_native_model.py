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

    def test_restart_releases_reservation_when_no_matching_broker_order_exists(self):
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
        self.assertEqual(self.model._SESSION["phase"], "MONITORING")
        self.assertEqual(len(calls), 2)

    def test_only_explicit_preopen_rejection_is_retried_twice(self):
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
                "m_strStatusMsg": "\u672a\u5230\u5f00\u5e02\u65f6\u95f4,\u7cfb\u7edf\u7981\u6b62\u59d4\u6258",
            })
            if expected_attempt < 3:
                self.model._drive(context, now=now)
                self.assertEqual(plan["attempts"], expected_attempt + 1)

        self.assertEqual(len(calls), 3)
        self.assertEqual(plan["status"], "rejected_preopen_max_retries")
        self.assertNotIn(plan["row_no"], self.model._SESSION["retry_due"])


if __name__ == "__main__":
    unittest.main()
