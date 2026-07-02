"""
Market data subscription management.

- Subscribe/unsubscribe real-time market data
- Normalize xtquant payloads into project-level models
- Track latest data timestamps and latency
- Dispatch ordinary tick and Level2 data through dedicated callbacks
"""
from __future__ import annotations

import sys
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from config.enums import SubscriptionPeriod
from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
from monitor.logger import get_logger

logger = get_logger("system")

try:
    from xtquant import xtdata

    _XT_AVAILABLE = True
except ImportError:
    _XT_AVAILABLE = False
    xtdata = None  # type: ignore


class DataSubscriptionManager:
    """Manage ordinary tick and Level2 data subscriptions."""

    SUPPORTED_L2_KINDS = frozenset({"l2quote", "l2transaction", "l2order", "l2orderqueue"})

    def __init__(
        self,
        latency_threshold_sec: float = 10.0,
        default_period: SubscriptionPeriod | str = SubscriptionPeriod.TICK,
        print_latest_status: bool = True,
    ):
        self._subscriptions: Dict[str, str] = {}
        self._subscription_ids: Dict[str, int] = {}
        self._l2_subscriptions: Dict[str, set[str]] = {}
        self._l2_subscription_ids: Dict[tuple[str, str], int] = {}

        self._data_callback: Optional[Callable[[Dict[str, TickData]], None]] = None
        self._l2_quote_callback: Optional[Callable[[Dict[str, L2QuoteEvent]], None]] = None
        self._l2_transaction_callback: Optional[
            Callable[[Dict[str, List[L2TransactionEvent]]], None]
        ] = None
        self._l2_order_callback: Optional[Callable[[Dict[str, List[L2OrderEvent]]], None]] = None
        self._l2_orderqueue_callback: Optional[Callable[[Dict[str, L2OrderQueueEvent]], None]] = None

        self._latency_threshold = latency_threshold_sec
        self._default_period = self._normalize_period(default_period)
        self._running = False
        self._whole_market = False
        self._whole_market_subscribe_id: Optional[int] = None
        self._lock = threading.Lock()
        self._last_recv_time: Optional[datetime] = None
        self._latest_data_time: Optional[datetime] = None
        self._latest_latency_ms: float = 0.0
        self._xtdata_connected = False
        self._print_latest_status = bool(print_latest_status)

    # ------------------------------------------------------------------ Public

    def subscribe_stocks(self, stock_codes: List[str], period: SubscriptionPeriod | str = "") -> None:
        """Subscribe ordinary tick data for a list of stocks."""
        period = self._normalize_period(period or self._default_period)
        xt_codes = [self._to_xt(c) for c in stock_codes]
        with self._lock:
            for code in stock_codes:
                self._subscriptions[code] = period

        if not _XT_AVAILABLE:
            logger.warning("DataSubscription: xtquant not installed, skip real tick subscribe")
            return

        try:
            self._ensure_xtdata_connected()
            for code, xt_code in zip(stock_codes, xt_codes):
                self._subscribe_xt_quote(
                    subscribe_key=code,
                    xt_code=xt_code,
                    period=period,
                    callback=self._on_data,
                    subscription_ids=self._subscription_ids,
                )
            logger.info("DataSubscription: subscribed %d stocks [%s]", len(xt_codes), period)
        except Exception as e:
            logger.error("DataSubscription: subscribe_stocks failed: %s", e, exc_info=True)

    def unsubscribe_stocks(self, stock_codes: List[str]) -> None:
        """Unsubscribe ordinary tick data for a list of stocks."""
        sub_ids: Dict[str, Optional[int]] = {}
        with self._lock:
            for code in stock_codes:
                sub_ids[code] = self._subscription_ids.get(code)
                self._subscriptions.pop(code, None)
                self._subscription_ids.pop(code, None)

        if not _XT_AVAILABLE:
            return

        try:
            for code in stock_codes:
                sub_id = sub_ids.get(code)
                if sub_id is not None:
                    xtdata.unsubscribe_quote(sub_id)
            logger.info("DataSubscription: unsubscribed %d stocks", len(stock_codes))
        except Exception as e:
            logger.error("DataSubscription: unsubscribe_stocks failed: %s", e, exc_info=True)

    def subscribe_l2_stocks(self, stock_codes: List[str], kinds: Optional[List[str] | set[str] | tuple[str, ...]] = None) -> None:
        """Subscribe Level2 feeds for a list of stocks."""
        requested_kinds = self._normalize_l2_kinds(kinds or self.SUPPORTED_L2_KINDS)
        xt_codes = [self._to_xt(c) for c in stock_codes]

        with self._lock:
            for code in stock_codes:
                self._l2_subscriptions.setdefault(code, set()).update(requested_kinds)

        if not _XT_AVAILABLE:
            logger.warning("DataSubscription: xtquant not installed, skip real Level2 subscribe")
            return

        try:
            self._ensure_xtdata_connected()
            for code, xt_code in zip(stock_codes, xt_codes):
                for kind in requested_kinds:
                    self._subscribe_xt_quote(
                        subscribe_key=(code, kind),
                        xt_code=xt_code,
                        period=kind,
                        callback=self._get_l2_callback(kind),
                        subscription_ids=self._l2_subscription_ids,
                    )
            logger.info(
                "DataSubscription: subscribed %d stocks for Level2 kinds=%s stocks=%s",
                len(stock_codes),
                ",".join(sorted(requested_kinds)),
                ",".join(stock_codes),
            )
        except Exception as e:
            logger.error("DataSubscription: subscribe_l2_stocks failed: %s", e, exc_info=True)

    def unsubscribe_l2_stocks(
        self,
        stock_codes: List[str],
        kinds: Optional[List[str] | set[str] | tuple[str, ...]] = None,
    ) -> None:
        """Unsubscribe Level2 feeds for a list of stocks."""
        requested_kinds = self._normalize_l2_kinds(kinds or self.SUPPORTED_L2_KINDS)
        sub_ids: Dict[tuple[str, str], Optional[int]] = {}

        with self._lock:
            for code in stock_codes:
                existing = self._l2_subscriptions.get(code, set())
                for kind in requested_kinds:
                    sub_ids[(code, kind)] = self._l2_subscription_ids.get((code, kind))
                    existing.discard(kind)
                    self._l2_subscription_ids.pop((code, kind), None)
                if existing:
                    self._l2_subscriptions[code] = existing
                else:
                    self._l2_subscriptions.pop(code, None)

        if not _XT_AVAILABLE:
            return

        try:
            for subscribe_key, sub_id in sub_ids.items():
                if sub_id is not None:
                    xtdata.unsubscribe_quote(sub_id)
            logger.info(
                "DataSubscription: unsubscribed Level2 for %d stocks kinds=%s stocks=%s",
                len(stock_codes),
                ",".join(sorted(requested_kinds)),
                ",".join(stock_codes),
            )
        except Exception as e:
            logger.error("DataSubscription: unsubscribe_l2_stocks failed: %s", e, exc_info=True)

    def subscribe_whole_market(self, period: SubscriptionPeriod | str = "") -> None:
        """Subscribe the whole market using the ordinary quote pipeline."""
        period = self._normalize_period(period or self._default_period)
        self._whole_market = True
        if not _XT_AVAILABLE:
            logger.warning("DataSubscription: xtquant not installed, skip whole-market subscribe")
            return

        try:
            self._ensure_xtdata_connected()
            self._whole_market_subscribe_id = xtdata.subscribe_whole_quote(["SH", "SZ"], callback=self._on_data)
            logger.info("DataSubscription: whole market subscribed [%s]", period)
        except Exception as e:
            logger.error("DataSubscription: subscribe_whole_market failed: %s", e, exc_info=True)

    def get_subscription_list(self) -> List[str]:
        """Return subscribed ordinary-tick stock codes."""
        with self._lock:
            return list(self._subscriptions.keys())

    def get_l2_subscription_map(self) -> Dict[str, List[str]]:
        """Return subscribed Level2 intents grouped by stock."""
        with self._lock:
            return {
                code: sorted(kinds)
                for code, kinds in self._l2_subscriptions.items()
                if kinds
            }

    def set_data_callback(self, callback: Callable[[Dict[str, TickData]], None]) -> None:
        self._data_callback = callback

    def set_l2_quote_callback(self, callback: Callable[[Dict[str, L2QuoteEvent]], None]) -> None:
        self._l2_quote_callback = callback

    def set_l2_transaction_callback(
        self, callback: Callable[[Dict[str, List[L2TransactionEvent]]], None]
    ) -> None:
        self._l2_transaction_callback = callback

    def set_l2_order_callback(self, callback: Callable[[Dict[str, List[L2OrderEvent]]], None]) -> None:
        self._l2_order_callback = callback

    def set_l2_orderqueue_callback(self, callback: Callable[[Dict[str, L2OrderQueueEvent]], None]) -> None:
        self._l2_orderqueue_callback = callback

    def get_latest_data_status(self) -> dict:
        with self._lock:
            latest_data_time = self._latest_data_time
            latest_latency_ms = self._latest_latency_ms
            last_recv_time = self._last_recv_time

        return {
            "latest_data_time": latest_data_time,
            "data_delay_ms": float(latest_latency_ms or 0.0),
            "last_recv_time": last_recv_time,
        }

    def resubscribe_all(self) -> None:
        """Restore subscription intents after reconnect."""
        with self._lock:
            subscriptions = dict(self._subscriptions)
            l2_subscriptions = {code: set(kinds) for code, kinds in self._l2_subscriptions.items()}
            whole_market = self._whole_market

        if whole_market:
            self.subscribe_whole_market()

        if subscriptions:
            period_groups: Dict[str, List[str]] = {}
            for code, period in subscriptions.items():
                period_groups.setdefault(period, []).append(code)
            for period, codes in period_groups.items():
                self.subscribe_stocks(codes, period)
        else:
            logger.info("DataSubscription: no ordinary subscriptions to restore")

        if l2_subscriptions:
            for code, kinds in l2_subscriptions.items():
                self.subscribe_l2_stocks([code], kinds=sorted(kinds))
        else:
            logger.info("DataSubscription: no Level2 subscriptions to restore")

        logger.info(
            "DataSubscription: resubscribe_all done tick=%d l2=%d whole=%s",
            len(subscriptions),
            len(l2_subscriptions),
            whole_market,
        )

    def start(self) -> None:
        """Run the xtdata event loop."""
        self._running = True
        logger.info("DataSubscription: start xtdata.run()")
        if _XT_AVAILABLE:
            try:
                self._ensure_xtdata_connected()
                xtdata.run()
            except Exception as e:
                logger.error("DataSubscription: xtdata.run() failed: %s", e, exc_info=True)
        else:
            while self._running:
                time.sleep(1)

    def stop(self) -> None:
        self._running = False
        logger.info("DataSubscription: stopped")

    # ------------------------------------------------------------------ Internal callbacks

    def _on_data(self, raw_data: dict) -> None:
        """Handle ordinary quote/tick payloads."""
        try:
            recv_time = datetime.now()
            ticks: Dict[str, TickData] = {}
            latest_tick: Optional[TickData] = None

            for xt_code, data in raw_data.items():
                code = xt_code.split(".")[0] if "." in xt_code else xt_code
                tick = self._parse_tick(code, data, recv_time)
                ticks[code] = tick
                if latest_tick is None or (tick.data_time or recv_time) >= (latest_tick.data_time or recv_time):
                    latest_tick = tick

                if tick.latency_ms > self._latency_threshold * 1000:
                    logger.warning(
                        "DataSubscription: data latency %.1fs > %.1fs [%s]",
                        tick.latency_ms / 1000,
                        self._latency_threshold,
                        code,
                    )

            if latest_tick is not None:
                self._update_latest_data_status(latest_tick)

            if ticks and self._data_callback:
                self._data_callback(ticks)

        except Exception as e:
            logger.error("DataSubscription: _on_data failed: %s", e, exc_info=True)

    def _on_l2_quote_data(self, raw_data: dict) -> None:
        try:
            recv_time = datetime.now()
            events: Dict[str, L2QuoteEvent] = {}
            for xt_code, data in raw_data.items():
                code = xt_code.split(".")[0] if "." in xt_code else xt_code
                event = self._parse_l2_quote(code, data, recv_time)
                events[code] = event
            if events:
                latest = max((event.event_time or recv_time) for event in events.values())
                self._update_latest_data_status_from_times(latest, recv_time)
            if events and self._l2_quote_callback:
                self._l2_quote_callback(events)
        except Exception as e:
            logger.error("DataSubscription: _on_l2_quote_data failed: %s", e, exc_info=True)

    def _on_l2_transaction_data(self, raw_data: dict) -> None:
        try:
            recv_time = datetime.now()
            events_by_code: Dict[str, List[L2TransactionEvent]] = {}
            for xt_code, data in raw_data.items():
                code = xt_code.split(".")[0] if "." in xt_code else xt_code
                records = self._normalize_record_payloads(code, data)
                events = [self._parse_l2_transaction_record(code, record, recv_time) for record in records]
                if events:
                    events_by_code[code] = events
            if events_by_code:
                latest = max((event.event_time or recv_time) for events in events_by_code.values() for event in events)
                self._update_latest_data_status_from_times(latest, recv_time)
            if events_by_code and self._l2_transaction_callback:
                self._l2_transaction_callback(events_by_code)
        except Exception as e:
            logger.error("DataSubscription: _on_l2_transaction_data failed: %s", e, exc_info=True)

    def _on_l2_order_data(self, raw_data: dict) -> None:
        try:
            recv_time = datetime.now()
            events_by_code: Dict[str, List[L2OrderEvent]] = {}
            for xt_code, data in raw_data.items():
                code = xt_code.split(".")[0] if "." in xt_code else xt_code
                records = self._normalize_record_payloads(code, data)
                events = [self._parse_l2_order_record(code, record, recv_time) for record in records]
                if events:
                    events_by_code[code] = events
            if events_by_code:
                latest = max((event.event_time or recv_time) for events in events_by_code.values() for event in events)
                self._update_latest_data_status_from_times(latest, recv_time)
            if events_by_code and self._l2_order_callback:
                self._l2_order_callback(events_by_code)
        except Exception as e:
            logger.error("DataSubscription: _on_l2_order_data failed: %s", e, exc_info=True)

    def _on_l2_orderqueue_data(self, raw_data: dict) -> None:
        try:
            recv_time = datetime.now()
            events: Dict[str, L2OrderQueueEvent] = {}
            for xt_code, data in raw_data.items():
                code = xt_code.split(".")[0] if "." in xt_code else xt_code
                event = self._parse_l2_orderqueue(code, data, recv_time)
                events[code] = event
            if events:
                latest = max((event.event_time or recv_time) for event in events.values())
                self._update_latest_data_status_from_times(latest, recv_time)
            if events and self._l2_orderqueue_callback:
                self._l2_orderqueue_callback(events)
        except Exception as e:
            logger.error("DataSubscription: _on_l2_orderqueue_data failed: %s", e, exc_info=True)

    # ------------------------------------------------------------------ Mock push

    def push_mock_tick(self, code: str, price: float, volume: int = 1000) -> None:
        recv_time = datetime.now()
        tick = TickData(
            stock_code=code,
            last_price=price,
            open=price,
            high=price,
            low=price,
            pre_close=price * 0.99,
            volume=volume,
            amount=price * volume,
            bid_prices=[price - 0.01, price - 0.02, price - 0.03, price - 0.04, price - 0.05],
            bid_volumes=[100] * 5,
            ask_prices=[price + 0.01, price + 0.02, price + 0.03, price + 0.04, price + 0.05],
            ask_volumes=[100] * 5,
            data_time=recv_time,
            recv_time=recv_time,
            latency_ms=0.0,
        )
        self._update_latest_data_status(tick)
        if self._data_callback:
            self._data_callback({code: tick})

    def push_mock_l2_quote(self, code: str, price: float, limit_up_price: float = 0.0) -> None:
        if not self._l2_quote_callback:
            return
        now = datetime.now()
        self._update_latest_data_status_from_times(now, now)
        self._l2_quote_callback(
            {
                code: L2QuoteEvent(
                    stock_code=code,
                    last_price=price,
                    pre_close=price * 0.99,
                    bid1=price,
                    ask1=price + 0.01,
                    bid1_volume=100,
                    ask1_volume=100,
                    limit_up_price=limit_up_price,
                    event_time=now,
                    recv_time=now,
                    raw_xt_fields={"mock": True},
                )
            }
        )

    def push_mock_l2_transaction(self, code: str, price: float, volume: int, side: str = "BUY") -> None:
        if not self._l2_transaction_callback:
            return
        now = datetime.now()
        self._update_latest_data_status_from_times(now, now)
        self._l2_transaction_callback(
            {
                code: [
                    L2TransactionEvent(
                        stock_code=code,
                        price=price,
                        volume=volume,
                        amount=price * volume,
                        side=side,
                        trade_flag=1 if str(side or "").upper() == "BUY" else 2,
                        event_time=now,
                        recv_time=now,
                        raw_xt_fields={"mock": True},
                    )
                ]
            }
        )

    def push_mock_l2_order(
        self,
        code: str,
        price: float,
        volume: int,
        side: str = "BUY",
        is_cancel: bool = False,
        entrust_no: str = "",
    ) -> None:
        if not self._l2_order_callback:
            return
        now = datetime.now()
        self._update_latest_data_status_from_times(now, now)
        self._l2_order_callback(
            {
                code: [
                    L2OrderEvent(
                        stock_code=code,
                        price=price,
                        volume=volume,
                        amount=price * volume,
                        side=side,
                        entrust_no=entrust_no,
                        entrust_type=1,
                        entrust_direction=1 if str(side or "").upper() == "BUY" else 2,
                        is_cancel=is_cancel,
                        event_time=now,
                        recv_time=now,
                        raw_xt_fields={"mock": True},
                    )
                ]
            }
        )

    def push_mock_l2_orderqueue(self, code: str, price: float, bid_level_volume: List[int]) -> None:
        if not self._l2_orderqueue_callback:
            return
        now = datetime.now()
        self._update_latest_data_status_from_times(now, now)
        self._l2_orderqueue_callback(
            {
                code: L2OrderQueueEvent(
                    stock_code=code,
                    price=price,
                    bid_level_volume=[int(v) for v in bid_level_volume],
                    reported_total_order_count=len(bid_level_volume),
                    observed_queue_count=len(bid_level_volume),
                    is_partial_queue=False,
                    event_time=now,
                    recv_time=now,
                    raw_xt_fields={"mock": True},
                )
            }
        )

    # ------------------------------------------------------------------ Private

    def _subscribe_xt_quote(
        self,
        subscribe_key: Any,
        xt_code: str,
        period: str,
        callback,
        subscription_ids: dict,
    ) -> None:
        old_sub_id = subscription_ids.get(subscribe_key)
        if old_sub_id is not None:
            try:
                xtdata.unsubscribe_quote(old_sub_id)
            except Exception:
                pass
        sub_id = xtdata.subscribe_quote(
            xt_code,
            period=period,
            count=-1,
            callback=callback,
        )
        subscription_ids[subscribe_key] = int(sub_id) if sub_id is not None else -1

    def _get_l2_callback(self, kind: str):
        callback_map = {
            "l2quote": self._on_l2_quote_data,
            "l2transaction": self._on_l2_transaction_data,
            "l2order": self._on_l2_order_data,
            "l2orderqueue": self._on_l2_orderqueue_data,
        }
        return callback_map[kind]

    def _ensure_xtdata_connected(self):
        if not _XT_AVAILABLE or self._xtdata_connected:
            return None

        with self._lock:
            if self._xtdata_connected:
                return None

            client = xtdata.connect()
            self._xtdata_connected = True
            logger.info("DataSubscription: xtdata connected data_dir=%s", xtdata.get_data_dir())
            return client

    def _update_latest_data_status(self, tick: TickData) -> None:
        latest_data_time = tick.data_time or tick.recv_time or datetime.now()
        last_recv_time = tick.recv_time or datetime.now()
        latency_ms = float(tick.latency_ms or 0.0)
        self._set_latest_data_status(latest_data_time, last_recv_time, latency_ms)
        if self._print_latest_status:
            self._print_latest_data_status(latest_data_time, latency_ms)

    def _update_latest_data_status_from_times(self, latest_data_time: datetime, recv_time: datetime) -> None:
        latency_ms = max(0.0, (recv_time - latest_data_time).total_seconds() * 1000)
        self._set_latest_data_status(latest_data_time, recv_time, latency_ms)

    def _set_latest_data_status(self, latest_data_time: datetime, last_recv_time: datetime, latency_ms: float) -> None:
        with self._lock:
            self._last_recv_time = last_recv_time
            self._latest_data_time = latest_data_time
            self._latest_latency_ms = float(latency_ms or 0.0)

    @staticmethod
    def _print_latest_data_status(latest_data_time: datetime, latency_ms: float) -> None:
        if not getattr(sys.stdout, "isatty", lambda: False)():
            return

        latest_text = latest_data_time.strftime("%Y-%m-%d %H:%M:%S")
        delay_text = f"{latency_ms / 1000:.2f}s" if latency_ms >= 1000 else f"{latency_ms:.0f}ms"
        sys.stdout.write(f"\rLatest data time: {latest_text} | Latency: {delay_text}      ")
        sys.stdout.flush()

    @staticmethod
    def _normalize_period(period: SubscriptionPeriod | str) -> str:
        if isinstance(period, SubscriptionPeriod):
            return period.value
        try:
            return SubscriptionPeriod(str(period)).value
        except ValueError:
            logger.warning("DataSubscription: invalid period %s, fallback to tick", period)
            return SubscriptionPeriod.TICK.value

    @classmethod
    def _normalize_l2_kinds(cls, kinds: List[str] | set[str] | tuple[str, ...]) -> List[str]:
        normalized = []
        seen = set()
        for kind in kinds:
            text = str(kind or "").strip().lower()
            if not text or text not in cls.SUPPORTED_L2_KINDS or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        if not normalized:
            raise ValueError("No valid Level2 kinds provided")
        return normalized

    @staticmethod
    def _parse_tick(code: str, data: dict | list, recv_time: datetime) -> TickData:
        data = DataSubscriptionManager._normalize_tick_payload(code, data)

        raw_time = DataSubscriptionManager._extract_scalar(data.get("time")) or DataSubscriptionManager._extract_scalar(
            data.get("sysTime")
        )
        data_time = DataSubscriptionManager._coerce_datetime(raw_time, recv_time)
        latency_ms = max(0.0, (recv_time - data_time).total_seconds() * 1000)

        def _get(key, default=0.0):
            value = DataSubscriptionManager._extract_scalar(data.get(key))
            return value if value is not None else default

        bids_p = DataSubscriptionManager._extract_book_values(data.get("bidPrice"))
        bids_v = DataSubscriptionManager._extract_book_values(data.get("bidVol"), cast_type=int)
        asks_p = DataSubscriptionManager._extract_book_values(data.get("askPrice"))
        asks_v = DataSubscriptionManager._extract_book_values(data.get("askVol"), cast_type=int)

        return TickData(
            stock_code=code,
            last_price=float(_get("lastPrice")),
            open=float(_get("open")),
            high=float(_get("high")),
            low=float(_get("low")),
            pre_close=float(_get("lastClose")),
            volume=int(_get("volume")),
            amount=float(_get("amount")),
            bid_prices=bids_p,
            bid_volumes=bids_v,
            ask_prices=asks_p,
            ask_volumes=asks_v,
            data_time=data_time,
            recv_time=recv_time,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _parse_l2_quote(code: str, data: dict | list, recv_time: datetime) -> L2QuoteEvent:
        payload = DataSubscriptionManager._normalize_tick_payload(code, data)
        event_time = DataSubscriptionManager._coerce_datetime(
            DataSubscriptionManager._extract_scalar(payload.get("time"))
            or DataSubscriptionManager._extract_scalar(payload.get("sysTime")),
            recv_time,
        )
        bids = DataSubscriptionManager._extract_book_values(payload.get("bidPrice"))
        asks = DataSubscriptionManager._extract_book_values(payload.get("askPrice"))
        bid_volumes = DataSubscriptionManager._extract_book_values(payload.get("bidVol"), cast_type=int)
        ask_volumes = DataSubscriptionManager._extract_book_values(payload.get("askVol"), cast_type=int)
        return L2QuoteEvent(
            stock_code=code,
            last_price=float(DataSubscriptionManager._extract_scalar(payload.get("lastPrice"), 0.0) or 0.0),
            pre_close=float(DataSubscriptionManager._extract_scalar(payload.get("lastClose"), 0.0) or 0.0),
            bid1=float(bids[0] if bids else 0.0),
            ask1=float(asks[0] if asks else 0.0),
            bid1_volume=int(bid_volumes[0] if bid_volumes else 0),
            ask1_volume=int(ask_volumes[0] if ask_volumes else 0),
            limit_up_price=float(
                DataSubscriptionManager._extract_scalar(payload.get("upLimitPrice"))
                or DataSubscriptionManager._extract_scalar(payload.get("upperLimitPrice"))
                or DataSubscriptionManager._extract_scalar(payload.get("limitUp"))
                or 0.0
            ),
            event_time=event_time,
            recv_time=recv_time,
            raw_xt_fields=dict(payload),
        )

    @staticmethod
    def _parse_l2_transaction_record(code: str, record: dict, recv_time: datetime) -> L2TransactionEvent:
        price = float(DataSubscriptionManager._extract_scalar(record.get("price"), 0.0) or 0.0)
        volume = int(DataSubscriptionManager._extract_scalar(record.get("volume"), 0) or 0)
        amount = float(DataSubscriptionManager._extract_scalar(record.get("amount"), price * volume) or (price * volume))
        side = str(
            DataSubscriptionManager._extract_scalar(record.get("side"))
            or DataSubscriptionManager._extract_scalar(record.get("bsflag"))
            or DataSubscriptionManager._extract_scalar(record.get("bsFlag"))
            or DataSubscriptionManager._extract_scalar(record.get("direction"))
            or ""
        )
        trade_index = DataSubscriptionManager._extract_string_scalar(record.get("tradeIndex"))
        buy_no = DataSubscriptionManager._extract_string_scalar(record.get("buyNo"))
        sell_no = DataSubscriptionManager._extract_string_scalar(record.get("sellNo"))
        trade_type = DataSubscriptionManager._extract_optional_int(record.get("tradeType"))
        trade_flag = DataSubscriptionManager._extract_optional_int(record.get("tradeFlag"))
        event_time = DataSubscriptionManager._coerce_datetime(
            DataSubscriptionManager._extract_scalar(record.get("time"))
            or DataSubscriptionManager._extract_scalar(record.get("tradeTime"))
            or DataSubscriptionManager._extract_scalar(record.get("transTime")),
            recv_time,
        )
        return L2TransactionEvent(
            stock_code=code,
            price=price,
            volume=volume,
            amount=amount,
            side=side,
            trade_index=trade_index,
            buy_no=buy_no,
            sell_no=sell_no,
            trade_type=trade_type,
            trade_flag=trade_flag,
            event_time=event_time,
            recv_time=recv_time,
            raw_xt_fields=dict(record),
        )

    @staticmethod
    def _parse_l2_order_record(code: str, record: dict, recv_time: datetime) -> L2OrderEvent:
        price = float(DataSubscriptionManager._extract_scalar(record.get("price"), 0.0) or 0.0)
        volume = int(DataSubscriptionManager._extract_scalar(record.get("volume"), 0) or 0)
        amount = float(DataSubscriptionManager._extract_scalar(record.get("amount"), price * volume) or (price * volume))
        entrust_type = DataSubscriptionManager._extract_optional_int(record.get("entrustType"))
        entrust_direction = DataSubscriptionManager._extract_optional_int(record.get("entrustDirection"))
        side = str(
            DataSubscriptionManager._extract_scalar(record.get("side"))
            or DataSubscriptionManager._extract_scalar(record.get("bsflag"))
            or DataSubscriptionManager._extract_scalar(record.get("bsFlag"))
            or DataSubscriptionManager._extract_scalar(record.get("direction"))
            or DataSubscriptionManager._map_entrust_direction_side(entrust_direction)
            or ""
        )
        entrust_no = (
            DataSubscriptionManager._extract_string_scalar(record.get("entrustNo"))
            or DataSubscriptionManager._extract_string_scalar(record.get("entrust_no"))
            or DataSubscriptionManager._extract_string_scalar(record.get("seq"))
        )
        event_time = DataSubscriptionManager._coerce_datetime(
            DataSubscriptionManager._extract_scalar(record.get("time"))
            or DataSubscriptionManager._extract_scalar(record.get("entrustTime"))
            or DataSubscriptionManager._extract_scalar(record.get("orderTime")),
            recv_time,
        )
        return L2OrderEvent(
            stock_code=code,
            price=price,
            volume=volume,
            amount=amount,
            side=side,
            entrust_no=entrust_no,
            entrust_type=entrust_type,
            entrust_direction=entrust_direction,
            is_cancel=DataSubscriptionManager._infer_order_cancel(record),
            event_time=event_time,
            recv_time=recv_time,
            raw_xt_fields=dict(record),
        )

    @staticmethod
    def _parse_l2_orderqueue(code: str, data: dict | list, recv_time: datetime) -> L2OrderQueueEvent:
        payload = DataSubscriptionManager._normalize_tick_payload(code, data)
        event_time = DataSubscriptionManager._coerce_datetime(
            DataSubscriptionManager._extract_scalar(payload.get("time"))
            or DataSubscriptionManager._extract_scalar(payload.get("sysTime")),
            recv_time,
        )
        bid_level_volume = DataSubscriptionManager._extract_book_values(
            payload.get("bidLevelVolume"), cast_type=int, limit=None
        )
        reported_total_order_count = int(
            DataSubscriptionManager._extract_scalar(payload.get("bidLevelNumber"), 0) or 0
        )
        observed_queue_count = len(bid_level_volume)
        return L2OrderQueueEvent(
            stock_code=code,
            price=float(
                DataSubscriptionManager._extract_scalar(payload.get("bidLevelPrice"))
                or DataSubscriptionManager._extract_scalar(payload.get("price"))
                or 0.0
            ),
            bid_level_volume=bid_level_volume,
            reported_total_order_count=reported_total_order_count,
            observed_queue_count=observed_queue_count,
            is_partial_queue=reported_total_order_count > observed_queue_count,
            event_time=event_time,
            recv_time=recv_time,
            raw_xt_fields=dict(payload),
        )

    @staticmethod
    def _normalize_tick_payload(code: str, data: dict | list) -> dict:
        if isinstance(data, dict):
            return data

        if isinstance(data, list):
            for item in reversed(data):
                if isinstance(item, dict):
                    return item
            logger.warning("DataSubscription: %s received unsupported list payload", code)
            return {}

        logger.warning("DataSubscription: %s received unknown payload type %s", code, type(data).__name__)
        return {}

    @staticmethod
    def _normalize_record_payloads(code: str, data: dict | list) -> List[dict]:
        if isinstance(data, list):
            records = [item for item in data if isinstance(item, dict)]
            if records:
                return records
            logger.warning("DataSubscription: %s received non-dict record list", code)
            return []

        if isinstance(data, dict):
            if any(isinstance(value, dict) for value in data.values()):
                nested_records = [value for value in data.values() if isinstance(value, dict)]
                if nested_records:
                    return nested_records

            sequence_lengths = []
            for value in data.values():
                normalized = DataSubscriptionManager._to_python_sequence(value)
                if isinstance(normalized, Sequence) and not isinstance(normalized, (str, bytes, bytearray)):
                    sequence_lengths.append(len(normalized))

            if sequence_lengths and len(set(sequence_lengths)) == 1 and sequence_lengths[0] > 0:
                length = sequence_lengths[0]
                records = []
                for index in range(length):
                    record = {}
                    for key, value in data.items():
                        record[key] = DataSubscriptionManager._extract_indexed_value(value, index)
                    records.append(record)
                return records

            return [data]

        logger.warning("DataSubscription: %s received unknown record payload type %s", code, type(data).__name__)
        return []

    @staticmethod
    def _coerce_datetime(raw_time: Any, default: Optional[datetime] = None) -> datetime:
        fallback = default or datetime.now()
        if raw_time in (None, ""):
            return fallback
        try:
            if isinstance(raw_time, datetime):
                return raw_time
            if isinstance(raw_time, (int, float)):
                timestamp = float(raw_time)
                if timestamp > 1e12:
                    return datetime.fromtimestamp(timestamp / 1000)
                if timestamp > 1e10:
                    return datetime.fromtimestamp(timestamp / 1000)
                return datetime.fromtimestamp(timestamp)
        except Exception:
            return fallback
        return fallback

    @staticmethod
    def _infer_order_cancel(record: dict) -> bool:
        entrust_direction = DataSubscriptionManager._extract_optional_int(record.get("entrustDirection"))
        if entrust_direction in (3, 4):
            return True
        explicit = DataSubscriptionManager._extract_scalar(record.get("isCancel"))
        if explicit is not None:
            return bool(explicit)
        explicit = DataSubscriptionManager._extract_scalar(record.get("cancelFlag"))
        if explicit is not None:
            text = str(explicit).strip().lower()
            return text in {"1", "true", "y", "yes", "cancel", "c"}
        text_fields = [
            DataSubscriptionManager._extract_scalar(record.get("execType")),
            DataSubscriptionManager._extract_scalar(record.get("orderKind")),
            DataSubscriptionManager._extract_scalar(record.get("type")),
        ]
        for value in text_fields:
            text = str(value or "").strip().lower()
            if "cancel" in text or text == "c":
                return True
        return False

    @staticmethod
    def _map_entrust_direction_side(entrust_direction: Optional[int]) -> str:
        if entrust_direction == 1:
            return "BUY"
        if entrust_direction == 2:
            return "SELL"
        if entrust_direction == 3:
            return "CANCEL_BUY"
        if entrust_direction == 4:
            return "CANCEL_SELL"
        return ""

    @staticmethod
    def _extract_optional_int(value) -> Optional[int]:
        scalar = DataSubscriptionManager._extract_scalar(value)
        if scalar in (None, ""):
            return None
        try:
            return int(scalar)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_string_scalar(value) -> str:
        scalar = DataSubscriptionManager._extract_scalar(value)
        if scalar is None:
            return ""
        return str(scalar)

    @staticmethod
    def _extract_scalar(value, default=None):
        if value is None:
            return default

        value = DataSubscriptionManager._to_python_sequence(value)

        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if not value:
                return default
            last_value = value[-1]
            if isinstance(last_value, Sequence) and not isinstance(last_value, (str, bytes, bytearray)):
                return DataSubscriptionManager._extract_scalar(last_value, default)
            return last_value if last_value is not None else default

        return value

    @staticmethod
    def _extract_book_values(value, cast_type=float, limit: Optional[int] = 5) -> list:
        if value is None:
            return []

        value = DataSubscriptionManager._to_python_sequence(value)

        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            scalar = DataSubscriptionManager._extract_scalar(value)
            if scalar is None:
                return []
            try:
                return [cast_type(scalar)]
            except (TypeError, ValueError):
                return []

        if value and isinstance(value[0], Sequence) and not isinstance(value[0], (str, bytes, bytearray)):
            value = value[-1]

        result = []
        items = list(value) if limit is None else list(value)[:limit]
        for item in items:
            scalar = DataSubscriptionManager._extract_scalar(item)
            if scalar is None:
                continue
            try:
                result.append(cast_type(scalar))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _to_python_sequence(value):
        if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
            try:
                return value.tolist()
            except Exception:
                return value
        return value

    @staticmethod
    def _extract_indexed_value(value, index: int):
        value = DataSubscriptionManager._to_python_sequence(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if not value:
                return None
            if index >= len(value):
                return value[-1]
            current = value[index]
            current = DataSubscriptionManager._to_python_sequence(current)
            if isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
                return current[-1] if current else None
            return current
        return value

    @staticmethod
    def _to_xt(code: str) -> str:
        code = str(code).strip().zfill(6)
        if code.startswith(("6", "5")):
            return f"{code}.SH"
        if code.startswith(("8", "4", "9")):
            return f"{code}.BJ"
        return f"{code}.SZ"


__all__ = ["DataSubscriptionManager"]
