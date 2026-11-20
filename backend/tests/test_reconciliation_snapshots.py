"""REST observations must not turn an in-flight, bounded exit into a quarantine."""
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import EngineState, ExecutionIncident, OrderRecord, StrategyPosition
from app.services import engine as engine_module
from app.services.engine import StrategyOrderCancellationError, TradingEngine


class SnapshotBroker:
    configured = True

    def __init__(self):
        self.order = dict(id='exit', client_order_id='qp-s-exit', symbol='NVDA',
                          side='sell', type='market', qty='6', filled_qty='2',
                          status='partially_filled')
        self.positions = [dict(symbol='NVDA', qty='2')]
        self.reads = 0
        self.cancel_calls = []
        self.fill_race = False

    def get_orders(self, status='all'):
        return [deepcopy(self.order)]

    def get_open_orders_fresh(self):
        self.reads += 1
        # The position endpoint has already reflected four executions while the
        # first two order reads still report only two (the incident's torn pair).
        if self.fill_race and self.reads >= 3:
            self.order['filled_qty'] = '4'
        return [deepcopy(self.order)] if self.order['status'] != 'canceled' else []

    def get_positions_fresh(self):
        return deepcopy(self.positions)

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        self.order['status'] = 'canceled'


@pytest.fixture
def audit_engine(tmp_path, monkeypatch):
    db_engine = create_engine(f"sqlite:///{tmp_path / 'snapshots.db'}")
    Base.metadata.create_all(db_engine)
    sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, 'SessionLocal', sessions)

    async def no_broadcast(*args, **kwargs):
        pass

    monkeypatch.setattr(engine_module.websocket_manager, 'broadcast', no_broadcast)
    broker = SnapshotBroker()
    engine = TradingEngine(Settings(_env_file=None), broker, user_id=7)
    with sessions() as db:
        db.add(EngineState(user_id=7, status='running'))
        db.add(StrategyPosition(user_id=7, strategy_id='s', symbol='NVDA', qty=6))
        db.add(OrderRecord(id='exit', user_id=7, strategy_id='s',
                           client_order_id='qp-s-exit', symbol='NVDA', side='sell',
                           order_type='market', qty=6, filled_qty=0, status='new'))
        db.commit()
    yield engine, broker, sessions
    db_engine.dispose()


@pytest.mark.asyncio
async def test_partial_exit_torn_pair_retries_without_cancel_or_ownership_reset(audit_engine):
    engine, broker, sessions = audit_engine
    broker.fill_race = True
    await engine.reconcile_orders()
    with sessions() as db:
        assert db.scalar(select(ExecutionIncident)) is None
        assert db.scalar(select(EngineState)).status == 'running'
        assert db.scalar(select(StrategyPosition)).qty == 2
        assert db.get(OrderRecord, 'exit').filled_qty == 4
    assert broker.cancel_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('unsafe', ['oversell', 'short', 'ownership'])
async def test_unexplained_violations_quarantine_without_partial_fill_grace(audit_engine, unsafe):
    engine, broker, sessions = audit_engine
    if unsafe == 'oversell':
        broker.order['qty'] = '8'  # Never covered by the six owned shares.
    elif unsafe == 'short':
        broker.positions[0]['qty'] = '-1'
    else:
        broker.order['status'] = 'canceled'
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        assert incident is not None
        assert db.scalar(select(EngineState)).status == 'paused'
        assert db.scalar(select(StrategyPosition)).qty == 0
        assert incident.details['actual_qty'] == (-1 if unsafe == 'short' else 2)
        assert incident.details['snapshot_started_at']
        assert incident.details['positions_observed_at']
        assert incident.details['orders_observed_at']
        assert incident.details['snapshot_completed_at']
        if unsafe == 'oversell':
            assert incident.details['open_orders'][0]['remaining_qty'] == 6
    # One audit pair; containment performs its own independent safety reads.
    assert broker.reads <= 4


@pytest.mark.asyncio
async def test_persistent_explainable_mismatch_is_bounded_and_keeps_evidence(audit_engine):
    engine, broker, sessions = audit_engine
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        assert incident is not None
        assert incident.details['snapshot_attempts'] == engine_module.INVARIANT_SNAPSHOT_ATTEMPTS
        assert len(incident.details['snapshot_history']) == engine_module.INVARIANT_SNAPSHOT_ATTEMPTS
    assert broker.cancel_calls == ['exit']


@pytest.mark.asyncio
@pytest.mark.parametrize('response_lost', [True, False])
async def test_bracket_cancel_failure_rechecks_final_state_and_retries(audit_engine, response_lost):
    engine, broker, sessions = audit_engine
    broker.order.update(id='take', type='limit', status='new', qty='6', filled_qty='0',
                        parent_order_id='entry', order_class='bracket')
    with sessions() as db:
        db.add(OrderRecord(id='entry', user_id=7, strategy_id='s', client_order_id='qp-b-entry',
                           symbol='NVDA', side='buy', order_type='market', qty=6,
                           filled_qty=6, status='filled'))
        db.commit()
    original_cancel = broker.cancel_order

    def cancel(order_id):
        if not broker.cancel_calls:
            broker.cancel_calls.append(order_id)
            if response_lost:
                broker.order['status'] = 'canceled'
            raise ConnectionError('first cancel response lost')
        original_cancel(order_id)

    broker.cancel_order = cancel
    orders = broker.get_open_orders_fresh()
    await engine._cancel_strategy_symbol_orders('s', 'NVDA', orders)
    assert orders == []
    assert broker.cancel_calls == (['take'] if response_lost else ['take', 'take'])
    await engine._cancel_strategy_symbol_orders('s', 'NVDA', orders)
    assert broker.cancel_calls == (['take'] if response_lost else ['take', 'take'])
    with sessions() as db:
        assert db.scalar(select(ExecutionIncident)) is None


@pytest.mark.asyncio
async def test_cancel_failure_with_still_open_leg_blocks_exit(audit_engine):
    engine, broker, sessions = audit_engine

    def fail(order_id):
        broker.cancel_calls.append(order_id)
        raise ConnectionError('cancel unavailable')

    broker.cancel_order = fail
    orders = broker.get_open_orders_fresh()
    with pytest.raises(StrategyOrderCancellationError):
        await engine._cancel_strategy_symbol_orders('s', 'NVDA', orders)
    assert [order['id'] for order in orders] == ['exit']
    with sessions() as db:
        assert db.scalar(select(ExecutionIncident)) is None


@pytest.mark.asyncio
async def test_exit_disappearing_from_open_list_refreshes_terminal_fill(audit_engine):
    engine, broker, sessions = audit_engine
    broker.positions[0]['qty'] = '1'
    calls = []

    def all_orders(status='all'):
        calls.append(status)
        if len(calls) > 1:
            broker.order.update(status='canceled', filled_qty='5')
        return [deepcopy(broker.order)]

    broker.get_orders = all_orders
    broker.get_open_orders_fresh = lambda: []
    await engine.reconcile_orders()
    with sessions() as db:
        assert db.scalar(select(ExecutionIncident)) is None
        assert db.scalar(select(StrategyPosition)).qty == 1
        assert db.get(OrderRecord, 'exit').filled_qty == 5
    assert broker.cancel_calls == []


def test_canceled_bracket_duplicate_does_not_reserve_stale_nested_leg():
    leg = dict(id='take', symbol='NVDA', side='sell', qty='6', filled_qty='0', status='new')
    parent = dict(id='entry', symbol='NVDA', side='buy', qty='6', filled_qty='6',
                  status='filled', order_class='bracket', legs=[leg])
    canceled = {**leg, 'status': 'canceled'}
    assert TradingEngine._reserved_sell_qty([parent, canceled], 'NVDA') == 0
    assert TradingEngine._reserved_sell_qty([canceled, parent], 'NVDA') == 0


@pytest.mark.asyncio
async def test_external_fill_observed_during_retry_does_not_receive_more_grace(audit_engine):
    engine, broker, sessions = audit_engine
    with sessions() as db:
        db.add(OrderRecord(id='manual', user_id=7, client_order_id='manual', symbol='NVDA',
                           side='sell', order_type='market', qty=2, filled_qty=0, status='new'))
        db.commit()
    original = broker.get_orders
    reads = []

    def all_orders(status='all'):
        reads.append(status)
        orders = original(status)
        if len(reads) > 1:
            orders.append(dict(id='manual', client_order_id='manual', symbol='NVDA',
                               side='sell', type='market', qty=2, filled_qty=2, status='filled'))
        return orders

    broker.get_orders = all_orders
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        assert incident.details['external_order_id'] == 'manual'
        assert incident.details['snapshot_attempts'] == 2
        assert '未由 QuantPilot' in incident.reason
        assert db.scalar(select(EngineState)).status == 'paused'


def test_incident_api_exposes_only_current_users_evidence(audit_engine):
    from app.api import execution_incident_summaries
    _, _, sessions = audit_engine
    with sessions() as db:
        db.add_all([
            ExecutionIncident(user_id=7, symbol='NVDA', reason='audit', status='contained',
                              details={'actual_qty': 2, 'open_orders': [{'id': 'exit', 'remaining_qty': 4}]}),
            ExecutionIncident(user_id=8, symbol='PRIVATE', reason='other account', status='active'),
        ])
        db.commit()
        rows = execution_incident_summaries(db, 7)
        assert [row['symbol'] for row in rows] == ['NVDA']
        assert rows[0]['details']['open_orders'][0]['remaining_qty'] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', [False, True])
async def test_newer_all_order_fill_corrects_lagging_or_missing_open_order(audit_engine, terminal):
    engine, broker, sessions = audit_engine
    initial = deepcopy(broker.order)
    if terminal:
        initial['filled_qty'] = '0'
        broker.positions[0]['qty'] = '0'
    reads = []

    def all_orders(status='all'):
        reads.append(status)
        return [{**initial, 'filled_qty': ('6' if terminal else '4') if len(reads) > 1 else initial['filled_qty'],
                 'status': 'filled' if terminal and len(reads) > 1 else 'partially_filled'}]

    broker.get_orders = all_orders
    broker.get_open_orders_fresh = lambda: [] if terminal else [deepcopy(initial)]
    await engine.reconcile_orders()
    with sessions() as db:
        assert db.scalar(select(ExecutionIncident)) is None
        assert db.scalar(select(EngineState)).status == 'running'
        assert db.scalar(select(StrategyPosition)).qty == (0 if terminal else 2)
    assert broker.cancel_calls == []


@pytest.mark.asyncio
async def test_ambiguous_snapshot_blocks_concurrent_submission_until_resolved(audit_engine, monkeypatch):
    import asyncio
    engine, broker, sessions = audit_engine
    waiting = asyncio.Event()
    continue_audit = asyncio.Event()
    original_sleep = asyncio.sleep

    async def grace_delay(seconds):
        waiting.set()
        await continue_audit.wait()

    monkeypatch.setattr(engine_module.asyncio, 'sleep', grace_delay)
    reconciliation = asyncio.create_task(engine.reconcile_orders())
    await asyncio.wait_for(waiting.wait(), timeout=2)
    acquired = []

    async def competing_submission():
        async with engine._order_submission_lock:
            acquired.append(True)

    submission = asyncio.create_task(competing_submission())
    await original_sleep(0)
    assert acquired == []
    with sessions() as db:
        assert db.scalar(select(StrategyPosition)).qty == 4
        assert db.scalar(select(EngineState)).status == 'running'
    broker.fill_race = True
    broker.reads = 2
    continue_audit.set()
    await asyncio.wait_for(reconciliation, timeout=2)
    await asyncio.wait_for(submission, timeout=2)
    assert acquired == [True]
    assert broker.cancel_calls == []


@pytest.mark.asyncio
async def test_stale_canceled_leg_input_does_not_cancel_it_again(audit_engine):
    engine, broker, _ = audit_engine
    stale_orders = [deepcopy(broker.order)]
    broker.order['status'] = 'canceled'
    await engine._cancel_strategy_symbol_orders('s', 'NVDA', stale_orders)
    assert stale_orders == []
    assert broker.cancel_calls == []


@pytest.fixture
def entry_lag_engine(tmp_path, monkeypatch):
    """Nov 12: a tracked bracket buy reached the ledger before Alpaca positions."""
    from datetime import datetime, timezone
    db_engine = create_engine(f"sqlite:///{tmp_path / 'entry-lag.db'}")
    Base.metadata.create_all(db_engine)
    sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, 'SessionLocal', sessions)

    async def no_broadcast(*args, **kwargs):
        pass

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(engine_module.websocket_manager, 'broadcast', no_broadcast)
    monkeypatch.setattr(engine_module.asyncio, 'sleep', no_sleep)
    filled_at = datetime.now(timezone.utc).isoformat()

    class Broker:
        configured = True

        def __init__(self):
            self.positions = [dict(symbol='GOOGL', qty='9')]
            self.cancel_calls = []
            self.buy = dict(id='entry', client_order_id='qp-b-entry', symbol='GOOGL', side='buy', type='market',
                            qty='10', filled_qty='10', status='filled', order_class='bracket', filled_at=filled_at)
            self.old_stop = dict(id='old-stop', parent_order_id='old-entry', symbol='GOOGL', side='sell',
                                 type='stop', qty='4', filled_qty='0', status='new', order_class='bracket')

        def get_orders(self, status='all'):
            return [deepcopy(self.buy), deepcopy(self.old_stop)]

        def get_open_orders_fresh(self):
            return [deepcopy(self.old_stop)]

        def get_positions_fresh(self):
            return deepcopy(self.positions)

        def cancel_order(self, order_id):
            self.cancel_calls.append(order_id)

    broker = Broker()
    engine = TradingEngine(Settings(_env_file=None), broker, user_id=7)
    with sessions() as db:
        db.add(EngineState(user_id=7, status='running', reason='用户开启'))
        db.add_all([StrategyPosition(user_id=7, strategy_id='old', symbol='GOOGL', qty=4),
                    StrategyPosition(user_id=7, strategy_id='new', symbol='GOOGL', qty=10)])
        db.add_all([OrderRecord(id='entry', user_id=7, strategy_id='new', client_order_id='qp-b-entry', symbol='GOOGL',
                                side='buy', order_type='market', qty=10, filled_qty=10, status='filled'),
                    OrderRecord(id='old-entry', user_id=7, strategy_id='old', client_order_id='qp-b-old', symbol='GOOGL',
                                side='buy', order_type='market', qty=4, filled_qty=4, status='filled')])
        db.commit()
    yield engine, broker, sessions
    db_engine.dispose()


@pytest.mark.asyncio
async def test_entry_fill_lag_holds_without_destruction_then_auto_recovers(entry_lag_engine):
    engine, broker, sessions = entry_lag_engine
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        state = db.scalar(select(EngineState))
        assert incident.status == 'active' and incident.details['destructive'] is False
        assert incident.details['kind'] == 'entry_fill_lag'
        assert state.status == 'paused' and state.reason.startswith(engine_module.HOLD_REASON_PREFIX)
        assert sum(row.qty for row in db.scalars(select(StrategyPosition))) == 14
    assert broker.cancel_calls == []

    broker.positions[0]['qty'] = '14'
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        state = db.scalar(select(EngineState))
        assert incident.status == 'contained' and incident.details['resolution'] == 'auto_recovered_consistent'
        assert state.status == 'running'
        assert sum(row.qty for row in db.scalars(select(StrategyPosition))) == 14
    assert broker.cancel_calls == []


@pytest.mark.asyncio
async def test_entry_lag_that_never_catches_up_escalates_to_quarantine(entry_lag_engine, monkeypatch):
    engine, broker, sessions = entry_lag_engine
    await engine.reconcile_orders()
    monkeypatch.setattr(engine_module, 'OWNERSHIP_LAG_ESCALATE_SECONDS', 0)
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        assert incident.details['destructive'] is True and 'escalated_at' in incident.details
        assert db.scalar(select(EngineState)).status == 'paused'
        assert sum(row.qty for row in db.scalars(select(StrategyPosition))) == 0


@pytest.mark.asyncio
async def test_old_buy_does_not_excuse_ownership_mismatch(entry_lag_engine):
    engine, broker, sessions = entry_lag_engine
    broker.buy['filled_at'] = '2026-11-01T14:45:47+00:00'
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        assert incident.details['destructive'] is True
        assert sum(row.qty for row in db.scalars(select(StrategyPosition))) == 0
