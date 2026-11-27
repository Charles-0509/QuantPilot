#!/usr/bin/env python3
"""Bounded Alpaca Paper validation of one short-stock and one long-option lifecycle.

Run inside the application container. Creates two clearly named, high-trigger
test strategies (1 share short with a bracket; 1 option contract), each with a
paper_validation deadline, one entry, quantity 1 and a $1,000 cap, then lets the
running engine trade them. Existing strategies, risk settings and positions are
not touched. start/finish need --execute; status is read-only.
"""

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import OrderRecord, Signal, StrategyPosition  # noqa: E402
from app.schemas import OptionConfig, RuleDefinition  # noqa: E402
from app.services.instruments import choose_contract  # noqa: E402

_spec = importlib.util.spec_from_file_location("paper_validation", ROOT / "scripts" / "paper-validation.py")
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
api_session, broker, save, utc = _base.api_session, _base.broker, _base.save, _base.utc

SHORT_CANDIDATES = ["TSLA", "NVDA", "AAPL", "QQQ", "SPY", "AMD", "INTC", "BAC", "KO", "PFE", "F", "T", "XLF", "IWM", "DIS"]
OPTION_UNDERLYINGS = ["SPY", "QQQ"]
MAX_NOTIONAL = 1000.0
TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}


def group(*children, op="AND"):
    return {"type": "group", "op": op, "negate": False, "children": list(children)}


def cond(left, operator, right, label):
    return {"type": "condition", "left": left, "operator": operator, "right": right, "label": label}


ALWAYS_ENTER = group(cond({"kind": "price", "field": "close"}, ">", {"kind": "number", "value": 0}, "有效价格即入场"))
NEXT_BAR_EXIT = group(cond({"kind": "indicator", "indicator": "RSI", "params": {"period": 2}, "field": "value"},
                           ">=", {"kind": "number", "value": 0}, "下一根有效K线即离场"))


def base_definition(name, symbol, run_id, expires_at):
    return {
        "version": 1, "name": name, "description": f"限额 Paper 生命周期验证 {run_id}，不用于观察收益。",
        "symbols": [symbol], "timeframe": "5Min", "warmup_bars": 30,
        "schedule": {"session": "regular", "weekdays": [0, 1, 2, 3, 4]},
        "entry": ALWAYS_ENTER, "exit": NEXT_BAR_EXIT,
        "position": {"mode": "fixed_qty", "value": 1, "allow_pyramiding": False, "max_additions": 1},
        # Strategy max_positions counts every account position (not only this strategy's), so a
        # value of 1 rejects the entry whenever the account holds anything; the global limit governs.
        "risk": {"max_symbol_pct": 10, "max_positions": 30, "cooldown_bars": 100},
        "paper_validation": {"expires_at": expires_at.isoformat(), "max_entries": 1, "max_quantity": 1,
                             "max_notional": MAX_NOTIONAL},
    }


def preflight(alpaca, user_id, require_open=True):
    clock = alpaca.get_clock()
    now = datetime.now(timezone.utc)
    if require_open and (not clock.get("is_open") or utc(clock["next_close"]) - now < timedelta(minutes=35)):
        raise RuntimeError("需要常规开盘且距收盘至少35分钟")
    account = alpaca.get_account()
    if not account.get("shorting_enabled"):
        raise RuntimeError("模拟账户未开通做空")
    if int(account.get("options_trading_level") or 0) < 2:
        raise RuntimeError("模拟账户期权权限低于 2 级，不能买入期权")
    positions = alpaca.get_positions_fresh()
    held = {str(p.get("symbol", "")).upper() for p in positions}
    open_symbols = {str(o.get("symbol", "")).upper() for o in alpaca.get_open_orders_fresh()}
    with SessionLocal() as db:
        from app.models import RiskSettings, Strategy
        enabled = db.scalars(select(Strategy).where(Strategy.owner_user_id == user_id, Strategy.enabled.is_(True),
                                                    Strategy.is_template.is_(False))).all()
        risk = db.scalar(select(RiskSettings).where(RiskSettings.user_id == user_id))
    used = {symbol for strategy in enabled for symbol in (strategy.definition or {}).get("symbols", [])}
    if len(held) + 2 > (risk.max_positions or 8):
        raise RuntimeError(f"账户已有 {len(held)} 个持仓，加 2 个测试持仓会超过全局上限 {risk.max_positions}")
    quotes = alpaca.get_latest_quotes(SHORT_CANDIDATES)
    short_symbol = None
    skipped = {}
    for symbol in SHORT_CANDIDATES:
        asset = alpaca.get_asset(symbol)
        bid = float((quotes.get(symbol) or {}).get("bid_price") or 0)
        reasons = [label for label, bad in (("已持有", symbol in held), ("有挂单", symbol in open_symbols),
                                            ("其他策略使用", symbol in used), ("不可做空", not asset.get("shortable")),
                                            ("非易借券", not asset.get("easy_to_borrow")),
                                            ("价格不符", not 0 < bid <= MAX_NOTIONAL * 0.95)) if bad]
        if not reasons:
            short_symbol = symbol
            break
        skipped[symbol] = reasons
    if short_symbol is None:
        raise RuntimeError(f"没有合适的做空候选：{json.dumps(skipped, ensure_ascii=False)}")
    today = datetime.now(timezone.utc).date()
    option_choice = None
    for underlying in OPTION_UNDERLYINGS:
        # Latest trade, like the engine's bar close; an off-hours IEX ask can be far from the market.
        snapshot = (alpaca.get_snapshots([underlying]) or {}).get(underlying) or {}
        spot = float((snapshot.get("latest_trade") or {}).get("price") or 0)
        if spot <= 0:
            continue
        for offset in (4.0, 6.0, 8.0, 10.0):
            config = OptionConfig(right="call", min_dte=7, max_dte=21, strike_mode="otm_pct", strike_offset_pct=offset,
                                  min_open_interest=100, max_spread_pct=25, stop_loss_pct=50, take_profit_pct=100,
                                  close_dte=1)
            contracts = alpaca.get_option_contracts(
                underlying, right="call", expiration_gte=today + timedelta(days=7), expiration_lte=today + timedelta(days=21),
                strike_gte=spot * (1 + offset / 100 - 0.03), strike_lte=spot * (1 + offset / 100 + 0.03))
            pick = choose_contract(contracts, spot, config, today)
            if pick is None:
                continue
            quote = alpaca.get_option_quotes([pick["symbol"]]).get(pick["symbol"]) or {}
            bid, ask = float(quote.get("bid_price") or 0), float(quote.get("ask_price") or 0)
            if 0 < bid < ask and ask * 100 <= MAX_NOTIONAL * 0.9 and (ask - bid) / ((ask + bid) / 2) * 100 <= 25:
                option_choice = {"underlying": underlying, "offset": offset, "config": config.model_dump(),
                                 "preview_contract": pick["symbol"], "preview_bid": bid, "preview_ask": ask}
                break
        if option_choice:
            break
    if option_choice is None:
        raise RuntimeError("没有权利金≤$900、价差≤25% 的看涨期权候选")
    return clock, account, short_symbol, option_choice, sorted(used)


def start(args, path):
    if path.exists():
        raise RuntimeError("此运行已存在；请先查看 status，禁止重复启动")
    alpaca = broker(args.user_id)
    if getattr(alpaca, "broker_name", "alpaca") != "alpaca" or not getattr(alpaca, "paper", True):
        raise RuntimeError("此验证只获授权使用 Alpaca Paper")
    clock, account, short_symbol, option_choice, used = preflight(alpaca, args.user_id)
    now = datetime.now(timezone.utc)
    expires = min(now + timedelta(minutes=args.minutes), utc(clock["next_close"]) - timedelta(minutes=15))
    short_definition = {**base_definition(f"[验证] 做空生命周期 {short_symbol}", short_symbol, args.run_id, expires),
                        "direction": "short",
                        "order": {"type": "market", "limit_offset_bps": 0, "time_in_force": "day",
                                  "stop_loss": {"mode": "percent", "value": 3, "atr_period": 14},
                                  "take_profit": {"mode": "percent", "value": 3, "atr_period": 14}, "trailing_stop": None}}
    option_definition = {**base_definition(f"[验证] 期权生命周期 {option_choice['underlying']} 看涨",
                                           option_choice["underlying"], args.run_id, expires),
                         "instrument": "option", "option": option_choice["config"],
                         "order": {"type": "limit", "limit_offset_bps": 0, "time_in_force": "day",
                                   "stop_loss": None, "take_profit": None, "trailing_stop": None}}
    for definition in (short_definition, option_definition):
        RuleDefinition.model_validate(definition)
    with api_session(args.user_id) as api:
        state = api("GET", "/engine")
        if state["status"] != "running" or state.get("active_incidents"):
            raise RuntimeError("引擎必须正在运行且无活动隔离（脚本不会替你恢复引擎）")
        run = {"run_id": args.run_id, "user_id": args.user_id, "kind": "short_option_lifecycle", "phase": "preparing",
               "started_at": now.isoformat(), "expires_at": expires.isoformat(), "broker": "alpaca",
               "broker_account_id": str(account.get("id") or ""), "short_symbol": short_symbol,
               "option": option_choice, "other_strategy_symbols": used, "strategies": {},
               "definitions": {"short": short_definition, "option": option_definition}}
        save(path, run)
        for key, definition in (("short", short_definition), ("option", option_definition)):
            created = api("POST", "/strategies", {"definition": definition})
            run["strategies"][key] = created["id"]
            save(path, run)
            api("POST", f"/strategies/{created['id']}/enable")
        run["phase"] = "running"
        save(path, run)
    return run


def status(args, path):
    run = json.loads(path.read_text())
    ids = list(run["strategies"].values())
    with SessionLocal() as db:
        signals = db.scalars(select(Signal).where(Signal.user_id == run["user_id"], Signal.strategy_id.in_(ids))).all()
        orders = db.scalars(select(OrderRecord).where(OrderRecord.user_id == run["user_id"], OrderRecord.strategy_id.in_(ids))).all()
        owned = db.scalars(select(StrategyPosition).where(StrategyPosition.user_id == run["user_id"],
                                                          StrategyPosition.strategy_id.in_(ids))).all()
    results = {}
    for key, sid in run["strategies"].items():
        rows = []
        for order in sorted((o for o in orders if o.strategy_id == sid), key=lambda o: o.submitted_at):
            raw = (order.raw or {}).get("order") or order.raw or {}
            submitted, filled = utc(raw.get("submitted_at")), utc(raw.get("filled_at"))
            rows.append({"symbol": order.symbol, "side": order.side, "type": order.order_type, "qty": order.qty,
                         "filled_qty": order.filled_qty, "avg_price": order.filled_avg_price,
                         "status": str(order.status).lower().split(".")[-1], "client_prefix": order.client_order_id[:5],
                         "submit_to_fill_seconds": round((filled - submitted).total_seconds(), 3) if filled and submitted else None})
        results[key] = {
            "signals": [{"action": s.action, "symbol": s.symbol, "status": s.status, "reason": s.reason,
                         "error": (s.payload or {}).get("error")} for s in signals if s.strategy_id == sid],
            "orders": rows,
            "ledger": {p.symbol: p.qty for p in owned if p.strategy_id == sid and abs(p.qty) > 1e-9},
        }
    report = {"run_id": run["run_id"], "observed_at": datetime.now(timezone.utc).isoformat(), "phase": run["phase"],
              "expires_at": run["expires_at"], "results": results}
    save(path.with_name(path.stem + "-status.json"), report)
    return report


def finish(args, path):
    run = json.loads(path.read_text())
    current = broker(run["user_id"])
    if run.get("broker_account_id") and str(current.get_account().get("id") or "") != run["broker_account_id"]:
        raise RuntimeError("账户已切换，拒绝处理其他账户的验证")
    report = status(args, path)
    for key, result in report["results"].items():
        if result["ledger"] or any(s["status"] in ("pending_submission", "pending_reconciliation") for s in result["signals"]) \
                or any(o["status"] not in TERMINAL for o in result["orders"]):
            raise RuntimeError(f"{key} 测试仓位或订单尚未结清，不能 finish")
    with api_session(run["user_id"]) as api:
        for sid in run["strategies"].values():
            current_strategy = api("GET", f"/strategies/{sid}")
            if current_strategy["enabled"]:
                api("POST", f"/strategies/{sid}/disable")
    run["phase"] = "finished"
    run["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(path, run)
    return status(args, path)


def check(args, path):
    """Read-only preflight: what start would use, without creating anything."""
    alpaca = broker(args.user_id)
    clock, account, short_symbol, option_choice, used = preflight(alpaca, args.user_id, require_open=False)
    return {"market_open": clock.get("is_open"), "next_open": clock.get("next_open"),"short_symbol": short_symbol, "option": option_choice, "other_strategy_symbols": used,
            "shorting_enabled": account.get("shorting_enabled"), "options_level": account.get("options_trading_level")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "start", "status", "finish"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--minutes", type=int, default=45)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.run_id.replace("-", "").isalnum() or not 15 <= args.minutes <= 90:
        raise SystemExit("run-id 或时长无效")
    if args.action in ("start", "finish") and not args.execute:
        raise SystemExit("改变状态需要 --execute")
    path = Path(get_settings().investor_db_path).parent / "validation" / f"{args.run_id}.json"
    result = {"check": check, "start": start, "status": status, "finish": finish}[args.action](args, path)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
