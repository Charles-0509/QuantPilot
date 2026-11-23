from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.api import market_cache_for
from app.services.market_cache import MarketCache


class Clock:
    def __init__(self):
        self.wall = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)   # Tuesday 11:00 ET, market open
        self.mono = 1000.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


class FakeData:
    configured = True

    def __init__(self, clock):
        self.clock = clock
        self.bar_calls = []
        self.snapshot_calls = []
        self.fail = False

    def get_bars(self, symbols, timeframe, start, end, limit=None, feed="iex"):
        self.bar_calls.append((tuple(symbols), timeframe, start, end, feed))
        if self.fail:
            raise RuntimeError("upstream down")
        step = timedelta(days=1) if timeframe == "1Day" else timedelta(minutes=15)
        stamps = pd.date_range(start=pd.Timestamp(start).ceil("15min"), end=pd.Timestamp(end) - step, freq=step)
        out = {}
        for symbol in symbols:
            frame = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": [float(i) for i in range(len(stamps))],
                                  "volume": 10.0}, index=stamps)
            frame.attrs["data_feed"] = feed
            out[symbol] = frame
        return out

    def get_snapshots(self, symbols):
        self.snapshot_calls.append(tuple(symbols))
        return {symbol: {"latest_trade": {"price": 101.0, "timestamp": self.clock.wall.isoformat()},
                         "previous_daily_bar": {"close": 100.0}} for symbol in symbols}


def make(tmp_path, clock=None):
    clock = clock or Clock()
    data = FakeData(clock)
    cache = MarketCache(lambda: data, tmp_path / "cache", monotonic=clock.monotonic, clock=clock.now)
    return cache, data, clock


def test_bars_fetch_history_once_then_only_new_bars(tmp_path):
    cache, data, clock = make(tmp_path)
    first = cache.bars("spy", "15Min", 600)
    assert len(data.bar_calls) == 1 and data.bar_calls[0][2] < clock.wall - timedelta(days=10)
    assert first.attrs["data_feed"] == "iex" and len(first) == 600

    cache.bars("SPY", "15Min", 600)                   # within the freshness window: no upstream call
    assert len(data.bar_calls) == 1

    clock.advance(15 * 60)                            # one new 15-minute bar has completed
    latest_before = cache.peek("SPY", "15Min").index[-1]
    refreshed = cache.bars("SPY", "15Min", 600)
    start = data.bar_calls[-1][2]
    assert len(data.bar_calls) == 2 and start == latest_before.to_pydatetime() - timedelta(minutes=45)
    assert refreshed.index[-1] > latest_before and refreshed.index.is_unique


def test_daily_history_uses_consolidated_feed_and_is_restored_after_restart(tmp_path):
    cache, data, clock = make(tmp_path)
    cache.bars("AAPL", "1Day", 500)
    assert data.bar_calls[0][4] == "sip"

    restarted, data2, _ = make(tmp_path, clock)
    assert restarted.load() == 1
    frame = restarted.bars("AAPL", "1Day", 500)
    assert data2.bar_calls == [] and len(frame) == 500 and frame.attrs["data_feed"] == "sip"


def test_quotes_never_wait_for_cold_daily_history(tmp_path):
    cache, data, clock = make(tmp_path)
    result = cache.quotes(["NVDA", "QQQ"])
    assert data.bar_calls == [] and data.snapshot_calls == [("NVDA", "QQQ")]
    assert result["quotes"][0]["prev_close"] == 100.0 and result["quotes"][0]["change"] == pytest.approx(1.0)

    cache.quotes(["NVDA"])                            # snapshots are reused for 10 seconds
    assert len(data.snapshot_calls) == 1

    cache.tick([])                                    # background pass loads the history the quote asked for
    loaded = {call[0] for call in data.bar_calls if call[1] == "1Day"}
    assert loaded == {("NVDA",), ("QQQ",)}
    assert cache.quotes(["NVDA"])["quotes"][0]["avg_volume_30"] == 10.0


def test_refresh_failure_serves_the_cached_frame(tmp_path):
    cache, data, clock = make(tmp_path)
    cache.bars("TSLA", "15Min", 100)
    clock.advance(60)
    data.fail = True
    assert len(cache.bars("TSLA", "15Min", 100)) == 100
    with pytest.raises(RuntimeError):
        cache.bars("GOOGL", "15Min", 100)             # nothing cached yet: the error surfaces


def test_tick_keeps_watchlist_warm_with_one_incremental_request(tmp_path):
    cache, data, clock = make(tmp_path)
    cache.tick(["SPY", "QQQ"])
    initial = [call for call in data.bar_calls if call[1] == "15Min"]
    assert sorted(call[0] for call in initial) == [("QQQ",), ("SPY",)]   # cold loads are per symbol
    clock.advance(30)
    data.bar_calls.clear()
    cache.tick(["SPY", "QQQ"])
    assert [call[0] for call in data.bar_calls if call[1] == "15Min"] == [("QQQ", "SPY")]
    assert not [call for call in data.bar_calls if call[1] == "1Day"]    # daily stays fresh for 15 minutes


def test_cache_is_skipped_for_ibkr_market_data(tmp_path):
    cache, _data, _clock = make(tmp_path)

    class Request:
        class app:
            class state:
                market_cache = cache

    class Ibkr:
        configured = True
        broker_name = "ibkr"

    class Alpaca:
        configured = True
        broker_name = "alpaca"

    assert market_cache_for(Request, Ibkr()) is None
    assert market_cache_for(Request, Alpaca()) is cache
