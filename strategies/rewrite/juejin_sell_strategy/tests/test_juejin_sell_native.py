import importlib.util
from pathlib import Path
import tempfile
import unittest


MODEL_PATH = Path(__file__).resolve().parents[1] / "QMT_JUEJIN_SELL.py"
SPEC = importlib.util.spec_from_file_location("juejin_sell_native_test", MODEL_PATH)
MODEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL)


class _Context:
    def __init__(self, details=None, ticks=None):
        self.details = details or {}
        self.ticks = ticks or {}
        self.subscriptions = []
        self.cancels = []

    def get_instrument_detail(self, code):
        return self.details.get(code, {})

    def get_full_tick(self, codes):
        return {code: self.ticks.get(code, {}) for code in codes}

    def subscribe_quote(self, code, **kwargs):
        self.subscriptions.append((code, kwargs.get("period")))
        return len(self.subscriptions)

    def unsubscribe_quote(self, sub_id):
        return sub_id


class NativeSellModelTests(unittest.TestCase):
    def setUp(self):
        MODEL._INSTANCES.clear()
        MODEL._SUBSCRIPTIONS[:] = []
        MODEL._C = None
        MODEL._LOG = None
        MODEL.ENABLE_LIVE_ORDERS = False
        MODEL.ACCOUNT_ID = ""
        self.plan = {
            "code": "000001.SZ",
            "nick": "test",
            "exp": 0,
            "sell_quantity": 200,
        }
        self.inst = MODEL._new_instance(self.plan)
        self.inst["pre_close"] = 10.0
        self.inst["limit_up"] = 11.0
        self.inst["limit_down"] = 9.0
        self.inst["open_price"] = 10.5
        MODEL._INSTANCES[self.inst["code"]] = self.inst

    def tearDown(self):
        for name in ("get_trade_detail_data", "passorder", "cancel", "get_last_order_id"):
            if hasattr(MODEL, name):
                delattr(MODEL, name)
        MODEL.ENABLE_LIVE_ORDERS = False
        MODEL.ACCOUNT_ID = ""

    def _quote(self, at, bid, bid_volume=110000, ask=None, ask_volume=1000,
               high=None, low=None):
        return {
            "stime": at,
            "preClose": 10.0,
            "open": 10.5,
            "bidPrice": [bid],
            "bidVol": [bid_volume],
            "askPrice": [ask if ask is not None else bid],
            "askVol": [ask_volume],
            "lastPrice": bid,
            "high": high if high is not None else bid,
            "low": low if low is not None else bid,
        }

    def _l2(self, at, bid, **kwargs):
        MODEL._process_l2quote(self.inst, self._quote(at, bid, **kwargs), MODEL._event_seconds(at))

    def test_utf8_csv_loads_symbol_quantity_and_unicode_nick(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sell_10.csv"
            path.write_text("symbol,exp,sellvol,nick\n600088,0,1200,传媒\n", encoding="utf-8-sig")

            plans = MODEL._load_pool(str(path))

        self.assertEqual(plans[0]["code"], "600088.SH")
        self.assertEqual(plans[0]["sell_quantity"], 1200)
        self.assertEqual(plans[0]["nick"], "传媒")

    def _reach_limit_up_and_open(self):
        self._l2("093500", 11.0, high=11.0, low=10.5)
        self.assertEqual(self.inst["flag"], MODEL.FLAG_LIMIT_UP)
        # Native elapsed-time rules use monotonic time, not the quote label.
        self.inst["limit_up_seen_at"] = MODEL._now() - 61
        self._l2("093700", 10.95, bid_volume=20000, ask=10.96, high=11.0, low=10.5)
        self.assertEqual(self.inst["up_sell"], MODEL.LIMIT_UP_FIRST_SELL_DONE)
        self.assertEqual(self.inst["open_flag"], MODEL.FLAG_LIMIT_UP)
        self.assertEqual(self.inst["flag"], MODEL.FLAG_SELL_DONE)

    def test_limit_up_open_then_break_three_percent_submits_second_sell(self):
        self._reach_limit_up_and_open()
        self._l2("093701", 10.25, bid_volume=10000, ask=10.26, high=11.0, low=10.2)

        self.assertEqual(len(self.inst["orders"]), 2)
        self.assertEqual(self.inst["orders"][-1]["action"], "limit_open_second_sell")
        self.assertEqual(self.inst["orders"][-1]["quantity"], 200)
        self.assertEqual(self.inst["orders"][-1]["price"], 10.15)
        self.assertTrue(self.inst["limit_open_second_sell_done"])

    def test_limit_up_open_five_minutes_only_restores_normal_price_logic(self):
        self._reach_limit_up_and_open()
        self.inst["limit_open_started_at"] = MODEL._now() - 301
        self._l2("094301", 10.80, bid_volume=1000000, ask=10.81, high=11.0, low=10.5)

        self.assertEqual(len(self.inst["orders"]), 1)
        self.assertEqual(self.inst["open_flag"], 0)
        self.assertEqual(self.inst["flag"], MODEL.FLAG_STRONG)

    def test_flag_five_only_follows_weak_state(self):
        self._l2("093000", 10.50, high=10.60)
        self.assertEqual(self.inst["flag"], MODEL.FLAG_NORMAL)

        self._l2("093001", 9.70, high=10.60, low=9.70)
        self.assertEqual(self.inst["flag"], MODEL.FLAG_WEAK)

        self._l2("093100", 10.50, high=10.60, low=9.70)
        self.assertEqual(self.inst["flag"], MODEL.FLAG_REBOUND)

    def test_flag_seven_high_pullback_submits_one_sell(self):
        self._l2("093500", 10.75, high=10.80, low=10.70)
        self.assertEqual(self.inst["flag"], MODEL.FLAG_STRONG)

        self._l2("093501", 10.50, high=10.80, low=10.50)

        self.assertEqual(len(self.inst["orders"]), 1)
        self.assertEqual(self.inst["orders"][0]["action"], "flag7_high_pullback_25")
        self.assertEqual(self.inst["orders"][0]["quantity"], 200)
        self.assertEqual(self.inst["orders"][0]["price"], 10.39)
        self.assertEqual(self.inst["flag"], MODEL.FLAG_SELL_DONE)

    def test_live_sell_is_clamped_to_current_available_position(self):
        MODEL.ENABLE_LIVE_ORDERS = True
        MODEL.ACCOUNT_ID = "ACCOUNT"
        calls = []
        queries = []

        def get_positions(account, account_type, kind):
            queries.append((account, account_type, kind))
            return [{"stock_code": "000001.SZ", "can_use_volume": 100}]

        MODEL.get_trade_detail_data = get_positions
        MODEL.passorder = lambda *args: calls.append(args) or "order-1"

        order = MODEL._submit_sell(self.inst, 200, 10.0, "test", "test_action")

        self.assertIsNotNone(order)
        self.assertEqual(order["quantity"], 100)
        self.assertEqual(len(calls[0]), 11)
        self.assertEqual(calls[0][0:2], (MODEL.OP_TYPE_SELL, MODEL.ORDER_TYPE_LIMIT))
        self.assertEqual(calls[0][3:8], ("000001.SZ", MODEL.PRICE_TYPE_LIMIT, 10.0, 100, MODEL.STRATEGY_NAME))
        self.assertEqual(queries, [("ACCOUNT", "stock", "POSITION")])

    def test_native_detail_uses_qmt_limit_price_field_names(self):
        context = _Context(
            details={
                "000001.SZ": {
                    "PreClose": 10.0,
                    "UpStopPrice": 11.0,
                    "DownStopPrice": 9.0,
                }
            }
        )
        MODEL._initial_quote(context, self.inst)

        self.assertEqual(self.inst["pre_close"], 10.0)
        self.assertEqual(self.inst["limit_up"], 11.0)
        self.assertEqual(self.inst["limit_down"], 9.0)

    def test_native_xtorder_callback_fields_are_recognized(self):
        MODEL.ENABLE_LIVE_ORDERS = True
        MODEL.ACCOUNT_ID = "ACCOUNT"
        MODEL.get_trade_detail_data = lambda account, account_type, kind: [
            {"stock_code": "000001.SZ", "can_use_volume": 200}
        ]
        MODEL.passorder = lambda *args: "order-native"

        order = MODEL._submit_sell(self.inst, 200, 10.0, "test", "native_order")
        MODEL.order_callback(None, {
            "order_id": "order-native",
            "stock_code": "000001.SZ",
            "order_remark": order["token"],
            "order_volume": 200,
            "traded_volume": 100,
            "order_status": 52,
            "order_price": 10.0,
        })

        self.assertEqual(order["status"], MODEL.ORDER_CANCEL_REQUESTED)
        self.assertEqual(order["traded_quantity"], 100)

    def test_native_xttrade_callback_counts_trade_and_deduplicates(self):
        MODEL.ENABLE_LIVE_ORDERS = True
        MODEL.ACCOUNT_ID = "ACCOUNT"
        MODEL.get_trade_detail_data = lambda account, account_type, kind: [
            {"stock_code": "000001.SZ", "can_use_volume": 200}
        ]
        MODEL.passorder = lambda *args: "order-native"

        order = MODEL._submit_sell(self.inst, 200, 10.0, "test", "native_trade")
        trade = {
            "order_id": "order-native",
            "stock_code": "000001.SZ",
            "traded_volume": 100,
            "traded_id": "trade-native-1",
            "traded_price": 10.01,
        }
        MODEL.deal_callback(None, trade)
        MODEL.deal_callback(None, trade)

        self.assertEqual(order["deal_traded_quantity"], 100)
        self.assertEqual(order["traded_quantity"], 100)
        self.assertEqual(order["status"], MODEL.ORDER_PARTIAL)

    def test_native_trade_before_order_id_binding_is_replayed(self):
        MODEL.ENABLE_LIVE_ORDERS = True
        MODEL.ACCOUNT_ID = "ACCOUNT"
        MODEL.get_trade_detail_data = lambda account, account_type, kind: [
            {"stock_code": "000001.SZ", "can_use_volume": 200}
        ]
        MODEL.passorder = lambda *args: "0"

        order = MODEL._submit_sell(self.inst, 200, 10.0, "test", "pending_native_trade")
        MODEL.deal_callback(None, {
            "order_id": "order-native-late",
            "stock_code": "000001.SZ",
            "traded_volume": 100,
            "traded_id": "trade-native-late-1",
            "traded_price": 10.01,
        })

        self.assertEqual(order["deal_traded_quantity"], 0)
        MODEL._bind_order_id(self.inst, order, "order-native-late")

        self.assertEqual(order["deal_traded_quantity"], 100)
        self.assertEqual(order["traded_quantity"], 100)
        self.assertEqual(order["status"], MODEL.ORDER_PARTIAL)

    def test_live_second_sell_waits_for_cancel_callback(self):
        MODEL.ENABLE_LIVE_ORDERS = True
        MODEL.ACCOUNT_ID = "ACCOUNT"
        calls = []
        MODEL.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_strInstrumentID": "000001.SZ", "m_nCanUseVolume": 200}
        ]
        MODEL.passorder = lambda *args: calls.append(args) or str(len(calls))
        MODEL.cancel = lambda *args: True

        first = MODEL._submit_sell(self.inst, 200, 10.0, "first", "first_action")
        self.assertEqual(first["status"], MODEL.ORDER_ACTIVE)
        self.inst["up_sell"] = MODEL.LIMIT_UP_FIRST_SELL_DONE
        self.inst["pre_close"] = 10.0
        self.inst["pre_bid"] = 10.40
        self.inst["limit_open_second_sell_done"] = False
        quote = self._quote("093701", 10.25, high=11.0, low=10.2)
        MODEL._handle_limit_open_second_sell(self.inst, MODEL._quote_values(quote), True)

        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.inst["pending_after_cancel"])
        MODEL.order_callback(None, {
            "m_strOrderID": "1",
            "m_strInstrumentID": "000001.SZ",
            "m_nOrderStatus": "52",
            "m_nVolumeTotalOriginal": 200,
            "m_nVolumeTraded": 100,
            "m_dPrice": 10.0,
        })

        self.assertEqual(len(calls), 1)
        self.assertEqual(first["status"], MODEL.ORDER_CANCEL_REQUESTED)
        self.assertIsNotNone(self.inst["pending_after_cancel"])

        MODEL.order_callback(None, {
            "m_strOrderID": "1",
            "m_strInstrumentID": "000001.SZ",
            "m_nOrderStatus": "53",
            "m_nVolumeTotalOriginal": 200,
            "m_nVolumeTraded": 100,
            "m_dPrice": 10.0,
        })

        self.assertEqual(len(calls), 2)
        self.assertEqual(self.inst["orders"][-1]["action"], "limit_open_second_sell")

    def test_deal_callback_accumulates_partial_fills(self):
        MODEL.ENABLE_LIVE_ORDERS = True
        MODEL.ACCOUNT_ID = "ACCOUNT"
        MODEL.get_trade_detail_data = lambda account, account_type, kind: [
            {"m_strInstrumentID": "000001.SZ", "m_nCanUseVolume": 200}
        ]
        MODEL.passorder = lambda *args: "order-1"

        order = MODEL._submit_sell(self.inst, 200, 10.0, "test", "fill_action")
        MODEL.deal_callback(None, {
            "m_strOrderID": "order-1",
            "m_strInstrumentID": "000001.SZ",
            "m_nVolume": 100,
            "m_strTradeID": "deal-1",
        })
        MODEL.deal_callback(None, {
            "m_strOrderID": "order-1",
            "m_strInstrumentID": "000001.SZ",
            "m_nVolume": 100,
            "m_strTradeID": "deal-1",
        })
        MODEL.deal_callback(None, {
            "m_strOrderID": "order-1",
            "m_strInstrumentID": "000001.SZ",
            "m_nVolume": 100,
            "m_strTradeID": "deal-2",
        })

        self.assertEqual(order["traded_quantity"], 200)
        self.assertEqual(order["status"], MODEL.ORDER_FILLED)


if __name__ == "__main__":
    unittest.main()
