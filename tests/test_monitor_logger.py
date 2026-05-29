import logging

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
