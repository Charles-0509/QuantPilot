from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import pytest

from app import database
from app.config import Settings
from app.database import Base
from app.models import OrderRecord, StrategyPosition
from app.services import engine as engine_module
from app.services.engine import TradingEngine


class FakeAlpaca:
    configured = False

    def stop_streams(self) -> None:
        return None


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
async def test_untracked_manual_sell_clears_strategy_ownership(tmp_path, monkeypatch) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'manual-sell.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    async def no_broadcast(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        FakeAlpaca(),
        user_id=7,
    )
    with sessions() as db:
        db.add(
            StrategyPosition(
                user_id=7, strategy_id="strategy-1", symbol="SPY", qty=5
            )
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
        assert position is not None and position.qty == 0
    database_engine.dispose()
