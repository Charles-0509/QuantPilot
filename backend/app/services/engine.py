from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    EngineState,
    EventLog,
    ExecutionIncident,
    MarketBar,
    OrderRecord,
    RiskSettings,
    Signal,
    Strategy,
    StrategyPosition,
    WatchlistItem,
)
from app.schemas import RuleDefinition

from .alpaca_service import (
    AlpacaAmbiguousOrderError,
    AlpacaService,
    AlpacaTransientError,
)

from .indicators import latest_atr
from .risk import RiskManager, as_float
from .rules import evaluate_latest
from .strategy_safety import (
    TERMINAL_ORDER_STATUSES,
    strategy_has_unresolved_execution,
    user_has_unresolved_execution,
)
from .websocket import websocket_manager

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


TIMEFRAME_SECONDS = {
    "5Min": 300,
    "15Min": 900,
    "30Min": 1800,
    "1Hour": 3600,
    "1Day": 86400,
}

EVALUATION_INTERVAL_SECONDS = 15
CLOSED_MARKET_INTERVAL_SECONDS = 60
RECONCILE_INTERVAL_SECONDS = 60
RISK_SNAPSHOT_INTERVAL_SECONDS = 60
CONNECTION_ERROR_LOG_INTERVAL_SECONDS = 300
INSUFFICIENT_DATA_LOG_INTERVAL_SECONDS = 300
PENDING_RECONCILIATION_TIMEOUT_SECONDS = 300
PENDING_RECONCILIATION_MIN_MISSES = 3
UNRESOLVED_ORDER_STATUSES = ("pending_submission", "pending_reconciliation")


class StrategyOrderCancellationError(RuntimeError):
    """At least one strategy-owned open order could not be cancelled safely."""

    def __init__(self, order_ids: list[str]):
        self.order_ids = order_ids
        super().__init__("策略自有未成交订单未能全部取消，已中止本次平仓")


class StrategyExecutionActiveError(RuntimeError):
    """A strategy cannot stop while it still owns execution state or positions."""

    def __init__(self):
        super().__init__("该策略仍有持仓、待对账信号或未结订单，暂不能停用")


class ConnectionReconfigurationBlockedError(RuntimeError):
    """Paper credentials cannot change while the old account owns execution state."""

    def __init__(self):
        super().__init__("仍有策略持仓、待对账信号或未结订单，暂不能更换或删除 Alpaca 配置")


class ExecutionQuarantineError(RuntimeError):
    """Automatic trading remains paused until long-only invariants are restored."""

    def __init__(self):
        super().__init__("仍有未解除的执行安全隔离，请先确认持仓和开放卖单")


class LongOnlyInvariantError(RuntimeError):
    def __init__(self, symbol: str, reason: str):
        self.symbol = symbol.upper()
        self.reason = reason
        super().__init__(reason)


@dataclass(slots=True)
class PreparedStrategy:
    strategy: Strategy
    definition: RuleDefinition


@dataclass(slots=True)
class EvaluationSnapshot:
    clock: dict[str, Any]
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    position_map: dict[str, dict[str, Any]]
    open_orders: list[dict[str, Any]]
    bars_by_timeframe: dict[str, dict[str, pd.DataFrame]]


class TradingEngine:
    def __init__(self, settings: Settings, alpaca: AlpacaService, user_id: int = 1):
        self.settings = settings
        self.alpaca = alpaca
        self.user_id = user_id
        self.risk = RiskManager()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._order_submission_lock = asyncio.Lock()
        self._last_evaluated: dict[tuple[str, str], str] = {}
        self._last_reconcile = datetime.min.replace(tzinfo=timezone.utc)
        self._next_strategy_evaluation = datetime.min.replace(tzinfo=timezone.utc)
        self._stream_signature: tuple[str, ...] = ()
        self._connection_failure_active = False
        self._last_connection_error_log = datetime.min.replace(tzinfo=timezone.utc)
        self._last_insufficient_data_log: dict[tuple[str, str], datetime] = {}
        self._last_pending_wait_log = datetime.min.replace(tzinfo=timezone.utc)
        self._stream_limit_error_active = False
        self._abort_new_orders_for_cycle = False
        self._monotonic = time.monotonic
        self._stream_failure_count = 0
        self._next_stream_retry_monotonic = 0.0
        self._stream_recovery_pending = False
        self._last_risk_snapshot = datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _risk_settings_snapshot(settings: RiskSettings) -> dict[str, Any]:
        return {
            "max_symbol_pct": settings.max_symbol_pct,
            "max_total_exposure_pct": settings.max_total_exposure_pct,
            "max_positions": settings.max_positions,
            "max_daily_loss_pct": settings.max_daily_loss_pct,
            "max_intraday_drawdown_pct": settings.max_intraday_drawdown_pct,
            "stale_data_seconds": settings.stale_data_seconds,
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name=f"trading-engine-{self.user_id}")

    async def shutdown(self) -> None:
        self._stop.set()
        await asyncio.to_thread(self.alpaca.stop_streams)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def reset_connection(self) -> None:
        """Forget stream/evaluation state after replacing Paper credentials."""
        self._stream_signature = ()
        self._last_evaluated.clear()
        self._last_reconcile = datetime.min.replace(tzinfo=timezone.utc)
        self._next_strategy_evaluation = datetime.min.replace(tzinfo=timezone.utc)
        self._connection_failure_active = False
        self._last_connection_error_log = datetime.min.replace(tzinfo=timezone.utc)
        self._last_insufficient_data_log.clear()
        self._last_pending_wait_log = datetime.min.replace(tzinfo=timezone.utc)
        self._stream_limit_error_active = False
        self._abort_new_orders_for_cycle = False
        self._stream_failure_count = 0
        self._next_stream_retry_monotonic = 0.0
        self._stream_recovery_pending = False
        self._last_risk_snapshot = datetime.min.replace(tzinfo=timezone.utc)

    async def _run_loop(self) -> None:
        await self.log("info", "engine", "交易引擎后台任务已启动")
        while not self._stop.is_set():
            try:
                await self._heartbeat()
                if self.alpaca.configured:
                    await self._sync_streams()
                    with SessionLocal() as db:
                        state = db.scalar(
                            select(EngineState).where(EngineState.user_id == self.user_id)
                        )
                        running = state is not None and state.status == "running"
                    now = datetime.now(timezone.utc)
                    evaluation_due = running and now >= self._next_strategy_evaluation
                    if evaluation_due:
                        await self.evaluate_active_strategies()
                    elif now - self._last_risk_snapshot >= timedelta(
                        seconds=RISK_SNAPSHOT_INTERVAL_SECONDS
                    ):
                        self._last_risk_snapshot = now
                        await self._refresh_risk_equity_snapshot()
                    if now - self._last_reconcile > timedelta(
                        seconds=RECONCILE_INTERVAL_SECONDS
                    ):
                        await self.reconcile_orders()
                        self._last_reconcile = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - safety boundary
                logger.exception("Trading engine loop error")
                await self.log("error", "engine", f"交易引擎循环异常: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.quote_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _heartbeat(self) -> None:
        with SessionLocal() as db:
            state = db.scalar(select(EngineState).where(EngineState.user_id == self.user_id))
            if state:
                state.last_heartbeat = datetime.now(timezone.utc)
                db.commit()

    async def _refresh_risk_equity_snapshot(
        self, account: dict[str, Any] | None = None
    ) -> bool:
        """Refresh day-start equity and the intraday high independently of signals."""
        try:
            if account is None:
                clock = await asyncio.to_thread(self.alpaca.get_clock)
                if not bool(clock.get("is_open")):
                    return False
                account = await asyncio.to_thread(self.alpaca.get_account)
            with SessionLocal() as db:
                state = db.scalar(
                    select(EngineState).where(EngineState.user_id == self.user_id)
                )
                if state is None:
                    return False
                self.risk.update_equity_state(account=account, state=state)
                db.commit()
            self._last_risk_snapshot = datetime.now(timezone.utc)
            return True
        except Exception as exc:
            logger.info("Unable to refresh Alpaca risk equity snapshot: %s", type(exc).__name__)
            return False

    async def _sync_streams(self) -> None:
        with SessionLocal() as db:
            active = db.scalars(
                select(Strategy).where(
                    Strategy.enabled.is_(True), Strategy.owner_user_id == self.user_id
                )
            ).all()
            watchlist = db.scalars(
                select(WatchlistItem.symbol).where(WatchlistItem.user_id == self.user_id)
            ).all()
        symbols = sorted(
            set(watchlist).union(
                symbol
                for strategy in active
                for symbol in strategy.definition.get("symbols", [])
            )
        )
        if len(symbols) > 30:
            if self._stream_signature:
                await asyncio.to_thread(self.alpaca.stop_streams)
                self._stream_signature = ()
            self._reset_stream_backoff()
            if not self._stream_limit_error_active:
                self._stream_limit_error_active = True
                await self.log(
                    "error",
                    "market_data",
                    f"实时订阅股票共 {len(symbols)} 只，超过 IEX 免费上限 30；"
                    "为避免部分股票静默漏行情，数据流未启动",
                    {"symbol_count": len(symbols), "limit": 30},
                )
            return
        if self._stream_limit_error_active:
            self._stream_limit_error_active = False
            await self.log(
                "info",
                "market_data",
                "实时订阅股票数量已恢复到上限内，数据流将自动重启",
                {"symbol_count": len(symbols), "limit": 30},
            )
        signature = tuple(symbols)
        if signature != self._stream_signature:
            self._reset_stream_backoff()
            await self.alpaca.start_streams(
                symbols, self.handle_bar_update, self.handle_trade_update
            )
            self._stream_signature = signature
            self._stream_recovery_pending = False
            return
        if not signature:
            self._reset_stream_backoff()
            return
        if self.alpaca.streams_healthy():
            if self._stream_recovery_pending or self._stream_failure_count:
                await self.log(
                    "info",
                    "market_data",
                    "Alpaca 实时数据流已恢复",
                    {"symbols": len(signature)},
                )
            self._reset_stream_backoff()
            return

        now = self._monotonic()
        if now < self._next_stream_retry_monotonic:
            return
        self._stream_failure_count += 1
        delay = min(
            self.settings.alpaca_stream_retry_max_seconds,
            self.settings.alpaca_stream_retry_base_seconds
            * (2 ** min(self._stream_failure_count - 1, 10)),
        )
        self._next_stream_retry_monotonic = now + delay
        await self.log(
            "warning",
            "market_data",
            f"Alpaca 实时数据流已中断，{delay:.0f} 秒后继续自动重连",
            {
                "attempt": self._stream_failure_count,
                "retry_in_seconds": delay,
                "symbols": len(signature),
            },
        )
        try:
            await self.alpaca.start_streams(
                symbols, self.handle_bar_update, self.handle_trade_update
            )
        except Exception as exc:
            logger.info("Alpaca stream restart failed: %s", type(exc).__name__)
        self._stream_signature = signature
        self._stream_recovery_pending = True

    def _reset_stream_backoff(self) -> None:
        self._stream_failure_count = 0
        self._next_stream_retry_monotonic = 0.0
        self._stream_recovery_pending = False

    async def handle_bar_update(self, payload: dict[str, Any]) -> None:
        await websocket_manager.broadcast(self.user_id, "market_bar", payload)

    async def handle_trade_update(self, payload: dict[str, Any]) -> None:
        event = str(payload.get("event", "update"))
        order = payload.get("order") or {}
        order_id = str(order.get("id", ""))
        manual_sell_fill: tuple[str, str] | None = None
        with SessionLocal() as db:
            record = db.scalar(
                select(OrderRecord).where(
                    OrderRecord.id == order_id, OrderRecord.user_id == self.user_id
                )
            ) if order_id else None
            if record is None and order_id:
                parent_id = str(order.get("parent_order_id") or "")
                parent = db.scalar(
                    select(OrderRecord).where(
                        OrderRecord.id == parent_id,
                        OrderRecord.user_id == self.user_id,
                    )
                ) if parent_id else None
                if parent is not None:
                    record = OrderRecord(
                        user_id=self.user_id,
                        id=order_id,
                        client_order_id=str(
                            order.get("client_order_id") or f"leg-{order_id[:20]}"
                        ),
                        strategy_id=parent.strategy_id,
                        signal_id=parent.signal_id,
                        symbol=str(order.get("symbol") or parent.symbol),
                        side=self._order_side(order),
                        order_type=str(order.get("type") or "bracket_leg"),
                        qty=as_float(order.get("qty")) or None,
                        notional=None,
                        status=str(order.get("status", event)),
                        raw=payload,
                    )
                    db.add(record)
                    db.flush()
            if record is None and order_id:
                parent_id = str(order.get("parent_order_id") or "")
                client_order_id = str(order.get("client_order_id") or "")
                if not parent_id and not client_order_id.startswith("qp-"):
                    record = OrderRecord(
                        user_id=self.user_id,
                        id=order_id,
                        client_order_id=client_order_id or f"external-{order_id}",
                        strategy_id=None,
                        signal_id=None,
                        symbol=str(order.get("symbol") or "").upper(),
                        side=self._order_side(order),
                        order_type=str(order.get("type") or "external"),
                        qty=as_float(order.get("qty")) or None,
                        notional=None,
                        status=str(order.get("status", event)),
                        filled_qty=0.0,
                        raw=payload,
                    )
                    db.add(record)
                    db.flush()
            if record:
                previous_filled_qty = record.filled_qty
                record.status = str(order.get("status", event))
                filled = order.get("filled_avg_price")
                record.filled_avg_price = as_float(filled) if filled is not None else None
                record.raw = payload
                self._apply_fill_delta(db, record, order)
                db.commit()
                if (
                    record.strategy_id is None
                    and record.side == "sell"
                    and record.filled_qty > previous_filled_qty
                ):
                    manual_sell_fill = (record.symbol, record.id)
                if event in {"fill", "filled"} and record.side == "buy" and record.strategy_id:
                    source_signal = db.get(Signal, record.signal_id) if record.signal_id else None
                    trailing = (
                        (source_signal.payload or {}).get("trailing_stop")
                        if source_signal is not None
                        else None
                    )
                    if trailing:
                        existing_trailing = db.scalar(
                            select(OrderRecord.id).where(
                                OrderRecord.user_id == self.user_id,
                                OrderRecord.signal_id == record.signal_id,
                                OrderRecord.order_type == "trailing_stop",
                            )
                        )
                        if existing_trailing is None:
                            qty = as_float(order.get("filled_qty") or order.get("qty"))
                            if record.signal_id and qty > 0:
                                self._persist_trailing_intent(
                                    record.signal_id,
                                    record.strategy_id,
                                    record.symbol,
                                    qty,
                                    str(trailing.get("mode") or "percent"),
                                    as_float(trailing.get("value")),
                                )
                                await self._submit_pending_trailing_intent(
                                    record.signal_id, recover_existing=False
                                )
            elif (
                self._order_side(order) == "sell"
                and as_float(order.get("filled_qty")) > 0
                and not str(order.get("client_order_id") or "").startswith("qp-")
                and not order.get("parent_order_id")
            ):
                manual_sell_fill = (
                    str(order.get("symbol", "")).upper(),
                    order_id,
                )
        if manual_sell_fill and manual_sell_fill[0]:
            symbol, trigger_order_id = manual_sell_fill
            newly_active = self._activate_execution_incident(
                symbol,
                "检测到未由 QuantPilot 跟踪的卖出成交",
                trigger_order_id=trigger_order_id or None,
                details={"source": "trading_stream"},
            )
            if newly_active:
                await self.log(
                    "critical",
                    "risk",
                    f"{symbol} 检测到外部卖出，已暂停引擎并进入执行安全隔离",
                )
            await self._contain_active_execution_incidents()
        await websocket_manager.broadcast(self.user_id, "trade_update", payload)

    @staticmethod
    def _order_side(order: dict[str, Any]) -> str:
        value = str(order.get("side", "")).lower()
        return "sell" if "sell" in value else "buy"

    @staticmethod
    def _is_ambiguous_order_error(exc: Exception) -> bool:
        return isinstance(exc, AlpacaAmbiguousOrderError)

    def _apply_fill_delta(
        self, db, record: OrderRecord, order: dict[str, Any]
    ) -> None:
        new_filled_qty = as_float(order.get("filled_qty"))
        if new_filled_qty <= record.filled_qty:
            return
        delta = new_filled_qty - record.filled_qty
        record.filled_qty = new_filled_qty
        if not record.strategy_id:
            return
        position = db.scalar(
            select(StrategyPosition).where(
                StrategyPosition.user_id == self.user_id,
                StrategyPosition.strategy_id == record.strategy_id,
                StrategyPosition.symbol == record.symbol,
            )
        )
        if position is None:
            position = StrategyPosition(
                user_id=self.user_id,
                strategy_id=record.strategy_id,
                symbol=record.symbol,
                qty=0,
            )
            db.add(position)
        if record.side == "buy":
            position.qty += delta
        else:
            position.qty = max(0.0, position.qty - delta)

    def _owned_qty(self, strategy_id: str, symbol: str) -> float:
        with SessionLocal() as db:
            position = db.scalar(
                select(StrategyPosition).where(
                    StrategyPosition.user_id == self.user_id,
                    StrategyPosition.strategy_id == strategy_id,
                    StrategyPosition.symbol == symbol,
                )
            )
            return max(0.0, position.qty if position else 0.0)

    @staticmethod
    def _order_is_terminal(order: dict[str, Any]) -> bool:
        return str(order.get("status") or "").lower() in TERMINAL_ORDER_STATUSES

    @staticmethod
    def _position_qty(positions: list[dict[str, Any]], symbol: str) -> float:
        normalized = symbol.upper()
        return as_float(
            next(
                (
                    position.get("qty")
                    for position in positions
                    if str(position.get("symbol", "")).upper() == normalized
                ),
                0,
            )
        )

    async def _fresh_positions(self) -> list[dict[str, Any]]:
        reader = getattr(self.alpaca, "get_positions_fresh", None)
        if reader is None:
            reader = self.alpaca.get_positions
        return await asyncio.to_thread(reader)

    async def _fresh_open_orders(self) -> list[dict[str, Any]]:
        reader = getattr(self.alpaca, "get_open_orders_fresh", None)
        if reader is not None:
            return self._flatten_orders(await asyncio.to_thread(reader))
        return self._flatten_orders(
            await asyncio.to_thread(self.alpaca.get_orders, "open")
        )

    @classmethod
    def _reserved_sell_qty(
        cls,
        orders: list[dict[str, Any]],
        symbol: str,
        *,
        exclude_client_order_id: str = "",
    ) -> float:
        groups: dict[str, float] = {}
        normalized = symbol.upper()
        for order in cls._flatten_orders(orders):
            if cls._order_is_terminal(order) or cls._order_side(order) != "sell":
                continue
            if str(order.get("symbol", "")).upper() != normalized:
                continue
            if exclude_client_order_id and str(order.get("client_order_id") or "") == exclude_client_order_id:
                continue
            remaining = max(
                0.0,
                as_float(order.get("qty")) - as_float(order.get("filled_qty")),
            )
            if remaining <= 0:
                continue
            parent_id = str(order.get("parent_order_id") or "")
            order_identity = str(
                order.get("id") or order.get("client_order_id") or id(order)
            )
            group = f"parent:{parent_id}" if parent_id else f"single:{order_identity}"
            groups[group] = max(groups.get(group, 0.0), remaining)
        return sum(groups.values())

    async def _verify_long_only_sell_capacity(
        self,
        symbol: str,
        intended_qty: float,
        client_order_id: str,
    ) -> None:
        try:
            positions = await self._fresh_positions()
            open_orders = await self._fresh_open_orders()
        except Exception as exc:
            raise AlpacaTransientError(
                "无法取得最新持仓和开放订单，已阻止卖单提交"
            ) from exc
        actual_qty = self._position_qty(positions, symbol)
        reserved_qty = self._reserved_sell_qty(
            open_orders,
            symbol,
            exclude_client_order_id=client_order_id,
        )
        if actual_qty < -1e-6:
            reason = "Alpaca 账户已出现空头持仓，禁止继续卖出"
        elif intended_qty <= 0:
            reason = "待提交卖单数量无效"
        elif reserved_qty + intended_qty > max(actual_qty, 0.0) + 1e-6:
            reason = "卖单数量与其他开放卖单合计超过实际多头持仓"
        else:
            return
        self._activate_execution_incident(
            symbol,
            reason,
            details={
                "source": "pre_submit_sell_capacity",
                "actual_qty": actual_qty,
                "reserved_sell_qty": reserved_qty,
                "intended_qty": intended_qty,
            },
        )
        raise LongOnlyInvariantError(symbol, reason)

    def _active_incident_symbols(self) -> list[str]:
        with SessionLocal() as db:
            return list(
                db.scalars(
                    select(ExecutionIncident.symbol).where(
                        ExecutionIncident.user_id == self.user_id,
                        ExecutionIncident.status == "active",
                    )
                ).all()
            )

    def _activate_execution_incident(
        self,
        symbol: str,
        reason: str,
        *,
        trigger_order_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        normalized = symbol.upper()
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            incident = db.scalar(
                select(ExecutionIncident).where(
                    ExecutionIncident.user_id == self.user_id,
                    ExecutionIncident.symbol == normalized,
                )
            )
            newly_active = incident is None or incident.status != "active"
            if incident is None:
                incident = ExecutionIncident(
                    user_id=self.user_id,
                    symbol=normalized,
                    status="active",
                    reason=reason,
                    trigger_order_id=trigger_order_id,
                    details=details or {},
                )
                db.add(incident)
            else:
                incident.status = "active"
                incident.reason = reason
                incident.trigger_order_id = trigger_order_id or incident.trigger_order_id
                incident.details = {**(incident.details or {}), **(details or {})}
                incident.resolved_at = None
                incident.updated_at = now
            db.query(StrategyPosition).filter(
                StrategyPosition.user_id == self.user_id,
                StrategyPosition.symbol == normalized,
            ).update({StrategyPosition.qty: 0.0})
            state = db.scalar(
                select(EngineState).where(EngineState.user_id == self.user_id)
            )
            if state is not None:
                state.status = "paused"
                state.reason = f"执行安全隔离：{normalized} {reason}"
            db.commit()
        self._abort_new_orders_for_cycle = True
        return newly_active

    def _quantpilot_order_identity(
        self, symbol: str
    ) -> tuple[set[str], set[str]]:
        with SessionLocal() as db:
            records = db.scalars(
                select(OrderRecord).where(
                    OrderRecord.user_id == self.user_id,
                    OrderRecord.symbol == symbol.upper(),
                    OrderRecord.strategy_id.is_not(None),
                )
            ).all()
        return (
            {str(record.id) for record in records},
            {str(record.client_order_id) for record in records},
        )

    def _is_quantpilot_order(
        self,
        order: dict[str, Any],
        local_order_ids: set[str],
        local_client_ids: set[str],
    ) -> bool:
        order_id = str(order.get("id") or "")
        parent_id = str(order.get("parent_order_id") or "")
        client_order_id = str(order.get("client_order_id") or "")
        return bool(
            client_order_id.startswith("qp-")
            or order_id in local_order_ids
            or parent_id in local_order_ids
            or client_order_id in local_client_ids
        )

    def _audit_long_only_invariants(
        self,
        positions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
    ) -> dict[str, str]:
        with SessionLocal() as db:
            owned_rows = db.scalars(
                select(StrategyPosition).where(
                    StrategyPosition.user_id == self.user_id,
                    StrategyPosition.qty > 0,
                )
            ).all()
        owned_by_symbol: dict[str, float] = defaultdict(float)
        for row in owned_rows:
            owned_by_symbol[row.symbol.upper()] += row.qty
        symbols = set(owned_by_symbol)
        symbols.update(str(position.get("symbol", "")).upper() for position in positions)
        symbols.update(str(order.get("symbol", "")).upper() for order in open_orders)
        violations: dict[str, str] = {}
        for symbol in sorted(symbol for symbol in symbols if symbol):
            actual_qty = self._position_qty(positions, symbol)
            owned_qty = owned_by_symbol.get(symbol, 0.0)
            reserved_sell_qty = self._reserved_sell_qty(open_orders, symbol)
            if actual_qty < -1e-6:
                violations[symbol] = "Alpaca 账户出现空头持仓"
            elif owned_qty > max(actual_qty, 0.0) + 1e-6:
                violations[symbol] = "策略归属数量超过 Alpaca 实际多头持仓"
            elif reserved_sell_qty > max(actual_qty, 0.0) + 1e-6:
                violations[symbol] = "开放卖单数量超过 Alpaca 实际多头持仓"
        return violations

    async def _contain_active_execution_incidents(self) -> bool:
        symbols = self._active_incident_symbols()
        if not symbols or not self.alpaca.configured:
            return not symbols
        contained: list[str] = []
        async with self._order_submission_lock:
            try:
                open_orders = await self._fresh_open_orders()
                await self._fresh_positions()
            except Exception:
                return False
            for symbol in symbols:
                local_order_ids, local_client_ids = self._quantpilot_order_identity(
                    symbol
                )
                for order in open_orders:
                    if str(order.get("symbol", "")).upper() != symbol:
                        continue
                    if not self._is_quantpilot_order(
                        order, local_order_ids, local_client_ids
                    ):
                        continue
                    order_id = str(order.get("id") or "")
                    if not order_id or self._order_is_terminal(order):
                        continue
                    try:
                        await asyncio.to_thread(self.alpaca.cancel_order, order_id)
                    except Exception:
                        # A fresh snapshot below resolves response-loss ambiguity.
                        pass
            try:
                fresh_open_orders = await self._fresh_open_orders()
                fresh_positions = await self._fresh_positions()
            except Exception:
                return False
            with SessionLocal() as db:
                for symbol in symbols:
                    open_sell_qty = self._reserved_sell_qty(
                        fresh_open_orders, symbol
                    )
                    actual_qty = self._position_qty(fresh_positions, symbol)
                    local_order_ids, local_client_ids = (
                        self._quantpilot_order_identity(symbol)
                    )
                    qp_open = any(
                        str(order.get("symbol", "")).upper() == symbol
                        and not self._order_is_terminal(order)
                        and self._is_quantpilot_order(
                            order, local_order_ids, local_client_ids
                        )
                        for order in fresh_open_orders
                    )
                    incident = db.scalar(
                        select(ExecutionIncident).where(
                            ExecutionIncident.user_id == self.user_id,
                            ExecutionIncident.symbol == symbol,
                        )
                    )
                    if (
                        incident is not None
                        and actual_qty >= -1e-6
                        and open_sell_qty <= 1e-6
                        and not qp_open
                    ):
                        incident.status = "contained"
                        incident.resolved_at = datetime.now(timezone.utc)
                        incident.details = {
                            **(incident.details or {}),
                            "resolution": "fresh_snapshot_safe",
                        }
                        contained.append(symbol)
                db.commit()
        for symbol in contained:
            await self.log(
                "warning",
                "risk",
                f"{symbol} 执行安全隔离已解除，可由用户确认后重新启动引擎",
            )
        return not self._active_incident_symbols()

    async def _cancel_strategy_symbol_orders(
        self,
        strategy_id: str,
        symbol: str,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> None:
        with SessionLocal() as db:
            own_order_ids = set(
                db.scalars(
                    select(OrderRecord.id).where(
                        OrderRecord.user_id == self.user_id,
                        OrderRecord.strategy_id == strategy_id,
                        OrderRecord.symbol == symbol,
                    )
                ).all()
            )
        if open_orders is None:
            open_orders = await asyncio.to_thread(self.alpaca.get_orders, "open")
        cancelled_ids: set[str] = set()
        failed_ids: list[str] = []
        for order in list(open_orders):
            order_id = str(order.get("id", ""))
            parent_id = str(order.get("parent_order_id") or "")
            if order_id in own_order_ids or parent_id in own_order_ids:
                try:
                    await asyncio.to_thread(self.alpaca.cancel_order, order_id)
                    cancelled_ids.add(order_id)
                except Exception as exc:
                    failed_ids.append(order_id)
                    logger.warning("Unable to cancel strategy order %s: %s", order_id, exc)
        if cancelled_ids:
            open_orders[:] = [
                order
                for order in open_orders
                if str(order.get("id", "")) not in cancelled_ids
            ]
        if failed_ids:
            # A market exit submitted while a bracket/trailing order remains open
            # can oversell the position.  Even an ambiguous cancel is treated as
            # incomplete; the next cycle first refreshes Alpaca's open orders and
            # retries only the still-open orders.
            raise StrategyOrderCancellationError(failed_ids)

    async def evaluate_active_strategies(self, *, force: bool = False) -> None:
        now = datetime.now(timezone.utc)
        if not force and now < self._next_strategy_evaluation:
            return
        # Reserve the next slot before performing network I/O. A slow cycle must
        # never cause another evaluation to overlap or immediately repeat.
        self._next_strategy_evaluation = now + timedelta(
            seconds=EVALUATION_INTERVAL_SECONDS
        )
        with SessionLocal() as db:
            strategies = db.scalars(
                select(Strategy).where(Strategy.enabled.is_(True), Strategy.is_template.is_(False))
                .where(Strategy.owner_user_id == self.user_id)
                .order_by(Strategy.created_at, Strategy.id)
            ).all()
        prepared = self._prepare_strategies(strategies)
        if not prepared:
            await self._resume_pending_submissions_cycle(now)
            return
        await self._evaluate_prepared_strategies(prepared, now)

    async def evaluate_strategy(self, strategy_id: str) -> None:
        """Evaluate one strategy using the same snapshot path as the batch engine."""
        with SessionLocal() as db:
            strategy = db.scalar(
                select(Strategy).where(
                    Strategy.id == strategy_id, Strategy.owner_user_id == self.user_id
                )
            )
            if strategy is None or not strategy.enabled:
                return
        prepared = self._prepare_strategies([strategy])
        if prepared:
            await self._evaluate_prepared_strategies(
                prepared, datetime.now(timezone.utc)
            )

    def _prepare_strategies(self, strategies: list[Strategy]) -> list[PreparedStrategy]:
        prepared: list[PreparedStrategy] = []
        for strategy in strategies:
            try:
                definition = RuleDefinition.model_validate(strategy.definition)
            except Exception as exc:
                logger.warning("Invalid strategy %s skipped: %s", strategy.id, exc)
                continue
            prepared.append(PreparedStrategy(strategy=strategy, definition=definition))
        return prepared

    def _pending_submission_ids(self) -> list[str]:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            signals = db.scalars(
                select(Signal)
                .where(
                    Signal.user_id == self.user_id,
                    Signal.status == "pending_submission",
                )
                .order_by(Signal.created_at, Signal.id)
            ).all()
        result: list[str] = []
        for signal in signals:
            next_attempt_at = self._payload_datetime(
                (signal.payload or {}).get("next_attempt_at")
            )
            if next_attempt_at is None or next_attempt_at <= now:
                result.append(signal.id)
        return result

    @staticmethod
    def _payload_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _log_pending_connection_wait(self, count: int, state: str) -> None:
        now = datetime.now(timezone.utc)
        if now - self._last_pending_wait_log < timedelta(
            seconds=CONNECTION_ERROR_LOG_INTERVAL_SECONDS
        ):
            return
        self._last_pending_wait_log = now
        await self.log(
            "warning",
            "order",
            f"{count} 个待提交订单意图正在等待 Alpaca 连接恢复",
            {"pending_count": count, "connection_state": state},
        )

    async def _resume_pending_submissions_cycle(
        self, cycle_started_at: datetime
    ) -> None:
        """Drain persisted order intents even if their strategy is now unavailable.

        This path performs no Alpaca request when the outbox is empty.  A paused
        engine never reaches it because the run loop only evaluates while running.
        """
        if not self._pending_submission_ids():
            return
        try:
            clock = await asyncio.to_thread(self.alpaca.get_clock)
            if not bool(clock.get("is_open")):
                self._next_strategy_evaluation = cycle_started_at + timedelta(
                    seconds=CLOSED_MARKET_INTERVAL_SECONDS
                )
                if self._snapshot_connection_ready(
                    self._alpaca_connection_status()
                ):
                    await self._record_connection_recovery()
                return
            account = await asyncio.to_thread(self.alpaca.get_account)
            await self._refresh_risk_equity_snapshot(account)
            positions = await asyncio.to_thread(self.alpaca.get_positions)
            raw_open_orders = await asyncio.to_thread(self.alpaca.get_orders, "open")
            open_orders = self._enrich_open_orders(raw_open_orders)
            connection = self._alpaca_connection_status()
            if not self._snapshot_connection_ready(connection):
                raise RuntimeError(
                    str(connection.get("message") or "Alpaca 连接尚未完全恢复")
                )
            await self._resume_pending_submissions(
                open_orders,
                account=account,
                positions=positions,
                clock=clock,
            )
        except Exception as exc:
            await self._record_connection_failure(exc)
            return
        await self._record_connection_recovery()

    async def _evaluate_prepared_strategies(
        self, prepared: list[PreparedStrategy], cycle_started_at: datetime
    ) -> None:
        self._abort_new_orders_for_cycle = False
        try:
            clock = await asyncio.to_thread(self.alpaca.get_clock)
            if not bool(clock.get("is_open")):
                self._next_strategy_evaluation = cycle_started_at + timedelta(
                    seconds=CLOSED_MARKET_INTERVAL_SECONDS
                )
                if self._snapshot_connection_ready(
                    self._alpaca_connection_status()
                ):
                    await self._record_connection_recovery()
                return

            now_et = datetime.now(ET)
            eligible = [
                item
                for item in prepared
                if now_et.weekday() in item.definition.schedule.weekdays
            ]
            if not eligible:
                await self._record_connection_recovery()
                return

            # Trading calls share one requests.Session/lock.  Run them in order so
            # the first exhausted retry budget aborts the cycle immediately instead
            # of letting two queued calls extend the outage window.
            account = await asyncio.to_thread(self.alpaca.get_account)
            await self._refresh_risk_equity_snapshot(account)
            positions = await asyncio.to_thread(self.alpaca.get_positions)
            raw_open_orders = await asyncio.to_thread(self.alpaca.get_orders, "open")
            open_orders = self._enrich_open_orders(raw_open_orders)
            bars_by_timeframe = await self._fetch_grouped_bars(eligible)
            connection = self._alpaca_connection_status()
            if not self._snapshot_connection_ready(connection):
                raise RuntimeError(
                    str(connection.get("message") or "Alpaca 连接尚未完全恢复")
                )
            await self._resume_pending_submissions(
                open_orders,
                account=account,
                positions=positions,
                clock=clock,
                reference_bars=self._latest_reference_bars(
                    bars_by_timeframe, now_et
                ),
            )
            if not self._accepting_new_orders(self._alpaca_connection_status()):
                raise RuntimeError("Alpaca 下单通道尚未恢复，本轮不生成新信号")
        except Exception as exc:
            await self._record_connection_failure(exc)
            return

        await self._record_connection_recovery()
        snapshot = EvaluationSnapshot(
            clock=clock,
            account=account,
            positions=positions,
            position_map={
                str(position.get("symbol", "")).upper(): position
                for position in positions
            },
            open_orders=open_orders,
            bars_by_timeframe=bars_by_timeframe,
        )
        for item in eligible:
            try:
                await self._evaluate_prepared_strategy(item, snapshot, now_et)
            except Exception as exc:
                await self.log(
                    "error",
                    "strategy",
                    f"策略 {item.strategy.name} 执行失败: {exc}",
                )

    def _alpaca_connection_status(self) -> dict[str, Any]:
        status = getattr(self.alpaca, "connection_status", None)
        if status is None:
            return {"state": "connected", "connected": True}
        return dict(status())

    @staticmethod
    def _snapshot_connection_ready(connection: dict[str, Any]) -> bool:
        state = str(connection.get("state") or "unknown")
        if state == "connected":
            return True
        if state != "degraded":
            return False
        failures = (connection.get("health") or {}).get("failed_operations") or []
        critical = {
            ("trading", "clock"),
            ("trading", "account"),
            ("trading", "positions"),
            ("trading", "orders:open"),
            ("trading", "calendar"),
            ("data", "stock_bars"),
        }
        return not any(
            (str(item.get("channel")), str(item.get("operation"))) in critical
            for item in failures
        )

    @classmethod
    def _accepting_new_orders(cls, connection: dict[str, Any]) -> bool:
        if not cls._snapshot_connection_ready(connection):
            return False
        failures = (connection.get("health") or {}).get("failed_operations") or []
        return not any(
            str(item.get("channel")) == "trading"
            and str(item.get("operation")) == "submit_order"
            for item in failures
        )

    @staticmethod
    def _reconciliation_connection_ready(connection: dict[str, Any]) -> bool:
        if not bool(connection.get("configured", True)):
            return False
        trading = (connection.get("health") or {}).get("trading") or {}
        if trading:
            return str(trading.get("state") or "closed") != "open"
        return str(connection.get("state") or "unknown") != "circuit_open"

    async def _fetch_grouped_bars(
        self, prepared: list[PreparedStrategy]
    ) -> dict[str, dict[str, pd.DataFrame]]:
        symbols_by_timeframe: dict[str, set[str]] = defaultdict(set)
        bars_by_timeframe: dict[str, int] = {}
        for item in prepared:
            timeframe = item.definition.timeframe
            symbols_by_timeframe[timeframe].update(item.definition.symbols)
            bars_by_timeframe[timeframe] = max(
                bars_by_timeframe.get(timeframe, 0),
                max(
                    item.definition.warmup_bars,
                    self._definition_required_bars(item.definition),
                )
                + 20,
            )

        result: dict[str, dict[str, pd.DataFrame]] = {}
        for timeframe in sorted(symbols_by_timeframe):
            result[timeframe] = await asyncio.to_thread(
                self.alpaca.recent_bars,
                sorted(symbols_by_timeframe[timeframe]),
                timeframe,
                bars_by_timeframe[timeframe],
            )
        return result

    @staticmethod
    def _latest_reference_bars(
        bars_by_timeframe: dict[str, dict[str, pd.DataFrame]],
        now_et: datetime,
    ) -> dict[tuple[str, str], tuple[datetime, float, pd.DataFrame]]:
        latest: dict[tuple[str, str], tuple[datetime, float, pd.DataFrame]] = {}
        for timeframe, frames in bars_by_timeframe.items():
            for symbol, frame in frames.items():
                if frame is None or frame.empty or "close" not in frame:
                    continue
                completed = TradingEngine._completed_bars(frame, timeframe, now_et)
                if completed.empty:
                    continue
                timestamp = pd.Timestamp(completed.index[-1]).to_pydatetime()
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                latest[(timeframe, symbol.upper())] = (
                    timestamp.astimezone(timezone.utc),
                    float(completed.iloc[-1]["close"]),
                    completed,
                )
        return latest

    async def _evaluate_prepared_strategy(
        self,
        item: PreparedStrategy,
        snapshot: EvaluationSnapshot,
        now_et: datetime,
    ) -> None:
        strategy = item.strategy
        definition = item.definition
        bars_by_symbol = snapshot.bars_by_timeframe.get(definition.timeframe, {})
        for symbol in definition.symbols:
            if self._abort_new_orders_for_cycle or not self._accepting_new_orders(
                self._alpaca_connection_status()
            ):
                return
            frame = bars_by_symbol.get(symbol)
            required_bars = max(
                3,
                definition.warmup_bars,
                self._definition_required_bars(definition),
            )
            if frame is None or frame.empty:
                await self._log_insufficient_bars(
                    strategy, symbol, definition.timeframe, 0, required_bars
                )
                continue
            frame = self._completed_bars(frame, definition.timeframe, now_et)
            if frame.empty:
                await self._log_insufficient_bars(
                    strategy, symbol, definition.timeframe, 0, required_bars
                )
                continue
            if len(frame) < required_bars:
                await self._log_insufficient_bars(
                    strategy, symbol, definition.timeframe, len(frame), required_bars
                )
                # Do not consume this bar.  A later REST refill can provide the
                # missing history and safely evaluate the same completed bar.
                continue
            self._last_insufficient_data_log.pop((strategy.id, symbol), None)
            latest_timestamp = frame.index[-1]
            latest_key = latest_timestamp.isoformat()
            key = (strategy.id, symbol)
            if self._last_evaluated.get(key) == latest_key:
                continue
            if self._has_pending_reconciliation(strategy.id, symbol):
                # The previous submit may already exist remotely. Leave this bar
                # unconsumed and wait for reconciliation instead of risking a duplicate.
                continue

            try:
                self.cache_bars(symbol, definition.timeframe, frame.tail(5))
                entry = evaluate_latest(frame, definition.entry)
                exit_ = evaluate_latest(frame, definition.exit)
                account_position_qty = as_float(
                    snapshot.position_map.get(symbol, {}).get("qty")
                )
                owned_qty = min(
                    self._owned_qty(strategy.id, symbol), account_position_qty
                )
                has_position = owned_qty > 0

                if has_position and exit_.matched:
                    await self._create_exit_signal(
                        strategy,
                        definition,
                        symbol,
                        frame,
                        exit_,
                        owned_qty,
                        snapshot.open_orders,
                        snapshot.positions,
                    )
                else:
                    can_pyramid = (
                        definition.position.allow_pyramiding
                        and await self._cooldown_passed(
                            strategy.id, symbol, definition
                        )
                    )
                    if entry.matched and (not has_position or can_pyramid):
                        await self._create_entry_signal(
                            strategy,
                            definition,
                            symbol,
                            frame,
                            entry,
                            snapshot.account,
                            snapshot.positions,
                            snapshot.open_orders,
                            snapshot.clock,
                        )
            except Exception as exc:
                await self.log(
                    "error",
                    "strategy",
                    f"{strategy.name}/{symbol} 处理失败: {exc}",
                )
                continue

            # Only consume the bar after all local evaluation and order handling
            # completed. Snapshot/data failures therefore leave it retryable.
            self._last_evaluated[key] = latest_key

    async def _log_insufficient_bars(
        self,
        strategy: Strategy,
        symbol: str,
        timeframe: str,
        available: int,
        required: int,
    ) -> None:
        key = (strategy.id, symbol)
        now = datetime.now(timezone.utc)
        last_logged = self._last_insufficient_data_log.get(key)
        if (
            last_logged is not None
            and now - last_logged
            < timedelta(seconds=INSUFFICIENT_DATA_LOG_INTERVAL_SECONDS)
        ):
            return
        self._last_insufficient_data_log[key] = now
        await self.log(
            "warning",
            "market_data",
            f"{strategy.name}/{symbol} 历史K线不足，暂不评估："
            f"{available}/{required} 根 {timeframe} 已完成K线",
            {
                "strategy_id": strategy.id,
                "symbol": symbol,
                "timeframe": timeframe,
                "available_bars": available,
                "required_bars": required,
            },
        )

    @staticmethod
    def _completed_bars(
        frame: pd.DataFrame, timeframe: str, now_et: datetime
    ) -> pd.DataFrame:
        """Return only bars whose full interval has elapsed.

        Alpaca timestamps intraday bars at their start.  Evaluating the latest
        15-minute bar at 10:07, for example, leaks eight minutes of future data.
        Daily bars instead complete at the regular New York session close.
        """
        if frame.empty:
            return frame
        index = pd.DatetimeIndex(frame.index)
        if index.tz is None:
            index = index.tz_localize(timezone.utc)
        session_closes = frame.attrs.get("session_closes", {})

        def session_close(session_date) -> datetime:
            raw = session_closes.get(session_date.isoformat())
            if raw:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=ET)
                return parsed.astimezone(ET)
            return datetime.combine(
                session_date, datetime_time(16, 0), tzinfo=ET
            )

        if timeframe == "1Day":
            local_index = index.tz_convert(ET)
            mask = [
                now_et >= session_close(timestamp.date())
                for timestamp in local_index
            ]
        elif timeframe == "1Hour":
            local_index = index.tz_convert(ET)
            now_utc = pd.Timestamp(now_et).tz_convert(timezone.utc)
            mask = [
                pd.Timestamp(
                    min(
                        timestamp.to_pydatetime() + timedelta(hours=1),
                        session_close(timestamp.date()),
                    )
                ).tz_convert(timezone.utc)
                <= now_utc
                for timestamp in local_index
            ]
        else:
            seconds = TIMEFRAME_SECONDS[timeframe]
            now_utc = pd.Timestamp(now_et).tz_convert(timezone.utc)
            mask = index + pd.to_timedelta(seconds, unit="s") <= now_utc
        return frame.loc[mask]

    @classmethod
    def _definition_required_bars(cls, definition: RuleDefinition) -> int:
        """Calculate the minimum history needed by every rule operand."""

        def visit(node: Any) -> int:
            if getattr(node, "type", None) == "condition":
                required = max(
                    cls._operand_required_bars(node.left),
                    cls._operand_required_bars(node.right),
                )
                if node.operator in {"crosses_above", "crosses_below"}:
                    required += 1
                return required
            return max((visit(child) for child in node.children), default=1)

        required = max(visit(definition.entry), visit(definition.exit))
        for guard in (definition.order.stop_loss, definition.order.take_profit):
            if guard is not None and guard.mode == "atr":
                required = max(required, guard.atr_period)
        return required

    @staticmethod
    def _operand_required_bars(operand: Any) -> int:
        offset = max(0, int(operand.offset or 0))
        if operand.kind != "indicator":
            return 1 + offset
        params = operand.params or {}
        name = str(operand.indicator)

        def positive_int(key: str, default: int) -> int:
            try:
                return max(1, int(params.get(key, default)))
            except (TypeError, ValueError):
                return default

        if name in {"SMA", "EMA", "BOLLINGER", "ATR", "VOLUME_SMA", "DEVIATION"}:
            required = positive_int("period", 20 if name != "ATR" else 14)
        elif name == "RSI":
            required = positive_int("period", 14) + 1
        elif name == "MACD":
            slow = positive_int("slow", 26)
            signal = positive_int("signal", 9)
            required = slow + signal
        elif name == "ROC":
            required = positive_int("period", 12) + 1
        elif name in {"HIGHEST", "LOWEST"}:
            required = positive_int("period", 20)
            if bool(params.get("exclude_current", True)):
                required += 1
        else:
            required = 1
        return required + offset

    async def _record_connection_failure(self, exc: Exception) -> None:
        now = datetime.now(timezone.utc)
        should_log = (
            not self._connection_failure_active
            or now - self._last_connection_error_log
            >= timedelta(seconds=CONNECTION_ERROR_LOG_INTERVAL_SECONDS)
        )
        self._connection_failure_active = True
        if not should_log:
            return
        self._last_connection_error_log = now
        await self.log(
            "error",
            "connection",
            f"Alpaca 连接异常，本轮全部策略已安全跳过: {exc}",
            {"error_type": type(exc).__name__},
        )

    async def _record_connection_recovery(self) -> None:
        if not self._connection_failure_active:
            return
        self._connection_failure_active = False
        await self.log("info", "connection", "Alpaca 连接已恢复，策略评估自动继续")

    def _enrich_open_orders(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        order_ids = [str(order.get("id", "")) for order in orders if order.get("id")]
        enriched = [dict(order) for order in orders]
        with SessionLocal() as db:
            records = (
                db.scalars(
                    select(OrderRecord).where(
                        OrderRecord.user_id == self.user_id,
                        OrderRecord.id.in_(order_ids),
                    )
                ).all()
                if order_ids
                else []
            )
            pending_signals = db.scalars(
                select(Signal).where(
                    Signal.user_id == self.user_id,
                    Signal.status.in_(UNRESOLVED_ORDER_STATUSES),
                )
            ).all()
        notional_by_id = {
            record.id: record.notional for record in records if record.notional
        }
        for order in enriched:
            order["_estimated_notional"] = notional_by_id.get(
                str(order.get("id", ""))
            )

        existing_client_ids = {
            str(order.get("client_order_id", ""))
            for order in enriched
            if order.get("client_order_id")
        }
        for signal in pending_signals:
            payload = signal.payload or {}
            client_order_id = str(payload.get("client_order_id") or "")
            if (
                signal.action != "buy"
                or not client_order_id
                or client_order_id in existing_client_ids
            ):
                continue
            enriched.append(
                {
                    "id": f"pending:{signal.id}",
                    "client_order_id": client_order_id,
                    "symbol": signal.symbol,
                    "side": "buy",
                    "qty": payload.get("qty"),
                    "status": signal.status,
                    "_estimated_notional": payload.get("notional"),
                }
            )
        return enriched

    def _has_pending_reconciliation(self, strategy_id: str, symbol: str) -> bool:
        """Return whether any unresolved order intent blocks another signal."""
        with SessionLocal() as db:
            return (
                db.scalar(
                    select(Signal.id).where(
                        Signal.user_id == self.user_id,
                        Signal.strategy_id == strategy_id,
                        Signal.symbol == symbol,
                        Signal.status.in_(UNRESOLVED_ORDER_STATUSES),
                    )
                )
                is not None
            )

    async def _cooldown_passed(self, strategy_id: str, symbol: str, definition: RuleDefinition) -> bool:
        with SessionLocal() as db:
            last = db.scalar(
                select(Signal)
                .where(
                    Signal.strategy_id == strategy_id,
                    Signal.user_id == self.user_id,
                    Signal.symbol == symbol,
                    Signal.action == "buy",
                )
                .order_by(Signal.bar_timestamp.desc())
            )
        if last is None:
            return True
        seconds = TIMEFRAME_SECONDS[definition.timeframe] * definition.risk.cooldown_bars
        timestamp = last.bar_timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - timestamp >= timedelta(seconds=seconds)

    async def _create_entry_signal(
        self,
        strategy: Strategy,
        definition: RuleDefinition,
        symbol: str,
        frame: pd.DataFrame,
        result,
        account: dict[str, Any],
        positions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
        clock: dict[str, Any],
    ) -> None:
        price = float(frame.iloc[-1]["close"])
        equity = as_float(account.get("equity"))
        config = definition.position
        atr_value = latest_atr(frame, definition.order.stop_loss.atr_period if definition.order.stop_loss else 14)
        stop_price = self._guard_price(price, definition.order.stop_loss, atr_value, False)
        take_price = self._guard_price(price, definition.order.take_profit, atr_value, True)
        if config.mode == "fixed_qty":
            qty = config.value
            desired_notional = qty * price
        elif config.mode == "fixed_notional":
            desired_notional = config.value
            qty = desired_notional / price
        elif config.mode == "risk_based":
            if stop_price is None or stop_price >= price:
                await self.log("warning", "risk", f"{symbol} 无法计算风险仓位")
                return
            risk_dollars = equity * config.value / 100
            qty = risk_dollars / (price - stop_price)
            desired_notional = qty * price
        else:
            desired_notional = equity * config.value / 100
            qty = desired_notional / price

        try:
            asset = await asyncio.to_thread(self.alpaca.get_asset, symbol)
        except Exception:
            # Tradability is mandatory for every entry. Stop fan-out for this
            # cycle, but allow the next cycle to make one controlled retry.
            self._abort_new_orders_for_cycle = True
            raise
        with SessionLocal() as db:
            risk_settings = db.scalar(
                select(RiskSettings).where(RiskSettings.user_id == self.user_id)
            )
            state = db.scalar(select(EngineState).where(EngineState.user_id == self.user_id))
            max_age = max(risk_settings.stale_data_seconds, TIMEFRAME_SECONDS[definition.timeframe] * 3)
            decision = self.risk.check_entry(
                symbol=symbol,
                desired_notional=desired_notional,
                account=account,
                positions=positions,
                asset=asset,
                clock=clock,
                bar_timestamp=frame.index[-1].to_pydatetime(),
                max_data_age_seconds=max_age,
                settings=risk_settings,
                state=state,
                strategy_max_symbol_pct=definition.risk.max_symbol_pct,
                strategy_max_positions=definition.risk.max_positions,
                open_orders=open_orders,
                reference_price=price,
            )
            db.commit()
        if not decision.allowed:
            await self.log("warning", "risk", f"{strategy.name}/{symbol}: {decision.reason}")
            if decision.halt_engine:
                await self.pause(decision.reason, cancel_orders=True)
            return

        timestamp = frame.index[-1].to_pydatetime()
        client_id = self._client_order_id(strategy.id, symbol, timestamp, "buy")
        limit_price = price * (1 - definition.order.limit_offset_bps / 10_000)
        signal = await self._persist_order_intent(
            strategy,
            symbol,
            timestamp,
            "buy",
            price,
            "；".join(result.labels) or "入场条件成立",
            {
                "intent_version": 1,
                "strategy_version": strategy.version,
                "client_order_id": client_id,
                "side": "buy",
                "qty": qty,
                "notional": desired_notional,
                "original_qty": qty,
                "original_notional": desired_notional,
                "order_type": definition.order.type,
                "time_in_force": definition.order.time_in_force,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "take_price": take_price,
                "timeframe": definition.timeframe,
                "trailing_stop": (
                    definition.order.trailing_stop.model_dump()
                    if definition.order.trailing_stop is not None
                    else None
                ),
            },
        )
        if signal is None:
            return
        await self._submit_pending_signal(
            signal.id,
            open_orders,
            account=account,
            positions=positions,
            clock=clock,
            reference_bars={
                (definition.timeframe, symbol.upper()): (timestamp, price, frame)
            },
            recover_existing=False,
        )

    async def _create_exit_signal(
        self,
        strategy,
        definition,
        symbol,
        frame,
        result,
        owned_qty: float,
        open_orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
    ) -> None:
        price = float(frame.iloc[-1]["close"])
        timestamp = frame.index[-1].to_pydatetime()
        client_id = self._client_order_id(strategy.id, symbol, timestamp, "sell")
        signal = await self._persist_order_intent(
            strategy,
            symbol,
            timestamp,
            "sell",
            price,
            "；".join(result.labels) or "离场条件成立",
            {
                "intent_version": 1,
                "strategy_version": strategy.version,
                "client_order_id": client_id,
                "side": "sell",
                "qty": owned_qty,
                "notional": None,
                "order_type": "market",
                "time_in_force": "day",
                "cancel_strategy_orders": True,
                "timeframe": definition.timeframe,
            },
        )
        if signal is None:
            return
        await self._submit_pending_signal(
            signal.id,
            open_orders,
            positions=positions,
            recover_existing=False,
        )

    def _client_order_id(
        self,
        strategy_id: str,
        symbol: str,
        timestamp: datetime,
        action: str,
    ) -> str:
        material = (
            f"{self.user_id}:{strategy_id}:{symbol.upper()}:"
            f"{timestamp.isoformat()}:{action}"
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:40]
        side = "t" if action == "trail" else ("s" if action == "sell" else "b")
        return f"qp-{side}-{digest}"

    async def _persist_order_intent(
        self,
        strategy: Strategy,
        symbol: str,
        timestamp: datetime,
        action: str,
        price: float,
        reason: str,
        payload: dict[str, Any],
    ) -> Signal | None:
        """Atomically persist the signal and the complete replayable order intent.

        `pending_submission` is the durable outbox state.  If the process dies at
        any point after this commit, the same client order id and parameters are
        replayed.  Alpaca's client-order-id uniqueness turns that replay into a
        lookup of the already accepted order instead of a duplicate trade.
        """
        async with self._order_submission_lock:
            with SessionLocal() as db:
                state = db.scalar(
                    select(EngineState).where(EngineState.user_id == self.user_id)
                )
                current_strategy = db.scalar(
                    select(Strategy).where(
                        Strategy.id == strategy.id,
                        Strategy.owner_user_id == self.user_id,
                    )
                )
                if (
                    state is None
                    or state.status != "running"
                    or current_strategy is None
                    or not current_strategy.enabled
                    or current_strategy.version != strategy.version
                ):
                    return None
            return self._persist_order_intent_record(
                strategy,
                symbol,
                timestamp,
                action,
                price,
                reason,
                payload,
            )

    def _persist_order_intent_record(
        self,
        strategy: Strategy,
        symbol: str,
        timestamp: datetime,
        action: str,
        price: float,
        reason: str,
        payload: dict[str, Any],
    ) -> Signal | None:
        unique_key = (
            f"{self.user_id}:{strategy.id}:{symbol.upper()}:"
            f"{timestamp.isoformat()}:{action}"
        )
        with SessionLocal() as db:
            signal = Signal(
                user_id=self.user_id,
                unique_key=unique_key,
                strategy_id=strategy.id,
                symbol=symbol,
                bar_timestamp=timestamp,
                action=action,
                price=price,
                reason=reason,
                status="pending_submission",
                payload=payload,
            )
            db.add(signal)
            try:
                db.commit()
                db.refresh(signal)
                return signal
            except IntegrityError:
                db.rollback()
                existing = db.scalar(
                    select(Signal).where(Signal.unique_key == unique_key)
                )
                if existing is None:
                    return None
                if existing.status == "created":
                    # Upgrade a legacy signal left by a crash between the old
                    # signal insert and submit path when the same bar is retried.
                    existing.status = "pending_submission"
                    existing.payload = payload
                    db.commit()
                    db.refresh(existing)
                    return existing
                if existing.status == "pending_submission":
                    return existing
                return None

    async def _resume_pending_submissions(
        self,
        open_orders: list[dict[str, Any]],
        *,
        account: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
        clock: dict[str, Any] | None = None,
        reference_bars: dict[
            tuple[str, str], tuple[datetime, float, pd.DataFrame]
        ] | None = None,
    ) -> None:
        signal_ids = self._pending_submission_ids()
        if not signal_ids:
            return
        connection = self._alpaca_connection_status()
        state = str(connection.get("state") or "unknown")
        if not self._snapshot_connection_ready(connection):
            await self._log_pending_connection_wait(len(signal_ids), state)
            return
        for signal_id in signal_ids:
            should_continue = await self._submit_pending_signal(
                signal_id,
                open_orders,
                account=account,
                positions=positions,
                clock=clock,
                reference_bars=reference_bars,
            )
            if not should_continue:
                break

    @staticmethod
    def _remove_pending_reservation(
        open_orders: list[dict[str, Any]], signal_id: str
    ) -> None:
        open_orders[:] = [
            order
            for order in open_orders
            if str(order.get("id", "")) != f"pending:{signal_id}"
        ]

    def _reserve_pending_buy(
        self,
        open_orders: list[dict[str, Any]],
        signal: Signal,
        status: str,
    ) -> None:
        self._remove_pending_reservation(open_orders, signal.id)
        payload = signal.payload or {}
        if signal.action != "buy":
            return
        open_orders.append(
            {
                "id": f"pending:{signal.id}",
                "client_order_id": payload.get("client_order_id"),
                "symbol": signal.symbol,
                "side": "buy",
                "qty": payload.get("qty"),
                "status": status,
                "_estimated_notional": payload.get("notional"),
            }
        )

    def _update_signal_state(
        self,
        signal_id: str,
        status: str,
        *,
        error: Exception | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Signal | None:
        with SessionLocal() as db:
            signal = db.get(Signal, signal_id)
            if signal is None:
                return None
            payload = dict(signal.payload or {})
            if error is not None:
                payload["error"] = str(error)
                now = datetime.now(timezone.utc)
                payload["last_attempt_at"] = now.isoformat()
                attempts = int(payload.get("attempts") or 0) + 1
                payload["attempts"] = attempts
                if status in UNRESOLVED_ORDER_STATUSES:
                    delay = min(300, 15 * (2 ** min(attempts - 1, 4)))
                    payload["next_attempt_at"] = (
                        now + timedelta(seconds=delay)
                    ).isoformat()
            if status not in UNRESOLVED_ORDER_STATUSES:
                payload.pop("next_attempt_at", None)
            if extra:
                payload.update(extra)
            signal.status = status
            signal.payload = payload
            db.commit()
            db.refresh(signal)
            return signal

    def _save_submitted_order(
        self, signal_id: str, order: dict[str, Any]
    ) -> Signal:
        with SessionLocal() as db:
            signal = db.get(Signal, signal_id)
            if signal is None:
                raise RuntimeError("订单对应的策略信号不存在")
            payload = dict(signal.payload or {})
            client_order_id = str(
                order.get("client_order_id") or payload.get("client_order_id") or ""
            )
            order_id = str(order.get("id") or "")
            if not client_order_id or not order_id:
                raise RuntimeError("Alpaca 返回的订单标识不完整")
            record = db.scalar(
                select(OrderRecord).where(
                    OrderRecord.user_id == self.user_id,
                    (
                        (OrderRecord.id == order_id)
                        | (OrderRecord.client_order_id == client_order_id)
                    ),
                )
            )
            if record is None:
                record = OrderRecord(
                    user_id=self.user_id,
                    id=order_id,
                    client_order_id=client_order_id,
                    strategy_id=signal.strategy_id,
                    signal_id=signal.id,
                    symbol=signal.symbol,
                    side=signal.action,
                    order_type=str(order.get("type") or payload.get("order_type") or "market"),
                    qty=as_float(order.get("qty") or payload.get("qty")) or None,
                    notional=(
                        as_float(payload.get("notional")) or None
                        if signal.action == "buy"
                        else None
                    ),
                    status=str(order.get("status", "accepted")),
                    filled_qty=0.0,
                    raw=order,
                )
                db.add(record)
                db.flush()
            else:
                record.status = str(order.get("status", record.status))
                record.raw = order
            record.filled_avg_price = as_float(order.get("filled_avg_price")) or None
            self._apply_fill_delta(db, record, order)
            remote_status = str(order.get("status", "accepted")).lower()
            signal.status = (
                "rejected" if remote_status in {"rejected", "expired"} else "submitted"
            )
            signal_payload = {
                **payload,
                "submitted_order_id": order_id,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            signal_payload.pop("next_attempt_at", None)
            signal.payload = signal_payload
            db.commit()
            db.refresh(signal)
            return signal

    def _persist_trailing_intent(
        self,
        signal_id: str,
        strategy_id: str,
        symbol: str,
        qty: float,
        mode: str,
        value: float,
    ) -> None:
        with SessionLocal() as db:
            signal = db.get(Signal, signal_id)
            if signal is None:
                return
            payload = dict(signal.payload or {})
            existing = payload.get("trailing_stop_intent") or {}
            if existing.get("status") in {
                "pending_submission",
                "pending_reconciliation",
                "submitted",
            }:
                return
            client_order_id = self._client_order_id(
                strategy_id,
                symbol,
                signal.bar_timestamp,
                "trail",
            )
            payload["trailing_stop_intent"] = {
                "status": "pending_submission",
                "client_order_id": client_order_id,
                "symbol": symbol,
                "qty": qty,
                "mode": mode,
                "value": value,
                "attempts": 0,
            }
            signal.payload = payload
            db.commit()

    def _pending_trailing_signal_ids(self) -> list[str]:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            signals = db.scalars(
                select(Signal).where(Signal.user_id == self.user_id)
            ).all()
        result: list[str] = []
        for signal in signals:
            intent = (signal.payload or {}).get("trailing_stop_intent") or {}
            if intent.get("status") not in {
                "pending_submission",
                "pending_reconciliation",
            }:
                continue
            next_attempt_at = self._payload_datetime(intent.get("next_attempt_at"))
            if next_attempt_at is None or next_attempt_at <= now:
                result.append(signal.id)
        return result

    def _update_trailing_intent(
        self,
        signal_id: str,
        status: str,
        *,
        error: Exception | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with SessionLocal() as db:
            signal = db.get(Signal, signal_id)
            if signal is None:
                return None
            payload = dict(signal.payload or {})
            intent = dict(payload.get("trailing_stop_intent") or {})
            if not intent:
                return None
            intent["status"] = status
            if error is not None:
                now = datetime.now(timezone.utc)
                attempts = int(intent.get("attempts") or 0) + 1
                intent.update(
                    {
                        "error": str(error),
                        "attempts": attempts,
                        "last_attempt_at": now.isoformat(),
                        "next_attempt_at": (
                            now
                            + timedelta(
                                seconds=min(
                                    300, 15 * (2 ** min(attempts - 1, 4))
                                )
                            )
                        ).isoformat(),
                    }
                )
            if status not in {"pending_submission", "pending_reconciliation"}:
                intent.pop("next_attempt_at", None)
            if extra:
                intent.update(extra)
            payload["trailing_stop_intent"] = intent
            signal.payload = payload
            db.commit()
            return intent

    def _save_trailing_order(
        self, signal_id: str, order: dict[str, Any]
    ) -> None:
        with SessionLocal() as db:
            signal = db.get(Signal, signal_id)
            if signal is None:
                raise RuntimeError("移动止损对应的策略信号不存在")
            payload = dict(signal.payload or {})
            intent = dict(payload.get("trailing_stop_intent") or {})
            client_order_id = str(
                order.get("client_order_id") or intent.get("client_order_id") or ""
            )
            order_id = str(order.get("id") or "")
            if not client_order_id or not order_id:
                raise RuntimeError("Alpaca 返回的移动止损订单标识不完整")
            record = db.scalar(
                select(OrderRecord).where(
                    OrderRecord.user_id == self.user_id,
                    (
                        (OrderRecord.id == order_id)
                        | (OrderRecord.client_order_id == client_order_id)
                    ),
                )
            )
            if record is None:
                record = OrderRecord(
                    user_id=self.user_id,
                    id=order_id,
                    client_order_id=client_order_id,
                    strategy_id=signal.strategy_id,
                    signal_id=signal.id,
                    symbol=signal.symbol,
                    side="sell",
                    order_type="trailing_stop",
                    qty=as_float(order.get("qty") or intent.get("qty")) or None,
                    notional=None,
                    status=str(order.get("status", "accepted")),
                    filled_qty=0.0,
                    raw=order,
                )
                db.add(record)
                db.flush()
            else:
                record.status = str(order.get("status", record.status))
                record.raw = order
            record.filled_avg_price = as_float(order.get("filled_avg_price")) or None
            self._apply_fill_delta(db, record, order)
            intent.update(
                {
                    "status": "submitted",
                    "order_id": order_id,
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            intent.pop("next_attempt_at", None)
            payload["trailing_stop_intent"] = intent
            signal.payload = payload
            db.commit()

    async def _submit_pending_trailing_intent(
        self, signal_id: str, *, recover_existing: bool = True
    ) -> bool:
        with SessionLocal() as db:
            signal = db.get(Signal, signal_id)
            if signal is None:
                return True
            intent = dict(
                (signal.payload or {}).get("trailing_stop_intent") or {}
            )
        if intent.get("status") not in {
            "pending_submission",
            "pending_reconciliation",
        }:
            return True
        client_order_id = str(intent.get("client_order_id") or "")
        if not client_order_id:
            self._update_trailing_intent(
                signal_id,
                "rejected",
                error=RuntimeError("移动止损意图缺少客户端订单号"),
            )
            return True

        if recover_existing:
            try:
                existing = await asyncio.to_thread(
                    self.alpaca.get_order_by_client_id, client_order_id
                )
            except Exception as exc:
                self._update_trailing_intent(
                    signal_id, intent["status"], error=exc
                )
                return False
            if existing is not None:
                self._save_trailing_order(signal_id, existing)
                return True

        with SessionLocal() as db:
            state = db.scalar(
                select(EngineState).where(EngineState.user_id == self.user_id)
            )
        if state is None or state.status != "running":
            # Pausing the engine must be a hard no-new-POST boundary. We still
            # perform the lookup above so an order accepted before the pause is
            # never lost locally.
            return False

        owned_qty = self._owned_qty(signal.strategy_id, signal.symbol)
        qty = min(as_float(intent.get("qty")), owned_qty)
        if qty <= 0:
            self._update_trailing_intent(
                signal_id,
                "cancelled",
                extra={"resolution": "no_strategy_owned_position"},
            )
            return True
        try:
            async with self._order_submission_lock:
                with SessionLocal() as db:
                    current_state = db.scalar(
                        select(EngineState).where(
                            EngineState.user_id == self.user_id
                        )
                    )
                if current_state is None or current_state.status != "running":
                    return False
                await self._verify_long_only_sell_capacity(
                    signal.symbol,
                    qty,
                    client_order_id,
                )
                order = await asyncio.to_thread(
                    self.alpaca.submit_trailing_stop,
                    signal.symbol,
                    qty,
                    str(intent.get("mode") or "percent"),
                    as_float(intent.get("value")),
                    client_order_id,
                )
        except LongOnlyInvariantError as exc:
            self._update_trailing_intent(
                signal_id,
                "cancelled",
                extra={"resolution": "long_only_quarantine", "error": str(exc)},
            )
            await self.log("critical", "risk", f"{signal.symbol}: {exc}")
            return False
        except AlpacaAmbiguousOrderError as exc:
            self._update_trailing_intent(
                signal_id, "pending_reconciliation", error=exc
            )
            return False
        except AlpacaTransientError as exc:
            self._update_trailing_intent(
                signal_id, "pending_submission", error=exc
            )
            return False
        except Exception as exc:
            self._update_trailing_intent(signal_id, "rejected", error=exc)
            await self.log(
                "error", "order", f"{signal.symbol} 创建移动止损失败: {exc}"
            )
            return True
        self._save_trailing_order(signal_id, order)
        return True

    async def _resume_pending_trailing_intents(self) -> None:
        signal_ids = self._pending_trailing_signal_ids()
        if not signal_ids:
            return
        if not self._reconciliation_connection_ready(
            self._alpaca_connection_status()
        ):
            await self._log_pending_connection_wait(
                len(signal_ids),
                str(self._alpaca_connection_status().get("state") or "unknown"),
            )
            return
        for signal_id in signal_ids:
            if not await self._submit_pending_trailing_intent(signal_id):
                break

    async def _ensure_trailing_intents_for_filled_entries(self) -> None:
        with SessionLocal() as db:
            records = db.scalars(
                select(OrderRecord).where(
                    OrderRecord.user_id == self.user_id,
                    OrderRecord.side == "buy",
                    OrderRecord.filled_qty > 0,
                    OrderRecord.strategy_id.is_not(None),
                    OrderRecord.signal_id.is_not(None),
                )
            ).all()
        for record in records:
            status = str(record.status or "").lower()
            requested_qty = as_float(record.qty)
            fill_is_complete = (
                requested_qty > 0 and record.filled_qty + 1e-9 >= requested_qty
            )
            entry_is_terminal = status in {
                "filled",
                "canceled",
                "cancelled",
                "expired",
                "rejected",
            }
            if not fill_is_complete and not entry_is_terminal:
                # A still-open partial entry can receive more fills. Creating a
                # one-shot trailing stop now would leave the later fill delta
                # unprotected because Alpaca fixes the stop quantity at submit.
                continue
            with SessionLocal() as db:
                existing = db.scalar(
                    select(OrderRecord.id).where(
                        OrderRecord.user_id == self.user_id,
                        OrderRecord.signal_id == record.signal_id,
                        OrderRecord.order_type == "trailing_stop",
                    )
                )
                signal = db.get(Signal, record.signal_id)
            if existing is not None or signal is None:
                continue
            trailing = (signal.payload or {}).get("trailing_stop")
            if trailing is None:
                continue
            self._persist_trailing_intent(
                str(record.signal_id),
                str(record.strategy_id),
                record.symbol,
                record.filled_qty,
                str(trailing.get("mode") or "percent"),
                as_float(trailing.get("value")),
            )
        await self._resume_pending_trailing_intents()

    async def _revalidate_pending_entry(
        self,
        signal: Signal,
        strategy: Strategy,
        payload: dict[str, Any],
        open_orders: list[dict[str, Any]],
        account: dict[str, Any] | None,
        positions: list[dict[str, Any]] | None,
        clock: dict[str, Any] | None,
        reference_bars: dict[
            tuple[str, str], tuple[datetime, float, pd.DataFrame]
        ] | None,
    ) -> tuple[str, str, bool, dict[str, Any]]:
        if account is None or positions is None or clock is None:
            return "wait", "缺少最新账户风险快照，订单意图继续等待", False, {}
        if not bool(clock.get("is_open")):
            return "wait", "当前不在美股常规交易时段", False, {}
        try:
            definition = RuleDefinition.model_validate(strategy.definition)
        except Exception:
            return "reject", "策略定义已失效", False, {}
        intent_strategy_version = payload.get("strategy_version")
        if (
            intent_strategy_version is not None
            and int(intent_strategy_version) != strategy.version
        ):
            return "reject", "策略版本已变化，旧入场意图已安全取消", False, {}

        bar_timestamp = signal.bar_timestamp
        if bar_timestamp.tzinfo is None:
            bar_timestamp = bar_timestamp.replace(tzinfo=timezone.utc)
        else:
            bar_timestamp = bar_timestamp.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)

        with SessionLocal() as db:
            risk_settings = db.scalar(
                select(RiskSettings).where(RiskSettings.user_id == self.user_id)
            )
            state = db.scalar(
                select(EngineState).where(EngineState.user_id == self.user_id)
            )
            if risk_settings is None or state is None:
                return "reject", "风险设置或引擎状态不存在", False, {}
            timeframe = str(payload.get("timeframe") or definition.timeframe)
            max_age = max(
                risk_settings.stale_data_seconds,
                TIMEFRAME_SECONDS[timeframe] * 3,
            )
            latest_reference = (reference_bars or {}).get(
                (timeframe, signal.symbol.upper())
            )
            if latest_reference is None:
                return "wait", "缺少最新已完成K线，无法确认信号有效期", False, {}
            latest_bar_timestamp = latest_reference[0]
            latest_frame = latest_reference[2]
            if latest_bar_timestamp.tzinfo is None:
                latest_bar_timestamp = latest_bar_timestamp.replace(
                    tzinfo=timezone.utc
                )
            if not self._entry_intent_is_fresh(
                bar_timestamp,
                latest_bar_timestamp.astimezone(timezone.utc),
                timeframe,
                now,
                max_age,
                self._frame_session_close(latest_frame, bar_timestamp),
            ):
                return "reject", "入场信号对应K线或交易时段已过期", False, {}

        try:
            quotes = await asyncio.to_thread(
                self.alpaca.get_latest_quotes, [signal.symbol]
            )
        except Exception:
            self._abort_new_orders_for_cycle = True
            return "wait", "最新报价暂不可用，订单意图继续等待", False, {}
        quote = quotes.get(signal.symbol) or quotes.get(signal.symbol.upper()) or {}
        quote_timestamp = self._payload_datetime(
            quote.get("timestamp") or quote.get("t")
        )
        if quote_timestamp is None:
            return "wait", "最新报价缺少时间戳，订单意图继续等待", False, {}
        if (
            (now - quote_timestamp).total_seconds() < 0
            or (now - quote_timestamp).total_seconds()
            > risk_settings.stale_data_seconds
        ):
            return "wait", "最新报价已过期，订单意图继续等待", False, {}

        order_type = str(payload.get("order_type") or "market")
        if order_type == "limit":
            reference_price = as_float(payload.get("limit_price"))
        else:
            reference_price = as_float(
                quote.get("ask_price") or quote.get("ap")
            )
            if reference_price <= 0:
                return "wait", "最新卖价无效，订单意图继续等待", False, {}

        stop_guard = definition.order.stop_loss
        take_guard = definition.order.take_profit
        stop_atr = (
            latest_atr(latest_frame, stop_guard.atr_period)
            if stop_guard is not None and stop_guard.mode == "atr"
            else None
        )
        take_atr = (
            latest_atr(latest_frame, take_guard.atr_period)
            if take_guard is not None and take_guard.mode == "atr"
            else None
        )
        stop_price = self._guard_price(
            reference_price, stop_guard, stop_atr, False
        )
        take_price = self._guard_price(
            reference_price, take_guard, take_atr, True
        )
        if (stop_guard is not None and stop_price is None) or (
            take_guard is not None and take_price is None
        ):
            return "wait", "保护价所需ATR数据不足，订单意图继续等待", False, {}

        equity = as_float(account.get("equity"))
        config = definition.position
        if config.mode == "fixed_qty":
            qty = config.value
            desired_notional = qty * reference_price
        elif config.mode == "fixed_notional":
            desired_notional = config.value
            qty = desired_notional / reference_price
        elif config.mode == "percent_equity":
            desired_notional = equity * config.value / 100
            qty = desired_notional / reference_price
        else:
            if stop_price is None or stop_price <= 0 or stop_price >= reference_price:
                return "reject", "开盘价变化后无法安全计算风险仓位", False, {}
            risk_dollars = equity * config.value / 100
            qty = risk_dollars / (reference_price - stop_price)
            desired_notional = qty * reference_price
        original_qty = as_float(payload.get("original_qty") or payload.get("qty"))
        original_notional = as_float(
            payload.get("original_notional") or payload.get("notional")
        )
        if original_qty > 0:
            qty = min(qty, original_qty)
        if original_notional > 0:
            qty = min(qty, original_notional / reference_price)
        desired_notional = qty * reference_price
        if qty <= 0 or desired_notional <= 0:
            return "reject", "待提交订单仓位无效", False, {}

        try:
            asset = await asyncio.to_thread(self.alpaca.get_asset, signal.symbol)
        except Exception:
            self._abort_new_orders_for_cycle = True
            return "wait", "证券可交易状态暂不可用，订单意图继续等待", False, {}
        own_client_id = str(payload.get("client_order_id") or "")
        risk_open_orders = [
            order
            for order in open_orders
            if str(order.get("client_order_id") or "") != own_client_id
            and str(order.get("id") or "") != f"pending:{signal.id}"
        ]
        with SessionLocal() as db:
            risk_settings = db.scalar(
                select(RiskSettings).where(RiskSettings.user_id == self.user_id)
            )
            state = db.scalar(
                select(EngineState).where(EngineState.user_id == self.user_id)
            )
            decision = self.risk.check_entry(
                symbol=signal.symbol,
                desired_notional=desired_notional,
                account=account,
                positions=positions,
                asset=asset,
                clock=clock,
                # The rule signal remains tied to its completed bar for audit;
                # execution freshness is proven by the timestamped live quote.
                bar_timestamp=quote_timestamp,
                max_data_age_seconds=max_age,
                settings=risk_settings,
                state=state,
                strategy_max_symbol_pct=definition.risk.max_symbol_pct,
                strategy_max_positions=definition.risk.max_positions,
                open_orders=risk_open_orders,
                reference_price=reference_price,
            )
            db.commit()
        return (
            "allow" if decision.allowed else "reject",
            decision.reason,
            decision.halt_engine,
            {
                "qty": qty,
                "notional": desired_notional,
                "risk_reference_price": reference_price,
                "stop_price": stop_price,
                "take_price": take_price,
                "risk_settings_snapshot": self._risk_settings_snapshot(
                    risk_settings
                ),
            },
        )

    @staticmethod
    def _frame_session_close(
        frame: pd.DataFrame, timestamp: datetime
    ) -> datetime | None:
        session_date = timestamp.astimezone(ET).date()
        raw = (frame.attrs.get("session_closes", {}) or {}).get(
            session_date.isoformat()
        )
        if not raw:
            return None
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)

    @staticmethod
    def _entry_intent_is_fresh(
        bar_timestamp: datetime,
        latest_bar_timestamp: datetime,
        timeframe: str,
        now: datetime,
        max_age_seconds: int,
        session_close_at: datetime | None = None,
    ) -> bool:
        if latest_bar_timestamp != bar_timestamp:
            return False
        bar_et = bar_timestamp.astimezone(ET)
        now_et = now.astimezone(ET)
        if bar_et.date() == now_et.date():
            return (now - bar_timestamp).total_seconds() <= max_age_seconds
        if now_et.date() < bar_et.date():
            return False
        if not (
            (now_et.hour, now_et.minute) >= (9, 30)
            and (now_et.hour, now_et.minute) < (10, 0)
        ):
            return False
        if timeframe == "1Day":
            return True
        bar_end = bar_et + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
        session_close_at = session_close_at or datetime.combine(
            bar_et.date(), datetime_time(16, 0), tzinfo=ET
        )
        # The final regular-session bar can only be acted on at the next open.
        # Exact latest-bar equality handles weekends/holidays without guessing a
        # calendar gap, and rejects once any newer completed session bar exists.
        return bar_end >= session_close_at

    def _revalidated_exit_qty(
        self,
        signal: Signal,
        positions: list[dict[str, Any]] | None,
    ) -> float:
        if positions is None:
            return 0.0
        account_qty = as_float(
            next(
                (
                    position.get("qty")
                    for position in positions
                    if str(position.get("symbol", "")).upper() == signal.symbol.upper()
                ),
                0,
            )
        )
        return min(self._owned_qty(signal.strategy_id, signal.symbol), account_qty)

    async def _submit_pending_signal(
        self,
        signal_id: str,
        open_orders: list[dict[str, Any]],
        *,
        account: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
        clock: dict[str, Any] | None = None,
        reference_bars: dict[
            tuple[str, str], tuple[datetime, float, pd.DataFrame]
        ] | None = None,
        recover_existing: bool = True,
    ) -> bool:
        with SessionLocal() as db:
            signal = db.get(Signal, signal_id)
            if signal is None or signal.status != "pending_submission":
                return True
            payload = dict(signal.payload or {})
            strategy = db.get(Strategy, signal.strategy_id)
            state = db.scalar(
                select(EngineState).where(EngineState.user_id == self.user_id)
            )

        client_order_id = str(payload.get("client_order_id") or "")
        if not client_order_id:
            self._update_signal_state(
                signal.id,
                "rejected",
                error=RuntimeError("持久化订单意图缺少客户端订单号"),
            )
            self._remove_pending_reservation(open_orders, signal.id)
            return True

        if recover_existing:
            lookup = getattr(self.alpaca, "get_order_by_client_order_id", None)
            if lookup is None:
                lookup = getattr(self.alpaca, "get_order_by_client_id", None)
            if lookup is not None:
                try:
                    existing_order = await asyncio.to_thread(
                        lookup, client_order_id
                    )
                except Exception as exc:
                    saved = self._update_signal_state(
                        signal.id, "pending_submission", error=exc
                    )
                    if saved is not None:
                        self._reserve_pending_buy(
                            open_orders, saved, "pending_submission"
                        )
                    await self.log(
                        "warning",
                        "order",
                        f"{signal.symbol} 待提交订单查询失败，将在退避后重试",
                        {"client_order_id": client_order_id},
                    )
                    return False
                if existing_order is not None:
                    saved = self._save_submitted_order(signal.id, existing_order)
                    self._remove_pending_reservation(open_orders, signal.id)
                    if saved.action == "buy":
                        open_orders.append(
                            {
                                **existing_order,
                                "_estimated_notional": saved.payload.get("notional"),
                            }
                        )
                    await self.log(
                        "info",
                        "order",
                        f"{signal.symbol} 待提交订单已按客户端订单号恢复",
                        {"client_order_id": client_order_id},
                    )
                    return True

        # Recovery must precede local pause/disable checks: the POST may already
        # have succeeded before the process stopped.  Pausing blocks only a new
        # POST; it must never discard tracking of an existing Paper order.
        if state is None or state.status != "running":
            return False
        if strategy is None or not strategy.enabled:
            self._update_signal_state(
                signal.id,
                "cancelled",
                extra={"resolution": "strategy_disabled_before_submission"},
            )
            self._remove_pending_reservation(open_orders, signal.id)
            await self.log(
                "warning",
                "order",
                f"{signal.symbol} 待提交订单已取消：策略已停用",
            )
            return True
        strategy_name = strategy.name

        if signal.action == "buy":
            (
                disposition,
                reason,
                halt_engine,
                intent_updates,
            ) = await self._revalidate_pending_entry(
                signal,
                strategy,
                payload,
                open_orders,
                account,
                positions,
                clock,
                reference_bars,
            )
            if disposition == "wait":
                saved = self._update_signal_state(
                    signal.id,
                    "pending_submission",
                    error=RuntimeError(reason),
                )
                if saved is not None:
                    self._reserve_pending_buy(
                        open_orders, saved, "pending_submission"
                    )
                await self.log(
                    "warning",
                    "order",
                    f"{strategy.name}/{signal.symbol}: {reason}",
                )
                return False
            if disposition != "allow":
                self._update_signal_state(
                    signal.id,
                    "rejected",
                    extra={"resolution": "pre_submit_revalidation", "error": reason},
                )
                self._remove_pending_reservation(open_orders, signal.id)
                await self.log(
                    "warning",
                    "risk",
                    f"{strategy.name}/{signal.symbol}: 待提交订单已取消：{reason}",
                )
                if halt_engine:
                    await self.pause(reason, cancel_orders=True)
                return True
            payload.update(intent_updates)
            signal = self._update_signal_state(
                signal.id,
                "pending_submission",
                extra=intent_updates,
            ) or signal
        else:
            exit_qty = self._revalidated_exit_qty(signal, positions)
            if exit_qty <= 0:
                self._update_signal_state(
                    signal.id,
                    "cancelled",
                    extra={"resolution": "no_strategy_owned_position"},
                )
                self._remove_pending_reservation(open_orders, signal.id)
                await self.log(
                    "warning",
                    "order",
                    f"{signal.symbol} 待平仓订单已取消：策略已无可平仓持仓",
                )
                return True
            payload["qty"] = exit_qty
            signal = self._update_signal_state(
                signal.id, "pending_submission", extra={"qty": exit_qty}
            ) or signal

        if signal.action == "sell" and payload.get("cancel_strategy_orders", True):
            try:
                await self._cancel_strategy_symbol_orders(
                    signal.strategy_id, signal.symbol, open_orders
                )
            except StrategyOrderCancellationError as exc:
                saved = self._update_signal_state(
                    signal.id,
                    "pending_submission",
                    error=exc,
                    extra={"cancel_failed_order_ids": exc.order_ids},
                )
                if saved is not None:
                    await self.log(
                        "warning",
                        "order",
                        f"{signal.symbol} 自有订单未全部取消，本轮不会提交市价平仓",
                        {
                            "client_order_id": client_order_id,
                            "failed_order_ids": exc.order_ids,
                        },
                    )
                return True

        try:
            async with self._order_submission_lock:
                # Re-read the durable control state immediately before the POST.
                # The earlier validation performs network I/O and may have raced
                # with a user pause or strategy disable.
                with SessionLocal() as db:
                    current_state = db.scalar(
                        select(EngineState).where(
                            EngineState.user_id == self.user_id
                        )
                    )
                    current_strategy = db.get(Strategy, signal.strategy_id)
                    current_risk_settings = db.scalar(
                        select(RiskSettings).where(
                            RiskSettings.user_id == self.user_id
                        )
                    )
                if current_state is None or current_state.status != "running":
                    return False
                if current_strategy is None or not current_strategy.enabled:
                    return False
                expected_risk_settings = payload.get("risk_settings_snapshot")
                if (
                    expected_risk_settings is not None
                    and (
                        current_risk_settings is None
                        or self._risk_settings_snapshot(current_risk_settings)
                        != expected_risk_settings
                    )
                ):
                    return False
                if signal.action == "buy":
                    order = await asyncio.to_thread(
                        self.alpaca.submit_entry_order,
                        symbol=signal.symbol,
                        qty=as_float(payload.get("qty")) or None,
                        notional=None,
                        order_type=str(payload.get("order_type") or "market"),
                        time_in_force=str(payload.get("time_in_force") or "day"),
                        client_order_id=client_order_id,
                        limit_price=payload.get("limit_price"),
                        stop_price=payload.get("stop_price"),
                        take_price=payload.get("take_price"),
                    )
                else:
                    await self._verify_long_only_sell_capacity(
                        signal.symbol,
                        as_float(payload.get("qty")),
                        client_order_id,
                    )
                    order = await asyncio.to_thread(
                        self.alpaca.submit_exit_order,
                        signal.symbol,
                        as_float(payload.get("qty")),
                        client_order_id,
                    )
        except LongOnlyInvariantError as exc:
            self._update_signal_state(
                signal.id,
                "cancelled",
                extra={"resolution": "long_only_quarantine", "error": str(exc)},
            )
            self._remove_pending_reservation(open_orders, signal.id)
            await self.log("critical", "risk", f"{signal.symbol}: {exc}")
            return False
        except AlpacaAmbiguousOrderError as exc:
            self._abort_new_orders_for_cycle = True
            saved = self._update_signal_state(
                signal.id, "pending_reconciliation", error=exc
            )
            if saved is not None:
                self._reserve_pending_buy(
                    open_orders, saved, "pending_reconciliation"
                )
            await self.log(
                "warning",
                "order",
                f"{signal.symbol} 订单结果不确定，已等待按客户端订单号对账",
                {"client_order_id": client_order_id},
            )
            return False
        except AlpacaTransientError as exc:
            self._abort_new_orders_for_cycle = True
            # The adapter raises AlpacaAmbiguousOrderError after an attempted POST.
            # Other transient errors therefore mean the circuit rejected the call
            # before submission; keep the outbox item replayable.
            saved = self._update_signal_state(
                signal.id, "pending_submission", error=exc
            )
            if saved is not None:
                self._reserve_pending_buy(open_orders, saved, "pending_submission")
            await self.log(
                "warning",
                "order",
                f"{signal.symbol} 订单尚未提交，连接恢复后将自动重试",
                {"client_order_id": client_order_id},
            )
            return False
        except Exception as exc:
            self._update_signal_state(signal.id, "rejected", error=exc)
            self._remove_pending_reservation(open_orders, signal.id)
            await self.log("error", "order", f"{signal.symbol} 下单失败: {exc}")
            return True

        try:
            saved = self._save_submitted_order(signal.id, order)
        except Exception as exc:
            # The remote order is known to exist but local persistence failed.
            # Reconciliation, not a second POST, is the only safe next action.
            saved = self._update_signal_state(
                signal.id,
                "pending_reconciliation",
                error=exc,
                extra={"known_remote_order": order},
            )
            if saved is not None:
                self._reserve_pending_buy(
                    open_orders, saved, "pending_reconciliation"
                )
            await self.log(
                "warning",
                "order",
                f"{signal.symbol} 订单已被 Alpaca 接收，本地记录等待自动对账",
                {"client_order_id": client_order_id},
            )
            return True

        self._remove_pending_reservation(open_orders, signal.id)
        if saved.action == "buy":
            open_orders.append(
                {**order, "_estimated_notional": saved.payload.get("notional")}
            )
        action_label = "模拟买单" if saved.action == "buy" else "模拟平仓"
        await self.log(
            "info", "order", f"{strategy_name}: 已提交 {signal.symbol} {action_label}"
        )
        await websocket_manager.broadcast(
            self.user_id,
            "signal",
            {
                "action": saved.action,
                "symbol": saved.symbol,
                "strategy": strategy_name,
            },
        )

    @staticmethod
    def _guard_price(price: float, guard, atr_value: float | None, is_take: bool) -> float | None:
        if guard is None:
            return None
        if guard.mode == "percent":
            distance = price * guard.value / 100
        else:
            if atr_value is None:
                return None
            distance = atr_value * guard.value
        return round(price + distance if is_take else price - distance, 2)

    def cache_bars(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> None:
        with SessionLocal() as db:
            for timestamp, row in frame.iterrows():
                existing = db.scalar(
                    select(MarketBar).where(
                        MarketBar.symbol == symbol,
                        MarketBar.timeframe == timeframe,
                        MarketBar.timestamp == timestamp.to_pydatetime(),
                    )
                )
                if existing:
                    existing.open = float(row["open"])
                    existing.high = float(row["high"])
                    existing.low = float(row["low"])
                    existing.close = float(row["close"])
                    existing.volume = float(row["volume"])
                else:
                    db.add(
                        MarketBar(
                            symbol=symbol,
                            timeframe=timeframe,
                            timestamp=timestamp.to_pydatetime(),
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"]),
                            feed="iex",
                        )
                    )
            db.commit()

    @staticmethod
    def _flatten_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        seen: set[str] = set()

        def visit(order: dict[str, Any], parent_id: str = "") -> None:
            current = dict(order)
            order_id = str(current.get("id") or "")
            if parent_id and not current.get("parent_order_id"):
                current["parent_order_id"] = parent_id
            if not order_id or order_id not in seen:
                flattened.append(current)
                if order_id:
                    seen.add(order_id)
            for leg in order.get("legs") or []:
                if isinstance(leg, dict):
                    visit(leg, order_id or parent_id)

        for order in orders:
            visit(order)
        return flattened

    async def reconcile_orders(self) -> None:
        if not self.alpaca.configured:
            return
        connection = self._alpaca_connection_status()
        if not self._reconciliation_connection_ready(connection):
            with SessionLocal() as db:
                pending_count = len(
                    db.scalars(
                    select(Signal.id)
                    .where(
                        Signal.user_id == self.user_id,
                        Signal.status.in_(UNRESOLVED_ORDER_STATUSES),
                    )
                    ).all()
                )
            if pending_count:
                await self._log_pending_connection_wait(
                    pending_count, str(connection.get("state") or "unknown")
                )
            return
        try:
            orders = self._flatten_orders(
                await asyncio.to_thread(self.alpaca.get_orders, "all")
            )
            positions = await asyncio.to_thread(self.alpaca.get_positions)
            with SessionLocal() as db:
                pending_snapshot = db.scalars(
                    select(Signal).where(
                        Signal.user_id == self.user_id,
                        Signal.status.in_(UNRESOLVED_ORDER_STATUSES),
                    )
                ).all()

            order_client_ids = {
                str(order.get("client_order_id") or "") for order in orders
            }
            confirmed_missing: set[str] = set()
            lookup_failures: dict[str, Exception] = {}
            lookup = getattr(self.alpaca, "get_order_by_client_id", None)
            reconciliation_now = datetime.now(timezone.utc)
            for pending_signal in pending_snapshot:
                payload = pending_signal.payload or {}
                client_order_id = str(payload.get("client_order_id") or "")
                if not client_order_id or client_order_id in order_client_ids:
                    continue
                known_remote = payload.get("known_remote_order")
                if isinstance(known_remote, dict) and known_remote.get("id"):
                    orders.extend(self._flatten_orders([known_remote]))
                    order_client_ids.add(client_order_id)
                    continue
                next_attempt_at = self._payload_datetime(payload.get("next_attempt_at"))
                if next_attempt_at is not None and next_attempt_at > reconciliation_now:
                    continue
                if lookup is None:
                    lookup_failures[pending_signal.id] = RuntimeError(
                        "Alpaca 客户端订单号查询接口不可用"
                    )
                    break
                try:
                    recovered = await asyncio.to_thread(lookup, client_order_id)
                except Exception as exc:
                    lookup_failures[pending_signal.id] = exc
                    # The first uncertain lookup usually degrades/opens the shared
                    # channel. Do not fan the same outage across every pending row.
                    break
                if recovered is None:
                    confirmed_missing.add(pending_signal.id)
                else:
                    orders.extend(self._flatten_orders([recovered]))
                    order_client_ids.add(client_order_id)

            recovered_pending: list[tuple[str, str]] = []
            expired_pending: list[tuple[str, str]] = []
            external_sell_fills: list[tuple[str, str]] = []
            with SessionLocal() as db:
                pending_signals = db.scalars(
                    select(Signal).where(
                        Signal.user_id == self.user_id,
                        Signal.status.in_(UNRESOLVED_ORDER_STATUSES),
                    )
                ).all()
                pending_by_client_id = {
                    str(signal.payload.get("client_order_id")): signal
                    for signal in pending_signals
                    if signal.payload and signal.payload.get("client_order_id")
                }
                for order in orders:
                    order_id = str(order.get("id"))
                    if not order_id or order_id == "None":
                        continue
                    record = db.scalar(
                        select(OrderRecord).where(
                            OrderRecord.id == order_id,
                            OrderRecord.user_id == self.user_id,
                        )
                    )
                    if record is None:
                        client_order_id = str(order.get("client_order_id") or "")
                        pending_signal = pending_by_client_id.get(client_order_id)
                        if pending_signal is not None:
                            payload = pending_signal.payload or {}
                            record = OrderRecord(
                                user_id=self.user_id,
                                id=order_id,
                                client_order_id=client_order_id,
                                strategy_id=pending_signal.strategy_id,
                                signal_id=pending_signal.id,
                                symbol=str(
                                    order.get("symbol") or pending_signal.symbol
                                ).upper(),
                                side=self._order_side(order),
                                order_type=str(order.get("type") or "market"),
                                qty=as_float(order.get("qty") or payload.get("qty"))
                                or None,
                                notional=(
                                    as_float(payload.get("notional")) or None
                                    if pending_signal.action == "buy"
                                    else None
                                ),
                                status=str(order.get("status", "accepted")),
                                filled_qty=0.0,
                                raw=order,
                            )
                            db.add(record)
                            db.flush()
                            remote_status = str(order.get("status", "accepted")).lower()
                            pending_signal.status = (
                                "rejected"
                                if remote_status in {"rejected", "expired"}
                                else "submitted"
                            )
                            reconciled_payload = {
                                **payload,
                                "reconciled_order_id": order_id,
                            }
                            reconciled_payload.pop("next_attempt_at", None)
                            pending_signal.payload = reconciled_payload
                            recovered_pending.append(
                                (pending_signal.symbol, client_order_id)
                            )
                            pending_by_client_id.pop(client_order_id, None)

                    if record is None:
                        parent_id = str(order.get("parent_order_id") or "")
                        parent = db.scalar(
                            select(OrderRecord).where(
                                OrderRecord.id == parent_id,
                                OrderRecord.user_id == self.user_id,
                            )
                        ) if parent_id else None
                        if parent is not None:
                            record = OrderRecord(
                                user_id=self.user_id,
                                id=order_id,
                                client_order_id=str(
                                    order.get("client_order_id") or f"leg-{order_id[:20]}"
                                ),
                                strategy_id=parent.strategy_id,
                                signal_id=parent.signal_id,
                                symbol=str(order.get("symbol") or parent.symbol),
                                side=self._order_side(order),
                                order_type=str(order.get("type") or "bracket_leg"),
                                qty=as_float(order.get("qty")) or None,
                                notional=None,
                                status=str(order.get("status", "accepted")),
                                raw=order,
                            )
                            db.add(record)
                            db.flush()
                    if record is None:
                        client_order_id = str(order.get("client_order_id") or "")
                        parent_id = str(order.get("parent_order_id") or "")
                        if not client_order_id.startswith("qp-") and not parent_id:
                            record = OrderRecord(
                                user_id=self.user_id,
                                id=order_id,
                                client_order_id=(
                                    client_order_id or f"external-{order_id}"
                                ),
                                strategy_id=None,
                                signal_id=None,
                                symbol=str(order.get("symbol") or "").upper(),
                                side=self._order_side(order),
                                order_type=str(order.get("type") or "external"),
                                qty=as_float(order.get("qty")) or None,
                                notional=None,
                                status=str(order.get("status", "unknown")),
                                # First sight of a historical external order is a
                                # baseline. Current risk is established by the
                                # position/open-order invariant audit below.
                                filled_qty=as_float(order.get("filled_qty")),
                                filled_avg_price=(
                                    as_float(order.get("filled_avg_price")) or None
                                ),
                                raw=order,
                            )
                            db.add(record)
                            db.flush()
                    if record:
                        previous_filled_qty = record.filled_qty
                        record.status = str(order.get("status", record.status))
                        record.filled_avg_price = as_float(order.get("filled_avg_price")) or None
                        record.raw = order
                        self._apply_fill_delta(db, record, order)
                        if (
                            record.strategy_id is None
                            and record.side == "sell"
                            and record.filled_qty > previous_filled_qty
                        ):
                            external_sell_fills.append((record.symbol, record.id))

                for pending_signal in pending_by_client_id.values():
                    payload = pending_signal.payload or {}
                    lookup_error = lookup_failures.get(pending_signal.id)
                    if lookup_error is not None:
                        attempts = int(payload.get("attempts") or 0) + 1
                        delay = min(300, 15 * (2 ** min(attempts - 1, 4)))
                        pending_signal.payload = {
                            **payload,
                            "error": str(lookup_error),
                            "attempts": attempts,
                            "last_attempt_at": reconciliation_now.isoformat(),
                            "next_attempt_at": (
                                reconciliation_now + timedelta(seconds=delay)
                            ).isoformat(),
                        }
                        continue
                    if pending_signal.id not in confirmed_missing:
                        continue
                    if pending_signal.status == "pending_submission":
                        # A paused engine is allowed to discover an order that a
                        # pre-crash POST already created, but a confirmed 404 is
                        # not permission to submit. Keep the durable intent for
                        # the next explicit engine resume.
                        pending_signal.payload = {
                            **payload,
                            "last_reconciliation_at": reconciliation_now.isoformat(),
                            "reconciliation_resolution": "not_found_without_submission",
                        }
                        continue
                    misses = int(payload.get("reconciliation_misses") or 0) + 1
                    pending_signal.payload = {
                        **payload,
                        "reconciliation_misses": misses,
                        "last_reconciliation_at": reconciliation_now.isoformat(),
                    }
                    created_at = pending_signal.created_at
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    if (
                        misses >= PENDING_RECONCILIATION_MIN_MISSES
                        and reconciliation_now - created_at
                        >= timedelta(seconds=PENDING_RECONCILIATION_TIMEOUT_SECONDS)
                    ):
                        pending_signal.status = "rejected"
                        pending_signal.payload = {
                            **pending_signal.payload,
                            "reconciliation_resolution": "not_found",
                        }
                        expired_pending.append(
                            (
                                pending_signal.symbol,
                                str(payload.get("client_order_id") or ""),
                            )
                        )
                db.commit()
            newly_quarantined: set[str] = set()
            for symbol, order_id in external_sell_fills:
                if self._activate_execution_incident(
                    symbol,
                    "REST 对账检测到未由 QuantPilot 跟踪的卖出增量",
                    trigger_order_id=order_id,
                    details={"source": "rest_reconciliation"},
                ):
                    newly_quarantined.add(symbol)
            for symbol, reason in self._audit_long_only_invariants(
                positions, orders
            ).items():
                if self._activate_execution_incident(
                    symbol,
                    reason,
                    details={"source": "long_only_invariant"},
                ):
                    newly_quarantined.add(symbol)
            for symbol in sorted(newly_quarantined):
                await self.log(
                    "critical",
                    "risk",
                    f"{symbol} 违反只做多执行约束，已暂停引擎并进入安全隔离",
                )
            had_incidents = bool(self._active_incident_symbols())
            if had_incidents:
                await self._contain_active_execution_incidents()
            else:
                await self._ensure_trailing_intents_for_filled_entries()
            for symbol, client_order_id in recovered_pending:
                await self.log(
                    "info",
                    "order",
                    f"{symbol} 不确定订单已通过 Alpaca 对账恢复",
                    {"client_order_id": client_order_id},
                )
            for symbol, client_order_id in expired_pending:
                await self.log(
                    "warning",
                    "order",
                    f"{symbol} 不确定订单经多次对账确认未找到，已解除交易阻塞",
                    {"client_order_id": client_order_id},
                )
        except Exception as exc:  # pragma: no cover - network dependent
            await self.log("warning", "connection", f"订单对账失败: {exc}")

    @asynccontextmanager
    async def connection_reconfiguration(
        self, reason: str
    ) -> AsyncIterator[None]:
        """Freeze intent creation and POSTs while Paper credentials are replaced."""
        completed = False
        async with self._order_submission_lock:
            with SessionLocal() as db:
                active_incident = db.scalar(
                    select(ExecutionIncident.id).where(
                        ExecutionIncident.user_id == self.user_id,
                        ExecutionIncident.status == "active",
                    )
                )
                if active_incident is not None or user_has_unresolved_execution(
                    db, self.user_id
                ):
                    raise ConnectionReconfigurationBlockedError()
                state = db.scalar(
                    select(EngineState).where(EngineState.user_id == self.user_id)
                )
                state.status = "paused"
                state.reason = reason
                state.day_start_equity = None
                state.daily_high_equity = None
                state.session_date = None
                db.commit()
            yield
            completed = True
        if completed:
            await self.log("warning", "engine", f"交易引擎已暂停：{reason}")
            await websocket_manager.broadcast(
                self.user_id,
                "engine",
                {"status": "paused", "reason": reason},
            )

    async def resume(self, reason: str = "用户开启") -> None:
        if not self.alpaca.configured:
            raise RuntimeError("请先配置 Alpaca Paper API 密钥")
        async with self._order_submission_lock:
            with SessionLocal() as db:
                active_incident = db.scalar(
                    select(ExecutionIncident.id).where(
                        ExecutionIncident.user_id == self.user_id,
                        ExecutionIncident.status == "active",
                    )
                )
                if active_incident is not None:
                    raise ExecutionQuarantineError()
                state = db.scalar(
                    select(EngineState).where(EngineState.user_id == self.user_id)
                )
                state.status = "running"
                state.reason = reason
                db.commit()
        await self.log("info", "engine", "交易引擎已开启")
        await websocket_manager.broadcast(self.user_id, "engine", {"status": "running"})

    async def disable_strategy(self, strategy_id: str) -> None:
        """Disable only at the same gate used by automatic order submissions."""
        async with self._order_submission_lock:
            with SessionLocal() as db:
                strategy = db.scalar(
                    select(Strategy).where(
                        Strategy.id == strategy_id,
                        Strategy.owner_user_id == self.user_id,
                    )
                )
                if strategy is None:
                    raise KeyError(strategy_id)
                if strategy_has_unresolved_execution(
                    db, self.user_id, strategy_id
                ):
                    raise StrategyExecutionActiveError()
                strategy.enabled = False
                db.commit()

    async def update_risk_settings(self, values: dict[str, Any]) -> None:
        """Apply global risk changes at the same boundary as order POSTs."""
        async with self._order_submission_lock:
            with SessionLocal() as db:
                settings = db.scalar(
                    select(RiskSettings).where(
                        RiskSettings.user_id == self.user_id
                    )
                )
                if settings is None:
                    raise RuntimeError("风险设置不存在")
                for key, value in values.items():
                    setattr(settings, key, value)
                db.commit()

    async def cancel_all_orders(self) -> list[Any]:
        """Cancel Paper orders without racing submits or credential changes."""
        async with self._order_submission_lock:
            return await asyncio.to_thread(self.alpaca.cancel_all_orders)

    async def pause(self, reason: str = "用户暂停", cancel_orders: bool = True) -> None:
        # Serialize pause with every automatic order POST. An already in-flight
        # submit finishes first and is then covered by cancel_all_orders; queued
        # submits re-check the persisted paused state and never reach Alpaca.
        cancel_error: Exception | None = None
        async with self._order_submission_lock:
            with SessionLocal() as db:
                state = db.scalar(
                    select(EngineState).where(EngineState.user_id == self.user_id)
                )
                state.status = "paused"
                state.reason = reason
                db.commit()
            if cancel_orders and self.alpaca.configured:
                try:
                    await asyncio.to_thread(self.alpaca.cancel_all_orders)
                except Exception as exc:
                    cancel_error = exc
        if cancel_error is not None:
            await self.log("warning", "order", f"取消订单时出现异常: {cancel_error}")
        await self.log("warning", "engine", f"交易引擎已暂停：{reason}")
        await websocket_manager.broadcast(
            self.user_id, "engine", {"status": "paused", "reason": reason}
        )

    async def emergency_liquidate(self, reason: str = "用户紧急平仓") -> list[Any]:
        # Keep the account-operation gate from the pause boundary through the
        # cancel-and-close request so resume or credential replacement cannot
        # redirect any part of the emergency action to another account.
        async with self._order_submission_lock:
            with SessionLocal() as db:
                state = db.scalar(
                    select(EngineState).where(EngineState.user_id == self.user_id)
                )
                state.status = "paused"
                state.reason = reason
                db.commit()
            result = await asyncio.to_thread(self.alpaca.close_all_positions)
            with SessionLocal() as db:
                db.query(StrategyPosition).filter(
                    StrategyPosition.user_id == self.user_id
                ).update({StrategyPosition.qty: 0.0})
                db.commit()
        await self.log("critical", "risk", "已向 Alpaca 模拟盘发送全部平仓指令")
        await websocket_manager.broadcast(self.user_id, "engine", {"status": "liquidating"})
        return result

    async def log(self, level: str, category: str, message: str, details: dict | None = None) -> None:
        with SessionLocal() as db:
            db.add(
                EventLog(
                    user_id=self.user_id,
                    level=level,
                    category=category,
                    message=message,
                    details=details or {},
                )
            )
            db.commit()
        await websocket_manager.broadcast(
            self.user_id,
            "log",
            {
                "level": level,
                "category": category,
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return True
