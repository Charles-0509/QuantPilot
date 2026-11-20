from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import EngineState, RiskSettings, Strategy, StrategyPosition
from app.services import portfolio as portfolio_module
from app.services.engine import ET, TradingEngine
from app.services.portfolio import build_portfolio, enabled_strategy_count
from app.services.risk import RiskManager


def sessions_for(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class Broker:
    configured = True
    broker_name = 'alpaca'
    paper = True

    def __init__(self, first, fresh):
        self.first, self.fresh, self.fresh_reads = first, fresh, 0

    def get_account(self):
        return dict(self.first)

    def get_account_fresh(self):
        self.fresh_reads += 1
        return dict(self.fresh)

    def get_positions(self):
        return [dict(symbol='SPY', qty='25', market_value='19000', current_price='760', avg_entry_price='750',
                     unrealized_pl='250', unrealized_plpc='0.013', change_today='-0.01')]

    get_positions_fresh = get_positions

    def get_orders(self, status='open'):
        return [dict(id='p', symbol='SPY', side='buy', status='filled', legs=[
            dict(id='take', symbol='SPY', side='sell', type='limit', qty='20', status='new', limit_price='780')])]

    get_open_orders_fresh = get_orders

    def get_clock(self):
        return {'is_open': True}


def test_inconsistent_first_account_read_is_replaced_by_fresh_alpaca_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio_module, '_last_good_equity', {})
    sessions = sessions_for(tmp_path)
    good = dict(equity='102426', last_equity='101000', cash='83426', buying_power='90000')
    broker = Broker(dict(good, equity='125432'), good)
    with sessions() as db:
        db.add(Strategy(id='s', owner_user_id=1, name='布林带', enabled=True, definition={}))
        db.add(StrategyPosition(user_id=1, strategy_id='s', symbol='SPY', qty=20))
        db.commit()
        snapshot = build_portfolio(broker, db, 1)
    assert broker.fresh_reads == 1 and snapshot['status'] == 'ok'
    assert snapshot['account']['equity'] == 102426
    assert round(snapshot['account']['day_change_pct'], 4) == round((102426 / 101000 - 1) * 100, 4)
    position = snapshot['positions'][0]
    assert position['strategy_qty'] == 20 and position['unattributed_qty'] == 5
    assert snapshot['orders'][0]['strategy_name'] is None and snapshot['orders'][0]['parent_order_id'] == 'p'


def test_persistent_contradiction_is_marked_suspect(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio_module, '_last_good_equity', {})
    bad = dict(equity='7041418', last_equity='101000', cash='83426', buying_power='90000')
    with sessions_for(tmp_path)() as db:
        snapshot = build_portfolio(Broker(bad, bad), db, 1)
    assert snapshot['status'] == 'suspect' and any('不一致' in item for item in snapshot['checks'])


def test_enabled_count_excludes_templates(tmp_path):
    with sessions_for(tmp_path)() as db:
        db.add_all([Strategy(id='a', owner_user_id=1, name='a', enabled=True, definition={}),
                    Strategy(id='t', owner_user_id=1, name='t', enabled=True, is_template=True, definition={}),
                    Strategy(id='b', owner_user_id=1, name='b', enabled=False, definition={})])
        db.commit()
        assert enabled_strategy_count(db, 1) == 1


def test_previous_session_final_bar_is_actionable_at_open_but_not_later():
    final_bar = datetime(2026, 11, 13, 15, 45, tzinfo=ET).astimezone(timezone.utc)
    at_open = datetime(2026, 11, 16, 9, 31, tzinfo=ET).astimezone(timezone.utc)
    assert TradingEngine._entry_intent_is_fresh(final_bar, final_bar, '15Min', at_open, 2700)
    assert not TradingEngine._entry_intent_is_fresh(final_bar, final_bar, '15Min', at_open + timedelta(hours=1), 2700)
    common = dict(symbol='SPY', desired_notional=100, account={'equity': '100000', 'buying_power': '100000'},
                  positions=[], asset={'tradable': True, 'status': 'active'}, clock={'is_open': True},
                  bar_timestamp=final_bar, max_data_age_seconds=2700,
                  settings=RiskSettings(max_symbol_pct=10, max_total_exposure_pct=80, max_positions=8,
                                        max_daily_loss_pct=3, max_intraday_drawdown_pct=5),
                  state=EngineState(user_id=1, status='running'), strategy_max_symbol_pct=10, strategy_max_positions=8)
    assert RiskManager().check_entry(**common, data_fresh=True).reason != '行情数据已过期，拒绝下单'
    assert RiskManager().check_entry(**common).reason == '行情数据已过期，拒绝下单'


def test_evaluation_health_flags_stalled_intraday_strategy():
    engine = TradingEngine(Settings(_env_file=None), Broker({}, {}), user_id=1)
    strategy = Strategy(id='s', name='Donchian', definition={})
    now = datetime.now(timezone.utc)
    engine._record_evaluation(strategy, 'SPY', '15Min', 'no_trigger', now - timedelta(minutes=20))
    assert engine.evaluation_health(True)['stalled'] == []
    engine._evaluation_status[('s', 'SPY')]['checked_at'] = (now - timedelta(minutes=10)).isoformat()
    assert engine.evaluation_health(True)['stalled'] == ['Donchian/SPY']
    assert engine.evaluation_health(False)['stalled'] == []
