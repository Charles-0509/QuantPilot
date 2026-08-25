import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import RiskSettings, Strategy, EngineState
from app.config import Settings
import app.services.engine as engine_module
from app.services.engine import TradingEngine


def test_cash_sweep_settings_defaults(tmp_path, monkeypatch):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'test-cash-sweep.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    with sessions() as db:
        risk = RiskSettings(user_id=1)
        db.add(risk)
        db.commit()
        db.refresh(risk)
        assert risk.cash_sweep_enabled is False
        assert risk.cash_sweep_symbol == "SGOV"
        assert risk.cash_sweep_buffer_pct == 2.0

@pytest.mark.asyncio
async def test_cash_sweep_liquidation_called_when_enabled(tmp_path, monkeypatch):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'test-cash-sweep-2.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    class DummyAlpaca:
        configured = True
        submitted_exits = []
        submitted_entries = []

        def get_positions(self):
            return [{"symbol": "SGOV", "qty": "50", "current_price": "100.5"}]

        def get_open_orders_fresh(self):
            return []

        def get_orders(self, status="open"):
            return []

        def submit_exit_order(self, symbol, qty, order_type, time_in_force, client_order_id):
            self.submitted_exits.append({"symbol": symbol, "qty": qty, "client_order_id": client_order_id})
            return {"id": "exit-1", "symbol": symbol, "qty": qty, "status": "accepted"}

        def submit_entry_order(self, symbol, qty, notional, order_type, time_in_force, client_order_id, **kwargs):
            self.submitted_entries.append({"symbol": symbol, "qty": qty, "client_order_id": client_order_id})
            return {"id": "entry-1", "symbol": symbol, "qty": qty, "status": "accepted"}

    alpaca = DummyAlpaca()
    engine = TradingEngine(Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")), alpaca, user_id=1)

    with sessions() as db:
        risk = RiskSettings(user_id=1, cash_sweep_enabled=True, cash_sweep_symbol="SGOV", cash_sweep_buffer_pct=2.0)
        db.add(risk)
        db.commit()

    # When buying $1000 of AAPL, it should sell (1000 * 1.02 / 100.5) = ~11 shares of SGOV
    await engine._execute_cash_sweep_liquidation_if_needed(risk, "AAPL", 1000.0)
    assert len(alpaca.submitted_exits) == 1
    assert alpaca.submitted_exits[0]["symbol"] == "SGOV"
    assert alpaca.submitted_exits[0]["qty"] == 11.0
