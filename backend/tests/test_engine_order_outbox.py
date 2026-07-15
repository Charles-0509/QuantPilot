from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import (
    EngineState,
    EventLog,
    ExecutionIncident,
    OrderRecord,
    RiskSettings,
    Signal,
    Strategy,
    StrategyPosition,
)
from app.services import engine as engine_module
from app.services import risk as risk_module
from app.services.alpaca_service import AlpacaAmbiguousOrderError
from app.services.engine import StrategyExecutionActiveError, TradingEngine


class SimulatedProcessCrash(BaseException):
    pass


class OutboxAlpaca:
    configured = True

    def __init__(self) -> None:
        self.remote_orders: dict[str, dict] = {}
        self.submit_calls = 0
        self.remote_accepts = 0
        self.crash_after_accept = False
        self.cancel_calls: list[str] = []
        self.cancel_failures: set[str] = set()
        self.exit_submit_calls = 0
        self.lookup_calls = 0
        self.connection_state = "connected"
        self.failed_operations: list[dict] = []
        self.quote_price = 100.0
        self.quote_calls = 0
        self.quote_timestamp = "2026-07-15T14:30:00+00:00"
        self.last_entry_kwargs: dict | None = None
        self.trailing_submit_calls = 0
        self.trailing_crash_after_accept = False
        self.ambiguous_entry_submit = False
        self.positions = [{"symbol": "SPY", "qty": "100"}]

    def stop_streams(self) -> None:
        return None

    def connection_status(self) -> dict:
        return {
            "state": self.connection_state,
            "connected": self.connection_state == "connected",
            "health": {
                "trading": {
                    "state": "open" if self.connection_state == "circuit_open" else "closed"
                },
                "failed_operations": list(self.failed_operations),
            },
        }

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        self.lookup_calls += 1
        order = self.remote_orders.get(client_order_id)
        return dict(order) if order is not None else None

    def get_asset(self, symbol: str) -> dict:
        return {"symbol": symbol, "tradable": True, "status": "active"}

    def get_latest_quotes(self, symbols: list[str]) -> dict:
        self.quote_calls += 1
        return {
            symbol: {
                "ask_price": str(self.quote_price),
                "timestamp": self.quote_timestamp,
            }
            for symbol in symbols
        }

    def get_orders(self, status: str = "all") -> list[dict]:
        return [dict(order) for order in self.remote_orders.values()]

    def get_open_orders_fresh(self) -> list[dict]:
        return self.get_orders("open")

    def get_positions(self) -> list[dict]:
        return [dict(position) for position in self.positions]

    def get_positions_fresh(self) -> list[dict]:
        return self.get_positions()

    def submit_entry_order(self, **kwargs) -> dict:
        self.submit_calls += 1
        self.last_entry_kwargs = dict(kwargs)
        if self.ambiguous_entry_submit:
            raise AlpacaAmbiguousOrderError(kwargs["client_order_id"])
        client_order_id = kwargs["client_order_id"]
        order = self.remote_orders.get(client_order_id)
        if order is None:
            self.remote_accepts += 1
            order = {
                "id": f"remote-{self.remote_accepts}",
                "client_order_id": client_order_id,
                "symbol": kwargs["symbol"],
                "side": "buy",
                "type": kwargs["order_type"],
                "qty": str(kwargs["qty"]),
                "filled_qty": "0",
                "status": "accepted",
            }
            self.remote_orders[client_order_id] = order
            if self.crash_after_accept:
                raise SimulatedProcessCrash("process stopped after Alpaca accepted")
        return dict(order)

    def cancel_order(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if order_id in self.cancel_failures:
            raise ConnectionError("simulated cancellation transport failure")

    def submit_exit_order(
        self, symbol: str, qty: float, client_order_id: str
    ) -> dict:
        self.exit_submit_calls += 1
        order = self.remote_orders.get(client_order_id)
        if order is None:
            self.remote_accepts += 1
            order = {
                "id": f"remote-{self.remote_accepts}",
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": "sell",
                "type": "market",
                "qty": str(qty),
                "filled_qty": "0",
                "status": "accepted",
            }
            self.remote_orders[client_order_id] = order
        return dict(order)

    def submit_trailing_stop(
        self,
        symbol: str,
        qty: float,
        mode: str,
        value: float,
        client_order_id: str,
    ) -> dict:
        self.trailing_submit_calls += 1
        order = self.remote_orders.get(client_order_id)
        if order is None:
            self.remote_accepts += 1
            order = {
                "id": f"remote-{self.remote_accepts}",
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": "sell",
                "type": "trailing_stop",
                "qty": str(qty),
                "filled_qty": "0",
                "status": "accepted",
                "trail_mode": mode,
                "trail_value": value,
            }
            self.remote_orders[client_order_id] = order
            if self.trailing_crash_after_accept:
                raise SimulatedProcessCrash("crash after trailing stop accepted")
        return dict(order)


def _definition() -> dict:
    condition = {
        "type": "condition",
        "left": {"kind": "price", "field": "close", "offset": 0},
        "operator": ">",
        "right": {"kind": "number", "value": 1_000_000},
        "label": "never",
    }
    return {
        "version": 1,
        "name": "Crash-safe strategy",
        "description": "",
        "symbols": ["SPY"],
        "timeframe": "15Min",
        "warmup_bars": 30,
        "schedule": {"session": "regular", "weekdays": [0, 1, 2, 3, 4]},
        "entry": {"type": "group", "op": "AND", "negate": False, "children": [condition]},
        "exit": {"type": "group", "op": "AND", "negate": False, "children": [condition]},
        "position": {
            "mode": "fixed_qty",
            "value": 1,
            "allow_pyramiding": False,
            "max_additions": 1,
        },
        "order": {
            "type": "market",
            "limit_offset_bps": 0,
            "time_in_force": "day",
            "stop_loss": None,
            "take_profit": None,
            "trailing_stop": None,
        },
        "risk": {"max_symbol_pct": 10, "max_positions": 8, "cooldown_bars": 1},
    }


def _sessions(tmp_path, monkeypatch):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'order-outbox.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    async def no_broadcast(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)
    now_ref = {"value": datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)}

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = now_ref["value"]
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(engine_module, "datetime", FixedDateTime)
    monkeypatch.setattr(risk_module, "datetime", FixedDateTime)
    with sessions() as db:
        db.add_all(
            [
                Strategy(
                id="strategy-1",
                owner_user_id=7,
                name="Crash-safe strategy",
                description="",
                is_template=False,
                enabled=True,
                    definition=_definition(),
                ),
                EngineState(user_id=7, status="running", reason="test"),
                RiskSettings(user_id=7),
            ]
        )
        db.commit()
    return database_engine, sessions, now_ref


def _engine(tmp_path, alpaca: OutboxAlpaca) -> TradingEngine:
    return TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )


def _reference_bars(
    signal: Signal,
    *,
    latest_timestamp: datetime | None = None,
    price: float = 100.0,
) -> dict[tuple[str, str], tuple[datetime, float, pd.DataFrame]]:
    timestamp = latest_timestamp or signal.bar_timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    frame = pd.DataFrame(
        {
            "open": [price] * 30,
            "high": [price + 1] * 30,
            "low": [price - 1] * 30,
            "close": [price] * 30,
            "volume": [1000.0] * 30,
        },
        index=pd.date_range(end=timestamp, periods=30, freq="15min"),
    )
    return {
        (str((signal.payload or {}).get("timeframe") or "15Min"), signal.symbol): (
            timestamp,
            price,
            frame,
        )
    }


async def _entry_intent(
    engine: TradingEngine,
    sessions,
    *,
    timestamp: datetime | None = None,
    timeframe: str = "15Min",
    qty: float = 1.0,
) -> Signal:
    with sessions() as db:
        strategy = db.get(Strategy, "strategy-1")
    timestamp = timestamp or datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    client_order_id = engine._client_order_id(
        strategy.id, "SPY", timestamp, "buy"
    )
    signal = await engine._persist_order_intent(
        strategy,
        "SPY",
        timestamp,
        "buy",
        100.0,
        "entry",
        {
            "intent_version": 1,
            "strategy_version": strategy.version,
            "client_order_id": client_order_id,
            "side": "buy",
            "qty": qty,
            "notional": qty * 100.0,
            "original_qty": qty,
            "original_notional": qty * 100.0,
            "order_type": "market",
            "time_in_force": "day",
            "limit_price": 100.0,
            "stop_price": None,
            "take_price": None,
            "timeframe": timeframe,
        },
    )
    assert signal is not None
    return signal


@pytest.mark.asyncio
async def test_restart_drains_intent_persisted_before_submit_once(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    first_engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(first_engine, sessions)

    # Simulate the process ending immediately after the outbox commit and before
    # entering AlpacaService.submit_entry_order.
    restarted_engine = _engine(tmp_path, alpaca)
    open_orders: list[dict] = []
    context = {
        "account": {"equity": "100000", "buying_power": "200000"},
        "positions": [],
        "clock": {"is_open": True},
        "reference_bars": _reference_bars(signal),
    }
    await restarted_engine._resume_pending_submissions(open_orders, **context)
    await restarted_engine._resume_pending_submissions(open_orders, **context)

    with sessions() as db:
        saved = db.get(Signal, signal.id)
        records = db.scalars(select(OrderRecord)).all()
    assert saved is not None and saved.status == "submitted"
    assert len(records) == 1
    assert alpaca.submit_calls == 1
    assert alpaca.remote_accepts == 1
    database_engine.dispose()


@pytest.mark.asyncio
async def test_pause_during_entry_revalidation_blocks_the_final_post(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    revalidation_started = asyncio.Event()
    allow_revalidation_to_finish = asyncio.Event()

    async def delayed_revalidation(*_args, **_kwargs):
        revalidation_started.set()
        await allow_revalidation_to_finish.wait()
        return (
            "allow",
            "允许",
            False,
            {
                "qty": 1.0,
                "notional": 100.0,
                "risk_reference_price": 100.0,
                "stop_price": None,
                "take_price": None,
            },
        )

    monkeypatch.setattr(engine, "_revalidate_pending_entry", delayed_revalidation)
    submit_task = asyncio.create_task(
        engine._submit_pending_signal(
            signal.id,
            [],
            account={"equity": "100000", "buying_power": "200000"},
            positions=[],
            clock={"is_open": True},
            reference_bars=_reference_bars(signal),
            recover_existing=False,
        )
    )
    await revalidation_started.wait()
    await engine.pause("concurrent pause", cancel_orders=False)
    allow_revalidation_to_finish.set()
    assert await submit_task is False

    with sessions() as db:
        state = db.scalar(select(EngineState).where(EngineState.user_id == 7))
        saved = db.get(Signal, signal.id)
    assert state is not None and state.status == "paused"
    assert saved is not None and saved.status == "pending_submission"
    assert alpaca.submit_calls == 0
    assert alpaca.remote_accepts == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_disable_during_entry_revalidation_is_rejected_until_execution_settles(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    revalidation_started = asyncio.Event()
    allow_revalidation_to_finish = asyncio.Event()

    async def delayed_revalidation(*_args, **_kwargs):
        revalidation_started.set()
        await allow_revalidation_to_finish.wait()
        return (
            "allow",
            "允许",
            False,
            {
                "qty": 1.0,
                "notional": 100.0,
                "risk_reference_price": 100.0,
                "stop_price": None,
                "take_price": None,
            },
        )

    monkeypatch.setattr(engine, "_revalidate_pending_entry", delayed_revalidation)
    submit_task = asyncio.create_task(
        engine._submit_pending_signal(
            signal.id,
            [],
            account={"equity": "100000", "buying_power": "200000"},
            positions=[],
            clock={"is_open": True},
            reference_bars=_reference_bars(signal),
            recover_existing=False,
        )
    )
    await revalidation_started.wait()
    with pytest.raises(StrategyExecutionActiveError):
        await engine.disable_strategy("strategy-1")
    allow_revalidation_to_finish.set()
    assert await submit_task is None

    with sessions() as db:
        strategy = db.get(Strategy, "strategy-1")
        saved = db.get(Signal, signal.id)
    assert strategy is not None and strategy.enabled is True
    assert saved is not None and saved.status == "submitted"
    assert alpaca.submit_calls == 1
    database_engine.dispose()


@pytest.mark.asyncio
async def test_risk_change_after_revalidation_blocks_post_and_rechecks_next_cycle(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    revalidation_started = asyncio.Event()
    allow_revalidation_to_finish = asyncio.Event()
    with sessions() as db:
        settings = db.scalar(select(RiskSettings).where(RiskSettings.user_id == 7))
        old_risk_snapshot = engine._risk_settings_snapshot(settings)
    original_revalidation = engine._revalidate_pending_entry

    async def delayed_revalidation(*_args, **_kwargs):
        revalidation_started.set()
        await allow_revalidation_to_finish.wait()
        return (
            "allow",
            "允许",
            False,
            {
                "qty": 1.0,
                "notional": 100.0,
                "risk_reference_price": 100.0,
                "stop_price": None,
                "take_price": None,
                "risk_settings_snapshot": old_risk_snapshot,
            },
        )

    monkeypatch.setattr(engine, "_revalidate_pending_entry", delayed_revalidation)
    submit_task = asyncio.create_task(
        engine._submit_pending_signal(
            signal.id,
            [],
            account={"equity": "100000", "buying_power": "200000"},
            positions=[],
            clock={"is_open": True},
            reference_bars=_reference_bars(signal),
            recover_existing=False,
        )
    )
    await revalidation_started.wait()
    await engine.update_risk_settings(
        {**old_risk_snapshot, "max_symbol_pct": 0.05}
    )
    allow_revalidation_to_finish.set()
    assert await submit_task is False
    assert alpaca.submit_calls == 0

    monkeypatch.setattr(
        engine, "_revalidate_pending_entry", original_revalidation
    )
    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )
    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "rejected"
    assert alpaca.submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_final_sell_capacity_gate_blocks_oversell_with_manual_open_order(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    alpaca.positions = [{"symbol": "SPY", "qty": "10"}]
    alpaca.remote_orders["manual-sell-client"] = {
        "id": "manual-open-sell",
        "client_order_id": "manual-sell-client",
        "symbol": "SPY",
        "side": "sell",
        "type": "limit",
        "qty": "6",
        "filled_qty": "0",
        "status": "new",
    }
    engine = _engine(tmp_path, alpaca)
    with sessions() as db:
        strategy = db.get(Strategy, "strategy-1")
        db.add(
            StrategyPosition(
                user_id=7,
                strategy_id=strategy.id,
                symbol="SPY",
                qty=5,
            )
        )
        db.commit()
    timestamp = datetime(2026, 7, 15, 14, 15, tzinfo=timezone.utc)
    signal = await engine._persist_order_intent(
        strategy,
        "SPY",
        timestamp,
        "sell",
        100,
        "exit",
        {
            "intent_version": 1,
            "strategy_version": strategy.version,
            "client_order_id": engine._client_order_id(
                strategy.id, "SPY", timestamp, "sell"
            ),
            "side": "sell",
            "qty": 5,
            "notional": None,
            "order_type": "market",
            "time_in_force": "day",
            "cancel_strategy_orders": True,
            "timeframe": "15Min",
        },
    )
    assert signal is not None

    assert await engine._submit_pending_signal(
        signal.id,
        [],
        positions=[{"symbol": "SPY", "qty": "10"}],
        recover_existing=False,
    ) is False

    with sessions() as db:
        saved = db.get(Signal, signal.id)
        incident = db.scalar(select(ExecutionIncident))
        state = db.scalar(select(EngineState).where(EngineState.user_id == 7))
    assert saved is not None and saved.status == "cancelled"
    assert incident is not None and incident.status == "active"
    assert state is not None and state.status == "paused"
    assert alpaca.exit_submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_restart_after_remote_accept_reuses_client_id_without_duplicate(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    alpaca.crash_after_accept = True

    with pytest.raises(SimulatedProcessCrash):
        await engine._submit_pending_signal(
            signal.id,
            [],
            account={"equity": "100000", "buying_power": "200000"},
            positions=[],
            clock={"is_open": True},
            reference_bars=_reference_bars(signal),
            recover_existing=False,
        )

    with sessions() as db:
        crashed = db.get(Signal, signal.id)
        assert crashed is not None and crashed.status == "pending_submission"
        assert db.scalars(select(OrderRecord)).all() == []

    alpaca.crash_after_accept = False
    restarted_engine = _engine(tmp_path, alpaca)
    await restarted_engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )
    await restarted_engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        recovered = db.get(Signal, signal.id)
        records = db.scalars(select(OrderRecord)).all()
    assert recovered is not None and recovered.status == "submitted"
    assert len(records) == 1
    assert records[0].client_order_id == signal.payload["client_order_id"]
    assert alpaca.submit_calls == 1
    assert alpaca.lookup_calls == 1
    assert alpaca.remote_accepts == 1
    assert len(alpaca.remote_orders) == 1
    database_engine.dispose()


@pytest.mark.asyncio
async def test_exit_waits_until_every_strategy_order_is_cancelled(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    with sessions() as db:
        strategy = db.get(Strategy, "strategy-1")
        db.add_all(
            [
                StrategyPosition(
                    user_id=7,
                    strategy_id=strategy.id,
                    symbol="SPY",
                    qty=2,
                ),
                OrderRecord(
                    id="open-a",
                    user_id=7,
                    client_order_id="open-client-a",
                    strategy_id=strategy.id,
                    signal_id=None,
                    symbol="SPY",
                    side="sell",
                    order_type="limit",
                    qty=1,
                    notional=None,
                    status="new",
                ),
                OrderRecord(
                    id="open-b",
                    user_id=7,
                    client_order_id="open-client-b",
                    strategy_id=strategy.id,
                    signal_id=None,
                    symbol="SPY",
                    side="sell",
                    order_type="stop",
                    qty=1,
                    notional=None,
                    status="new",
                ),
            ]
        )
        db.commit()

    timestamp = datetime(2026, 7, 15, 14, 15, tzinfo=timezone.utc)
    client_order_id = engine._client_order_id(
        strategy.id, "SPY", timestamp, "sell"
    )
    signal = await engine._persist_order_intent(
        strategy,
        "SPY",
        timestamp,
        "sell",
        99.0,
        "exit",
        {
            "intent_version": 1,
            "client_order_id": client_order_id,
            "side": "sell",
            "qty": 2.0,
            "notional": None,
            "order_type": "market",
            "time_in_force": "day",
            "cancel_strategy_orders": True,
            "timeframe": "15Min",
        },
    )
    assert signal is not None
    alpaca.cancel_failures = {"open-b"}
    open_orders = [
        {"id": "open-a", "symbol": "SPY", "side": "sell", "status": "new"},
        {"id": "open-b", "symbol": "SPY", "side": "sell", "status": "new"},
    ]

    await engine._submit_pending_signal(
        signal.id,
        open_orders,
        positions=[{"symbol": "SPY", "qty": "2"}],
    )

    with sessions() as db:
        waiting = db.get(Signal, signal.id)
    assert waiting is not None and waiting.status == "pending_submission"
    assert waiting.payload["cancel_failed_order_ids"] == ["open-b"]
    assert alpaca.exit_submit_calls == 0
    assert [order["id"] for order in open_orders] == ["open-b"]

    alpaca.cancel_failures.clear()
    now_ref["value"] = datetime(2026, 7, 15, 14, 31, tzinfo=timezone.utc)
    await engine._resume_pending_submissions(
        [{"id": "open-b", "symbol": "SPY", "side": "sell", "status": "new"}],
        positions=[{"symbol": "SPY", "qty": "2"}],
        clock={"is_open": True},
    )

    with sessions() as db:
        submitted = db.get(Signal, signal.id)
        records = db.scalars(
            select(OrderRecord).where(OrderRecord.signal_id == signal.id)
        ).all()
    assert submitted is not None and submitted.status == "submitted"
    assert alpaca.exit_submit_calls == 1
    assert len(records) == 1
    database_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("local_change", ["pause", "disable"])
async def test_remote_order_is_recovered_even_if_engine_paused_or_strategy_disabled(
    tmp_path, monkeypatch, local_change
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    alpaca.crash_after_accept = True
    with pytest.raises(SimulatedProcessCrash):
        await engine._submit_pending_signal(
            signal.id,
            [],
            account={"equity": "100000", "buying_power": "200000"},
            positions=[],
            clock={"is_open": True},
            reference_bars=_reference_bars(signal),
            recover_existing=False,
        )

    with sessions() as db:
        if local_change == "pause":
            db.scalar(select(EngineState).where(EngineState.user_id == 7)).status = "paused"
        else:
            db.get(Strategy, "strategy-1").enabled = False
        db.commit()
    alpaca.crash_after_accept = False

    await _engine(tmp_path, alpaca)._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        recovered = db.get(Signal, signal.id)
        records = db.scalars(select(OrderRecord)).all()
    assert recovered is not None and recovered.status == "submitted"
    assert len(records) == 1
    assert alpaca.remote_accepts == 1
    database_engine.dispose()


@pytest.mark.asyncio
async def test_paused_engine_does_not_issue_new_post(tmp_path, monkeypatch) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    with sessions() as db:
        db.scalar(select(EngineState).where(EngineState.user_id == 7)).status = "paused"
        db.commit()

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "pending_submission"
    assert alpaca.lookup_calls == 1
    assert alpaca.submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_disabled_strategy_without_remote_order_cancels_intent(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    with sessions() as db:
        db.get(Strategy, "strategy-1").enabled = False
        db.commit()

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "cancelled"
    assert alpaca.lookup_calls == 1
    assert alpaca.submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_risk_limit_change_and_price_jump_block_replayed_entry(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    alpaca.quote_price = 200.0
    engine = _engine(tmp_path, alpaca)
    with sessions() as db:
        strategy = db.get(Strategy, "strategy-1")
        definition = dict(strategy.definition)
        definition["position"] = {**definition["position"], "value": 60}
        strategy.definition = definition
        db.commit()
    signal = await _entry_intent(engine, sessions, qty=60)
    with sessions() as db:
        db.scalar(
            select(RiskSettings).where(RiskSettings.user_id == 7)
        ).max_symbol_pct = 5
        db.commit()

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "rejected"
    assert saved.payload["resolution"] == "pre_submit_revalidation"
    assert alpaca.submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_non_closing_intraday_signal_expires_overnight(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    now_ref["value"] = datetime(2026, 7, 16, 13, 35, tzinfo=timezone.utc)

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "rejected"
    assert alpaca.submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timestamp", "timeframe", "next_open"),
    [
        (
            datetime(2026, 7, 14, 19, 55, tzinfo=timezone.utc),
            "5Min",
            datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 7, 10, 19, 55, tzinfo=timezone.utc),
            "5Min",
            datetime(2026, 7, 13, 13, 35, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc),
            "1Day",
            datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc),
        ),
    ],
)
async def test_closing_and_daily_signals_carry_to_next_regular_session(
    tmp_path, monkeypatch, timestamp, timeframe, next_open
) -> None:
    database_engine, sessions, now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    now_ref["value"] = timestamp + timedelta(minutes=10)
    signal = await _entry_intent(
        engine, sessions, timestamp=timestamp, timeframe=timeframe
    )
    now_ref["value"] = next_open
    alpaca.quote_timestamp = next_open.isoformat()

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "submitted", saved.payload if saved else None
    assert alpaca.submit_calls == 1
    database_engine.dispose()


@pytest.mark.asyncio
async def test_nine_pending_intents_do_not_touch_sdk_while_circuit_is_open(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    alpaca.connection_state = "circuit_open"
    engine = _engine(tmp_path, alpaca)
    for index in range(9):
        await _entry_intent(
            engine,
            sessions,
            timestamp=datetime(2026, 7, 15, 14, index, tzinfo=timezone.utc),
        )

    await engine._resume_pending_submissions([])
    await engine._resume_pending_submissions([])

    with sessions() as db:
        events = db.scalars(
            select(EventLog).where(
                EventLog.user_id == 7,
                EventLog.message.contains("9 个待提交订单意图"),
            )
        ).all()
    assert alpaca.lookup_calls == 0
    assert alpaca.submit_calls == 0
    assert len(events) == 1
    database_engine.dispose()


@pytest.mark.asyncio
async def test_first_ambiguous_submit_stops_remaining_pending_orders(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    timestamp = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    with sessions() as db:
        for index in range(2, 10):
            db.add(
                Strategy(
                    id=f"strategy-{index}",
                    owner_user_id=7,
                    name=f"Strategy {index}",
                    description="",
                    is_template=False,
                    enabled=True,
                    version=1,
                    definition=_definition(),
                )
            )
        db.commit()
        strategies = db.scalars(
            select(Strategy).where(Strategy.owner_user_id == 7)
        ).all()

    alpaca = OutboxAlpaca()
    alpaca.ambiguous_entry_submit = True
    engine = _engine(tmp_path, alpaca)
    signals: list[Signal] = []
    for strategy in strategies:
        client_order_id = engine._client_order_id(
            strategy.id, "SPY", timestamp, "buy"
        )
        signal = await engine._persist_order_intent(
            strategy,
            "SPY",
            timestamp,
            "buy",
            100,
            "entry",
            {
                "intent_version": 1,
                "strategy_version": strategy.version,
                "client_order_id": client_order_id,
                "side": "buy",
                "qty": 1.0,
                "notional": 100.0,
                "original_qty": 1.0,
                "original_notional": 100.0,
                "order_type": "market",
                "time_in_force": "day",
                "limit_price": 100.0,
                "stop_price": None,
                "take_price": None,
                "timeframe": "15Min",
            },
        )
        assert signal is not None
        signals.append(signal)

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signals[0]),
    )

    with sessions() as db:
        statuses = [db.get(Signal, signal.id).status for signal in signals]
    assert alpaca.submit_calls == 1
    assert statuses.count("pending_reconciliation") == 1
    assert statuses.count("pending_submission") == 8
    database_engine.dispose()


def test_client_order_id_hashes_the_full_unique_identity(tmp_path) -> None:
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    timestamp = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    first = engine._client_order_id("abcdefgh-one", "VERYLONGSYMBOL", timestamp, "buy")
    second = engine._client_order_id("abcdefgh-two", "VERYLONGSYMBOL", timestamp, "buy")

    assert first != second
    assert len(first) <= 48
    assert first.isascii() and second.isascii()


@pytest.mark.asyncio
async def test_strategy_edit_cannot_expand_persisted_intent(tmp_path, monkeypatch) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions, qty=1)
    with sessions() as db:
        strategy = db.get(Strategy, "strategy-1")
        definition = dict(strategy.definition)
        definition["position"] = {**definition["position"], "value": 100}
        strategy.definition = definition
        strategy.version += 1
        db.commit()

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "rejected"
    assert alpaca.submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_stale_quote_keeps_intent_waiting_without_post(tmp_path, monkeypatch) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    alpaca.quote_timestamp = "2026-07-15T13:00:00+00:00"
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "pending_submission"
    assert saved.payload.get("next_attempt_at")
    assert alpaca.submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_gap_open_fixed_notional_shrinks_quantity_and_reprices_bracket(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    with sessions() as db:
        strategy = db.get(Strategy, "strategy-1")
        definition = dict(strategy.definition)
        definition["position"] = {
            **definition["position"],
            "mode": "fixed_notional",
            "value": 6000,
        }
        definition["order"] = {
            **definition["order"],
            "stop_loss": {"mode": "percent", "value": 5, "atr_period": 14},
            "take_profit": {"mode": "percent", "value": 10, "atr_period": 14},
        }
        strategy.definition = definition
        db.commit()
    alpaca = OutboxAlpaca()
    alpaca.quote_price = 120.0
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions, qty=60)
    with sessions() as db:
        saved = db.get(Signal, signal.id)
        payload = dict(saved.payload)
        payload.update({"stop_price": 95.0, "take_price": 110.0})
        saved.payload = payload
        db.commit()

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal),
    )

    with sessions() as db:
        submitted = db.get(Signal, signal.id)
    assert submitted is not None and submitted.status == "submitted"
    assert alpaca.last_entry_kwargs is not None
    assert alpaca.last_entry_kwargs["qty"] == pytest.approx(50.0)
    assert alpaca.last_entry_kwargs["stop_price"] == pytest.approx(114.0)
    assert alpaca.last_entry_kwargs["take_price"] == pytest.approx(132.0)
    database_engine.dispose()


@pytest.mark.asyncio
async def test_newer_completed_bar_expires_carried_closing_signal(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, now_ref = _sessions(tmp_path, monkeypatch)
    timestamp = datetime(2026, 7, 14, 19, 55, tzinfo=timezone.utc)
    next_bar = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    alpaca = OutboxAlpaca()
    alpaca.quote_timestamp = "2026-07-15T13:35:00+00:00"
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(
        engine, sessions, timestamp=timestamp, timeframe="5Min"
    )
    now_ref["value"] = datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc)

    await engine._resume_pending_submissions(
        [],
        account={"equity": "100000", "buying_power": "200000"},
        positions=[],
        clock={"is_open": True},
        reference_bars=_reference_bars(signal, latest_timestamp=next_bar),
    )

    with sessions() as db:
        saved = db.get(Signal, signal.id)
    assert saved is not None and saved.status == "rejected"
    assert alpaca.submit_calls == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_trailing_stop_intent_recovers_after_remote_accept_crash(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    with sessions() as db:
        saved = db.get(Signal, signal.id)
        saved.status = "submitted"
        db.add(
            StrategyPosition(
                user_id=7,
                strategy_id="strategy-1",
                symbol="SPY",
                qty=2,
            )
        )
        db.commit()
    engine._persist_trailing_intent(
        signal.id, "strategy-1", "SPY", 2, "percent", 5
    )
    alpaca.trailing_crash_after_accept = True

    with pytest.raises(SimulatedProcessCrash):
        await engine._submit_pending_trailing_intent(
            signal.id, recover_existing=False
        )

    with sessions() as db:
        crashed = db.get(Signal, signal.id)
        assert crashed.payload["trailing_stop_intent"]["status"] == "pending_submission"
        assert db.scalars(
            select(OrderRecord).where(OrderRecord.order_type == "trailing_stop")
        ).all() == []

    alpaca.trailing_crash_after_accept = False
    await _engine(tmp_path, alpaca)._resume_pending_trailing_intents()
    await _engine(tmp_path, alpaca)._resume_pending_trailing_intents()

    with sessions() as db:
        recovered = db.get(Signal, signal.id)
        records = db.scalars(
            select(OrderRecord).where(OrderRecord.order_type == "trailing_stop")
        ).all()
    assert recovered.payload["trailing_stop_intent"]["status"] == "submitted"
    assert len(records) == 1
    assert alpaca.trailing_submit_calls == 1
    assert alpaca.remote_accepts == 1
    database_engine.dispose()


@pytest.mark.asyncio
async def test_rest_reconciliation_backfills_missing_trailing_stop(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions, _now_ref = _sessions(tmp_path, monkeypatch)
    alpaca = OutboxAlpaca()
    engine = _engine(tmp_path, alpaca)
    signal = await _entry_intent(engine, sessions)
    with sessions() as db:
        signal_row = db.get(Signal, signal.id)
        signal_row.status = "submitted"
        signal_payload = dict(signal_row.payload)
        signal_payload["trailing_stop"] = {"mode": "percent", "value": 5}
        signal_row.payload = signal_payload
        db.add(
            OrderRecord(
                id="filled-entry",
                user_id=7,
                client_order_id=signal.payload["client_order_id"],
                strategy_id="strategy-1",
                signal_id=signal.id,
                symbol="SPY",
                side="buy",
                order_type="market",
                qty=2,
                notional=200,
                status="accepted",
                filled_qty=0,
            )
        )
        db.commit()
    alpaca.remote_orders[signal.payload["client_order_id"]] = {
        "id": "filled-entry",
        "client_order_id": signal.payload["client_order_id"],
        "symbol": "SPY",
        "side": "buy",
        "type": "market",
        "qty": "2",
        "filled_qty": "1",
        "filled_avg_price": "100",
        "status": "partially_filled",
    }

    await engine.reconcile_orders()
    await engine.reconcile_orders()

    with sessions() as db:
        position = db.scalar(select(StrategyPosition))
        trailing_records = db.scalars(
            select(OrderRecord).where(OrderRecord.order_type == "trailing_stop")
        ).all()
    assert position is not None and position.qty == 1
    assert trailing_records == []
    assert alpaca.trailing_submit_calls == 0

    alpaca.remote_orders[signal.payload["client_order_id"]].update(
        {"filled_qty": "2", "status": "filled"}
    )
    await engine.reconcile_orders()
    await engine.reconcile_orders()

    with sessions() as db:
        position = db.scalar(select(StrategyPosition))
        trailing_records = db.scalars(
            select(OrderRecord).where(OrderRecord.order_type == "trailing_stop")
        ).all()
    assert position is not None and position.qty == 2
    assert len(trailing_records) == 1
    assert alpaca.trailing_submit_calls == 1
    database_engine.dispose()
