from datetime import datetime
from pathlib import Path

from config.enums import OrderDirection, OrderStatus
from core.models import TickData
from position.models import PositionInfo
from strategy.juejin_sell_strategy import JuejinSellStrategy
from strategy.models import StrategyConfig
from trading.models import Order


class _FakeTradeExecutor:
    def __init__(self):
        self.orders = []
        self.canceled = []

    def sell_limit(self, strategy_id, strategy_name, stock_code, price, quantity, remark=""):
        order = Order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.SELL,
            price=price,
            quantity=quantity,
            remark=remark,
            status=OrderStatus.WAIT_REPORTING,
        )
        self.orders.append(order)
        return order

    def cancel_order(self, order_uuid, remark=""):
        self.canceled.append((order_uuid, remark))
        for order in self.orders:
            if order.order_uuid == order_uuid:
                order.status = OrderStatus.REPORTED_CANCEL
                order.status_msg = remark
                break
        return True


class _FakePositionManager:
    def __init__(self):
        self.positions = {}
        self.prices = []

    def get_position(self, strategy_id):
        return self.positions.get(strategy_id)

    def get_all_positions(self):
        return dict(self.positions)

    def update_price(self, stock_code, price):
        self.prices.append((stock_code, price))


def _strategy(**params):
    executor = _FakeTradeExecutor()
    position_mgr = _FakePositionManager()
    strategy = JuejinSellStrategy(
        StrategyConfig(
            stock_code=params.pop("stock_code", "000001"),
            params={
                "exp": params.pop("exp", 1),
                "sellvol": params.pop("sellvol", 200),
                "nick": params.pop("nick", "测试股"),
                **params,
            },
        ),
        executor,
        position_mgr,
    )
    position_mgr.positions[strategy.strategy_id] = PositionInfo(
        strategy_id=strategy.strategy_id,
        strategy_name=strategy.strategy_name,
        stock_code=strategy.stock_code,
        total_quantity=500,
        sellable_base_quantity=500,
        available_quantity=500,
        avg_cost=9.0,
        total_cost=4500.0,
        current_price=10.0,
    )
    strategy.start()
    return strategy, executor, position_mgr


def _tick(
    at: str,
    *,
    stock_code="000001",
    pre_close=10.0,
    bid=10.0,
    bid_volume=1000,
    ask=10.01,
    ask_volume=1000,
    open_price=10.0,
    high=10.0,
    low=10.0,
    last_price=None,
):
    return TickData(
        stock_code=stock_code,
        last_price=float(last_price if last_price is not None else bid),
        open=float(open_price),
        high=float(high),
        low=float(low),
        pre_close=float(pre_close),
        bid_prices=[float(bid)],
        bid_volumes=[int(bid_volume)],
        ask_prices=[float(ask)],
        ask_volumes=[int(ask_volume)],
        data_time=datetime.fromisoformat(f"2026-06-06 {at}"),
        recv_time=datetime.fromisoformat(f"2026-06-06 {at}"),
    )


def test_juejin_sell_select_stocks_from_csv_normalizes_symbols(tmp_path: Path):
    csv_path = tmp_path / "sell_10.csv"
    csv_path.write_text(
        "symbol,exp,sellvol,nick\n"
        "SZSE.000977,1,200,浪潮信息\n"
        "600519.SH,0,300,贵州茅台\n"
        "bad,1,100,坏行\n",
        encoding="utf-8",
    )

    strategy = JuejinSellStrategy(StrategyConfig(params={"csv_path": str(csv_path)}))

    configs = strategy.select_stocks()

    assert [cfg.stock_code for cfg in configs] == ["000977", "600519"]
    assert configs[0].params["exp"] == 1
    assert configs[0].params["sellvol"] == 200
    assert configs[0].params["instance_key"] == "juejin_sell:000977"
    assert configs[1].params["source_symbol"] == "600519.SH"


def test_juejin_sell_auction_under_expectation_places_two_sell_orders():
    strategy, executor, _ = _strategy(exp=1, sellvol=200)

    strategy.process_tick(
        _tick(
            "09:24:56",
            bid=9.70,
            ask=9.71,
            pre_close=10.0,
            open_price=9.7,
            high=9.8,
            low=9.6,
        )
    )

    assert [(order.quantity, order.price) for order in executor.orders] == [(200, 9.0), (300, 10.5)]
    assert all(order.direction == OrderDirection.SELL for order in executor.orders)
    assert executor.orders[0].remark == "竞价严重不及预期: 第一笔跌停价卖出"
    assert strategy._flag == 1


def test_juejin_sell_limit_down_clear_cancels_existing_orders_before_sell():
    strategy, executor, _ = _strategy(exp=0, sellvol=200)
    existing = strategy._submit_sell(100, 10.2, "existing active order", action_key="existing")

    strategy.process_tick(
        _tick(
            "09:30:00",
            bid=9.0,
            ask=9.0,
            ask_volume=6_000_000,
            pre_close=10.0,
            open_price=9.5,
            high=9.7,
            low=9.0,
        )
    )

    assert existing is not None
    assert executor.canceled == [(existing.order_uuid, "跌停止损清仓前撤单")]
    assert executor.orders[-1].quantity == 500
    assert executor.orders[-1].price == 9.0
    assert executor.orders[-1].remark == "非跌停开后跌停止损清仓"
    assert strategy._flag == 99


def test_juejin_sell_limit_up_open_first_sell_and_five_min_sell():
    strategy, executor, _ = _strategy(exp=0, sellvol=200)

    strategy.process_tick(
        _tick(
            "09:35:00",
            bid=11.0,
            bid_volume=11_000_000,
            ask=11.0,
            pre_close=10.0,
            open_price=10.5,
            high=11.0,
            low=10.5,
        )
    )
    assert strategy._flag == 10
    assert strategy._open_flag == 0

    strategy.process_tick(
        _tick(
            "09:37:00",
            bid=10.95,
            bid_volume=2_000_000,
            ask=10.96,
            pre_close=10.0,
            open_price=10.5,
            high=11.0,
            low=10.5,
        )
    )

    assert executor.orders[-1].quantity == 200
    assert executor.orders[-1].price == 10.75
    assert executor.orders[-1].remark == "涨停开板先卖一笔"
    assert strategy._up_sell == 99
    assert strategy._open_flag == 10
    assert strategy._flag == -9

    strategy.process_tick(
        _tick(
            "09:43:01",
            bid=10.80,
            bid_volume=1_000_000,
            ask=10.81,
            pre_close=10.0,
            open_price=10.5,
            high=11.0,
            low=10.5,
        )
    )

    assert executor.orders[-1].quantity == 200
    assert executor.orders[-1].price == 10.8
    assert executor.orders[-1].remark == "涨停开板 5 分钟不回封卖出"
    assert strategy._open_flag == 0
    assert strategy._flag == 7
