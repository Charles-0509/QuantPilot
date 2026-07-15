import asyncio
import threading

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import pytest

from app import database
from app.config import Settings
from app.database import Base
from app.models import (
    EngineState,
    ExecutionIncident,
    OrderRecord,
    Strategy,
    StrategyPosition,
)
from app.services import engine as engine_module
from app.services.engine import ExecutionQuarantineError, TradingEngine


class FakeAlpaca:
    configured = False

    def stop_streams(self) -> None:
        return None


class SafetyAlpaca(FakeAlpaca):
    configured = True

    def __init__(self, *, cancel_fails: bool = False) -> None:
        self.positions = [{"symbol": "SPY", "qty": "4"}]
        self.open_orders = [
            {
                "id": "qp-trailing",
                "client_order_id": "qp-t-safety",
                "symbol": "SPY",
                "side": "sell",
                "type": "trailing_stop",
                "qty": "5",
                "filled_qty": "0",
                "status": "new",
            }
        ]
        self.all_orders = list(self.open_orders)
        self.cancel_fails = cancel_fails
        self.cancel_calls: list[str] = []

    def get_orders(self, status: str = "all") -> list[dict]:
        source = self.open_orders if status == "open" else self.all_orders
        return [dict(order) for order in source]

    def get_open_orders_fresh(self) -> list[dict]:
        return self.get_orders("open")

    def get_positions(self) -> list[dict]:
        return [dict(position) for position in self.positions]

    def get_positions_fresh(self) -> list[dict]:
        return self.get_positions()

    def cancel_order(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self.cancel_fails:
            raise ConnectionError("cancel response lost")
        self.open_orders = [
            order for order in self.open_orders if order["id"] != order_id
        ]


def test_fill_deltas_create_strategy_owned_positions_idempotently(tmp_path, monkeypatch) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'engine-safety.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        FakeAlpaca(),
        user_id=7,
    )

    with sessions() as db:
        buy = OrderRecord(
            id="buy-order",
            user_id=7,
            client_order_id="buy-client",
            strategy_id="strategy-1",
            signal_id="signal-1",
            symbol="SPY",
            side="buy",
            order_type="market",
            qty=10,
            notional=5000,
            status="partially_filled",
        )
        db.add(buy)
        db.flush()
        engine._apply_fill_delta(db, buy, {"filled_qty": "4"})
        engine._apply_fill_delta(db, buy, {"filled_qty": "4"})
        engine._apply_fill_delta(db, buy, {"filled_qty": "10"})
        db.commit()

        position = db.scalar(select(StrategyPosition))
        assert position is not None
        assert position.qty == 10
        assert buy.filled_qty == 10

        sell = OrderRecord(
            id="sell-order",
            user_id=7,
            client_order_id="sell-client",
            strategy_id="strategy-1",
            signal_id="signal-2",
            symbol="SPY",
            side="sell",
            order_type="market",
            qty=3,
            notional=None,
            status="filled",
        )
        db.add(sell)
        db.flush()
        engine._apply_fill_delta(db, sell, {"filled_qty": "3"})
        db.commit()
        assert position.qty == 7
        assert engine._owned_qty("strategy-1", "SPY") == 7

    database_engine.dispose()


@pytest.mark.asyncio
async def test_untracked_manual_sell_is_quarantined_and_cancels_qp_sell_orders(
    tmp_path, monkeypatch
) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'manual-sell.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    async def no_broadcast(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)
    alpaca = SafetyAlpaca()
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )
    with sessions() as db:
        db.add_all(
            [
                Strategy(
                    id="strategy-1",
                    owner_user_id=7,
                    name="Safety strategy",
                    description="",
                    is_template=False,
                    enabled=True,
                    definition={},
                ),
                EngineState(user_id=7, status="running", reason="test"),
                StrategyPosition(
                    user_id=7, strategy_id="strategy-1", symbol="SPY", qty=5
                ),
                OrderRecord(
                    id="qp-trailing",
                    user_id=7,
                    client_order_id="qp-t-safety",
                    strategy_id="strategy-1",
                    signal_id=None,
                    symbol="SPY",
                    side="sell",
                    order_type="trailing_stop",
                    qty=5,
                    notional=None,
                    status="new",
                ),
            ]
        )
        db.commit()

    await engine.handle_trade_update(
        {
            "event": "fill",
            "order": {
                "id": "manual-order",
                "symbol": "SPY",
                "side": "sell",
                "status": "filled",
                "filled_qty": "1",
            },
        }
    )

    with sessions() as db:
        position = db.scalar(select(StrategyPosition))
        state = db.scalar(select(EngineState).where(EngineState.user_id == 7))
        incident = db.scalar(select(ExecutionIncident))
        assert position is not None and position.qty == 0
        assert state is not None and state.status == "paused"
        assert incident is not None and incident.status == "contained"
    assert alpaca.cancel_calls == ["qp-trailing"]
    database_engine.dispose()


@pytest.mark.asyncio
async def test_active_manual_sell_quarantine_blocks_resume_until_fresh_cancel_confirmed(
    tmp_path, monkeypatch
) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'manual-sell-retry.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    async def no_broadcast(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)
    alpaca = SafetyAlpaca(cancel_fails=True)
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )
    with sessions() as db:
        db.add_all(
            [
                Strategy(
                    id="strategy-1",
                    owner_user_id=7,
                    name="Safety strategy",
                    description="",
                    is_template=False,
                    enabled=True,
                    definition={},
                ),
                EngineState(user_id=7, status="running", reason="test"),
                StrategyPosition(
                    user_id=7, strategy_id="strategy-1", symbol="SPY", qty=5
                ),
                OrderRecord(
                    id="qp-trailing",
                    user_id=7,
                    client_order_id="qp-t-safety",
                    strategy_id="strategy-1",
                    signal_id=None,
                    symbol="SPY",
                    side="sell",
                    order_type="trailing_stop",
                    qty=5,
                    notional=None,
                    status="new",
                ),
            ]
        )
        db.commit()

    await engine.handle_trade_update(
        {
            "event": "fill",
            "order": {
                "id": "manual-order",
                "client_order_id": "manual-order-client",
                "symbol": "SPY",
                "side": "sell",
                "status": "filled",
                "qty": "1",
                "filled_qty": "1",
            },
        }
    )
    with pytest.raises(ExecutionQuarantineError):
        await engine.resume("unsafe resume")

    alpaca.cancel_fails = False
    assert await engine._contain_active_execution_incidents() is True
    await engine.resume("confirmed safe")
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        state = db.scalar(select(EngineState).where(EngineState.user_id == 7))
    assert incident is not None and incident.status == "contained"
    assert state is not None and state.status == "running"
    database_engine.dispose()


@pytest.mark.asyncio
async def test_rest_reconciliation_detects_manual_sell_when_stream_was_missed(
    tmp_path, monkeypatch
) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'manual-sell-rest.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    async def no_broadcast(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)
    alpaca = SafetyAlpaca()
    alpaca.all_orders.append(
        {
            "id": "manual-rest-sell",
            "client_order_id": "manual-rest-client",
            "symbol": "SPY",
            "side": "sell",
            "type": "market",
            "qty": "1",
            "filled_qty": "1",
            "status": "filled",
        }
    )
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )
    with sessions() as db:
        db.add_all(
            [
                Strategy(
                    id="strategy-1",
                    owner_user_id=7,
                    name="Safety strategy",
                    description="",
                    is_template=False,
                    enabled=True,
                    definition={},
                ),
                EngineState(user_id=7, status="running", reason="test"),
                StrategyPosition(
                    user_id=7, strategy_id="strategy-1", symbol="SPY", qty=5
                ),
                OrderRecord(
                    id="qp-trailing",
                    user_id=7,
                    client_order_id="qp-t-safety",
                    strategy_id="strategy-1",
                    signal_id=None,
                    symbol="SPY",
                    side="sell",
                    order_type="trailing_stop",
                    qty=5,
                    notional=None,
                    status="new",
                ),
            ]
        )
        db.commit()

    await engine.reconcile_orders()

    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        state = db.scalar(select(EngineState).where(EngineState.user_id == 7))
        position = db.scalar(select(StrategyPosition))
        external = db.get(OrderRecord, "manual-rest-sell")
    assert incident is not None and incident.status == "contained"
    assert state is not None and state.status == "paused"
    assert position is not None and position.qty == 0
    assert external is not None and external.strategy_id is None
    assert alpaca.cancel_calls == ["qp-trailing"]
    database_engine.dispose()


@pytest.mark.asyncio
async def test_pause_persists_hard_boundary_before_cancel_request(
    tmp_path, monkeypatch
) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'pause-boundary.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    async def no_broadcast(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)

    observed: list[str] = []

    class PausingAlpaca(FakeAlpaca):
        configured = True

        def cancel_all_orders(self) -> list[object]:
            with sessions() as db:
                state = db.scalar(select(EngineState).where(EngineState.user_id == 7))
                observed.append(state.status)
            return []

    with sessions() as db:
        db.add(EngineState(user_id=7, status="running", reason="test"))
        db.commit()

    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        PausingAlpaca(),
        user_id=7,
    )
    await engine.pause("test pause", cancel_orders=True)

    with sessions() as db:
        state = db.scalar(select(EngineState).where(EngineState.user_id == 7))
        assert state is not None
        assert state.status == "paused"
        assert state.reason == "test pause"
    assert observed == ["paused"]
    database_engine.dispose()


@pytest.mark.asyncio
async def test_emergency_liquidation_holds_gate_until_close_request_finishes(
    tmp_path, monkeypatch
) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'emergency-gate.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    async def no_broadcast(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)
    close_started = threading.Event()
    allow_close = threading.Event()

    class BlockingCloseAlpaca(FakeAlpaca):
        configured = True

        def close_all_positions(self) -> list[object]:
            close_started.set()
            assert allow_close.wait(timeout=2)
            return []

    with sessions() as db:
        db.add(EngineState(user_id=7, status="running", reason="test"))
        db.commit()
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        BlockingCloseAlpaca(),
        user_id=7,
    )

    liquidation = asyncio.create_task(engine.emergency_liquidate("test emergency"))
    assert await asyncio.to_thread(close_started.wait, 1)
    resume = asyncio.create_task(engine.resume("resume after emergency"))
    await asyncio.sleep(0.05)
    assert resume.done() is False
    allow_close.set()
    await liquidation
    await resume

    with sessions() as db:
        state = db.scalar(select(EngineState).where(EngineState.user_id == 7))
    assert state is not None and state.status == "running"
    database_engine.dispose()
