"""One broker-backed portfolio snapshot shared by every page and the monitor."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord, Strategy, StrategyPosition
from .instruments import parse_option_symbol
from .risk import as_float

EQUITY_TOLERANCE_PCT = 1.0
EQUITY_TOLERANCE_USD = 50.0
EQUITY_JUMP_PCT = 10.0
_last_good_equity: dict[int, tuple[float, float]] = {}


def _flatten(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for order in orders or []:
        flat.append(order)
        for leg in order.get("legs") or []:
            flat.append({**leg, "parent_order_id": leg.get("parent_order_id") or order.get("id")})
    return flat


def account_checks(account: dict[str, Any], positions: list[dict[str, Any]]) -> list[str]:
    """Return contradictions inside one Alpaca account/positions reading."""
    problems: list[str] = []
    equity = as_float(account.get("equity"))
    if equity <= 0:
        return ["账户净值无效"]
    if account.get("cash") is not None:
        position_value = sum(as_float(p.get("market_value")) for p in positions)
        expected = as_float(account.get("cash")) + position_value
        if abs(expected - equity) > max(EQUITY_TOLERANCE_USD, equity * EQUITY_TOLERANCE_PCT / 100):
            problems.append(f"净值 {equity:,.2f} 与现金+持仓市值 {expected:,.2f} 不一致")
    return problems


def read_broker(alpaca, *, fresh: bool = False) -> dict[str, Any]:
    account_reader = getattr(alpaca, "get_account_fresh", None) if fresh else None
    position_reader = getattr(alpaca, "get_positions_fresh", None) if fresh else None
    order_reader = getattr(alpaca, "get_open_orders_fresh", None) if fresh else None
    return {
        "account": (account_reader or alpaca.get_account)(),
        "positions": (position_reader or alpaca.get_positions)(),
        "orders": order_reader() if order_reader else alpaca.get_orders("open"),
        "clock": alpaca.get_clock(),
    }


def build_portfolio(alpaca, db: Session, user_id: int) -> dict[str, Any]:
    as_of = datetime.now(timezone.utc)
    result: dict[str, Any] = {"as_of": as_of, "source": getattr(alpaca, "broker_name", "alpaca"),
                              "paper": bool(getattr(alpaca, "paper", True)), "status": "unavailable",
                              "errors": [], "checks": []}
    if not alpaca.configured:
        result["errors"] = ["券商未配置"]
        return result
    try:
        raw = read_broker(alpaca)
        problems = account_checks(raw["account"], raw["positions"])
        previous = _last_good_equity.get(user_id)
        equity = as_float(raw["account"].get("equity"))
        jumped = bool(previous and time.monotonic() - previous[1] < 1800
                      and abs(equity / previous[0] - 1) * 100 > EQUITY_JUMP_PCT)
        if problems or jumped:
            raw = read_broker(alpaca, fresh=True)
            problems = account_checks(raw["account"], raw["positions"])
            result["checks"].append("首次读取不一致，已强制重新读取券商快照")
    except Exception as exc:
        result["errors"] = [f"券商快照暂不可用：{type(exc).__name__}"]
        return result
    account, positions = raw["account"], raw["positions"]
    equity = as_float(account.get("equity"))
    last_equity = as_float(account.get("last_equity")) or None
    result["status"] = "suspect" if problems else "ok"
    result["checks"].extend(problems)
    if not problems:
        _last_good_equity[user_id] = (equity, time.monotonic())

    names = dict(db.execute(select(Strategy.id, Strategy.name).where(Strategy.owner_user_id == user_id)).all())
    owned: dict[str, list[dict[str, Any]]] = {}
    for row in db.scalars(select(StrategyPosition).where(
            StrategyPosition.user_id == user_id, StrategyPosition.qty != 0)).all():
        owned.setdefault(row.symbol.upper(), []).append(
            {"strategy_id": row.strategy_id, "strategy_name": names.get(row.strategy_id, row.strategy_id),
             "qty": abs(row.qty), "side": "short" if row.qty < 0 else "long"})
    rows = []
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        qty = as_float(position.get("qty"))
        side = "short" if qty < 0 else "long"
        owners = [item for item in owned.get(symbol, []) if item["side"] == side]
        owned_qty = min(abs(qty), sum(item["qty"] for item in owners))
        option = parse_option_symbol(symbol)
        rows.append({
            "symbol": symbol, "qty": qty, "side": str(position.get("side") or "long").split(".")[-1].lower(),
            "avg_entry_price": as_float(position.get("avg_entry_price")),
            "current_price": as_float(position.get("current_price")),
            "market_value": as_float(position.get("market_value")),
            "cost_basis": as_float(position.get("cost_basis")),
            "unrealized_pl": as_float(position.get("unrealized_pl")),
            "unrealized_plpc": as_float(position.get("unrealized_plpc")) * 100,
            "change_today_pct": as_float(position.get("change_today")) * 100,
            "owners": owners, "strategy_qty": owned_qty, "unattributed_qty": max(0.0, abs(qty) - owned_qty),
            "instrument": "option" if option else "stock",
            **({"underlying": option.root, "expiration": option.expiration.isoformat(), "option_right": option.right,
                "strike": option.strike} if option else {}),
        })
    order_owner = dict(db.execute(select(OrderRecord.id, OrderRecord.strategy_id).where(
        OrderRecord.user_id == user_id, OrderRecord.strategy_id.is_not(None))).all())
    orders = []
    for order in _flatten(raw["orders"]):
        status = str(order.get("status", "")).split(".")[-1].lower()
        if status in {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}:
            continue
        strategy_id = order_owner.get(str(order.get("id"))) or order_owner.get(str(order.get("parent_order_id") or ""))
        orders.append({key: order.get(key) for key in (
            "id", "parent_order_id", "symbol", "qty", "filled_qty", "limit_price", "stop_price", "submitted_at")} | {
            "side": str(order.get("side", "")).split(".")[-1].lower(),
            "type": str(order.get("type") or order.get("order_type") or "").split(".")[-1].lower(),
            "status": status, "strategy_name": names.get(strategy_id) if strategy_id else None})
    market_value = sum(row["market_value"] for row in rows)
    gross_value = sum(abs(row["market_value"]) for row in rows)
    result.update({
        "account": {
            "equity": equity, "last_equity": last_equity, "cash": as_float(account.get("cash")),
            "buying_power": as_float(account.get("buying_power")), "market_value": market_value,
            "day_change": equity - last_equity if last_equity else None,
            "day_change_pct": (equity / last_equity - 1) * 100 if last_equity else None,
            "exposure_pct": gross_value / equity * 100 if equity > 0 else None,
            "short_market_value": sum(row["market_value"] for row in rows if row["qty"] < 0),
            "trading_blocked": bool(account.get("trading_blocked")),
        },
        "positions": sorted(rows, key=lambda row: -abs(row["market_value"])),
        "orders": orders,
        "market_open": bool(raw["clock"].get("is_open")),
        "next_open": raw["clock"].get("next_open"), "next_close": raw["clock"].get("next_close"),
    })
    return result


def enabled_strategy_count(db: Session, user_id: int) -> int:
    return len(db.scalars(select(Strategy.id).where(
        Strategy.owner_user_id == user_id, Strategy.enabled.is_(True), Strategy.is_template.is_(False))).all())
