"""Per-strategy live (Paper) performance reconstructed from broker fills.

Each strategy's opening fills create FIFO lots and its own closing fills match
them, so P&L is never mixed with manual holdings, cash sweep or another strategy.
Long lots open with buys; short lots (short/cover signals) open with sells.
Option P&L uses the 100-share contract multiplier.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord, Signal, Strategy, StrategyPosition
from .instruments import multiplier
from .risk import as_float

EXIT_LABELS = {
    "stop_loss": "止损", "trailing_stop": "移动止损", "take_profit": "止盈",
    "strategy_exit": "策略离场信号", "other": "其他卖出",
}


def _order_payload(record: OrderRecord) -> dict[str, Any]:
    raw = record.raw or {}
    return raw.get("order") if isinstance(raw.get("order"), dict) else raw


def _fill_time(record: OrderRecord) -> datetime:
    value = _order_payload(record).get("filled_at") or _order_payload(record).get("updated_at")
    parsed = None
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    parsed = parsed or record.submitted_at
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def exit_kind(record: OrderRecord, signal: Signal | None) -> str:
    order_type = str(record.order_type or _order_payload(record).get("type") or "").lower().split(".")[-1]
    if "trailing" in order_type:
        return "trailing_stop"
    if "stop" in order_type:
        return "stop_loss"
    if order_type == "limit" and (_order_payload(record).get("parent_order_id") or record.client_order_id.startswith("leg-")
                                  or (signal is not None and signal.action in ("buy", "short"))):
        return "take_profit"
    if signal is not None and signal.action in ("sell", "cover"):
        return "strategy_exit"
    return "other"


def _max_drawdown(values: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst


def strategy_performance(db: Session, user_id: int, prices: dict[str, float] | None = None) -> list[dict[str, Any]]:
    prices = prices or {}
    strategies = {row.id: row for row in db.scalars(select(Strategy).where(
        Strategy.owner_user_id == user_id, Strategy.is_template.is_(False))).all()}
    owned = defaultdict(float)
    for row in db.scalars(select(StrategyPosition).where(StrategyPosition.user_id == user_id)).all():
        owned[(row.strategy_id, row.symbol.upper())] += abs(row.qty)
    records = db.scalars(select(OrderRecord).where(
        OrderRecord.user_id == user_id, OrderRecord.strategy_id.is_not(None), OrderRecord.filled_qty > 0)).all()
    signals = {row.id: row for row in db.scalars(select(Signal).where(Signal.user_id == user_id)).all()}

    lots: dict[tuple[str, str], deque] = defaultdict(deque)
    trades: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmatched_sells: dict[str, float] = defaultdict(float)
    for record in sorted(records, key=_fill_time):
        key = (record.strategy_id, record.symbol.upper())
        price = as_float(record.filled_avg_price)
        if price <= 0:
            continue
        signal = signals.get(record.signal_id) if record.signal_id else None
        short = signal is not None and signal.action in ("short", "cover")
        opening_side = "sell" if short else "buy"
        if record.side == opening_side:
            lots[key].append({"qty": record.filled_qty, "price": price, "time": _fill_time(record),
                              "reason": signal.reason if signal else "", "order_id": record.id,
                              "short": short})
            continue
        remaining = record.filled_qty
        kind = exit_kind(record, signal)
        sign = -1 if short else 1
        size = multiplier(record.symbol)
        while remaining > 1e-9 and lots[key]:
            lot = lots[key][0]
            qty = min(lot["qty"], remaining)
            pnl = (price - lot["price"]) * qty * sign * size
            trades[record.strategy_id].append({
                "symbol": record.symbol.upper(), "qty": qty, "side": "short" if short else "long",
                "entry_time": lot["time"], "entry_price": lot["price"], "entry_reason": lot["reason"],
                "exit_time": _fill_time(record), "exit_price": price, "exit_kind": kind,
                "exit_label": EXIT_LABELS[kind],
                "exit_reason": signal.reason if signal and signal.action in ("sell", "cover") else "",
                "pnl": pnl, "pnl_pct": (price / lot["price"] - 1) * 100 * sign,
                "holding_minutes": (_fill_time(record) - lot["time"]).total_seconds() / 60,
                "exit_order_id": record.id,
            })
            lot["qty"] -= qty
            remaining -= qty
            if lot["qty"] <= 1e-9:
                lots[key].popleft()
        if remaining > 1e-9:
            unmatched_sells[record.strategy_id] += remaining

    open_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cleared_by_strategy: dict[str, float] = defaultdict(float)
    for (strategy_id, symbol), queue in lots.items():
        held = owned.get((strategy_id, symbol), 0.0)
        for lot in queue:
            # Quarantine can clear ownership without a sell; such lots have no known exit.
            qty = min(lot["qty"], held)
            held -= qty
            if lot["qty"] - qty > 1e-9:
                cleared_by_strategy[strategy_id] += lot["qty"] - qty
            if qty > 1e-9:
                mark = prices.get(symbol)
                sign = -1 if lot.get("short") else 1
                open_by_strategy[strategy_id].append({
                    "symbol": symbol, "qty": qty, "side": "short" if lot.get("short") else "long",
                    "entry_price": lot["price"], "entry_time": lot["time"], "current_price": mark,
                    "unrealized_pnl": (mark - lot["price"]) * qty * sign * multiplier(symbol) if mark else None})

    result = []
    for strategy_id in set(strategies) | set(trades) | set(open_by_strategy):
        strategy = strategies.get(strategy_id)
        rows = sorted(trades.get(strategy_id, []), key=lambda item: item["exit_time"])
        open_rows = open_by_strategy.get(strategy_id, [])
        if not rows and not open_rows and not (strategy and strategy.enabled):
            continue
        wins = [row["pnl"] for row in rows if row["pnl"] > 0]
        losses = [row["pnl"] for row in rows if row["pnl"] <= 0]
        cumulative, curve = 0.0, []
        for row in rows:
            cumulative += row["pnl"]
            curve.append({"time": row["exit_time"], "pnl": cumulative})
        by_exit: dict[str, dict[str, float]] = {}
        for row in rows:
            bucket = by_exit.setdefault(row["exit_label"], {"count": 0, "pnl": 0.0})
            bucket["count"] += 1
            bucket["pnl"] += row["pnl"]
        unrealized = sum(row["unrealized_pnl"] or 0 for row in open_rows)
        realized = sum(row["pnl"] for row in rows)
        result.append({
            "strategy_id": strategy_id, "name": strategy.name if strategy else strategy_id,
            "enabled": bool(strategy and strategy.enabled),
            "symbols": (strategy.definition or {}).get("symbols", []) if strategy else [],
            "realized_pnl": realized, "unrealized_pnl": unrealized, "total_pnl": realized + unrealized,
            "trades": len(rows), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(rows) * 100 if rows else None,
            "avg_win": sum(wins) / len(wins) if wins else None,
            "avg_loss": sum(losses) / len(losses) if losses else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses and sum(losses) < 0 else None,
            "max_drawdown": _max_drawdown([point["pnl"] for point in curve]),
            "avg_holding_minutes": sum(row["holding_minutes"] for row in rows) / len(rows) if rows else None,
            "exit_breakdown": by_exit, "curve": curve, "trade_list": list(reversed(rows)),
            "open_lots": open_rows, "ownership_cleared_qty": cleared_by_strategy.get(strategy_id, 0.0),
            "unmatched_sell_qty": unmatched_sells.get(strategy_id, 0.0),
            "diagnosis": diagnose(rows, by_exit, open_rows),
            "first_trade_at": rows[0]["entry_time"] if rows else None,
        })
    return sorted(result, key=lambda item: item["total_pnl"], reverse=True)


def diagnose(rows: list[dict[str, Any]], by_exit: dict[str, dict[str, float]], open_rows: list[dict[str, Any]]) -> list[str]:
    """Plain-language observations derived only from the strategy's own fills."""
    if not rows:
        return ["尚无已平仓交易，暂时无法判断盈亏来源。"] + (["当前有未平仓持仓，收益仍未实现。"] if open_rows else [])
    notes = []
    total = sum(row["pnl"] for row in rows)
    if len(rows) < 10:
        notes.append(f"只有 {len(rows)} 笔已平仓交易，样本太少，结果偶然性很大。")
    worst = min(by_exit.items(), key=lambda item: item[1]["pnl"])
    if worst[1]["pnl"] < 0:
        notes.append(f"亏损主要来自“{worst[0]}”：{int(worst[1]['count'])} 笔，合计 {worst[1]['pnl']:,.2f} 美元。")
    if "止盈" not in by_exit and any(label in by_exit for label in ("止损", "移动止损")):
        notes.append("止盈从未成交而止损已触发，止盈目标可能设得太远，或止损太近。")
    wins = [row["pnl"] for row in rows if row["pnl"] > 0]
    losses = [row["pnl"] for row in rows if row["pnl"] <= 0]
    if wins and losses and abs(sum(losses) / len(losses)) > sum(wins) / len(wins) * 1.5:
        notes.append("平均亏损明显大于平均盈利，即使胜率尚可也难以赚钱。")
    quick = [row for row in rows if row["holding_minutes"] < 60]
    if len(quick) >= max(3, len(rows) // 2) and sum(row["pnl"] for row in quick) < 0:
        notes.append("一半以上交易持仓不到 1 小时且合计亏损，可能频繁被短期波动扫出。")
    notes.append(f"已平仓合计 {'盈利' if total > 0 else '亏损'} {abs(total):,.2f} 美元（未计手续费，模拟盘成交）。")
    return notes
