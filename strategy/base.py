"""策略基类模块。

本模块定义所有策略必须遵循的统一接口与通用行为，包括：
- 行情处理入口
- 信号到交易动作的转换
- 通用止损止盈检查
- 订单跟踪与快照恢复

这样不同策略只需要关注“何时买卖”，而不必重复实现公共骨架。
"""
import uuid
import threading
import weakref
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.enums import OrderDirection, OrderStatus, StrategyStatus
from core.models import TickData
from position.models import PositionInfo
from strategy.models import StrategyConfig, StrategySnapshot
from trading.models import Order
from monitor.logger import get_logger

logger = get_logger("trade")


class BaseStrategy(ABC):
    """策略基类模板。

    类属性（所有策略实例共享）需在子类中定义：
        strategy_name: str      = "未命名策略"
        max_positions: int      = 1       最大持仓标的数
        max_total_amount: float = 0.0     最大可用总金额

    对象属性（每个实例独立）：
        strategy_id:       str (UUID)
        stock_code:        str
        status:            StrategyStatus
        config:            StrategyConfig

    建议把这个基类理解成“策略执行骨架”：
    1. 子类只负责回答“现在该不该买 / 卖”。
    2. 基类负责把信号变成委托、维护待成交订单、处理订单回调。
    3. 基类还负责快照持久化与恢复，让子类能跨交易日续跑。
    """

    # ---- 子类需覆盖的类属性 -------------------------------------------------
    strategy_name: str = "BaseStrategy"
    max_positions: int = 5
    max_total_amount: float = 100000.0
    state_version: int = 1

    # ---- 类级别共享统计（需子类自行维护 thread safety 如有需要） ----
    current_positions: int = 0
    current_used_amount: float = 0.0
    _class_used_amount: float = 0.0
    _supported_data_kinds = frozenset({"tick", "l2quote", "l2transaction", "l2order", "l2orderqueue"})
    _current_positions_count: int = 0  # 当前持仓标的数
    _lock = threading.Lock()  # 类级别锁，保护共享统计

    def __init__(self, config: StrategyConfig,
                 trade_executor=None, position_manager=None):
        """初始化策略实例。

        Args:
            config: 当前策略实例的配置对象。
            trade_executor: 交易执行器。
            position_manager: 持仓管理器。
        """
        # ``strategy_id`` 是每个策略实例的唯一标识。
        self.strategy_id: str = str(uuid.uuid4())
        # ``stock_code`` 表示当前策略实例唯一负责的标的代码。
        self.stock_code: str = config.stock_code
        # ``status`` 记录策略当前运行状态，例如运行中、暂停、已停止。
        self.status: StrategyStatus = StrategyStatus.INITIALIZING
        # ``config`` 保存该策略实例的全部配置参数。
        self.config: StrategyConfig = config
        # ``_trade_executor`` 负责把策略信号变成下单请求。
        self._trade_executor = trade_executor
        # ``_position_mgr`` 用于查询当前策略的持仓状态。
        self._position_mgr = position_manager
        # ``_data_mgr`` 用于运行态持久化和清理，由 Runner 在接管时注入。
        self._data_mgr = None
        # ``_state_persist_callback`` 由 Runner 注入，便于策略在关键事件后请求即时持久化。
        self._state_persist_callback = None
        # ``_pending_orders`` 保存尚未终结的订单对象，便于防重复下单和状态跟踪。
        self._pending_orders: Dict[str, Order] = {}   # {order_uuid: Order}
        # ``_pending_order_recovery_ids`` 暂存从快照恢复出的活动订单 UUID，
        # 由 Runner 在启动时统一从 SQLite 重建真实订单对象。
        self._pending_order_recovery_ids: List[str] = []
        # ``_create_time`` 记录该策略实例创建时间。
        self._create_time = datetime.now()
        # ``_orders_history`` 保存该策略实例经历过的全部订单历史。
        self._orders_history: List[Order] = []
        # ``_pause_reason`` 保存最近一次暂停原因，便于 Web/API 展示与恢复。
        self._pause_reason: str = ""
        # ``_pending_close_requested`` 表示当前策略还有未完成的跨日平仓请求。
        self._pending_close_requested: bool = False
        # ``_pending_close_remark`` 保留触发平仓的原始原因，重启和跨日后继续沿用。
        self._pending_close_remark: str = ""
        # ``_last_stop_loss_remark`` / ``_last_stop_loss_value`` 允许子类把更复杂的止损原因回传给统一日志层。
        self._last_stop_loss_remark: str = "触发止损"
        self._last_stop_loss_value: float = float(self.config.stop_loss_price or 0.0)

        self._register_instance()

        logger.info("Strategy[%s] %s 初始化 stock=%s",
                    self.strategy_id[:8], self.strategy_name, self.stock_code)

    def bind_persistence(self, data_manager=None, persist_callback=None) -> None:
        """绑定运行态持久化依赖。"""
        self._data_mgr = data_manager
        self._state_persist_callback = persist_callback

    def request_state_persist(self, reason: str = "", min_interval_sec: float = 0.0) -> None:
        """请求 Runner 立即持久化当前运行态。"""
        if callable(self._state_persist_callback):
            self._state_persist_callback(reason=reason, min_interval_sec=min_interval_sec)

    # ------------------------------------------------------------------ Abstract

    @abstractmethod
    def on_tick(self, tick: TickData) -> Optional[dict]:
        """
        每次行情推送时调用。
        只生成信号，不直接调用交易方法（信号与交易分离）。

        Returns:
            信号字典 或 None（无信号）
            格式: {
                "action": "BUY" | "SELL" | "CLOSE",
                "price": float,
                "quantity": int,       # 按数量时使用
                "amount": float,       # 按金额时使用
                "remark": str,
            }
        """

    @abstractmethod
    def select_stocks(self) -> List[StrategyConfig]:
        """
        选股方法。返回待开仓标的的配置列表。
        可从外部文件读取，也可自行计算。
        """

    # ------------------------------------------------------------------ 主处理流程

    @classmethod
    def required_data_kinds(cls) -> set[str]:
        """Declare which market data channels this strategy needs."""
        return {"tick"}

    def process_tick(self, tick: TickData) -> None:
        """处理一条最新行情。

        Args:
            tick: 当前标的的最新标准化行情对象。
        """
        # 非运行态时直接忽略行情，避免暂停/停止中的策略继续动作。
        if self.status not in (StrategyStatus.RUNNING,):
            return
        # 每个策略实例只处理自己绑定的那个标的。
        if tick.stock_code != self.stock_code:
            return
        try:
            # 更新持仓最新价
            if self._position_mgr:
                self._position_mgr.update_price(self.stock_code, tick.last_price)

            # 若已经有活跃中的卖出单，说明该标的正在退出流程中；
            # 此时继续重复做止损检查只会产生噪声日志和重复平仓请求。
            if self._has_active_exit_order():
                return

            # 若已存在跨交易日待平仓请求，则优先继续执行退出流程，
            # 防止策略在持仓未清完时重新产生新的开仓/加仓动作。
            if self._process_pending_close_request():
                return

            # 风控前置检查：先看是否需要立即止损/止盈，只有通过后才交给子类继续生成新信号。
            if self._check_risk(tick):
                return

            # 先由子类根据行情生成信号，
            # 再统一走 `_execute_signal()` 转换成交易动作。
            signal = self.on_tick(tick)

            # 执行信号
            if signal:
                self._execute_signal(signal)

        except Exception as e:
            logger.error("Strategy[%s] process_tick 异常: %s",
                         self.strategy_id[:8], e, exc_info=True)
            self.status = StrategyStatus.ERROR

    def before_process_tick(self, tick: TickData) -> None:
        """在处理 tick 前执行轻量状态维护。

        子类可覆盖，用于在真正进入 `process_tick()` 前做恢复/清理判断。
        """
        return None

    def on_l2_quote(self, event: Any) -> None:
        """Handle a normalized Level2 quote event."""
        return None

    def on_l2_transaction(self, event: Any) -> None:
        """Handle a normalized Level2 transaction event."""
        return None

    def on_l2_order(self, event: Any) -> None:
        """Handle a normalized Level2 order event."""
        return None

    def on_l2_orderqueue(self, event: Any) -> None:
        """Handle a normalized Level2 order queue event."""
        return None

    def _check_risk(self, tick: TickData) -> bool:
        """执行通用风控检查。

        Returns:
            若已触发止损或止盈并完成处理，则返回 `True`。
        """
        # 止损优先级高于止盈，先检查下行风险。
        if self.check_stop_loss(tick):
            stop_remark = self._get_stop_loss_remark(tick)
            stop_value = self._get_stop_loss_log_value(tick)
            if stop_value > 0:
                logger.warning("Strategy[%s] %s price=%.3f stop=%.3f",
                               self.strategy_id[:8], stop_remark, tick.last_price,
                               stop_value)
            else:
                logger.warning("Strategy[%s] %s price=%.3f",
                               self.strategy_id[:8], stop_remark, tick.last_price)
            self._handle_stop_loss_exit(tick, stop_remark)
            return True
        if self.check_take_profit(tick):
            logger.info("Strategy[%s] 触发止盈 price=%.3f tp=%.3f",
                        self.strategy_id[:8], tick.last_price,
                        self.config.take_profit_price)
            self.close_position(remark="触发止盈")
            return True
        return False

    def _execute_signal(self, signal: dict) -> None:
        """根据信号执行交易。

        策略只负责生成信号；真正的下单动作统一走交易执行器，
        这样可以让“策略逻辑”和“交易接口细节”彻底分离。
        """
        # 统一把信号字典拆成标准字段，子类只要按约定返回 dict 即可。
        action = signal.get("action", "").upper()
        price = float(signal.get("price", 0))
        quantity = int(signal.get("quantity", 0))
        amount = float(signal.get("amount", 0))
        remark = signal.get("remark", action)

        if action == "BUY":
            # BUY 同时支持“按金额下单”和“按股数下单”两种模式。
            if amount > 0:
                self.add_position_by_amount(price, amount, remark)
            elif quantity > 0:
                self.add_position(price, quantity, remark)
        elif action == "SELL":
            # SELL 这里表示减仓，不一定是全平；全平走 CLOSE。
            if quantity > 0:
                self.reduce_position(price, quantity, remark)
        elif action == "CLOSE":
            self.close_position(remark)

    @staticmethod
    def _is_insufficient_funds_message(message: str) -> bool:
        """判断失败原因是否属于账户资金不足。"""
        text = str(message or "").strip()
        if not text:
            return False
        keywords = (
            "资金不足",
            "可用资金不足",
            "可用金额不足",
            "余额不足",
            "现金不足",
            "insufficient funds",
            "insufficient cash",
        )
        lowered = text.lower()
        return any(keyword in text or keyword in lowered for keyword in keywords)

    def _pause_for_order_rejection(self, reason: str) -> None:
        """在关键下单失败时暂停策略，并持久化暂停原因。"""
        pause_reason = str(reason or "下单失败")
        logger.warning("Strategy[%s] 因下单失败自动暂停: %s", self.strategy_id[:8], pause_reason)
        self.pause(reason=pause_reason)
        self.request_state_persist(reason=f"order_rejection_pause:{self.strategy_id}")

    # ------------------------------------------------------------------ 仓位操作

    def add_position(self, price: float, quantity: int, remark: str = "") -> Optional[Order]:
        """按股数加仓，并执行最大持仓金额限制检查。"""
        if not self._trade_executor:
            return None
            
        # 这里的风控口径是“当前持仓市值 + 本次计划委托金额”。
        if self._position_mgr and self.config.max_position_amount > 0:
            pos = self._position_mgr.get_position(self.strategy_id)
            # current_value 是当前策略实例已持仓的市值；order_value 是本次计划买入金额。
            current_value = pos.market_value if pos else 0.0
            order_value = price * quantity
            if current_value + order_value > self.config.max_position_amount:
                logger.warning("Strategy[%s] 加仓受限: 当前市值 %.2f + 购买金额 %.2f > 上限 %.2f",
                               self.strategy_id[:8], current_value, order_value, self.config.max_position_amount)
                # 调整为允许的最大可买数量
                allowed_amount = self.config.max_position_amount - current_value
                if allowed_amount < price * 100:  # 不足一手
                    return None
                quantity = int((allowed_amount / price) // 100) * 100
                logger.info("Strategy[%s] 订单重置为允许的最大数量: %d 股", self.strategy_id[:8], quantity)

            # 真正的买入委托由 trade_executor 发出，策略层不直接接触券商接口细节。
        order = self._trade_executor.buy_limit(
            self.strategy_id, self.strategy_name,
            self.stock_code, price, quantity, remark
        )
        self._track_order(order)
        # 订单成交后会通过成交回调更新持仓，此处同步类属性
        self.__class__._sync_class_stats(self._position_mgr)
        return order

    def add_position_by_amount(self, price: float, amount: float,
                                remark: str = "") -> Optional[Order]:
        """按金额加仓，并执行最大持仓金额限制检查。"""
        if not self._trade_executor:
            return None
            
        # 仓位上限风控：即使策略传进来一个金额，也要先约束到 max_position_amount 以内。
        if self._position_mgr and self.config.max_position_amount > 0:
            pos = self._position_mgr.get_position(self.strategy_id)
            current_value = pos.market_value if pos else 0.0
            if current_value + amount > self.config.max_position_amount:
                logger.warning("Strategy[%s] 加仓金额受限: 当前市值 %.2f + 计划金额 %.2f > 上限 %.2f",
                               self.strategy_id[:8], current_value, amount, self.config.max_position_amount)
                amount = self.config.max_position_amount - current_value
                if amount < price * 100:
                    return None

        # buy_by_amount 由执行器决定如何换算成股数，基类只负责把意图往下传。
        order = self._trade_executor.buy_by_amount(
            self.strategy_id, self.strategy_name,
            self.stock_code, price, amount, remark
        )
        self._track_order(order)
        return order

    def reduce_position(self, price: float, quantity: int,
                        remark: str = "") -> Optional[Order]:
        """按指定价格和数量减仓。"""
        if not self._trade_executor:
            return None
        # 减仓不会自动把策略停掉，是否停掉要等订单回报后结合剩余仓位再判断。
        order = self._trade_executor.sell_limit(
            self.strategy_id, self.strategy_name,
            self.stock_code, price, quantity, remark
        )
        self._track_order(order)
        return order

    def close_position(self, remark: str = "") -> Optional[Order]:
        """提交清仓请求。"""
        if not self._trade_executor:
            return None

        position = self._position_mgr.get_position(self.strategy_id) if self._position_mgr else None
        total_quantity = int(getattr(position, "total_quantity", 0) or 0)
        available_quantity = int(getattr(position, "available_quantity", 0) or 0)
        close_remark = remark or "策略平仓"

        if total_quantity <= 0:
            self._clear_pending_close_request()
            return None

        if total_quantity > available_quantity:
            self._set_pending_close_request(close_remark)

        if available_quantity <= 0:
            logger.info(
                "Strategy[%s] 平仓请求已登记，当前无可用持仓，等待下一交易日解锁后继续平仓 code=%s total=%d available=%d reason=%s",
                self.strategy_id[:8],
                self.stock_code,
                total_quantity,
                available_quantity,
                close_remark,
            )
            return None

        if total_quantity <= available_quantity:
            self._clear_pending_close_request()

        # close_position 表示“把当前策略实例对应仓位全部平掉”，由执行器内部决定具体可卖数量。
        order = self._trade_executor.close_position(
            self.strategy_id, self.strategy_name,
            self.stock_code, remark=close_remark
        )
        self._track_order(order)
        # 平仓成交后同步类属性
        self.__class__._sync_class_stats(self._position_mgr)
        return order

    # ------------------------------------------------------------------ 止盈止损

    def check_stop_loss(self, tick: TickData) -> bool:
        """止损检查（子类可覆盖）"""
        # 基类只支持固定价格止损，更复杂的逻辑由子类覆盖这个方法。
        if self.config.stop_loss_price <= 0:
            return False
        pos = self._position_mgr.get_position(self.strategy_id) if self._position_mgr else None
        if not pos or pos.total_quantity <= 0:
            return False
        return tick.last_price <= self.config.stop_loss_price

    def check_take_profit(self, tick: TickData) -> bool:
        """止盈检查（子类可覆盖）"""
        # 与止损相同，基类只实现最简单的固定价格止盈。
        if self.config.take_profit_price <= 0:
            return False
        pos = self._position_mgr.get_position(self.strategy_id) if self._position_mgr else None
        if not pos or pos.total_quantity <= 0:
            return False
        return tick.last_price >= self.config.take_profit_price

    def _get_stop_loss_remark(self, tick: TickData) -> str:
        """返回当前止损触发时用于日志和平仓备注的文案。"""
        return self._last_stop_loss_remark or "触发止损"

    def _get_stop_loss_log_value(self, tick: TickData) -> float:
        """返回当前止损日志中应显示的阈值。"""
        value = float(self._last_stop_loss_value or 0.0)
        if value > 0:
            return value
        return float(self.config.stop_loss_price or 0.0)

    def _handle_stop_loss_exit(self, tick: TickData, remark: str) -> Optional[Order]:
        """止损触发时，先撤未完成买单，再按最新价提交卖单。"""
        canceled_count = self._cancel_active_buy_orders(remark=f"{remark} 前撤销未完成买单")
        if canceled_count > 0:
            logger.info("Strategy[%s] 止损前撤销 %d 笔未完成买单",
                        self.strategy_id[:8], canceled_count)
        return self._submit_stop_loss_order(tick, remark)

    def _cancel_active_buy_orders(self, remark: str = "") -> int:
        """撤销当前策略仍处于活动态的买单。"""
        if not self._trade_executor:
            return 0

        canceled_count = 0
        cancel_order = getattr(self._trade_executor, "cancel_order", None)
        if not callable(cancel_order):
            return 0

        for order_uuid, order in list(self._pending_orders.items()):
            if order.direction != OrderDirection.BUY or not order.is_active():
                continue
            canceled = bool(cancel_order(order_uuid, remark=remark or "止损前撤单"))
            if not canceled:
                logger.warning("Strategy[%s] 止损前撤买单失败 uuid=%s",
                               self.strategy_id[:8], order_uuid[:8])
                continue
            order.status = OrderStatus.CANCELED
            self._pending_orders.pop(order_uuid, None)
            canceled_count += 1
        return canceled_count

    def _submit_stop_loss_order(self, tick: TickData, remark: str) -> Optional[Order]:
        """用最新价模式提交止损卖单；不可用时退回普通平仓。

        当前模拟盘阶段，交易执行器内部已经把 ``sell_market`` 统一收敛到
        ``sell_latest``，这样止损与主动平仓都走同一套最新价口径。
        """
        if not self._trade_executor or not self._position_mgr:
            return self.close_position(remark=remark)

        position = self._position_mgr.get_position(self.strategy_id)
        available_quantity = int(getattr(position, "available_quantity", 0) or 0)

        if available_quantity <= 0:
            return self.close_position(remark=remark)

        order = self._trade_executor.sell_market(
            self.strategy_id,
            self.strategy_name,
            self.stock_code,
            available_quantity,
            remark,
        )
        self._track_order(order)
        self.__class__._sync_class_stats(self._position_mgr)
        return order

    def _has_active_exit_order(self) -> bool:
        """判断当前策略是否已经存在活跃中的卖出/平仓订单。"""
        return any(order.direction == OrderDirection.SELL and order.is_active() for order in self._pending_orders.values())

    # ------------------------------------------------------------------ 控制

    def start(self) -> None:
        """把策略状态切换为运行中。"""
        self.status = StrategyStatus.RUNNING
        logger.info("Strategy[%s] %s 启动", self.strategy_id[:8], self.strategy_name)

    def prepare_for_trading_day(self, trade_day: str) -> bool:
        """为交易日启动做预初始化。

        默认实现什么都不做，返回 ``True`` 表示准备成功。
        具体策略可覆盖该钩子，把原本首个 tick 才执行的重初始化前移到订阅前。
        """
        return True

    def can_recover_from_account_position(self, account_position) -> bool:
        """判断是否允许用账户真实持仓恢复该策略实例。"""
        return False

    def suggest_account_recovery_quantity(self, account_position) -> int:
        """返回该策略期望接管的账户持仓数量。

        返回 ``0`` 表示交由运行器按默认分配规则处理。
        """
        return 0

    def on_account_position_recovered(self, position: PositionInfo, trade_day: str) -> None:
        """账户持仓恢复成功后的策略级回调。"""
        pass

    def pause(self, reason: str = "") -> None:
        """把策略状态切换为暂停。"""
        self.status = StrategyStatus.PAUSED
        self._pause_reason = str(reason or self._pause_reason or "")
        if self._pause_reason:
            logger.info("Strategy[%s] 暂停: %s", self.strategy_id[:8], self._pause_reason)
        else:
            logger.info("Strategy[%s] 暂停", self.strategy_id[:8])

    def resume(self) -> None:
        """恢复策略运行。"""
        self.status = StrategyStatus.RUNNING
        self._pause_reason = ""
        logger.info("Strategy[%s] 恢复", self.strategy_id[:8])

    def stop(self) -> None:
        """停止策略运行。"""
        self.status = StrategyStatus.STOPPED
        self._clear_pending_close_request()
        logger.info("Strategy[%s] 停止", self.strategy_id[:8])

    def get_pause_reason(self) -> str:
        """返回最近一次暂停原因。"""
        return str(self._pause_reason or "")

    @classmethod
    def uses_position_slot_management(cls) -> bool:
        """是否启用“总持仓标的名额”通用能力。"""
        return False

    @classmethod
    def capacity_wait_pause_reason(cls) -> str:
        """等待空余名额时使用的统一暂停原因。"""
        return f"{cls.strategy_name} 等待空余名额"

    @classmethod
    def capacity_config(cls) -> dict:
        """返回当前策略类的总标的名额配置。"""
        enabled = bool(cls.uses_position_slot_management())
        return {
            "enabled": enabled,
            "limit": int(cls.max_positions if enabled else 0),
            "wait_reason": cls.capacity_wait_pause_reason() if enabled else "",
        }

    @classmethod
    def persistent_class_fields(cls) -> List[str]:
        """返回该策略类需要持久化的共享字段列表。"""
        return []

    def persistent_instance_fields(self) -> List[str]:
        """返回当前策略实例需要持久化的字段列表。"""
        return []

    @classmethod
    def persistent_class_state(cls) -> dict:
        """导出该策略类需要持久化的共享运行态。"""
        return cls._export_class_state_fields(cls.persistent_class_fields())

    @classmethod
    def restore_persistent_class_state(cls, state: dict) -> None:
        """恢复该策略类的共享运行态。"""
        cls._restore_class_state_fields(state, cls.persistent_class_fields())

    def persistent_instance_state(self) -> dict:
        """导出当前策略实例的自定义运行态。"""
        return self._get_custom_state()

    def restore_persistent_instance_state(self, state: dict) -> None:
        """恢复当前策略实例的自定义运行态。"""
        self._restore_custom_state(state)

    def clear_persistent_state(self) -> int:
        """清除当前策略实例在持久化层中的运行态。"""
        if not self._data_mgr:
            return 0
        return int(self._data_mgr.clear_strategy_runtime_state(self.strategy_id, strategy_type=self.strategy_name) or 0)

    def should_wait_for_position_slot(self) -> bool:
        """判断当前实例是否应参与名额竞争。"""
        return False

    def occupies_position_slot(self) -> bool:
        """判断当前实例是否已占用一个总持仓标的名额。"""
        return self._has_position_for_slot() or self._has_active_entry_order()

    def has_position_slot_available(self) -> bool:
        """判断当前策略类是否还有空余总持仓标的名额。"""
        if not self.__class__.uses_position_slot_management():
            return True
        if self.occupies_position_slot():
            return True
        self.__class__._sync_class_stats(self._position_mgr)
        return self.__class__.active_position_slot_count() < self.max_positions

    def is_waiting_for_position_slot(self) -> bool:
        """判断当前实例是否处于等待空余名额的暂停态。"""
        return (
            self.status == StrategyStatus.PAUSED
            and self.get_pause_reason() == self.__class__.capacity_wait_pause_reason()
        )

    def pause_for_position_slot(self) -> None:
        """把当前实例切到等待空余名额的暂停态。"""
        if self.is_waiting_for_position_slot():
            return
        self.pause(reason=self.__class__.capacity_wait_pause_reason())

    def reconcile_position_slot_state(self) -> None:
        """按当前类级名额占用情况收敛等待/恢复状态。"""
        if not self.__class__.uses_position_slot_management():
            return
        if not self.should_wait_for_position_slot():
            return
        if self.status == StrategyStatus.STOPPED:
            return
        if self.occupies_position_slot():
            if self.is_waiting_for_position_slot():
                self.resume()
            return
        if self.has_position_slot_available():
            if self.is_waiting_for_position_slot():
                self.resume()
            return
        self.pause_for_position_slot()

    # ------------------------------------------------------------------ 订单回调

    def on_order_update(self, order: Order) -> None:
        """处理订单状态更新回调。

        Args:
            order: 最新状态的订单对象。
        """
        try:
            # 只处理属于自己的订单回报，避免不同策略实例互相污染状态。
            if order.strategy_id != self.strategy_id:
                return
            from config.enums import OrderStatus, OrderDirection
            # 订单进入终态后，从待处理列表中移除。
            if order.order_uuid in self._pending_orders:
                if order.status in (OrderStatus.SUCCEEDED, OrderStatus.CANCELED,
                                    OrderStatus.PART_CANCEL, OrderStatus.JUNK,
                                    OrderStatus.UNKNOWN):
                    self._pending_orders.pop(order.order_uuid, None)

            if (
                order.direction == OrderDirection.SELL
                and order.status == OrderStatus.JUNK
                and "可用数量不足" in str(getattr(order, "status_msg", "") or "")
            ):
                logger.warning(
                    "Strategy[%s] 卖单被拒，检测到账户可用仓位不足，自动暂停策略 code=%s msg=%s",
                    self.strategy_id[:8],
                    self.stock_code,
                    getattr(order, "status_msg", ""),
                )
                self.pause(reason=str(getattr(order, "status_msg", "") or "账户可用数量不足"))

            if (
                order.direction == OrderDirection.BUY
                and order.status == OrderStatus.JUNK
                and self._is_insufficient_funds_message(str(getattr(order, "status_msg", "") or ""))
            ):
                self._pause_for_order_rejection(
                    str(getattr(order, "status_msg", "") or "账户资金不足，买入失败")
                )

                # 卖出订单全部成交后，如果该策略已经没有持仓，
                # 则自动将策略标记为停止状态。
            if (order.direction == OrderDirection.SELL and
                    order.status == OrderStatus.SUCCEEDED and
                    self._position_mgr):
                pos = self._position_mgr.get_position(self.strategy_id)
                if not pos or pos.total_quantity <= 0:
                    self._clear_pending_close_request()
                    self.stop()

            # 类级统计和子类扩展钩子都放在订单回报阶段更新，保证与真实成交状态一致。
            self.__class__._sync_class_stats(self._position_mgr)
            self._on_order_update_hook(order)
            if (
                order.direction == OrderDirection.BUY
                and order.status in (
                    OrderStatus.SUCCEEDED,
                    OrderStatus.CANCELED,
                    OrderStatus.PART_CANCEL,
                    OrderStatus.JUNK,
                    OrderStatus.UNKNOWN,
                )
                and not self._has_position_for_slot()
                and not self._has_active_entry_order()
            ):
                self.recover_unfilled_entry_state()
        except Exception as e:
            logger.error("Strategy[%s] on_order_update 异常: %s",
                         self.strategy_id[:8], e, exc_info=True)

    def _on_order_update_hook(self, order: Order) -> None:
        """子类可覆盖以处理订单状态变更"""
        pass

    def recover_unfilled_entry_state(self) -> None:
        """在买单终结且无持仓时，把策略收敛回可竞争状态。"""
        self.reconcile_position_slot_state()

    # ------------------------------------------------------------------ 持久化

    def get_snapshot(self) -> StrategySnapshot:
        """生成当前策略的可持久化快照。"""
        pos = None
        if self._position_mgr:
            pos = self._position_mgr.get_position(self.strategy_id)

        from position.models import PositionInfo
        # 这里把“框架层通用状态”统一封装进快照；子类特有状态再走 _get_custom_state 补充。
        return StrategySnapshot(
            strategy_id=self.strategy_id,
            strategy_name=self.strategy_name,
            stock_code=self.stock_code,
            status=self.status,
            config=self.config,
            position=pos or PositionInfo(),
            pending_order_uuids=list(self._pending_orders.keys()),
            pause_reason=self.get_pause_reason(),
            pending_close_requested=bool(self._pending_close_requested),
            pending_close_remark=str(self._pending_close_remark or ""),
            custom_state=self.persistent_instance_state(),
            create_time=self._create_time if isinstance(self._create_time, datetime) else datetime.now(),
            update_time=datetime.now(),
        )

    def prepare_for_persist(self) -> None:
        """在保存快照前执行状态收敛。

        子类可覆盖，用于在持久化前补做跨时点清理动作。
        """
        return None

    def should_persist_state(self) -> bool:
        """判断当前策略是否应进入本轮快照。"""
        return self.status != StrategyStatus.STOPPED

    def restore_from_snapshot(self, snapshot: StrategySnapshot) -> None:
        """从历史快照恢复策略状态。"""
        # 先恢复框架通用字段，再交给 position_manager 和子类去恢复各自负责的状态。
        self.strategy_id = snapshot.strategy_id
        self.stock_code = snapshot.stock_code
        self.status = snapshot.status
        self.config = snapshot.config
        self._pending_order_recovery_ids = list(snapshot.pending_order_uuids or [])
        self._pause_reason = str(getattr(snapshot, "pause_reason", "") or "")
        self._pending_close_requested = bool(getattr(snapshot, "pending_close_requested", False))
        self._pending_close_remark = str(getattr(snapshot, "pending_close_remark", "") or "")
        
        # 持仓恢复与策略对象恢复分开进行，
        # 这样持仓管理器仍然是唯一的持仓状态维护中心。
        if (
            self._position_mgr and
            snapshot.position and
            int(getattr(snapshot.position, "total_quantity", 0) or 0) > 0
        ):
            if not str(getattr(snapshot.position, "strategy_id", "") or "").strip():
                snapshot.position.strategy_id = self.strategy_id
            if not str(getattr(snapshot.position, "strategy_name", "") or "").strip():
                snapshot.position.strategy_name = self.strategy_name
            if not str(getattr(snapshot.position, "stock_code", "") or "").strip():
                snapshot.position.stock_code = self.stock_code
            # 使用 restore_position 方法，内部自动处理加锁和 T+1 解锁
            self._position_mgr.restore_position(self.strategy_id, snapshot.position)
            self.__class__._sync_class_stats(self._position_mgr)

        self.restore_persistent_instance_state(snapshot.custom_state)
        snapshot_create_time = getattr(snapshot, "create_time", None)
        if isinstance(snapshot_create_time, datetime):
            self._create_time = snapshot_create_time
        logger.info("Strategy[%s] 从快照恢复 stock=%s status=%s",
                    self.strategy_id[:8], self.stock_code, self.status.value)

    def get_pending_order_recovery_ids(self) -> List[str]:
        """返回待重建的活动订单 UUID 列表。"""
        return list(self._pending_order_recovery_ids)

    def restore_pending_orders(self, orders: List[Order]) -> None:
        """把已持久化的活动订单重新挂回当前策略实例。"""
        self._pending_orders.clear()
        restored_history_ids = {order.order_uuid for order in self._orders_history}
        for order in orders:
            if order.strategy_id != self.strategy_id or not order.is_active():
                continue
            self._pending_orders[order.order_uuid] = order
            if order.order_uuid not in restored_history_ids:
                self._orders_history.append(order)
        self._pending_order_recovery_ids = []

    def _get_custom_state(self) -> dict:
        """子类覆盖，返回需持久化的额外状态"""
        return self._export_state_fields(self.persistent_instance_fields())

    def _restore_custom_state(self, state: dict) -> None:
        """子类覆盖，恢复额外状态"""
        self._restore_state_fields(state, self.persistent_instance_fields())

    def _export_state_fields(self, fields: List[str]) -> Dict[str, Any]:
        """按字段清单导出实例运行态。"""
        state: Dict[str, Any] = {}
        for field_name in fields or []:
            if not hasattr(self, field_name):
                continue
            state[field_name] = deepcopy(getattr(self, field_name))
        return state

    def _restore_state_fields(self, state: dict, fields: List[str]) -> None:
        """按字段清单恢复实例运行态。"""
        for field_name in fields or []:
            if field_name not in state:
                continue
            setattr(self, field_name, deepcopy(state.get(field_name)))

    @classmethod
    def _export_class_state_fields(cls, fields: List[str]) -> Dict[str, Any]:
        """按字段清单导出类共享运行态。"""
        state: Dict[str, Any] = {}
        for field_name in fields or []:
            if not hasattr(cls, field_name):
                continue
            state[field_name] = deepcopy(getattr(cls, field_name))
        return state

    @classmethod
    def _restore_class_state_fields(cls, state: dict, fields: List[str]) -> None:
        """按字段清单恢复类共享运行态。"""
        for field_name in fields or []:
            if field_name not in state:
                continue
            setattr(cls, field_name, deepcopy(state.get(field_name)))

    # ------------------------------------------------------------------ Private

    def _track_order(self, order: Order) -> None:
        """把订单记录到待处理列表和历史列表。"""
        if not order:
            return
        # 活跃订单进入 pending；无论是否活跃，都保留一份历史，便于审计和排查。
        if order and order.is_active():
            self._pending_orders[order.order_uuid] = order
        self._orders_history.append(order)
        if order.status in (OrderStatus.JUNK, OrderStatus.UNKNOWN, OrderStatus.CANCELED, OrderStatus.PART_CANCEL):
            self.on_order_update(order)

    def _set_pending_close_request(self, remark: str = "") -> None:
        """登记跨交易日待平仓请求。"""
        self._pending_close_requested = True
        self._pending_close_remark = str(remark or self._pending_close_remark or "策略平仓")

    def _clear_pending_close_request(self) -> None:
        """清除已完成的待平仓请求。"""
        self._pending_close_requested = False
        self._pending_close_remark = ""

    def _process_pending_close_request(self) -> bool:
        """若存在跨交易日待平仓请求，则优先尝试继续卖出当前可用仓位。"""
        if not self._pending_close_requested:
            return False
        if not self._position_mgr:
            return True

        position = self._position_mgr.get_position(self.strategy_id)
        total_quantity = int(getattr(position, "total_quantity", 0) or 0)
        available_quantity = int(getattr(position, "available_quantity", 0) or 0)

        if total_quantity <= 0:
            self._clear_pending_close_request()
            self.stop()
            return True

        if available_quantity <= 0:
            return True

        logger.info(
            "Strategy[%s] 检测到待平仓请求，继续卖出当前可用仓位 code=%s total=%d available=%d reason=%s",
            self.strategy_id[:8],
            self.stock_code,
            total_quantity,
            available_quantity,
            self._pending_close_remark or "策略平仓",
        )
        self.close_position(remark=self._pending_close_remark or "策略平仓")
        return True

    def _has_position_for_slot(self) -> bool:
        """判断当前实例是否已有真实持仓，可用于名额占用统计。"""
        if not self._position_mgr:
            return False
        position = self._position_mgr.get_position(self.strategy_id)
        return bool(position and int(getattr(position, "total_quantity", 0) or 0) > 0)

    def _has_active_entry_order(self) -> bool:
        """判断当前实例是否存在尚未终结的买入单。"""
        return any(
            order.direction == OrderDirection.BUY and order.is_active()
            for order in self._pending_orders.values()
        )

    def _register_instance(self) -> None:
        """把当前实例注册到策略类级别的活动实例表。"""
        self.__class__._ensure_instance_registry()
        with self.__class__._instance_lock:
            self.__class__._live_instances[self.strategy_id] = self

    @classmethod
    def _ensure_instance_registry(cls) -> None:
        """确保每个具体策略子类都有独立的活动实例表。"""
        if "_instance_lock" not in cls.__dict__:
            cls._instance_lock = threading.Lock()
        if "_live_instances" not in cls.__dict__:
            cls._live_instances = weakref.WeakValueDictionary()

    @classmethod
    def active_position_slot_count(cls) -> int:
        """统计当前策略类已经占用的总持仓标的名额数。"""
        cls._ensure_instance_registry()
        with cls._instance_lock:
            live_instances = list(cls._live_instances.values())

        active_strategy_ids = set()
        for instance in live_instances:
            if instance.status == StrategyStatus.STOPPED:
                continue
            if instance.occupies_position_slot():
                active_strategy_ids.add(instance.strategy_id)
        return len(active_strategy_ids)

    @classmethod
    def _sync_class_stats(cls, position_manager=None) -> None:
        """同步类级别统计信息。

        这些统计量是“按策略类聚合”的，而不是按单个实例聚合，
        便于子类实现全局仓位数和总资金占用约束。
        """
        if not position_manager:
            return
        try:
            with cls._lock:
                positions = position_manager.get_all_positions()
                # 统计该策略类的所有实例的持仓。
                # 这里按 strategy_name 聚合，所以同一个策略类的不同实例会被合并统计。
                class_positions = [p for p in positions.values() 
                                 if p.strategy_name == cls.strategy_name]
                cls._current_positions_count = len(class_positions)
                cls._class_used_amount = sum(p.market_value for p in class_positions)
                cls.current_positions = cls._current_positions_count
                cls.current_used_amount = cls._class_used_amount
                logger.debug(f"Strategy[{cls.strategy_name}] 类属性同步: "
                           f"当前持仓数={cls._current_positions_count}, "
                           f"已用金额={cls._class_used_amount:.2f}")
        except Exception as e:
            logger.warning(f"Strategy[{cls.strategy_name}] 同步类属性失败: {e}")

    def __repr__(self) -> str:
        """返回便于调试的对象描述字符串。"""
        return (f"<{self.strategy_name}[{self.strategy_id[:8]}] "
                f"stock={self.stock_code} status={self.status.value}>")


__all__ = ["BaseStrategy"]
