"""cytrade 涓荤▼搴忓叆鍙ｃ€?

鏈枃浠朵笉璐熻矗鎵胯浇鍏蜂綋浜ゆ槗绛栫暐閫昏緫锛岃€屾槸璐熻矗鎶婄郴缁熸寜鈥滃彲杩愯鈥濈殑鏂瑰紡缁勮璧锋潵銆?
褰撳墠涓荤▼搴忛噰鐢ㄢ€滀袱灞傝繍琛屾ā鍨嬧€濓細

1. 鐖惰繘绋嬶細
    - 甯搁┗杩愯銆?
    - 浣跨敤 `BlockingScheduler` 缁存姢鏃ョ骇璁″垝浠诲姟銆?
    - 鍒拌揪璁惧畾鏃跺埢鍚庯紝閫氳繃 `ProcessPoolExecutor` 鍚姩鐙珛瀛愯繘绋嬨€?

2. 瀛愯繘绋嬶細
    - 鎵挎媴涓€娆″畬鏁寸殑浜ゆ槗鏃ヤ細璇濄€?
    - 鍦ㄨ杩涚▼鍐呭畬鎴?QMT 杩炴帴銆佽鎯呰闃呫€佺瓥鐣ユ仮澶嶄笌杩愯銆乄eb 鏈嶅姟銆佺湅闂ㄧ嫍绛夊叏閮ㄥ伐浣溿€?
    - 鏀剁洏鍚庝繚瀛樼姸鎬佸苟涓诲姩閫€鍑猴紝浠庤€岄噴鏀捐繘绋嬭祫婧愩€?

涔嬫墍浠ヤ娇鐢ㄨ繖绉嶇粨鏋勶紝鏄负浜嗘妸鈥滈暱鏈熷父椹昏皟搴︹€濅笌鈥滄棩鍐呬氦鏄撲細璇濃€濊В鑰︼細

- 鐖惰繘绋嬪彧鍋氳交閲忚皟搴︼紝涓嶆寔鏈変氦鏄撹繛鎺ュ拰琛屾儏璧勬簮銆?
- 瀛愯繘绋嬪彧璐熻矗鍗曟浜ゆ槗鏃ヤ細璇濓紝缁撴潫鍚庢暣浣撻€€鍑猴紝閬垮厤娈嬬暀绾跨▼銆佽繛鎺ユ垨鍐呭瓨鐘舵€佽法澶╃疮绉€?
- 鑻ュ瓙杩涚▼鍥犲紓甯搁€€鍑猴紝涓嶄細鐩存帴姹℃煋鐖惰繘绋嬭皟搴﹀櫒锛屽彲鍦ㄤ笅涓€娆¤鍒掍换鍔℃椂閲嶆柊鎷夎捣銆?

闃呰鏈枃浠舵椂锛屽缓璁寜濡備笅椤哄簭鐞嗚В锛?

1. `build_app()`锛氱悊瑙ｅ瓙杩涚▼鍐呴儴鏈夊摢浜涙ā鍧椼€佸浣曡閰嶃€?
2. `_run_managed_session()`锛氱悊瑙ｅ崟娆′氦鏄撴棩浼氳瘽鍦ㄥ瓙杩涚▼涓浣曞畬鏁磋繍琛屻€?
3. `run_daily_session()`锛氱悊瑙ｄ竴娆′細璇濈殑鏃ュ巻涓庢椂闂存帶鍒躲€?
4. `_launch_session_in_process()`锛氱悊瑙ｇ埗杩涚▼濡備綍涓哄崟娆′細璇濆垱寤虹嫭绔嬪瓙杩涚▼銆?
5. `run_scheduler_service()`锛氱悊瑙ｇ埗杩涚▼濡備綍闀挎湡璋冨害鏁翠釜绯荤粺銆?
"""
import sys
import os
import signal
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

# 纭繚椤圭洰鏍圭洰褰曞湪 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.xtquant_bootstrap import bootstrap_xtquant_sys_path

XTQUANT_BOOTSTRAP_ROOT = bootstrap_xtquant_sys_path()

from config.settings import Settings
from config.fee_schedule import FeeSchedule
from monitor.logger import LogManager, get_logger
from data.manager import DataManager
from core.connection import ConnectionManager
from core.callback import MyXtQuantTraderCallback
from core.data_subscription import DataSubscriptionManager
from core.trading_calendar import is_market_day
from trading.order_manager import OrderManager
from trading.executor import TradeExecutor
from position.manager import PositionManager
from strategy.runner import StrategyRunner
from monitor.watchdog import Watchdog
from runtime.heartbeat import format_dt as _format_dt
from runtime.heartbeat import start_runtime_heartbeat as _start_runtime_heartbeat
from runtime import accounts as _runtime_accounts
from runtime import session as _runtime_session
from runtime import strategies as _runtime_strategies


def _to_strategy_spec(strategy_class_or_spec) -> str:
    return _runtime_strategies.to_strategy_spec(strategy_class_or_spec)


def _normalize_strategy_specs(strategy_classes=None) -> list[str]:
    return _runtime_strategies.normalize_strategy_specs(strategy_classes)


def _resolve_strategy_specs(strategy_specs) -> list[type]:
    return _runtime_strategies.resolve_strategy_specs(strategy_specs)


def build_app(strategy_classes=None, settings: Settings = None):
    """鏋勫缓骞惰繛鎺ユ墍鏈夋牳蹇冩ā鍧椼€?

    杩欐槸鏁翠釜绋嬪簭鐨勨€滆閰嶅嚱鏁扳€濓紝鍙礋璐ｄ緷璧栨敞鍏ュ拰瀵硅薄杩炴帴锛?
    涓嶈礋璐ｇ湡姝ｈ繘鍏ヨ繍琛屽惊鐜€?

    Args:
        strategy_classes: 闇€瑕佹墭绠＄殑绛栫暐绫诲垪琛ㄣ€?
        settings: 鍙€夐厤缃璞★紱涓嶄紶鏃朵娇鐢ㄩ粯璁ら厤缃€?

    Returns:
        涓€涓寘鍚墍鏈夋牳蹇冩ā鍧楀疄渚嬬殑瀛楀吀锛屼究浜庢祴璇曞拰涓荤▼搴忓鐢ㄣ€?

    杩愯璇存槑锛?
        杩欎釜鍑芥暟鍙互鐞嗚В涓衡€滀氦鏄撳瓙杩涚▼鍐呴儴鐨勫鍣ㄨ閰嶉樁娈碘€濄€傚畠浼氫緷娆″畬鎴愶細

        1. 鍒濆鍖栨棩蹇楃郴缁燂紝纭繚鍚庣画杩愯鏃ュ織鏈夌ǔ瀹氳緭鍑恒€?
        2. 鍒濆鍖栨暟鎹鐞嗗櫒锛屽噯澶?SQLite銆佺姸鎬佸揩鐓х洰褰曞拰鍙€夎繙绋嬪悓姝ヨ兘鍔涖€?
        3. 鍒濆鍖栬垂鐜囬厤缃紝缁熶竴浜ゆ槗璐圭敤璁＄畻瑙勫垯銆?
        4. 鍒濆鍖栬繛鎺ョ鐞嗗櫒锛屼负鍚庣画 QMT 杩炴帴鍜岄噸杩炴彁渚涚粺涓€鍏ュ彛銆?
        5. 鍒濆鍖栬鍗曠鐞嗗櫒銆佹寔浠撶鐞嗗櫒銆佷氦鏄撴墽琛屽櫒銆?
        6. 寤虹珛鍥炶皟閾捐矾锛氭煖鍙板洖鎶?-> 璁㈠崟 -> 鎸佷粨/绛栫暐銆?
        7. 鍒濆鍖栬鎯呰闃呯鐞嗗櫒銆?
        8. 鍒濆鍖栫瓥鐣ヨ繍琛屽櫒锛屽苟娉ㄥ叆鐘舵€佹仮澶嶃€佽处鎴烽妫€鏌ュ拰寤惰繜鐩戞帶鑳藉姏銆?
        9. 鍒濆鍖栫湅闂ㄧ嫍锛屽苟鎶婂績璺充笌鍛婅鍥炶皟鎸傚埌绛栫暐杩愯鍣ㄤ笂銆?

        娉ㄦ剰锛?
        - 杩欓噷鍙畬鎴愨€滃璞¤閰嶁€濓紝涓嶄細鐪熸杩炴帴 QMT锛屼篃涓嶄細鍚姩璁㈤槄鍜岀瓥鐣ヨ繍琛屻€?
        - 鐪熸鐨勫惎鍔ㄥ姩浣滃彂鐢熷湪 `_run_managed_session()` 涓€?
    """
    settings = settings or Settings()
    # 鍏堝噯澶囪繍琛岀洰褰曪紝閬垮厤鍚庣画鏃ュ織銆佹暟鎹簱銆佺姸鎬佹枃浠跺啓鍏ュけ璐ャ€?
    settings.ensure_dirs()

    # ---- 鏃ュ織 ----
    log_mgr = LogManager(
        log_dir=settings.LOG_DIR,
        max_days=settings.LOG_MAX_DAYS,
        level=settings.LOG_LEVEL,
        summary_mode=settings.LOG_SUMMARY_MODE,
    )
    logger = get_logger("system")
    logger.info("=" * 50)
    logger.info("cytrade 鍚姩")
    if XTQUANT_BOOTSTRAP_ROOT:
        logger.info("cytrade: xtquant root=%s", XTQUANT_BOOTSTRAP_ROOT)

    # ---- 鏁版嵁绠＄悊 ----
    data_mgr = DataManager(
        db_path=settings.SQLITE_DB_PATH,
        state_dir=settings.STATE_SAVE_DIR,
        remote_cfg=settings.REMOTE_DB_CONFIG,
    )
    if settings.ENABLE_REMOTE_DB:
        data_mgr.set_remote_enabled(True)

    # ``fee_schedule`` 缁熶竴灏佽涔板崠浣ｉ噾銆佸嵃鑺辩◣鍜?T+0 灞炴€у垽鏂€?
    fee_schedule = FeeSchedule(
        file_path=settings.FEE_TABLE_PATH,
        default_buy_fee_rate=settings.DEFAULT_BUY_FEE_RATE,
        default_sell_fee_rate=settings.DEFAULT_SELL_FEE_RATE,
        default_stamp_tax_rate=settings.DEFAULT_STAMP_TAX_RATE,
    )

    # ---- 浜ゆ槗杩炴帴 ----
    conn_mgr = ConnectionManager(
        qmt_path=settings.QMT_PATH,
        account_id=settings.ACCOUNT_ID,
        account_type=settings.ACCOUNT_TYPE,
        base_interval=settings.RECONNECT_BASE_SEC,
        max_interval=settings.RECONNECT_MAX_INTERVAL_SEC,
        max_retries=(settings.RECONNECT_MAX_RETRIES
                     if settings.RECONNECT_MAX_RETRIES > 0 else None),
    )

    # ---- 璁㈠崟绠＄悊 ----
    order_mgr = OrderManager(data_manager=data_mgr, fee_schedule=fee_schedule)

    # ---- 鎸佷粨绠＄悊 ----
    pos_mgr = PositionManager(
        cost_method=settings.COST_METHOD,
        data_manager=data_mgr,
        fee_schedule=fee_schedule,
    )

    # ---- 娉ㄥ唽鍥炶皟閾撅細鎴愪氦 鈫?鎸佷粨 ----
    order_mgr.set_position_callback(pos_mgr.on_trade_callback)

    # ---- 浜ゆ槗鎵ц鍣?----
    trade_exec = TradeExecutor(
        conn_mgr,
        order_mgr,
        pos_mgr,
        live_trading_enabled=not bool(settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN),
    )

    # ---- XtQuant 鍥炶皟 ----
    callback = MyXtQuantTraderCallback(
        order_manager=order_mgr,
        connection_manager=conn_mgr,
    )
    conn_mgr.register_callback(callback)

    # ---- 鏁版嵁璁㈤槄 ----
    # 琛屾儏璁㈤槄妯″潡涓庝氦鏄撹繛鎺ユā鍧楄В鑰︼紝渚夸簬閲嶈繛鍚庣嫭绔嬫仮澶嶈闃呫€?
    data_sub = DataSubscriptionManager(
        latency_threshold_sec=settings.DATA_LATENCY_THRESHOLD_SEC,
        default_period=settings.SUBSCRIPTION_PERIOD,
    )

    # ---- 绛栫暐杩愯 ----
    runner = StrategyRunner(
        data_subscription=data_sub,
        trade_executor=trade_exec,
        order_manager=order_mgr,
        position_manager=pos_mgr,
        data_manager=data_mgr,
        connection_manager=conn_mgr,
        strategy_classes=strategy_classes or [],
        load_previous_state_on_start=settings.LOAD_PREVIOUS_STATE_ON_START,
        latency_threshold_sec=settings.DATA_LATENCY_THRESHOLD_SEC,
        process_threshold_ms=settings.STRATEGY_PROCESS_THRESHOLD_MS,
    )

    # 娉ㄥ唽鈥滆鍗曠姸鎬佸彉鍖?-> 绛栫暐瀵硅薄鈥濈殑鍥炶皟銆?
    # 杩欐牱绛栫暐鎵嶈兘鍦ㄦ垚浜ゃ€佹挙鍗曘€佸簾鍗曞悗鍙婃椂鏇存柊鑷繁鐨勫唴閮ㄧ姸鎬併€?
    order_mgr.set_strategy_callback(runner.dispatch_order_update)

    # 缃戠粶鏂紑鍚庯紝杩炴帴妯″潡浼氳礋璐ｉ噸杩烇紱
    # 杩欓噷鍐嶆妸鈥滈噸杩炴垚鍔熷悗鐨勮ˉ鍋垮姩浣溾€濇寕杩涘幓锛岃嚜鍔ㄦ仮澶嶈鎯呰闃呫€?
    conn_mgr.register_reconnect_callback(data_sub.resubscribe_all)

    # ---- 鐪嬮棬鐙?----
    watchdog = Watchdog(
        interval_sec=settings.WATCHDOG_INTERVAL_SEC,
        dingtalk_webhook=settings.DINGTALK_WEBHOOK_URL,
        dingtalk_secret=settings.DINGTALK_SECRET,
        cpu_threshold=settings.CPU_ALERT_THRESHOLD,
        mem_threshold=settings.MEM_ALERT_THRESHOLD,
        position_report_times=settings.POSITION_REPORT_TIMES,
        position_manager=pos_mgr,
        connection_manager=conn_mgr,
        data_subscription=data_sub,
    )

    # 琛屾儏鍒拌揪鏃跺埛鏂扮湅闂ㄧ嫍蹇冭烦
    runner.set_heartbeat_callback(watchdog.register_heartbeat)
    runner.set_alert_callback(watchdog.send_dingtalk_alert)

    # 杩斿洖瑁呴厤濂界殑涓婁笅鏂囷紝鏂逛究锛?
    # 1. `run()` 鐩存帴澶嶇敤銆?
    # 2. 娴嬭瘯浠ｇ爜绮剧‘鏂█妯″潡瑁呴厤鍏崇郴銆?
    return {
        "settings": settings,
        "log_mgr": log_mgr,
        "data_mgr": data_mgr,
        "fee_schedule": fee_schedule,
        "conn_mgr": conn_mgr,
        "order_mgr": order_mgr,
        "pos_mgr": pos_mgr,
        "trade_exec": trade_exec,
        "callback": callback,
        "data_sub": data_sub,
        "runner": runner,
        "watchdog": watchdog,
    }




def _log_runtime_startup_config(settings: Settings, conn_mgr: ConnectionManager, mode: str) -> None:
    return _runtime_accounts.log_runtime_startup_config(settings, conn_mgr, mode)


def _log_connection_failure(conn_mgr: ConnectionManager, mode: str) -> None:
    return _runtime_accounts.log_connection_failure(conn_mgr, mode)


def _is_dry_run(settings: Settings) -> bool:
    return _runtime_accounts.is_dry_run(settings)


def _sync_account_after_connection_recovered(ctx: dict, mode: str) -> None:
    return _runtime_accounts.sync_account_after_connection_recovered(ctx, mode)


def _start_account_connection_retry(ctx: dict, stop_event: threading.Event, mode: str) -> threading.Thread:
    return _runtime_accounts.start_account_connection_retry(
        ctx,
        stop_event,
        mode,
        sync_fn=_sync_account_after_connection_recovered,
        log_failure_fn=_log_connection_failure,
    )


def _validate_live_trading_preflight(ctx: dict, mode: str) -> bool:
    return _runtime_accounts.validate_live_trading_preflight(ctx, mode)


def _connect_account_for_runtime(ctx: dict, mode: str, stop_event: threading.Event | None = None) -> bool:
    return _runtime_accounts.connect_account_for_runtime(
        ctx,
        mode,
        stop_event,
        start_retry_fn=_start_account_connection_retry,
        validate_fn=_validate_live_trading_preflight,
        log_startup_fn=_log_runtime_startup_config,
        log_failure_fn=_log_connection_failure,
    )


def _parse_hhmm(value: str) -> tuple[int, int]:
    return _runtime_session.parse_hhmm(value)


def _build_session_time(anchor: datetime, hhmm: str) -> datetime:
    return _runtime_session.build_session_time(anchor, hhmm)


def _wait_until_session_start(settings: Settings, stop_event: threading.Event, now_provider=None, sleep_fn=None) -> bool:
    return _runtime_session.wait_until_session_start(settings, stop_event, now_provider, sleep_fn)


def run(strategy_classes=None, settings: Settings = None):
    return _runtime_session.run_live(
        build_app_fn=build_app,
        connect_account_fn=_connect_account_for_runtime,
        start_heartbeat_fn=_start_runtime_heartbeat,
        strategy_classes=strategy_classes,
        settings=settings,
    )


def _run_managed_session(strategy_classes=None, settings: Settings = None, now_provider=None, sleep_fn=None):
    return _runtime_session.run_managed_session(
        build_app_fn=build_app,
        connect_account_fn=_connect_account_for_runtime,
        start_heartbeat_fn=_start_runtime_heartbeat,
        strategy_classes=strategy_classes,
        settings=settings,
        now_provider=now_provider,
        sleep_fn=sleep_fn,
    )


def run_daily_session(strategy_classes=None, settings: Settings = None, now_provider=None, sleep_fn=None):
    return _runtime_session.run_daily_session(
        run_managed_session_fn=_run_managed_session,
        strategy_classes=strategy_classes,
        settings=settings,
        now_provider=now_provider,
        sleep_fn=sleep_fn,
    )


def _run_daily_session_in_subprocess(strategy_specs: list[str], settings: Settings) -> str:
    return _runtime_session.run_daily_session_in_subprocess(
        strategy_specs,
        settings,
        run_daily_session_fn=run_daily_session,
    )


def _launch_session_in_process(strategy_classes=None, settings: Settings = None) -> str:
    return _runtime_session.launch_session_in_process(
        run_daily_session_in_subprocess_fn=_run_daily_session_in_subprocess,
        strategy_classes=strategy_classes,
        settings=settings,
    )


def run_scheduler_service(strategy_classes=None, settings: Settings = None, scheduler_cls=None) -> None:
    return _runtime_session.run_scheduler_service(
        launch_session_fn=_launch_session_in_process,
        strategy_classes=strategy_classes,
        settings=settings,
        scheduler_cls=scheduler_cls,
    )


if __name__ == "__main__":
    from strategy.test_grid_strategy import TestGridStrategy
    run_scheduler_service(strategy_classes=[TestGridStrategy])
