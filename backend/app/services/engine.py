from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    EngineState,
    EventLog,
    MarketBar,
    OrderRecord,
    RiskSettings,
    Signal,
    Strategy,
    WatchlistItem,
)
from app.schemas import RuleDefinition

from .alpaca_service import AlpacaService
from .indicators import latest_atr
from .risk import RiskManager, as_float
from .rules import evaluate_latest
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


class TradingEngine:
    def __init__(self, settings: Settings, alpaca: AlpacaService, user_id: int = 1):
        self.settings = settings
        self.alpaca = alpaca
        self.user_id = user_id
        self.risk = RiskManager()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_evaluated: dict[tuple[str, str], str] = {}
        self._last_reconcile = datetime.min.replace(tzinfo=timezone.utc)
        self._stream_signature: tuple[str, ...] = ()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name=f"trading-engine-{self.user_id}")

    async def shutdown(self) -> None:
        self._stop.set()
        self.alpaca.stop_streams()
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
                    if running:
                        await self.evaluate_active_strategies()
                    if datetime.now(timezone.utc) - self._last_reconcile > timedelta(seconds=60):
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
        )[:30]
        signature = tuple(symbols)
        if signature != self._stream_signature:
            await self.alpaca.start_streams(symbols, self.handle_bar_update, self.handle_trade_update)
            self._stream_signature = signature

    async def handle_bar_update(self, payload: dict[str, Any]) -> None:
        await websocket_manager.broadcast(self.user_id, "market_bar", payload)

    async def handle_trade_update(self, payload: dict[str, Any]) -> None:
        event = str(payload.get("event", "update"))
        order = payload.get("order") or {}
        order_id = str(order.get("id", ""))
        with SessionLocal() as db:
            record = db.scalar(
                select(OrderRecord).where(
                    OrderRecord.id == order_id, OrderRecord.user_id == self.user_id
                )
            ) if order_id else None
            if record:
                record.status = str(order.get("status", event))
                filled = order.get("filled_avg_price")
                record.filled_avg_price = as_float(filled) if filled is not None else None
                record.raw = payload
                db.commit()
                if event in {"fill", "filled"} and record.side == "buy" and record.strategy_id:
                    strategy = db.get(Strategy, record.strategy_id)
                    if strategy:
                        definition = RuleDefinition.model_validate(strategy.definition)
                        trailing = definition.order.trailing_stop
                        if trailing:
                            qty = as_float(order.get("filled_qty") or order.get("qty"))
                            client_id = f"trail-{record.client_order_id}"[:48]
                            try:
                                trailing_order = self.alpaca.submit_trailing_stop(
                                    record.symbol, qty, trailing.mode, trailing.value, client_id
                                )
                                db.add(
                                    OrderRecord(
                                        user_id=self.user_id,
                                        id=str(trailing_order["id"]),
                                        client_order_id=str(trailing_order.get("client_order_id", client_id)),
                                        strategy_id=record.strategy_id,
                                        signal_id=record.signal_id,
                                        symbol=record.symbol,
                                        side="sell",
                                        order_type="trailing_stop",
                                        qty=qty,
                                        notional=None,
                                        status=str(trailing_order.get("status", "accepted")),
                                        raw=trailing_order,
                                    )
                                )
                                db.commit()
                            except Exception as exc:  # pragma: no cover - network dependent
                                await self.log("error", "order", f"创建移动止损失败: {exc}")
        await websocket_manager.broadcast(self.user_id, "trade_update", payload)

    async def evaluate_active_strategies(self) -> None:
        with SessionLocal() as db:
            strategies = db.scalars(
                select(Strategy).where(Strategy.enabled.is_(True), Strategy.is_template.is_(False))
                .where(Strategy.owner_user_id == self.user_id)
            ).all()
        for strategy in strategies:
            try:
                await self.evaluate_strategy(strategy.id)
            except Exception as exc:
                await self.log("error", "strategy", f"策略 {strategy.name} 执行失败: {exc}")

    async def evaluate_strategy(self, strategy_id: str) -> None:
        with SessionLocal() as db:
            strategy = db.scalar(
                select(Strategy).where(
                    Strategy.id == strategy_id, Strategy.owner_user_id == self.user_id
                )
            )
            if strategy is None or not strategy.enabled:
                return
            definition = RuleDefinition.model_validate(strategy.definition)

        clock = self.alpaca.get_clock()
        if not bool(clock.get("is_open")):
            return
        now_et = datetime.now(ET)
        if now_et.weekday() not in definition.schedule.weekdays:
            return

        bars_by_symbol = self.alpaca.recent_bars(
            definition.symbols, definition.timeframe, definition.warmup_bars + 20
        )
        positions = self.alpaca.get_positions()
        position_map = {str(position.get("symbol")): position for position in positions}
        account = self.alpaca.get_account()

        for symbol, frame in bars_by_symbol.items():
            if frame.empty or len(frame) < 3:
                continue
            # Daily bars can contain the still-forming current session bar; only evaluate completed bars.
            if definition.timeframe == "1Day" and frame.index[-1].tz_convert(ET).date() == now_et.date():
                frame = frame.iloc[:-1]
            if frame.empty:
                continue
            latest_timestamp = frame.index[-1]
            latest_key = latest_timestamp.isoformat()
            key = (strategy_id, symbol)
            if self._last_evaluated.get(key) == latest_key:
                continue
            self._last_evaluated[key] = latest_key
            self.cache_bars(symbol, definition.timeframe, frame.tail(5))

            entry = evaluate_latest(frame, definition.entry)
            exit_ = evaluate_latest(frame, definition.exit)
            has_position = symbol in position_map and as_float(position_map[symbol].get("qty")) > 0

            if has_position and exit_.matched:
                await self._create_exit_signal(strategy, definition, symbol, frame, exit_)
                continue
            can_pyramid = definition.position.allow_pyramiding and await self._cooldown_passed(
                strategy.id, symbol, definition
            )
            if entry.matched and (not has_position or can_pyramid):
                await self._create_entry_signal(
                    strategy, definition, symbol, frame, entry, account, positions, clock
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

        asset = self.alpaca.get_asset(symbol)
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
            )
            db.commit()
        if not decision.allowed:
            await self.log("warning", "risk", f"{strategy.name}/{symbol}: {decision.reason}")
            if decision.halt_engine:
                await self.pause(decision.reason, cancel_orders=True)
            return

        signal = await self._insert_signal(
            strategy,
            symbol,
            frame.index[-1].to_pydatetime(),
            "buy",
            price,
            "；".join(result.labels) or "入场条件成立",
        )
        if signal is None:
            return
        client_id = f"qp-u{self.user_id}-{strategy.id[:8]}-{symbol}-{int(frame.index[-1].timestamp())}"[:48]
        limit_price = price * (1 - definition.order.limit_offset_bps / 10_000)
        try:
            order = self.alpaca.submit_entry_order(
                symbol=symbol,
                qty=qty,
                notional=None,
                order_type=definition.order.type,
                time_in_force=definition.order.time_in_force,
                client_order_id=client_id,
                limit_price=limit_price,
                stop_price=stop_price,
                take_price=take_price,
            )
            with SessionLocal() as db:
                db.add(
                    OrderRecord(
                        user_id=self.user_id,
                        id=str(order["id"]),
                        client_order_id=str(order.get("client_order_id", client_id)),
                        strategy_id=strategy.id,
                        signal_id=signal.id,
                        symbol=symbol,
                        side="buy",
                        order_type=definition.order.type,
                        qty=qty,
                        notional=desired_notional,
                        status=str(order.get("status", "accepted")),
                        raw=order,
                    )
                )
                saved_signal = db.get(Signal, signal.id)
                saved_signal.status = "submitted"
                db.commit()
            await self.log("info", "order", f"{strategy.name}: 已提交 {symbol} 模拟买单")
            await websocket_manager.broadcast(
                self.user_id,
                "signal",
                {"action": "buy", "symbol": symbol, "strategy": strategy.name},
            )
        except Exception as exc:
            with SessionLocal() as db:
                saved_signal = db.get(Signal, signal.id)
                if saved_signal:
                    saved_signal.status = "rejected"
                    saved_signal.payload = {"error": str(exc)}
                    db.commit()
            await self.log("error", "order", f"{symbol} 下单失败: {exc}")

    async def _create_exit_signal(self, strategy, definition, symbol, frame, result) -> None:
        price = float(frame.iloc[-1]["close"])
        signal = await self._insert_signal(
            strategy,
            symbol,
            frame.index[-1].to_pydatetime(),
            "sell",
            price,
            "；".join(result.labels) or "离场条件成立",
        )
        if signal is None:
            return
        try:
            order = self.alpaca.close_position(symbol)
            with SessionLocal() as db:
                db.add(
                    OrderRecord(
                        user_id=self.user_id,
                        id=str(order["id"]),
                        client_order_id=str(order.get("client_order_id", f"exit-{signal.id[:12]}")),
                        strategy_id=strategy.id,
                        signal_id=signal.id,
                        symbol=symbol,
                        side="sell",
                        order_type=str(order.get("type", "market")),
                        qty=as_float(order.get("qty")),
                        notional=None,
                        status=str(order.get("status", "accepted")),
                        raw=order,
                    )
                )
                saved_signal = db.get(Signal, signal.id)
                saved_signal.status = "submitted"
                db.commit()
            await self.log("info", "order", f"{strategy.name}: 已提交 {symbol} 模拟平仓")
        except Exception as exc:
            await self.log("error", "order", f"{symbol} 平仓失败: {exc}")

    async def _insert_signal(
        self, strategy: Strategy, symbol: str, timestamp: datetime, action: str, price: float, reason: str
    ) -> Signal | None:
        unique_key = f"{self.user_id}:{strategy.id}:{symbol}:{timestamp.isoformat()}:{action}"
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
            )
            db.add(signal)
            try:
                db.commit()
                db.refresh(signal)
                return signal
            except IntegrityError:
                db.rollback()
                return None

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

    async def reconcile_orders(self) -> None:
        if not self.alpaca.configured:
            return
        try:
            orders = self.alpaca.get_orders("all")
            with SessionLocal() as db:
                for order in orders:
                    order_id = str(order.get("id"))
                    record = db.scalar(
                        select(OrderRecord).where(
                            OrderRecord.id == order_id,
                            OrderRecord.user_id == self.user_id,
                        )
                    )
                    if record:
                        record.status = str(order.get("status", record.status))
                        record.filled_avg_price = as_float(order.get("filled_avg_price")) or None
                        record.raw = order
                db.commit()
        except Exception as exc:  # pragma: no cover - network dependent
            await self.log("warning", "connection", f"订单对账失败: {exc}")

    async def resume(self, reason: str = "用户开启") -> None:
        if not self.alpaca.configured:
            raise RuntimeError("请先配置 Alpaca Paper API 密钥")
        with SessionLocal() as db:
            state = db.scalar(select(EngineState).where(EngineState.user_id == self.user_id))
            state.status = "running"
            state.reason = reason
            db.commit()
        await self.log("info", "engine", "交易引擎已开启")
        await websocket_manager.broadcast(self.user_id, "engine", {"status": "running"})

    async def pause(self, reason: str = "用户暂停", cancel_orders: bool = True) -> None:
        if cancel_orders and self.alpaca.configured:
            try:
                self.alpaca.cancel_all_orders()
            except Exception as exc:
                await self.log("warning", "order", f"取消订单时出现异常: {exc}")
        with SessionLocal() as db:
            state = db.scalar(select(EngineState).where(EngineState.user_id == self.user_id))
            state.status = "paused"
            state.reason = reason
            db.commit()
        await self.log("warning", "engine", f"交易引擎已暂停：{reason}")
        await websocket_manager.broadcast(
            self.user_id, "engine", {"status": "paused", "reason": reason}
        )

    async def emergency_liquidate(self, reason: str = "用户紧急平仓") -> list[Any]:
        await self.pause(reason, cancel_orders=True)
        result = self.alpaca.close_all_positions()
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
