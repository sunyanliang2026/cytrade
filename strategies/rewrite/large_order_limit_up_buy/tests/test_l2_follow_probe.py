import importlib.util
from pathlib import Path
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_L2_FOLLOW_PROBE.py"
SPEC = importlib.util.spec_from_file_location("qmt_l2_follow_probe", SCRIPT)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


class FakeContext:
    def __init__(self, bid=10.0, quote_age_ms=0):
        self.bid = bid
        self.quote_age_ms = quote_age_ms
        self.callback = None
        self.quote_callback = None
        self.scheduled = []
        self.cancelled_timers = []
        self.cancelled_orders = []
        self.unsubscribed = []
        self.accounts = []

    def set_account(self, account_id, account_type):
        self.accounts.append((account_id, account_type))

    def get_instrument_detail(self, _code):
        return {"UpStopPrice": 10.0}

    def get_full_tick(self, _codes):
        today = time.strftime("%Y%m%d")
        return {
            PROBE.TARGET_CODE: {
                "upLimit": 10.0,
                "lastPrice": 10.0,
                "bidPrice": [self.bid],
                "bidVol": [5000],
                "time": str(int(time.time() * 1000) - self.quote_age_ms),
                "stime": today + "100000000",
            }
        }

    def subscribe_quote(self, _code, **kwargs):
        if kwargs["period"] == "l2order":
            self.callback = kwargs["callback"]
            return 21
        self.quote_callback = kwargs["callback"]
        return 22

    def schedule_run(self, callback, when, repeat_times=0, name=""):
        timer_id = len(self.scheduled) + 1
        self.scheduled.append((timer_id, callback, when, repeat_times, name))
        return timer_id

    def cancel_schedule_run(self, timer_id):
        self.cancelled_timers.append(timer_id)

    def unsubscribe_quote(self, _sub_id):
        self.unsubscribed.append(_sub_id)
        return True

    def is_last_bar(self):
        return True


class L2FollowProbeTests(unittest.TestCase):
    def setUp(self):
        self.originals = {}
        for name in ("QMT_HOME", "ACCOUNT_ID", "ENABLE_LIVE_ORDERS", "passorder",
                     "cancel", "get_trade_detail_data", "_log"):
            self.originals[name] = (hasattr(PROBE, name), getattr(PROBE, name, None))
        self.temp_dir = tempfile.TemporaryDirectory()
        PROBE.QMT_HOME = self.temp_dir.name
        PROBE.ACCOUNT_ID = "32011276"
        PROBE.ENABLE_LIVE_ORDERS = True
        PROBE._C = None
        PROBE._LOG = None
        PROBE._LOG_PATH = ""
        PROBE._STATE_PATH = ""
        PROBE._SUB_ID = None
        PROBE._QUOTE_SUB_ID = None
        PROBE._PREFLIGHT_TIMER = None
        PROBE._PREFLIGHT_ACTIVE = False
        PROBE._SNAPSHOT_NEEDS_REFRESH = False
        PROBE._TOKEN = ""
        PROBE._LIMIT_UP = None
        PROBE._ORDER_ID = ""
        PROBE._ORDER_STATUS = ""
        PROBE._ORDER_QTY = PROBE.PROBE_QTY
        PROBE._ORDER_FILLED = 0.0
        PROBE._SEEN_ORDER_IDS = set()
        PROBE._SEEN_DEAL_IDS = set()
        PROBE._VALIDATION_ACTIVE = False
        PROBE._VALIDATION_RESULT = ""
        PROBE._VALIDATION_STARTED = None
        PROBE._VALIDATION_CUTOFF = None
        PROBE._VALIDATION_TIMER = None
        PROBE._VERIFY_COUNT = 0
        PROBE._VERIFY_LARGE_COUNT = 0
        PROBE._VERIFY_LARGE_IDS = set()
        PROBE._STALE_COUNT = 0
        PROBE._ORDER_LOOKUP_TIMER = None
        PROBE._ORDER_LOOKUP_ATTEMPTS = 0
        PROBE._ORDER_LOOKUP_ERROR_LOGGED = False
        PROBE._CANCEL_DUE = False
        PROBE._CANCEL_DUE_REASON = ""
        PROBE._CANCEL_REQUESTED = False
        PROBE._CANCEL_CONFIRMED = False
        PROBE._CANCEL_FAILED = False
        PROBE._ORDER_TERMINAL = False
        PROBE._ATTEMPTED = False
        PROBE._STATE = {}
        self.logs = []
        self.cancel_calls = []
        PROBE._log = self.logs.append
        PROBE.passorder = lambda *args: "SYS-603278-1"
        PROBE.cancel = lambda *args: self.cancel_calls.append(args) or 0
        self.context = FakeContext()

    def tearDown(self):
        if PROBE._LOG is not None:
            PROBE._LOG.close()
        for name, (had_value, old_value) in self.originals.items():
            if had_value:
                setattr(PROBE, name, old_value)
            elif hasattr(PROBE, name):
                delattr(PROBE, name)
        self.temp_dir.cleanup()

    def _init(self, context=None):
        PROBE.init(context or self.context)
        self.assertIsNotNone(self.context.callback if context is None else context.callback)

    def _send_order(self, order_id, event_time, amount=1000.0):
        callback = self.context.callback
        callback({
            "time": event_time,
            "stime": event_time,
            "price": 10.0,
            "volume": amount / 10.0,
            "amount": amount,
            "entrustDirection": "B",
            "entrustNo": order_id,
        })

    def test_probe_arms_validation_before_sending_one_lot_at_limit_up(self):
        calls = []

        def passorder(*args):
            self.assertTrue(PROBE._VALIDATION_ACTIVE)
            self.assertIsNotNone(PROBE._VALIDATION_TIMER)
            calls.append(args)
            return "SYS-603278-1"

        PROBE.passorder = passorder
        self._init()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 11)
        self.assertEqual(calls[0][2:7], (
            "32011276", PROBE.TARGET_CODE, PROBE.PRICE_TYPE_LIMIT, 10.0, 100))
        self.assertEqual(calls[0][8], PROBE.QUICK_TRADE)
        self.assertEqual(calls[0][9], PROBE._TOKEN)
        self.assertEqual(PROBE._ORDER_ID, "SYS-603278-1")
        self.assertTrue(any("start=before_passorder" in line for line in self.logs))

    def test_two_large_limit_up_buy_followers_are_logged_and_probe_order_cancelled(self):
        self._init()
        today = time.strftime("%Y%m%d")
        PROBE._VALIDATION_CUTOFF = PROBE._event_seconds(today + "093000000")

        self._send_order("SYS-603278-1", today + "100000500", amount=2000000.0)
        self.assertEqual(PROBE._VERIFY_COUNT, 0)
        self._send_order("stale", today + "092959000", amount=2000000.0)
        self.assertEqual(PROBE._VERIFY_COUNT, 0)
        self._send_order("large-1", today + "100001000", amount=1500000.0)
        self._send_order("large-2", today + "100002000", amount=1800000.0)

        self.assertEqual(PROBE._VALIDATION_RESULT, "PASS")
        self.assertEqual(PROBE._VERIFY_COUNT, 2)
        self.assertEqual(PROBE._VERIFY_LARGE_COUNT, 2)
        self.assertEqual(len(self.cancel_calls), 1)
        self.assertEqual(self.cancel_calls[0][0], "SYS-603278-1")
        self.assertTrue(any("probe_cleanup_after_pass" in line for line in self.logs))

    def test_timeout_without_followers_requests_cancel(self):
        self._init()
        PROBE._VALIDATION_STARTED = time.monotonic() - PROBE.VERIFY_WINDOW_SECONDS - 0.01

        PROBE._validation_timer(PROBE._TOKEN)

        self.assertEqual(PROBE._VALIDATION_RESULT, "FAIL")
        self.assertEqual(len(self.cancel_calls), 1)
        self.assertEqual(self.cancel_calls[0][0], "SYS-603278-1")
        self.assertTrue(any("reason=timeout" in line for line in self.logs))

    def test_missing_order_id_waits_for_exact_token_before_cancel(self):
        PROBE.passorder = lambda *args: 0
        self._init()
        PROBE._VALIDATION_STARTED = time.monotonic() - PROBE.VERIFY_WINDOW_SECONDS - 0.01
        PROBE._validation_timer(PROBE._TOKEN)
        self.assertTrue(PROBE._CANCEL_DUE)
        self.assertEqual(self.cancel_calls, [])
        PROBE.get_trade_detail_data = lambda *_args: [{
            "m_strOrderSysID": "SYS-EXACT",
            "m_strInstrumentID": "603278",
            "m_strExchangeID": "SH",
            "m_strRemark": PROBE._TOKEN,
            "m_nOrderStatus": 50,
            "m_nVolumeTotalOriginal": 100,
            "m_dPrice": 10.0,
        }]

        PROBE._resolve_order_id(PROBE._TOKEN)

        self.assertEqual(PROBE._ORDER_ID, "SYS-EXACT")
        self.assertEqual(len(self.cancel_calls), 1)
        self.assertEqual(self.cancel_calls[0][0], "SYS-EXACT")

    def test_explicit_passorder_rejection_stops_validation_without_cancel(self):
        PROBE.passorder = lambda *args: -1

        self._init()

        self.assertFalse(PROBE._VALIDATION_ACTIVE)
        self.assertTrue(PROBE._ORDER_TERMINAL)
        self.assertEqual(self.cancel_calls, [])
        self.assertEqual(PROBE._STATE["phase"], "submit_rejected")

    def test_filled_order_callback_marks_full_quantity_without_cancel(self):
        self._init()

        PROBE.order_callback(None, {
            "m_strOrderSysID": "SYS-603278-1",
            "m_strInstrumentID": "603278",
            "m_strExchangeID": "SH",
            "m_nOrderStatus": 56,
            "m_nVolumeTotalOriginal": 100,
        })

        self.assertEqual(PROBE._ORDER_FILLED, 100)
        self.assertTrue(PROBE._ORDER_TERMINAL)
        self.assertEqual(PROBE._STATE["phase"], "filled")
        self.assertEqual(self.cancel_calls, [])

    def test_nonsealed_snapshot_prevents_any_order(self):
        context = FakeContext(bid=9.99)
        calls = []
        PROBE.passorder = lambda *args: calls.append(args) or 0

        PROBE.init(context)

        self.assertEqual(calls, [])
        self.assertIsNone(context.callback)
        self.assertFalse(PROBE._ATTEMPTED)
        self.assertTrue(any("not sealed" in line for line in self.logs))

    def test_stale_snapshot_waits_for_fresh_sealed_l2quote_before_order(self):
        context = FakeContext(quote_age_ms=4500)
        calls = []
        PROBE.passorder = lambda *args: calls.append(args) or "SYS-603278-1"

        PROBE.init(context)

        self.assertEqual(calls, [])
        self.assertTrue(PROBE._PREFLIGHT_ACTIVE)
        self.assertIsNotNone(context.quote_callback)
        today = time.strftime("%Y%m%d")
        context.quote_callback({
            PROBE.TARGET_CODE: {
                "time": str(int(time.time() * 1000)),
                "stime": today + "100000000",
                "lastPrice": 10.0,
                "bidPrice": [10.0],
                "bidVol": [5000],
            }
        })

        self.assertEqual(len(calls), 1)
        self.assertFalse(PROBE._PREFLIGHT_ACTIVE)
        self.assertTrue(any("PREFLIGHT_L2QUOTE_OK" in line for line in self.logs))

    def test_stale_snapshot_without_fresh_l2quote_times_out_without_order(self):
        context = FakeContext(quote_age_ms=4500)
        calls = []
        PROBE.passorder = lambda *args: calls.append(args) or 0

        PROBE.init(context)
        PROBE._preflight_timeout()

        self.assertEqual(calls, [])
        self.assertFalse(PROBE._PREFLIGHT_ACTIVE)
        self.assertFalse(PROBE._ATTEMPTED)
        self.assertEqual(context.unsubscribed, [21, 22])
        self.assertTrue(any("no fresh sealed l2quote" in line for line in self.logs))

    def test_same_day_persistent_reservation_prevents_duplicate_probe(self):
        calls = []
        PROBE.passorder = lambda *args: calls.append(args) or "SYS-603278-1"
        self._init()
        first_log = PROBE._LOG
        first_log.close()
        PROBE._LOG = None
        second_context = FakeContext()

        PROBE.init(second_context)

        self.assertEqual(len(calls), 1)
        self.assertIsNone(second_context.callback)
        self.assertTrue(any("already reserved today" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
