from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import math
import random
import ssl
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from alpaca.common.exceptions import APIError
from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
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
ET = ZoneInfo("America/New_York")


class AlpacaServiceError(RuntimeError):
    """A user-safe Alpaca error that never includes credentials or request headers."""

    def __init__(self, message: str):
        self.user_message = message
        super().__init__(message)


class AlpacaTransientError(AlpacaServiceError):
    """A bounded retry sequence was exhausted."""


class AlpacaCircuitOpenError(AlpacaTransientError):
    def __init__(self, channel: str, retry_after: float):
        self.channel = channel
        self.retry_after = max(0.0, retry_after)
        super().__init__(
            f"Alpaca {channel} 连接暂时不可用，约 {max(1, round(self.retry_after))} 秒后自动重试"
        )


class AlpacaAmbiguousOrderError(AlpacaServiceError):
    """The submit response was lost and Alpaca could not confirm the client order id."""

    def __init__(self, client_order_id: str):
        self.client_order_id = client_order_id
        super().__init__("订单提交结果暂时无法确认，系统将按客户端订单号继续对账")


class _TimeoutSession(requests.Session):
    def __init__(self, connect_timeout: float, read_timeout: float):
        super().__init__()
        self._default_timeout = (connect_timeout, read_timeout)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(method, url, **kwargs)


@dataclass(slots=True)
class _CircuitBreaker:
    channel: str
    threshold: int
    recovery_seconds: float
    monotonic: Callable[[], float]
    consecutive_failures: int = 0
    open_until: float = 0.0
    half_open_probe: bool = False
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    last_error_category: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def before_call(self) -> None:
        now = self.monotonic()
        with self.lock:
            if self.open_until <= 0:
                return
            if now < self.open_until:
                raise AlpacaCircuitOpenError(self.channel, self.open_until - now)
            if self.half_open_probe:
                raise AlpacaCircuitOpenError(self.channel, 1.0)
            self.half_open_probe = True

    def success(self) -> None:
        with self.lock:
            self.consecutive_failures = 0
            self.open_until = 0.0
            self.half_open_probe = False
            self.last_success_at = datetime.now(timezone.utc)
            self.last_error = None
            self.last_error_category = None

    def reset(self) -> None:
        with self.lock:
            self.consecutive_failures = 0
            self.open_until = 0.0
            self.half_open_probe = False
            self.last_success_at = None
            self.last_failure_at = None
            self.last_error = None
            self.last_error_category = None

    def failure(self, message: str, category: str) -> bool:
        """Record one exhausted logical request; return True when the circuit opens."""
        now = self.monotonic()
        with self.lock:
            was_open = self.open_until > now
            self.consecutive_failures += 1
            self.last_failure_at = datetime.now(timezone.utc)
            self.last_error = message
            self.last_error_category = category
            if self.half_open_probe or self.consecutive_failures >= self.threshold:
                self.open_until = now + self.recovery_seconds
                self.half_open_probe = False
            return not was_open and self.open_until > now

    def reachable(self) -> bool:
        with self.lock:
            return self.open_until <= self.monotonic()

    def cache_allowed(self) -> bool:
        """Reject an active circuit and force a real half-open probe after cooldown."""
        now = self.monotonic()
        with self.lock:
            if self.open_until <= 0:
                return True
            if now < self.open_until:
                raise AlpacaCircuitOpenError(self.channel, self.open_until - now)
            return False

    def snapshot(self) -> dict[str, Any]:
        now = self.monotonic()
        with self.lock:
            retry_after = max(0.0, self.open_until - now)
            state = "open" if retry_after > 0 else ("half_open" if self.half_open_probe else "closed")
            return {
                "state": state,
                "consecutive_failures": self.consecutive_failures,
                "retry_after_seconds": round(retry_after, 1),
                "last_success_at": self.last_success_at,
                "last_failure_at": self.last_failure_at,
                "last_error": self.last_error,
                "last_error_category": self.last_error_category,
            }


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

    def __init__(
        self,
        settings: Settings,
        *,
        use_env: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ):
        self.settings = settings
        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter
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
        self._stream_generation = 0
        self._stream_lock = threading.RLock()
        self._trading_lock = threading.RLock()
        self._data_lock = threading.RLock()
        self._health_lock = threading.Lock()
        self._operation_failures: dict[str, dict[str, dict[str, Any]]] = {
            "trading": {},
            "data": {},
        }
        self._trading_cache: dict[str, tuple[float, Any]] = {}
        self._recent_bars_cache: dict[
            tuple[tuple[str, ...], str, int], tuple[float, dict[str, pd.DataFrame]]
        ] = {}
        self._calendar_closes: dict[date, datetime] = {}
        self._calendar_coverage: list[tuple[date, date]] = []
        self._bars_cache: OrderedDict[tuple[Any, ...], tuple[float, dict[str, pd.DataFrame]]] = OrderedDict()
        self._bars_cache_lock = threading.Lock()
        self._trading_circuit = _CircuitBreaker(
            "Trading API",
            settings.alpaca_circuit_failure_threshold,
            settings.alpaca_circuit_recovery_seconds,
            monotonic,
        )
        self._data_circuit = _CircuitBreaker(
            "Market Data API",
            settings.alpaca_circuit_failure_threshold,
            settings.alpaca_circuit_recovery_seconds,
            monotonic,
        )
        if use_env:
            self.configure_from_env()
        else:
            self.clear_configuration()

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
        client = TradingClient(api_key_id, api_secret_key, paper=True)
        AlpacaService._install_session(client, 5.0, 6.0)
        try:
            for attempt in range(3):
                try:
                    client.get_account()
                    return
                except Exception as exc:
                    if not AlpacaService._is_transient(exc) or attempt == 2:
                        raise AlpacaService._safe_exception(exc) from exc
                    time.sleep(0.5 * (2**attempt))
        finally:
            AlpacaService._close_client(client)

    @staticmethod
    def _install_session(client: Any, connect_timeout: float, read_timeout: float) -> None:
        """Install bounded timeouts and disable the SDK's nested fixed-delay retry loop."""
        if hasattr(client, "_session"):
            old_session = client._session
            client._session = _TimeoutSession(connect_timeout, read_timeout)
            try:
                old_session.close()
            except Exception:
                pass
        if hasattr(client, "_retry"):
            client._retry = 0

    @staticmethod
    def _close_client(client: Any | None) -> None:
        session = getattr(client, "_session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def _prepare_clients(self) -> None:
        if self.trading is not None:
            self._install_session(
                self.trading,
                self.settings.alpaca_connect_timeout_seconds,
                self.settings.alpaca_trading_read_timeout_seconds,
            )
        if self.historical is not None:
            self._install_session(
                self.historical,
                self.settings.alpaca_connect_timeout_seconds,
                self.settings.alpaca_data_read_timeout_seconds,
            )

    def _clear_caches(self) -> None:
        self._trading_cache.clear()
        self._recent_bars_cache.clear()
        self._calendar_closes.clear()
        self._calendar_coverage.clear()
        with self._bars_cache_lock:
            self._bars_cache.clear()

    def _clear_operation_health(self) -> None:
        with self._health_lock:
            self._operation_failures = {"trading": {}, "data": {}}

    def _mark_operation_failure(
        self, channel: str, operation: str, exc: BaseException
    ) -> None:
        safe = self._safe_exception(exc)
        with self._health_lock:
            self._operation_failures[channel][operation] = {
                "at": datetime.now(timezone.utc),
                "category": self._error_category(exc),
                "message": str(safe),
            }

    def _mark_operation_success(self, channel: str, operation: str) -> None:
        with self._health_lock:
            self._operation_failures[channel].pop(operation, None)

    def _operation_failed(self, channel: str, operation: str) -> bool:
        with self._health_lock:
            return operation in self._operation_failures[channel]

    def _operation_health_snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self._health_lock:
            return copy.deepcopy(self._operation_failures)

    def _reset_health(self) -> None:
        self._trading_circuit.reset()
        self._data_circuit.reset()
        self._clear_operation_health()

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
        # When both channels are needed, always acquire data before trading.
        # recent_bars follows the same order while enriching bars with Alpaca's
        # official early-close calendar, avoiding lock inversion on reconfigure.
        with self._data_lock, self._trading_lock:
            old_trading = self.trading
            old_historical = self.historical
            self.api_key_id = api_key_id.strip()
            self.api_secret_key = api_secret_key.strip()
            self.feed = "iex"
            self.source = source if source in {"web", "env"} else "none"
            self.configuration_updated_at = updated_at
            self.configured = self._credentials_valid(self.api_key_id, self.api_secret_key)
            self.historical = None
            self.trading = None
            self._clear_caches()
            self._reset_health()
            if self.configured:
                self.historical = StockHistoricalDataClient(self.api_key_id, self.api_secret_key)
                self.trading = TradingClient(self.api_key_id, self.api_secret_key, paper=True)
                self._prepare_clients()
            self._close_client(old_trading)
            self._close_client(old_historical)

    def clear_configuration(self) -> None:
        self.stop_streams()
        with self._data_lock, self._trading_lock:
            old_trading = self.trading
            old_historical = self.historical
            self.api_key_id = ""
            self.api_secret_key = ""
            self.feed = "iex"
            self.source = "none"
            self.configuration_updated_at = None
            self.configured = False
            self.historical = None
            self.trading = None
            self._clear_caches()
            self._reset_health()
            self._close_client(old_trading)
            self._close_client(old_historical)

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

    @staticmethod
    def _exception_chain(exc: BaseException) -> list[BaseException]:
        result: list[BaseException] = []
        current: BaseException | None = exc
        while current is not None and current not in result:
            result.append(current)
            current = current.__cause__ or current.__context__
        return result

    @staticmethod
    def _http_status(exc: BaseException) -> int | None:
        for item in AlpacaService._exception_chain(exc):
            if isinstance(item, APIError):
                try:
                    return item.status_code
                except Exception:
                    return None
            response = getattr(item, "response", None)
            status = getattr(response, "status_code", None)
            if isinstance(status, int):
                return status
        return None

    @staticmethod
    def _is_transport_error(exc: BaseException) -> bool:
        types = (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
            requests.exceptions.JSONDecodeError,
            ssl.SSLError,
            ConnectionResetError,
            BrokenPipeError,
            TimeoutError,
        )
        return any(isinstance(item, types) for item in AlpacaService._exception_chain(exc))

    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        status = AlpacaService._http_status(exc)
        return AlpacaService._is_transport_error(exc) or status == 429 or bool(
            status is not None and 500 <= status <= 599
        )

    @staticmethod
    def _error_category(exc: BaseException) -> str:
        status = AlpacaService._http_status(exc)
        chain = AlpacaService._exception_chain(exc)
        if any(
            isinstance(item, (requests.exceptions.Timeout, TimeoutError)) for item in chain
        ):
            return "timeout"
        if any(isinstance(item, (requests.exceptions.SSLError, ssl.SSLError)) for item in chain):
            return "tls"
        if any(isinstance(item, requests.exceptions.ConnectionError) for item in chain):
            return "connection"
        if status == 429:
            return "rate_limit"
        if status is not None and 500 <= status <= 599:
            return "upstream_5xx"
        if status in {401, 403}:
            return "authentication"
        if status is not None:
            return "api_rejection"
        return "unknown"

    @staticmethod
    def _safe_exception(exc: BaseException) -> Exception:
        if isinstance(exc, AlpacaServiceError):
            return exc
        status = AlpacaService._http_status(exc)
        if AlpacaService._is_transport_error(exc):
            if any(
                isinstance(item, (requests.exceptions.Timeout, TimeoutError))
                for item in AlpacaService._exception_chain(exc)
            ):
                return AlpacaTransientError("Alpaca 请求超时")
            return AlpacaTransientError("Alpaca 安全连接意外中断")
        if status in {401, 403}:
            return AlpacaServiceError("Alpaca Paper API 认证失败，请检查密钥")
        if status is not None:
            return AlpacaServiceError(f"Alpaca API 拒绝请求（HTTP {status}）")
        return AlpacaServiceError("Alpaca 请求失败")

    @staticmethod
    def _retry_after_seconds(exc: BaseException) -> float:
        for item in AlpacaService._exception_chain(exc):
            response = getattr(item, "response", None)
            headers = getattr(response, "headers", None)
            if not headers:
                continue
            raw = headers.get("Retry-After")
            try:
                return min(30.0, max(0.0, float(raw)))
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _backoff(self, attempt: int) -> float:
        ceiling = min(
            self.settings.alpaca_retry_max_seconds,
            self.settings.alpaca_retry_base_seconds * (2**attempt),
        )
        if ceiling <= 0:
            return 0.0
        return self._jitter(ceiling / 2, ceiling)

    def _refresh_session(self, channel: str) -> None:
        client = self.trading if channel == "trading" else self.historical
        if client is None or not hasattr(client, "_session"):
            return
        read_timeout = (
            self.settings.alpaca_trading_read_timeout_seconds
            if channel == "trading"
            else self.settings.alpaca_data_read_timeout_seconds
        )
        old_session = client._session
        client._session = _TimeoutSession(
            self.settings.alpaca_connect_timeout_seconds,
            read_timeout,
        )
        try:
            old_session.close()
        except Exception:
            pass

    def _resilient_locked(
        self,
        channel: str,
        operation: str,
        callback: Callable[[], Any],
    ) -> Any:
        circuit = self._trading_circuit if channel == "trading" else self._data_circuit
        circuit.before_call()
        for attempt in range(self.settings.alpaca_retry_attempts):
            try:
                result = callback()
            except Exception as exc:
                if not self._is_transient(exc):
                    # A definite API response proves that the network path is reachable.
                    safe = self._safe_exception(exc)
                    if self._http_status(exc) in {401, 403}:
                        circuit.success()
                        self._mark_operation_failure(channel, operation, exc)
                    else:
                        circuit.success()
                        self._mark_operation_success(channel, operation)
                    raise safe from exc
                if attempt + 1 < self.settings.alpaca_retry_attempts:
                    if self._is_transport_error(exc):
                        self._refresh_session(channel)
                    delay = max(
                        self._backoff(attempt), self._retry_after_seconds(exc)
                    )
                    logger.info(
                        "Retrying Alpaca %s operation %s after %s (attempt %s/%s)",
                        channel,
                        operation,
                        type(exc).__name__,
                        attempt + 2,
                        self.settings.alpaca_retry_attempts,
                    )
                    self._sleep(delay)
                    continue
                safe = self._safe_exception(exc)
                self._mark_operation_failure(channel, operation, exc)
                opened = circuit.failure(str(safe), self._error_category(exc))
                if opened:
                    logger.warning("Alpaca %s circuit opened after %s", channel, operation)
                raise safe from exc
            else:
                circuit.success()
                self._mark_operation_success(channel, operation)
                return result
        raise AssertionError("unreachable")

    def _cached_trading_read(
        self,
        key: str,
        callback: Callable[[], Any],
        *,
        ttl: float | None = None,
        force_refresh: bool = False,
        operation: str | None = None,
    ) -> Any:
        with self._trading_lock:
            self.require_connection()
            now = self._monotonic()
            cache_ttl = self.settings.alpaca_read_cache_seconds if ttl is None else ttl
            cached = self._trading_cache.get(key)
            operation_key = operation or key
            cache_allowed = self._trading_circuit.cache_allowed()
            if (
                not force_refresh
                and cache_allowed
                and not self._operation_failed("trading", operation_key)
                and cached is not None
                and now - cached[0] < cache_ttl
            ):
                return copy.deepcopy(cached[1])
            value = serialize_model(
                self._resilient_locked("trading", operation_key, callback)
            )
            self._trading_cache[key] = (self._monotonic(), value)
            return copy.deepcopy(value)

    def _data_read(self, operation: str, callback: Callable[[], Any]) -> Any:
        with self._data_lock:
            self.require_connection()
            return self._resilient_locked("data", operation, callback)

    def _invalidate_trading_cache(self) -> None:
        self._trading_cache.clear()

    def connection_status(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "configured": False,
                "connected": False,
                "state": "unconfigured",
                "paper": True,
                "feed": self.feed,
                "source": self.source,
                "message": "请在设置页填写 Alpaca Paper API 密钥",
                "consecutive_failures": 0,
                "last_success_at": None,
                "last_failure_at": None,
                "retry_at": None,
                "last_error_category": None,
            }
        trading_health = self._trading_circuit.snapshot()
        data_health = self._data_circuit.snapshot()
        operation_health = self._operation_health_snapshot()
        operation_failures = [
            {"channel": channel, "operation": operation, **details}
            for channel, failures in operation_health.items()
            for operation, details in failures.items()
        ]
        if trading_health["state"] == "open" or data_health["state"] == "open":
            state = "circuit_open"
        elif (
            trading_health["last_success_at"] is None
            and trading_health["last_failure_at"] is None
        ):
            state = "unknown"
        elif operation_failures:
            state = "degraded"
        else:
            state = "connected"
        connected = state == "connected"
        cached_account = self._trading_cache.get("account")
        account_status = cached_account[1].get("status") if cached_account else None
        if state == "circuit_open":
            message = "Alpaca 模拟盘连接暂时中断，系统将自动重试"
        elif state == "unknown":
            message = "Alpaca 模拟盘已配置，等待首次连接检测"
        elif state == "degraded":
            message = "Alpaca 模拟盘连接不稳定，系统正在自动恢复"
        else:
            message = "Alpaca 模拟盘连接正常"
        retry_at = None
        retry_after = max(
            trading_health["retry_after_seconds"], data_health["retry_after_seconds"]
        )
        if retry_after > 0:
            retry_at = datetime.now(timezone.utc) + timedelta(
                seconds=retry_after
            )
        latest_operation_failure = max(
            operation_failures,
            key=lambda item: item["at"],
            default=None,
        )
        circuit_failures = [
            item
            for item in (trading_health, data_health)
            if item["last_failure_at"] is not None
        ]
        latest_circuit_failure = max(
            circuit_failures,
            key=lambda item: item["last_failure_at"],
            default=None,
        )
        last_failure_at = (
            latest_operation_failure["at"]
            if latest_operation_failure is not None
            else (
                latest_circuit_failure["last_failure_at"]
                if latest_circuit_failure is not None
                else None
            )
        )
        last_error_category = (
            latest_operation_failure["category"]
            if latest_operation_failure is not None
            else (
                latest_circuit_failure["last_error_category"]
                if latest_circuit_failure is not None
                else None
            )
        )
        return {
            "configured": True,
            "connected": connected,
            "state": state,
            "paper": True,
            "feed": self.feed,
            "source": self.source,
            "account_status": account_status,
            "message": message,
            "consecutive_failures": max(
                trading_health["consecutive_failures"],
                data_health["consecutive_failures"],
                len(operation_failures),
            ),
            "last_success_at": trading_health["last_success_at"],
            "last_failure_at": last_failure_at,
            "retry_at": retry_at,
            "last_error_category": last_error_category,
            "health": {
                "trading": trading_health,
                "data": data_health,
                "failed_operations": operation_failures,
            },
            "streams_healthy": self.streams_healthy(),
        }

    def probe_connection(self) -> dict[str, Any]:
        """Explicitly probe Alpaca; connection_status itself never performs I/O."""
        self._cached_trading_read(
            "account", lambda: self.trading.get_account(), force_refresh=True
        )
        return self.connection_status()

    def get_account(self) -> dict[str, Any]:
        return self._cached_trading_read("account", lambda: self.trading.get_account())

    def get_positions(self) -> list[dict[str, Any]]:
        return self._cached_trading_read(
            "positions", lambda: self.trading.get_all_positions()
        )

    def get_positions_fresh(self) -> list[dict[str, Any]]:
        return self._cached_trading_read(
            "positions",
            lambda: self.trading.get_all_positions(),
            force_refresh=True,
        )

    def get_orders(self, status: str = "all") -> list[dict[str, Any]]:
        query_status = QueryOrderStatus.ALL if status == "all" else QueryOrderStatus.OPEN
        request = GetOrdersRequest(status=query_status, nested=True, limit=500)
        if status == "open":
            return self._cached_trading_read(
                "orders:open", lambda: self.trading.get_orders(filter=request)
            )
        with self._trading_lock:
            self.require_connection()
            return serialize_model(
                self._resilient_locked(
                    "trading", "orders:all", lambda: self.trading.get_orders(filter=request)
                )
            )

    def get_open_orders_fresh(self) -> list[dict[str, Any]]:
        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            nested=True,
            limit=500,
        )
        return self._cached_trading_read(
            "orders:open",
            lambda: self.trading.get_orders(filter=request),
            force_refresh=True,
        )

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        """Return a confirmed order, None for a confirmed 404, or raise on uncertainty."""
        with self._trading_lock:
            self.require_connection()

            def lookup() -> Any | None:
                try:
                    return self.trading.get_order_by_client_id(client_order_id)
                except Exception as exc:
                    if self._http_status(exc) == 404:
                        return None
                    raise

            order = self._resilient_locked(
                "trading", "order_by_client_id", lookup
            )
            # A definite found/404 resolves the uncertainty created by the
            # original submit attempt. Transport/circuit errors never reach here.
            self._mark_operation_success("trading", "submit_order")
            return serialize_model(order) if order is not None else None

    def get_clock(self) -> dict[str, Any]:
        return self._cached_trading_read("clock", lambda: self.trading.get_clock())

    def get_asset(self, symbol: str) -> dict[str, Any]:
        normalized = symbol.upper()
        return self._cached_trading_read(
            f"asset:{normalized}",
            lambda: self.trading.get_asset(normalized),
            ttl=self.settings.alpaca_asset_cache_seconds,
            operation="asset",
        )

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

    def _session_close(self, session_date: date) -> datetime:
        return self._calendar_closes.get(
            session_date,
            datetime.combine(session_date, datetime_time(16, 0), tzinfo=ET),
        )

    def _ensure_calendar_closes(self, start_date: date, end_date: date) -> None:
        if any(
            covered_start <= start_date and covered_end >= end_date
            for covered_start, covered_end in self._calendar_coverage
        ):
            return
        if self.trading is None or not hasattr(self.trading, "get_calendar"):
            return
        request = GetCalendarRequest(start=start_date, end=end_date)
        with self._trading_lock:
            self.require_connection()
            calendars = self._resilient_locked(
                "trading",
                "calendar",
                lambda: self.trading.get_calendar(filters=request),
            )
        for session in calendars:
            session_date = session.date
            close = session.close
            if close.tzinfo is None:
                close = close.replace(tzinfo=ET)
            else:
                close = close.astimezone(ET)
            self._calendar_closes[session_date] = close
        self._calendar_coverage.append((start_date, end_date))

    def get_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
        use_cache: bool = False,
    ) -> dict[str, pd.DataFrame]:
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
                if cached is not None and self._monotonic() - cached[0] < 300:
                    self._bars_cache.move_to_end(cache_key)
                    return {symbol: frame.copy() for symbol, frame in cached[1].items()}
        request_limit = None
        if limit is not None and len(normalized_symbols) == 1:
            if timeframe == "1Hour":
                request_limit = limit * 2
            elif timeframe != "1Day":
                request_limit = limit
        request = StockBarsRequest(
            symbol_or_symbols=normalized_symbols,
            timeframe=self.timeframe(
                "30Min" if timeframe in {"1Hour", "1Day"} else timeframe
            ),
            start=start,
            end=end,
            limit=request_limit,
            sort=Sort.DESC if request_limit is not None else Sort.ASC,
            adjustment=Adjustment.ALL,
            feed=DataFeed.IEX,
        )
        response = self._data_read(
            "stock_bars", lambda: self.historical.get_stock_bars(request)
        )
        frame = response.df.copy()
        if frame.empty:
            result = {symbol: pd.DataFrame() for symbol in normalized_symbols}
            if use_cache:
                self._store_bars_cache(cache_key, result)
            return result
        if timeframe in {"1Hour", "1Day"}:
            self._ensure_calendar_closes(
                start.astimezone(ET).date(),
                end.astimezone(ET).date(),
            )
        result: dict[str, pd.DataFrame] = {}
        if isinstance(frame.index, pd.MultiIndex):
            for symbol in normalized_symbols:
                try:
                    symbol_frame = frame.xs(symbol, level="symbol").copy()
                except (KeyError, ValueError):
                    symbol_frame = pd.DataFrame()
                prepared = self._prepare_bar_frame(symbol_frame, timeframe, end)
                result[symbol] = prepared.tail(limit) if limit is not None else prepared
        else:
            prepared = self._prepare_bar_frame(frame, timeframe, end)
            result[normalized_symbols[0]] = prepared.tail(limit) if limit is not None else prepared
        for symbol in normalized_symbols:
            result.setdefault(symbol, pd.DataFrame())
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
                self._monotonic(),
                {symbol: frame.copy() for symbol, frame in frames.items()},
            )
            self._bars_cache.move_to_end(key)
            while len(self._bars_cache) > 8:
                self._bars_cache.popitem(last=False)

    def recent_bars(
        self, symbols: list[str], timeframe: str, bars: int = 300
    ) -> dict[str, pd.DataFrame]:
        # The data lock is re-entrant because get_bars() uses the same guarded
        # client. Holding it across the cache miss makes identical concurrent
        # requests true single-flight instead of two serialized upstream calls.
        with self._data_lock:
            return self._recent_bars_locked(symbols, timeframe, bars)

    def _recent_bars_locked(
        self, symbols: list[str], timeframe: str, bars: int
    ) -> dict[str, pd.DataFrame]:
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        cache_key = (normalized, timeframe, bars)
        cached_snapshot: dict[str, pd.DataFrame] | None = None
        cache_allowed = self._data_circuit.cache_allowed()
        cached = self._recent_bars_cache.get(cache_key)
        cache_ttl = (
            self.settings.alpaca_daily_bars_cache_seconds
            if timeframe == "1Day"
            else self.settings.alpaca_recent_bars_cache_seconds
        )
        if (
            cache_allowed
            and not self._operation_failed("data", "stock_bars")
            and cached is not None
            and self._monotonic() - cached[0] < cache_ttl
        ):
            return {symbol: frame.copy() for symbol, frame in cached[1].items()}
        if cached is not None:
            cached_snapshot = {
                symbol: frame.copy() for symbol, frame in cached[1].items()
            }
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self._recent_window_days(timeframe, bars))
        if timeframe == "1Day" and cached_snapshot:
            latest = [
                frame.index[-1]
                for frame in cached_snapshot.values()
                if not frame.empty
            ]
            if latest:
                incremental_start = min(latest).to_pydatetime() - timedelta(days=10)
                start = max(start, incremental_start)
        frames = self.get_bars(list(normalized), timeframe, start, end, limit=None)
        result: dict[str, pd.DataFrame] = {}
        for symbol in normalized:
            fresh = frames.get(symbol, pd.DataFrame())
            previous = (
                cached_snapshot.get(symbol, pd.DataFrame())
                if cached_snapshot is not None
                else pd.DataFrame()
            )
            if timeframe == "1Day" and not previous.empty:
                merged = pd.concat([previous, fresh]).sort_index()
                merged = merged[~merged.index.duplicated(keep="last")]
            else:
                merged = fresh
            merged.attrs["session_closes"] = {
                **(previous.attrs.get("session_closes", {}) if not previous.empty else {}),
                **(fresh.attrs.get("session_closes", {}) if not fresh.empty else {}),
            }
            result[symbol] = merged.tail(bars).copy()
        self._recent_bars_cache[cache_key] = (
            self._monotonic(),
            {symbol: frame.copy() for symbol, frame in result.items()},
        )
        if len(self._recent_bars_cache) > 32:
            oldest = min(
                self._recent_bars_cache,
                key=lambda key: self._recent_bars_cache[key][0],
            )
            self._recent_bars_cache.pop(oldest, None)
        return result

    @staticmethod
    def _recent_window_days(timeframe: str, bars: int) -> int:
        if timeframe == "1Day":
            trading_days = bars
        else:
            minutes = {"5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60}[timeframe]
            bars_per_session = math.ceil(390 / minutes)
            trading_days = math.ceil(bars / bars_per_session)
        # 7/5 accounts for weekends; the additional 25% and ten days cover US holidays.
        return max(14, math.ceil(trading_days * 7 / 5 * 1.25) + 10)

    def _prepare_bar_frame(
        self, frame: pd.DataFrame, timeframe: str, end: datetime
    ) -> pd.DataFrame:
        normalized = self._normalize_bar_frame(frame)
        if normalized.empty:
            return normalized
        source_minutes = 30 if timeframe in {"1Hour", "1Day"} else {
            "5Min": 5,
            "15Min": 15,
            "30Min": 30,
        }[timeframe]
        end_timestamp = pd.Timestamp(end)
        if end_timestamp.tzinfo is None:
            end_timestamp = end_timestamp.tz_localize("UTC")
        else:
            end_timestamp = end_timestamp.tz_convert("UTC")
        local_index = normalized.index.tz_convert(ET)
        minute_of_day = local_index.hour * 60 + local_index.minute
        session_closes = pd.DatetimeIndex(
            [
                pd.Timestamp(self._session_close(session_date)).tz_convert("UTC")
                for session_date in local_index.date
            ]
        )
        bar_ends = normalized.index + pd.to_timedelta(source_minutes, unit="m")
        regular = (
            (local_index.weekday < 5)
            & (minute_of_day >= 9 * 60 + 30)
            & (bar_ends <= session_closes)
            & (bar_ends <= end_timestamp)
        )
        normalized = normalized.loc[regular]
        if normalized.empty:
            return normalized
        if timeframe == "1Hour":
            prepared = self._aggregate_regular_hour_bars(normalized)
        elif timeframe == "1Day":
            prepared = self._aggregate_regular_daily_bars(normalized, end_timestamp)
        else:
            prepared = normalized
        prepared.attrs["session_closes"] = {
            session_date.isoformat(): close.isoformat()
            for session_date, close in self._calendar_closes.items()
            if normalized.index[0].tz_convert(ET).date()
            <= session_date
            <= normalized.index[-1].tz_convert(ET).date()
        }
        return prepared

    def _aggregate_regular_daily_bars(
        self, frame: pd.DataFrame, end_timestamp: pd.Timestamp
    ) -> pd.DataFrame:
        working = frame.copy()
        local_index = working.index.tz_convert(ET)
        working["_session_date"] = local_index.date
        end_date = end_timestamp.tz_convert(ET).date()
        rows: list[dict[str, Any]] = []
        timestamps: list[pd.Timestamp] = []
        for session_date, group in working.groupby("_session_date", sort=True):
            group = group.sort_index()
            actual_end = group.index[-1].tz_convert(ET).to_pydatetime() + timedelta(
                minutes=30
            )
            regular_close = self._session_close(session_date)
            if actual_end < regular_close:
                continue
            if session_date == end_date and end_timestamp < pd.Timestamp(
                regular_close
            ).tz_convert("UTC"):
                continue
            volume = float(group["volume"].sum()) if "volume" in group else 0.0
            row: dict[str, Any] = {
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
                "volume": volume,
            }
            if "trades" in group:
                row["trades"] = float(group["trades"].sum())
            if "vwap" in group:
                row["vwap"] = (
                    float((group["vwap"] * group["volume"]).sum() / volume)
                    if volume > 0
                    else float(group["vwap"].iloc[-1])
                )
            rows.append(row)
            session_midnight = datetime.combine(
                session_date, datetime_time(0, 0), tzinfo=ET
            )
            timestamps.append(pd.Timestamp(session_midnight).tz_convert("UTC"))
        if not rows:
            return pd.DataFrame(
                columns=[
                    column
                    for column in [
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "trades",
                        "vwap",
                    ]
                    if column in frame.columns
                ]
            )
        return pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps)).sort_index()

    def _aggregate_regular_hour_bars(self, frame: pd.DataFrame) -> pd.DataFrame:
        working = frame.copy()
        local_index = working.index.tz_convert(ET)
        minute_of_day = local_index.hour * 60 + local_index.minute
        working["_session_date"] = local_index.date
        working["_bucket"] = ((minute_of_day - (9 * 60 + 30)) // 60).astype(int)
        rows: list[dict[str, Any]] = []
        timestamps: list[pd.Timestamp] = []
        for (session_date, bucket), group in working.groupby(
            ["_session_date", "_bucket"], sort=True
        ):
            group = group.sort_index()
            bucket_start = datetime.combine(
                session_date,
                datetime_time(9, 30),
                tzinfo=ET,
            ) + timedelta(hours=int(bucket))
            expected_end = min(
                bucket_start + timedelta(hours=1),
                self._session_close(session_date),
            )
            actual_end = group.index[-1].tz_convert(ET).to_pydatetime() + timedelta(
                minutes=30
            )
            if actual_end < expected_end:
                continue
            volume = float(group["volume"].sum()) if "volume" in group else 0.0
            row: dict[str, Any] = {
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
                "volume": volume,
            }
            if "trades" in group:
                row["trades"] = float(group["trades"].sum())
            if "vwap" in group:
                row["vwap"] = (
                    float((group["vwap"] * group["volume"]).sum() / volume)
                    if volume > 0
                    else float(group["vwap"].iloc[-1])
                )
            rows.append(row)
            timestamps.append(pd.Timestamp(bucket_start).tz_convert("UTC"))
        if not rows:
            return pd.DataFrame(columns=[column for column in frame.columns])
        return pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps)).sort_index()

    @staticmethod
    def _normalize_bar_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        result = frame.rename(columns={"trade_count": "trades", "vwap": "vwap"})
        result.index = pd.to_datetime(result.index, utc=True)
        return result[[column for column in ["open", "high", "low", "close", "volume", "trades", "vwap"] if column in result.columns]].sort_index()

    def get_latest_quotes(self, symbols: list[str]) -> dict[str, Any]:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
        return serialize_model(
            self._data_read(
                "latest_quotes", lambda: self.historical.get_stock_latest_quote(request)
            )
        )

    def _recover_order_by_client_id_locked(
        self, client_order_id: str
    ) -> tuple[str, Any | None, BaseException | None]:
        """Poll a deterministic client id after an ambiguous submit; never submits again."""
        for attempt in range(self.settings.alpaca_retry_attempts):
            try:
                order = self.trading.get_order_by_client_id(client_order_id)
            except Exception as exc:
                status = self._http_status(exc)
                if status == 404:
                    if attempt + 1 < self.settings.alpaca_retry_attempts:
                        self._sleep(self._backoff(attempt))
                        continue
                    return "not_found", None, None
                if self._is_transient(exc):
                    if self._is_transport_error(exc):
                        self._refresh_session("trading")
                    if attempt + 1 < self.settings.alpaca_retry_attempts:
                        self._sleep(self._backoff(attempt))
                        continue
                    return "unknown", None, exc
                safe = self._safe_exception(exc)
                if status in {401, 403}:
                    self._trading_circuit.success()
                    self._mark_operation_failure("trading", "submit_order", exc)
                else:
                    self._trading_circuit.success()
                raise safe from exc
            else:
                return "found", order, None
        return "unknown", None, None

    def _submit_order_safely(self, request: Any, client_order_id: str) -> dict[str, Any]:
        """Submit exactly once, then recover only with Alpaca's client-order-id lookup."""
        with self._trading_lock:
            self.require_connection()
            self._trading_circuit.before_call()
            try:
                order = self.trading.submit_order(order_data=request)
            except Exception as exc:
                status = self._http_status(exc)
                should_recover = self._is_transient(exc) or status == 422
                if not should_recover:
                    safe = self._safe_exception(exc)
                    if status in {401, 403}:
                        self._trading_circuit.success()
                        self._mark_operation_failure("trading", "submit_order", exc)
                    else:
                        self._trading_circuit.success()
                        self._mark_operation_success("trading", "submit_order")
                    raise safe from exc
                if self._is_transport_error(exc):
                    self._refresh_session("trading")
                recovery_state, recovered, recovery_error = (
                    self._recover_order_by_client_id_locked(client_order_id)
                )
                if recovery_state == "found":
                    self._trading_circuit.success()
                    self._mark_operation_success("trading", "submit_order")
                    self._invalidate_trading_cache()
                    return serialize_model(recovered)
                if self._is_transient(exc) or recovery_state == "unknown":
                    failure = recovery_error or exc
                    safe = self._safe_exception(failure)
                    if self._is_transport_error(exc):
                        self._refresh_session("trading")
                    self._invalidate_trading_cache()
                    self._mark_operation_failure("trading", "submit_order", failure)
                    opened = self._trading_circuit.failure(
                        str(safe), self._error_category(failure)
                    )
                    if opened:
                        logger.warning("Alpaca trading circuit opened after ambiguous order submit")
                    raise AlpacaAmbiguousOrderError(client_order_id) from exc
                safe = self._safe_exception(exc)
                if self._http_status(exc) in {401, 403}:
                    self._trading_circuit.success()
                    self._mark_operation_failure("trading", "submit_order", exc)
                else:
                    self._trading_circuit.success()
                    self._mark_operation_success("trading", "submit_order")
                raise safe from exc
            else:
                self._trading_circuit.success()
                self._mark_operation_success("trading", "submit_order")
                self._invalidate_trading_cache()
                return serialize_model(order)

    def _trading_mutation_once(self, operation: str, callback: Callable[[], Any]) -> Any:
        """Bound a mutation without replaying it after an ambiguous transport failure."""
        with self._trading_lock:
            self.require_connection()
            self._trading_circuit.before_call()
            try:
                result = callback()
            except Exception as exc:
                if self._is_transient(exc):
                    safe = self._safe_exception(exc)
                    if self._is_transport_error(exc):
                        self._refresh_session("trading")
                    self._invalidate_trading_cache()
                    self._mark_operation_failure("trading", operation, exc)
                    opened = self._trading_circuit.failure(
                        str(safe), self._error_category(exc)
                    )
                    if opened:
                        logger.warning("Alpaca trading circuit opened after %s", operation)
                    raise safe from exc
                safe = self._safe_exception(exc)
                if self._http_status(exc) in {401, 403}:
                    self._trading_circuit.success()
                    self._mark_operation_failure("trading", operation, exc)
                else:
                    self._trading_circuit.success()
                    self._mark_operation_success("trading", operation)
                raise safe from exc
            else:
                self._trading_circuit.success()
                self._mark_operation_success("trading", operation)
                self._invalidate_trading_cache()
                return result

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
        return self._submit_order_safely(request, client_order_id)

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
        return self._submit_order_safely(
            TrailingStopOrderRequest(**params), client_order_id
        )

    def submit_exit_order(
        self, symbol: str, qty: float, client_order_id: str
    ) -> dict[str, Any]:
        """Sell only the quantity attributed to one QuantPilot strategy."""
        self.require_connection()
        request = MarketOrderRequest(
            symbol=symbol,
            qty=round(qty, 6),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        return self._submit_order_safely(request, client_order_id)

    def cancel_order(self, order_id: str) -> None:
        self._trading_mutation_once(
            "cancel_order", lambda: self.trading.cancel_order_by_id(order_id)
        )

    def cancel_all_orders(self) -> list[Any]:
        return serialize_model(
            self._trading_mutation_once(
                "cancel_all_orders", lambda: self.trading.cancel_orders()
            )
        )

    def cancel_symbol_orders(self, symbol: str) -> None:
        with self._trading_lock:
            self.require_connection()
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=500)
            orders = self._resilient_locked(
                "trading",
                "symbol_open_orders",
                lambda: self.trading.get_orders(filter=request),
            )
            for order in orders:
                try:
                    self._trading_mutation_once(
                        "cancel_symbol_order",
                        lambda order_id=order.id: self.trading.cancel_order_by_id(order_id),
                    )
                except Exception as exc:
                    logger.warning(
                        "Unable to cancel Alpaca order %s: %s",
                        getattr(order, "id", "unknown"),
                        self._safe_exception(exc),
                    )

    def close_position(self, symbol: str) -> dict[str, Any]:
        self.cancel_symbol_orders(symbol)
        return serialize_model(
            self._trading_mutation_once(
                "close_position", lambda: self.trading.close_position(symbol)
            )
        )

    def close_all_positions(self) -> list[Any]:
        return serialize_model(
            self._trading_mutation_once(
                "close_all_positions",
                lambda: self.trading.close_all_positions(cancel_orders=True),
            )
        )

    async def start_streams(
        self,
        symbols: list[str],
        bar_callback: Callable[[dict[str, Any]], Any],
        trade_callback: Callable[[dict[str, Any]], Any],
    ) -> None:
        if not self.configured or not symbols:
            await asyncio.to_thread(self.stop_streams)
            return
        normalized = tuple(sorted(set(symbols)))
        with self._stream_lock:
            if normalized == self._stream_symbols and self.streams_healthy():
                return
        await asyncio.to_thread(self.stop_streams)
        main_loop = asyncio.get_running_loop()
        with self._stream_lock:
            stream_generation = self._stream_generation
        data_stream = StockDataStream(
            self.api_key_id,
            self.api_secret_key,
            feed=DataFeed.IEX,
            websocket_params={
                "open_timeout": 10,
                "close_timeout": 5,
                "ping_interval": 20,
                "ping_timeout": 20,
            },
        )
        trade_stream = TradingStream(
            self.api_key_id,
            self.api_secret_key,
            paper=True,
            websocket_params={
                "open_timeout": 10,
                "close_timeout": 5,
                "ping_interval": 20,
                "ping_timeout": 20,
            },
        )

        async def invoke_callback(
            callback: Callable[[dict[str, Any]], Any], payload: dict[str, Any]
        ) -> None:
            result = callback(payload)
            if inspect.isawaitable(result):
                await result

        async def dispatch_to_main_loop(
            callback: Callable[[dict[str, Any]], Any], payload: dict[str, Any]
        ) -> None:
            if asyncio.get_running_loop() is main_loop:
                await invoke_callback(callback, payload)
                return
            future = asyncio.run_coroutine_threadsafe(
                invoke_callback(callback, payload), main_loop
            )
            await asyncio.wrap_future(future)

        async def on_bar(bar: Any) -> None:
            with self._stream_lock:
                if stream_generation != self._stream_generation:
                    return
            await dispatch_to_main_loop(bar_callback, serialize_model(bar))

        async def on_trade(update: Any) -> None:
            with self._stream_lock:
                if stream_generation != self._stream_generation:
                    return
            await dispatch_to_main_loop(trade_callback, serialize_model(update))

        data_stream.subscribe_bars(on_bar, *normalized)
        trade_stream.subscribe_trade_updates(on_trade)

        def run_data() -> None:
            try:
                data_stream.run()
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Alpaca market stream stopped: %s", type(exc).__name__)

        def run_trades() -> None:
            try:
                trade_stream.run()
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Alpaca trade stream stopped: %s", type(exc).__name__)

        threads: list[threading.Thread] = []
        for target, name in [(run_data, "alpaca-data"), (run_trades, "alpaca-trades")]:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            threads.append(thread)
        with self._stream_lock:
            self._data_stream = data_stream
            self._trade_stream = trade_stream
            self._stream_threads = threads
            self._stream_symbols = normalized
        await asyncio.sleep(0)

    def streams_healthy(self) -> bool:
        with self._stream_lock:
            return bool(
                self._stream_symbols
                and len(self._stream_threads) == 2
                and all(thread.is_alive() for thread in self._stream_threads)
            )

    def stop_streams(self) -> None:
        with self._stream_lock:
            streams = (self._data_stream, self._trade_stream)
            threads = list(self._stream_threads)
            self._data_stream = None
            self._trade_stream = None
            self._stream_threads = []
            self._stream_symbols = ()
            self._stream_generation += 1
        for stream in streams:
            if stream is None:
                continue

            def stop_one(target=stream) -> None:
                try:
                    if hasattr(target, "stop"):
                        result = target.stop()
                        if inspect.isawaitable(result):
                            asyncio.run(result)
                    elif hasattr(target, "stop_ws"):
                        result = target.stop_ws()
                        if inspect.isawaitable(result):
                            asyncio.run(result)
                except Exception:
                    logger.debug("Ignoring stream shutdown error", exc_info=True)

            stopper = threading.Thread(
                target=stop_one,
                name="alpaca-stream-stop",
                daemon=True,
            )
            stopper.start()
            stopper.join(timeout=6.0)
            if stopper.is_alive():
                logger.warning("Alpaca stream shutdown exceeded 6 seconds; isolating old stream")
        current = threading.current_thread()
        for thread in threads:
            if thread is not current and thread.is_alive():
                thread.join(timeout=1.0)
