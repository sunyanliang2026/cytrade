"""绛栫暐杩愯妯″潡銆?

鏈ā鍧楁槸椤圭洰涓繛鎺モ€滆鎯呫€佺瓥鐣ャ€佽鍗曘€佹寔浠撱€佺姸鎬佹仮澶嶁€濈殑璋冨害涓績銆?
瀹冧笉鍏冲績鍏蜂綋绛栫暐閫昏緫鏈韩锛岃€屾槸璐熻矗璁╁涓瓥鐣ュ疄渚嬪湪缁熶竴瑙勫垯涓嬭繍琛屻€?
"""
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


def _select_configs_in_subprocess(strategy_class):
    """鍦ㄥ瓙杩涚▼涓墽琛岄€夎偂閫昏緫骞惰繑鍥為厤缃垪琛ㄣ€?

    杩欐牱鍋氱殑涓昏鐩殑鏄妸娼滃湪鑰楁椂杈冮暱銆佷笖鍙兘渚濊禆澶栭儴璁＄畻鐨勯€夎偂閫昏緫
    涓庝富杩涚▼闅旂寮€锛岄檷浣庨樆濉炰富娴佺▼鐨勯闄┿€?
    """
    strategy = strategy_class(StrategyConfig(), None, None)
    return strategy.select_stocks()


class StrategyRunner:
    """绛栫暐杩愯绠＄悊鍣ㄣ€?

    瀹冭礋璐ｇ粺涓€璋冨害鎵€鏈夌瓥鐣ュ璞★紝鏄瓥鐣ュ眰鐨勬€绘帶涓績銆?
    """

    def __init__(self, data_subscription=None, trade_executor=None,
                 order_manager=None,
                 position_manager=None, data_manager=None,
                 connection_manager=None,
                 strategy_classes: List[Type[BaseStrategy]] = None,
                 load_previous_state_on_start: bool = True,
                 state_autosave_interval_sec: int = 300,
                 state_realtime_persist_min_interval_sec: float = 3.0,
                 latency_threshold_sec: float = 10.0,
                 process_threshold_ms: float = 200.0):
        """鍒濆鍖栫瓥鐣ヨ繍琛屽櫒銆?

        Args:
            data_subscription: 琛屾儏璁㈤槄绠＄悊鍣ㄣ€?
            trade_executor: 浜ゆ槗鎵ц鍣ㄣ€?
            position_manager: 鎸佷粨绠＄悊鍣ㄣ€?
            data_manager: 鏁版嵁鎸佷箙鍖栫鐞嗗櫒銆?
            connection_manager: 浜ゆ槗杩炴帴绠＄悊鍣紝鐢ㄤ簬鍚姩鍓嶈处鎴锋牎楠屻€?
            strategy_classes: 闇€瑕佹墭绠＄殑绛栫暐绫诲垪琛ㄣ€?
            load_previous_state_on_start: 褰撴棩鐘舵€佷笉瀛樺湪鏃讹紝鏄惁鍥為€€鍔犺浇涓婁竴浜ゆ槗鏃ョ姸鎬併€?
            state_autosave_interval_sec: 鐩樹腑鑷姩淇濆瓨鐘舵€佺殑鍛ㄦ湡锛屽崟浣嶇锛沗`0`` 琛ㄧず鍏抽棴銆?
            state_realtime_persist_min_interval_sec: 鐩樹腑瀹炴椂鐘舵€佷繚瀛樼殑鏈€灏忛棿闅旓紝鍗曚綅绉掋€?
            latency_threshold_sec: 琛屾儏寤惰繜鍛婅闃堝€硷紝鍗曚綅绉掋€?
            process_threshold_ms: 鍗曟绛栫暐澶勭悊鑰楁椂鍛婅闃堝€硷紝鍗曚綅姣銆?
        """
        # ``_data_sub`` 璐熻矗鍚戣繍琛屽櫒鎺ㄩ€佹渶鏂拌鎯呮暟鎹€?
        self._data_sub = data_subscription
        # ``_trade_exec`` 璐熻矗鎶婄瓥鐣ヤ俊鍙风炕璇戞垚鐪熷疄涓嬪崟鍔ㄤ綔銆?
        self._trade_exec = trade_executor
        # ``_order_mgr`` 鐢ㄤ簬鍦ㄩ噸鍚椂鎶婃椿鍔ㄨ鍗曚粠鎸佷箙鍖栧眰閲嶆柊瑁呰浇鍥炲唴瀛樸€?
        self._order_mgr = order_manager
        # ``_position_mgr`` 璐熻矗鏌ヨ鍜岀淮鎶ょ瓥鐣ユ寔浠撱€?
        self._position_mgr = position_manager
        # ``_data_mgr`` 鐢ㄤ簬淇濆瓨鍜屾仮澶嶇瓥鐣ョ姸鎬佸揩鐓с€?
        self._data_mgr = data_manager
        # ``_connection_mgr`` 鐢ㄤ簬鍦ㄥ惎鍔ㄥ墠鏌ヨ璐︽埛璧勪骇涓庢寔浠撱€?
        self._connection_mgr = connection_manager
        # ``_strategy_classes`` 淇濆瓨鎵€鏈夊彲鍙備笌鑷姩閫夎偂/鎭㈠鐨勭瓥鐣ョ被銆?
        self._strategy_classes = strategy_classes or []
        # ``_load_previous_state_on_start`` 鎺у埗鏄惁鍥為€€鍒颁笂涓€浜ゆ槗鏃ョ姸鎬佹枃浠躲€?
        self._load_previous_state_on_start = load_previous_state_on_start
        # ``_state_autosave_interval_sec`` 鎺у埗鐩樹腑鐘舵€佽嚜鍔ㄤ繚瀛橀鐜囥€?
        self._state_autosave_interval_sec = max(0, int(state_autosave_interval_sec or 0))
        # ``_state_realtime_persist_min_interval_sec`` 鎺у埗绾鎯呮€佷笅鏈€灏忎繚瀛橀棿闅旓紝閬垮厤姣忎釜 tick 閮藉啓蹇収銆?
        self._state_realtime_persist_min_interval_sec = max(0.0, float(state_realtime_persist_min_interval_sec or 0.0))
        # ``_strategies`` 淇濆瓨褰撳墠姝ｅ湪鎵樼鐨勭瓥鐣ュ疄渚嬪垪琛ㄣ€?
        self._strategies: List[BaseStrategy] = []
        # ``_lock`` 淇濇姢绛栫暐鍒楄〃鍦ㄥ绾跨▼鐜涓嬬殑澧炲垹鏀规煡銆?
        self._lock = threading.Lock()
        # ``_latency_threshold`` 鏄鎯呭欢杩熷憡璀﹂槇鍊硷紝鍗曚綅绉掋€?
        self._latency_threshold = latency_threshold_sec
        # ``_process_threshold_ms`` 鏄崟娆＄瓥鐣ュ鐞嗚€楁椂闃堝€硷紝鍗曚綅姣銆?
        self._process_threshold_ms = process_threshold_ms
        # ``_last_round_total_process_ms`` 璁板綍鏈€杩戜竴杞鎯呮帹閫佸搴旂殑绛栫暐鎬诲鐞嗚€楁椂銆?
        self._last_round_total_process_ms = 0.0
        # ``_running`` 鏍囪杩愯鍣ㄦ槸鍚﹀凡杩涘叆宸ヤ綔鐘舵€併€?
        self._running = False
        # ``_scheduler`` 鏄?APScheduler 瀹炰緥锛岀敤浜庡畾鏃堕€夎偂涓庝繚瀛樼姸鎬併€?
        self._scheduler = None
        # ``_state_save_lock`` 鐢ㄤ簬涓茶鍖栦簨浠堕┍鍔ㄥ揩鐓т繚瀛橈紝閬垮厤澶氱嚎绋嬪苟鍙戝啓 pickle銆?
        self._state_save_lock = threading.RLock()
        # ``_last_state_save_monotonic`` 璁板綍鏈€杩戜竴娆℃垚鍔熶繚瀛樼殑鍗曡皟鏃堕挓鏃堕棿銆?
        self._last_state_save_monotonic = 0.0
        # ``_scheduler_thread`` 鏄皟搴﹀櫒鎵€鍦ㄧ嚎绋嬨€?
        self._scheduler_thread = None
        # ``_heartbeat_callback`` 鐢ㄤ簬鍚戠湅闂ㄧ嫍鎶ュ憡涓诲惊鐜椿璺冪姸鎬併€?
        self._heartbeat_callback = None
        # ``_alert_callback`` 鐢ㄤ簬鍙戦€佸惎鍔ㄥ墠璐︽埛鏍￠獙鍛婅銆?
        self._alert_callback = None
        # ``_known_trade_ids`` 缂撳瓨宸插鐞嗘垚浜わ紝閬垮厤涓诲姩鍚屾閲嶅鍥炴斁鍚屼竴绗旀垚浜ゃ€?
        self._known_trade_ids: Optional[set[str]] = None

    def set_heartbeat_callback(self, callback) -> None:
        """娉ㄥ唽蹇冭烦鍥炶皟锛屼緵鐪嬮棬鐙楁劅鐭ョ瓥鐣ヤ富寰幆鏄惁浠嶅湪宸ヤ綔銆?"""
        self._heartbeat_callback = callback

    def set_alert_callback(self, callback) -> None:
        """娉ㄥ唽棰勬鏌ュ憡璀﹀洖璋冦€?

        褰撳墠涓昏鐢ㄤ簬鎶婂惎鍔ㄥ墠鐨勮处鎴锋牎楠岀粨鏋滆浆鍙戝埌閽夐拤銆?
        """
        self._alert_callback = callback

    # ------------------------------------------------------------------ 鍚姩/鍋滄

    def start(self) -> None:
        """鍚姩绛栫暐杩愯鍣ㄣ€?"""
        self._running = True
        logger.info("StrategyRunner: 启动")

        # 灏濊瘯鎭㈠鐘舵€?
        self._load_state()

        # 鏃犺鏄惁鎭㈠鍑哄揩鐓э紝閮藉啀鎸夊綋鏃?CSV 琛ラ綈涓€娆＄己澶卞疄渚嬨€?
        # add_strategy 浼氭寜 instance_key 鍘婚噸锛屽洜姝や笉浼氳鐩栧凡鎭㈠鐨勫疄渚嬨€?
        self.run_stock_selection()

        # 鎶婂揩鐓ч噷璁板綍鐨勬椿鍔ㄨ鍗?UUID 閲嶆柊瑁呰浇鍥炲唴瀛橈紝
        # 閬垮厤寮傚父閲嶅惎鍚庣瓥鐣ュ繕璁拌嚜宸变粛鏈夋寕鍗曟湭瀹岀粨銆?
        self._restore_pending_orders_from_storage()
        self._cleanup_orphaned_pending_orders_from_storage()

        # 鍦ㄧ湡姝ｅ紑濮嬬洴鐩樺墠锛屽厛鏍稿璐︽埛璧勪骇鍜岃处鎴锋寔浠擄紝
        # 闃叉绛栫暐鍐呴儴鐘舵€佷笌鐪熷疄璐︽埛鐘舵€佹槑鏄句笉涓€鑷淬€?
        self._validate_account_constraints()
        self.sync_orders_and_trades_once(reason="startup")

        # 娉ㄥ唽鏁版嵁鍥炶皟
        if self._data_sub:
            self._data_sub.set_data_callback(self.on_market_data)
            self._data_sub.set_l2_quote_callback(self.on_l2_quote_data)
            self._data_sub.set_l2_transaction_callback(self.on_l2_transaction_data)
            self._data_sub.set_l2_order_callback(self.on_l2_order_data)
            self._data_sub.set_l2_orderqueue_callback(self.on_l2_orderqueue_data)

        # 鍚姩璋冨害鍣?
        self._start_scheduler()

        # 浠呭湪浜ゆ槗鏃ユ縺娲荤瓥鐣?
        self._activate_for_trading_day(reason="startup")

        logger.info("StrategyRunner: 已启动 %d 个策略", len(self._strategies))
        self.request_state_persist("runner_started")

    def stop(self) -> None:
        """鍋滄杩愯鍣紝骞朵繚瀛樺綋鍓嶇瓥鐣ョ姸鎬併€?"""
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

    # ------------------------------------------------------------------ 鏁版嵁鍒嗗彂

    def on_market_data(self, tick_data: Dict[str, TickData]) -> None:
        """澶勭悊涓€鎵规渶鏂拌鎯呮暟鎹€?

        Args:
            tick_data: 浠ヨ瘉鍒镐唬鐮佷负閿殑琛屾儏瀛楀吀銆?
        """
        if not self._running:
            return
        try:
            if self._heartbeat_callback:
                self._heartbeat_callback("strategy_runner")

            if self._position_mgr and tick_data:
                first_tick = next(iter(tick_data.values()), None)
                if first_tick and getattr(first_tick, "data_time", None):
                    self._position_mgr.unlock_available_quantities(first_tick.data_time.strftime("%Y%m%d"))

            # 鍏堝仛缁熶竴鐨勫欢杩熸娴嬶紝閬垮厤绛栫暐鍐呴儴鍚勮嚜閲嶅鍒ゆ柇銆?
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

            # 姣忚疆琛屾儏缁撴潫鍚庨『鎵嬫竻鐞嗗凡鍋滄绛栫暐锛?
            # 鍙互閬垮厤绛栫暐鍒楄〃鎸佺画鑶ㄨ儉銆?
            self._cleanup_stopped()

        except Exception as e:
            logger.error("StrategyRunner: on_market_data 异常: %s", e, exc_info=True)

    # ------------------------------------------------------------------ 绛栫暐绠＄悊

    def on_l2_quote_data(self, events_by_code: Dict[str, L2QuoteEvent]) -> None:
        """Dispatch Level2 quote events to matching strategies."""
        self._dispatch_l2_single(events_by_code, "on_l2_quote")

    def on_l2_transaction_data(self, events_by_code: Dict[str, List[L2TransactionEvent]]) -> None:
        """Dispatch Level2 transaction events to matching strategies."""
        self._dispatch_l2_batch(events_by_code, "on_l2_transaction")

    def on_l2_order_data(self, events_by_code: Dict[str, List[L2OrderEvent]]) -> None:
        """Dispatch Level2 order events to matching strategies."""
        self._dispatch_l2_batch(events_by_code, "on_l2_order")

    def on_l2_orderqueue_data(self, events_by_code: Dict[str, L2OrderQueueEvent]) -> None:
        """Dispatch Level2 order-queue events to matching strategies."""
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
        """杩斿洖鐢ㄤ簬鍒ゆ柇绛栫暐瀹炰緥鏄惁閲嶅鐨勫敮涓€閿€?"""
        params = getattr(strategy.config, "params", {}) or {}
        instance_key = str(params.get("instance_key") or strategy.stock_code)
        return strategy.strategy_name, instance_key

    def get_last_round_total_process_ms(self) -> float:
        """杩斿洖鏈€杩戜竴杞鎯呮帹閫佸搴旂殑绛栫暐鎬诲鐞嗚€楁椂锛屽崟浣嶆绉掋€?"""
        return float(self._last_round_total_process_ms or 0.0)

    def add_strategy(self, strategy: BaseStrategy) -> None:
        """鍚戣繍琛屽櫒涓坊鍔犱竴涓瓥鐣ュ疄渚嬨€?"""
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
                    "StrategyRunner: 璺宠繃閲嶅绛栫暐 %s stock=%s key=%s",
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

        # 璁㈤槄璇ユ爣鐨?
        if self._data_sub and is_trading_day:
            self._sync_subscriptions()
        if self._running:
            self.request_state_persist(f"add_strategy:{strategy.strategy_id}")

    def remove_strategy(self, strategy_id: str) -> None:
        """鎸夌瓥鐣?ID 绉婚櫎绛栫暐瀹炰緥銆?"""
        with self._lock:
            self._strategies = [s for s in self._strategies
                                 if s.strategy_id != strategy_id]
        logger.info("StrategyRunner: 移除策略 %s", strategy_id[:8])

        if self._data_sub and self._running and self.is_trading_day():
            self._sync_subscriptions()

    def get_strategy(self, strategy_id: str) -> Optional[BaseStrategy]:
        """鎸夌瓥鐣?ID 鑾峰彇绛栫暐瀵硅薄銆?"""
        with self._lock:
            for s in self._strategies:
                if s.strategy_id == strategy_id:
                    return s
        return None

    def get_all_strategies(self) -> List[BaseStrategy]:
        """杩斿洖褰撳墠鍏ㄩ儴绛栫暐瀵硅薄鐨勫壇鏈垪琛ㄣ€?"""
        with self._lock:
            return list(self._strategies)

    def get_paused_strategy_reconciliation(self) -> List[dict]:
        """杩斿洖鏆傚仠绛栫暐鐨勬寔浠撳璐﹁鍥俱€?"""
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

    # ------------------------------------------------------------------ 閫夎偂

    def run_stock_selection(self) -> None:
        """鎵ц閫夎偂锛屽苟涓烘瘡涓厤缃垱寤轰竴涓瓥鐣ュ疄渚嬨€?"""
        if not self.is_trading_day():
            logger.info("StrategyRunner: 今日非交易日，跳过选股")
            return

        for cls in self._strategy_classes:
            try:
                configs: List[StrategyConfig] = []
                try:
                    with ProcessPoolExecutor(max_workers=1) as pool:
                        configs = pool.submit(_select_configs_in_subprocess, cls).result(timeout=30)
                except Exception as e:
                    logger.warning("StrategyRunner: 子进程选股失败，降级为主进程执行 [%s]: %s",
                                   cls.__name__, e)
                    configs = cls(
                        StrategyConfig(),
                        self._trade_exec,
                        self._position_mgr
                    ).select_stocks()

                for cfg in configs:
                    strategy = cls(cfg, self._trade_exec, self._position_mgr)
                    self.add_strategy(strategy)

            except Exception as e:
                logger.error("StrategyRunner: 选股异常 [%s]: %s",
                             cls.__name__, e, exc_info=True)

        self._activate_for_trading_day(reason="stock_selection")

    # ------------------------------------------------------------------ 鎸佷箙鍖?

    def save_state(self) -> None:
        """淇濆瓨鍏ㄩ儴绛栫暐鐨勫揩鐓х姸鎬併€?"""
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
        """娓呯┖ SQLite 杩愯鎬佸苟绔嬪嵆鎸夊綋鍓嶅唴瀛樼瓥鐣ラ噸寤恒€?"""
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
        """鍦ㄥ叧閿繍琛屼簨浠跺悗绔嬪嵆淇濆瓨绛栫暐蹇収銆?"""
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
        """鍔犺浇鍘嗗彶绛栫暐鐘舵€併€?

        Returns:
            鏄惁鎴愬姛鎭㈠鍑鸿嚦灏戜竴涓瓥鐣ュ疄渚嬨€?
        """
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
                # 鍚屼竴浜ゆ槗鏃ュ唴閲嶅惎鏃讹紝蹇収涓殑 available_quantity 宸茬粡浠ｈ〃褰撴棩鐪熷疄鐘舵€侊紝
                # 涓嶅簲鍦ㄩ涓?tick 鍒版潵鏃跺啀娆℃墽琛屸€滄柊浜ゆ槗鏃ヨВ閿佲€濄€?
                self._position_mgr.mark_trade_day_processed(current_trade_day)
            else:
                self._position_mgr.unlock_available_quantities(current_trade_day)

        logger.info("StrategyRunner: 从快照恢复 %d 个策略", len(self._strategies))
        return len(self._strategies) > 0

    @staticmethod
    def _has_open_position(position: Optional[PositionInfo]) -> bool:
        """鍒ゆ柇鎸佷粨瀵硅薄鏄惁浠ｈ〃闈為浂鎸佷粨銆?"""
        return bool(position and int(getattr(position, "total_quantity", 0) or 0) > 0)

    def _restore_position_from_storage_if_needed(self, strategy: BaseStrategy, snapshot: StrategySnapshot) -> None:
        """褰撳揩鐓т腑鐨勬寔浠撲负绌烘椂锛屼娇鐢?SQLite 鎸佷粨蹇収鍏滃簳鎭㈠銆?"""
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
            "StrategyRunner: Strategy[%s] 浣跨敤 SQLite 鎸佷粨鍏滃簳鎭㈠ qty=%d price=%.3f",
            strategy.strategy_id[:8],
            position.total_quantity,
            position.current_price,
        )

    def _restore_position_from_trades_if_available(self, strategy: BaseStrategy) -> None:
        """褰撶瓥鐣ュ瓨鍦ㄦ垚浜ゅ巻鍙叉椂锛屾寜鎴愪氦鍥炴斁閲嶅缓鎸佷粨涓庡彲鍗栨暟閲忋€?"""
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
            "StrategyRunner: Strategy[%s] 浣跨敤鎴愪氦鍥炴斁鎭㈠鎸佷粨 qty=%d available=%d",
            strategy.strategy_id[:8],
            rebuilt.total_quantity,
            rebuilt.available_quantity,
        )

    def _rebuild_position_from_trade_rows(self, rows: List[dict]) -> Optional[PositionInfo]:
        """鎸夊崟绛栫暐鎴愪氦璁板綍鍥炴斁閲嶅缓鎸佷粨銆?"""
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
        """鎸?trade_id 鍘婚噸鎴愪氦璁板綍锛岄伩鍏嶉噸澶嶅洖鏀惧悓涓€绗旀垚浜ゃ€?"""
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
        """浠庢垚浜よ褰曚腑鎻愬彇浜ゆ槗鏃ワ紝缁熶竴鎴?YYYYMMDD銆?"""
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
        """鎶婃暟鎹簱鎴愪氦璁板綍鍙嶅簭鍒楀寲涓?TradeRecord銆?"""
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
        """鎶?SQLite 鎸佷粨璁板綍杞崲鎴?PositionInfo銆?"""
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
        """鏍规嵁绛栫暐鍚嶇О鎵惧埌瀵瑰簲鐨勭瓥鐣ョ被銆?"""
        for cls in self._strategy_classes:
            if cls.strategy_name == strategy_name:
                return cls
        return None

    # ------------------------------------------------------------------ 璋冨害鍣?

    def _start_scheduler(self) -> None:
        """鍚姩 APScheduler 瀹氭椂浠诲姟绾跨▼銆?"""
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
            from apscheduler.executors.pool import ProcessPoolExecutor as APSProcessPoolExecutor

            executors = {
                "default": {"type": "threadpool", "max_workers": 10},
                "processpool": APSProcessPoolExecutor(max_workers=2),
            }
            self._scheduler = BlockingScheduler(executors=executors)
            # 寮€鐩樺墠鍒锋柊褰撴棩绛栫暐骞舵縺娲?
            self._scheduler.add_job(self.run_stock_selection, "cron",
                                    hour=9, minute=25, id="stock_selection")
            # 鏀剁洏鍚庝繚瀛樼姸鎬?
            self._scheduler.add_job(self.save_state, "cron",
                                    hour=15, minute=5, id="save_state")
            if self._state_autosave_interval_sec > 0:
                self._scheduler.add_job(self._autosave_state, "interval",
                                        seconds=self._state_autosave_interval_sec,
                                        id="autosave_state")
            self._scheduler.add_job(self._sync_orders_and_trades_job, "interval",
                                    seconds=30, id="sync_orders_and_trades")
            # 姣?0鍒嗛挓娓呯悊宸插仠姝㈢瓥鐣?
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
        """鐩樹腑鍛ㄦ湡淇濆瓨鐘舵€侊紝闄嶄綆寮傚父閫€鍑哄鑷寸殑鎸佷粨涓㈠け椋庨櫓銆?"""
        if not self._running or not self.is_trading_day():
            return
        self.save_state()

    def is_trading_time(self) -> bool:
        """鍒ゆ柇褰撳墠鏄惁浣嶄簬鏃ュ唴浜ゆ槗鏃舵銆?"""
        now = datetime.now()
        if not self.is_trading_day(now):
            return False
        t = now.strftime("%H:%M")
        return (("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00"))

    def is_trading_day(self, when=None) -> bool:
        """鍒ゆ柇鎸囧畾鏃ユ湡鏄惁涓轰氦鏄撴棩銆?"""
        target = when or datetime.now()
        return is_market_day(target)

    def _activate_for_trading_day(self, reason: str = "") -> bool:
        """鍦ㄤ氦鏄撴棩婵€娲荤瓥鐣ャ€佹仮澶嶈闃呫€?"""
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
        """鍦ㄧ粺涓€璁㈤槄鍓嶏紝鍏堝畬鎴愭墍鏈夌瓥鐣ョ殑浜ゆ槗鏃ラ鍒濆鍖栥€?"""
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
            "StrategyRunner: 浜ゆ槗鏃ュ墠鍒濆鍖栧畬鎴?[%s]锛屾垚鍔?%d锛屽け璐?%d",
            reason or "unknown",
            prepared,
            failed,
        )

    def _prepare_strategy_for_trading_day(self, strategy: BaseStrategy, trade_day: str = "") -> bool:
        """涓哄崟涓瓥鐣ユ墽琛屼氦鏄撴棩鍓嶅垵濮嬪寲銆?"""
        target_trade_day = trade_day or datetime.now().strftime("%Y%m%d")
        try:
            return bool(strategy.prepare_for_trading_day(target_trade_day))
        except Exception as exc:
            logger.error(
                "StrategyRunner: Strategy[%s] 浜ゆ槗鏃ュ墠鍒濆鍖栧け璐? %s",
                strategy.strategy_id[:8],
                exc,
                exc_info=True,
            )
            return False

    def _subscribe_all(self) -> None:
        self._sync_subscriptions()
        return

    def _sync_subscriptions(self) -> None:
        """Keep ordinary-tick and Level2 subscriptions aligned with strategy needs."""
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
        raw_kinds = getattr(strategy.__class__, "required_data_kinds", lambda: {"tick"})()
        normalized = {str(kind or "").strip().lower() for kind in (raw_kinds or {"tick"})}
        normalized.discard("")
        allowed = getattr(strategy.__class__, "_supported_data_kinds", {"tick"})
        valid = normalized & set(allowed)
        return valid or {"tick"}

    def _restore_pending_orders_from_storage(self) -> int:
        """浠?SQLite 閲嶅缓蹇収涓褰曠殑娲诲姩璁㈠崟銆?"""
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
        """娓呯悊鏁版嵁搴撻噷鏈浠讳綍娲荤瓥鐣ユ帴绠＄殑鏈湴寰呮姤鎸傚崟銆?"""
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
        """鎶婃暟鎹簱琛屽弽搴忓垪鍖栨垚鍐呴儴 Order 瀵硅薄銆?"""
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
        """鎶?SQLite 鏃堕棿瀛楁瑙ｆ瀽涓?datetime銆?"""
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
        """瀹夊叏瑙ｆ瀽璁㈠崟蹇収 JSON銆?"""
        import json

        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _cleanup_stopped(self) -> None:
        """绉婚櫎宸插仠姝笖鏃犳寔浠撶殑绛栫暐锛屽悓鏃跺綊妗ｇ泩浜?"""
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
        """灏嗚鍗曟洿鏂板垎鍙戠粰瀵瑰簲绛栫暐"""
        strategy = self.get_strategy(order.strategy_id)
        if strategy:
            strategy.on_order_update(order)

    def _sync_orders_and_trades_job(self) -> None:
        """浠呭湪浜ゆ槗鏃舵杩愯涓诲姩鍚屾锛岃ˉ鍋挎紡鍥炴姤鍦烘櫙銆?"""
        if not self._running or not self.is_trading_time():
            return
        self.sync_orders_and_trades_once(reason="scheduler")

    def sync_orders_and_trades_once(self, reason: str = "manual") -> Dict[str, int]:
        """涓诲姩鎷夊彇璐︽埛濮旀墭/鎴愪氦骞剁籂姝ｆ湰鍦扮姸鎬併€?"""
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
        """浜哄伐鎾ら攢鏈垚浜ゅ缓浠撳崟锛屽苟鍦ㄥ畨鍏ㄦ椂鎭㈠鍏钩绔炰簤鐘舵€併€?"""
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
        """鏌ヨ骞舵爣鍑嗗寲璐︽埛鎸佷粨鏄犲皠銆?"""
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
        """鍦ㄧ瓥鐣ヨ繍琛屽墠鏍稿璐︽埛璧勪骇鍜岃处鎴锋寔浠撱€?

        鏍￠獙瑙勫垯锛?
        1. 绛栫暐鐨勬渶澶у彲鐢ㄨ祫閲戜笉鑳芥槑鏄惧ぇ浜庤处鎴峰彲鐢ㄨ祫閲戙€?
        2. 绛栫暐鍐呴儴璁板綍鐨勬爣鐨勬寔浠撴暟閲忎笉鑳藉ぇ浜庤处鎴风湡瀹炴寔浠撴暟閲忋€?
        3. 绛栫暐鍐呴儴璁板綍鐨勫彲鐢ㄦ暟閲忎笉鑳藉ぇ浜庤处鎴风湡瀹炲彲鐢ㄦ暟閲忋€?

        娉ㄦ剰锛氳繖閲屾寜鐢ㄦ埛瑕佹眰浠呭彂鍑鸿鍛婏紝涓嶉樆姝㈢▼搴忕户缁繍琛屻€?
        """
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
        """鎸夎处鎴风湡瀹炲彲鐢ㄦ寔浠撳帇闄嶇瓥鐣ヤ晶 available_quantity銆?"""
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
        """鎸夎处鎴峰彲鐢ㄤ笂闄愬帇闄嶅悓涓€鏍囩殑澶氫釜绛栫暐鐨勫彲鍗栨暟閲忋€?"""
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
        """褰撹处鎴锋寔浠撶害鏉熶笉婊¤冻鏃讹紝鏆傚仠鐩稿叧绛栫暐浠ラ伩鍏嶇户缁彂鍑洪敊璇氦鏄撴寚浠ゃ€?"""
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
        """鎶婅处鎴锋垚浜ゆ煡璇㈢粨鏋滆ˉ鐏屽洖鍐呴儴璁㈠崟/鎸佷粨閾捐矾銆?"""
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
        """杩斿洖宸茬煡鎴愪氦 ID 闆嗗悎锛屽苟鍦ㄩ娆′娇鐢ㄦ椂浠庢暟鎹簱棰勭儹銆?"""
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
        """鎶婅处鎴峰鎵樻煡璇㈢粨鏋滃洖鍐欏埌鏈湴璁㈠崟鐘舵€併€?"""
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
        """鍒ゆ柇鏈湴娲诲姩鍗曟槸鍚﹀凡鍦ㄦ煖鍙板鎵樺垪琛ㄤ腑鍑虹幇銆?"""
        xt_order_id = int(getattr(local_order, "xt_order_id", 0) or 0)
        if xt_order_id > 0 and xt_order_id in seen_xt_order_ids:
            return True
        order_trace_id = str(getattr(local_order, "order_trace_id", "") or "").strip()
        return bool(order_trace_id and order_trace_id in seen_trace_ids)

    @staticmethod
    def _resolve_missing_active_order_status(local_order: Order) -> OrderStatus:
        """涓衡€滄煖鍙颁晶涓嶅瓨鍦ㄢ€濈殑鏈湴娲诲姩鍗曢€夋嫨鏀舵暃缁堟€併€?"""
        if int(getattr(local_order, "filled_quantity", 0) or 0) > 0:
            return OrderStatus.PART_CANCEL
        if int(getattr(local_order, "xt_order_id", 0) or 0) > 0:
            return OrderStatus.CANCELED
        return OrderStatus.JUNK

    def _recover_strategy_after_entry_release(self, strategy: BaseStrategy) -> bool:
        """鍦ㄦ棤鎸佷粨涓旀棤娲诲姩涔板崟鏃讹紝鎶婄瓥鐣ユ敹鏁涘洖姝ｇ‘鐘舵€併€?"""
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
        """缁熶竴澶勭悊鍚姩鍓嶆牎楠岃鍛婏細鍚屾椂鍐欐棩蹇楀苟鍙戦€佸憡璀︺€?"""
        logger.warning(message)
        if self._alert_callback:
            try:
                self._alert_callback(AlertLevel.WARNING, message)
            except Exception as exc:
                logger.error("StrategyRunner: 启动前告警发送失败: %s", exc, exc_info=True)

    @staticmethod
    def _map_xt_order_status(xt_status) -> OrderStatus:
        """灏?xtquant 鍘熷璁㈠崟鐘舵€佺爜鏄犲皠涓哄唴閮ㄧ姸鎬併€?"""
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
        """鎻愬彇瀵硅薄涓婄殑鍏紑灞炴€э紝渚夸簬璋冭瘯鍜屾寔涔呭寲銆?"""
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
        """鎶婃煡璇㈠緱鍒扮殑 XtOrder 瀵硅薄杞崲鎴愮粺涓€ dict銆?"""
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
        """鎶?xtquant 璇佸埜浠ｇ爜杞崲涓?6 浣嶅唴閮ㄨ瘉鍒镐唬鐮併€?"""
        return xt_code.split(".")[0] if "." in xt_code else xt_code


__all__ = ["StrategyRunner"]
