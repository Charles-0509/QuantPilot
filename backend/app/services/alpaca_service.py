from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
    TrailingStopOrderRequest,
)
from alpaca.trading.stream import TradingStream

from app.config import Settings

logger = logging.getLogger(__name__)


def serialize_model(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [serialize_model(item) for item in value]
    if isinstance(value, dict):
        return {str(key): serialize_model(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return serialize_model(value.model_dump())
    if hasattr(value, "dict"):
        return serialize_model(value.dict())
    return str(value)


class AlpacaService:
    """Thin adapter that is permanently locked to Alpaca paper trading."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.configured = False
        self.api_key_id = ""
        self.api_secret_key = ""
        self.feed = "iex"
        self.source = "none"
        self.configuration_updated_at: datetime | None = None
        self.historical: StockHistoricalDataClient | None = None
        self.trading: TradingClient | None = None
        self._data_stream: StockDataStream | None = None
        self._trade_stream: TradingStream | None = None
        self._stream_threads: list[threading.Thread] = []
        self._stream_symbols: tuple[str, ...] = ()
        self._bars_cache: OrderedDict[tuple[Any, ...], tuple[float, dict[str, pd.DataFrame]]] = OrderedDict()
        self._bars_cache_lock = threading.Lock()
        self.configure_from_env()

    @staticmethod
    def _credentials_valid(api_key_id: str, api_secret_key: str) -> bool:
        key = api_key_id.strip()
        secret = api_secret_key.strip()
        return bool(
            key
            and secret
            and not key.lower().startswith("your_")
            and not secret.lower().startswith("your_")
        )

    @staticmethod
    def validate_credentials(api_key_id: str, api_secret_key: str) -> None:
        """Verify a credential pair against the permanently paper-only endpoint."""
        TradingClient(api_key_id, api_secret_key, paper=True).get_account()

    def configure(
        self,
        api_key_id: str,
        api_secret_key: str,
        *,
        feed: str = "iex",
        source: str = "web",
        updated_at: datetime | None = None,
    ) -> None:
        if feed.lower() != "iex":
            raise ValueError("当前版本仅支持 IEX 免费行情")
        self.stop_streams()
        self.api_key_id = api_key_id.strip()
        self.api_secret_key = api_secret_key.strip()
        self.feed = "iex"
        self.source = source if source in {"web", "env"} else "none"
        self.configuration_updated_at = updated_at
        self.configured = self._credentials_valid(self.api_key_id, self.api_secret_key)
        self.historical = None
        self.trading = None
        with self._bars_cache_lock:
            self._bars_cache.clear()
        if self.configured:
            self.historical = StockHistoricalDataClient(self.api_key_id, self.api_secret_key)
            self.trading = TradingClient(self.api_key_id, self.api_secret_key, paper=True)

    def clear_configuration(self) -> None:
        self.stop_streams()
        self.api_key_id = ""
        self.api_secret_key = ""
        self.feed = "iex"
        self.source = "none"
        self.configuration_updated_at = None
        self.configured = False
        self.historical = None
        self.trading = None
        with self._bars_cache_lock:
            self._bars_cache.clear()

    def configure_from_env(self) -> None:
        if self.settings.alpaca_configured:
            self.configure(
                self.settings.apca_api_key_id,
                self.settings.apca_api_secret_key,
                feed="iex",
                source="env",
            )
        else:
            self.clear_configuration()

    def connection_config(self) -> dict[str, Any]:
        hint = f"...{self.api_key_id[-4:]}" if self.configured else None
        return {
            "configured": self.configured,
            "paper": True,
            "source": self.source,
            "api_key_hint": hint,
            "feed": self.feed,
            "updated_at": self.configuration_updated_at,
        }

    def require_connection(self) -> None:
        if not self.configured or self.trading is None or self.historical is None:
            raise RuntimeError("尚未配置 Alpaca Paper API 密钥")

    def connection_status(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "configured": False,
                "connected": False,
                "paper": True,
                "feed": self.feed,
                "source": self.source,
                "message": "请在设置页填写 Alpaca Paper API 密钥",
            }
        try:
            account = self.get_account()
            return {
                "configured": True,
                "connected": True,
                "paper": True,
                "feed": self.feed,
                "source": self.source,
                "account_status": account.get("status"),
                "message": "Alpaca 模拟盘连接正常",
            }
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Alpaca Paper connection check failed: %s", type(exc).__name__)
            return {
                "configured": True,
                "connected": False,
                "paper": True,
                "feed": self.feed,
                "source": self.source,
                "message": "无法连接 Alpaca Paper，请在设置中检查密钥和网络",
            }

    def get_account(self) -> dict[str, Any]:
        self.require_connection()
        return serialize_model(self.trading.get_account())

    def get_positions(self) -> list[dict[str, Any]]:
        self.require_connection()
        return serialize_model(self.trading.get_all_positions())

    def get_orders(self, status: str = "all") -> list[dict[str, Any]]:
        self.require_connection()
        query_status = QueryOrderStatus.ALL if status == "all" else QueryOrderStatus.OPEN
        request = GetOrdersRequest(status=query_status, nested=True, limit=500)
        return serialize_model(self.trading.get_orders(filter=request))

    def get_clock(self) -> dict[str, Any]:
        self.require_connection()
        return serialize_model(self.trading.get_clock())

    def get_asset(self, symbol: str) -> dict[str, Any]:
        self.require_connection()
        return serialize_model(self.trading.get_asset(symbol.upper()))

    @staticmethod
    def timeframe(value: str) -> TimeFrame:
        mapping = {
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "30Min": TimeFrame(30, TimeFrameUnit.Minute),
            "1Hour": TimeFrame.Hour,
            "1Day": TimeFrame.Day,
            "1Min": TimeFrame.Minute,
        }
        if value not in mapping:
            raise ValueError(f"不支持的周期: {value}")
        return mapping[value]

    def get_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
        use_cache: bool = False,
    ) -> dict[str, pd.DataFrame]:
        self.require_connection()
        normalized_symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        cache_key = (
            tuple(normalized_symbols),
            timeframe,
            start.isoformat(),
            end.isoformat(),
            limit,
        )
        if use_cache:
            with self._bars_cache_lock:
                cached = self._bars_cache.get(cache_key)
                if cached is not None and time.monotonic() - cached[0] < 300:
                    self._bars_cache.move_to_end(cache_key)
                    return {symbol: frame.copy() for symbol, frame in cached[1].items()}
        request = StockBarsRequest(
            symbol_or_symbols=normalized_symbols,
            timeframe=self.timeframe(timeframe),
            start=start,
            end=end,
            limit=limit,
            adjustment=Adjustment.ALL,
            feed=DataFeed.IEX,
        )
        response = self.historical.get_stock_bars(request)
        frame = response.df.copy()
        if frame.empty:
            result = {symbol: pd.DataFrame() for symbol in normalized_symbols}
            if use_cache:
                self._store_bars_cache(cache_key, result)
            return result
        result: dict[str, pd.DataFrame] = {}
        if isinstance(frame.index, pd.MultiIndex):
            for symbol in normalized_symbols:
                try:
                    symbol_frame = frame.xs(symbol, level="symbol").copy()
                except (KeyError, ValueError):
                    symbol_frame = pd.DataFrame()
                result[symbol] = self._normalize_bar_frame(symbol_frame)
        else:
            result[normalized_symbols[0]] = self._normalize_bar_frame(frame)
        if use_cache:
            self._store_bars_cache(cache_key, result)
        return result

    def _store_bars_cache(
        self,
        key: tuple[Any, ...],
        frames: dict[str, pd.DataFrame],
    ) -> None:
        with self._bars_cache_lock:
            self._bars_cache[key] = (
                time.monotonic(),
                {symbol: frame.copy() for symbol, frame in frames.items()},
            )
            self._bars_cache.move_to_end(key)
            while len(self._bars_cache) > 8:
                self._bars_cache.popitem(last=False)

    def recent_bars(self, symbols: list[str], timeframe: str, bars: int = 300) -> dict[str, pd.DataFrame]:
        minutes = {"5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60, "1Day": 390}[timeframe]
        calendar_factor = 4 if timeframe != "1Day" else 2
        start = datetime.now(timezone.utc) - timedelta(minutes=minutes * bars * calendar_factor)
        return self.get_bars(symbols, timeframe, start, datetime.now(timezone.utc), limit=bars * len(symbols))

    @staticmethod
    def _normalize_bar_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        result = frame.rename(columns={"trade_count": "trades", "vwap": "vwap"})
        result.index = pd.to_datetime(result.index, utc=True)
        return result[[column for column in ["open", "high", "low", "close", "volume", "trades", "vwap"] if column in result.columns]].sort_index()

    def get_latest_quotes(self, symbols: list[str]) -> dict[str, Any]:
        self.require_connection()
        request = StockLatestQuoteRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
        return serialize_model(self.historical.get_stock_latest_quote(request))

    def submit_entry_order(
        self,
        *,
        symbol: str,
        qty: float | None,
        notional: float | None,
        order_type: str,
        time_in_force: str,
        client_order_id: str,
        limit_price: float | None = None,
        stop_price: float | None = None,
        take_price: float | None = None,
    ) -> dict[str, Any]:
        self.require_connection()
        common: dict[str, Any] = {
            "symbol": symbol,
            "side": OrderSide.BUY,
            "time_in_force": TimeInForce.DAY if time_in_force == "day" else TimeInForce.GTC,
            "client_order_id": client_order_id,
        }
        if qty is not None:
            common["qty"] = round(qty, 6)
        else:
            common["notional"] = round(float(notional or 0), 2)
        if stop_price is not None and take_price is not None:
            common.update(
                {
                    "order_class": OrderClass.BRACKET,
                    "take_profit": TakeProfitRequest(limit_price=round(take_price, 2)),
                    "stop_loss": StopLossRequest(stop_price=round(stop_price, 2)),
                }
            )
        if order_type == "limit":
            request = LimitOrderRequest(limit_price=round(float(limit_price), 2), **common)
        else:
            request = MarketOrderRequest(**common)
        return serialize_model(self.trading.submit_order(order_data=request))

    def submit_trailing_stop(
        self, symbol: str, qty: float, mode: str, value: float, client_order_id: str
    ) -> dict[str, Any]:
        self.require_connection()
        params: dict[str, Any] = {
            "symbol": symbol,
            "qty": round(qty, 6),
            "side": OrderSide.SELL,
            "time_in_force": TimeInForce.GTC,
            "client_order_id": client_order_id,
        }
        if mode == "percent":
            params["trail_percent"] = value
        else:
            params["trail_price"] = value
        return serialize_model(self.trading.submit_order(order_data=TrailingStopOrderRequest(**params)))

    def cancel_all_orders(self) -> list[Any]:
        self.require_connection()
        return serialize_model(self.trading.cancel_orders())

    def cancel_symbol_orders(self, symbol: str) -> None:
        self.require_connection()
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=500)
        for order in self.trading.get_orders(filter=request):
            try:
                self.trading.cancel_order_by_id(order.id)
            except Exception:
                logger.exception("Unable to cancel order %s", getattr(order, "id", "unknown"))

    def close_position(self, symbol: str) -> dict[str, Any]:
        self.require_connection()
        self.cancel_symbol_orders(symbol)
        return serialize_model(self.trading.close_position(symbol))

    def close_all_positions(self) -> list[Any]:
        self.require_connection()
        return serialize_model(self.trading.close_all_positions(cancel_orders=True))

    async def start_streams(
        self,
        symbols: list[str],
        bar_callback: Callable[[dict[str, Any]], Any],
        trade_callback: Callable[[dict[str, Any]], Any],
    ) -> None:
        if not self.configured or not symbols:
            return
        normalized = tuple(sorted(set(symbols)))
        if normalized == self._stream_symbols and self._stream_threads:
            return
        self.stop_streams()
        self._stream_symbols = normalized
        loop = asyncio.get_running_loop()
        self._data_stream = StockDataStream(
            self.api_key_id,
            self.api_secret_key,
            feed=DataFeed.IEX,
        )
        self._trade_stream = TradingStream(
            self.api_key_id,
            self.api_secret_key,
            paper=True,
        )

        async def on_bar(bar: Any) -> None:
            payload = serialize_model(bar)
            result = bar_callback(payload)
            if inspect.isawaitable(result):
                await result

        async def on_trade(update: Any) -> None:
            payload = serialize_model(update)
            result = trade_callback(payload)
            if inspect.isawaitable(result):
                await result

        self._data_stream.subscribe_bars(on_bar, *normalized)
        self._trade_stream.subscribe_trade_updates(on_trade)

        def run_data() -> None:
            try:
                self._data_stream.run()
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Alpaca market stream stopped: %s", exc)

        def run_trades() -> None:
            try:
                self._trade_stream.run()
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Alpaca trade stream stopped: %s", exc)

        for target, name in [(run_data, "alpaca-data"), (run_trades, "alpaca-trades")]:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._stream_threads.append(thread)
        await asyncio.sleep(0)

    def stop_streams(self) -> None:
        for stream in (self._data_stream, self._trade_stream):
            if stream is None:
                continue
            try:
                if hasattr(stream, "stop"):
                    result = stream.stop()
                    if inspect.isawaitable(result):
                        # The stream lives in its own thread; stop_ws is handled by the SDK loop.
                        pass
                elif hasattr(stream, "stop_ws"):
                    stream.stop_ws()
            except Exception:
                logger.debug("Ignoring stream shutdown error", exc_info=True)
        self._data_stream = None
        self._trade_stream = None
        self._stream_threads = []
        self._stream_symbols = ()
