"""Historical simulation of buy-to-open option strategies on real option bars.

Signals come from the underlying's bars exactly as in live trading. On an entry
signal the contract is chosen with the same rule as the live engine from the
contracts listed at that date (expired contracts included), and it is bought at
the open of its next option bar. Exits: the strategy's exit rule (next option
bar open), premium stop/take-profit on option bar lows/highs, the pre-expiry
close rule, or intrinsic value at expiration if a contract is still held.

Alpaca's historical option data starts in February 2024. Free bars come from
the indicative feed, so fills are estimates rather than exchange prints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Protocol

import pandas as pd

from app.schemas import RuleDefinition

from .backtest import BacktestResult, MAX_CHART_POINTS, build_benchmark_curve, calculate_metrics, curve_sample_indices
from .indicators import ensure_frame
from .instruments import OPTION_MULTIPLIER, choose_contract, days_to_expiry, parse_option_symbol
from .rules import evaluate_tree, required_history

OPTION_DATA_START = pd.Timestamp("2024-02-01", tz="UTC")


class OptionHistory(Protocol):
    def contracts(self, underlying: str, right: str, expiration_gte: date, expiration_lte: date,
                  strike_gte: float, strike_lte: float) -> list[dict[str, Any]]: ...

    def bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame: ...


@dataclass(slots=True)
class OptionPosition:
    underlying: str
    symbol: str
    qty: int
    premium: float
    entry_time: pd.Timestamp
    bars: pd.DataFrame
    fees: float = 0.0


def _et_date(timestamp: pd.Timestamp) -> date:
    stamp = pd.Timestamp(timestamp)
    return (stamp.tz_convert("America/New_York") if stamp.tzinfo else stamp).date()


def run_option_backtest(
    definition: RuleDefinition,
    bars_by_symbol: dict[str, pd.DataFrame],
    history: OptionHistory,
    initial_cash: float = 100_000,
    slippage_bps: float = 5,
    commission: float = 0,
    benchmark: pd.DataFrame | None = None,
    *,
    start: Any = None,
    end: Any = None,
    risk_settings: dict[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> BacktestResult:
    config = definition.option
    if config is None:
        raise ValueError("期权回测需要期权参数")
    frames = {symbol: ensure_frame(frame) for symbol, frame in bars_by_symbol.items() if frame is not None and not frame.empty}
    if not frames:
        raise ValueError("没有可用于回测的K线数据")
    signals = {symbol: (evaluate_tree(frame, definition.entry), evaluate_tree(frame, definition.exit))
               for symbol, frame in frames.items()}
    required_bars = max(definition.warmup_bars, required_history(definition))
    limits = risk_settings or {}
    max_positions = min(definition.risk.max_positions, limits.get("max_positions", 30))
    max_symbol_pct = min(definition.risk.max_symbol_pct, limits.get("max_symbol_pct", 100))
    max_exposure_pct = limits.get("max_total_exposure_pct", 100)
    slippage = slippage_bps / 10_000

    timeline = pd.DatetimeIndex([])
    for frame in frames.values():
        timeline = timeline.union(frame.index)
    timeline = timeline.sort_values()
    if start is not None:
        timeline = timeline[timeline >= pd.Timestamp(start)]
    if timeline.empty:
        raise ValueError("回测区间没有K线数据")
    if timeline[0] < OPTION_DATA_START:
        raise ValueError("期权历史数据从 2024 年 2 月开始，请把回测开始日期设在 2024-02-01 之后")
    final_end = pd.Timestamp(end) if end is not None else timeline[-1] + pd.Timedelta(days=1)

    cash = float(initial_cash)
    positions: dict[str, OptionPosition] = {}       # keyed by underlying
    pending: dict[str, tuple[str, pd.Timestamp, str]] = {}   # underlying -> (action, created_at, reason)
    last_entry_index: dict[str, int] = {}
    trades: list[dict[str, Any]] = []
    equity_points: list[dict[str, Any]] = []
    exposure_points: list[float] = []
    marks: dict[str, float] = {}
    underlying_close: dict[str, float] = {}

    def mark_value() -> float:
        return sum(position.qty * OPTION_MULTIPLIER * marks.get(position.symbol, position.premium)
                   for position in positions.values())

    def equity() -> float:
        return cash + mark_value()

    def option_bar(position_or_bars, timestamp, *, after: bool = False, same_day: bool = True):
        """First option bar at/after a time; entries never fill on a later session."""
        bars = position_or_bars.bars if isinstance(position_or_bars, OptionPosition) else position_or_bars
        if bars.empty:
            return None, None
        index = bars.index.searchsorted(timestamp, side="right" if after else "left")
        if index >= len(bars):
            return None, None
        stamp = bars.index[index]
        if same_day and _et_date(stamp) != _et_date(timestamp):
            return None, None
        return stamp, bars.iloc[index]

    def close(underlying: str, timestamp: pd.Timestamp, price: float, reason: str) -> None:
        nonlocal cash
        position = positions.pop(underlying)
        exit_price = max(0.0, price * (1 - slippage))
        proceeds = position.qty * OPTION_MULTIPLIER * exit_price - commission
        cash += proceeds
        cost = position.qty * OPTION_MULTIPLIER * position.premium + position.fees
        trades.append({
            "symbol": position.symbol, "underlying": underlying, "instrument": "option",
            "entry_time": position.entry_time.isoformat(), "exit_time": pd.Timestamp(timestamp).isoformat(),
            "qty": position.qty, "entry_price": round(position.premium, 4), "exit_price": round(exit_price, 4),
            "pnl": round(proceeds - cost, 2),
            "return_pct": round((exit_price / position.premium - 1) * 100, 3) if position.premium else 0.0,
            "reason": reason,
            "bars_held": max(1, int(frames[underlying].index.searchsorted(timestamp, side="right")
                                    - frames[underlying].index.searchsorted(position.entry_time, side="left"))),
        })

    def open_position(underlying: str, signal_time: pd.Timestamp, timestamp: pd.Timestamp) -> None:
        nonlocal cash
        spot = underlying_close.get(underlying)
        if not spot:
            return
        today = _et_date(signal_time)
        room = max(0.25, config.strike_offset_pct / 100 + 0.15)
        contracts = history.contracts(underlying, config.right, today + timedelta(days=config.min_dte),
                                      today + timedelta(days=config.max_dte), spot * (1 - room), spot * (1 + room))
        # Historical open interest is not available per date; only the listing rule applies.
        relaxed = config.model_copy(update={"min_open_interest": 0})
        contract = choose_contract(contracts, spot, relaxed, today)
        if contract is None:
            return
        symbol = str(contract["symbol"]).upper()
        parsed = parse_option_symbol(symbol)
        bars = ensure_frame(history.bars(symbol, pd.Timestamp(signal_time) - pd.Timedelta(days=1),
                                         min(final_end, pd.Timestamp(parsed.expiration, tz="America/New_York")
                                             + pd.Timedelta(days=1))))
        stamp, bar = option_bar(bars, signal_time, after=True, same_day=False)
        if bar is None or stamp > timestamp or _et_date(stamp) != _et_date(timestamp):
            # No option print between the signal and this bar: skip rather than fill in the future.
            return
        fill = float(bar["open"]) * (1 + slippage)
        if fill <= 0:
            return
        current = equity()
        config_position = definition.position
        cost = fill * OPTION_MULTIPLIER
        if config_position.mode == "fixed_qty":
            qty = int(math.floor(config_position.value))
        else:
            budget = config_position.value if config_position.mode == "fixed_notional" else current * config_position.value / 100
            qty = int(math.floor(budget / cost + 1e-9))
        qty = min(qty, int(current * max_symbol_pct / 100 // cost),
                  int(max(0.0, current * max_exposure_pct / 100 - mark_value()) // cost),
                  int(max(0.0, cash - commission) // cost))
        if qty < 1:
            return
        cash -= qty * cost + commission
        positions[underlying] = OptionPosition(underlying, symbol, qty, fill, stamp, bars, commission)
        marks[symbol] = float(bar["close"])
        last_entry_index[underlying] = frames[underlying].index.get_loc(timestamp)

    previous_timestamp: pd.Timestamp | None = None
    for timestamp in timeline:
        rows = {symbol: frame.loc[timestamp] for symbol, frame in frames.items() if timestamp in frame.index}
        for symbol, row in rows.items():
            underlying_close[symbol] = float(row["close"])
        today = _et_date(timestamp)

        # Pending entries/exits execute on the next option bar after their signal.
        for underlying, (action, created_at, reason) in list(pending.items()):
            if timestamp <= created_at or underlying not in rows:
                continue
            pending.pop(underlying)
            if action == "exit" and underlying in positions:
                position = positions[underlying]
                stamp, bar = option_bar(position, created_at, after=True, same_day=False)
                if bar is not None and stamp <= timestamp:
                    close(underlying, stamp, float(bar["open"]), reason)
                else:
                    pending[underlying] = (action, created_at, reason)   # no print yet; retry next bar
            elif action == "entry" and underlying not in positions and len(positions) < max_positions:
                open_position(underlying, created_at, timestamp)

        # Premium stop/take-profit, pre-expiry close and expiration settlement.
        for underlying, position in list(positions.items()):
            parsed = parse_option_symbol(position.symbol)
            seen = position.bars[position.bars.index <= timestamp]
            if not seen.empty:
                marks[position.symbol] = float(seen.iloc[-1]["close"])
            window_start = max(position.entry_time, previous_timestamp) if previous_timestamp is not None else position.entry_time
            day_bars = seen[seen.index > window_start]
            if today > parsed.expiration:
                spot = underlying_close.get(underlying, 0.0)
                intrinsic = max(0.0, spot - parsed.strike) if parsed.right == "call" else max(0.0, parsed.strike - spot)
                close(underlying, timestamp, intrinsic / (1 - slippage) if intrinsic else 0.0, "到期按内在价值结算")
                continue
            if days_to_expiry(parsed.expiration, today) <= config.close_dte:
                stamp, bar = option_bar(position, timestamp)
                if bar is not None:
                    close(underlying, stamp, float(bar["open"]), "临近到期平仓")
                continue
            # Conservative order inside each option bar: stop before take-profit.
            stop_level = position.premium * (1 - config.stop_loss_pct / 100) if config.stop_loss_pct else None
            take_level = position.premium * (1 + config.take_profit_pct / 100) if config.take_profit_pct else None
            for stamp, bar in day_bars.iterrows():
                if stop_level is not None and float(bar["low"]) <= stop_level:
                    close(underlying, stamp, min(float(bar["open"]), stop_level), "权利金止损")
                    break
                if take_level is not None and float(bar["high"]) >= take_level:
                    close(underlying, stamp, max(float(bar["open"]), take_level), "权利金止盈")
                    break

        # Underlying signals after the completed bar.
        for symbol, frame in frames.items():
            if symbol not in rows or symbol in pending:
                continue
            bar_index = frame.index.get_loc(timestamp)
            if bar_index + 1 < required_bars or today.weekday() not in definition.schedule.weekdays:
                continue
            entry_signal, exit_signal = signals[symbol]
            if symbol in positions:
                if bool(exit_signal.loc[timestamp]):
                    pending[symbol] = ("exit", timestamp, "规则离场")
                continue
            cooldown_ok = bar_index - last_entry_index.get(symbol, -10_000) >= definition.risk.cooldown_bars
            if cooldown_ok and bool(entry_signal.loc[timestamp]):
                pending[symbol] = ("entry", timestamp, "")

        previous_timestamp = timestamp
        value = equity()
        equity_points.append({"timestamp": timestamp.isoformat(), "equity": round(value, 2)})
        exposure_points.append(max(0.0, mark_value() / value) if value else 0)
        if progress is not None and len(equity_points) % 500 == 0:
            progress(f"{len(equity_points)}/{len(timeline)}")

    final = timeline[-1]
    for underlying in list(positions):
        position = positions[underlying]
        close(underlying, final, marks.get(position.symbol, position.premium), "回测结束")
    if equity_points:
        equity_points[-1]["equity"] = round(cash, 2)
    metrics = calculate_metrics(equity_points, trades, initial_cash, exposure_points, definition.timeframe)
    benchmark_curve = (build_benchmark_curve(benchmark, initial_cash, [pd.Timestamp(p["timestamp"]) for p in equity_points])
                       if benchmark is not None and not benchmark.empty else [])
    indices = curve_sample_indices(equity_points, MAX_CHART_POINTS)
    return BacktestResult(metrics, [equity_points[i] for i in indices],
                          [benchmark_curve[i] for i in indices] if benchmark_curve else [], trades)


class RecordingOptionHistory:
    """Live Alpaca-backed history that records every response for the artifact."""

    def __init__(self, client: Any, timeframe: str):
        self.client = client
        self.timeframe = timeframe
        self.chains: dict[str, list[dict[str, Any]]] = {}
        self.frames: dict[str, pd.DataFrame] = {}

    @staticmethod
    def chain_key(underlying, right, expiration_gte, expiration_lte, strike_gte, strike_lte) -> str:
        return f"{underlying}|{right}|{expiration_gte}|{expiration_lte}|{strike_gte:.2f}|{strike_lte:.2f}"

    def contracts(self, underlying, right, expiration_gte, expiration_lte, strike_gte, strike_lte):
        key = self.chain_key(underlying, right, expiration_gte, expiration_lte, strike_gte, strike_lte)
        if key not in self.chains:
            rows = self.client.get_option_contracts(
                underlying, right=right, expiration_gte=expiration_gte, expiration_lte=expiration_lte,
                strike_gte=strike_gte, strike_lte=strike_lte, include_expired=True)
            self.chains[key] = [{k: row.get(k) for k in ("symbol", "type", "style", "tradable", "expiration_date",
                                                        "strike_price", "status")} | {"tradable": True}
                                for row in rows]
        return self.chains[key]

    def bars(self, symbol, start, end):
        if symbol not in self.frames:
            frames = self.client.get_option_bars([symbol], self.timeframe, start.to_pydatetime(), end.to_pydatetime())
            self.frames[symbol] = frames.get(symbol, pd.DataFrame())
        return self.frames[symbol]


class ReplayOptionHistory:
    def __init__(self, chains: dict[str, list[dict[str, Any]]], frames: dict[str, pd.DataFrame]):
        self.chains = chains
        self.frames = frames

    def contracts(self, underlying, right, expiration_gte, expiration_lte, strike_gte, strike_lte):
        key = RecordingOptionHistory.chain_key(underlying, right, expiration_gte, expiration_lte, strike_gte, strike_lte)
        if key not in self.chains:
            raise ValueError("回测快照缺少期权合约列表，无法复现")
        return self.chains[key]

    def bars(self, symbol, start, end):
        return self.frames.get(symbol, pd.DataFrame())
