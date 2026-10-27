import datetime
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "QMT_LARGE_ORDER_LIMIT_UP_BUY.py"
SPEC = importlib.util.spec_from_file_location("qmt_dynamic_l2order", SCRIPT)
QMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QMT)
DATETIME = datetime.datetime


class FakeContext:
    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []
        self.scheduled = []
        self.reject = False

    def set_account(self, account, account_type):
        assert account == "TEST_ACCOUNT"
        assert account_type == "stock"

    def subscribe_quote(self, code, **kwargs):
        self.subscribed.append((code, kwargs))
        return 0 if self.reject else len(self.subscribed) + 100

    def unsubscribe_quote(self, subscription_id):
        self.unsubscribed.append(subscription_id)
        return True

    def schedule_run(self, callback, target, **kwargs):
        self.scheduled.append((callback, target, kwargs))
        return len(self.scheduled)


class DynamicL2OrderSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.context = FakeContext()
        self.logs = []
        self.patches = [
            mock.patch.object(QMT, "_C", self.context),
            mock.patch.object(QMT, "_log", self.logs.append),
            mock.patch.object(QMT, "ENABLE_LIVE_ORDERS", False),
            mock.patch.object(QMT, "ACCOUNT_ID", "TEST_ACCOUNT"),
            mock.patch.object(QMT, "_submit"),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.cleanup)
        QMT._INSTANCES.clear()
        QMT._SUBSCRIPTIONS[:] = []
        QMT._SUB_SUCCESS = QMT._SUB_FAILED = 0
        QMT._L2ORDER_SUBSCRIBE_TIMER = None
        QMT._LATEST_EVENT_EPOCH.clear()
        QMT._STARTUP_EVENT_WATERMARK.clear()
        QMT._FRESHNESS_DROPS.clear()
        QMT._FRESHNESS_LAST_LOG.clear()
        QMT._ENTRY_DROPS.clear()
        QMT._CALLBACK_COUNTS.clear()
        self.inst = self.instance()

    def cleanup(self):
        QMT._INSTANCES.clear()
        QMT._SUBSCRIPTIONS[:] = []
        for patch in reversed(self.patches):
            patch.stop()

    def instance(self, code="000001.SZ"):
        inst = QMT._new_instance(code, 10000)
        inst.update({
            "name": "TEST", "state": QMT.STATE_READY, "limit_up": 11.0,
            "last_close": 10.0, "open_price": 10.0, "day_low": 9.8,
            "last_l2quote": {"bid_at_limit": False, "bidPrice": 10.5, "bidVol": 1000},
        })
        QMT._INSTANCES[code] = inst
        return inst

    def epoch(self, hour=9, minute=30, second=30, millisecond=0):
        return DATETIME(2026, 10, 22, hour, minute, second,
                        millisecond * 1000).timestamp()

    def quote(self, last=10.71, now=None, event_at=None, **fields):
        now = self.epoch() if now is None else now
        event_at = now if event_at is None else event_at
        payload = {
            "time": str(int(round(event_at * 1000))),
            "stime": DATETIME.fromtimestamp(event_at).strftime("%Y%m%d%H%M%S.%f")[:-3],
            "lastPrice": last, "lastClose": 10.0,
            "bidPrice": [10.5], "bidVol": [1000],
        }
        payload.update(fields)
        with mock.patch.object(QMT.time, "time", return_value=now):
            QMT._handle_quote(self.inst["code"], "l2quote", payload)
        return payload

    def frozen_datetime(self, now):
        class FrozenDatetime(DATETIME):
            @classmethod
            def now(cls, tz=None):
                return DATETIME.fromtimestamp(now, tz)
        return mock.patch.object(QMT._dt, "datetime", FrozenDatetime)

    def test_first_thirty_seconds_only_cache_and_boundary_subscribes(self):
        before = self.epoch(second=29, millisecond=900)
        self.quote(now=before)
        self.assertEqual(self.context.subscribed, [])
        with mock.patch.object(QMT.time, "time", return_value=self.epoch()):
            QMT._subscribe_l2order_now(self.context)
        self.assertEqual(len(self.context.subscribed), 2)
        self.assertEqual(
            {item[1]["period"] for item in self.context.subscribed},
            {"l2order", "l2transaction"},
        )

    def test_native_init_logs_policy_and_subscribes_only_quotes_before_boundary(self):
        now = self.epoch(second=10)
        pool = [{"code": self.inst["code"], "plan_amount": 10000}]
        with self.frozen_datetime(now), mock.patch.object(QMT.time, "time", return_value=now), \
                mock.patch.object(QMT, "_load_pool", return_value=pool), \
                mock.patch.object(QMT, "_initial_quote_batch"), \
                mock.patch.object(QMT, "_schedule_auction_retry"):
            QMT.init(self.context)
        self.assertEqual(len(self.context.subscribed), 1)
        self.assertEqual(self.context.subscribed[0][1]["period"], "l2quote")
        self.assertEqual(self.context.scheduled[0][1], DATETIME(2026, 10, 22, 9, 30, 30))
        self.assertTrue(any("gain>7%,retain_after_opening" in log for log in self.logs))
        self.inst = QMT._INSTANCES[self.inst["code"]]
        self.quote(now=self.epoch())
        self.assertEqual(len(self.context.subscribed), 3)
        self.assertEqual(
            {item[1]["period"] for item in self.context.subscribed[1:]},
            {"l2order", "l2transaction"},
        )

    def test_gain_is_strictly_above_seven_percent_before_business_filter(self):
        self.quote(last=10.70)
        self.assertEqual(self.context.subscribed, [])
        self.quote(last=10.71, now=self.epoch(millisecond=100))
        self.assertEqual(len(self.context.subscribed), 2)
        self.assertEqual(QMT._ENTRY_DROPS["l2quote"]["reasons"]["unchanged_quote"], 2)
        self.assertNotIn(("l2quote", self.inst["code"]), QMT._LATEST_EVENT_EPOCH)
        self.assertTrue(any("reason=gain_gt_7pct" in log for log in self.logs))

    def test_newer_below_threshold_or_missing_price_invalidates_cached_candidate(self):
        for last in (10.70, 10.60, None, 0, float("nan"), float("inf")):
            with self.subTest(last=last):
                self.inst["l2order_candidate"] = None
                self.quote(now=self.epoch(second=29, millisecond=700))
                self.quote(last=last, now=self.epoch(second=29, millisecond=900))
                with mock.patch.object(QMT.time, "time", return_value=self.epoch()):
                    QMT._subscribe_l2order_now(self.context)
                self.assertEqual(self.context.subscribed, [])

    def test_newer_subscription_quote_blocks_older_crossing_without_business_watermark(self):
        now = self.epoch(second=25)
        self.quote(last=10.6, now=now, event_at=now - 0.1)
        self.quote(last=10.8, now=now, event_at=now - 0.2)
        self.assertEqual(self.context.subscribed, [])
        self.assertAlmostEqual(self.inst["l2order_candidate"][0], now - 0.1)
        self.assertNotIn(("l2quote", self.inst["code"]), QMT._LATEST_EVENT_EPOCH)

    def test_invalid_stale_future_and_out_of_order_quotes_cannot_subscribe(self):
        now = self.epoch(second=25)
        bad_times = (None, "", 0, "garbage", "093025", str(int((now - 3.001) * 1000)),
                     str(int((now + 1.001) * 1000)))
        for event_raw in bad_times:
            with self.subTest(event_raw=event_raw):
                self.quote(now=now, time=event_raw, stime=None)
                self.assertEqual(self.context.subscribed, [])
                self.assertIsNone(self.inst["l2order_candidate"])
        QMT._LATEST_EVENT_EPOCH[("l2quote", self.inst["code"])] = now
        self.quote(now=now, event_at=now - 0.2)
        self.assertEqual(self.context.subscribed, [])

    def test_timer_does_not_subscribe_stale_or_absent_candidates(self):
        now = self.epoch(second=35)
        self.inst["l2order_candidate"] = (now - 2.001, 11.0, 10.0)
        with mock.patch.object(QMT.time, "time", return_value=now):
            QMT._subscribe_l2order_now(self.context)
            self.inst["l2order_candidate"] = None
            QMT._subscribe_l2order_now(self.context)
        self.assertEqual(self.context.subscribed, [])

    def test_subscription_is_retained_through_price_decline_opening_and_reseal(self):
        self.quote()
        subscription_ids = list(self.inst["sub_ids"])
        self.quote(last=10.3, now=self.epoch(second=31))
        self.quote(last=11.0, now=self.epoch(second=32), bidPrice=[11.0])
        self.quote(last=10.5, now=self.epoch(second=33))
        self.assertEqual(len(self.context.subscribed), 2)
        self.assertEqual(self.context.unsubscribed, [])
        self.assertEqual(self.inst["sub_ids"], subscription_ids)

    def test_existing_negative_return_exit_still_unsubscribes(self):
        self.quote()
        subscription_ids = list(self.inst["sub_ids"])
        self.quote(last=9.9, now=self.epoch(second=31))
        self.assertTrue(self.inst["subscriptions_stopped"])
        self.assertEqual(self.inst["state"], QMT.STATE_SKIPPED)
        self.assertEqual(self.context.unsubscribed, subscription_ids)

    def test_quote_reseal_does_not_wait_for_first_order_callback_or_registration(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                self.inst = self.instance()
                self.inst.update({"state": QMT.STATE_WAIT_RESEAL,
                                  "reseal_ready": True, "break_started": QMT._now() - 4.0})
                self.context.reject = rejected
                QMT._submit.reset_mock()
                self.quote(last=11.0, bidPrice=[11.0])
                QMT._submit.assert_called_once_with(self.inst, "回封")
                self.assertIsNone(self.inst["l2order_first_callback_at"])

    def test_rejected_subscription_retries_on_fresh_quotes_with_cooldown_and_limit(self):
        self.context.reject = True
        now = self.epoch()
        self.quote(now=now)
        self.quote(now=now + 0.9)
        self.assertEqual(len(self.context.subscribed), 2)
        self.quote(now=now + 1.0)
        self.quote(now=now + 2.0)
        self.quote(now=now + 20.0)
        self.assertEqual(len(self.context.subscribed), QMT.L2ORDER_SUBSCRIBE_MAX_ATTEMPTS * 2)
        self.assertEqual(QMT._SUB_FAILED, 6)

    def test_failed_subscription_recovery_uses_native_arguments_and_avoids_duplicates(self):
        self.context.reject = True
        self.quote()
        self.context.reject = False
        self.quote(now=self.epoch(second=31))
        self.quote(now=self.epoch(second=32))
        self.assertEqual(len(self.context.subscribed), 4)
        kwargs = self.context.subscribed[-1][1]
        self.assertEqual(set(kwargs), {"period", "dividend_type", "result_type", "callback"})
        self.assertEqual(kwargs["result_type"], "dict")

    def test_opening_timer_batches_requests_and_rechecks_remaining_stocks(self):
        now = self.epoch()
        for index in range(QMT.L2ORDER_SUBSCRIBE_BATCH + 2):
            inst = self.instance("%06d.SZ" % (index + 1))
            inst["l2order_candidate"] = (now - 0.1, 10.8, 10.0)
        with mock.patch.object(QMT.time, "time", return_value=now):
            QMT._subscribe_l2order_now(self.context)
        self.assertEqual(len(self.context.subscribed), QMT.L2ORDER_SUBSCRIBE_BATCH * 2)
        self.assertEqual(len(self.context.scheduled), 1)
        remaining = QMT._INSTANCES["000012.SZ"]
        remaining["l2order_candidate"] = (now, 10.6, 10.0)
        with mock.patch.object(QMT.time, "time", return_value=now + 0.1):
            self.context.scheduled[0][0](self.context)
        self.assertEqual(len(self.context.subscribed), (QMT.L2ORDER_SUBSCRIBE_BATCH + 1) * 2)

    def test_timer_failure_cannot_bypass_opening_delay_and_quotes_still_activate(self):
        now = self.epoch(second=29)
        self.quote(now=now)
        with self.frozen_datetime(now), mock.patch.object(
                self.context, "schedule_run", side_effect=RuntimeError("timer unavailable")):
            QMT._schedule_l2order_subscriptions(self.context)
        self.assertEqual(self.context.subscribed, [])
        self.quote(now=self.epoch())
        self.assertEqual(len(self.context.subscribed), 2)

    def test_schedule_targets_opening_boundary_and_next_day_after_close(self):
        for now, target_day in ((self.epoch(second=10), 22), (self.epoch(hour=15), 23)):
            with self.subTest(now=now), self.frozen_datetime(now):
                QMT._schedule_l2order_subscriptions(self.context)
                self.assertEqual(self.context.scheduled[-1][1],
                                 DATETIME(2026, 10, target_day, 9, 30, 30))
        self.assertEqual(self.context.subscribed, [])

    def test_intraday_start_uses_valid_snapshot_but_does_not_fan_out(self):
        now = self.epoch(hour=10, minute=0, second=0)
        payload = self.quote(now=now, last=10.6)
        payload["lastPrice"] = 10.8
        with mock.patch.object(QMT.time, "time", return_value=now):
            QMT._initial_quote(self.context, self.inst, {self.inst["code"]: payload})
            below = self.instance("000002.SZ")
            below["l2order_candidate"] = (now, 10.6, 10.0)
            with self.frozen_datetime(now):
                QMT._schedule_l2order_subscriptions(self.context)
        self.assertEqual(
            [code for code, _ in self.context.subscribed],
            [self.inst["code"], self.inst["code"]],
        )

    def test_terminal_stopped_and_outside_session_instances_do_not_subscribe(self):
        now = self.epoch()
        self.inst["l2order_candidate"] = (now, 11.0, 10.0)
        for state in (QMT.STATE_DONE, QMT.STATE_SKIPPED):
            self.inst["state"] = state
            QMT._try_subscribe_l2order(self.context, self.inst, now)
        self.inst["state"] = QMT.STATE_READY
        self.inst["subscriptions_stopped"] = True
        QMT._try_subscribe_l2order(self.context, self.inst, now)
        self.inst["subscriptions_stopped"] = False
        for outside in (self.epoch(hour=12), self.epoch(hour=15)):
            self.inst["l2order_candidate"] = (outside, 11.0, 10.0)
            QMT._try_subscribe_l2order(self.context, self.inst, outside)
        self.assertEqual(self.context.subscribed, [])

    def test_first_callback_records_arrival_even_for_sell_and_stale_data(self):
        self.quote()
        callback = next(
            item[1]["callback"] for item in self.context.subscribed
            if item[1]["period"] == "l2order"
        )
        now = self.epoch(second=34)
        with mock.patch.object(QMT.time, "time", return_value=now):
            callback({"time": str(int(self.epoch() * 1000)), "direction": "S"})
            callback({"time": str(int(now * 1000)), "direction": "S"})
        first = [log for log in self.logs if "L2ORDER_FIRST_CALLBACK" in log]
        self.assertEqual(len(first), 1)
        self.assertIn("request_to_callback_ms=4000", first[0])
        self.assertEqual(QMT._CALLBACK_COUNTS["l2order"], 2)
        self.assertEqual(self.inst["l2order_history"], [])
        QMT._submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
