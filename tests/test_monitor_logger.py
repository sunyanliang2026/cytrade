import logging
from pathlib import Path

from monitor.logger import LogManager, _SummaryFilter, get_logger


def test_log_manager_reapplies_summary_mode_after_early_logger_access(tmp_path):
    LogManager._instance = None

    logger = get_logger("system")
    stream_handlers = [handler for handler in logger.handlers if isinstance(handler, logging.StreamHandler)]
    assert stream_handlers
    assert all(not any(isinstance(flt, _SummaryFilter) for flt in handler.filters) for handler in stream_handlers)

    mgr = LogManager(log_dir=str(tmp_path / "logs"), summary_mode=True)
    logger = mgr.get_logger("system")
    stream_handlers = [handler for handler in logger.handlers if isinstance(handler, logging.StreamHandler)]

    assert all(any(isinstance(flt, _SummaryFilter) for flt in handler.filters) for handler in stream_handlers)


def test_json_file_logger_preserves_chinese_text(tmp_path):
    LogManager._instance = None
    logging.getLogger("cytrade.trade").handlers.clear()

    mgr = LogManager(log_dir=str(tmp_path / "logs"), summary_mode=False)
    logger = mgr.get_logger("trade")
    logger.info('MSF_EVENT {"stock":"000700","name":"模塑科技","event":"观察单成交"}')
    for handler in logger.handlers:
        handler.flush()

    log_text = Path(mgr.get_log_file_path("trade")).read_text(encoding="utf-8")
    assert "模塑科技" in log_text
    assert "观察单成交" in log_text
    assert "\\u6a21" not in log_text
