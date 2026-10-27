import importlib.util
from datetime import datetime
from pathlib import Path
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_L2_SUBSCRIPTION_PROBE.py"
SPEC = importlib.util.spec_from_file_location("qmt_l2_subscription_probe", SCRIPT)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


class FakeContext:
    def __init__(self):
        self.subscriptions = []
        self.unsubscribed = []
        self.scheduled = []
        self.cancelled = []
        self.callback_during_unsubscribe = False

    def subscribe_quote(self, code, **kwargs):
        self.subscriptions.append((code, kwargs))
        return 40 + len(self.subscriptions)

    def unsubscribe_quote(self, sub_id):
        self.unsubscribed.append(sub_id)
        if self.callback_during_unsubscribe:
            callback = self.subscriptions[-1][1]["callback"]
            callback({
                PROBE.TARGET_CODE: {
                    "time": str(int((time.time() - 1.0) * 1000)),
                    "price": 10.0,
                    "volume": 100.0,
                    "entrustDirection": "B",
                }
            })
        return True

    def schedule_run(self, callback, when, repeat_times=0, name=""):
        timer_id = len(self.scheduled) + 1
        self.scheduled.append((timer_id, callback, when, repeat_times, name))
        return timer_id

    def cancel_schedule_run(self, timer_id):
        self.cancelled.append(timer_id)


class L2SubscriptionProbeTests(unittest.TestCase):
    def setUp(self):
        self.originals = {
            "QMT_HOME": PROBE.QMT_HOME,
            "TARGET_CODE": PROBE.TARGET_CODE,
            "_log": PROBE._log,
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        PROBE.QMT_HOME = self.temp_dir.name
        PROBE.TARGET_CODE = "603278.SH"
        self.logs = []
        PROBE._log = self.logs.append
        PROBE._LOG = None
        PROBE._C = None
        PROBE._reset_state()
        self.context = FakeContext()

    def tearDown(self):
        if PROBE._LOG is not None:
            PROBE._LOG.close()
            PROBE._LOG = None
        PROBE._C = None
        PROBE._reset_state()
        for name, value in self.originals.items():
            setattr(PROBE, name, value)
        self.temp_dir.cleanup()

    def test_event_time_parses_epoch_milliseconds_and_clock_strings(self):
        reference = datetime(2026, 10, 23, 13, 5, 0).timestamp()
        expected = datetime(2026, 10, 23, 13, 0, 0, 123000).timestamp()

        self.assertAlmostEqual(
            PROBE._event_epoch("20261023130000123", reference), expected, places=3)
        self.assertAlmostEqual(
            PROBE._event_epoch("13:00:00.123", reference), expected, places=3)
        self.assertAlmostEqual(
            PROBE._event_epoch(str(int(expected * 1000)), reference), expected, places=3)

    def test_cycle_targets_next_open_and_three_minute_observation(self):
        before_afternoon = datetime(2026, 10, 23, 12, 50, 0).timestamp()
        open_epoch, cycle_epoch = PROBE._choose_cycle(before_afternoon)
        self.assertEqual(
            datetime.fromtimestamp(open_epoch).strftime("%H:%M:%S"), "13:00:00")
        self.assertEqual(cycle_epoch - open_epoch, 180.0)

        before_morning = datetime(2026, 10, 24, 9, 10, 0).timestamp()
        open_epoch, cycle_epoch = PROBE._choose_cycle(before_morning)
        self.assertEqual(
            datetime.fromtimestamp(open_epoch).strftime("%H:%M:%S"), "09:30:00")
        self.assertEqual(cycle_epoch - open_epoch, 180.0)

    def test_init_subscribes_to_l2order_only_without_account_or_trade_calls(self):
        PROBE.init(self.context)

        self.assertEqual(len(self.context.subscriptions), 1)
        code, kwargs = self.context.subscriptions[0]
        self.assertEqual(code, PROBE.TARGET_CODE)
        self.assertEqual(kwargs["period"], "l2order")
        self.assertEqual(kwargs["result_type"], "dict")
        self.assertNotIn("count", kwargs)
        self.assertNotIn("start_time", kwargs)
        self.assertFalse(hasattr(self.context, "set_account"))
        self.assertFalse(hasattr(PROBE, "passorder"))
        self.assertTrue(any("NO_TRADING" in line for line in self.logs))

    def test_callback_counts_pre_subscription_and_stale_events_without_io(self):
        PROBE.init(self.context)
        request_epoch = PROBE._PHASES["phase1"]["request_epoch"]
        PROBE._CYCLE_OPEN_EPOCH = request_epoch + 60.0
        callback = self.context.subscriptions[0][1]["callback"]
        callback({
            PROBE.TARGET_CODE: {
                "time": str(int((request_epoch - 4.0) * 1000)),
                "price": 10.0,
                "volume": 100.0,
                "entrustDirection": "B",
            }
        })

        metrics = PROBE._PHASES["phase1"]
        self.assertEqual(metrics["total_callbacks"], 1)
        self.assertEqual(metrics["before_boundary"], 1)
        self.assertEqual(metrics["stale"], 1)
        self.assertEqual(PROBE._STALE_CALLBACK_STREAK, 0)
        self.assertFalse(PROBE._BACKLOG_RESET_SCHEDULED)
        self.assertEqual(len(PROBE._SAMPLE_QUEUE), 1)
        self.assertFalse(any("CALLBACK_SAMPLE" in line for line in self.logs))

    def test_sustained_stale_callbacks_schedule_a_controlled_reset(self):
        PROBE.init(self.context)
        request_epoch = PROBE._PHASES["phase1"]["request_epoch"]
        PROBE._CYCLE_OPEN_EPOCH = request_epoch - 5.0
        callback = self.context.subscriptions[0][1]["callback"]
        stale_payload = {
            PROBE.TARGET_CODE: {
                "time": str(int((request_epoch - 4.0) * 1000)),
                "price": 10.0,
                "volume": 100.0,
            }
        }

        callback(stale_payload)
        callback(stale_payload)
        self.assertFalse(PROBE._BACKLOG_RESET_SCHEDULED)
        callback(stale_payload)

        self.assertTrue(PROBE._BACKLOG_RESET_SCHEDULED)
        self.assertEqual(self.context.unsubscribed, [])
        reset_timer = next(
            item for item in self.context.scheduled if item[4] == "backlog_reset")
        reset_timer[1](self.context)
        self.assertTrue(PROBE._BACKLOG_RESET_TRIGGERED)
        self.assertEqual(self.context.unsubscribed, [41])
        self.assertEqual(PROBE._ACTIVE_PHASE, "between_subscriptions")

    def test_observation_without_backlog_keeps_subscription_active(self):
        PROBE.init(self.context)

        PROBE._end_initial_observation(time.time())

        self.assertFalse(PROBE._BACKLOG_RESET_TRIGGERED)
        self.assertEqual(len(self.context.subscriptions), 1)
        self.assertEqual(self.context.unsubscribed, [])
        self.assertEqual(PROBE._ACTIVE_PHASE, "phase1")
        self.assertTrue(any("OPENING_OBSERVATION_CHECK" in line for line in self.logs))

    def test_stale_events_after_opening_check_still_trigger_reset(self):
        PROBE.init(self.context)
        request_epoch = PROBE._PHASES["phase1"]["request_epoch"]
        PROBE._CYCLE_OPEN_EPOCH = request_epoch - 5.0
        PROBE._end_initial_observation(time.time())
        callback = self.context.subscriptions[0][1]["callback"]
        stale_payload = {
            PROBE.TARGET_CODE: {
                "time": str(int((request_epoch - 4.0) * 1000)),
                "price": 10.0,
                "volume": 100.0,
            }
        }

        callback(stale_payload)
        callback(stale_payload)
        callback(stale_payload)

        self.assertTrue(PROBE._BACKLOG_RESET_SCHEDULED)
        self.assertEqual(self.context.unsubscribed, [])
        self.assertTrue(any(
            item[4] == "backlog_reset" for item in self.context.scheduled))

    def test_unsubscribe_window_and_resubscribe_are_measured_separately(self):
        PROBE.init(self.context)
        self.context.callback_during_unsubscribe = True
        PROBE._begin_backlog_reset(time.time())

        self.assertEqual(PROBE._CALLBACKS_DURING_UNSUBSCRIBE, 1)
        self.assertEqual(PROBE._ACTIVE_PHASE, "between_subscriptions")
        old_callback = self.context.subscriptions[0][1]["callback"]
        old_callback({
            PROBE.TARGET_CODE: {
                "time": str(int((time.time() - 1.0) * 1000)),
                "price": 10.0,
                "volume": 100.0,
            }
        })
        self.assertEqual(PROBE._POST_UNSUBSCRIBE_CALLBACKS, 1)

        self.context.callback_during_unsubscribe = False
        PROBE._resubscribe(time.time())
        self.assertEqual(len(self.context.subscriptions), 2)
        self.assertEqual(PROBE._ACTIVE_PHASE, "phase2")
        second_callback = self.context.subscriptions[1][1]["callback"]
        second_callback({
            PROBE.TARGET_CODE: {
                "time": str(int((PROBE._PHASES["phase2"]["request_epoch"] - 3.0) * 1000)),
                "price": 10.0,
                "volume": 100.0,
            }
        })
        self.assertEqual(PROBE._PHASES["phase2"]["before_boundary"], 1)
        self.assertEqual(self.context.unsubscribed, [41])
        self.assertFalse(hasattr(PROBE, "passorder"))

        old_callback({
            PROBE.TARGET_CODE: {
                "time": str(int((time.time() - 1.0) * 1000)),
                "price": 10.0,
                "volume": 100.0,
            }
        })
        self.assertEqual(PROBE._PHASES["phase2"]["total_callbacks"], 1)
        self.assertEqual(PROBE._PHASES["late_phase1"]["total_callbacks"], 1)
        self.assertEqual(PROBE._LATE_OLD_SUBSCRIPTION_CALLBACKS, 1)

    def test_manual_stop_counts_callbacks_raised_during_unsubscribe(self):
        PROBE.init(self.context)
        self.context.callback_during_unsubscribe = True

        PROBE.stop(self.context)

        self.assertEqual(PROBE._CALLBACKS_DURING_UNSUBSCRIBE, 1)
        self.assertEqual(PROBE._POST_FINAL_UNSUBSCRIBE_CALLBACKS, 0)
        self.assertTrue(PROBE._DONE)


if __name__ == "__main__":
    unittest.main()
