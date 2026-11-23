"""Server-side market data cache for the market center.

The market center used to ask Alpaca for full bar history on every request.
Daily bars are built from ~2.5 years of 30-minute bars, so a single daily chart
took ~25 s and blocked other data reads behind the shared client lock.

This cache keeps one frame per (symbol, timeframe) for all users, refreshes it
incrementally (only bars after the last cached one), and persists frames to
disk so a restart does not refetch years of history. It uses its own Alpaca
client, so its reads never queue behind the trading engine's data lock.
A background loop keeps watchlist symbols and recently viewed charts warm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from .alpaca_service import AlpacaService
from .quotes import summarize

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
TIMEFRAMES = {"5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60, "1Day": 1440}
INITIAL_BARS = {"1Day": 520}
DEFAULT_INITIAL_BARS = 600
KEEP_BARS = {"1Day": 600}
DEFAULT_KEEP_BARS = 1000
SNAPSHOT_SECONDS = 10.0
ACTIVE_SECONDS = 15 * 60       # keep a viewed chart warm this long after the last view
EVICT_SECONDS = 3 * 3600        # drop unused non-watchlist intraday frames after this long
WARM_TIMEFRAMES = ("15Min",)    # intraday frames kept warm for every watchlist symbol


def _now() -> datetime:
    return datetime.now(timezone.utc)


def market_active(now: datetime | None = None) -> bool:
    """Weekday extended session (04:00-20:00 ET), when prices can move."""
    local = (now or _now()).astimezone(ET)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 4 * 60 <= minutes < 20 * 60


def fresh_seconds(timeframe: str, active: bool) -> float:
    if timeframe == "1Day":
        return 15 * 60.0
    return 15.0 if active else 5 * 60.0


@dataclass
class Entry:
    frame: pd.DataFrame
    feed: str | None
    fetched_at: float          # monotonic
    fetched_wall: datetime
    last_access: float = 0.0
    saved_at: float = 0.0


class MarketCache:
    def __init__(self, client_factory: Callable[[], Any | None], directory: Path | str | None = None,
                 monotonic: Callable[[], float] = time.monotonic, clock: Callable[[], datetime] = _now):
        self._client_factory = client_factory
        self._client: Any | None = None
        self._client_checked: float | None = None
        self._dir = Path(directory) if directory else None
        self._monotonic = monotonic
        self._clock = clock
        self._entries: dict[tuple[str, str], Entry] = {}
        self._entries_lock = threading.Lock()
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._snapshots: dict[str, tuple[float, dict[str, Any]]] = {}
        self._snapshot_lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._loaded = False

    # ----- client -------------------------------------------------------
    def client(self) -> Any | None:
        if self._client is not None and getattr(self._client, "configured", False):
            return self._client
        if self._client_checked is not None and self._monotonic() - self._client_checked < 60:
            return None
        self._client_checked = self._monotonic()
        try:
            self._client = self._client_factory()
        except Exception:
            logger.exception("Market cache could not build its data client")
            self._client = None
        return self._client if self._client is not None and getattr(self._client, "configured", False) else None

    @property
    def available(self) -> bool:
        return self.client() is not None

    # ----- persistence --------------------------------------------------
    def _path(self, symbol: str, timeframe: str) -> Path | None:
        return self._dir / timeframe / f"{symbol}.json" if self._dir else None

    def load(self) -> int:
        """Load persisted frames; unreadable files are skipped and refetched."""
        self._loaded = True
        if not self._dir or not self._dir.exists():
            return 0
        count = 0
        for timeframe in TIMEFRAMES:
            folder = self._dir / timeframe
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                try:
                    data = json.loads(path.read_text())
                    index = pd.DatetimeIndex(pd.to_datetime(data["index"], utc=True), name="timestamp")
                    frame = pd.DataFrame(data["data"], index=index, columns=data["columns"]).astype(float)
                    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
                    fetched_wall = datetime.fromisoformat(data["fetched_at"])
                except Exception:
                    logger.warning("Ignoring unreadable market cache file %s", path.name)
                    continue
                age = max(0.0, (self._clock() - fetched_wall).total_seconds())
                self._entries[(path.stem.upper(), timeframe)] = Entry(
                    frame, data.get("feed"), self._monotonic() - age, fetched_wall,
                    last_access=self._monotonic() - ACTIVE_SECONDS, saved_at=self._monotonic())
                count += 1
        return count

    def _save(self, symbol: str, timeframe: str, entry: Entry) -> None:
        path = self._path(symbol, timeframe)
        if path is None:
            return
        if timeframe != "1Day" and entry.saved_at and self._monotonic() - entry.saved_at < 300:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            frame = entry.frame
            payload = {"feed": entry.feed, "fetched_at": entry.fetched_wall.isoformat(),
                       "index": [stamp.isoformat() for stamp in frame.index],
                       "columns": list(frame.columns), "data": frame.to_numpy().tolist()}
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload))
            os.replace(temp, path)
            entry.saved_at = self._monotonic()
        except Exception:
            logger.warning("Could not persist market cache for %s %s", symbol, timeframe)

    # ----- bars ---------------------------------------------------------
    def _lock_for(self, key: tuple[str, str]) -> threading.Lock:
        with self._entries_lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def peek(self, symbol: str, timeframe: str) -> pd.DataFrame | None:
        entry = self._entries.get((symbol.upper(), timeframe))
        return entry.frame if entry is not None else None

    def _fresh(self, entry: Entry | None, timeframe: str) -> bool:
        return entry is not None and self._monotonic() - entry.fetched_at < fresh_seconds(
            timeframe, market_active(self._clock()))

    def bars(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Cached bars; refreshes only when the cache is older than its freshness window."""
        symbol = symbol.upper()
        key = (symbol, timeframe)
        entry = self._entries.get(key)
        if entry is not None:
            entry.last_access = self._monotonic()
            if self._fresh(entry, timeframe):
                return self._view(entry, limit)
        with self._lock_for(key):
            entry = self._entries.get(key)
            if self._fresh(entry, timeframe):
                return self._view(entry, limit)
            try:
                self.refresh([symbol], timeframe)
            except Exception:
                if entry is None:
                    raise
                logger.warning("Serving cached %s %s after refresh failure", symbol, timeframe)
            entry = self._entries[key]
            entry.last_access = self._monotonic()
            return self._view(entry, limit)

    @staticmethod
    def _view(entry: Entry, limit: int) -> pd.DataFrame:
        frame = entry.frame.tail(limit).copy()
        frame.attrs["data_feed"] = entry.feed
        return frame

    def _has_data(self, symbol: str, timeframe: str) -> bool:
        entry = self._entries.get((symbol, timeframe))
        return entry is not None and not entry.frame.empty

    def refresh(self, symbols: Iterable[str], timeframe: str) -> None:
        """Fetch bars after the last cached bar (or the initial history) and merge."""
        client = self.client()
        if client is None:
            raise RuntimeError("行情缓存没有可用的 Alpaca 行情密钥")
        symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        now = self._clock()
        warm = [symbol for symbol in symbols if self._has_data(symbol, timeframe)]
        cold = [symbol for symbol in symbols if symbol not in warm]
        overlap = timedelta(days=10) if timeframe == "1Day" else timedelta(minutes=TIMEFRAMES[timeframe] * 3)
        if warm:
            start = min(self._entries[(symbol, timeframe)].frame.index[-1] for symbol in warm).to_pydatetime() - overlap
            self._fetch_merge(client, warm, timeframe, start, now)
        for symbol in cold:   # initial history is large; fetch one symbol at a time
            days = AlpacaService._recent_window_days(timeframe, INITIAL_BARS.get(timeframe, DEFAULT_INITIAL_BARS))
            self._fetch_merge(client, [symbol], timeframe, now - timedelta(days=days), now)

    def _fetch_merge(self, client: Any, symbols: list[str], timeframe: str, start: datetime, end: datetime) -> None:
        frames = client.get_bars(symbols, timeframe, start, end, limit=None,
                                 feed="sip" if timeframe == "1Day" else "iex")
        keep = KEEP_BARS.get(timeframe, DEFAULT_KEEP_BARS)
        for symbol in symbols:
            fresh = frames.get(symbol, pd.DataFrame())
            key = (symbol, timeframe)
            previous = self._entries.get(key)
            if previous is not None and not previous.frame.empty:
                merged = pd.concat([previous.frame, fresh]) if not fresh.empty else previous.frame
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            else:
                merged = fresh
            merged = merged.tail(keep).copy()
            merged.attrs = {}
            feed = fresh.attrs.get("data_feed") if not fresh.empty else (previous.feed if previous else None)
            entry = Entry(merged, feed, self._monotonic(), self._clock(),
                          last_access=previous.last_access if previous else self._monotonic(),
                          saved_at=previous.saved_at if previous else 0.0)
            with self._entries_lock:
                self._entries[key] = entry
            if not fresh.empty:
                self._save(symbol, timeframe, entry)

    # ----- quotes -------------------------------------------------------
    def snapshots(self, symbols: list[str]) -> dict[str, Any]:
        symbols = [symbol.upper() for symbol in symbols]

        def stale_of(items: list[str]) -> list[str]:
            now = self._monotonic()
            return [symbol for symbol in items
                    if symbol not in self._snapshots or now - self._snapshots[symbol][0] >= SNAPSHOT_SECONDS]

        if stale_of(symbols):
            with self._snapshot_lock:
                stale = stale_of(symbols)
                if stale:
                    client = self.client()
                    if client is None:
                        raise RuntimeError("行情缓存没有可用的 Alpaca 行情密钥")
                    fetched = client.get_snapshots(stale) or {}
                    for symbol in stale:
                        if symbol in fetched:
                            self._snapshots[symbol] = (self._monotonic(), fetched[symbol])
        return {symbol: self._snapshots[symbol][1] for symbol in symbols if symbol in self._snapshots}

    def quotes(self, symbols: list[str]) -> dict[str, Any]:
        """Quote rows without waiting for cold daily history.

        A symbol whose daily history is not cached yet falls back to the
        snapshot's previous daily bar; the background loop fills the history.
        """
        symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        errors: list[str] = []
        try:
            snaps = self.snapshots(symbols)
        except Exception as exc:
            snaps, errors = {}, [f"实时报价暂不可用：{type(exc).__name__}"]
        quotes = []
        for symbol in symbols:
            entry = self._entries.get((symbol, "1Day"))
            if entry is not None:
                entry.last_access = self._monotonic()
            daily = self._view(entry, 260) if entry is not None and not entry.frame.empty else None
            quotes.append(summarize(symbol, daily, snaps.get(symbol), now=self._clock()))
            if entry is None:
                # Placeholder so the background loop picks the symbol up on its next pass.
                with self._entries_lock:
                    self._entries.setdefault((symbol, "1Day"), Entry(pd.DataFrame(), None, float("-inf"), self._clock(),
                                                                    last_access=self._monotonic()))
        return {"as_of": self._clock().isoformat(), "errors": errors, "quotes": quotes}

    # ----- background ---------------------------------------------------
    def tick(self, watchlist: list[str]) -> None:
        if not self._loaded:
            self.load()
        if self.client() is None:
            return
        active = market_active(self._clock())
        now = self._monotonic()
        watch = list(dict.fromkeys(symbol.upper() for symbol in watchlist))
        if watch and active:
            try:
                self.snapshots(watch)
            except Exception:
                logger.warning("Market cache snapshot refresh failed", exc_info=True)
        wanted: dict[str, set[str]] = {timeframe: set() for timeframe in TIMEFRAMES}
        wanted["1Day"].update(watch)
        for timeframe in WARM_TIMEFRAMES:
            wanted[timeframe].update(watch)
        for (symbol, timeframe), entry in list(self._entries.items()):
            if now - entry.last_access < ACTIVE_SECONDS:
                wanted[timeframe].add(symbol)
            elif timeframe != "1Day" and symbol not in wanted[timeframe] and now - entry.last_access > EVICT_SECONDS:
                with self._entries_lock:
                    self._entries.pop((symbol, timeframe), None)
        for timeframe, symbols in wanted.items():
            due = [symbol for symbol in sorted(symbols) if not self._fresh(self._entries.get((symbol, timeframe)), timeframe)]
            warm = [symbol for symbol in due if self._has_data(symbol, timeframe)]
            cold = [symbol for symbol in due if symbol not in warm]
            # Warm symbols share one small incremental request; each cold symbol
            # loads separately so a long initial download never holds other keys.
            for batch in ([warm] if warm else []) + [[symbol] for symbol in cold]:
                self._refresh_unlocked_only(batch, timeframe)

    def _refresh_unlocked_only(self, symbols: list[str], timeframe: str) -> None:
        locks = {symbol: self._lock_for((symbol, timeframe)) for symbol in symbols}
        acquired = [symbol for symbol, lock in locks.items() if lock.acquire(blocking=False)]
        try:
            if acquired:
                self.refresh(acquired, timeframe)
        except Exception:
            logger.warning("Market cache refresh failed for %s %s", timeframe, ",".join(acquired), exc_info=True)
        finally:
            for symbol in acquired:
                locks[symbol].release()

    async def run(self, watchlist_reader: Callable[[], list[str]], interval: float = 15.0) -> None:
        while True:
            try:
                symbols = await asyncio.to_thread(watchlist_reader)
                await asyncio.to_thread(self.tick, symbols)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Market cache loop failed")
            await asyncio.sleep(interval if market_active(self._clock()) else interval * 4)

    def start(self, watchlist_reader: Callable[[], list[str]]) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(watchlist_reader), name="market-cache")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
