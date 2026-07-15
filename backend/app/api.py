from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, defer

from .auth_api import require_session
from .database import SessionLocal, get_db
from .models import (
    BacktestRun,
    ConnectionConfig,
    EngineState,
    EventLog,
    ExecutionIncident,
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
    BacktestSummaryRead,
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
from .services.auth import AuthenticatedSession
from .services.backtest import run_backtest
from .services.credentials import encrypt_credential
from .services.engine import (
    ConnectionReconfigurationBlockedError,
    ExecutionQuarantineError,
    StrategyExecutionActiveError,
    TradingEngine,
)
from .services.runtime import UserRuntime, UserRuntimeManager
from .services.strategy_safety import strategy_has_unresolved_execution
from .services.websocket import websocket_manager

router = APIRouter(prefix="/api")
backtest_semaphore = asyncio.Semaphore(2)


def current_user_id(session: AuthenticatedSession = Depends(require_session)) -> int:
    return session.user.id


async def get_runtime(
    request: Request,
    session: AuthenticatedSession = Depends(require_session),
) -> UserRuntime:
    manager: UserRuntimeManager = request.app.state.runtime_manager
    return await manager.ensure(session.user.id)


def get_alpaca(runtime: UserRuntime = Depends(get_runtime)) -> AlpacaService:
    return runtime.alpaca


def get_engine(runtime: UserRuntime = Depends(get_runtime)) -> TradingEngine:
    return runtime.engine


def service_error(exc: Exception) -> HTTPException:
    user_message = getattr(exc, "user_message", None)
    return HTTPException(
        status_code=503,
        detail=user_message or "Alpaca Paper 暂时不可用，系统将自动重试",
    )


ENGINE_CRITICAL_OPERATIONS = {
    ("trading", "clock"),
    ("trading", "account"),
    ("trading", "positions"),
    ("trading", "orders:open"),
    ("trading", "calendar"),
    ("trading", "asset"),
    ("trading", "submit_order"),
    ("data", "stock_bars"),
    ("data", "latest_quotes"),
}

def ensure_strategy_definition_is_mutable(
    db: Session, user_id: int, strategy_id: str
) -> None:
    if strategy_has_unresolved_execution(db, user_id, strategy_id):
        raise HTTPException(
            status_code=409,
            detail="该策略仍有持仓、待对账信号或未结订单，暂不能修改或删除",
        )


def engine_connection_state(connection: dict[str, Any]) -> str:
    """Return the connection state that actually gates automatic orders.

    Only failures that can invalidate the account snapshot, tradability check,
    execution price, or submit result block automatic orders. Other UI/research
    requests remain visible in global health without contradicting the engine.
    """
    state = str(connection.get("state", "unknown"))
    if state != "degraded":
        return state
    health = connection.get("health")
    if not isinstance(health, dict) or "failed_operations" not in health:
        return "degraded"
    failures = health.get("failed_operations", [])
    for failure in failures:
        key = (str(failure.get("channel", "")), str(failure.get("operation", "")))
        if key in ENGINE_CRITICAL_OPERATIONS:
            return "degraded"
    return "connected"


def operational_engine_status(
    desired_status: str, connection: dict[str, Any]
) -> tuple[str, bool, str]:
    if desired_status != "running":
        return "paused", False, "交易引擎已暂停"
    connection_state = engine_connection_state(connection)
    if connection_state == "connected":
        return "active", True, "交易链路正常"
    if connection_state == "circuit_open":
        return "circuit_open", False, "Alpaca 连接保护已开启，等待自动恢复"
    return "degraded", False, "Alpaca 连接不稳定，暂缓策略评估与新订单"


def collect_dashboard_snapshot(alpaca: AlpacaService) -> dict[str, Any]:
    values: dict[str, Any] = {
        "account": {},
        "positions": [],
        "orders": [],
        "clock": {},
    }
    availability = {key: "unavailable" for key in values}
    errors: dict[str, str] = {}
    if not alpaca.configured:
        return {**values, "availability": availability, "data_errors": errors}
    operations = (
        ("account", alpaca.get_account, "账户数据暂时不可用"),
        ("positions", alpaca.get_positions, "暂时无法确认当前持仓"),
        ("orders", lambda: alpaca.get_orders("open"), "暂时无法确认开放订单"),
        ("clock", alpaca.get_clock, "市场开闭状态暂时不可用"),
    )
    for index, (key, operation, message) in enumerate(operations):
        try:
            values[key] = operation()
            availability[key] = "fresh"
        except Exception:
            errors[key] = message
            # All dashboard reads use the same Alpaca Trading channel. Once one
            # read fails, continuing with the remaining calls can multiply a
            # bounded retry window into a minute-long HTTP request. Preserve
            # the fields that already succeeded and fail the rest closed.
            for remaining_key, _remaining_operation, remaining_message in operations[
                index + 1 :
            ]:
                errors.setdefault(remaining_key, remaining_message)
            break
    return {**values, "availability": availability, "data_errors": errors}


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "paper": True, "version": "1.4.0"}


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
def connection(alpaca: AlpacaService = Depends(get_alpaca)) -> dict[str, Any]:
    # This endpoint is polled by the UI. It must only read the in-memory health
    # snapshot and must never generate another Alpaca request by itself.
    return alpaca.connection_status()


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
    user_id: int = Depends(current_user_id),
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

    reason = "Alpaca 连接配置已更新，请检查后重新启动引擎"
    try:
        async with engine.connection_reconfiguration(reason):
            saved = db.scalar(
                select(ConnectionConfig).where(ConnectionConfig.user_id == user_id)
            )
            if saved is None:
                saved = ConnectionConfig(
                    user_id=user_id,
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
            await asyncio.to_thread(
                alpaca.configure,
                api_key_id,
                api_secret_key,
                feed="iex",
                source="web",
                updated_at=saved.updated_at,
            )
            engine.reset_connection()
    except ConnectionReconfigurationBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return alpaca.connection_config()


@router.delete("/connection/config", response_model=ConnectionConfigRead)
async def delete_connection_config(
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
    engine: TradingEngine = Depends(get_engine),
    user_id: int = Depends(current_user_id),
) -> dict[str, Any]:
    reason = "已移除网页 Alpaca 配置，请检查后重新启动引擎"
    try:
        async with engine.connection_reconfiguration(reason):
            saved = db.scalar(
                select(ConnectionConfig).where(ConnectionConfig.user_id == user_id)
            )
            if saved is not None:
                db.delete(saved)
                db.commit()
            if user_id == 1:
                await asyncio.to_thread(alpaca.configure_from_env)
            else:
                await asyncio.to_thread(alpaca.clear_configuration)
            engine.reset_connection()
    except ConnectionReconfigurationBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
def get_watchlist(
    db: Session = Depends(get_db), user_id: int = Depends(current_user_id)
) -> list[str]:
    return db.scalars(
        select(WatchlistItem.symbol)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.symbol)
    ).all()


@router.put("/watchlist")
def update_watchlist(
    payload: WatchlistUpdate,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> list[str]:
    enabled = db.scalars(
        select(Strategy).where(
            Strategy.enabled.is_(True), Strategy.owner_user_id == user_id
        )
    ).all()
    subscribed_symbols = set(payload.symbols)
    for strategy in enabled:
        subscribed_symbols.update(strategy.definition.get("symbols", []))
    if len(subscribed_symbols) > 30:
        raise HTTPException(
            status_code=409,
            detail="自选股与已启用策略合计最多订阅30个股票代码",
        )
    db.execute(delete(WatchlistItem).where(WatchlistItem.user_id == user_id))
    for symbol in payload.symbols:
        db.add(WatchlistItem(user_id=user_id, symbol=symbol))
    db.commit()
    return payload.symbols


@router.get("/strategies", response_model=list[StrategyRead])
def list_strategies(
    db: Session = Depends(get_db), user_id: int = Depends(current_user_id)
) -> list[Strategy]:
    return db.scalars(
        select(Strategy)
        .where((Strategy.is_template.is_(True)) | (Strategy.owner_user_id == user_id))
        .order_by(Strategy.is_template.desc(), Strategy.updated_at.desc())
    ).all()


@router.get("/strategies/{strategy_id}", response_model=StrategyRead)
def get_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> Strategy:
    strategy = db.scalar(
        select(Strategy).where(
            Strategy.id == strategy_id,
            (Strategy.is_template.is_(True)) | (Strategy.owner_user_id == user_id),
        )
    )
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return strategy


@router.post("/strategies/validate")
def validate_strategy(definition: RuleDefinition) -> dict[str, Any]:
    return {"valid": True, "definition": definition.model_dump(mode="json")}


@router.post("/strategies", response_model=StrategyRead, status_code=201)
def create_strategy(
    payload: StrategyCreate,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> Strategy:
    definition = payload.definition.model_dump(mode="json")
    strategy = Strategy(
        owner_user_id=user_id,
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
def update_strategy(
    strategy_id: str,
    payload: StrategyCreate,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> Strategy:
    strategy = db.scalar(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.owner_user_id == user_id)
    )
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    if strategy.is_template:
        raise HTTPException(status_code=409, detail="模板不可直接修改，请先复制")
    ensure_strategy_definition_is_mutable(db, user_id, strategy_id)
    definition = payload.definition.model_dump(mode="json")
    if strategy.enabled:
        subscribed_symbols = set(definition["symbols"])
        subscribed_symbols.update(
            db.scalars(
                select(WatchlistItem.symbol).where(WatchlistItem.user_id == user_id)
            ).all()
        )
        other_enabled = db.scalars(
            select(Strategy).where(
                Strategy.enabled.is_(True),
                Strategy.owner_user_id == user_id,
                Strategy.id != strategy.id,
            )
        ).all()
        for item in other_enabled:
            subscribed_symbols.update(item.definition.get("symbols", []))
        if len(subscribed_symbols) > 30:
            raise HTTPException(
                status_code=409,
                detail="自选股与已启用策略合计最多订阅30个股票代码",
            )
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
def clone_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> Strategy:
    source = db.scalar(
        select(Strategy).where(
            Strategy.id == strategy_id,
            (Strategy.is_template.is_(True)) | (Strategy.owner_user_id == user_id),
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    definition = deepcopy(source.definition)
    definition["name"] = f"{source.name} - 副本"
    clone = Strategy(
        owner_user_id=user_id,
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
def delete_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> None:
    strategy = db.scalar(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.owner_user_id == user_id)
    )
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    if strategy.is_template:
        raise HTTPException(status_code=409, detail="内置模板不能删除")
    ensure_strategy_definition_is_mutable(db, user_id, strategy_id)
    db.delete(strategy)
    db.commit()


@router.post("/strategies/{strategy_id}/enable", response_model=StrategyRead)
def enable_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
    user_id: int = Depends(current_user_id),
) -> Strategy:
    strategy = db.scalar(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.owner_user_id == user_id)
    )
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    if strategy.is_template:
        raise HTTPException(status_code=409, detail="请复制模板后再启用")
    if not alpaca.configured:
        raise HTTPException(status_code=503, detail="请先配置 Alpaca Paper API 密钥")
    RuleDefinition.model_validate(strategy.definition)
    enabled = db.scalars(
        select(Strategy).where(
            Strategy.enabled.is_(True), Strategy.owner_user_id == user_id
        )
    ).all()
    symbols = set(strategy.definition["symbols"])
    for item in enabled:
        symbols.update(item.definition.get("symbols", []))
    symbols.update(
        db.scalars(
            select(WatchlistItem.symbol).where(WatchlistItem.user_id == user_id)
        ).all()
    )
    if len(symbols) > 30:
        raise HTTPException(
            status_code=409,
            detail="自选股与已启用策略合计最多订阅30个股票代码",
        )
    strategy.enabled = True
    db.commit()
    db.refresh(strategy)
    return strategy


@router.post("/strategies/{strategy_id}/disable", response_model=StrategyRead)
async def disable_strategy(
    strategy_id: str,
    db: Session = Depends(get_db),
    engine: TradingEngine = Depends(get_engine),
    user_id: int = Depends(current_user_id),
) -> Strategy:
    try:
        await engine.disable_strategy(strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="策略不存在")
    except StrategyExecutionActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.expire_all()
    strategy = db.scalar(
        select(Strategy).where(
            Strategy.id == strategy_id, Strategy.owner_user_id == user_id
        )
    )
    if strategy is None:  # pragma: no cover - protected by the engine gate
        raise HTTPException(status_code=404, detail="策略不存在")
    return strategy


@router.get("/strategies/{strategy_id}/versions", response_model=list[StrategyVersionRead])
def strategy_versions(
    strategy_id: str,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> list[StrategyVersion]:
    strategy = db.scalar(
        select(Strategy.id).where(
            Strategy.id == strategy_id,
            (Strategy.is_template.is_(True)) | (Strategy.owner_user_id == user_id),
        )
    )
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return db.scalars(
        select(StrategyVersion)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version.desc())
    ).all()


@router.post("/strategies/{strategy_id}/restore/{version}", response_model=StrategyRead)
def restore_strategy(
    strategy_id: str,
    version: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> Strategy:
    strategy = db.scalar(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.owner_user_id == user_id)
    )
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
    ensure_strategy_definition_is_mutable(db, user_id, strategy_id)
    definition = RuleDefinition.model_validate(saved.definition).model_dump(mode="json")
    if strategy.enabled:
        subscribed_symbols = set(definition["symbols"])
        subscribed_symbols.update(
            db.scalars(
                select(WatchlistItem.symbol).where(WatchlistItem.user_id == user_id)
            ).all()
        )
        other_enabled = db.scalars(
            select(Strategy).where(
                Strategy.enabled.is_(True),
                Strategy.owner_user_id == user_id,
                Strategy.id != strategy.id,
            )
        ).all()
        for item in other_enabled:
            subscribed_symbols.update(item.definition.get("symbols", []))
        if len(subscribed_symbols) > 30:
            raise HTTPException(
                status_code=409,
                detail="自选股与已启用策略合计最多订阅30个股票代码",
            )
    strategy.version += 1
    strategy.definition = definition
    strategy.name = definition["name"]
    strategy.description = definition["description"]
    db.add(StrategyVersion(strategy_id=strategy.id, version=strategy.version, definition=definition))
    db.commit()
    db.refresh(strategy)
    return strategy


@router.get("/backtests", response_model=list[BacktestSummaryRead])
def list_backtests(
    db: Session = Depends(get_db), user_id: int = Depends(current_user_id)
) -> list[BacktestRun]:
    return db.scalars(
        select(BacktestRun)
        .where(BacktestRun.user_id == user_id)
        .options(
            defer(BacktestRun.equity_curve),
            defer(BacktestRun.benchmark_curve),
            defer(BacktestRun.trades),
        )
        .order_by(BacktestRun.created_at.desc())
        .limit(50)
    ).all()


@router.get("/backtests/{run_id}", response_model=BacktestRead)
def get_backtest(
    run_id: str,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> BacktestRun:
    run = db.scalar(
        select(BacktestRun).where(BacktestRun.id == run_id, BacktestRun.user_id == user_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return run


async def execute_backtest_job(
    user_id: int,
    run_id: str,
    definition_data: dict[str, Any],
    payload_data: dict[str, Any],
    alpaca: AlpacaService,
) -> None:
    async with backtest_semaphore:
        with SessionLocal() as db:
            run = db.scalar(
                select(BacktestRun).where(
                    BacktestRun.id == run_id, BacktestRun.user_id == user_id
                )
            )
            if run is None:
                return
            run.status = "running"
            db.commit()
        await websocket_manager.broadcast(user_id, "backtest", {"id": run_id, "status": "running"})

        definition = RuleDefinition.model_validate(definition_data)
        payload = BacktestRequest.model_validate(payload_data)

        def execute() -> tuple[dict, list, list, list]:
            requested = list(dict.fromkeys([*definition.symbols, payload.benchmark]))
            frames = alpaca.get_bars(
                requested,
                definition.timeframe,
                payload.start,
                payload.end,
                use_cache=True,
            )
            result = run_backtest(
                definition,
                {symbol: frames.get(symbol, pd.DataFrame()) for symbol in definition.symbols},
                initial_cash=payload.initial_cash,
                slippage_bps=payload.slippage_bps,
                commission=payload.commission,
                benchmark=frames.get(payload.benchmark),
            )
            return result.metrics, result.equity_curve, result.benchmark_curve, result.trades

        try:
            metrics, equity, benchmark, trades = await asyncio.to_thread(execute)
            with SessionLocal() as db:
                run = db.scalar(
                    select(BacktestRun).where(
                        BacktestRun.id == run_id, BacktestRun.user_id == user_id
                    )
                )
                if run is None:
                    return
                run.status = "completed"
                run.metrics = metrics
                run.equity_curve = equity
                run.benchmark_curve = benchmark
                run.trades = trades
                run.error = None
                run.completed_at = utcnow()
                db.commit()
            await websocket_manager.broadcast(
                user_id, "backtest", {"id": run_id, "status": "completed"}
            )
        except Exception as exc:
            with SessionLocal() as db:
                run = db.scalar(
                    select(BacktestRun).where(
                        BacktestRun.id == run_id, BacktestRun.user_id == user_id
                    )
                )
                if run is None:
                    return
                run.status = "failed"
                run.error = str(exc)
                run.completed_at = utcnow()
                db.commit()
            await websocket_manager.broadcast(
                user_id,
                "backtest",
                {"id": run_id, "status": "failed"},
            )


@router.post("/backtests", response_model=BacktestSummaryRead, status_code=202)
async def create_backtest(
    payload: BacktestRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
    user_id: int = Depends(current_user_id),
) -> BacktestRun:
    strategy = db.scalar(
        select(Strategy).where(
            Strategy.id == payload.strategy_id,
            (Strategy.is_template.is_(True)) | (Strategy.owner_user_id == user_id),
        )
    )
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    if not alpaca.configured:
        raise HTTPException(status_code=503, detail="回测需要配置 Alpaca Paper API 密钥以获取历史行情")
    definition_data = deepcopy(strategy.definition)
    if payload.symbols:
        definition_data["symbols"] = payload.symbols
    definition = RuleDefinition.model_validate(definition_data)
    max_days = {
        "5Min": 730,
        "15Min": 1825,
        "30Min": 3650,
        "1Hour": 5475,
        "1Day": 10950,
    }[definition.timeframe]
    if (payload.end - payload.start).days > max_days:
        raise HTTPException(
            status_code=422,
            detail=f"{definition.timeframe} 回测区间最多支持 {max_days} 天，请缩短日期范围",
        )
    parameters = payload.model_dump(mode="json")
    parameters["symbols"] = definition.symbols
    run = BacktestRun(
        user_id=user_id, strategy_id=strategy.id, status="queued", parameters=parameters
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    background_tasks.add_task(
        execute_backtest_job,
        user_id,
        run.id,
        definition.model_dump(mode="json"),
        payload.model_dump(mode="json"),
        alpaca,
    )
    return run


@router.get("/engine")
def engine_status(
    db: Session = Depends(get_db),
    alpaca: AlpacaService = Depends(get_alpaca),
    user_id: int = Depends(current_user_id),
) -> dict[str, Any]:
    state = db.scalar(select(EngineState).where(EngineState.user_id == user_id))
    enabled_count = db.scalar(
        select(func.count())
        .select_from(Strategy)
        .where(Strategy.enabled.is_(True), Strategy.owner_user_id == user_id)
    )
    active_incidents = db.scalars(
        select(ExecutionIncident.symbol).where(
            ExecutionIncident.user_id == user_id,
            ExecutionIncident.status == "active",
        )
    ).all()
    connection = alpaca.connection_status()
    operational_status, accepting_new_orders, operational_reason = operational_engine_status(
        state.status, connection
    )
    return {
        "status": state.status,
        "reason": state.reason,
        "last_heartbeat": state.last_heartbeat,
        "enabled_strategies": enabled_count,
        "paper": True,
        "operational_status": operational_status,
        "operational_reason": operational_reason,
        "accepting_new_orders": accepting_new_orders,
        "connection_state": engine_connection_state(connection),
        "last_alpaca_success_at": connection.get("last_success_at"),
        "next_retry_at": connection.get("retry_at"),
        "consecutive_failures": connection.get("consecutive_failures", 0),
        "active_incidents": list(active_incidents),
    }


@router.post("/engine/resume")
async def resume_engine(payload: EngineAction, engine: TradingEngine = Depends(get_engine)) -> dict[str, str]:
    try:
        await engine.resume(payload.reason)
        return {"status": "running"}
    except ExecutionQuarantineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise service_error(exc) from exc


@router.post("/engine/pause")
async def pause_engine(payload: EngineAction, engine: TradingEngine = Depends(get_engine)) -> dict[str, str]:
    await engine.pause(payload.reason, cancel_orders=True)
    return {"status": "paused"}


@router.post("/engine/cancel-orders")
async def cancel_orders(engine: TradingEngine = Depends(get_engine)) -> Any:
    try:
        return await engine.cancel_all_orders()
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
def get_risk_settings(
    db: Session = Depends(get_db), user_id: int = Depends(current_user_id)
) -> RiskSettings:
    return db.scalar(select(RiskSettings).where(RiskSettings.user_id == user_id))


@router.put("/risk-settings", response_model=RiskSettingsRead)
async def update_risk_settings(
    payload: RiskSettingsUpdate,
    db: Session = Depends(get_db),
    engine: TradingEngine = Depends(get_engine),
    user_id: int = Depends(current_user_id),
) -> RiskSettings:
    await engine.update_risk_settings(payload.model_dump())
    db.expire_all()
    settings = db.scalar(select(RiskSettings).where(RiskSettings.user_id == user_id))
    if settings is None:  # pragma: no cover - runtime bootstrap guarantees it
        raise HTTPException(status_code=404, detail="风险设置不存在")
    return settings


@router.get("/events")
def events(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(EventLog)
        .where(EventLog.user_id == user_id)
        .order_by(EventLog.created_at.desc())
        .limit(limit)
    ).all()
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
    user_id: int = Depends(current_user_id),
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Signal)
        .where(Signal.user_id == user_id)
        .order_by(Signal.created_at.desc())
        .limit(limit)
    ).all()
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
    user_id: int = Depends(current_user_id),
) -> dict[str, Any]:
    snapshot = await asyncio.to_thread(collect_dashboard_snapshot, alpaca)
    connection_status = alpaca.connection_status()
    state = db.scalar(select(EngineState).where(EngineState.user_id == user_id))
    operational_status, accepting_new_orders, operational_reason = operational_engine_status(
        state.status, connection_status
    )
    recent_events = db.scalars(
        select(EventLog)
        .where(EventLog.user_id == user_id)
        .order_by(EventLog.created_at.desc())
        .limit(12)
    ).all()
    recent_signals = db.scalars(
        select(Signal)
        .where(Signal.user_id == user_id)
        .order_by(Signal.created_at.desc())
        .limit(8)
    ).all()
    return {
        "connection": connection_status,
        "account": snapshot["account"],
        "positions": snapshot["positions"],
        "orders": snapshot["orders"],
        "clock": snapshot["clock"],
        "availability": snapshot["availability"],
        "data_errors": snapshot["data_errors"],
        "snapshot_at": datetime.now(timezone.utc),
        "engine": {
            "status": state.status,
            "reason": state.reason,
            "last_heartbeat": state.last_heartbeat,
            "operational_status": operational_status,
            "operational_reason": operational_reason,
            "accepting_new_orders": accepting_new_orders,
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


async def websocket_endpoint(websocket: WebSocket, user_id: int) -> None:
    await websocket_manager.connect(websocket, user_id)
    try:
        await websocket.send_json({"event": "connected", "data": {"paper": True}})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await websocket_manager.disconnect(websocket)
    except Exception:
        await websocket_manager.disconnect(websocket)
