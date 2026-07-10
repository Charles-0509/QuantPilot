from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .database import get_db
from .models import (
    BacktestRun,
    ConnectionConfig,
    EngineState,
    EventLog,
    OrderRecord,
    RiskSettings,
    Signal,
    Strategy,
    StrategyVersion,
    WatchlistItem,
    utcnow,
)
from .schemas import (
    BacktestRead,
    BacktestRequest,
    ConnectionConfigRead,
    ConnectionConfigUpdate,
    EngineAction,
    RiskSettingsRead,
    RiskSettingsUpdate,
    RuleDefinition,
    StrategyCreate,
    StrategyRead,
    StrategyVersionRead,
    WatchlistUpdate,
)
from .services.alpaca_service import AlpacaService
from .services.backtest import run_backtest
from .services.credentials import encrypt_credential
from .services.engine import TradingEngine
from .services.websocket import websocket_manager

router = APIRouter(prefix="/api")


def get_alpaca(request: Request) -> AlpacaService:
    return request.app.state.alpaca


def get_engine(request: Request) -> TradingEngine:
    return request.app.state.trading_engine


def service_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "paper": True, "version": "1.1.0"}


@router.get("/metadata")
def metadata() -> dict[str, Any]:
    return {
        "timeframes": ["5Min", "15Min", "30Min", "1Hour", "1Day"],
        "operators": [">", ">=", "<", "<=", "==", "crosses_above", "crosses_below"],
        "indicators": {
            "SMA": {"period": 20},
            "EMA": {"period": 20},
            "RSI": {"period": 14},
            "MACD": {"fast": 12, "slow": 26, "signal": 9},
            "BOLLINGER": {"period": 20, "std": 2},
            "ATR": {"period": 14},
            "ROC": {"period": 12},
            "HIGHEST": {"period": 20, "exclude_current": True},
            "LOWEST": {"period": 20, "exclude_current": True},
            "VOLUME_SMA": {"period": 20, "multiplier": 1.5},
            "DEVIATION": {"period": 20},
        },
        "paper_only": True,
        "max_iex_symbols": 30,
    }


@router.get("/connection")
async def connection(alpaca: AlpacaService = Depends(get_alpaca)) -> dict[str, Any]:
    return await asyncio.to_thread(alpaca.connection_status)


@router.get("/connection/config", response_model=ConnectionConfigRead)
def connection_config(alpaca: AlpacaService = Depends(get_alpaca)) -> dict[str, Any]:
    """Return connection metadata only; API secrets are never returned."""
    return alpaca.connection_config()


@router.put("/connection/config", response_model=ConnectionConfigRead)
async def update_connection_config(
    payload: ConnectionConfigUpdate,
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
    engine: TradingEngine = Depends(get_engine),
) -> dict[str, Any]:
    api_key_id = payload.api_key_id.strip()
    api_secret_key = payload.api_secret_key.strip()
    if not AlpacaService._credentials_valid(api_key_id, api_secret_key):
        raise HTTPException(status_code=422, detail="请填写有效的 Alpaca Paper API Key 和 Secret")

    try:
        await asyncio.to_thread(AlpacaService.validate_credentials, api_key_id, api_secret_key)
    except Exception:
        # Do not return SDK exception text: it can include request details and is
        # not useful enough to justify any chance of exposing credential data.
        raise HTTPException(status_code=422, detail="Alpaca Paper 验证失败，请检查 API Key、Secret 和网络")

    try:
        api_key_cipher = encrypt_credential(api_key_id, alpaca.settings)
        api_secret_cipher = encrypt_credential(api_secret_key, alpaca.settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="无法安全保存本机 Alpaca 配置") from exc

    saved = db.get(ConnectionConfig, 1)
    if saved is None:
        saved = ConnectionConfig(
            id=1,
            api_key_cipher=api_key_cipher,
            api_secret_cipher=api_secret_cipher,
            data_feed="iex",
        )
        db.add(saved)
    else:
        saved.api_key_cipher = api_key_cipher
        saved.api_secret_cipher = api_secret_cipher
        saved.data_feed = "iex"
    db.commit()
    db.refresh(saved)

    # No orders are cancelled here: credentials may be changing accounts, and
    # cancellation must never be sent with an account that the user did not choose.
    await engine.pause("Alpaca 连接配置已更新，请检查后重新启动引擎", cancel_orders=False)
    alpaca.configure(
        api_key_id,
        api_secret_key,
        feed="iex",
        source="web",
        updated_at=saved.updated_at,
    )
    engine.reset_connection()
    return alpaca.connection_config()


@router.delete("/connection/config", response_model=ConnectionConfigRead)
async def delete_connection_config(
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
    engine: TradingEngine = Depends(get_engine),
) -> dict[str, Any]:
    saved = db.get(ConnectionConfig, 1)
    if saved is not None:
        db.delete(saved)
        db.commit()
    await engine.pause("已移除网页 Alpaca 配置，请检查后重新启动引擎", cancel_orders=False)
    alpaca.configure_from_env()
    engine.reset_connection()
    return alpaca.connection_config()


@router.get("/account")
async def account(alpaca: AlpacaService = Depends(get_alpaca)) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(alpaca.get_account)
    except Exception as exc:
        raise service_error(exc) from exc


@router.get("/positions")
async def positions(alpaca: AlpacaService = Depends(get_alpaca)) -> list[dict[str, Any]]:
    try:
        return await asyncio.to_thread(alpaca.get_positions)
    except Exception as exc:
        raise service_error(exc) from exc


@router.get("/orders")
async def orders(
    status: str = Query(default="all", pattern="^(all|open)$"),
    alpaca: AlpacaService = Depends(get_alpaca),
) -> list[dict[str, Any]]:
    try:
        return await asyncio.to_thread(alpaca.get_orders, status)
    except Exception as exc:
        raise service_error(exc) from exc


@router.get("/market/clock")
async def market_clock(alpaca: AlpacaService = Depends(get_alpaca)) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(alpaca.get_clock)
    except Exception as exc:
        raise service_error(exc) from exc


@router.get("/market/quotes")
async def latest_quotes(
    symbols: str,
    alpaca: AlpacaService = Depends(get_alpaca),
) -> dict[str, Any]:
    parsed = [symbol.strip().upper() for symbol in symbols.split(",") if symbol.strip()][:30]
    try:
        return await asyncio.to_thread(alpaca.get_latest_quotes, parsed)
    except Exception as exc:
        raise service_error(exc) from exc


@router.get("/market/bars")
async def market_bars(
    symbol: str,
    timeframe: str = "15Min",
    limit: int = Query(default=200, ge=20, le=1000),
    alpaca: AlpacaService = Depends(get_alpaca),
) -> list[dict[str, Any]]:
    try:
        frames = await asyncio.to_thread(alpaca.recent_bars, [symbol.upper()], timeframe, limit)
        frame = frames.get(symbol.upper())
        if frame is None or frame.empty:
            return []
        return [
            {
                "timestamp": index.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            for index, row in frame.tail(limit).iterrows()
        ]
    except Exception as exc:
        raise service_error(exc) from exc


@router.get("/watchlist")
def get_watchlist(db: Session = Depends(get_db)) -> list[str]:
    return db.scalars(select(WatchlistItem.symbol).order_by(WatchlistItem.symbol)).all()


@router.put("/watchlist")
def update_watchlist(payload: WatchlistUpdate, db: Session = Depends(get_db)) -> list[str]:
    db.execute(delete(WatchlistItem))
    for symbol in payload.symbols:
        db.add(WatchlistItem(symbol=symbol))
    db.commit()
    return payload.symbols


@router.get("/strategies", response_model=list[StrategyRead])
def list_strategies(db: Session = Depends(get_db)) -> list[Strategy]:
    return db.scalars(select(Strategy).order_by(Strategy.is_template.desc(), Strategy.updated_at.desc())).all()


@router.get("/strategies/{strategy_id}", response_model=StrategyRead)
def get_strategy(strategy_id: str, db: Session = Depends(get_db)) -> Strategy:
    strategy = db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return strategy


@router.post("/strategies/validate")
def validate_strategy(definition: RuleDefinition) -> dict[str, Any]:
    return {"valid": True, "definition": definition.model_dump(mode="json")}


@router.post("/strategies", response_model=StrategyRead, status_code=201)
def create_strategy(payload: StrategyCreate, db: Session = Depends(get_db)) -> Strategy:
    definition = payload.definition.model_dump(mode="json")
    strategy = Strategy(
        name=payload.definition.name,
        description=payload.definition.description,
        definition=definition,
        is_template=False,
        enabled=False,
        version=1,
    )
    db.add(strategy)
    db.flush()
    db.add(StrategyVersion(strategy_id=strategy.id, version=1, definition=definition))
    db.commit()
    db.refresh(strategy)
    return strategy


@router.put("/strategies/{strategy_id}", response_model=StrategyRead)
def update_strategy(strategy_id: str, payload: StrategyCreate, db: Session = Depends(get_db)) -> Strategy:
    strategy = db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    if strategy.is_template:
        raise HTTPException(status_code=409, detail="模板不可直接修改，请先复制")
    definition = payload.definition.model_dump(mode="json")
    strategy.version += 1
    strategy.name = payload.definition.name
    strategy.description = payload.definition.description
    strategy.definition = definition
    strategy.updated_at = utcnow()
    db.add(StrategyVersion(strategy_id=strategy.id, version=strategy.version, definition=definition))
    db.commit()
    db.refresh(strategy)
    return strategy


@router.post("/strategies/{strategy_id}/clone", response_model=StrategyRead, status_code=201)
def clone_strategy(strategy_id: str, db: Session = Depends(get_db)) -> Strategy:
    source = db.get(Strategy, strategy_id)
    if source is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    definition = deepcopy(source.definition)
    definition["name"] = f"{source.name} - 副本"
    clone = Strategy(
        name=definition["name"],
        description=source.description,
        definition=definition,
        is_template=False,
        enabled=False,
        version=1,
    )
    db.add(clone)
    db.flush()
    db.add(StrategyVersion(strategy_id=clone.id, version=1, definition=definition))
    db.commit()
    db.refresh(clone)
    return clone


@router.delete("/strategies/{strategy_id}", status_code=204)
def delete_strategy(strategy_id: str, db: Session = Depends(get_db)) -> None:
    strategy = db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    if strategy.is_template:
        raise HTTPException(status_code=409, detail="内置模板不能删除")
    db.delete(strategy)
    db.commit()


@router.post("/strategies/{strategy_id}/enable", response_model=StrategyRead)
def enable_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
) -> Strategy:
    strategy = db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    if strategy.is_template:
        raise HTTPException(status_code=409, detail="请复制模板后再启用")
    if not alpaca.configured:
        raise HTTPException(status_code=503, detail="请先配置 Alpaca Paper API 密钥")
    RuleDefinition.model_validate(strategy.definition)
    enabled = db.scalars(select(Strategy).where(Strategy.enabled.is_(True))).all()
    symbols = set(strategy.definition["symbols"])
    for item in enabled:
        symbols.update(item.definition.get("symbols", []))
    if len(symbols) > 30:
        raise HTTPException(status_code=409, detail="免费 IEX 实时订阅最多支持30个股票代码")
    strategy.enabled = True
    db.commit()
    db.refresh(strategy)
    return strategy


@router.post("/strategies/{strategy_id}/disable", response_model=StrategyRead)
def disable_strategy(strategy_id: str, db: Session = Depends(get_db)) -> Strategy:
    strategy = db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    strategy.enabled = False
    db.commit()
    db.refresh(strategy)
    return strategy


@router.get("/strategies/{strategy_id}/versions", response_model=list[StrategyVersionRead])
def strategy_versions(strategy_id: str, db: Session = Depends(get_db)) -> list[StrategyVersion]:
    return db.scalars(
        select(StrategyVersion)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version.desc())
    ).all()


@router.post("/strategies/{strategy_id}/restore/{version}", response_model=StrategyRead)
def restore_strategy(strategy_id: str, version: int, db: Session = Depends(get_db)) -> Strategy:
    strategy = db.get(Strategy, strategy_id)
    saved = db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.version == version,
        )
    )
    if strategy is None or saved is None:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    if strategy.is_template:
        raise HTTPException(status_code=409, detail="模板不能恢复版本")
    definition = RuleDefinition.model_validate(saved.definition).model_dump(mode="json")
    strategy.version += 1
    strategy.definition = definition
    strategy.name = definition["name"]
    strategy.description = definition["description"]
    db.add(StrategyVersion(strategy_id=strategy.id, version=strategy.version, definition=definition))
    db.commit()
    db.refresh(strategy)
    return strategy


@router.get("/backtests", response_model=list[BacktestRead])
def list_backtests(db: Session = Depends(get_db)) -> list[BacktestRun]:
    return db.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(50)).all()


@router.get("/backtests/{run_id}", response_model=BacktestRead)
def get_backtest(run_id: str, db: Session = Depends(get_db)) -> BacktestRun:
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return run


@router.post("/backtests", response_model=BacktestRead, status_code=201)
async def create_backtest(
    payload: BacktestRequest,
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
) -> BacktestRun:
    strategy = db.get(Strategy, payload.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    if not alpaca.configured:
        raise HTTPException(status_code=503, detail="回测需要配置 Alpaca Paper API 密钥以获取历史行情")
    definition = RuleDefinition.model_validate(strategy.definition)
    parameters = payload.model_dump(mode="json")
    run = BacktestRun(strategy_id=strategy.id, status="running", parameters=parameters)
    db.add(run)
    db.commit()
    db.refresh(run)

    def execute() -> tuple[dict, list, list, list]:
        frames = alpaca.get_bars(definition.symbols, definition.timeframe, payload.start, payload.end)
        benchmark_frames = alpaca.get_bars([payload.benchmark], definition.timeframe, payload.start, payload.end)
        result = run_backtest(
            definition,
            frames,
            initial_cash=payload.initial_cash,
            slippage_bps=payload.slippage_bps,
            commission=payload.commission,
            benchmark=benchmark_frames.get(payload.benchmark),
        )
        return result.metrics, result.equity_curve, result.benchmark_curve, result.trades

    try:
        metrics, equity, benchmark, trades = await asyncio.to_thread(execute)
        run.status = "completed"
        run.metrics = metrics
        run.equity_curve = equity
        run.benchmark_curve = benchmark
        run.trades = trades
        run.completed_at = utcnow()
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)
        run.completed_at = utcnow()
    db.commit()
    db.refresh(run)
    return run


@router.get("/engine")
def engine_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    state = db.get(EngineState, 1)
    enabled_count = db.scalar(select(func.count()).select_from(Strategy).where(Strategy.enabled.is_(True)))
    return {
        "status": state.status,
        "reason": state.reason,
        "last_heartbeat": state.last_heartbeat,
        "enabled_strategies": enabled_count,
        "paper": True,
    }


@router.post("/engine/resume")
async def resume_engine(payload: EngineAction, engine: TradingEngine = Depends(get_engine)) -> dict[str, str]:
    try:
        await engine.resume(payload.reason)
        return {"status": "running"}
    except Exception as exc:
        raise service_error(exc) from exc


@router.post("/engine/pause")
async def pause_engine(payload: EngineAction, engine: TradingEngine = Depends(get_engine)) -> dict[str, str]:
    await engine.pause(payload.reason, cancel_orders=True)
    return {"status": "paused"}


@router.post("/engine/cancel-orders")
async def cancel_orders(alpaca: AlpacaService = Depends(get_alpaca)) -> Any:
    try:
        return await asyncio.to_thread(alpaca.cancel_all_orders)
    except Exception as exc:
        raise service_error(exc) from exc


@router.post("/engine/emergency-liquidate")
async def emergency_liquidate(
    payload: EngineAction, engine: TradingEngine = Depends(get_engine)
) -> dict[str, Any]:
    try:
        result = await engine.emergency_liquidate(payload.reason)
        return {"status": "liquidating", "orders": result}
    except Exception as exc:
        raise service_error(exc) from exc


@router.get("/risk-settings", response_model=RiskSettingsRead)
def get_risk_settings(db: Session = Depends(get_db)) -> RiskSettings:
    return db.get(RiskSettings, 1)


@router.put("/risk-settings", response_model=RiskSettingsRead)
def update_risk_settings(payload: RiskSettingsUpdate, db: Session = Depends(get_db)) -> RiskSettings:
    settings = db.get(RiskSettings, 1)
    for key, value in payload.model_dump().items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


@router.get("/events")
def events(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.scalars(select(EventLog).order_by(EventLog.created_at.desc()).limit(limit)).all()
    return [
        {
            "id": row.id,
            "level": row.level,
            "category": row.category,
            "message": row.message,
            "details": row.details,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/signals")
def signals(
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.scalars(select(Signal).order_by(Signal.created_at.desc()).limit(limit)).all()
    return [
        {
            "id": row.id,
            "strategy_id": row.strategy_id,
            "symbol": row.symbol,
            "action": row.action,
            "price": row.price,
            "reason": row.reason,
            "status": row.status,
            "bar_timestamp": row.bar_timestamp,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/dashboard")
async def dashboard(
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
) -> dict[str, Any]:
    connection_status = await asyncio.to_thread(alpaca.connection_status)
    account_data: dict[str, Any] = {}
    positions_data: list[dict[str, Any]] = []
    orders_data: list[dict[str, Any]] = []
    clock_data: dict[str, Any] = {}
    if connection_status["connected"]:
        try:
            account_data, positions_data, orders_data, clock_data = await asyncio.gather(
                asyncio.to_thread(alpaca.get_account),
                asyncio.to_thread(alpaca.get_positions),
                asyncio.to_thread(alpaca.get_orders, "open"),
                asyncio.to_thread(alpaca.get_clock),
            )
        except Exception:
            pass
    state = db.get(EngineState, 1)
    recent_events = db.scalars(select(EventLog).order_by(EventLog.created_at.desc()).limit(12)).all()
    recent_signals = db.scalars(select(Signal).order_by(Signal.created_at.desc()).limit(8)).all()
    return {
        "connection": connection_status,
        "account": account_data,
        "positions": positions_data,
        "orders": orders_data,
        "clock": clock_data,
        "engine": {
            "status": state.status,
            "reason": state.reason,
            "last_heartbeat": state.last_heartbeat,
        },
        "events": [
            {
                "level": row.level,
                "category": row.category,
                "message": row.message,
                "created_at": row.created_at,
            }
            for row in recent_events
        ],
        "signals": [
            {
                "symbol": row.symbol,
                "action": row.action,
                "price": row.price,
                "reason": row.reason,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in recent_signals
        ],
    }


async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket_manager.connect(websocket)
    try:
        await websocket.send_json({"event": "connected", "data": {"paper": True}})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await websocket_manager.disconnect(websocket)
    except Exception:
        await websocket_manager.disconnect(websocket)
