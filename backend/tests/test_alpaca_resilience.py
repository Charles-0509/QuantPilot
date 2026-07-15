from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests
import pandas as pd
from alpaca.common.exceptions import APIError

from app.config import Settings
from app.services.alpaca_service import (
    AlpacaAmbiguousOrderError,
    AlpacaCircuitOpenError,
    AlpacaService,
    AlpacaTransientError,
    _TimeoutSession,
)
from app.services.engine import TradingEngine


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def local_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "investor_db_path": str(tmp_path / "investor.db"),
        "alpaca_retry_base_seconds": 0,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def attached_service(
    tmp_path: Path,
    trading,
    historical=None,
    *,
    clock: FakeClock | None = None,
    sleeps: list[float] | None = None,
    **settings_overrides,
) -> AlpacaService:
    fake_clock = clock or FakeClock()
    sleep_calls = sleeps if sleeps is not None else []
    service = AlpacaService(
        local_settings(tmp_path, **settings_overrides),
        use_env=False,
        monotonic=fake_clock.monotonic,
        sleep=sleep_calls.append,
        jitter=lambda _low, high: high,
    )
    service.configured = True
    service.trading = trading
    service.historical = historical or object()
    return service


def api_error(status: int, *, retry_after: str | None = None) -> APIError:
    response = requests.Response()
    response.status_code = status
    response._content = b'{"code": 10000, "message": "safe"}'
    response.url = "https://paper-api.alpaca.markets/v2/test"
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return APIError(response.text, requests.HTTPError(response=response))


def test_timeout_session_applies_default_and_preserves_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def fake_request(_self, _method, _url, **kwargs):
        captured.append(kwargs.get("timeout"))
        return requests.Response()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    session = _TimeoutSession(5, 12)

    session.request("GET", "https://example.test")
    session.request("GET", "https://example.test", timeout=2)

    assert captured == [(5, 12), 2]


def test_configure_installs_bounded_sessions_and_disables_sdk_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class OldSession:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeHistorical:
        def __init__(self, _key: str, _secret: str):
            self._session = OldSession()
            self._retry = 3

    class FakeTrading:
        def __init__(self, _key: str, _secret: str, *, paper: bool):
            self.paper = paper
            self._session = OldSession()
            self._retry = 3

    monkeypatch.setattr("app.services.alpaca_service.StockHistoricalDataClient", FakeHistorical)
    monkeypatch.setattr("app.services.alpaca_service.TradingClient", FakeTrading)
    service = AlpacaService(local_settings(tmp_path), use_env=False)

    service.configure("paper-key", "paper-secret")

    assert isinstance(service.trading._session, _TimeoutSession)
    assert service.trading._session._default_timeout == (5.0, 6.0)
    assert service.historical._session._default_timeout == (5.0, 45.0)
    assert service.trading._retry == 0
    assert service.historical._retry == 0


def test_read_retries_are_bounded_and_short_cache_is_single_flight(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = 0
            self.remaining_failures = 2

        def get_account(self):
            self.calls += 1
            if self.remaining_failures:
                self.remaining_failures -= 1
                raise requests.exceptions.SSLError("credential-secret-must-not-leak")
            return {"status": "ACTIVE", "equity": "100000"}

    clock = FakeClock()
    fake = FakeTrading()
    service = attached_service(
        tmp_path,
        fake,
        clock=clock,
        alpaca_retry_attempts=3,
        alpaca_retry_base_seconds=0.5,
    )

    first = service.get_account()
    first["status"] = "MUTATED"
    second = service.get_account()

    assert fake.calls == 3
    assert second["status"] == "ACTIVE"
    clock.advance(6)
    service.get_account()
    assert fake.calls == 4


@pytest.mark.parametrize(
    "failure",
    [
        requests.exceptions.ConnectTimeout("connect timeout"),
        api_error(429, retry_after="2"),
        api_error(503),
    ],
)
def test_timeout_rate_limit_and_5xx_are_retried(
    failure: Exception, tmp_path: Path
) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = 0

        def get_account(self):
            self.calls += 1
            if self.calls == 1:
                raise failure
            return {"status": "ACTIVE"}

    sleeps: list[float] = []
    fake = FakeTrading()
    service = attached_service(
        tmp_path,
        fake,
        sleeps=sleeps,
        alpaca_retry_attempts=2,
        alpaca_retry_base_seconds=0.5,
    )

    assert service.get_account()["status"] == "ACTIVE"
    assert fake.calls == 2
    if AlpacaService._http_status(failure) == 429:
        assert sleeps == [2.0]


def test_authentication_error_is_not_retried_or_leaked(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = 0

        def get_account(self):
            self.calls += 1
            raise api_error(401)

    fake = FakeTrading()
    service = attached_service(tmp_path, fake, alpaca_retry_attempts=3)

    with pytest.raises(Exception) as captured:
        service.get_account()

    assert fake.calls == 1
    assert "认证失败" in str(captured.value)
    assert "paper-api.alpaca.markets" not in str(captured.value)
    status = service.connection_status()
    assert status["state"] == "degraded"
    assert status["last_error_category"] == "authentication"


def test_account_positions_open_orders_and_clock_each_use_short_cache(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = {"account": 0, "positions": 0, "orders": 0, "clock": 0}

        def get_account(self):
            self.calls["account"] += 1
            return {"status": "ACTIVE"}

        def get_all_positions(self):
            self.calls["positions"] += 1
            return []

        def get_orders(self, *, filter):
            self.calls["orders"] += 1
            return []

        def get_clock(self):
            self.calls["clock"] += 1
            return {"is_open": True}

    fake = FakeTrading()
    service = attached_service(tmp_path, fake)

    for _ in range(2):
        service.get_account()
        service.get_positions()
        service.get_orders("open")
        service.get_clock()

    assert fake.calls == {"account": 1, "positions": 1, "orders": 1, "clock": 1}


def test_recent_bar_windows_cover_weekends_holidays_and_daily_warmup() -> None:
    assert AlpacaService._recent_window_days("5Min", 300) >= 17
    assert AlpacaService._recent_window_days("15Min", 300) >= 30
    assert AlpacaService._recent_window_days("1Day", 300) >= 500


def test_recent_bars_filter_extended_hours_keep_each_symbol_latest_and_cache(
    tmp_path: Path,
) -> None:
    local_times = pd.DatetimeIndex(
        [
            "2026-07-14 09:00:00-04:00",
            "2026-07-14 09:30:00-04:00",
            "2026-07-14 15:30:00-04:00",
            "2026-07-14 16:00:00-04:00",
        ]
    ).tz_convert("UTC")

    def symbol_frame(offset: float) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open": [offset + value for value in (1, 2, 3, 4)],
                "high": [offset + value for value in (2, 3, 4, 5)],
                "low": [offset + value for value in (0, 1, 2, 3)],
                "close": [offset + value for value in (1.5, 2.5, 3.5, 4.5)],
                "volume": [100, 200, 300, 400],
            },
            index=local_times,
        )

    combined = pd.concat(
        {"SPY": symbol_frame(0), "QQQ": symbol_frame(100)},
        names=["symbol", "timestamp"],
    )

    class Response:
        df = combined

    class FakeHistorical:
        def __init__(self) -> None:
            self.calls = 0
            self.requests = []

        def get_stock_bars(self, request):
            self.calls += 1
            self.requests.append(request)
            return Response()

    historical = FakeHistorical()
    service = attached_service(tmp_path, object(), historical)

    first = service.recent_bars(["SPY", "QQQ"], "15Min", bars=2)
    second = service.recent_bars(["SPY", "QQQ"], "15Min", bars=2)

    assert historical.calls == 1
    assert historical.requests[0].limit is None
    for symbol in ("SPY", "QQQ"):
        assert list(first[symbol].index.tz_convert("America/New_York").strftime("%H:%M")) == [
            "09:30",
            "15:30",
        ]
        assert second[symbol].equals(first[symbol])


def test_failed_market_data_operation_forces_real_probe_instead_of_recent_cache(
    tmp_path: Path,
) -> None:
    index = pd.date_range("2026-07-14 14:00", periods=3, freq="15min", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": [1, 2, 3],
            "high": [2, 3, 4],
            "low": [0, 1, 2],
            "close": [1, 2, 3],
            "volume": [10, 10, 10],
        },
        index=index,
    )

    class Response:
        df = frame

    class FakeHistorical:
        def __init__(self) -> None:
            self.calls = 0
            self.fail = False

        def get_stock_bars(self, _request):
            self.calls += 1
            if self.fail:
                raise requests.exceptions.ConnectionError("data down")
            return Response()

    historical = FakeHistorical()
    service = attached_service(
        tmp_path,
        object(),
        historical,
        alpaca_retry_attempts=1,
        alpaca_circuit_failure_threshold=3,
    )
    service.recent_bars(["SPY"], "15Min", bars=2)
    historical.fail = True
    with pytest.raises(AlpacaTransientError):
        service.get_bars(
            ["SPY"],
            "15Min",
            datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc),
        )
    historical.fail = False

    service.recent_bars(["SPY"], "15Min", bars=2)

    assert historical.calls == 3
    assert service.connection_status()["health"]["failed_operations"] == []


def test_one_hour_uses_regular_session_thirty_minute_buckets(tmp_path: Path) -> None:
    index = pd.DatetimeIndex(
        [
            "2026-07-14 09:00:00-04:00",
            "2026-07-14 09:30:00-04:00",
            "2026-07-14 10:00:00-04:00",
            "2026-07-14 10:30:00-04:00",
            "2026-07-14 15:30:00-04:00",
        ]
    ).tz_convert("UTC")
    frame = pd.DataFrame(
        {
            "open": [90, 100, 101, 102, 110],
            "high": [91, 103, 104, 105, 112],
            "low": [89, 99, 100, 101, 109],
            "close": [90, 102, 103, 104, 111],
            "volume": [10, 20, 30, 40, 50],
        },
        index=index,
    )

    class Response:
        df = frame

    class FakeHistorical:
        def __init__(self) -> None:
            self.request = None

        def get_stock_bars(self, request):
            self.request = request
            return Response()

    historical = FakeHistorical()
    service = attached_service(tmp_path, object(), historical)
    result = service.get_bars(
        ["SPY"],
        "1Hour",
        datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc),
    )["SPY"]

    assert historical.request.timeframe.amount == 30
    assert list(result.index.tz_convert("America/New_York").strftime("%H:%M")) == [
        "09:30",
        "15:30",
    ]
    assert result.iloc[0].to_dict() == {
        "open": 100.0,
        "high": 104.0,
        "low": 99.0,
        "close": 103.0,
        "volume": 50.0,
    }


def test_early_close_calendar_keeps_final_hour_and_completes_daily_bar(
    tmp_path: Path,
) -> None:
    et = ZoneInfo("America/New_York")
    index = pd.date_range(
        "2026-11-27 09:30:00",
        "2026-11-27 12:30:00",
        freq="30min",
        tz=et,
    ).tz_convert("UTC")
    frame = pd.DataFrame(
        {
            "open": range(100, 107),
            "high": range(101, 108),
            "low": range(99, 106),
            "close": range(100, 107),
            "volume": [100] * 7,
        },
        index=index,
    )

    class Response:
        df = frame

    class FakeHistorical:
        def get_stock_bars(self, _request):
            return Response()

    class FakeTrading:
        def get_calendar(self, filters):
            assert filters.start <= datetime(2026, 11, 27).date() <= filters.end
            return [
                SimpleNamespace(
                    date=datetime(2026, 11, 27).date(),
                    close=datetime(2026, 11, 27, 13, 0, tzinfo=et),
                )
            ]

    service = attached_service(tmp_path, FakeTrading(), FakeHistorical())
    start = datetime(2026, 11, 27, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 27, 18, 5, tzinfo=timezone.utc)
    hourly = service.get_bars(["SPY"], "1Hour", start, end)["SPY"]
    daily = service.get_bars(["SPY"], "1Day", start, end)["SPY"]

    assert list(hourly.index.tz_convert(et).strftime("%H:%M")) == [
        "09:30",
        "10:30",
        "11:30",
        "12:30",
    ]
    now_et = datetime(2026, 11, 27, 13, 1, tzinfo=et)
    assert len(TradingEngine._completed_bars(hourly, "1Hour", now_et)) == 4
    assert len(TradingEngine._completed_bars(daily, "1Day", now_et)) == 1


def test_daily_bars_are_aggregated_only_from_regular_thirty_minute_volume(
    tmp_path: Path,
) -> None:
    index = pd.DatetimeIndex(
        [
            "2026-07-14 04:00:00-04:00",
            "2026-07-14 09:30:00-04:00",
            "2026-07-14 10:00:00-04:00",
            "2026-07-14 15:30:00-04:00",
            "2026-07-14 16:00:00-04:00",
            "2026-07-14 19:30:00-04:00",
            "2026-07-15 09:30:00-04:00",
        ]
    ).tz_convert("UTC")
    frame = pd.DataFrame(
        {
            "open": [50, 100, 102, 104, 105, 250, 106],
            "high": [200, 103, 105, 106, 300, 350, 108],
            "low": [10, 99, 101, 103, 20, 200, 105],
            "close": [150, 102, 104, 105, 250, 300, 107],
            "volume": [9000, 100, 200, 300, 8000, 7000, 400],
        },
        index=index,
    )

    class Response:
        df = frame

    class FakeHistorical:
        def __init__(self) -> None:
            self.request = None

        def get_stock_bars(self, request):
            self.request = request
            return Response()

    historical = FakeHistorical()
    service = attached_service(tmp_path, object(), historical)
    result = service.get_bars(
        ["SPY"],
        "1Day",
        datetime(2026, 7, 13, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc),
    )["SPY"]

    assert historical.request.timeframe.amount == 30
    assert len(result) == 1
    assert result.index[0].tz_convert("America/New_York").date().isoformat() == "2026-07-14"
    assert result.iloc[0].to_dict() == {
        "open": 100.0,
        "high": 106.0,
        "low": 99.0,
        "close": 105.0,
        "volume": 600.0,
    }


def test_recent_daily_cache_is_fifteen_minutes_and_refreshes_incrementally(
    tmp_path: Path,
) -> None:
    index = pd.DatetimeIndex(
        [
            "2026-07-14 09:30:00-04:00",
            "2026-07-14 15:30:00-04:00",
        ]
    ).tz_convert("UTC")
    frame = pd.DataFrame(
        {
            "open": [100, 104],
            "high": [103, 106],
            "low": [99, 103],
            "close": [102, 105],
            "volume": [100, 300],
        },
        index=index,
    )

    class Response:
        df = frame

    class FakeHistorical:
        def __init__(self) -> None:
            self.requests = []

        def get_stock_bars(self, request):
            self.requests.append(request)
            return Response()

    clock = FakeClock()
    historical = FakeHistorical()
    service = attached_service(tmp_path, object(), historical, clock=clock)

    first = service.recent_bars(["SPY"], "1Day", bars=300)
    clock.advance(100)
    second = service.recent_bars(["SPY"], "1Day", bars=300)
    clock.advance(901)
    third = service.recent_bars(["SPY"], "1Day", bars=300)

    assert service.settings.alpaca_daily_bars_cache_seconds >= 900
    assert len(historical.requests) == 2
    assert second["SPY"].equals(first["SPY"])
    assert third["SPY"].equals(first["SPY"])
    assert historical.requests[1].start > historical.requests[0].start


def test_concurrent_cached_reads_only_call_upstream_once(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = 0

        def get_account(self):
            self.calls += 1
            time.sleep(0.02)
            return {"status": "ACTIVE"}

    fake = FakeTrading()
    service = attached_service(tmp_path, fake)
    barrier = threading.Barrier(6)
    results: list[dict] = []

    def worker() -> None:
        barrier.wait()
        results.append(service.get_account())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert len(results) == 5
    assert fake.calls == 1


def test_connection_status_is_memory_only(tmp_path: Path) -> None:
    class ExplodingTrading:
        def get_account(self):
            raise AssertionError("connection_status performed network I/O")

    service = attached_service(tmp_path, ExplodingTrading())

    status = service.connection_status()

    assert status["configured"] is True
    assert status["connected"] is False
    assert status["state"] == "unknown"
    assert status["health"]["trading"]["last_success_at"] is None


def test_circuit_opens_fast_and_recovers_with_one_half_open_probe(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = 0
            self.healthy = False

        def get_clock(self):
            self.calls += 1
            if not self.healthy:
                raise requests.exceptions.ConnectionError("network down")
            return {"is_open": True}

    clock = FakeClock()
    fake = FakeTrading()
    service = attached_service(
        tmp_path,
        fake,
        clock=clock,
        alpaca_retry_attempts=1,
        alpaca_circuit_failure_threshold=2,
        alpaca_circuit_recovery_seconds=10,
    )

    with pytest.raises(AlpacaTransientError):
        service.get_clock()
    with pytest.raises(AlpacaTransientError):
        service.get_clock()
    assert fake.calls == 2
    with pytest.raises(AlpacaCircuitOpenError):
        service.get_clock()
    assert fake.calls == 2
    assert service.connection_status()["connected"] is False

    fake.healthy = True
    clock.advance(10)
    assert service.get_clock()["is_open"] is True
    assert fake.calls == 3
    assert service.connection_status()["health"]["trading"]["state"] == "closed"


def test_open_circuit_does_not_serve_short_cache_as_fresh(tmp_path: Path) -> None:
    class FakeTrading:
        def get_account(self):
            return {"status": "ACTIVE"}

        def get_clock(self):
            raise requests.exceptions.ConnectionError("down")

    service = attached_service(
        tmp_path,
        FakeTrading(),
        alpaca_retry_attempts=1,
        alpaca_circuit_failure_threshold=1,
    )
    service.get_account()
    with pytest.raises(AlpacaTransientError):
        service.get_clock()

    with pytest.raises(AlpacaCircuitOpenError):
        service.get_account()


def test_half_open_non_network_error_does_not_stick_probe_forever(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = 0

        def get_clock(self):
            self.calls += 1
            if self.calls == 1:
                raise requests.exceptions.ConnectionError("down")
            if self.calls == 2:
                raise ValueError("bad response model")
            return {"is_open": True}

    clock = FakeClock()
    fake = FakeTrading()
    service = attached_service(
        tmp_path,
        fake,
        clock=clock,
        alpaca_retry_attempts=1,
        alpaca_circuit_failure_threshold=1,
        alpaca_circuit_recovery_seconds=10,
    )
    with pytest.raises(AlpacaTransientError):
        service.get_clock()
    clock.advance(10)
    with pytest.raises(Exception):
        service.get_clock()

    assert service.get_clock()["is_open"] is True
    assert fake.calls == 3


def test_trading_and_data_circuits_are_independent(tmp_path: Path) -> None:
    class FakeTrading:
        def get_clock(self):
            raise requests.exceptions.ConnectionError("trading down")

    class QuoteResponse(dict):
        pass

    class FakeHistorical:
        def __init__(self) -> None:
            self.calls = 0

        def get_stock_latest_quote(self, _request):
            self.calls += 1
            return QuoteResponse(SPY={"ask_price": 100})

    historical = FakeHistorical()
    service = attached_service(
        tmp_path,
        FakeTrading(),
        historical,
        alpaca_retry_attempts=1,
        alpaca_circuit_failure_threshold=1,
    )

    with pytest.raises(AlpacaTransientError):
        service.get_clock()
    assert service.get_latest_quotes(["SPY"])["SPY"]["ask_price"] == 100
    assert historical.calls == 1


def test_data_circuit_degrades_composite_connection_state(tmp_path: Path) -> None:
    class FakeTrading:
        def get_account(self):
            return {"status": "ACTIVE"}

    class FakeHistorical:
        def get_stock_latest_quote(self, _request):
            raise requests.exceptions.ConnectionError("data down")

    service = attached_service(
        tmp_path,
        FakeTrading(),
        FakeHistorical(),
        alpaca_retry_attempts=1,
        alpaca_circuit_failure_threshold=1,
    )
    service.get_account()
    with pytest.raises(AlpacaTransientError):
        service.get_latest_quotes(["SPY"])

    status = service.connection_status()
    assert status["state"] == "circuit_open"
    assert status["connected"] is False
    assert status["last_error_category"] == "connection"


def test_later_success_does_not_immediately_hide_open_orders_failure(tmp_path: Path) -> None:
    class FakeTrading:
        def get_account(self):
            return {"status": "ACTIVE"}

        def get_orders(self, *, filter):
            raise requests.exceptions.ConnectionError("orders unavailable")

        def get_clock(self):
            return {"is_open": True}

    service = attached_service(
        tmp_path,
        FakeTrading(),
        alpaca_retry_attempts=1,
        alpaca_circuit_failure_threshold=3,
        alpaca_circuit_recovery_seconds=30,
    )
    service.get_account()
    with pytest.raises(AlpacaTransientError):
        service.get_orders("open")
    service.get_clock()

    status = service.connection_status()
    assert status["state"] == "degraded"
    assert status["consecutive_failures"] == 1
    assert status["last_error_category"] == "connection"


def test_same_operation_success_immediately_clears_degraded_state(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = 0

        def get_clock(self):
            self.calls += 1
            if self.calls == 1:
                raise requests.exceptions.ConnectionError("temporary")
            return {"is_open": True}

    service = attached_service(
        tmp_path,
        FakeTrading(),
        alpaca_retry_attempts=1,
        alpaca_circuit_failure_threshold=3,
    )
    with pytest.raises(AlpacaTransientError):
        service.get_clock()
    assert service.connection_status()["state"] == "degraded"

    assert service.get_clock()["is_open"] is True
    assert service.connection_status()["state"] == "connected"


def test_order_transport_failure_recovers_by_client_id_without_resubmit(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.submits = 0
            self.lookups = 0

        def submit_order(self, *, order_data):
            self.submits += 1
            raise requests.exceptions.SSLError("response was lost")

        def get_order_by_client_id(self, client_id: str):
            self.lookups += 1
            return {"id": "alpaca-order", "client_order_id": client_id, "status": "accepted"}

    fake = FakeTrading()
    service = attached_service(tmp_path, fake)

    order = service.submit_entry_order(
        symbol="SPY",
        qty=1,
        notional=None,
        order_type="market",
        time_in_force="day",
        client_order_id="qp-deterministic-id",
    )

    assert order["id"] == "alpaca-order"
    assert fake.submits == 1
    assert fake.lookups == 1


def test_order_ambiguity_never_blindly_reposts(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.submits = 0
            self.lookups = 0

        def submit_order(self, *, order_data):
            self.submits += 1
            raise requests.exceptions.ReadTimeout("response was lost")

        def get_order_by_client_id(self, _client_id: str):
            self.lookups += 1
            raise api_error(404)

    fake = FakeTrading()
    service = attached_service(tmp_path, fake, alpaca_retry_attempts=3)

    with pytest.raises(AlpacaAmbiguousOrderError) as captured:
        service.submit_exit_order("SPY", 1, "qp-exit-id")

    assert captured.value.client_order_id == "qp-exit-id"
    assert fake.submits == 1
    assert fake.lookups == 3
    assert "response was lost" not in str(captured.value)


def test_duplicate_order_lookup_transport_failure_stays_ambiguous(tmp_path: Path) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.submits = 0
            self.lookups = 0

        def submit_order(self, *, order_data):
            self.submits += 1
            raise api_error(422)

        def get_order_by_client_id(self, _client_id: str):
            self.lookups += 1
            raise requests.exceptions.ConnectionError("lookup unavailable")

    fake = FakeTrading()
    service = attached_service(tmp_path, fake, alpaca_retry_attempts=2)

    with pytest.raises(AlpacaAmbiguousOrderError):
        service.submit_exit_order("SPY", 1, "qp-duplicate-id")

    assert fake.submits == 1
    assert fake.lookups == 2


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"id": "order-1", "client_order_id": "qp-id"}, {"id": "order-1", "client_order_id": "qp-id"}),
        (api_error(404), None),
    ],
)
def test_public_client_order_lookup_distinguishes_found_and_confirmed_404(
    response, expected, tmp_path: Path
) -> None:
    class FakeTrading:
        def get_order_by_client_id(self, _client_id: str):
            if isinstance(response, Exception):
                raise response
            return response

    service = attached_service(tmp_path, FakeTrading())

    assert service.get_order_by_client_id("qp-id") == expected


def test_public_client_order_lookup_transport_failure_never_returns_none(
    tmp_path: Path,
) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.calls = 0

        def get_order_by_client_id(self, _client_id: str):
            self.calls += 1
            raise requests.exceptions.ConnectionError("lookup unavailable")

    fake = FakeTrading()
    service = attached_service(tmp_path, fake, alpaca_retry_attempts=2)

    with pytest.raises(AlpacaTransientError):
        service.get_order_by_client_id("qp-id")

    assert fake.calls == 2


@pytest.mark.parametrize("resolved_order", [{"id": "order-1"}, None])
def test_definite_client_order_lookup_clears_ambiguous_submit_health(
    resolved_order, tmp_path: Path
) -> None:
    class FakeTrading:
        def __init__(self) -> None:
            self.lookups = 0

        def submit_order(self, *, order_data):
            raise requests.exceptions.ReadTimeout("submit response lost")

        def get_order_by_client_id(self, client_order_id: str):
            self.lookups += 1
            if self.lookups <= 3 or resolved_order is None:
                raise api_error(404)
            return {**resolved_order, "client_order_id": client_order_id}

    fake = FakeTrading()
    service = attached_service(tmp_path, fake, alpaca_retry_attempts=3)
    with pytest.raises(AlpacaAmbiguousOrderError):
        service.submit_exit_order("SPY", 1, "qp-ambiguous")
    assert any(
        item["operation"] == "submit_order"
        for item in service.connection_status()["health"]["failed_operations"]
    )

    assert service.get_order_by_client_id("qp-ambiguous") == (
        {**resolved_order, "client_order_id": "qp-ambiguous"}
        if resolved_order is not None
        else None
    )
    assert not any(
        item["operation"] == "submit_order"
        for item in service.connection_status()["health"]["failed_operations"]
    )


@pytest.mark.asyncio
async def test_dead_stream_threads_are_detected_and_same_symbols_can_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[object] = []

    class DeadStream:
        def __init__(self, *_args, **_kwargs):
            created.append(self)

        def subscribe_bars(self, *_args) -> None:
            pass

        def subscribe_trade_updates(self, *_args) -> None:
            pass

        def run(self) -> None:
            return

        def stop(self) -> None:
            pass

    monkeypatch.setattr("app.services.alpaca_service.StockDataStream", DeadStream)
    monkeypatch.setattr("app.services.alpaca_service.TradingStream", DeadStream)
    service = attached_service(tmp_path, object())

    await service.start_streams(["SPY"], lambda _payload: None, lambda _payload: None)
    for thread in service._stream_threads:
        thread.join(timeout=1)
    assert service.streams_healthy() is False

    await service.start_streams(["SPY"], lambda _payload: None, lambda _payload: None)

    assert len(created) == 4
    await service.start_streams([], lambda _payload: None, lambda _payload: None)
    assert service._stream_symbols == ()
    assert service._stream_threads == []


@pytest.mark.asyncio
async def test_stream_callbacks_are_dispatched_to_fastapi_main_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class CallbackStream:
        def __init__(self, *_args, **_kwargs):
            self.handler = None

        def subscribe_bars(self, handler, *_symbols) -> None:
            self.handler = handler

        def subscribe_trade_updates(self, handler) -> None:
            self.handler = handler

        def run(self) -> None:
            asyncio.run(self.handler({"symbol": "SPY"}))

        def stop(self) -> None:
            pass

    monkeypatch.setattr("app.services.alpaca_service.StockDataStream", CallbackStream)
    monkeypatch.setattr("app.services.alpaca_service.TradingStream", CallbackStream)
    service = attached_service(tmp_path, object())
    main_loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    callback_contexts: list[tuple[asyncio.AbstractEventLoop, int]] = []
    received = asyncio.Event()

    async def callback(_payload) -> None:
        callback_contexts.append((asyncio.get_running_loop(), threading.get_ident()))
        if len(callback_contexts) == 2:
            received.set()

    await service.start_streams(["SPY"], callback, callback)
    await asyncio.wait_for(received.wait(), timeout=2)

    assert all(loop is main_loop for loop, _thread in callback_contexts)
    assert all(thread == main_thread for _loop, thread in callback_contexts)
    service.stop_streams()
