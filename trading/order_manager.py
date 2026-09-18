"""订单管理模块。

这个模块负责维护订单从“创建”到“完成/撤销/废单”的完整生命周期。
它同时也是成交分发中心，负责把成交回报同步给持仓模块、策略模块、
以及可选的 WebSocket 推送模块。
"""
import threading
from datetime import datetime
from typing import Callable, Dict, List, Optional

from config.fee_schedule import FeeBreakdown
from trading.models import Order, TradeRecord
from config.enums import OrderStatus, OrderDirection
from monitor.logger import get_logger

logger = get_logger("trade")


class OrderManager:
    """订单追踪管理器。

    它是整个交易链路的枢纽：
    - 新订单先在这里注册
    - 状态更新回到这里
    - 成交回报也在这里落地并向外分发

    如果按时间顺序理解：
    1. TradeExecutor 发单后先调用 ``register_order``。
    2. 柜台异步返回真实订单号时调用 ``on_async_response``。
    3. 柜台订单状态变化时调用 ``update_order_status``。
    4. 柜台成交回报到来时调用 ``on_trade``。
    """

    def __init__(self, data_manager=None, fee_schedule=None):
        """初始化订单管理器。

        Args:
            data_manager: 可选的数据管理器，用于持久化订单与成交。
            fee_schedule: 可选的费率表，用于按累计成交额重算费用。
        """
        # ``_data_mgr`` 负责把订单/成交同步到 SQLite。
        self._data_mgr = data_manager
        # ``_fee_schedule`` 负责计算手续费和印花税拆分。
        self._fee_schedule = fee_schedule
        # ``_orders`` 保存全部内部订单对象，键为内部 UUID。
        self._orders: Dict[str, Order] = {}                  # {order_uuid: Order}
        # ``_trace_to_uuid`` 用于把 xt `order_remark` 里的 23 位跟踪标识反查回内部订单 UUID。
        self._trace_to_uuid: Dict[str, str] = {}            # {order_trace_id: order_uuid}
        # ``_xt_to_uuid`` 用于把柜台订单号反查回内部订单 UUID。
        self._xt_to_uuid: Dict[int, str] = {}               # {xt_order_id: order_uuid}
        # ``_seq_to_uuid`` 用于异步下单时，先用 seq 暂存本地订单映射。
        self._seq_to_uuid: Dict[int, str] = {}              # {async_seq: order_uuid}
        # ``_position_callback`` 在成交后通知持仓模块更新仓位。
        self._position_callback: Optional[Callable[[TradeRecord], None]] = None
        # ``_strategy_callback`` 在订单状态变化后通知策略对象。
        self._strategy_callback: Optional[Callable[[Order], None]] = None
        # ``_trade_callback`` 在成交后通知其他订阅方，例如 WebSocket。
        self._trade_callback: Optional[Callable[[TradeRecord], None]] = None
        # ``_state_change_callback`` 用于在订单生命周期变化后触发策略快照持久化。
        self._state_change_callback: Optional[Callable[[str], None]] = None
        # ``_known_trade_ids`` 用于成交幂等保护，避免同一 trade_id 被重复记账。
        self._known_trade_ids: Optional[set[str]] = None
        # ``_lock`` 保护订单字典和映射字典在多线程环境下的一致性。
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 注册

    def register_order(self, order: Order) -> None:
        """注册新订单到内存和持久化层。

        Args:
            order: 新创建的内部订单对象。
        """
        with self._lock:
            self._orders[order.order_uuid] = order
            if str(getattr(order, "order_trace_id", "") or ""):
                self._trace_to_uuid[str(order.order_trace_id)] = order.order_uuid
            if order.xt_order_id:
                # 有真实 xt_order_id 时，立即建立“柜台号 -> 内部 UUID”的反查映射。
                self._xt_to_uuid[order.xt_order_id] = order.order_uuid
        if self._data_mgr:
            try:
                self._data_mgr.save_order(order)
            except Exception as e:
                logger.error("OrderManager: 持久化订单失败: %s", e, exc_info=True)
        logger.info(
            "[ORDER] 注册订单 uuid=%s trace=%s%s code=%s dir=%s price=%.3f qty=%d remark=%s",
            order.order_uuid[:8],
            str(getattr(order, "order_trace_id", "") or ""),
            f" xt_id={order.xt_order_id}" if order.xt_order_id else "",
            order.stock_code,
            order.direction.value,
            order.price,
            order.quantity,
            order.remark,
        )
        self._notify_state_change(f"register_order:{order.order_uuid}")

    # ------------------------------------------------------------------ 状态更新

    def update_order_status(self, xt_order_id: int, status: OrderStatus,
                             filled_qty: int = 0, filled_amount: float = 0,
                             avg_price: float = 0, order_info: Optional[dict] = None) -> None:
        """根据回调结果更新订单状态。

        Args:
            xt_order_id: 柜台订单号。
            status: 内部统一订单状态。
            filled_qty: 最新已成交数量。
            filled_amount: 最新已成交金额。
            avg_price: 最新成交均价。
            order_info: 可选的完整 XtOrder 字段快照。
        """
        with self._lock:
            uuid = self._xt_to_uuid.get(xt_order_id)
            if not uuid and order_info:
                uuid = self._bind_xt_order_id_by_trace_id(xt_order_id, str(order_info.get("order_remark", "") or ""))
            if not uuid:
                # 柜台先推状态、后绑定映射时，可能暂时找不到本地订单，直接忽略等待后续回报。
                return
            order = self._orders.get(uuid)
            if not order:
                return

            if order_info:
                # 如果这次回调携带了完整 XtOrder 信息，
                # 先把原始字段同步到内部订单对象，方便后续展示和排障。
                self._apply_xt_order_fields(order, order_info)

            order.status = status
            if filled_qty or (order_info and "traded_volume" in order_info):
                order.filled_quantity = int(filled_qty or order_info.get("traded_volume", 0) or 0)
            if filled_amount or (order_info and ("traded_amount" in order_info or "traded_price" in order_info)):
                order.filled_amount = self._resolve_filled_amount(order, filled_amount, order_info)
            if avg_price or (order_info and "traded_price" in order_info):
                order.filled_avg_price = float(avg_price or order_info.get("traded_price", 0.0) or 0.0)

            # 订单状态更新时也同步重算“整张订单”的累计费用，
            # 保证订单页看到的费用始终与当前累计成交额一致。
            self._recalculate_order_fee(order)
            order.update_time = datetime.now()

        if self._data_mgr:
            try:
                self._data_mgr.save_order(order)
            except Exception as e:
                logger.error("OrderManager: 更新持久化失败: %s", e, exc_info=True)

        if self._strategy_callback:
            try:
                # 订单状态变化先通知策略，让策略有机会清理 pending、推进自身状态机。
                self._strategy_callback(order)
            except Exception as e:
                logger.error("OrderManager: 策略回调异常: %s", e, exc_info=True)

        logger.debug("[ORDER] 订单状态变更 uuid=%s status=%s", uuid[:8], status.value)
        self._notify_state_change(f"update_order_status:{uuid}")

    def on_trade(self, xt_order_id: int, trade_info: dict) -> None:
        """成交回报入口。

        这里会完成以下动作：
        1. 找到这笔成交对应的内部订单。
        2. 构造 ``TradeRecord``。
        3. 计算手续费拆分。
        4. 更新订单累计成交状态。
        5. 持久化订单和成交。
        6. 通知持仓模块、前端推送模块、策略模块。
        """
        try:
            trade_id = str(trade_info.get("traded_id", trade_info.get("trade_id", "")) or "").strip()
            if trade_id and self._is_duplicate_trade_id(trade_id):
                logger.info("[ORDER] [TRADE] 忽略重复成交 trade_id=%s xt_order_id=%s", trade_id, xt_order_id)
                return

            with self._lock:
                uuid = self._xt_to_uuid.get(xt_order_id)
                if not uuid:
                    uuid = self._bind_xt_order_id_by_trace_id(xt_order_id, str(trade_info.get("order_remark", "") or ""))
                order = self._orders.get(uuid) if uuid else None

            # 如果能找到原订单，就尽量沿用原订单上的 strategy_id/strategy_name；
            # 找不到时再从成交回报字段里兜底取值。
            strategy_id = str(trade_info.get("strategy_id", "") or "") or (order.strategy_id if order else "")
            strategy_name = (order.strategy_name if order else "") or str(trade_info.get("strategy_name", "") or "")
            is_strategy_trade = bool(order or strategy_id)

            if not is_strategy_trade:
                return

            xt_order_type = self._to_int(trade_info.get("order_type", 0))
            xt_direction = self._to_int(trade_info.get("direction", 0))
            offset_flag = self._to_int(trade_info.get("offset_flag", 0))
            xt_traded_time = self._to_int(trade_info.get("traded_time", 0))
            traded_at = self._parse_xt_traded_time(xt_traded_time)
            direction = self._infer_trade_direction(
                offset_flag=offset_flag,
                order_type=xt_order_type,
                xt_direction=xt_direction,
                fallback_order=order,
                raw_direction=trade_info.get("direction", ""),
            )

            # TradeRecord 是“已成交事实”，一旦构造完成就会被分发给持仓层和其他监听方。
            trade = TradeRecord(
                account_type=int(trade_info.get("account_type", 0) or 0),
                account_id=str(trade_info.get("account_id", "") or ""),
                order_type=xt_order_type,
                trade_id=trade_id,
                xt_traded_time=xt_traded_time,
                order_uuid=uuid or "",
                order_trace_id=(getattr(order, "order_trace_id", "") if order else str(trade_info.get("order_remark", "") or "")),
                xt_order_id=xt_order_id,
                order_sysid=str(trade_info.get("order_sysid", "") or ""),
                strategy_id=strategy_id,
                strategy_name=strategy_name,
                order_remark=str(trade_info.get("order_remark", "") or ""),
                remark=(getattr(order, "remark", "") if order else str(trade_info.get("remark", "") or "")),
                stock_code=str(trade_info.get("stock_code", "") or ""),
                direction=direction,
                xt_direction=xt_direction,
                offset_flag=offset_flag,
                price=float(trade_info.get("traded_price", trade_info.get("price", 0)) or 0),
                quantity=int(trade_info.get("traded_volume", trade_info.get("quantity", 0)) or 0),
                amount=float(trade_info.get("traded_amount", trade_info.get("amount", 0)) or 0),
                commission=float(trade_info.get("commission", 0)),
                secu_account=str(trade_info.get("secu_account", "") or ""),
                instrument_name=str(trade_info.get("instrument_name", "") or ""),
                xt_fields=dict(trade_info.get("xt_fields", {}) or {}),
                trade_time=traded_at,
            )
            self._apply_fee_breakdown(trade)

            # 更新订单已成交量
            if order:
                with self._lock:
                    # previous_fee / delta_fee 的组合，是为了把“整单累计费用”拆回“本次新增成交费用”。
                    previous_fee = self._calculate_fee(order.stock_code, direction, order.filled_amount)
                    order.filled_quantity += trade.quantity
                    order.filled_amount += trade.amount
                    if order.filled_quantity > 0:
                        order.filled_avg_price = order.filled_amount / order.filled_quantity
                    if order.filled_quantity >= order.quantity:
                        order.status = OrderStatus.SUCCEEDED
                    else:
                        order.status = OrderStatus.PART_SUCC
                    self._recalculate_order_fee(order)
                    delta_fee = self._diff_fee(self._calculate_fee(order.stock_code, direction, order.filled_amount), previous_fee)
                    trade.buy_commission = delta_fee.buy_commission
                    trade.sell_commission = delta_fee.sell_commission
                    trade.stamp_tax = delta_fee.stamp_tax
                    trade.total_fee = delta_fee.total_fee
                    trade.is_t0 = delta_fee.is_t0
                    trade.commission = delta_fee.total_fee
                    order.update_time = datetime.now()

            # 持久化成交
            if self._data_mgr:
                try:
                    self._data_mgr.save_trade(trade)
                    if order:
                        self._data_mgr.save_order(order)
                except Exception as e:
                    logger.error("OrderManager: 成交持久化失败: %s", e, exc_info=True)

            # 通知持仓模块
            if self._position_callback:
                try:
                    # 这里由 PositionManager 根据成交事实更新持仓数量、成本和盈亏。
                    self._position_callback(trade)
                except Exception as e:
                    logger.error("OrderManager: 持仓回调异常: %s", e, exc_info=True)

            # 通知成交监听方（如 WebSocket）
            if self._trade_callback:
                try:
                    self._trade_callback(trade)
                except Exception as e:
                    logger.error("OrderManager: 成交通知回调异常: %s", e, exc_info=True)

            # 通知策略模块
            if order and self._strategy_callback:
                try:
                    # 成交后再通知策略，是为了让策略拿到更新后的 order 累计成交状态。
                    self._strategy_callback(order)
                except Exception as e:
                    logger.error("OrderManager: 策略回调异常: %s", e, exc_info=True)

            logger.info("[ORDER] [TRADE] 成交 uuid=%s code=%s price=%.3f qty=%d",
                        (uuid or "?")[:8], trade.stock_code, trade.price, trade.quantity)
            if trade_id:
                self._mark_trade_id_seen(trade_id)
            self._notify_state_change(f"trade:{uuid or xt_order_id}")

        except Exception as e:
            logger.error("OrderManager: on_trade 处理异常: %s", e, exc_info=True)

    def _is_duplicate_trade_id(self, trade_id: str) -> bool:
        """判断某个成交编号是否已经被处理过。"""
        normalized = str(trade_id or "").strip()
        if not normalized:
            return False
        return normalized in self._get_known_trade_ids()

    def _mark_trade_id_seen(self, trade_id: str) -> None:
        """把成交编号登记为已处理。"""
        normalized = str(trade_id or "").strip()
        if not normalized:
            return
        self._get_known_trade_ids().add(normalized)

    def _get_known_trade_ids(self) -> set[str]:
        """懒加载已知成交编号集合，兼容重启后的历史去重。"""
        if self._known_trade_ids is None:
            known_trade_ids: set[str] = set()
            if self._data_mgr:
                try:
                    known_trade_ids = {
                        str(row.get("trade_id", "") or "").strip()
                        for row in (self._data_mgr.query_trades() or [])
                        if str(row.get("trade_id", "") or "").strip()
                    }
                except Exception as exc:
                    logger.warning("OrderManager: 预热成交去重集合失败: %s", exc)
            self._known_trade_ids = known_trade_ids
        return self._known_trade_ids

    @staticmethod
    def _infer_trade_direction(offset_flag: int, order_type: int, xt_direction: int,
                               fallback_order: Optional[Order], raw_direction) -> OrderDirection:
        """根据 XtTrade 字段推断买卖方向（优先 offset_flag/order_type）。"""
        # xtquant 在不同回调里方向字段可能不一致，所以这里做多字段兜底推断。
        buy_markers = {23}
        sell_markers = {24}

        for marker in (offset_flag, order_type, xt_direction):
            if marker in buy_markers:
                return OrderDirection.BUY
            if marker in sell_markers:
                return OrderDirection.SELL

        raw = str(raw_direction or "").upper()
        if "BUY" in raw:
            return OrderDirection.BUY
        if "SELL" in raw:
            return OrderDirection.SELL

        if fallback_order:
            # 前面都推不出来时，退回到原订单方向，至少保证内部口径一致。
            return fallback_order.direction
        return OrderDirection.BUY

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        """把输入安全转换为整数，失败时返回默认值。"""
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_xt_traded_time(xt_traded_time: int) -> datetime:
        """把 XtTrade 的时间整数字段转换为 `datetime` 对象。"""
        if xt_traded_time <= 0:
            return datetime.now()
        text = str(xt_traded_time)
        for fmt, length in (("%Y%m%d%H%M%S", 14), ("%Y%m%d", 8)):
            try:
                # 有些环境返回到秒级，有些只给日期，这里两种格式都兼容。
                return datetime.strptime(text[:length], fmt)
            except ValueError:
                continue
        return datetime.now()

    def _apply_fee_breakdown(self, trade: TradeRecord) -> None:
        """为成交补齐手续费拆分结果。"""
        if trade.amount <= 0 and trade.price > 0 and trade.quantity > 0:
            # 有些成交回报不直接给 amount，这里按 price * quantity 兜底补齐。
            trade.amount = trade.price * trade.quantity

        if self._fee_schedule:
            fee = self._calculate_fee(trade.stock_code, trade.direction, trade.amount)
            trade.buy_commission = fee.buy_commission
            trade.sell_commission = fee.sell_commission
            trade.stamp_tax = fee.stamp_tax
            trade.total_fee = fee.total_fee
            trade.is_t0 = fee.is_t0
            trade.commission = fee.total_fee
            return

        trade.total_fee = float(trade.commission or 0.0)
        if trade.direction == OrderDirection.BUY:
            trade.buy_commission = trade.total_fee
        else:
            trade.sell_commission = trade.total_fee

    def _apply_xt_order_fields(self, order: Order, order_info: dict) -> None:
        """把 XtOrder 回报中的完整字段同步到内部订单对象。

        Args:
            order: 要更新的内部订单对象。
            order_info: 标准化后的 XtOrder 字段字典。
        """
        order.account_type = int(order_info.get("account_type", 0) or 0)
        order.account_id = str(order_info.get("account_id", "") or "")
        order.xt_stock_code = str(order_info.get("xt_stock_code", "") or "")
        order.order_sysid = str(order_info.get("order_sysid", "") or "")
        order.order_time = int(order_info.get("order_time", 0) or 0)
        order.xt_order_type = int(order_info.get("order_type", 0) or 0)
        order.price_type = int(order_info.get("price_type", 0) or 0)
        order.xt_order_status = int(order_info.get("order_status", 0) or 0)
        order.status_msg = str(order_info.get("status_msg", "") or "")
        order.xt_direction = int(order_info.get("direction", 0) or 0)
        order.offset_flag = int(order_info.get("offset_flag", 0) or 0)
        order.secu_account = str(order_info.get("secu_account", "") or "")
        order.instrument_name = str(order_info.get("instrument_name", "") or "")
        order.xt_fields = dict(order_info.get("xt_fields", {}) or {})
        trace_id = str(order_info.get("order_remark", "") or "").strip()
        if trace_id and not str(getattr(order, "order_trace_id", "") or ""):
            order.order_trace_id = trace_id
        if trace_id:
            self._trace_to_uuid[trace_id] = order.order_uuid
        if not order.stock_code:
            order.stock_code = str(order_info.get("stock_code", "") or "")
        if not order.strategy_name:
            order.strategy_name = str(order_info.get("strategy_name", "") or "")
        if not order.quantity:
            order.quantity = int(order_info.get("order_volume", 0) or 0)
        if not order.price:
            # 某些情况下本地下单对象价格为空，这里用回报里的价格回填。
            order.price = float(order_info.get("price", 0.0) or 0.0)

    @staticmethod
    def _resolve_filled_amount(order: Order, filled_amount: float, order_info: Optional[dict]) -> float:
        """优先使用显式成交额，否则退回“成交量 × 成交均价”估算。"""
        if filled_amount:
            return float(filled_amount)
        if not order_info:
            return order.filled_amount
        traded_amount = float(order_info.get("traded_amount", 0.0) or 0.0)
        if traded_amount > 0:
            return traded_amount
        traded_volume = int(order_info.get("traded_volume", 0) or 0)
        traded_price = float(order_info.get("traded_price", 0.0) or 0.0)
        if traded_volume > 0 and traded_price > 0:
            # 某些回报只给了成交量和成交均价，没有直接给成交额。
            return traded_volume * traded_price
        return order.filled_amount

    def _recalculate_order_fee(self, order: Order) -> None:
        """根据订单累计成交额重新计算订单总费用。

        这里按“整张订单的累计成交额”合并计算，
        避免部分成交逐笔向上取整导致手续费累计偏大。
        """
        # 订单页展示的费用应当是“截至当前累计成交”的总费用，而不是最后一笔成交的费用。
        fee = self._calculate_fee(order.stock_code, order.direction, order.filled_amount)
        order.buy_commission = fee.buy_commission
        order.sell_commission = fee.sell_commission
        order.stamp_tax = fee.stamp_tax
        order.total_fee = fee.total_fee
        order.commission = fee.total_fee

    def _calculate_fee(self, stock_code: str, direction: OrderDirection, amount: float) -> FeeBreakdown:
        """按累计成交额计算费用。"""
        if not self._fee_schedule:
            return FeeBreakdown()
        return self._fee_schedule.calculate(stock_code, direction, amount)

    @staticmethod
    def _diff_fee(current: FeeBreakdown, previous: FeeBreakdown) -> FeeBreakdown:
        """计算两次累计费率结果之间的差值，作为本次新增成交的费用。"""
        return FeeBreakdown(
            buy_commission=max(0.0, current.buy_commission - previous.buy_commission),
            sell_commission=max(0.0, current.sell_commission - previous.sell_commission),
            stamp_tax=max(0.0, current.stamp_tax - previous.stamp_tax),
            total_fee=max(0.0, current.total_fee - previous.total_fee),
            is_t0=current.is_t0,
        )

    def on_async_response(self, seq: int, xt_order_id: int) -> None:
        """绑定异步下单序列号与柜台订单号。

        Args:
            seq: 异步下单返回的序列号。
            xt_order_id: 柜台真实订单号。
        """
        with self._lock:
            uuid = self._seq_to_uuid.pop(seq, None)
            if uuid:
                # 异步下单先拿到 seq，再拿到 xt_order_id；这里完成两者与内部订单 UUID 的拼接。
                self._xt_to_uuid[xt_order_id] = uuid
                order = self._orders.get(uuid)
                if order:
                    order.xt_order_id = xt_order_id
                    order.status = OrderStatus.WAIT_REPORTING
                    order.update_time = datetime.now()
        logger.debug("OrderManager: async_response seq=%d → xt_id=%d", seq, xt_order_id)
        self._notify_state_change(f"async_response:{xt_order_id}")

    def mark_order_status(self, order_uuid: str, status: OrderStatus,
                          status_msg: str = "", order_info: Optional[dict] = None,
                          filled_qty: Optional[int] = None,
                          filled_amount: Optional[float] = None,
                          avg_price: Optional[float] = None) -> Optional[Order]:
        """直接按内部 UUID 修正订单状态。"""
        with self._lock:
            order = self._orders.get(order_uuid)
            if not order:
                return None

            if order_info:
                self._apply_xt_order_fields(order, order_info)

            order.status = status
            if status_msg:
                order.status_msg = str(status_msg)
            if filled_qty is not None:
                order.filled_quantity = int(filled_qty or 0)
            if filled_amount is not None:
                order.filled_amount = float(filled_amount or 0.0)
            if avg_price is not None:
                order.filled_avg_price = float(avg_price or 0.0)
            self._recalculate_order_fee(order)
            order.update_time = datetime.now()

        if self._data_mgr:
            try:
                self._data_mgr.save_order(order)
            except Exception as e:
                logger.error("OrderManager: 强制更新订单持久化失败: %s", e, exc_info=True)

        if self._strategy_callback:
            try:
                self._strategy_callback(order)
            except Exception as e:
                logger.error("OrderManager: 强制更新策略回调异常: %s", e, exc_info=True)

        self._notify_state_change(f"mark_order_status:{order_uuid}")
        return order

    def register_seq(self, seq: int, order_uuid: str) -> None:
        """为异步下单预注册 `seq -> order_uuid` 映射。"""
        with self._lock:
            # 这个映射是异步下单模式下把“本地下单请求”和“后续柜台订单号”连起来的关键。
            self._seq_to_uuid[int(seq)] = order_uuid

    # ------------------------------------------------------------------ 查询

    def get_order(self, order_uuid: str) -> Optional[Order]:
        """按内部 UUID 获取订单。"""
        return self._orders.get(order_uuid)

    def get_order_by_xt_id(self, xt_order_id: int) -> Optional[Order]:
        """按柜台订单号获取订单。"""
        with self._lock:
            uuid = self._xt_to_uuid.get(xt_order_id)
            return self._orders.get(uuid) if uuid else None

    def get_order_by_trace_id(self, order_trace_id: str) -> Optional[Order]:
        """按 23 位跟踪标识获取订单。"""
        with self._lock:
            uuid = self._trace_to_uuid.get(str(order_trace_id or ""))
            return self._orders.get(uuid) if uuid else None

    def get_orders_by_strategy(self, strategy_id: str) -> List[Order]:
        """获取某个策略名下的全部订单。"""
        with self._lock:
            return [o for o in self._orders.values() if o.strategy_id == strategy_id]

    def get_active_orders(self) -> List[Order]:
        """获取全部尚未终结的活跃订单。"""
        with self._lock:
            return [o for o in self._orders.values() if o.is_active()]

    # ------------------------------------------------------------------ 回调注册

    def set_position_callback(self, callback: Callable[[TradeRecord], None]) -> None:
        """注册“成交 -> 持仓更新”回调。"""
        self._position_callback = callback

    def set_strategy_callback(self, callback: Callable[[Order], None]) -> None:
        """注册“订单变化 -> 策略通知”回调。"""
        self._strategy_callback = callback

    def set_trade_callback(self, callback: Callable[[TradeRecord], None]) -> None:
        """注册“成交变化 -> 其他监听方”回调，例如 WebSocket 推送。"""
        self._trade_callback = callback

    def set_state_change_callback(self, callback: Callable[[str], None]) -> None:
        """注册订单生命周期变化后的持久化回调。"""
        self._state_change_callback = callback

    def restore_orders(self, orders: List[Order]) -> None:
        """把持久化层中的活动订单重新装载回内存索引。"""
        with self._lock:
            for order in orders:
                self._orders[order.order_uuid] = order
                if str(getattr(order, "order_trace_id", "") or ""):
                    self._trace_to_uuid[str(order.order_trace_id)] = order.order_uuid
                if int(order.xt_order_id or 0) > 0:
                    self._xt_to_uuid[int(order.xt_order_id)] = order.order_uuid

    def _bind_xt_order_id_by_trace_id(self, xt_order_id: int, order_trace_id: str) -> Optional[str]:
        """当异步响应丢失时，尝试用 xt `order_remark` 回查并补绑柜台订单号。"""
        trace_id = str(order_trace_id or "").strip()
        if not trace_id:
            return None

        uuid = self._trace_to_uuid.get(trace_id)
        if not uuid:
            return None

        order = self._orders.get(uuid)
        if not order:
            return None

        self._xt_to_uuid[int(xt_order_id)] = uuid
        order.xt_order_id = int(xt_order_id or 0)
        if not str(getattr(order, "order_trace_id", "") or ""):
            order.order_trace_id = trace_id
        return uuid

    def _notify_state_change(self, reason: str) -> None:
        """在订单生命周期发生变化后通知上层保存最新快照。"""
        if not self._state_change_callback:
            return
        try:
            self._state_change_callback(reason)
        except Exception as exc:
            logger.error("OrderManager: 状态变更回调失败: %s", exc, exc_info=True)


__all__ = ["OrderManager"]
