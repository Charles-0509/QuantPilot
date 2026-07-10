from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.schemas import PriceGuard, RuleDefinition

from .indicators import atr, ensure_frame
from .rules import evaluate_tree


@dataclass(slots=True)
class Position:
    symbol: str
    qty: float
    average_price: float
    entry_time: pd.Timestamp
    additions: int = 1
    stop_price: float | None = None
    take_price: float | None = None
    trailing_value: float | None = None
    trailing_mode: str | None = None
    high_water: float = 0
    entry_fees: float = 0


@dataclass(slots=True)
class PendingAction:
    symbol: str
    action: str
    created_at: pd.Timestamp
    limit_price: float | None = None


@dataclass
class BacktestResult:
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    benchmark_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]


def _price_guard(entry_price: float, guard: PriceGuard | None, atr_value: float | None, side: str) -> float | None:
    if guard is None:
        return None
    if guard.mode == "percent":
        distance = entry_price * guard.value / 100
    else:
        if atr_value is None or math.isnan(atr_value):
            return None
        distance = atr_value * guard.value
    return entry_price - distance if side == "stop" else entry_price + distance


def _position_size(definition: RuleDefinition, equity: float, cash: float, price: float, stop: float | None) -> float:
    config = definition.position
    if config.mode == "fixed_qty":
        qty = config.value
    elif config.mode == "fixed_notional":
        qty = config.value / price
    elif config.mode == "risk_based":
        if stop is None or price <= stop:
            return 0
        risk_dollars = equity * config.value / 100
        qty = risk_dollars / (price - stop)
    else:
        qty = equity * config.value / 100 / price
    return max(0, min(qty, cash / price))


def run_backtest(
    definition: RuleDefinition,
    bars_by_symbol: dict[str, pd.DataFrame],
    initial_cash: float = 100_000,
    slippage_bps: float = 5,
    commission: float = 0,
    benchmark: pd.DataFrame | None = None,
) -> BacktestResult:
    frames: dict[str, pd.DataFrame] = {}
    signals: dict[str, tuple[pd.Series, pd.Series, pd.Series]] = {}
    for symbol, raw in bars_by_symbol.items():
        frame = ensure_frame(raw)
        if frame.empty:
            continue
        frames[symbol] = frame
        entry = evaluate_tree(frame, definition.entry)
        exit_ = evaluate_tree(frame, definition.exit)
        atr_values = atr(frame, 14)
        signals[symbol] = (entry, exit_, atr_values)

    if not frames:
        raise ValueError("没有可用于回测的K线数据")

    timeline = sorted(set().union(*(frame.index for frame in frames.values())))
    cash = float(initial_cash)
    positions: dict[str, Position] = {}
    pending: dict[str, PendingAction] = {}
    last_entry_index: dict[str, int] = {}
    trades: list[dict[str, Any]] = []
    equity_points: list[dict[str, Any]] = []
    exposure_points: list[float] = []
    slippage = slippage_bps / 10_000

    def current_equity(timestamp: pd.Timestamp) -> float:
        value = cash
        for symbol, position in positions.items():
            frame = frames[symbol]
            eligible = frame.loc[frame.index <= timestamp]
            mark = float(eligible.iloc[-1]["close"]) if not eligible.empty else position.average_price
            value += position.qty * mark
        return value

    def close_position(symbol: str, timestamp: pd.Timestamp, price: float, reason: str) -> None:
        nonlocal cash
        position = positions.pop(symbol)
        exit_price = price * (1 - slippage)
        proceeds = position.qty * exit_price - commission
        cash += proceeds
        cost = position.qty * position.average_price + position.entry_fees
        pnl = proceeds - cost
        trades.append(
            {
                "symbol": symbol,
                "entry_time": position.entry_time.isoformat(),
                "exit_time": timestamp.isoformat(),
                "qty": round(position.qty, 6),
                "entry_price": round(position.average_price, 4),
                "exit_price": round(exit_price, 4),
                "pnl": round(pnl, 2),
                "return_pct": round((exit_price / position.average_price - 1) * 100, 3),
                "reason": reason,
                "bars_held": int(sum(1 for t in frames[symbol].index if position.entry_time <= t <= timestamp)),
            }
        )

    for timeline_index, timestamp in enumerate(timeline):
        # Orders created at the previous close execute at this bar's open. Limit entries expire after one bar.
        for symbol, action in list(pending.items()):
            frame = frames.get(symbol)
            if frame is None or timestamp not in frame.index or timestamp <= action.created_at:
                continue
            row = frame.loc[timestamp]
            if action.action == "exit" and symbol in positions:
                close_position(symbol, timestamp, float(row["open"]), "规则离场")
            elif action.action == "entry":
                if symbol in positions and not definition.position.allow_pyramiding:
                    pending.pop(symbol, None)
                    continue
                if action.limit_price is not None:
                    if float(row["low"]) > action.limit_price:
                        pending.pop(symbol, None)
                        continue
                    raw_fill = min(float(row["open"]), action.limit_price)
                else:
                    raw_fill = float(row["open"])
                fill = raw_fill * (1 + slippage)
                equity = current_equity(timestamp)
                atr_value = float(signals[symbol][2].loc[timestamp]) if timestamp in signals[symbol][2].index else None
                stop = _price_guard(fill, definition.order.stop_loss, atr_value, "stop")
                take = _price_guard(fill, definition.order.take_profit, atr_value, "take")
                qty = _position_size(definition, equity, cash, fill, stop)
                max_notional = equity * min(definition.risk.max_symbol_pct, 100) / 100
                current_notional = positions[symbol].qty * fill if symbol in positions else 0
                qty = min(qty, max(0, max_notional - current_notional) / fill)
                if qty * fill + commission <= cash and qty > 0:
                    cash -= qty * fill + commission
                    if symbol in positions:
                        old = positions[symbol]
                        total_qty = old.qty + qty
                        old.average_price = (old.average_price * old.qty + fill * qty) / total_qty
                        old.qty = total_qty
                        old.additions += 1
                        old.stop_price = stop
                        old.take_price = take
                        old.entry_fees += commission
                    else:
                        trailing = definition.order.trailing_stop
                        positions[symbol] = Position(
                            symbol=symbol,
                            qty=qty,
                            average_price=fill,
                            entry_time=timestamp,
                            stop_price=stop,
                            take_price=take,
                            trailing_value=trailing.value if trailing else None,
                            trailing_mode=trailing.mode if trailing else None,
                            high_water=fill,
                            entry_fees=commission,
                        )
                    last_entry_index[symbol] = timeline_index
            pending.pop(symbol, None)

        # Intrabar risk exits use conservative ordering: stop/trailing before take profit.
        for symbol, position in list(positions.items()):
            frame = frames[symbol]
            if timestamp not in frame.index:
                continue
            row = frame.loc[timestamp]
            position.high_water = max(position.high_water, float(row["high"]))
            trailing_price = None
            if position.trailing_value is not None:
                if position.trailing_mode == "percent":
                    trailing_price = position.high_water * (1 - position.trailing_value / 100)
                else:
                    trailing_price = position.high_water - position.trailing_value
            effective_stop = max(
                value for value in [position.stop_price, trailing_price] if value is not None
            ) if any(value is not None for value in [position.stop_price, trailing_price]) else None
            if effective_stop is not None and float(row["low"]) <= effective_stop:
                close_position(symbol, timestamp, min(float(row["open"]), effective_stop), "止损/移动止损")
                pending.pop(symbol, None)
                continue
            if position.take_price is not None and float(row["high"]) >= position.take_price:
                close_position(symbol, timestamp, max(float(row["open"]), position.take_price), "止盈")
                pending.pop(symbol, None)

        # Signals are generated only after the completed bar and execute on the next bar.
        for symbol, frame in frames.items():
            if timestamp not in frame.index or symbol in pending:
                continue
            entry_signal, exit_signal, _ = signals[symbol]
            if symbol in positions and bool(exit_signal.loc[timestamp]):
                pending[symbol] = PendingAction(symbol, "exit", timestamp)
                continue
            can_add = symbol not in positions or (
                definition.position.allow_pyramiding
                and positions[symbol].additions < definition.position.max_additions
            )
            cooldown_ok = timeline_index - last_entry_index.get(symbol, -10_000) >= definition.risk.cooldown_bars
            if can_add and cooldown_ok and bool(entry_signal.loc[timestamp]):
                limit = None
                if definition.order.type == "limit":
                    limit = float(frame.loc[timestamp, "close"]) * (
                        1 - definition.order.limit_offset_bps / 10_000
                    )
                pending[symbol] = PendingAction(symbol, "entry", timestamp, limit)

        equity = current_equity(timestamp)
        invested = equity - cash
        equity_points.append({"timestamp": timestamp.isoformat(), "equity": round(equity, 2)})
        exposure_points.append(max(0, invested / equity) if equity else 0)

    # Mark remaining holdings to the final close so metrics include their value; record an explicit final exit.
    final_timestamp = timeline[-1]
    for symbol in list(positions):
        frame = frames[symbol]
        eligible = frame.loc[frame.index <= final_timestamp]
        if not eligible.empty:
            close_position(symbol, final_timestamp, float(eligible.iloc[-1]["close"]), "回测结束")
    if equity_points:
        equity_points[-1]["equity"] = round(cash, 2)

    metrics = calculate_metrics(equity_points, trades, initial_cash, exposure_points, definition.timeframe)
    benchmark_curve = build_benchmark_curve(benchmark, initial_cash) if benchmark is not None else []
    return BacktestResult(metrics, equity_points, benchmark_curve, trades)


def periods_per_year(timeframe: str) -> int:
    return {
        "5Min": 252 * 78,
        "15Min": 252 * 26,
        "30Min": 252 * 13,
        "1Hour": 252 * 7,
        "1Day": 252,
    }[timeframe]


def calculate_metrics(
    equity_curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    initial_cash: float,
    exposure: list[float],
    timeframe: str,
) -> dict[str, Any]:
    if len(equity_curve) < 2:
        return {}
    equity = pd.Series([point["equity"] for point in equity_curve], dtype=float)
    returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    annual_periods = periods_per_year(timeframe)
    total_return = equity.iloc[-1] / initial_cash - 1
    years = max(len(returns) / annual_periods, 1 / annual_periods)
    cagr = (equity.iloc[-1] / initial_cash) ** (1 / years) - 1 if equity.iloc[-1] > 0 else -1
    volatility = returns.std(ddof=0) * math.sqrt(annual_periods) if len(returns) else 0
    sharpe = returns.mean() / returns.std(ddof=0) * math.sqrt(annual_periods) if returns.std(ddof=0) else 0
    downside = returns[returns < 0].std(ddof=0)
    sortino = returns.mean() / downside * math.sqrt(annual_periods) if downside else 0
    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    pnls = [float(trade["pnl"]) for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses else (999 if wins else 0)
    avg_win = float(np.mean(wins)) if wins else 0
    avg_loss = abs(float(np.mean(losses))) if losses else 0
    payoff = avg_win / avg_loss if avg_loss else (999 if avg_win else 0)
    return {
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "total_return_pct": round(total_return * 100, 3),
        "cagr_pct": round(cagr * 100, 3),
        "annual_volatility_pct": round(volatility * 100, 3),
        "sharpe": round(float(sharpe), 3),
        "sortino": round(float(sortino), 3),
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 3),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 2) if pnls else 0,
        "payoff_ratio": round(payoff, 3),
        "profit_factor": round(profit_factor, 3),
        "trade_count": len(trades),
        "average_bars_held": round(float(np.mean([t["bars_held"] for t in trades])), 2) if trades else 0,
        "exposure_pct": round(float(np.mean(exposure)) * 100, 2) if exposure else 0,
    }


def build_benchmark_curve(frame: pd.DataFrame, initial_cash: float) -> list[dict[str, Any]]:
    frame = ensure_frame(frame)
    if frame.empty:
        return []
    first = float(frame.iloc[0]["close"])
    return [
        {"timestamp": index.isoformat(), "equity": round(initial_cash * float(row["close"]) / first, 2)}
        for index, row in frame.iterrows()
    ]
