from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import api as api_module
from app.models import Strategy, WatchlistGroup, WatchlistItem
from app.services.quotes import summarize
from app.templates import seed_user_defaults
from tests.test_auth import auth_app, csrf_header, setup  # noqa: F401


def daily(dates, closes, volumes=None):
    index = pd.DatetimeIndex([pd.Timestamp(day, tz="America/New_York").tz_convert("UTC") for day in dates])
    frame = pd.DataFrame({"open": closes, "high": [c + 2 for c in closes], "low": [c - 2 for c in closes],
                          "close": closes, "volume": volumes or [1_000_000.0] * len(closes)}, index=index)
    frame.attrs["data_feed"] = "sip"
    return frame


def test_quote_uses_previous_session_close_even_after_todays_bar_completes():
    bars = daily(["2026-11-18", "2026-11-19", "2026-11-20"], [100.0, 102.0, 110.0], [10.0, 20.0, 30.0])
    snapshot = {"latest_trade": {"price": 104.0, "timestamp": "2026-11-20T20:59:00Z"},
                "daily_bar": {"timestamp": "2026-11-20T05:00:00Z", "open": 101, "high": 112, "low": 96, "volume": 5}}
    quote = summarize("SPY", bars, snapshot)
    assert quote["prev_close"] == 102.0 and quote["change"] == pytest.approx(2.0)
    assert quote["change_pct"] == pytest.approx(2 / 102 * 100)
    assert quote["prev_volume"] == 20.0 and quote["avg_volume_30"] == 15.0
    assert quote["high_52w"] == 112 and quote["low_52w"] == 96
    assert quote["session"] == "regular" and quote["history_feed"] == "sip"


def test_premarket_trade_is_labelled_and_compared_with_last_close():
    bars = daily(["2026-11-19", "2026-11-20"], [100.0, 105.0])
    quote = summarize("SPY", bars, {"latest_trade": {"price": 106.0, "timestamp": "2026-11-23T13:10:00Z"}})
    assert quote["session"] == "pre" and quote["prev_close"] == 105.0 and quote["open"] is None


def test_missing_realtime_falls_back_to_last_daily_close():
    quote = summarize("SPY", daily(["2026-11-19", "2026-11-20"], [100.0, 101.0]), None)
    assert quote["price"] == 101.0 and quote["prev_close"] == 100.0 and quote["live_feed"] is None


class Broker:
    configured = True

    def __init__(self):
        self.assets = {"AAPL": {"name": "Apple Inc.", "exchange": "AssetExchange.NASDAQ", "tradable": True}}

    def get_asset(self, symbol):
        if symbol not in self.assets:
            raise RuntimeError("not found")
        return self.assets[symbol]

    def recent_bars(self, symbols, timeframe, bars=300):
        return {symbol: daily(["2026-11-19", "2026-11-20"], [100.0, 101.0]) for symbol in symbols}

    def get_snapshots(self, symbols):
        return {symbol: {"latest_trade": {"price": 103.0, "timestamp": "2026-11-23T15:00:00Z"}} for symbol in symbols}


@pytest.fixture
def market_client(auth_app):  # noqa: F811
    app, sessions = auth_app
    broker = Broker()
    app.dependency_overrides[api_module.get_alpaca] = lambda: broker
    with TestClient(app) as client:
        setup(client)
        with sessions() as db:
            seed_user_defaults(db, 1)
            group = db.scalar(select(WatchlistGroup).where(WatchlistGroup.user_id == 1))
            for row in db.scalars(select(WatchlistItem)).all():
                db.delete(row)
            db.add_all([WatchlistItem(user_id=1, symbol="SPY", group_id=group.id, position=0),
                        WatchlistItem(user_id=1, symbol="QQQ", group_id=group.id, position=1)])
            db.commit()
        yield client, sessions, broker


def test_groups_can_be_created_renamed_reordered_and_deleted_without_losing_symbols(market_client):
    client, sessions, _ = market_client
    headers = csrf_header(client)
    groups = client.get("/api/watchlist/groups").json()
    assert [g["name"] for g in groups] == ["自选"] and groups[0]["symbols"] == ["SPY", "QQQ"]
    first = groups[0]["id"]
    groups = client.post("/api/watchlist/groups", json={"name": "ETF"}, headers=headers).json()
    etf = groups[1]["id"]
    groups = client.post("/api/watchlist/items", json={"symbol": "aapl", "group_id": etf}, headers=headers).json()
    assert groups[1]["symbols"] == ["AAPL"]
    assert client.post("/api/watchlist/items", json={"symbol": "AAPL", "group_id": first}, headers=headers).status_code == 409
    assert client.post("/api/watchlist/items", json={"symbol": "ZZZZ", "group_id": first}, headers=headers).status_code == 422
    groups = client.patch(f"/api/watchlist/groups/{etf}", json={"name": "科技股", "position": 0}, headers=headers).json()
    assert [g["name"] for g in groups] == ["科技股", "自选"]
    groups = client.patch("/api/watchlist/items/QQQ", json={"group_id": etf, "position": 0}, headers=headers).json()
    assert groups[0]["symbols"] == ["QQQ", "AAPL"] and groups[1]["symbols"] == ["SPY"]
    groups = client.delete(f"/api/watchlist/groups/{etf}", headers=headers).json()
    assert [g["name"] for g in groups] == ["自选"] and groups[0]["symbols"] == ["SPY", "QQQ", "AAPL"]
    assert client.delete(f"/api/watchlist/groups/{first}", headers=headers).status_code == 409
    assert client.get("/api/watchlist").json() == ["AAPL", "QQQ", "SPY"]


def test_deleted_default_symbols_are_not_restored_on_restart(market_client):
    client, sessions, _ = market_client
    client.delete("/api/watchlist/items/SPY", headers=csrf_header(client))
    with sessions() as db:
        seed_user_defaults(db, 1)
        assert db.scalars(select(WatchlistItem.symbol).where(WatchlistItem.user_id == 1)).all() == ["QQQ"]
        assert len(db.scalars(select(WatchlistGroup)).all()) == 1


def test_watchlist_and_enabled_strategies_share_the_30_symbol_stream_limit(market_client):
    client, sessions, broker = market_client
    group = client.get("/api/watchlist/groups").json()[0]["id"]
    with sessions() as db:
        db.add(Strategy(id="s", owner_user_id=1, name="big", enabled=True,
                        definition={"symbols": [f"S{index}" for index in range(28)]}))
        db.commit()
    broker.assets["MSFT"] = {"name": "Microsoft", "tradable": True}
    response = client.post("/api/watchlist/items", json={"symbol": "MSFT", "group_id": group}, headers=csrf_header(client))
    assert response.status_code == 409 and "30" in response.json()["detail"]


def test_other_users_groups_are_not_reachable(market_client):
    client, sessions, _ = market_client
    with sessions() as db:
        db.add(WatchlistGroup(id=99, user_id=2, name="别人的", position=0))
        db.commit()
    headers = csrf_header(client)
    assert client.patch("/api/watchlist/groups/99", json={"name": "x"}, headers=headers).status_code == 404
    assert client.post("/api/watchlist/items", json={"symbol": "AAPL", "group_id": 99}, headers=headers).status_code == 404


def test_quotes_and_detail_endpoints(market_client):
    client, _, _ = market_client
    response = client.get("/api/market/watch-quotes?symbols=SPY,AAPL")
    assert response.status_code == 200, response.text
    quotes = response.json()["quotes"]
    assert [q["symbol"] for q in quotes] == ["SPY", "AAPL"] and quotes[0]["change"] == pytest.approx(2.0)
    detail = client.get("/api/market/detail?symbol=AAPL").json()
    assert detail["name"] == "Apple Inc." and detail["exchange"] == "NASDAQ" and detail["price"] == 103.0
    assert client.get("/api/market/watch-quotes?symbols=SPY;DROP").status_code == 422
