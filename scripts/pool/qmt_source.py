from __future__ import annotations

from collections.abc import Iterable

from scripts.pool.common import (
    DEFAULT_SECTOR,
    PoolCandidate,
    is_main_board_code,
    max_amplitude,
    normalize_stock_code,
    should_include_limitup_candidate,
)


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def xt_name(xtdata, xt_code: str) -> str:
    try:
        detail = xtdata.get_instrument_detail(xt_code) or {}
    except Exception:
        detail = {}
    return str(detail.get("InstrumentName", "") or "").strip()


def collect_from_qmt(
    *,
    sector: str = DEFAULT_SECTOR,
    chunk_size: int = 500,
    min_float_market_value: float = 1_900_000_000.0,
    max_amplitude_threshold: float = 50.0,
    history_count: int = 31,
    download_history: bool = True,
) -> list[PoolCandidate]:
    from xtquant import xtdata

    try:
        xtdata.download_sector_data()
    except Exception as exc:
        raise RuntimeError(
            "QMT 来源需要连接 QMT/xtdata 读取板块、日线和证券资料，请确认 QMT 已启动并登录。"
        ) from exc
    stock_list = [code for code in list(xtdata.get_stock_list_in_sector(sector) or []) if is_main_board_code(code)]
    candidates: list[PoolCandidate] = []

    for chunk in chunks(stock_list, max(1, int(chunk_size or 500))):
        if download_history:
            xtdata.download_history_data2(chunk, "1d", "", "", incrementally=True)
        bars = xtdata.get_market_data_ex(
            field_list=["time", "open", "high", "low", "close", "volume", "amount"],
            stock_list=chunk,
            period="1d",
            count=max(31, int(history_count or 31)),
            dividend_type="none",
        ) or {}
        for xt_code, frame in bars.items():
            if frame is None or len(frame) < 31:
                continue
            frame = frame.tail(max(31, int(history_count or 31)))
            close_price = float(frame["close"].iloc[-1] or 0.0)
            pre_close = float(frame["close"].iloc[-2] or 0.0)
            amplitude = max_amplitude(frame["high"].tail(30).tolist(), frame["low"].tail(30).tolist())
            name = xt_name(xtdata, xt_code)
            detail = xtdata.get_instrument_detail(xt_code) or {}
            float_volume = float(detail.get("FloatVolume", 0.0) or detail.get("FloatVolumn", 0.0) or 0.0)
            float_market_value = float_volume * close_price
            ok, pct = should_include_limitup_candidate(
                xt_code=xt_code,
                name=name,
                close_price=close_price,
                pre_close=pre_close,
                float_market_value=float_market_value,
                max_amplitude_30d=amplitude,
                min_float_market_value=float(min_float_market_value),
                max_amplitude_threshold=float(max_amplitude_threshold),
            )
            if not ok:
                continue
            candidates.append(
                PoolCandidate(
                    code=normalize_stock_code(xt_code),
                    name=name,
                    pct_change=pct,
                    last_price=close_price,
                    pre_close=pre_close,
                    amount=float(frame["amount"].iloc[-1] or 0.0),
                    float_market_value=float_market_value,
                    max_amplitude_30d=amplitude,
                )
            )

    candidates.sort(key=lambda item: (-item.float_market_value, -item.amount, item.code))
    return candidates
