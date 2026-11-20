#!/usr/bin/env python3
"""Bounded validation through the running app's authenticated API.

Run inside the application container. No keys/tokens are printed or saved.
start/finish require --execute. status is read-only except a local report file.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import requests  # noqa: E402
from sqlalchemy import select  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import AuthUser, OAuthAccessToken, OrderRecord, Signal, StrategyPosition  # noqa: E402
from app.services.auth import issue_session  # noqa: E402
from app.services.runtime import UserRuntimeManager  # noqa: E402
from app.services.rules import evaluate_tree  # noqa: E402
from app.schemas import RuleDefinition  # noqa: E402
from app.validation_rules import SYMBOLS, validation_definitions  # noqa: E402


def utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


@contextmanager
def api_session(user_id):
    with SessionLocal() as db:
        user = db.get(AuthUser, user_id)
        if user is None or not user.is_active:
            raise RuntimeError("用户不存在或未启用")
        issued = issue_session(db, user, get_settings())
        token_id = issued.record.id
        db.commit()
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = f"Bearer {issued.access_token}"

    def call(method, path, payload=None):
        response = session.request(
            method, "http://127.0.0.1:10000/api" + path, json=payload, timeout=(5, 75)
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {path}: HTTP {response.status_code}: {response.json().get('detail', '请求失败')}"
            )
        return response.json()

    try:
        yield call
    finally:
        session.close()
        with SessionLocal() as db:
            token = db.get(OAuthAccessToken, token_id)
            if token:
                db.delete(token)
                db.commit()


def broker(user_id):
    return UserRuntimeManager(get_settings())._build(user_id).alpaca


def start(args, path):
    if path.exists():
        raise RuntimeError("此运行已存在；请先查看status，禁止重复启动")
    alpaca = broker(args.user_id)
    if getattr(alpaca, 'broker_name', 'alpaca') != 'alpaca' or not getattr(alpaca, 'paper', True):
        raise RuntimeError('此验证只获授权使用Alpaca Paper，当前券商/账户环境不匹配')
    clock = alpaca.get_clock()
    now = datetime.now(timezone.utc)
    if not clock.get("is_open") or utc(clock["next_close"]) - now < timedelta(minutes=35):
        raise RuntimeError("需要常规开盘且至少还剩35分钟")
    if alpaca.get_open_orders_fresh():
        raise RuntimeError("账户有开放订单，未启动验证")
    baseline = alpaca.get_positions_fresh()
    with SessionLocal() as db:
        if db.scalar(
            select(StrategyPosition.id).where(
                StrategyPosition.user_id == args.user_id, StrategyPosition.qty > 0
            )
        ):
            raise RuntimeError("原策略仍有归属仓位，未启动验证")
        if db.scalar(
            select(Signal.id).where(
                Signal.user_id == args.user_id,
                Signal.status.in_(["pending_submission", "pending_reconciliation"]),
            )
        ):
            raise RuntimeError("原策略仍有待对账意图，未启动验证")
    quotes = alpaca.get_latest_quotes(SYMBOLS)
    for symbol in SYMBOLS:
        asset = alpaca.get_asset(symbol)
        if not asset.get("tradable") or str(asset.get("status")).lower().split(".")[-1] != "active":
            raise RuntimeError(f"{symbol}不可交易")
        quote = quotes[symbol]
        if not 0 < float(quote.get("ask_price") or 0) <= 950:
            raise RuntimeError(f"{symbol}报价不满足每单1000美元上限")
    budget = sum(float(quotes[s]["ask_price"]) for s in SYMBOLS)
    account = alpaca.get_account()
    if budget > 6000 or float(account.get("cash") or 0) < budget * 1.05:
        raise RuntimeError("测试总预算超过6000美元或账户现金不足")
    expires = min(
        now + timedelta(minutes=args.minutes), utc(clock["next_close"]) - timedelta(minutes=15)
    )
    definitions = validation_definitions(args.run_id, expires)
    frames = alpaca.recent_bars(SYMBOLS, "5Min", 40)
    preflight = {}
    for definition in definitions:
        rule = RuleDefinition.model_validate(definition)
        frame = frames.get(rule.symbols[0])
        if frame is None or len(frame) < 30:
            raise RuntimeError(f"{rule.symbols[0]}历史K线不足30根")
        preflight[rule.symbols[0]] = {
            "bars": len(frame),
            "entry_matches_last_10": int(evaluate_tree(frame, rule.entry).tail(10).sum()),
            "exit_matches_last_10": int(evaluate_tree(frame, rule.exit).tail(10).sum()),
        }
    with api_session(args.user_id) as api:
        state = api("GET", "/engine")
        if state["status"] != "paused" or state.get("active_incidents"):
            raise RuntimeError("引擎必须已暂停且无活动隔离")
        risk = api("GET", "/risk-settings")
        originals = [
            s["id"] for s in api("GET", "/strategies") if s["enabled"] and not s["is_template"]
        ]
        run = {
            "run_id": args.run_id,
            "user_id": args.user_id,
            "phase": "preparing",
            "started_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "baseline_positions": {p["symbol"]: p["qty"] for p in baseline},
            "original_enabled": originals,
            "original_cash_sweep_enabled": risk["cash_sweep_enabled"],
            "estimated_peak_notional": budget,
            "broker": "alpaca",
            "broker_account_id": str(account.get("id") or ""),
            "strategies": {},
            "definitions": definitions,
            "preflight": preflight,
        }
        save(path, run)
        for strategy_id in originals:
            api("POST", f"/strategies/{strategy_id}/disable")
        api("PUT", "/risk-settings", {**risk, "cash_sweep_enabled": False})
        for definition in definitions:
            created = api("POST", "/strategies", {"definition": definition})
            run["strategies"][definition["symbols"][0]] = created["id"]
            save(path, run)
            api("POST", f"/strategies/{created['id']}/enable")
        api("POST", "/engine/resume", {"reason": f"用户授权：限额Paper验证 {args.run_id}"})
        run["phase"] = "running"
        save(path, run)
    return run


def status(args, path):
    run = json.loads(path.read_text())
    strategy_ids = list(run["strategies"].values())
    rows = []
    with SessionLocal() as db:
        signals = db.scalars(
            select(Signal).where(
                Signal.user_id == run["user_id"], Signal.strategy_id.in_(strategy_ids)
            )
        ).all()
        orders = db.scalars(
            select(OrderRecord).where(
                OrderRecord.user_id == run["user_id"], OrderRecord.strategy_id.in_(strategy_ids)
            )
        ).all()
        owned = db.scalars(
            select(StrategyPosition).where(
                StrategyPosition.user_id == run["user_id"],
                StrategyPosition.strategy_id.in_(strategy_ids),
            )
        ).all()
        for symbol, sid in run["strategies"].items():
            parent = [o for o in orders if o.strategy_id == sid]
            by_id = {s.id: s for s in signals}
            trades = []
            for o in parent:
                raw = o.raw or {}
                raw = raw.get("order") or raw
                signal = by_id.get(o.signal_id)
                submitted = utc(raw.get("submitted_at"))
                filled = utc(raw.get("filled_at"))
                trades.append(
                    {
                        "id": o.id,
                        "side": o.side,
                        "type": o.order_type,
                        "qty": o.qty,
                        "filled_qty": o.filled_qty,
                        "status": str(o.status).lower().split(".")[-1],
                        "signal_to_submit_seconds": round(
                            (submitted - utc(signal.created_at)).total_seconds(), 3
                        )
                        if submitted and signal and signal.action == o.side
                        else None,
                        "submit_to_fill_seconds": round((filled - submitted).total_seconds(), 3)
                        if filled and submitted
                        else None,
                        "submitted_at": submitted.isoformat() if submitted else None,
                        "filled_at": filled.isoformat() if filled else None,
                    }
                )
            rows.append(
                {
                    "symbol": symbol,
                    "signals": [
                        {
                            "id": s.id,
                            "action": s.action,
                            "status": s.status,
                            "error": (s.payload or {}).get("error"),
                            "bar_close_to_signal_seconds": round(
                                (
                                    utc(s.created_at) - utc(s.bar_timestamp) - timedelta(minutes=5)
                                ).total_seconds(),
                                3,
                            ),
                        }
                        for s in signals
                        if s.strategy_id == sid
                    ],
                    "orders": trades,
                    "owned_qty": sum(p.qty for p in owned if p.strategy_id == sid),
                }
            )
    report = {
        "run_id": run["run_id"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "phase": run["phase"],
        "expires_at": run["expires_at"],
        "results": rows,
    }
    save(path.with_name(path.stem + "-status.json"), report)
    return report


def finish(args, path):
    run = json.loads(path.read_text())
    current = broker(run['user_id'])
    if getattr(current, 'broker_name', 'alpaca') != 'alpaca' or not getattr(current, 'paper', True):
        raise RuntimeError('账户已切换，拒绝跨券商恢复旧验证设置')
    if run.get('broker_account_id') and str(current.get_account().get('id') or '') != run['broker_account_id']:
        raise RuntimeError('账户已切换，拒绝恢复其他账户的验证设置')
    report = status(args, path)
    if any(
        r["owned_qty"] > 1e-8
        or any(
            s["status"] in ["pending_submission", "pending_reconciliation"] for s in r["signals"]
        )
        or any(
            o["status"]
            not in ["filled", "canceled", "cancelled", "expired", "rejected", "replaced"]
            for o in r["orders"]
        )
        for r in report["results"]
    ):
        raise RuntimeError("测试仓位或订单尚未结清，保持原策略隔离，不能直接finish")
    with api_session(run["user_id"]) as api:
        api(
            "POST",
            "/engine/pause",
            {"reason": f"限额Paper验证结束 {args.run_id}", "cancel_orders": False},
        )
        for sid in run["strategies"].values():
            api("POST", f"/strategies/{sid}/disable")
        risk = api("GET", "/risk-settings")
        api(
            "PUT",
            "/risk-settings",
            {**risk, "cash_sweep_enabled": run["original_cash_sweep_enabled"]},
        )
        for sid in run["original_enabled"]:
            api("POST", f"/strategies/{sid}/enable")
    run["phase"] = "finished"
    run["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(path, run)
    return status(args, path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "finish"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--minutes", type=int, default=45)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.run_id.replace("-", "").isalnum() or not 10 <= args.minutes <= 60:
        raise SystemExit("run-id或时长无效")
    if args.action != "status" and not args.execute:
        raise SystemExit("改变状态需要 --execute")
    path = Path(get_settings().investor_db_path).parent / "validation" / f"{args.run_id}.json"
    result = {"start": start, "status": status, "finish": finish}[args.action](args, path)
    if args.action == "start":
        result = {
            k: result[k]
            for k in [
                "run_id",
                "phase",
                "expires_at",
                "estimated_peak_notional",
                "strategies",
                "preflight",
            ]
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
