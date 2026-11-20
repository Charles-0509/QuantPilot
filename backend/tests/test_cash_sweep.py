import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import RiskSettings, EngineState
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

        def submit_exit_order(self, symbol: str, qty: float, client_order_id: str):
            self.submitted_exits.append({"symbol": symbol, "qty": qty, "client_order_id": client_order_id})
            return {"id": "exit-1", "symbol": symbol, "qty": qty, "status": "accepted"}

        def submit_entry_order(self, *, symbol: str, qty: float | None, notional: float | None, order_type: str, time_in_force: str, client_order_id: str, **kwargs):
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


@pytest.mark.asyncio
@pytest.mark.parametrize('engine_status', ['running', 'paused'])
async def test_cash_sweep_investment_called_when_enabled(tmp_path, monkeypatch, engine_status):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'test-cash-sweep-3.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    class DummyAlpaca:
        configured = True
        submitted_entries = []

        def get_clock(self):
            return {"is_open": True}

        def get_account(self):
            return {"cash": "1000.0", "buying_power": "1000.0"}

        def get_open_orders_fresh(self):
            return []

        def get_orders(self, status="open"):
            return []

        def get_latest_quotes(self, symbols):
            return {"SGOV": {"ask_price": 100.0}}

        def submit_entry_order(self, *, symbol: str, qty: float | None, notional: float | None, order_type: str, time_in_force: str, client_order_id: str, **kwargs):
            self.submitted_entries.append({"symbol": symbol, "qty": qty, "client_order_id": client_order_id})
            return {"id": "entry-1", "symbol": symbol, "qty": qty, "status": "accepted"}

    alpaca = DummyAlpaca()
    engine = TradingEngine(Settings(_env_file=None, investor_db_path=str(tmp_path / 'unused.db')), alpaca, user_id=1)

    with sessions() as db:
        risk = RiskSettings(user_id=1, cash_sweep_enabled=True, cash_sweep_symbol="SGOV", cash_sweep_buffer_pct=2.0,
                            cash_sweep_min_trade_usd=0)
        db.add(risk)
        db.add(EngineState(user_id=1, status=engine_status))
        db.commit()

    # Cash = 1000, invest_cash = 1000 - 50 = 950, price = 100 => buy 9 shares
    await engine._execute_cash_sweep_investment(risk)
    assert len(alpaca.submitted_entries) == (1 if engine_status == 'running' else 0)
    if engine_status == 'running':
        assert alpaca.submitted_entries[0]["symbol"] == "SGOV"
        assert alpaca.submitted_entries[0]["qty"] == 9


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['paused', 'cooldown', 'minimum', 'ready'])
async def test_cash_sweep_controls_are_enforced_before_any_buy(tmp_path, monkeypatch, control):
    from datetime import datetime, timedelta, timezone
    from app.models import EventLog
    database_engine = create_engine(f"sqlite:///{tmp_path / 'sweep-controls.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    class Broker:
        configured = True

        def __init__(self):
            self.entries = []

        def get_clock(self):
            return {"is_open": True}

        def get_account(self):
            return {"cash": "15000"}

        def get_open_orders_fresh(self):
            return []

        def get_latest_quotes(self, symbols):
            return {"SGOV": {"ask_price": 100.0}}

        def submit_entry_order(self, **kwargs):
            self.entries.append(kwargs)
            return {"id": "sweep", "status": "accepted"}

    broker = Broker()
    engine = TradingEngine(Settings(_env_file=None), broker, user_id=1)
    now = datetime.now(timezone.utc)
    with sessions() as db:
        risk = RiskSettings(user_id=1, cash_sweep_enabled=True, cash_sweep_cooldown_minutes=60,
                            cash_sweep_min_trade_usd=20000 if control == 'minimum' else 1000,
                            cash_sweep_paused_until=now + timedelta(days=3) if control == 'paused' else None)
        db.add_all([risk, EngineState(user_id=1, status='running')])
        if control == 'cooldown':
            db.add(EventLog(user_id=1, level='info', category='order', created_at=now - timedelta(minutes=5),
                            message='[现金管理] 买入 SPY 自动市价卖出 147.0 股 SGOV 释放资金'))
        db.commit()

    status = engine.cash_sweep_status(risk)
    await engine._execute_cash_sweep_investment(risk)
    assert status['state'] == ('ready' if control in {'minimum', 'ready'} else control)
    assert len(broker.entries) == (1 if control == 'ready' else 0)
