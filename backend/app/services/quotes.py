"""Watchlist quote rows and the selected-symbol detail panel.

Price comes from the real-time feed (IEX latest trade); reference values that
need the whole market (previous close, volume averages, 52-week range) come
from completed daily bars, which use consolidated SIP history on Alpaca.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .risk import as_float

ET = ZoneInfo("America/New_York")
CACHE_SECONDS = 10
_cache: dict[tuple[int, tuple[str, ...]], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _session(stamp: datetime) -> str:
    local = stamp.astimezone(ET)
    minutes = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return "closed"
    if minutes < 9 * 60 + 30:
        return "pre"
    if minutes >= 16 * 60:
        return "post"
    return "regular"


def summarize(symbol: str, daily: pd.DataFrame | None, snapshot: dict[str, Any] | None,
              now: datetime | None = None) -> dict[str, Any]:
    snapshot = snapshot or {}
    trade = snapshot.get("latest_trade") or {}
    minute = snapshot.get("minute_bar") or {}
    today = snapshot.get("daily_bar") or {}
    price = as_float(trade.get("price")) or as_float(minute.get("close")) or None
    price_time = _parse(trade.get("timestamp") or minute.get("timestamp"))
    daily = daily if daily is not None else pd.DataFrame()
    if price is None and not daily.empty:
        price, price_time = float(daily["close"].iloc[-1]), daily.index[-1].to_pydatetime()
    reference_date = (price_time or now or datetime.now(timezone.utc)).astimezone(ET).date()
    completed = daily
    if not daily.empty:
        completed = daily[daily.index.tz_convert(ET).date < reference_date]
    prev_close = float(completed["close"].iloc[-1]) if not completed.empty else (
        as_float((snapshot.get("previous_daily_bar") or {}).get("close")) or None)
    today_date = _parse(today.get("timestamp"))
    today_is_current = bool(today_date and today_date.astimezone(ET).date() == reference_date)
    year = completed.tail(252)
    highs = ([float(year["high"].max())] if not year.empty else []) + (
        [as_float(today.get("high"))] if today_is_current and as_float(today.get("high")) > 0 else [])
    lows = ([float(year["low"].min())] if not year.empty else []) + (
        [as_float(today.get("low"))] if today_is_current and as_float(today.get("low")) > 0 else [])
    high_52, low_52 = (max(highs) if highs else None), (min(lows) if lows else None)
    change = price - prev_close if price is not None and prev_close else None
    return {
        "symbol": symbol,
        "price": price,
        "price_time": price_time.isoformat() if price_time else None,
        "session": _session(price_time) if price_time else None,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change / prev_close * 100 if change is not None and prev_close else None,
        "open": as_float(today.get("open")) or None if today_is_current else None,
        "high": as_float(today.get("high")) or None if today_is_current else None,
        "low": as_float(today.get("low")) or None if today_is_current else None,
        "volume_live": as_float(today.get("volume")) if today_is_current else None,
        "prev_volume": float(completed["volume"].iloc[-1]) if not completed.empty else None,
        "avg_volume_30": float(completed["volume"].tail(30).mean()) if not completed.empty else None,
        "high_52w": high_52,
        "low_52w": low_52,
        "history_feed": daily.attrs.get("data_feed") if not daily.empty else None,
        "live_feed": "iex" if snapshot else None,
    }


def market_quotes(client: Any, symbols: list[str]) -> dict[str, Any]:
    symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    if not symbols:
        return {"as_of": datetime.now(timezone.utc).isoformat(), "quotes": []}
    key = (id(client), tuple(symbols))
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    errors: list[str] = []
    try:
        daily = client.recent_bars(symbols, "1Day", 260)
    except Exception as exc:
        daily, errors = {}, [f"日线数据暂不可用：{type(exc).__name__}"]
    snapshots: dict[str, Any] = {}
    reader = getattr(client, "get_snapshots", None)
    if reader is not None:
        try:
            snapshots = reader(symbols) or {}
        except Exception as exc:
            errors.append(f"实时报价暂不可用：{type(exc).__name__}")
    result = {"as_of": datetime.now(timezone.utc).isoformat(), "errors": errors,
              "quotes": [summarize(symbol, daily.get(symbol), snapshots.get(symbol)) for symbol in symbols]}
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
        if len(_cache) > 64:
            _cache.pop(min(_cache, key=lambda item: _cache[item][0]))
    return result

