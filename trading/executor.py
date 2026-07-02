"""交易执行模块。

本模块的职责是把策略层发出的交易意图，转换为底层交易接口可执行的
下单或撤单请求。它不维护最终成交结果，真正的订单状态仍以后续回调为准。
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import time
from typing import Optional

from trading.models import Order
from trading.order_manager import OrderManager
from config.enums import OrderDirection, OrderType, OrderStatus
from monitor.logger import get_logger

logger = get_logger("trade")

try:
    from xtquant import xtconstant
    _XT_AVAILABLE = True
except ImportError:
    _XT_AVAILABLE = False

    class xtconstant:  # type: ignore
        STOCK_BUY = 23
        STOCK_SELL = 24
        LATEST_PRICE = 5
        FIX_PRICE = 11       # 限价
        MARKET_SH_CONVERT_5_CANCEL = 43
        MARKET_SZ_CONVERT_5_CANCEL = 46
        MARKET_SH_INSTANT = 42   # 上海市价（最优五档即时成交）
        MARKET_SZ_CONVERT = 45   # 深圳市价（即时成交剩余转限价）


class TradeExecutor:
    """交易执行器。

    该模块负责把策略发出的“买/卖/平仓/撤单意图”翻译成实际交易指令。

    可以把它理解成“策略层和柜台接口之间的翻译层”：
    1. 上游传入的是统一的内部意图。
    2. 这里负责补齐数量、方向、市场代码、价格类型。
    3. 最后交给 xtquant 或 mock 通道发出请求。
    """

    # A 股最小交易单位
    _LOT_SIZE = 100

    def __init__(self, connection_mgr, order_mgr: OrderManager,
                 position_mgr=None, live_trading_enabled: bool = False):
        """初始化交易执行器。

        Args:
            connection_mgr: 交易连接管理器。
            order_mgr: 订单管理器。
            position_mgr: 可选的持仓管理器，用于平仓前查询可用仓位。
            live_trading_enabled: 是否允许真实下单/撤单。False 时保留 mock 演练行为。
        """
        # ``_conn_mgr`` 提供 trader/account 等底层交易通道对象。
        self._conn_mgr = connection_mgr
        # ``_order_mgr`` 负责注册订单并维护订单生命周期。
        self._order_mgr = order_mgr
        # ``_position_mgr`` 主要用于平仓前查询可卖数量。
        self._position_mgr = position_mgr
        # 必须显式打开 live，执行器才允许把请求发到底层柜台。
        self._live_trading_enabled = bool(live_trading_enabled)

    @property
    def connection_manager(self):
        """返回当前执行器关联的连接管理器。"""
        return self._conn_mgr

    @property
    def live_trading_enabled(self) -> bool:
        """返回执行器是否允许真实下单/撤单。"""
        return self._live_trading_enabled

    def get_live_guard_status(self) -> dict:
        """返回执行器真实交易保护状态，供启动自检和日志展示。"""
        has_conn = self._conn_mgr is not None
        trader = self._conn_mgr.get_trader() if has_conn and hasattr(self._conn_mgr, "get_trader") else None
        account = getattr(self._conn_mgr, "account", None) if has_conn else None
        last_error = {}
        if has_conn and hasattr(self._conn_mgr, "get_last_error"):
            try:
                last_error = self._conn_mgr.get_last_error()
            except Exception as exc:
                last_error = {"error": str(exc)}
        trading_ready = False
        if has_conn and hasattr(self._conn_mgr, "is_trading_ready"):
            try:
                trading_ready = bool(self._conn_mgr.is_trading_ready())
            except Exception:
                trading_ready = False
        return {
            "live_trading_enabled": self._live_trading_enabled,
            "xtquant_available": _XT_AVAILABLE,
            "has_connection_manager": has_conn,
            "has_trader": trader is not None,
            "has_account": account is not None,
            "trading_ready": trading_ready,
            "last_error": last_error,
        }

    def get_live_account_snapshot(self) -> dict:
        """查询 live 账户资金快照，供启动自检展示。"""
        asset = self._query_live_asset()
        if asset is None:
            return {"asset_available": False, "available_cash": None, "total_asset": None}
        total_asset = None
        for name in ("total_asset", "total_balance", "asset_balance"):
            value = getattr(asset, name, None)
            if value is None and isinstance(asset, dict):
                value = asset.get(name)
            if value is None or value == "":
                continue
            try:
                total_asset = float(value)
                break
            except (TypeError, ValueError):
                continue
        return {
            "asset_available": True,
            "available_cash": self._extract_available_cash(asset),
            "total_asset": total_asset,
        }

    # ------------------------------------------------------------------ 买入

    def buy_limit(self, strategy_id: str, strategy_name: str,
                  stock_code: str, price: float,
                  quantity: int, remark: str = "") -> Order:
        """提交限价买入订单。"""
        # 这里先构造内部 Order 对象，再统一交给 _submit_order 发出。
        order = Order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            price=price,
            quantity=quantity,
            remark=remark or f"限价买入 {stock_code}",
        )
        return self._submit_order(order)

    def buy_latest(self, strategy_id: str, strategy_name: str,
                   stock_code: str, quantity: int, remark: str = "") -> Order:
        """提交最新价买入订单。"""
        return self._submit_order(self._build_market_order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.BUY,
            quantity=quantity,
            requested_price_type=self._resolve_latest_price_type(),
            remark=remark or f"最新价买入 {stock_code}",
        ))

    def buy_best5_or_cancel(self, strategy_id: str, strategy_name: str,
                            stock_code: str, quantity: int, remark: str = "") -> Order:
        """提交最优五档即时成交剩余撤销买单。"""
        return self._submit_order(self._build_market_order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.BUY,
            quantity=quantity,
            requested_price_type=self._resolve_best5_or_cancel_price_type(stock_code),
            remark=remark or f"最优五档买入 {stock_code}",
        ))

    def buy_market(self, strategy_id: str, strategy_name: str,
                   stock_code: str, quantity: int, remark: str = "") -> Order:
        """提交市价买入订单。

        当前默认实现仍映射到 ``buy_latest``，保持历史行为不变。
        如果后续某个策略要改成 best5，下游策略可直接显式调用
        ``buy_best5_or_cancel``，而不是依赖这里做全局切换。
        """
        return self.buy_latest(
            strategy_id,
            strategy_name,
            stock_code,
            quantity,
            remark=remark or f"市价买入 {stock_code}",
        )

    def buy_by_amount(self, strategy_id: str, strategy_name: str,
                      stock_code: str, price: float,
                      amount: float, remark: str = "") -> Order:
        """按金额换算股数后提交买入订单。"""
        if price <= 0:
            logger.error("buy_by_amount: price 必须 > 0")
            return self._failed_order(strategy_id, strategy_name, stock_code,
                                      OrderDirection.BUY, "price=0")
        quantity = self._calc_quantity(amount, price)
        if quantity <= 0:
            logger.warning("buy_by_amount: 金额 %.0f 不足买1手 (price=%.3f)", amount, price)
            return self._failed_order(strategy_id, strategy_name, stock_code,
                                      OrderDirection.BUY, "金额不足")
        # amount 仅表示上游意图；真正发单时仍落成“按股数委托”。
        order = Order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.BUY,
            order_type=OrderType.BY_AMOUNT,
            price=price,
            quantity=quantity,
            amount=amount,
            remark=remark or f"按金额买入 {stock_code} ¥{amount:.0f}",
        )
        return self._submit_order(order)

    # ------------------------------------------------------------------ 卖出

    def sell_limit(self, strategy_id: str, strategy_name: str,
                   stock_code: str, price: float,
                   quantity: int, remark: str = "") -> Order:
        """提交限价卖出订单。"""
        # 卖出与买入一样，先走内部统一订单模型。
        order = Order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.SELL,
            order_type=OrderType.LIMIT,
            price=price,
            quantity=quantity,
            remark=remark or f"限价卖出 {stock_code}",
        )
        return self._submit_order(order)

    def sell_latest(self, strategy_id: str, strategy_name: str,
                    stock_code: str, quantity: int, remark: str = "") -> Order:
        """提交最新价卖出订单。"""
        return self._submit_order(self._build_market_order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.SELL,
            quantity=quantity,
            requested_price_type=self._resolve_latest_price_type(),
            remark=remark or f"最新价卖出 {stock_code}",
        ))

    def sell_best5_or_cancel(self, strategy_id: str, strategy_name: str,
                             stock_code: str, quantity: int, remark: str = "") -> Order:
        """提交最优五档即时成交剩余撤销卖单。"""
        return self._submit_order(self._build_market_order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.SELL,
            quantity=quantity,
            requested_price_type=self._resolve_best5_or_cancel_price_type(stock_code),
            remark=remark or f"最优五档卖出 {stock_code}",
        ))

    def sell_market(self, strategy_id: str, strategy_name: str,
                    stock_code: str, quantity: int, remark: str = "") -> Order:
        """提交市价卖出订单。

        当前默认实现仍映射到 ``sell_latest``。后续若某个策略要切到别的
        卖出方式，直接在策略层显式改调用目标即可。
        """
        return self.sell_latest(
            strategy_id,
            strategy_name,
            stock_code,
            quantity,
            remark=remark or f"市价卖出 {stock_code}",
        )

    def close_position(self, strategy_id: str, strategy_name: str,
                       stock_code: str, remark: str = "") -> Order:
        """卖出该策略当前全部可用持仓。

        当前平仓默认复用 ``sell_market -> sell_latest``，保持模拟盘口径一致。
        后续如果某个策略想切到其他卖出方式，直接修改策略调用即可。
        """
        available = 0
        if self._position_mgr:
            pos = self._position_mgr.get_position(strategy_id)
            if pos:
                # close_position 只卖“当前可用仓位”，不碰不可卖冻结部分。
                available = pos.available_quantity
        if available <= 0:
            logger.warning("close_position: 无可用持仓 strategy=%s code=%s",
                           strategy_id[:8], stock_code)
            return self._failed_order(strategy_id, strategy_name, stock_code,
                                      OrderDirection.SELL, "无可用持仓")
        # 模拟盘阶段统一按最新价卖出，保证建仓、止损、止盈/平仓三条链路口径一致。
        return self.sell_market(strategy_id, strategy_name, stock_code, available,
                                remark=remark or f"平仓 {stock_code}")

    # ------------------------------------------------------------------ 撤单

    def cancel_order(self, order_uuid: str, remark: str = "") -> bool:
        """提交撤单请求。

        Returns:
            是否成功把撤单请求发到底层接口。
        """
        order = self._order_mgr.get_order(order_uuid)
        if not order:
            logger.warning("cancel_order: 未找到订单 uuid=%s", order_uuid[:8])
            return False
        if not order.is_active():
            logger.warning("cancel_order: 订单 %s 已终结 status=%s",
                           order_uuid[:8], order.status.value)
            return False

        if not self._live_trading_enabled:
            logger.warning("cancel_order [MOCK]: uuid=%s xt_id=%d live_enabled=false",
                           order_uuid[:8], order.xt_order_id)
            self._order_mgr.mark_order_status(
                order_uuid,
                OrderStatus.CANCELED,
                status_msg=remark or "mock cancel",
            )
            return True

        readiness_error = self._live_readiness_error()
        if readiness_error:
            logger.error(
                "cancel_order blocked: live trading not ready uuid=%s reason=%s",
                order_uuid[:8],
                readiness_error,
            )
            return False

        trader = self._conn_mgr.get_trader() if self._conn_mgr else None
        if not trader or not _XT_AVAILABLE:
            logger.warning("cancel_order [MOCK]: uuid=%s xt_id=%d",
                           order_uuid[:8], order.xt_order_id)
            # mock 模式下没有真实撤单回报，这里直接把内部状态切到已撤，便于上层流程继续。
            self._order_mgr.mark_order_status(
                order_uuid,
                OrderStatus.CANCELED,
                status_msg=remark or "mock cancel",
            )
            return True

        try:
            account = self._conn_mgr.account
            trader.cancel_order_stock(account, order.xt_order_id)
            logger.info("[ORDER] 撤单提交 uuid=%s xt_id=%d remark=%s",
                        order_uuid[:8], order.xt_order_id, remark)
            return True
        except Exception as e:
            logger.error("cancel_order 失败: %s", e, exc_info=True)
            return False

    # ------------------------------------------------------------------ Internal

    def _submit_order(self, order: Order) -> Order:
        """把内部订单提交到底层交易接口，并注册到订单管理器。

        注意：
        - 这里的“成功”只表示请求已发出。
        - 最终是否成交、是否废单，要以后续回调结果为准。
        """
        if order.order_type in (OrderType.LIMIT, OrderType.BY_AMOUNT, OrderType.BY_QUANTITY) and order.price > 0:
            order.price = self._normalize_limit_price(order.stock_code, order.direction, order.price)

        order.price_type = self._resolve_order_price_type(order)

        order_error = self._validate_order_for_submission(order)
        if order_error:
            return self._reject_order(order, order_error)

        if not self._live_trading_enabled:
            return self._submit_mock_order(order)

        readiness_error = self._live_readiness_error()
        if readiness_error:
            return self._reject_order(order, f"live_trading_not_ready:{readiness_error}")

        buying_power_error = self._validate_live_buying_power(order)
        if buying_power_error:
            return self._reject_order(order, buying_power_error)

        trader = self._conn_mgr.get_trader() if self._conn_mgr else None

        if not trader or not _XT_AVAILABLE:
            # Mock 模式下没有真实柜台，因此直接生成一个伪订单号，
            # 让后续策略链路依旧可以完整演练。
            order.xt_order_id = int(time.time() * 1000) % 2**31
            order.status = OrderStatus.WAIT_REPORTING
            self._order_mgr.register_order(order)
            logger.info("[ORDER] [MOCK] 下单 uuid=%s code=%s dir=%s price=%.3f qty=%d",
                        order.order_uuid[:8], order.stock_code,
                        order.direction.value, order.price, order.quantity)
            return order

        try:
            account = self._conn_mgr.account
            xt_code = self._code_to_xt(order.stock_code)
            # 内部买卖方向要转换成 xtquant 常量。
            xt_direction = (xtconstant.STOCK_BUY
                            if order.direction == OrderDirection.BUY
                            else xtconstant.STOCK_SELL)
            # 不同市场的市价单常量不同，这里按证券代码前缀自动选择。
            price_type = (xtconstant.FIX_PRICE
                          if order.order_type in (OrderType.LIMIT, OrderType.BY_AMOUNT,
                                                  OrderType.BY_QUANTITY)
                          else order.price_type)
            order.price_type = price_type
            self._log_live_order_preflight(order, account)

            # order_stock_async 返回的是本地下单序列号 seq，真正的柜台订单号要等异步回报再绑定。
            seq = trader.order_stock_async(
                account,
                xt_code,
                xt_direction,
                order.quantity,
                price_type,
                order.price,
                order.strategy_name,
                order.order_trace_id,
            )
            order.status = OrderStatus.WAIT_REPORTING
            self._order_mgr.register_order(order)
            self._order_mgr.register_seq(seq, order.order_uuid)
            logger.info(
                "[ORDER] 下单提交 uuid=%s trace=%s seq=%d code=%s dir=%s price=%.3f qty=%d remark=%s",
                order.order_uuid[:8],
                order.order_trace_id,
                seq,
                order.stock_code,
                order.direction.value,
                order.price,
                order.quantity,
                order.remark,
            )
        except Exception as e:
            order.status = OrderStatus.JUNK
            order.status_msg = str(e or "")
            self._order_mgr.register_order(order)
            logger.error("TradeExecutor: 下单失败 uuid=%s: %s",
                         order.order_uuid[:8], e, exc_info=True)
        return order

    def _submit_mock_order(self, order: Order) -> Order:
        """Register an observe/mock order without touching the live counter."""
        order.xt_order_id = int(time.time() * 1000) % 2**31
        order.status = OrderStatus.WAIT_REPORTING
        self._order_mgr.register_order(order)
        logger.info("[ORDER] [MOCK] 下单 uuid=%s code=%s dir=%s price=%.3f qty=%d live_enabled=false",
                    order.order_uuid[:8], order.stock_code,
                    order.direction.value, order.price, order.quantity)
        return order

    def _live_readiness_error(self) -> str:
        """返回 live 模式下不可交易的原因；非 live 模式返回空字符串。"""
        if not self._live_trading_enabled:
            return ""
        if not _XT_AVAILABLE:
            return "xtquant_unavailable"
        if not self._conn_mgr:
            return "connection_manager_missing"

        trader = self._conn_mgr.get_trader() if hasattr(self._conn_mgr, "get_trader") else None
        if not trader:
            return "trader_missing"

        account = getattr(self._conn_mgr, "account", None)
        if not account:
            return "account_missing"

        if hasattr(self._conn_mgr, "is_trading_ready"):
            try:
                if self._conn_mgr.is_trading_ready():
                    return ""
                return self._format_connection_not_ready_reason()
            except Exception as exc:
                return f"trading_ready_check_exception:{exc}"

        if hasattr(self._conn_mgr, "is_connected"):
            try:
                if not self._conn_mgr.is_connected():
                    return self._format_connection_not_ready_reason()
            except Exception as exc:
                return f"connection_check_exception:{exc}"
        return ""

    def _format_connection_not_ready_reason(self) -> str:
        """生成连接未就绪的结构化原因，便于日志直接定位账户订阅问题。"""
        last_error = {}
        if self._conn_mgr and hasattr(self._conn_mgr, "get_last_error"):
            try:
                last_error = self._conn_mgr.get_last_error()
            except Exception:
                last_error = {}
        if last_error:
            return (
                "connection_not_ready"
                f":stage={last_error.get('stage', '')}"
                f":return_code={last_error.get('return_code', '')}"
                f":account_id={last_error.get('account_id', '')}"
                f":error={last_error.get('error', '')}"
            )
        return "connection_not_ready"

    def _validate_order_for_submission(self, order: Order) -> str:
        """提交前做执行层通用校验，避免非法订单进入真实柜台。"""
        if not self._is_valid_stock_code(order.stock_code):
            return "invalid_stock_code"
        if order.quantity <= 0:
            return "invalid_quantity"
        if order.direction == OrderDirection.BUY and order.quantity % self._LOT_SIZE != 0:
            return "buy_quantity_not_lot_multiple"
        if order.order_type in (OrderType.LIMIT, OrderType.BY_AMOUNT, OrderType.BY_QUANTITY) and order.price <= 0:
            return "invalid_limit_price"
        return ""

    def _validate_live_buying_power(self, order: Order) -> str:
        """真实买入前查询账户可用资金，资金不足或无法查询都禁止进柜台。"""
        if not self._live_trading_enabled or order.direction != OrderDirection.BUY:
            return ""
        required_amount = self._estimate_required_amount(order)
        if required_amount <= 0:
            return "buying_power_price_missing"
        asset = self._query_live_asset()
        if asset is None:
            return f"buying_power_asset_unavailable:required_amount={required_amount:.2f}"
        available_cash = self._extract_available_cash(asset)
        if available_cash is None:
            return f"buying_power_cash_unavailable:required_amount={required_amount:.2f}"
        if available_cash + 1e-6 < required_amount:
            return (
                "insufficient_cash"
                f":available_cash={available_cash:.2f}"
                f":required_amount={required_amount:.2f}"
            )
        order.xt_fields["preflight_available_cash"] = available_cash
        order.xt_fields["preflight_required_amount"] = required_amount
        return ""

    def _query_live_asset(self):
        if not self._conn_mgr or not hasattr(self._conn_mgr, "query_stock_asset"):
            return None
        try:
            return self._conn_mgr.query_stock_asset()
        except Exception as exc:
            logger.error("TradeExecutor: query asset before live order failed: %s", exc, exc_info=True)
            return None

    @staticmethod
    def _extract_available_cash(asset) -> Optional[float]:
        """兼容不同 xtquant 字段名，优先取可用资金字段。"""
        for name in (
            "cash",
            "available_cash",
            "enable_balance",
            "enable_bail_balance",
            "available_balance",
            "fetch_balance",
        ):
            value = getattr(asset, name, None)
            if value is None and isinstance(asset, dict):
                value = asset.get(name)
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _estimate_required_amount(order: Order) -> float:
        price = float(order.price or 0.0)
        quantity = int(order.quantity or 0)
        if price <= 0 or quantity <= 0:
            return 0.0
        return price * quantity

    @staticmethod
    def _is_valid_stock_code(stock_code: str) -> bool:
        code = str(stock_code or "").strip().upper()
        if "." in code:
            code = code.split(".", 1)[0]
        return len(code) == 6 and code.isdigit()

    def _log_live_order_preflight(self, order: Order, account) -> None:
        if not self._live_trading_enabled:
            return
        logger.warning(
            (
                "[ORDER] LIVE preflight passed uuid=%s trace=%s account=%s code=%s "
                "dir=%s price=%.3f qty=%d required_amount=%.2f available_cash=%.2f "
                "remark=%s"
            ),
            order.order_uuid[:8],
            order.order_trace_id,
            getattr(account, "account_id", ""),
            order.stock_code,
            order.direction.value,
            order.price,
            order.quantity,
            float(order.xt_fields.get("preflight_required_amount", 0.0) or 0.0),
            float(order.xt_fields.get("preflight_available_cash", 0.0) or 0.0),
            order.remark,
        )

    def _reject_order(self, order: Order, reason: str) -> Order:
        """把执行层拦截的订单登记为 JUNK，保留审计记录但不形成活动委托。"""
        order.status = OrderStatus.JUNK
        order.status_msg = reason
        if reason not in order.remark:
            order.remark = f"{order.remark} [{reason}]" if order.remark else f"[{reason}]"
        self._order_mgr.register_order(order)
        logger.error(
            "[ORDER] 下单拦截 uuid=%s code=%s dir=%s price=%.3f qty=%d reason=%s live_enabled=%s",
            order.order_uuid[:8],
            order.stock_code,
            order.direction.value,
            order.price,
            order.quantity,
            reason,
            self._live_trading_enabled,
        )
        return order

    @staticmethod
    def _build_market_order(strategy_id: str, strategy_name: str,
                            stock_code: str, direction: OrderDirection,
                            quantity: int, requested_price_type: int,
                            remark: str) -> Order:
        """构造统一的吃单类委托对象。"""
        return Order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=direction,
            order_type=OrderType.MARKET,
            price=0.0,
            quantity=quantity,
            price_type=requested_price_type,
            remark=remark,
        )

    @staticmethod
    def _calc_quantity(amount: float, price: float) -> int:
        """按金额计算可买数量，并向下取整到一手（100 股）。"""
        # floor 后再乘 lot size，确保既不超预算，也符合 A 股整手规则。
        lots = math.floor(amount / price / TradeExecutor._LOT_SIZE)
        return lots * TradeExecutor._LOT_SIZE

    @staticmethod
    def _resolve_market_price_type(stock_code: str) -> int:
        """兼容不同 xtquant 版本的市价单常量命名。"""
        if str(stock_code).startswith("6"):
            # 沪市和深市的市价枚举名称不完全一致，所以这里按候选常量列表逐个尝试。
            candidates = (
                "MARKET_SH_INSTANT",
                "MARKET_SH_CONVERT_5_LIMIT",
                "MARKET_CONVERT_5",
                "MARKET_PEER_PRICE_FIRST",
            )
        else:
            candidates = (
                "MARKET_SZ_CONVERT",
                "MARKET_SZ_INSTBUSI_RESTCANCEL",
                "MARKET_CONVERT_5",
                "MARKET_PEER_PRICE_FIRST",
            )

        for name in candidates:
            value = getattr(xtconstant, name, None)
            if value is not None:
                return value

        # 找不到任何市价常量时，退回限价，至少保证接口还能正常调用。
        return xtconstant.FIX_PRICE

    @staticmethod
    def _resolve_latest_price_type() -> int:
        """返回最新价下单的 xtquant 报价类型。"""
        return getattr(xtconstant, "LATEST_PRICE", xtconstant.FIX_PRICE)

    @classmethod
    def _resolve_best5_or_cancel_price_type(cls, stock_code: str) -> int:
        """返回最优五档即时成交剩余撤销的报价类型。"""
        xt_code = cls._code_to_xt(stock_code)
        if xt_code.endswith(".SH"):
            candidates = (
                "MARKET_SH_CONVERT_5_CANCEL",
                "MARKET_SH_INSTANT",
                "MARKET_SH_CONVERT_5_LIMIT",
                "MARKET_CONVERT_5",
            )
        else:
            candidates = (
                "MARKET_SZ_CONVERT_5_CANCEL",
                "MARKET_SZ_INSTBUSI_RESTCANCEL",
                "MARKET_SZ_CONVERT",
                "MARKET_CONVERT_5",
            )

        for name in candidates:
            value = getattr(xtconstant, name, None)
            if value is not None:
                return value
        return xtconstant.FIX_PRICE

    @classmethod
    def _resolve_order_price_type(cls, order: Order) -> int:
        """统一解析订单应使用的报价类型。"""
        if order.order_type in (OrderType.LIMIT, OrderType.BY_AMOUNT, OrderType.BY_QUANTITY):
            return xtconstant.FIX_PRICE
        if order.price_type:
            return int(order.price_type)
        return cls._resolve_latest_price_type()

    @staticmethod
    def _price_tick(stock_code: str) -> Decimal:
        """返回证券对应的最小报价单位。

        规则说明：
        - 普通 A 股（0/3/6 开头）按 0.01 元报价。
        - ETF/LOF/场内基金（常见 1/5 开头）按 0.001 元报价。
        """
        code = str(stock_code or "").strip().zfill(6)
        if code.startswith(("1", "5")):
            return Decimal("0.001")
        return Decimal("0.01")

    @classmethod
    def _normalize_limit_price(cls, stock_code: str, direction: OrderDirection, price: float) -> float:
        """把限价单价格规整到合法的最小价差。

        - 买单向上取整，提高成交概率。
        - 卖单向下取整，提高成交概率。
        """
        if price <= 0:
            return 0.0

        tick = cls._price_tick(stock_code)
        decimal_price = Decimal(str(price))
        units = decimal_price / tick
        rounding = ROUND_CEILING if direction == OrderDirection.BUY else ROUND_FLOOR
        normalized = units.to_integral_value(rounding=rounding) * tick
        return float(normalized)

    @staticmethod
    def _code_to_xt(code: str) -> str:
        """把内部证券代码转换为 xtquant 需要的市场代码格式。"""
        code = str(code).strip().zfill(6)
        if code.startswith(("6", "5")):
            return f"{code}.SH"
        return f"{code}.SZ"

    @staticmethod
    def _failed_order(strategy_id: str, strategy_name: str, stock_code: str,
                      direction: OrderDirection, reason: str) -> Order:
        """构造一张失败订单对象，供上层统一处理。"""
        # 失败也统一返回 Order，对上层来说就不需要区分“异常”和“失败订单对象”两套处理路径。
        order = Order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=direction,
            status=OrderStatus.JUNK,
            remark=f"[JUNK] {reason}",
        )
        return order


__all__ = ["TradeExecutor"]
