from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from alpaca.data.enums import DataFeed

from app.services import alpaca_service as alpaca_module
from app.services.market_data import MarketDataRouter, market_data_info
from tests.test_alpaca_resilience import api_error, attached_service


def bars_frame(start="2026-11-17 14:30"):
    index = pd.date_range(start, periods=4, freq="15min", tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1000.0}, index=index)


class Historical:
    def __init__(self, deny_sip=False):
        self.requests, self.deny_sip = [], deny_sip

    def get_stock_bars(self, request):
        self.requests.append(request)
        if self.deny_sip and request.feed == DataFeed.SIP:
            raise api_error(403)
        response = type("Response", (), {})()
        response.df = bars_frame()
        return response


def test_sip_history_clamps_end_to_free_plan_delay(tmp_path: Path):
    historical = Historical()
    service = attached_service(tmp_path, object(), historical)
    now = datetime.now(timezone.utc)
    frames = service.get_bars(["SPY"], "15Min", now - timedelta(days=5), now, feed="sip")
    request = historical.requests[0]
    assert request.feed == DataFeed.SIP
    end = request.end if request.end.tzinfo else request.end.replace(tzinfo=timezone.utc)
    assert now - end >= timedelta(minutes=alpaca_module.SIP_HISTORY_DELAY_MINUTES - 0.1)
    assert frames["SPY"].attrs["data_feed"] == "sip"


def test_sip_permission_denied_falls_back_to_iex_without_degrading_connection(tmp_path: Path):
    historical = Historical(deny_sip=True)
    service = attached_service(tmp_path, object(), historical)
    now = datetime.now(timezone.utc)
    frames = service.get_bars(["SPY"], "15Min", now - timedelta(days=5), now, feed="sip")
    assert [request.feed for request in historical.requests] == [DataFeed.SIP, DataFeed.IEX]
    assert frames["SPY"].attrs["data_feed"] == "iex" and not frames["SPY"].empty
    assert not service._operation_failed("data", "stock_bars_sip")
    service.get_bars(["SPY"], "15Min", now - timedelta(days=5), now, feed="sip")
    assert historical.requests[-1].feed == DataFeed.IEX and len(historical.requests) == 3


@pytest.mark.parametrize("timeframe, feed", [("15Min", DataFeed.IEX), ("1Day", DataFeed.SIP)])
def test_live_intraday_stays_on_one_realtime_feed_while_daily_uses_sip(tmp_path: Path, timeframe, feed):
    historical = Historical()
    service = attached_service(tmp_path, object(), historical)
    service.recent_bars(["SPY"], timeframe, bars=5)
    assert historical.requests[0].feed == feed


class Broker:
    broker_name, paper, configured = "ibkr", True, True

    def get_bars(self, *args, **kwargs):
        raise AssertionError("bars must come from the data source")

    def submit_entry_order(self, **kwargs):
        return {"broker": "ibkr"}

    def connection_status(self):
        return {"broker": "ibkr", "state": "connected", "feed": "ibkr"}


class Data:
    feed = "iex"

    def get_bars(self, *args, **kwargs):
        return {"SPY": "alpaca"}

    def recent_bars(self, *args, **kwargs):
        return {"SPY": "alpaca-recent"}

    def get_latest_quotes(self, symbols):
        return {"SPY": {"ask_price": 1}}

    def connection_status(self):
        return {"state": "connected", "feed": "iex", "message": "ok"}


def test_router_keeps_execution_on_broker_and_reads_data_from_alpaca():
    broker, data = Broker(), Data()
    router = MarketDataRouter(broker, data)
    assert router.get_bars(["SPY"], "15Min", None, None) == {"SPY": "alpaca"}
    assert router.recent_bars(["SPY"], "15Min") == {"SPY": "alpaca-recent"}
    assert router.get_latest_quotes(["SPY"]) == {"SPY": {"ask_price": 1}}
    assert router.submit_entry_order(symbol="SPY") == {"broker": "ibkr"}
    assert (router.broker_name, router.paper, router.feed) == ("ibkr", True, "iex")
    status = router.connection_status()
    assert status["broker"] == "ibkr" and status["market_data"]["source"] == "alpaca"
    router.configured = False
    assert broker.configured is False
    assert market_data_info(router)["source"] == "alpaca" and market_data_info(broker)["source"] == "ibkr"


def test_ibkr_without_alpaca_keys_keeps_broker_data(monkeypatch):
    from app.services import runtime as runtime_module
    unconfigured = type("Alpaca", (), {"configured": False})()
    monkeypatch.setattr(runtime_module, "build_alpaca", lambda settings, user_id: unconfigured)
    broker = Broker()
    settings_row = type("Row", (), {"market_data_source": "alpaca"})()
    assert runtime_module.with_market_data(None, 1, settings_row, broker) is broker
    configured = type("Alpaca", (), {"configured": True})()
    monkeypatch.setattr(runtime_module, "build_alpaca", lambda settings, user_id: configured)
    assert isinstance(runtime_module.with_market_data(None, 1, settings_row, broker), MarketDataRouter)
    settings_row.market_data_source = "ibkr"
    assert runtime_module.with_market_data(None, 1, settings_row, broker) is broker


def test_too_recent_sip_window_falls_back_once_without_disabling_sip(tmp_path: Path):
    import requests
    from alpaca.common.exceptions import APIError

    class RecentDenied(Historical):
        def get_stock_bars(self, request):
            if request.feed == DataFeed.SIP and len(self.requests) == 0:
                self.requests.append(request)
                response = requests.Response()
                response.status_code = 403
                response._content = b'{"message":"subscription does not permit querying recent SIP data"}'
                raise APIError(response.text, requests.HTTPError(response=response))
            return super().get_stock_bars(request)

    historical = RecentDenied()
    service = attached_service(tmp_path, object(), historical)
    now = datetime.now(timezone.utc)
    first = service.get_bars(["SPY"], "1Day", now - timedelta(days=30), now, feed="sip")
    assert first["SPY"].attrs["data_feed"] == "iex" and service._sip_history_available is not False
    service.get_bars(["SPY"], "1Day", now - timedelta(days=31), now, feed="sip")
    assert historical.requests[-1].feed == DataFeed.SIP
