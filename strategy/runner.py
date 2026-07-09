"""Strategy runner orchestration.

This module coordinates market data, strategy instances, order state, position state, persistence, and account synchronization. Strategy-specific decision logic stays inside each strategy class."""
import json
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional, Type

from config.enums import AlertLevel, OrderDirection, OrderStatus, OrderType, StrategyStatus
from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
from core.trading_calendar import is_market_day, minus_one_market_day
from position.manager import PositionManager
from position.models import FifoLot, PositionInfo
from strategy.base import BaseStrategy
from strategy.models import StrategyConfig, StrategySnapshot
from trading.models import Order, TradeRecord
from monitor.logger import get_logger

logger = get_logger("system")


def _apply_selection_runtime_overrides(overrides: dict | None) -> None:
    if not overrides:
        return
    from config.settings import settings as global_runtime_settings

    for name, value in dict(overrides).items():
        if not isinstance(name, str) or not name.isupper():
            continue
        if hasattr(global_runtime_settings, name):
            setattr(global_runtime_settings, name, value)


def _select_configs_in_subprocess(strategy_class, runtime_overrides: dict | None = None):
    """Run stock selection in an isolated subprocess and return strategy configs."""
    _apply_selection_runtime_overrides(runtime_overrides)
    strategy = strategy_class(StrategyConfig(), None, None)
    return strategy.select_stocks()


class StrategyRunner:
    """Coordinates strategy lifecycle, subscriptions, persistence, and account synchronization."""

    def __init__(self, data_subscription=None, trade_executor=None,
                 order_manager=None,
                 position_manager=None, data_manager=None,
                 connection_manager=None,
                 strategy_classes: List[Type[BaseStrategy]] = None,
                 load_previous_state_on_start: bool = True,
                 state_autosave_interval_sec: int = 300,
                 state_realtime_persist_min_interval_sec: float = 3.0,
                 latency_threshold_sec: float = 10.0,
                 process_threshold_ms: float = 200.0,
                 selection_runtime_overrides: dict | None = None):
        """Initialize the strategy runner and its shared runtime dependencies."""
        # Receives market data and forwards it into the runner.
        self._data_sub = data_subscription
        # Translates strategy signals into order actions.
        self._trade_exec = trade_executor
        # Reloads active orders from persistence during restart.
        self._order_mgr = order_manager
        # Maintains strategy positions.
        self._position_mgr = position_manager
        # Persists and restores strategy snapshots.
        self._data_mgr = data_manager

        self._connection_mgr = connection_manager
        # Registered strategy classes available for selection and recovery.
        self._strategy_classes = strategy_classes or []
        # Controls fallback loading from the previous trading day.
        self._load_previous_state_on_start = load_previous_state_on_start
        # Periodic state autosave interval.
        self._state_autosave_interval_sec = max(0, int(state_autosave_interval_sec or 0))
        # Minimum interval for event-driven state persistence.
        self._state_realtime_persist_min_interval_sec = max(0.0, float(state_realtime_persist_min_interval_sec or 0.0))

        self._strategies: List[BaseStrategy] = []
        # Protects the strategy list in multi-threaded callbacks.
        self._lock = threading.Lock()
        # Market data latency warning threshold in seconds.
        self._latency_threshold = latency_threshold_sec
        # Per-strategy processing warning threshold in milliseconds.
        self._process_threshold_ms = process_threshold_ms
        # Last total processing duration for one market-data round.
        self._last_round_total_process_ms = 0.0
        self._last_strategy_event_time: Optional[datetime] = None
        self._last_strategy_event: str = ""
        # Whether the runner is currently active.
        self._running = False
        # APScheduler instance for selection and state persistence jobs.
        self._scheduler = None
        # Serializes event-driven snapshot writes.
        self._state_save_lock = threading.RLock()
        # Last successful state-save monotonic timestamp.
        self._last_state_save_monotonic = 0.0

        self._scheduler_thread = None
        # Reports main loop activity to the watchdog.
        self._heartbeat_callback = None
        # Sends startup preflight warnings.
        self._alert_callback = None
        # Caches processed trade ids to avoid duplicate replay.
        self._known_trade_ids: Optional[set[str]] = None
        self._selection_runtime_overrides = dict(selection_runtime_overrides or {})

    def set_heartbeat_callback(self, callback) -> None:
        """Register the heartbeat callback used by the watchdog."""
        self._heartbeat_callback = callback

    def set_alert_callback(self, callback) -> None:
        """Register the alert callback used for startup and safety warnings."""
        self._alert_callback = callback

    # ------------------------------------------------------------------ Lifecycle

    def start(self) -> None:
        """Start the strategy runner."""
        self._running = True
        logger.info("StrategyRunner: 启动")

        # Load persisted state first.
        self._load_state()


        # add_strategy de-duplicates by instance_key, preserving restored instances.
        self.run_stock_selection()


        # Avoid carrying stale pending-order assumptions across abnormal restarts.
        self._restore_pending_orders_from_storage()
        self._cleanup_orphaned_pending_orders_from_storage()

        # Verify account cash and positions before live monitoring starts.
        # This detects obvious divergence between strategy state and account state.
        self._validate_account_constraints()
        self.sync_orders_and_trades_once(reason="startup")

        # Register market-data callbacks.
        if self._data_sub:
            self._data_sub.set_data_callback(self.on_market_data)
            self._data_sub.set_l2_quote_callback(self.on_l2_quote_data)
            self._data_sub.set_l2_transaction_callback(self.on_l2_transaction_data)
            self._data_sub.set_l2_order_callback(self.on_l2_order_data)
            self._data_sub.set_l2_orderqueue_callback(self.on_l2_orderqueue_data)

        # Start scheduled jobs.
        self._start_scheduler()

        # Activate strategies only on trading days.
        self._activate_for_trading_day(reason="startup")

        logger.info("StrategyRunner: 已启动 %d 个策略", len(self._strategies))
        self.request_state_persist("runner_started")

    def stop(self) -> None:
        """Stop the runner and persist current strategy state."""
        self._running = False
        self.save_state()
        with self._lock:
            for s in self._strategies:
                if s.status == StrategyStatus.RUNNING:
                    s.pause()
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
        logger.info("StrategyRunner: 已停止")

    # ------------------------------------------------------------------ Data Dispatch

    def on_market_data(self, tick_data: Dict[str, TickData]) -> None:
        """Process the latest batch of tick data."""
        if not self._running:
            return
        try:
            if tick_data:
                self._mark_strategy_event(f"tick:{len(tick_data)}")
            if self._heartbeat_callback:
                self._heartbeat_callback("strategy_runner")

            if self._position_mgr and tick_data:
                first_tick = next(iter(tick_data.values()), None)
                if first_tick and getattr(first_tick, "data_time", None):
                    self._position_mgr.unlock_available_quantities(first_tick.data_time.strftime("%Y%m%d"))

            # Run centralized latency checks before strategy-specific processing.
            for code, tick in tick_data.items():
                if tick.latency_ms > self._latency_threshold * 1000:
                    print(f"[WARNING] 数据延迟 {tick.latency_ms/1000:.1f}s > "
                          f"{self._latency_threshold}s [{code}]")

            with self._lock:
                strategies = list(self._strategies)

            round_total_elapsed_ms = 0.0
            for strategy in strategies:
                code = strategy.stock_code
                tick = tick_data.get(code)
                if not tick:
                    continue
                if strategy.status == StrategyStatus.INITIALIZING:
                    strategy.start()
                t0 = time.perf_counter()
                try:
                    strategy.before_process_tick(tick)
                    strategy.process_tick(tick)
                except Exception as e:
                    logger.error("StrategyRunner: Strategy[%s] 处理异常: %s",
                                 strategy.strategy_id[:8], e, exc_info=True)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                round_total_elapsed_ms += elapsed_ms
                if elapsed_ms > self._process_threshold_ms:
                    logger.warning(
                        "StrategyRunner: Strategy[%s] 处理耗时 %.1fms 超过阈值 %.1fms",
                        strategy.strategy_id[:8], elapsed_ms, self._process_threshold_ms
                    )
                else:
                    logger.debug("StrategyRunner: Strategy[%s] 耗时 %.1fms",
                                 strategy.strategy_id[:8], elapsed_ms)

            self._last_round_total_process_ms = round_total_elapsed_ms

            # Remove stopped strategies after each market-data round.
            # This prevents the strategy list from growing indefinitely.
            self._cleanup_stopped()

        except Exception as e:
            logger.error("StrategyRunner: on_market_data 异常: %s", e, exc_info=True)

    # ------------------------------------------------------------------ Strategy Management

    def on_l2_quote_data(self, events_by_code: Dict[str, L2QuoteEvent]) -> None:
        """Dispatch Level2 quote events to matching strategies."""
        if events_by_code:
            self._mark_strategy_event(f"l2quote:{len(events_by_code)}")
        self._dispatch_l2_single(events_by_code, "on_l2_quote")
        self._sync_subscriptions()

    def on_l2_transaction_data(self, events_by_code: Dict[str, List[L2TransactionEvent]]) -> None:
        """Dispatch Level2 transaction events to matching strategies."""
        event_count = sum(len(events or []) for events in events_by_code.values())
        if event_count:
            self._mark_strategy_event(f"l2transaction:{event_count}")
        self._dispatch_l2_batch(events_by_code, "on_l2_transaction")

    def on_l2_order_data(self, events_by_code: Dict[str, List[L2OrderEvent]]) -> None:
        """Dispatch Level2 order events to matching strategies."""
        event_count = sum(len(events or []) for events in events_by_code.values())
        if event_count:
            self._mark_strategy_event(f"l2order:{event_count}")
        self._dispatch_l2_batch(events_by_code, "on_l2_order")

    def on_l2_orderqueue_data(self, events_by_code: Dict[str, L2OrderQueueEvent]) -> None:
        """Dispatch Level2 order queue events to matching strategies."""
        if events_by_code:
            self._mark_strategy_event(f"l2orderqueue:{len(events_by_code)}")
        self._dispatch_l2_single(events_by_code, "on_l2_orderqueue")

    def _dispatch_l2_single(self, events_by_code: Dict[str, object], handler_name: str) -> None:
        if not self._running:
            return

        if self._heartbeat_callback:
            self._heartbeat_callback("strategy_runner")

        with self._lock:
            strategies = list(self._strategies)

        for strategy in strategies:
            event = events_by_code.get(strategy.stock_code)
            if event is None:
                continue
            try:
                getattr(strategy, handler_name)(event)
            except Exception as e:
                logger.error(
                    "StrategyRunner: Strategy[%s] %s failed: %s",
                    strategy.strategy_id[:8],
                    handler_name,
                    e,
                    exc_info=True,
                )

    def _dispatch_l2_batch(self, events_by_code: Dict[str, List[object]], handler_name: str) -> None:
        if not self._running:
            return

        if self._heartbeat_callback:
            self._heartbeat_callback("strategy_runner")

        with self._lock:
            strategies = list(self._strategies)

        for strategy in strategies:
            events = events_by_code.get(strategy.stock_code) or []
            for event in events:
                try:
                    getattr(strategy, handler_name)(event)
                except Exception as e:
                    logger.error(
                        "StrategyRunner: Strategy[%s] %s failed: %s",
                        strategy.strategy_id[:8],
                        handler_name,
                        e,
                        exc_info=True,
                    )

    @staticmethod
    def _strategy_instance_key(strategy: BaseStrategy) -> tuple[str, str]:
        """Return the unique key used to de-duplicate strategy instances."""
        params = getattr(strategy.config, "params", {}) or {}
        instance_key = str(params.get("instance_key") or strategy.stock_code)
        return strategy.strategy_name, instance_key

    def get_last_round_total_process_ms(self) -> float:
        """Return the last market-data processing duration in milliseconds."""
        return float(self._last_round_total_process_ms or 0.0)

    def get_runtime_status(self) -> dict:
        """Return a compact runtime status snapshot."""
        with self._lock:
            strategy_count = len(self._strategies)
            last_event_time = self._last_strategy_event_time
            last_event = self._last_strategy_event
        return {
            "strategy_count": strategy_count,
            "last_strategy_event_time": last_event_time,
            "last_strategy_event": last_event,
            "last_round_total_process_ms": float(self._last_round_total_process_ms or 0.0),
        }

    def add_strategy(self, strategy: BaseStrategy) -> None:
        """Add a strategy instance to the runner."""
        strategy.bind_persistence(self._data_mgr, self.request_state_persist)
        strategy_key = self._strategy_instance_key(strategy)
        with self._lock:
            exists = next(
                (
                    s for s in self._strategies
                    if self._strategy_instance_key(s) == strategy_key
                    and s.status != StrategyStatus.STOPPED
                ),
                None,
            )
            if exists:
                logger.info(
                    "StrategyRunner: 跳过重复策略 %s stock=%s key=%s",
                    strategy.strategy_name,
                    strategy.stock_code,
                    strategy_key[1],
                )
                return

            self._strategies.append(strategy)

        is_trading_day = self._running and self.is_trading_day()
        if is_trading_day:
            self._prepare_strategy_for_trading_day(strategy)
            if strategy.status == StrategyStatus.INITIALIZING:
                strategy.start()

        logger.info("StrategyRunner: 添加策略 %s stock=%s",
                    strategy.strategy_name, strategy.stock_code)
        with self._lock:
            should_subscribe = is_trading_day

        # Subscribe market data for this strategy.
        if self._data_sub and is_trading_day:
            self._sync_subscriptions()
        if self._running:
            self.request_state_persist(f"add_strategy:{strategy.strategy_id}")

    def remove_strategy(self, strategy_id: str) -> None:
        """Remove a strategy instance by strategy id."""
        with self._lock:
            self._strategies = [s for s in self._strategies
                                 if s.strategy_id != strategy_id]
        logger.info("StrategyRunner: 移除策略 %s", strategy_id[:8])

        if self._data_sub and self._running and self.is_trading_day():
            self._sync_subscriptions()

    def get_strategy(self, strategy_id: str) -> Optional[BaseStrategy]:
        """Return a strategy instance by strategy id."""
        with self._lock:
            for s in self._strategies:
                if s.strategy_id == strategy_id:
                    return s
        return None

    def get_all_strategies(self) -> List[BaseStrategy]:
        """Return a copy of all current strategy instances."""
        with self._lock:
            return list(self._strategies)

    def get_paused_strategy_reconciliation(self) -> List[dict]:
        """Return reconciliation details for paused strategies."""
        account_position_map = self._build_account_position_map()
        rows: List[dict] = []

        with self._lock:
            strategies = list(self._strategies)

        for strategy in strategies:
            if strategy.status != StrategyStatus.PAUSED:
                continue

            position = self._position_mgr.get_position(strategy.strategy_id) if self._position_mgr else None
            account_position = account_position_map.get(strategy.stock_code, {})
            rows.append({
                "strategy_id": strategy.strategy_id,
                "strategy_name": strategy.strategy_name,
                "stock_code": strategy.stock_code,
                "pause_reason": strategy.get_pause_reason(),
                "strategy_total_quantity": int(getattr(position, "total_quantity", 0) or 0),
                "strategy_sellable_base_quantity": int(getattr(position, "sellable_base_quantity", getattr(position, "available_quantity", 0)) or 0),
                "strategy_available_quantity": int(getattr(position, "available_quantity", 0) or 0),
                "account_total_quantity": int(account_position.get("volume", 0) or 0),
                "account_available_quantity": int(account_position.get("can_use_volume", 0) or 0),
            })

        rows.sort(key=lambda item: (item["stock_code"], item["strategy_name"], item["strategy_id"]))
        return rows

    # ------------------------------------------------------------------ Stock Selection

    def run_stock_selection(self) -> None:
        """Run stock selection and create strategy instances from the returned configs."""
        if not self.is_trading_day():
            logger.info("StrategyRunner: 今日非交易日，跳过选股")
            return

        for cls in self._strategy_classes:
            try:
                configs: List[StrategyConfig] = []
                selection_mode = "subprocess"
                try:
                    with ProcessPoolExecutor(max_workers=1) as pool:
                        configs = pool.submit(
                            _select_configs_in_subprocess,
                            cls,
                            self._selection_runtime_overrides,
                        ).result(timeout=30)
                except Exception as e:
                    logger.warning("StrategyRunner: 子进程选股失败，降级为主进程执行 [%s]: %s",
                                   cls.__name__, e)
                    selection_mode = "main_process_fallback"
                    _apply_selection_runtime_overrides(self._selection_runtime_overrides)
                    configs = cls(
                        StrategyConfig(),
                        self._trade_exec,
                        self._position_mgr
                    ).select_stocks()

                csv_paths = sorted(
                    {
                        str((cfg.params or {}).get("csv_path") or "")
                        for cfg in configs
                        if str((cfg.params or {}).get("csv_path") or "")
                    }
                )
                stock_codes = [str(cfg.stock_code or "") for cfg in configs if str(cfg.stock_code or "")]
                logger.info(
                    "STRATEGY_SELECTION strategy=%s mode=%s total=%d csv_path=%s stocks=%s",
                    cls.__name__,
                    selection_mode,
                    len(configs),
                    "|".join(csv_paths),
                    ",".join(stock_codes),
                )

                for cfg in configs:
                    strategy = cls(cfg, self._trade_exec, self._position_mgr)
                    self.add_strategy(strategy)

            except Exception as e:
                logger.error("StrategyRunner: 选股异常 [%s]: %s",
                             cls.__name__, e, exc_info=True)

        self._activate_for_trading_day(reason="stock_selection")

    # ------------------------------------------------------------------ Persistence

    def save_state(self) -> None:
        """Persist snapshots for all strategies."""
        if not self._data_mgr:
            return
        with self._state_save_lock:
            try:
                with self._lock:
                    for strategy in self._strategies:
                        prepare_for_persist = getattr(strategy, "prepare_for_persist", None)
                        if callable(prepare_for_persist):
                            prepare_for_persist()
                    snapshots = [
                        s.get_snapshot() for s in self._strategies
                        if bool(getattr(s, "should_persist_state", lambda: s.status != StrategyStatus.STOPPED)())
                    ]

                strategy_classes = {type(s) for s in self._strategies}
                strategy_classes.update(self._strategy_classes or [])
                class_states = []
                for cls in strategy_classes:
                    export_state = getattr(cls, "persistent_class_state", None)
                    if not callable(export_state):
                        continue
                    state = export_state() or {}
                    if not state:
                        continue
                    class_states.append({
                        "strategy_type": str(getattr(cls, "strategy_name", cls.__name__) or cls.__name__),
                        "state_version": int(getattr(cls, "state_version", 1) or 1),
                        "state": state,
                    })

                self._data_mgr.save_strategy_runtime_states(snapshots, class_states)
                self._last_state_save_monotonic = time.monotonic()
            except Exception as e:
                logger.error("StrategyRunner: 保存状态失败: %s", e, exc_info=True)

    def rebuild_runtime_state(self) -> dict:
        """Clear SQLite runtime state and rebuild it from current in-memory strategies."""
        if not self._data_mgr:
            return {"removed": 0, "persisted": 0}

        with self._lock:
            persisted = sum(
                1
                for strategy in self._strategies
                if bool(getattr(strategy, "should_persist_state", lambda: strategy.status != StrategyStatus.STOPPED)())
            )

        removed = int(self._data_mgr.clear_all_strategy_runtime_states() or 0)
        self.save_state()
        return {"removed": removed, "persisted": persisted}

    def request_state_persist(self, reason: str = "", min_interval_sec: float = 0.0) -> None:
        """Persist strategy snapshots after important runtime events."""
        if not self._data_mgr:
            return
        interval_limit = max(0.0, float(min_interval_sec or 0.0))
        if interval_limit > 0 and self._last_state_save_monotonic > 0:
            elapsed = time.monotonic() - self._last_state_save_monotonic
            if elapsed < interval_limit:
                return
        if reason:
            logger.debug("StrategyRunner: 触发实时持久化 [%s]", reason)
        self.save_state()

    def _load_state(self) -> bool:
        """Load historical strategy state from storage."""
        if not self._data_mgr:
            return False
        runtime_bundle = self._data_mgr.load_strategy_runtime_states(
            fallback_previous_market_day=self._load_previous_state_on_start,
        )
        snapshots = []
        if runtime_bundle:
            for class_state in runtime_bundle.get("class_states", []) or []:
                cls = self._find_strategy_class(str(class_state.get("strategy_type", "") or ""))
                if not cls:
                    logger.warning(
                        "StrategyRunner: 未找到策略类 %s，跳过共享状态恢复",
                        class_state.get("strategy_type", ""),
                    )
                    continue
                restore_class_state = getattr(cls, "restore_persistent_class_state", None)
                if callable(restore_class_state):
                    restore_class_state(dict(class_state.get("state") or {}))
            snapshots = list(runtime_bundle.get("instance_states", []) or [])

        if not snapshots:
            snapshots = self._data_mgr.load_strategy_state(
                fallback_previous_market_day=self._load_previous_state_on_start,
            )
        if not snapshots:
            return False
        with self._lock:
            self._strategies.clear()
        for snap in snapshots:
            if snap.status == StrategyStatus.STOPPED:
                continue
            cls = self._find_strategy_class(snap.strategy_name)
            if not cls:
                logger.warning(
                    "StrategyRunner: 未找到策略类 %s，跳过恢复",
                    snap.strategy_name,
                )
                continue
            strategy = cls(snap.config, self._trade_exec, self._position_mgr)
            strategy.bind_persistence(self._data_mgr, self.request_state_persist)
            strategy.restore_from_snapshot(snap)
            self._restore_position_from_trades_if_available(strategy)
            self._restore_position_from_storage_if_needed(strategy, snap)
            with self._lock:
                self._strategies.append(strategy)

        loaded_trade_day = str(getattr(self._data_mgr, "_last_loaded_state_day", "") or "")
        current_trade_day = datetime.now().strftime("%Y%m%d")
        if not is_market_day(current_trade_day):
            current_trade_day = minus_one_market_day(current_trade_day)
        if self._position_mgr and loaded_trade_day:
            if loaded_trade_day == current_trade_day:
                # On same-day restart, snapshot available_quantity already reflects current state.
                # Do not run the new-trading-day unlock again on the first tick.
                self._position_mgr.mark_trade_day_processed(current_trade_day)
            else:
                self._position_mgr.unlock_available_quantities(current_trade_day)

        logger.info("StrategyRunner: 从快照恢复 %d 个策略", len(self._strategies))
        return len(self._strategies) > 0

    @staticmethod
    def _has_open_position(position: Optional[PositionInfo]) -> bool:
        """Return whether the position object represents a non-zero holding."""
        return bool(position and int(getattr(position, "total_quantity", 0) or 0) > 0)

    def _restore_position_from_storage_if_needed(self, strategy: BaseStrategy, snapshot: StrategySnapshot) -> None:
        """Restore position from SQLite when the strategy snapshot has no position."""
        if not self._position_mgr or not self._data_mgr:
            return
        live_position = self._position_mgr.get_position(strategy.strategy_id)
        if self._has_open_position(live_position):
            return
        snapshot_position = getattr(snapshot, "position", None)
        if self._has_open_position(snapshot_position):
            return

        rows = self._data_mgr.query_positions(strategy_id=strategy.strategy_id, include_closed=True)
        if not rows:
            return

        position = self._position_from_storage_row(rows[0])
        if not self._has_open_position(position):
            return

        self._position_mgr.restore_position(strategy.strategy_id, position)
        logger.info(
            "StrategyRunner: Strategy[%s] 使用 SQLite 持仓兜底恢复 qty=%d price=%.3f",
            strategy.strategy_id[:8],
            position.total_quantity,
            position.current_price,
        )

    def _restore_position_from_trades_if_available(self, strategy: BaseStrategy) -> None:
        """Rebuild position and sellable quantity by replaying strategy trades."""
        if not self._position_mgr or not self._data_mgr:
            return

        rows = self._dedupe_trade_rows(self._data_mgr.query_trades(strategy_id=strategy.strategy_id))
        if not rows:
            return

        rebuilt = self._rebuild_position_from_trade_rows(rows)
        if not rebuilt:
            return

        rebuilt.strategy_id = strategy.strategy_id
        rebuilt.strategy_name = strategy.strategy_name
        rebuilt.stock_code = strategy.stock_code
        self._position_mgr.restore_position(strategy.strategy_id, rebuilt)
        logger.info(
            "StrategyRunner: Strategy[%s] 使用成交回放恢复持仓 qty=%d available=%d",
            strategy.strategy_id[:8],
            rebuilt.total_quantity,
            rebuilt.available_quantity,
        )

    def _rebuild_position_from_trade_rows(self, rows: List[dict]) -> Optional[PositionInfo]:
        """Replay one strategy trade history into position state."""
        rows = self._dedupe_trade_rows(rows)
        if not rows:
            return None

        cost_method = getattr(getattr(self._position_mgr, "_cost_method", None), "value", "moving_average")
        fee_schedule = getattr(self._position_mgr, "_fee_schedule", None)
        temp_mgr = PositionManager(cost_method=cost_method, fee_schedule=fee_schedule)

        current_day = ""
        sorted_rows = sorted(
            rows,
            key=lambda row: (
                self._trade_day_from_row(row),
                int(row.get("traded_time", 0) or 0),
                str(row.get("trade_id", "") or ""),
            ),
        )

        strategy_id = str(sorted_rows[0].get("strategy_id", "") or "")
        for row in sorted_rows:
            trade_day = self._trade_day_from_row(row)
            if trade_day and trade_day != current_day:
                temp_mgr.unlock_available_quantities(trade_day)
                current_day = trade_day
            temp_mgr.on_trade_callback(self._trade_from_storage_row(row))

        rebuilt = temp_mgr.get_position(strategy_id)
        if rebuilt and current_day:
            PositionManager.normalize_restored_position(rebuilt, source_trade_day=current_day)
        return rebuilt

    @staticmethod
    def _dedupe_trade_rows(rows: List[dict]) -> List[dict]:
        """De-duplicate trade rows by trade id."""
        deduped: List[dict] = []
        seen_trade_ids: set[str] = set()
        for row in rows or []:
            trade_id = str(row.get("trade_id", "") or row.get("traded_id", "") or "").strip()
            if trade_id:
                if trade_id in seen_trade_ids:
                    continue
                seen_trade_ids.add(trade_id)
            deduped.append(row)
        return deduped

    @staticmethod
    def _trade_day_from_row(row: dict) -> str:
        """Extract and normalize the trade date as YYYYMMDD."""
        for field in ("traded_time", "trade_time"):
            digits = "".join(ch for ch in str(row.get(field, "") or "") if ch.isdigit())
            if len(digits) < 8:
                continue
            if len(digits) in (10, 13):
                try:
                    ts = int(digits)
                    if len(digits) == 13:
                        ts = ts / 1000
                    return datetime.fromtimestamp(ts).strftime("%Y%m%d")
                except (TypeError, ValueError, OSError):
                    continue
            trade_day = digits[:8]
            if trade_day.startswith(("19", "20")):
                return trade_day
        return ""

    @staticmethod
    def _trade_from_storage_row(row: dict) -> TradeRecord:
        """Convert a SQLite trade row into a TradeRecord."""
        direction = OrderDirection(str(row.get("direction", OrderDirection.BUY.value) or OrderDirection.BUY.value))
        trade_time = StrategyRunner._parse_db_datetime(row.get("trade_time"))
        return TradeRecord(
            account_type=int(row.get("account_type", 0) or 0),
            account_id=str(row.get("account_id", "") or ""),
            order_type=int(row.get("order_type", 0) or 0),
            trade_id=str(row.get("trade_id", "") or ""),
            xt_traded_time=int(row.get("traded_time", 0) or 0),
            order_uuid=str(row.get("order_uuid", "") or ""),
            xt_order_id=int(row.get("xt_order_id", 0) or 0),
            order_sysid=str(row.get("order_sysid", "") or ""),
            strategy_id=str(row.get("strategy_id", "") or ""),
            strategy_name=str(row.get("strategy_name", "") or ""),
            order_remark=str(row.get("order_remark", "") or ""),
            stock_code=str(row.get("stock_code", "") or ""),
            direction=direction,
            xt_direction=int(row.get("xt_direction", 0) or 0),
            offset_flag=int(row.get("offset_flag", 0) or 0),
            price=float(row.get("price", 0.0) or 0.0),
            quantity=int(row.get("quantity", 0) or 0),
            amount=float(row.get("amount", 0.0) or 0.0),
            commission=float(row.get("commission", 0.0) or 0.0),
            buy_commission=float(row.get("buy_commission", 0.0) or 0.0),
            sell_commission=float(row.get("sell_commission", 0.0) or 0.0),
            stamp_tax=float(row.get("stamp_tax", 0.0) or 0.0),
            total_fee=float(row.get("total_fee", row.get("commission", 0.0)) or 0.0),
            is_t0=bool(row.get("is_t0", 0)),
            secu_account=str(row.get("secu_account", "") or ""),
            instrument_name=str(row.get("instrument_name", "") or ""),
            trade_time=trade_time,
        )

    @staticmethod
    def _position_from_storage_row(row: dict) -> PositionInfo:
        """Convert a SQLite position row into a PositionInfo."""
        fifo_lots = []
        raw_fifo = str(row.get("fifo_lots_json", "") or "").strip()
        if raw_fifo:
            try:
                for lot in json.loads(raw_fifo):
                    buy_time_text = str(lot.get("buy_time", "") or "").strip()
                    if buy_time_text:
                        try:
                            buy_time = datetime.fromisoformat(buy_time_text.replace("Z", "+00:00"))
                        except ValueError:
                            buy_time = datetime.now()
                    else:
                        buy_time = datetime.now()
                    fifo_lots.append(FifoLot(
                        quantity=int(lot.get("quantity", 0) or 0),
                        cost_price=float(lot.get("cost_price", 0.0) or 0.0),
                        buy_time=buy_time,
                    ))
            except Exception:
                fifo_lots = []

        update_time_text = str(row.get("update_time", "") or "").strip()
        try:
            update_time = datetime.fromisoformat(update_time_text.replace(" ", "T")) if update_time_text else datetime.now()
        except ValueError:
            update_time = datetime.now()

        return PositionInfo(
            strategy_id=str(row.get("strategy_id", "") or ""),
            strategy_name=str(row.get("strategy_name", "") or ""),
            stock_code=str(row.get("stock_code", "") or ""),
            total_quantity=int(row.get("total_quantity", 0) or 0),
            sellable_base_quantity=int(row.get("sellable_base_quantity", row.get("available_quantity", 0)) or 0),
            available_quantity=int(row.get("available_quantity", 0) or 0),
            is_t0=bool(row.get("is_t0", 0)),
            avg_cost=float(row.get("avg_cost", 0.0) or 0.0),
            total_cost=float(row.get("total_cost", 0.0) or 0.0),
            current_price=float(row.get("current_price", 0.0) or 0.0),
            market_value=float(row.get("market_value", 0.0) or 0.0),
            unrealized_pnl=float(row.get("unrealized_pnl", 0.0) or 0.0),
            unrealized_pnl_ratio=float(row.get("unrealized_pnl_ratio", 0.0) or 0.0),
            realized_pnl=float(row.get("realized_pnl", 0.0) or 0.0),
            total_commission=float(row.get("total_commission", 0.0) or 0.0),
            total_buy_commission=float(row.get("total_buy_commission", 0.0) or 0.0),
            total_sell_commission=float(row.get("total_sell_commission", 0.0) or 0.0),
            total_stamp_tax=float(row.get("total_stamp_tax", 0.0) or 0.0),
            total_fees=float(row.get("total_fees", 0.0) or 0.0),
            fifo_lots=fifo_lots,
            update_time=update_time,
        )

    def _find_strategy_class(self, strategy_name: str) -> Optional[Type[BaseStrategy]]:
        """Find a registered strategy class by name."""
        for cls in self._strategy_classes:
            if cls.strategy_name == strategy_name:
                return cls
        return None

    # ------------------------------------------------------------------ Scheduler

    def _start_scheduler(self) -> None:
        """Start the APScheduler jobs for selection and state persistence."""
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
            from apscheduler.executors.pool import ProcessPoolExecutor as APSProcessPoolExecutor

            executors = {
                "default": {"type": "threadpool", "max_workers": 10},
                "processpool": APSProcessPoolExecutor(max_workers=2),
            }
            self._scheduler = BlockingScheduler(executors=executors)
            # Refresh today's strategies before market open and activate them.
            self._scheduler.add_job(self.run_stock_selection, "cron",
                                    hour=9, minute=25, id="stock_selection")
            # Persist state after market close.
            self._scheduler.add_job(self.save_state, "cron",
                                    hour=15, minute=5, id="save_state")
            if self._state_autosave_interval_sec > 0:
                self._scheduler.add_job(self._autosave_state, "interval",
                                        seconds=self._state_autosave_interval_sec,
                                        id="autosave_state")
            self._scheduler.add_job(self._sync_orders_and_trades_job, "interval",
                                    seconds=30, id="sync_orders_and_trades")
            # Clean stopped strategies every 30 minutes.
            self._scheduler.add_job(self._cleanup_stopped, "interval",
                                    minutes=30, id="cleanup")
            self._scheduler_thread = threading.Thread(
                target=self._scheduler.start,
                daemon=True,
                name="strategy-scheduler"
            )
            self._scheduler_thread.start()
            logger.info("StrategyRunner: APScheduler 已启动")
        except ImportError:
            logger.warning("StrategyRunner: apscheduler 未安装，跳过定时任务")
        except Exception as e:
            logger.error("StrategyRunner: 调度器启动失败: %s", e, exc_info=True)

    def _autosave_state(self) -> None:
        """Periodically persist intraday state to reduce restart risk."""
        if not self._running or not self.is_trading_day():
            return
        self.save_state()

    def is_trading_time(self) -> bool:
        """Return whether the current time is inside the intraday trading window."""
        now = datetime.now()
        if not self.is_trading_day(now):
            return False
        t = now.strftime("%H:%M")
        return (("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00"))

    def is_trading_day(self, when=None) -> bool:
        """Return whether the given date is a trading day."""
        target = when or datetime.now()
        return is_market_day(target)

    def _activate_for_trading_day(self, reason: str = "") -> bool:
        """Activate strategies and subscriptions for a trading day."""
        if not self.is_trading_day():
            logger.info("StrategyRunner: 今日非交易日，跳过策略激活 [%s]", reason or "unknown")
            return False

        self._prepare_all_strategies_for_trading_day(reason=reason)
        self._subscribe_all()

        started = 0
        with self._lock:
            for strategy in self._strategies:
                if strategy.status == StrategyStatus.INITIALIZING:
                    strategy.start()
                    started += 1

        logger.info(
            "StrategyRunner: 交易日激活完成 [%s]，新增启动 %d 个策略",
            reason or "unknown",
            started,
        )
        return True

    def _prepare_all_strategies_for_trading_day(self, reason: str = "") -> None:
        """Run all per-strategy pre-trading-day initialization before subscription sync."""
        trade_day = datetime.now().strftime("%Y%m%d")
        with self._lock:
            strategies = list(self._strategies)

        prepared = 0
        failed = 0
        for strategy in strategies:
            if self._prepare_strategy_for_trading_day(strategy, trade_day=trade_day):
                prepared += 1
            else:
                failed += 1

        logger.info(
            "StrategyRunner: 交易日前初始化完成 [%s]，成功 %d，失败 %d",
            reason or "unknown",
            prepared,
            failed,
        )

    def _prepare_strategy_for_trading_day(self, strategy: BaseStrategy, trade_day: str = "") -> bool:
        """Run pre-trading-day initialization for one strategy."""
        target_trade_day = trade_day or datetime.now().strftime("%Y%m%d")
        try:
            return bool(strategy.prepare_for_trading_day(target_trade_day))
        except Exception as exc:
            logger.error(
                "StrategyRunner: Strategy[%s] 交易日前初始化失败: %s",
                strategy.strategy_id[:8],
                exc,
                exc_info=True,
            )
            return False

    def _subscribe_all(self) -> None:
        self._sync_subscriptions()
        return

    def _sync_subscriptions(self) -> None:
        """Synchronize market data subscriptions with current strategies."""
        if not self._data_sub:
            return

        tick_codes, l2_plan = self._build_subscription_plan()
        current_tick_codes = set(self._data_sub.get_subscription_list())
        current_l2_map = {code: set(kinds) for code, kinds in self._data_sub.get_l2_subscription_map().items()}

        desired_tick_codes = set(tick_codes)
        add_tick_codes = sorted(desired_tick_codes - current_tick_codes)
        remove_tick_codes = sorted(current_tick_codes - desired_tick_codes)

        if add_tick_codes:
            self._data_sub.subscribe_stocks(add_tick_codes)
        if remove_tick_codes:
            self._data_sub.unsubscribe_stocks(remove_tick_codes)

        desired_l2_codes = set(l2_plan)
        for code in sorted(desired_l2_codes | set(current_l2_map)):
            desired_kinds = set(l2_plan.get(code, set()))
            current_kinds = set(current_l2_map.get(code, set()))
            add_kinds = sorted(desired_kinds - current_kinds)
            remove_kinds = sorted(current_kinds - desired_kinds)
            if add_kinds:
                self._data_sub.subscribe_l2_stocks([code], kinds=add_kinds)
            if remove_kinds:
                self._data_sub.unsubscribe_l2_stocks([code], kinds=remove_kinds)

    def _build_subscription_plan(self) -> tuple[List[str], Dict[str, set[str]]]:
        tick_codes: set[str] = set()
        l2_plan: Dict[str, set[str]] = {}

        with self._lock:
            strategies = list(self._strategies)

        for strategy in strategies:
            kinds = self._normalize_strategy_data_kinds(strategy)
            if "tick" in kinds:
                tick_codes.add(strategy.stock_code)
            l2_kinds = {kind for kind in kinds if kind != "tick"}
            if l2_kinds:
                l2_plan.setdefault(strategy.stock_code, set()).update(l2_kinds)

        return sorted(tick_codes), l2_plan

    @staticmethod
    def _normalize_strategy_data_kinds(strategy: BaseStrategy) -> set[str]:
        current_data_kinds = getattr(strategy, "current_data_kinds", None)
        if callable(current_data_kinds):
            raw_kinds = current_data_kinds()
        else:
            raw_kinds = getattr(strategy.__class__, "required_data_kinds", lambda: {"tick"})()
        normalized = {str(kind or "").strip().lower() for kind in (raw_kinds or {"tick"})}
        normalized.discard("")
        allowed = getattr(strategy.__class__, "_supported_data_kinds", {"tick"})
        valid = normalized & set(allowed)
        return valid or {"tick"}

    def _restore_pending_orders_from_storage(self) -> int:
        """Rebuild active orders recorded in snapshots from SQLite."""
        if not self._data_mgr or not self._order_mgr:
            return 0

        with self._lock:
            strategies = list(self._strategies)

        pending_order_ids = sorted({
            order_uuid
            for strategy in strategies
            for order_uuid in strategy.get_pending_order_recovery_ids()
            if order_uuid
        })
        if not pending_order_ids:
            return 0

        rows = self._data_mgr.query_orders(order_uuids=pending_order_ids)
        if not rows:
            logger.warning(
                "StrategyRunner: 快照记录了 %d 个活动订单，但数据库未找到对应记录",
                len(pending_order_ids),
            )
            return 0

        restored_orders: Dict[str, Order] = {}
        active_statuses = {
            OrderStatus.UNREPORTED.value,
            OrderStatus.WAIT_REPORTING.value,
            OrderStatus.REPORTED.value,
            OrderStatus.REPORTED_CANCEL.value,
            OrderStatus.PARTSUCC_CANCEL.value,
            OrderStatus.PART_SUCC.value,
        }
        for row in rows:
            if str(row.get("status", "") or "") not in active_statuses:
                continue
            order = self._deserialize_order_row(row)
            if order.is_active():
                restored_orders[order.order_uuid] = order

        if not restored_orders:
            return 0

        self._order_mgr.restore_orders(list(restored_orders.values()))

        restored_count = 0
        for strategy in strategies:
            orders = [
                restored_orders[order_uuid]
                for order_uuid in strategy.get_pending_order_recovery_ids()
                if order_uuid in restored_orders
            ]
            if not orders:
                continue
            strategy.restore_pending_orders(orders)
            restored_count += len(orders)

        if restored_count > 0:
            logger.info("StrategyRunner: 已从持久化订单恢复 %d 个活动订单", restored_count)
        return restored_count

    def _cleanup_orphaned_pending_orders_from_storage(self) -> int:
        """Clean local pending orders that are not owned by any active strategy."""
        if not self._data_mgr:
            return 0

        active_statuses = {
            OrderStatus.UNREPORTED.value,
            OrderStatus.WAIT_REPORTING.value,
            OrderStatus.REPORTED.value,
            OrderStatus.REPORTED_CANCEL.value,
            OrderStatus.PARTSUCC_CANCEL.value,
            OrderStatus.PART_SUCC.value,
        }
        with self._lock:
            live_strategy_ids = {strategy.strategy_id for strategy in self._strategies}

        cleaned = 0
        for row in self._data_mgr.query_orders() or []:
            if str(row.get("status", "") or "") not in active_statuses:
                continue

            strategy_id = str(row.get("strategy_id", "") or "")
            if strategy_id in live_strategy_ids:
                continue

            xt_order_id = int(row.get("xt_order_id", 0) or 0)
            filled_quantity = int(row.get("filled_quantity", 0) or 0)
            if xt_order_id > 0 or filled_quantity > 0:
                continue

            order = self._deserialize_order_row(row)
            order.status = OrderStatus.CANCELED
            order.status_msg = "startup cleanup orphan pending order without live strategy"
            self._data_mgr.save_order(order)
            cleaned += 1

        if cleaned > 0:
            logger.info(
                "StrategyRunner: 已清理 %d 个未被活动策略接管的本地待报挂单",
                cleaned,
            )
        return cleaned

    @staticmethod
    def _deserialize_order_row(row: dict) -> Order:
        """Deserialize a SQLite order row into an internal Order object."""
        return Order(
            order_uuid=str(row.get("order_uuid", "") or ""),
            order_trace_id=str(row.get("order_trace_id", "") or ""),
            strategy_id=str(row.get("strategy_id", "") or ""),
            strategy_name=str(row.get("strategy_name", "") or ""),
            stock_code=str(row.get("stock_code", "") or ""),
            direction=OrderDirection(str(row.get("direction", OrderDirection.BUY.value) or OrderDirection.BUY.value)),
            order_type=OrderType(str(row.get("order_type", OrderType.LIMIT.value) or OrderType.LIMIT.value)),
            price=float(row.get("price", 0.0) or 0.0),
            quantity=int(row.get("quantity", 0) or 0),
            amount=float(row.get("amount", 0.0) or 0.0),
            status=OrderStatus(str(row.get("status", OrderStatus.UNKNOWN.value) or OrderStatus.UNKNOWN.value)),
            filled_quantity=int(row.get("filled_quantity", 0) or 0),
            filled_amount=float(row.get("filled_amount", 0.0) or 0.0),
            filled_avg_price=float(row.get("filled_avg_price", 0.0) or 0.0),
            xt_order_id=int(row.get("xt_order_id", 0) or 0),
            account_type=int(row.get("account_type", 0) or 0),
            account_id=str(row.get("account_id", "") or ""),
            xt_stock_code=str(row.get("xt_stock_code", "") or ""),
            order_sysid=str(row.get("order_sysid", "") or ""),
            order_time=int(row.get("order_time", 0) or 0),
            xt_order_type=int(row.get("xt_order_type", 0) or 0),
            price_type=int(row.get("price_type", 0) or 0),
            xt_order_status=int(row.get("xt_order_status", 0) or 0),
            status_msg=str(row.get("status_msg", "") or ""),
            xt_direction=int(row.get("xt_direction", 0) or 0),
            offset_flag=int(row.get("offset_flag", 0) or 0),
            secu_account=str(row.get("secu_account", "") or ""),
            instrument_name=str(row.get("instrument_name", "") or ""),
            xt_fields=dict(StrategyRunner._safe_json_loads(str(row.get("xt_order_snapshot", "") or ""))),
            remark=str(row.get("remark", "") or ""),
            commission=float(row.get("commission", 0.0) or 0.0),
            buy_commission=float(row.get("buy_commission", 0.0) or 0.0),
            sell_commission=float(row.get("sell_commission", 0.0) or 0.0),
            stamp_tax=float(row.get("stamp_tax", 0.0) or 0.0),
            total_fee=float(row.get("total_fee", 0.0) or 0.0),
            create_time=StrategyRunner._parse_db_datetime(row.get("create_time")),
            update_time=StrategyRunner._parse_db_datetime(row.get("update_time")),
        )

    @staticmethod
    def _parse_db_datetime(value) -> datetime:
        """Parse a SQLite timestamp into datetime."""
        text = str(value or "").strip()
        if not text:
            return datetime.now()
        for candidate in (text, text.replace(" ", "T")):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
        return datetime.now()

    @staticmethod
    def _safe_json_loads(raw: str) -> dict:
        """Safely parse order snapshot JSON."""
        import json

        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _cleanup_stopped(self) -> None:
        """Remove stopped strategies without positions and archive their realized PnL."""
        removed_ids = []
        with self._lock:
            remaining = []
            for strategy in self._strategies:
                if strategy.status == StrategyStatus.STOPPED:
                    removed_ids.append(strategy.strategy_id)
                else:
                    remaining.append(strategy)
            self._strategies = remaining

        if removed_ids and self._position_mgr:
            for strategy_id in removed_ids:
                try:
                    self._position_mgr.remove_position(strategy_id)
                except Exception as e:
                    logger.error(
                        "StrategyRunner: 清理策略持仓失败 [%s]: %s",
                        strategy_id[:8],
                        e,
                        exc_info=True,
                    )

        removed = len(removed_ids)
        if removed:
            logger.info("StrategyRunner: 清理并归档 %d 个已停止策略", removed)
            self.request_state_persist("cleanup_stopped")

    def dispatch_order_update(self, order) -> None:
        """Dispatch order updates to the owning strategy."""
        strategy = self.get_strategy(order.strategy_id)
        if strategy:
            self._mark_strategy_event(
                f"order_update:{getattr(order, 'stock_code', '')}:{getattr(order, 'status', '')}"
            )
            strategy.on_order_update(order)
            self._sync_subscriptions()

    def _mark_strategy_event(self, event: str) -> None:
        with self._lock:
            self._last_strategy_event_time = datetime.now()
            self._last_strategy_event = str(event or "")

    def _sync_orders_and_trades_job(self) -> None:
        """Run active account synchronization only during trading time."""
        if not self._running or not self.is_trading_time():
            return
        self.sync_orders_and_trades_once(reason="scheduler")

    def sync_orders_and_trades_once(self, reason: str = "manual") -> Dict[str, int]:
        """Fetch account orders and trades and reconcile local state."""
        summary = {
            "trades_synced": 0,
            "orders_synced": 0,
            "state_recovered": 0,
        }
        if not self._connection_mgr or not self._connection_mgr.is_connected() or not self._order_mgr:
            return summary

        try:
            queried_trades = self._connection_mgr.query_stock_trades()
            summary["trades_synced"] = self._sync_trades_from_account(queried_trades)
        except Exception as exc:
            logger.warning("StrategyRunner: 主动同步成交失败 reason=%s err=%s", reason, exc)

        try:
            queried_orders = self._connection_mgr.query_stock_orders(cancelable_only=False)
            sync_result = self._sync_orders_from_account(queried_orders)
            summary["orders_synced"] = sync_result["orders_synced"]
            summary["state_recovered"] = sync_result["state_recovered"]
        except Exception as exc:
            logger.warning("StrategyRunner: 主动同步委托失败 reason=%s err=%s", reason, exc)

        if any(summary.values()):
            logger.info(
                "StrategyRunner: 主动同步完成 reason=%s trades=%d orders=%d recovered=%d",
                reason,
                summary["trades_synced"],
                summary["orders_synced"],
                summary["state_recovered"],
            )
            self.request_state_persist(f"sync_orders_and_trades:{reason}")
        return summary

    def cancel_entry_orders_and_recover(self, strategy_id: str, remark: str = "") -> Dict[str, object]:
        """Cancel unfinished entry orders and restore fair competition when safe."""
        strategy = self.get_strategy(strategy_id)
        if not strategy:
            return {"success": False, "message": "策略不存在"}

        self.sync_orders_and_trades_once(reason=f"manual_release_precheck:{strategy_id}")

        position = self._position_mgr.get_position(strategy_id) if self._position_mgr else None
        total_quantity = int(getattr(position, "total_quantity", 0) or 0)
        if total_quantity > 0:
            return {"success": False, "message": "策略仍有持仓，不能恢复为未开仓竞争状态"}

        active_buy_orders = [
            order for order in self._order_mgr.get_orders_by_strategy(strategy_id)
            if order.direction == OrderDirection.BUY and order.is_active()
        ]
        if not active_buy_orders:
            recovered = 1 if self._recover_strategy_after_entry_release(strategy) else 0
            return {
                "success": True,
                "message": "当前无活动买单，已收敛策略状态",
                "submitted": 0,
                "forced": 0,
                "recovered": recovered,
            }

        if any(int(getattr(order, "filled_quantity", 0) or 0) > 0 for order in active_buy_orders):
            return {"success": False, "message": "存在已成交买单，不能直接恢复为未开仓状态"}

        submitted = 0
        forced = 0
        for order in active_buy_orders:
            if int(getattr(order, "xt_order_id", 0) or 0) > 0 and self._trade_exec:
                canceled = bool(self._trade_exec.cancel_order(order.order_uuid, remark=remark or "人工撤销建仓单并释放名额"))
                if canceled:
                    submitted += 1
                continue

            updated = self._order_mgr.mark_order_status(
                order.order_uuid,
                OrderStatus.CANCELED,
                status_msg=remark or "人工清理未被柜台接收的建仓单",
            )
            if updated:
                forced += 1

        recovered = 0

        if submitted == 0 and forced == 0:
            return {"success": False, "message": "没有成功撤销任何活动买单"}

        if submitted > 0 and forced == 0:
            message = f"已提交 {submitted} 笔撤单请求，待柜台回报后自动释放名额"
        elif submitted == 0:
            message = f"已本地清理 {forced} 笔未入柜台买单，并恢复策略竞争状态"
        else:
            message = f"已提交 {submitted} 笔撤单，并本地清理 {forced} 笔未入柜台买单"

        self.request_state_persist(f"manual_release_entry:{strategy_id}")
        return {
            "success": True,
            "message": message,
            "submitted": submitted,
            "forced": forced,
            "recovered": recovered,
        }

    def _build_account_position_map(self) -> Dict[str, Dict[str, int]]:
        """Query and normalize account positions."""
        if not self._connection_mgr or not self._connection_mgr.is_connected():
            return {}

        try:
            account_positions = self._connection_mgr.query_stock_positions()
        except Exception as exc:
            logger.warning("StrategyRunner: 查询账户持仓失败，暂停对账视图并返回空结果: %s", exc)
            return {}

        account_position_map: Dict[str, Dict[str, int]] = {}
        for account_position in account_positions:
            code = self._xt_to_code(str(getattr(account_position, "stock_code", "") or ""))
            volume = int(getattr(account_position, "volume", 0) or 0)
            can_use_volume = int(getattr(account_position, "can_use_volume", 0) or 0)
            on_road_volume = int(getattr(account_position, "on_road_volume", 0) or 0)
            yesterday_volume = int(getattr(account_position, "yesterday_volume", 0) or 0)
            account_position_map[code] = {
                "volume": volume,
                "can_use_volume": can_use_volume,
                "on_road_volume": on_road_volume,
                "yesterday_volume": yesterday_volume,
                "total_with_on_road": max(volume, yesterday_volume + on_road_volume),
            }
        return account_position_map

    def _validate_account_constraints(self) -> None:
        """Check account cash and position constraints before strategies run."""
        if not self._connection_mgr or not self._connection_mgr.is_connected():
            logger.info("StrategyRunner: 启动前账户校验已跳过，交易连接未就绪")
            return

        account_asset = self._connection_mgr.query_stock_asset()
        account_position_map = self._build_account_position_map()

        if account_asset is None:
            self._warn_preflight("[启动前校验] 无法获取账户资产信息，已跳过资金上限核验")
            return

        available_cash = float(getattr(account_asset, "cash", 0.0) or 0.0)
        total_asset = float(getattr(account_asset, "total_asset", 0.0) or 0.0)

        self._sync_position_availability_with_account(account_position_map)

        with self._lock:
            strategies = list(self._strategies)

        for strategy in strategies:
            class_budget_limit = float(getattr(strategy, "max_total_amount", 0.0) or 0.0)
            config_budget_limit = float(getattr(strategy.config, "max_position_amount", 0.0) or 0.0)

            if class_budget_limit > 0 and class_budget_limit > available_cash:
                self._warn_preflight(
                    f"[启动前校验] 策略 {strategy.strategy_name}[{strategy.strategy_id[:8]}] "
                    f"类级最大资金 {class_budget_limit:.2f} 超过账户可用资金 {available_cash:.2f} "
                    f"(总资产 {total_asset:.2f})"
                )

            if config_budget_limit > 0 and config_budget_limit > available_cash:
                self._warn_preflight(
                    f"[启动前校验] 策略 {strategy.strategy_name}[{strategy.strategy_id[:8]}] "
                    f"标的最大资金 {config_budget_limit:.2f} 超过账户可用资金 {available_cash:.2f} "
                    f"(标的 {strategy.stock_code})"
                )

        if not self._position_mgr:
            return

        strategy_position_map: Dict[str, Dict[str, object]] = {}
        for position in self._position_mgr.get_all_positions().values():
            info = strategy_position_map.setdefault(position.stock_code, {
                "total_quantity": 0,
                "available_quantity": 0,
                "strategy_names": set(),
            })
            info["total_quantity"] = int(info["total_quantity"]) + int(position.total_quantity or 0)
            info["available_quantity"] = int(info["available_quantity"]) + int(position.available_quantity or 0)
            cast_names = info["strategy_names"]
            if isinstance(cast_names, set):
                cast_names.add(position.strategy_name)

        for stock_code, info in strategy_position_map.items():
            strategy_total = int(info.get("total_quantity", 0) or 0)
            strategy_available = int(info.get("available_quantity", 0) or 0)
            strategy_names = ",".join(sorted(info.get("strategy_names", set()) or []))
            account_position = account_position_map.get(stock_code)
            account_volume = int((account_position or {}).get("total_with_on_road", 0) or 0)
            account_available = int((account_position or {}).get("can_use_volume", 0) or 0)

            if strategy_total <= 0 and strategy_available <= 0:
                continue

            if not account_position:
                self._warn_preflight(
                    f"[启动前校验] 策略持仓显示 {stock_code} 共 {strategy_total} 股，"
                    f"但账户中未查询到该标的持仓（策略: {strategy_names or '-'}）"
                )
                self._pause_strategies_for_stock(stock_code, "账户未查询到对应持仓")
                continue

            if strategy_total > account_volume:
                self._warn_preflight(
                    f"[启动前校验] 策略持仓 {stock_code} 共 {strategy_total} 股，"
                    f"超过账户实际持仓 {account_volume} 股（策略: {strategy_names or '-'}）"
                )
                self._pause_strategies_for_stock(stock_code, "策略持仓超过账户实际持仓")

            if strategy_available > account_available:
                self._warn_preflight(
                    f"[启动前校验] 策略可用持仓 {stock_code} 共 {strategy_available} 股，"
                    f"超过账户实际可用持仓 {account_available} 股（策略: {strategy_names or '-'}）"
                )
                self._pause_strategies_for_stock(stock_code, "策略可用持仓超过账户实际可用持仓")

    def _sync_position_availability_with_account(self, account_position_map: Dict[str, Dict[str, int]]) -> None:
        """Clamp strategy sellable quantity to account sellable holdings."""
        if not self._position_mgr:
            return

        grouped_positions: Dict[str, List[PositionInfo]] = {}
        for position in self._position_mgr.get_all_positions().values():
            if not self._position_mgr._is_managed_position(position):
                continue
            if int(position.total_quantity or 0) <= 0:
                continue
            grouped_positions.setdefault(position.stock_code, []).append(position)

        changed = False
        for stock_code, positions in grouped_positions.items():
            account_position = account_position_map.get(stock_code)
            if not account_position:
                continue
            allocations = self._allocate_strategy_available_quantities(
                positions,
                int(account_position.get("can_use_volume", 0) or 0),
            )
            for position in positions:
                assigned_available = allocations.get(position.strategy_id, 0)
                changed = self._position_mgr.sync_available_quantity(position.strategy_id, assigned_available) or changed

        if changed:
            logger.info("StrategyRunner: 已按账户可用持仓同步策略可卖数量")
            self.request_state_persist("sync_available_with_account")

    @staticmethod
    def _allocate_strategy_available_quantities(
        positions: List[PositionInfo],
        account_available: int,
    ) -> Dict[str, int]:
        """Allocate account sellable quantity across strategies for the same stock."""
        valid_positions = [pos for pos in positions if int(getattr(pos, "total_quantity", 0) or 0) > 0]
        if not valid_positions:
            return {}

        current_available_map = {
            str(pos.strategy_id or ""): min(
                max(0, int(getattr(pos, "sellable_base_quantity", getattr(pos, "available_quantity", 0)) or 0)),
                max(0, int(getattr(pos, "total_quantity", 0) or 0)),
            )
            for pos in valid_positions
        }
        distributable = max(0, int(account_available or 0))
        strategy_available_total = sum(current_available_map.values())

        if strategy_available_total <= distributable:
            return current_available_map
        if distributable <= 0:
            return {str(pos.strategy_id): 0 for pos in valid_positions}

        allocations: Dict[str, int] = {}
        remainders: List[tuple[float, str]] = []
        assigned_total = 0
        for pos in valid_positions:
            strategy_id = str(pos.strategy_id or "")
            position_available = current_available_map.get(strategy_id, 0)
            raw_share = distributable * position_available / strategy_available_total
            assigned = min(position_available, int(raw_share))
            allocations[strategy_id] = assigned
            assigned_total += assigned
            remainders.append((raw_share - int(raw_share), strategy_id))

        leftover = distributable - assigned_total
        for _, strategy_id in sorted(remainders, reverse=True):
            if leftover <= 0:
                break
            position = next((pos for pos in valid_positions if str(pos.strategy_id or "") == strategy_id), None)
            if not position:
                continue
            max_allowed = current_available_map.get(strategy_id, 0)
            if allocations[strategy_id] >= max_allowed:
                continue
            allocations[strategy_id] += 1
            leftover -= 1

        return allocations

    def _pause_strategies_for_stock(self, stock_code: str, reason: str) -> None:
        """Pause strategies when account position constraints are not satisfied."""
        paused_ids = []
        with self._lock:
            for strategy in self._strategies:
                if strategy.stock_code != stock_code:
                    continue
                if strategy.status == StrategyStatus.STOPPED:
                    continue
                strategy.pause(reason=reason)
                paused_ids.append(strategy.strategy_id[:8])
        if paused_ids:
            logger.warning(
                "StrategyRunner: 因账户仓位校验失败暂停 %s 的 %d 个策略实例 [%s]",
                stock_code,
                len(paused_ids),
                reason,
            )

    def _sync_trades_from_account(self, queried_trades: List[object]) -> int:
        """Backfill account trades into the internal order and position chain."""
        if not queried_trades:
            return 0

        recorded_trade_ids = self._get_known_trade_ids()

        synced = 0
        for trade in queried_trades:
            trade_id = str(getattr(trade, "traded_id", "") or getattr(trade, "trade_id", "") or "")
            if not trade_id or trade_id in recorded_trade_ids:
                continue

            xt_order_id = int(getattr(trade, "order_id", 0) or 0)
            trade_info = {
                "account_type": int(getattr(trade, "account_type", 0) or 0),
                "account_id": str(getattr(trade, "account_id", "") or ""),
                "strategy_id": str(getattr(trade, "strategy_id", "") or ""),
                "stock_code": self._xt_to_code(str(getattr(trade, "stock_code", "") or "")),
                "order_type": int(getattr(trade, "order_type", 0) or 0),
                "traded_id": trade_id,
                "traded_time": int(getattr(trade, "traded_time", 0) or 0),
                "traded_price": float(getattr(trade, "traded_price", 0) or 0.0),
                "traded_volume": int(getattr(trade, "traded_volume", 0) or 0),
                "traded_amount": float(getattr(trade, "traded_amount", 0) or 0.0),
                "order_id": xt_order_id,
                "order_sysid": str(getattr(trade, "order_sysid", "") or ""),
                "strategy_name": str(getattr(trade, "strategy_name", "") or ""),
                "order_remark": str(getattr(trade, "order_remark", "") or ""),
                "direction": int(getattr(trade, "direction", 0) or 0),
                "offset_flag": int(getattr(trade, "offset_flag", 0) or 0),
                "commission": float(getattr(trade, "commission", 0.0) or 0.0),
                "secu_account": str(getattr(trade, "secu_account", "") or ""),
                "instrument_name": str(getattr(trade, "instrument_name", "") or ""),
                "xt_fields": self._extract_public_attrs(trade),
            }
            trade_info.update({
                "trade_id": trade_id,
                "xt_order_id": xt_order_id,
                "price": trade_info["traded_price"],
                "quantity": trade_info["traded_volume"],
                "amount": trade_info["traded_amount"],
            })
            self._order_mgr.on_trade(xt_order_id, trade_info)
            recorded_trade_ids.add(trade_id)
            synced += 1
        return synced

    def _get_known_trade_ids(self) -> set[str]:
        """Return known trade ids and warm the cache from SQLite on first use."""
        if self._known_trade_ids is None:
            known_trade_ids: set[str] = set()
            if self._data_mgr:
                known_trade_ids = {
                    str(row.get("trade_id", "") or "")
                    for row in self._data_mgr.query_trades()
                    if str(row.get("trade_id", "") or "")
                }
            self._known_trade_ids = known_trade_ids
        return self._known_trade_ids

    def _sync_orders_from_account(self, queried_orders: List[object]) -> Dict[str, int]:
        """Write account order query results back into local order state."""
        summary = {"orders_synced": 0, "state_recovered": 0}
        if not queried_orders:
            return summary

        seen_xt_order_ids: set[int] = set()
        seen_trace_ids: set[str] = set()

        for queried_order in queried_orders:
            xt_order_id = int(getattr(queried_order, "order_id", 0) or 0)
            if xt_order_id <= 0:
                continue

            seen_xt_order_ids.add(xt_order_id)
            order_trace_id = str(getattr(queried_order, "order_remark", "") or "").strip()
            if order_trace_id:
                seen_trace_ids.add(order_trace_id)

            local_order = self._order_mgr.get_order_by_xt_id(xt_order_id)
            if not local_order:
                local_order = self._order_mgr.get_order_by_trace_id(order_trace_id)
            if not local_order:
                continue

            next_status = self._map_xt_order_status(getattr(queried_order, "order_status", 0))
            filled_qty = int(getattr(queried_order, "traded_volume", 0) or 0)
            filled_amount = float(getattr(queried_order, "traded_amount", 0) or 0.0)
            avg_price = float(getattr(queried_order, "traded_price", 0) or 0.0)
            changed = (
                local_order.status != next_status
                or int(getattr(local_order, "filled_quantity", 0) or 0) != filled_qty
                or abs(float(getattr(local_order, "filled_amount", 0.0) or 0.0) - filled_amount) > 1e-6
                or abs(float(getattr(local_order, "filled_avg_price", 0.0) or 0.0) - avg_price) > 1e-6
            )
            if not changed:
                continue

            before_terminal = local_order.status in (
                OrderStatus.SUCCEEDED,
                OrderStatus.CANCELED,
                OrderStatus.PART_CANCEL,
                OrderStatus.JUNK,
                OrderStatus.UNKNOWN,
            )
            self._order_mgr.update_order_status(
                xt_order_id=xt_order_id,
                status=next_status,
                filled_qty=filled_qty,
                filled_amount=filled_amount,
                avg_price=avg_price,
                order_info=self._build_xt_order_payload(queried_order),
            )
            summary["orders_synced"] += 1
            if not before_terminal and next_status in (
                OrderStatus.SUCCEEDED,
                OrderStatus.CANCELED,
                OrderStatus.PART_CANCEL,
                OrderStatus.JUNK,
                OrderStatus.UNKNOWN,
            ):
                summary["state_recovered"] += 1

        for local_order in self._order_mgr.get_active_orders():
            if self._should_keep_local_active_order(local_order, seen_xt_order_ids, seen_trace_ids):
                continue

            updated_order = self._order_mgr.mark_order_status(
                local_order.order_uuid,
                self._resolve_missing_active_order_status(local_order),
                status_msg="主动同步未在柜台委托列表中找到该活动订单，已按保护规则收敛为终态",
            )
            if not updated_order:
                continue

            summary["orders_synced"] += 1
            summary["state_recovered"] += 1
            strategy = self.get_strategy(updated_order.strategy_id)
            if strategy:
                self._recover_strategy_after_entry_release(strategy)
        return summary

    @staticmethod
    def _should_keep_local_active_order(
        local_order: Order,
        seen_xt_order_ids: set[int],
        seen_trace_ids: set[str],
    ) -> bool:
        """Return whether a local active order still exists in account orders."""
        xt_order_id = int(getattr(local_order, "xt_order_id", 0) or 0)
        if xt_order_id > 0 and xt_order_id in seen_xt_order_ids:
            return True
        order_trace_id = str(getattr(local_order, "order_trace_id", "") or "").strip()
        return bool(order_trace_id and order_trace_id in seen_trace_ids)

    @staticmethod
    def _resolve_missing_active_order_status(local_order: Order) -> OrderStatus:
        """Choose a terminal status for local active orders missing from account orders."""
        if int(getattr(local_order, "filled_quantity", 0) or 0) > 0:
            return OrderStatus.PART_CANCEL
        if int(getattr(local_order, "xt_order_id", 0) or 0) > 0:
            return OrderStatus.CANCELED
        return OrderStatus.JUNK

    def _recover_strategy_after_entry_release(self, strategy: BaseStrategy) -> bool:
        """Recover strategy state after an entry order is released."""
        if not strategy or strategy.status == StrategyStatus.STOPPED:
            return False

        position = self._position_mgr.get_position(strategy.strategy_id) if self._position_mgr else None
        if position and int(getattr(position, "total_quantity", 0) or 0) > 0:
            return False

        if any(
            order.direction == OrderDirection.BUY and order.is_active()
            for order in self._order_mgr.get_orders_by_strategy(strategy.strategy_id)
        ):
            return False

        strategy.recover_unfilled_entry_state()
        return True

    def _warn_preflight(self, message: str) -> None:
        """Log and send a startup preflight warning."""
        logger.warning(message)
        if self._alert_callback:
            try:
                self._alert_callback(AlertLevel.WARNING, message)
            except Exception as exc:
                logger.error("StrategyRunner: 启动前告警发送失败: %s", exc, exc_info=True)

    @staticmethod
    def _map_xt_order_status(xt_status) -> OrderStatus:
        """Map xtquant order status into the internal status enum."""
        mapping = {
            48: OrderStatus.UNREPORTED,
            49: OrderStatus.WAIT_REPORTING,
            50: OrderStatus.REPORTED,
            51: OrderStatus.REPORTED_CANCEL,
            52: OrderStatus.PARTSUCC_CANCEL,
            53: OrderStatus.PART_CANCEL,
            54: OrderStatus.CANCELED,
            55: OrderStatus.PART_SUCC,
            56: OrderStatus.SUCCEEDED,
            57: OrderStatus.JUNK,
            255: OrderStatus.UNKNOWN,
        }
        return mapping.get(int(xt_status or 0), OrderStatus.UNKNOWN)

    @staticmethod
    def _extract_public_attrs(payload) -> Dict[str, object]:
        """Extract public object attributes for debugging and persistence."""
        data: Dict[str, object] = {}
        if payload is None:
            return data
        for attr in dir(payload):
            if attr.startswith("_"):
                continue
            try:
                value = getattr(payload, attr)
            except Exception:
                continue
            if callable(value):
                continue
            data[attr] = value
        return data

    def _build_xt_order_payload(self, order) -> Dict[str, object]:
        """Convert an XtOrder object into a normalized dict."""
        return {
            "account_type": int(getattr(order, "account_type", 0) or 0),
            "account_id": str(getattr(order, "account_id", "") or ""),
            "xt_stock_code": str(getattr(order, "stock_code", "") or ""),
            "stock_code": self._xt_to_code(str(getattr(order, "stock_code", "") or "")),
            "order_sysid": str(getattr(order, "order_sysid", "") or ""),
            "order_time": int(getattr(order, "order_time", 0) or 0),
            "order_type": int(getattr(order, "order_type", 0) or 0),
            "price_type": int(getattr(order, "price_type", 0) or 0),
            "order_status": int(getattr(order, "order_status", 0) or 0),
            "status_msg": str(getattr(order, "status_msg", "") or ""),
            "direction": int(getattr(order, "direction", 0) or 0),
            "offset_flag": int(getattr(order, "offset_flag", 0) or 0),
            "secu_account": str(getattr(order, "secu_account", "") or ""),
            "instrument_name": str(getattr(order, "instrument_name", "") or ""),
            "order_volume": int(getattr(order, "order_volume", 0) or 0),
            "price": float(getattr(order, "price", 0.0) or 0.0),
            "traded_volume": int(getattr(order, "traded_volume", 0) or 0),
            "traded_amount": float(getattr(order, "traded_amount", 0.0) or 0.0),
            "traded_price": float(getattr(order, "traded_price", 0.0) or 0.0),
            "strategy_name": str(getattr(order, "strategy_name", "") or ""),
            "order_remark": str(getattr(order, "order_remark", "") or ""),
            "xt_fields": self._extract_public_attrs(order),
        }

    @staticmethod
    def _xt_to_code(xt_code: str) -> str:
        """Convert an xtquant security code into the internal six-digit code."""
        return xt_code.split(".")[0] if "." in xt_code else xt_code


__all__ = ["StrategyRunner"]
