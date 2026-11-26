"""Short-stock and long-option execution paths, kept separate from long stocks."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import EngineState, EventLog, OrderRecord, RiskSettings, Signal, Strategy, StrategyPosition
from app.schemas import RuleDefinition
from app.services import engine as engine_module
from app.services import risk as risk_module
from app.services.engine import TradingEngine
from app.services.instruments import choose_contract, option_symbol, parse_option_symbol, round_option_price
from app.services.risk import RiskManager

NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)   # Wednesday 11:00 ET


def condition(operator: str, value: float) -> dict:
    return {"type": "group", "op": "AND", "negate": False, "children": [{
        "type": "condition", "left": {"kind": "price", "field": "close", "offset": 0},
        "operator": operator, "right": {"kind": "number", "value": value}, "label": f"close {operator} {value}"}]}


def definition(**overrides) -> dict:
    base = {
        "version": 1, "name": "test", "description": "", "symbols": ["SPY"], "timeframe": "15Min", "warmup_bars": 30,
        "schedule": {"session": "regular", "weekdays": [0, 1, 2, 3, 4]},
        "entry": condition(">", 0), "exit": condition("<", 0),
        "position": {"mode": "fixed_qty", "value": 10, "allow_pyramiding": False, "max_additions": 1},
        "order": {"type": "market", "limit_offset_bps": 0, "time_in_force": "day",
                  "stop_loss": {"mode": "percent", "value": 2, "atr_period": 14},
                  "take_profit": {"mode": "percent", "value": 4, "atr_period": 14}, "trailing_stop": None},
        "risk": {"max_symbol_pct": 10, "max_positions": 8, "cooldown_bars": 1},
    }
    base.update(overrides)
    return base


OPTION_CONFIG = {"right": "call", "min_dte": 14, "max_dte": 45, "strike_mode": "atm", "strike_offset_pct": 0,
                 "min_open_interest": 0, "max_spread_pct": 25, "stop_loss_pct": 50, "take_profit_pct": 100,
                 "close_dte": 1}


def option_definition(**overrides) -> dict:
    values = {"instrument": "option", "option": OPTION_CONFIG,
              "position": {"mode": "percent_equity", "value": 2, "allow_pyramiding": False, "max_additions": 1},
              "order": {"type": "market", "limit_offset_bps": 0, "time_in_force": "day",
                        "stop_loss": None, "take_profit": None, "trailing_stop": None}}
    values.update(overrides)
    return definition(**values)


# ----- schema -----------------------------------------------------------------
def test_existing_definitions_default_to_long_stock():
    parsed = RuleDefinition.model_validate(definition())
    assert parsed.direction == "long" and parsed.instrument == "stock" and parsed.option is None


@pytest.mark.parametrize("bad", [
    definition(direction="short", order={"type": "market", "limit_offset_bps": 0, "time_in_force": "day",
                                         "stop_loss": None, "take_profit": None,
                                         "trailing_stop": {"mode": "percent", "value": 3}}),
    definition(instrument="option"),
    definition(instrument="option", option=OPTION_CONFIG),                  # bracket not allowed on options
    definition(instrument="option", direction="short", option=OPTION_CONFIG),
    option_definition(option={**OPTION_CONFIG, "close_dte": 14}),
    option_definition(option={**OPTION_CONFIG, "min_dte": 30, "max_dte": 20}),
])
def test_invalid_short_and_option_definitions_are_rejected(bad):
    with pytest.raises(ValidationError):
        RuleDefinition.model_validate(bad)


# ----- instruments ------------------------------------------------------------
def test_occ_symbols_round_trip_and_contract_choice():
    symbol = option_symbol("SPY", date(2026, 8, 14), "call", 600.5)
    assert symbol == "SPY260814C00600500"
    parsed = parse_option_symbol(symbol)
    assert parsed.root == "SPY" and parsed.right == "call" and parsed.strike == 600.5
    assert parse_option_symbol("SPY") is None
    contracts = [{"symbol": option_symbol("SPY", expiry, "call", strike), "type": "call", "style": "american",
                  "tradable": True, "open_interest": "500"}
                 for expiry in (date(2026, 7, 24), date(2026, 8, 14), date(2026, 9, 18))
                 for strike in (95, 100, 105)]
    config = RuleDefinition.model_validate(option_definition()).option
    chosen = choose_contract(contracts, 101.0, config, date(2026, 7, 15))
    assert chosen["symbol"] == "SPY260814C00100000"          # 30 DTE is nearest the 14-45 middle
    otm = config.model_copy(update={"strike_mode": "otm_pct", "strike_offset_pct": 4})
    assert choose_contract(contracts, 101.0, otm, date(2026, 7, 15))["symbol"] == "SPY260814C00105000"
    puts = config.model_copy(update={"right": "put"})
    assert choose_contract(contracts, 101.0, puts, date(2026, 7, 15)) is None
    assert round_option_price(2.01, "buy") == 2.05 and round_option_price(2.04, "sell") == 2.0


def test_short_entries_add_exposure_but_covers_do_not():
    exposure = RiskManager.pending_buy_exposure([
        {"symbol": "TSLA", "side": "sell", "qty": "10", "status": "new", "client_order_id": "qp-x-1", "limit_price": "200"},
        {"symbol": "TSLA", "side": "buy", "qty": "10", "status": "held", "parent_order_id": "p", "stop_price": "210"},
        {"symbol": "TSLA", "side": "buy", "qty": "5", "status": "new", "client_order_id": "qp-c-2"},
        {"symbol": "SPY", "side": "sell", "qty": "3", "status": "new", "client_order_id": "qp-s-3"},
        {"symbol": "SPY", "side": "buy", "qty": "1", "status": "new", "client_order_id": "qp-b-4", "limit_price": "100"},
    ], 100)
    assert exposure == {"TSLA": 2000.0, "SPY": 100.0}


# ----- engine -----------------------------------------------------------------
class Broker:
    configured = True
    broker_name = "alpaca"
    paper = True
    supports_short = True
    supports_options = True
    supports_trailing_stop = True

    def __init__(self):
        self.positions: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.entries: list[dict] = []
        self.exits: list[tuple] = []
        self.cancelled: list[str] = []
        self.asset = {"tradable": True, "status": "active", "shortable": True, "easy_to_borrow": True, "fractionable": True}
        self.account = {"equity": "100000", "last_equity": "100000", "buying_power": "200000", "shorting_enabled": True}
        self.option_bid, self.option_ask = 2.0, 2.1
        self.contract_calls = 0

    def stop_streams(self):
        return None

    def connection_status(self):
        return {"state": "connected", "connected": True, "health": {"trading": {"state": "closed"}, "failed_operations": []}}

    def get_clock(self):
        return {"is_open": True}

    def get_account(self):
        return dict(self.account)

    def get_positions(self):
        return [dict(item) for item in self.positions]

    get_positions_fresh = get_positions

    def get_orders(self, status="all"):
        orders = [dict(order) for order in self.orders.values()]
        if status == "all":
            return orders
        return [order for order in orders if order["status"] not in {"filled", "canceled"}]

    def get_open_orders_fresh(self):
        return self.get_orders("open")

    def get_order_by_client_id(self, client_order_id):
        return next((dict(order) for order in self.orders.values() if order["client_order_id"] == client_order_id), None)

    def get_asset(self, symbol):
        return dict(self.asset, symbol=symbol)

    def get_latest_quotes(self, symbols):
        return {symbol: {"bid_price": "100", "ask_price": "100.1", "timestamp": NOW.isoformat()} for symbol in symbols}

    def recent_bars(self, symbols, timeframe, bars):
        index = pd.date_range(end=datetime(2026, 7, 15, 14, 45, tzinfo=timezone.utc), periods=max(60, bars), freq="15min")
        frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0}, index=index)
        return {symbol: frame.copy() for symbol in symbols}

    def get_option_contracts(self, underlying, **kwargs):
        self.contract_calls += 1
        return [{"symbol": option_symbol(underlying, expiry, "call", strike), "type": "call", "style": "american",
                 "tradable": True, "status": "active", "open_interest": "1000",
                 "expiration_date": expiry.isoformat(), "strike_price": str(strike)}
                for expiry in (date(2026, 7, 24), date(2026, 8, 14)) for strike in (95, 100, 105)]

    def get_option_contract(self, symbol):
        return {"symbol": symbol, "tradable": True, "status": "active"}

    def get_option_quotes(self, symbols):
        return {symbol: {"bid_price": self.option_bid, "ask_price": self.option_ask, "timestamp": NOW.isoformat()}
                for symbol in symbols}

    def _order(self, client_order_id, symbol, side, qty, kind="market", **extra):
        order = {"id": f"o-{len(self.orders) + 1}", "client_order_id": client_order_id, "symbol": symbol, "side": side,
                 "type": kind, "qty": str(qty), "filled_qty": "0", "status": "accepted", **extra}
        self.orders[client_order_id] = order
        return dict(order)

    def submit_entry_order(self, **kwargs):
        self.entries.append(kwargs)
        return self._order(kwargs["client_order_id"], kwargs["symbol"], kwargs.get("side", "buy"), kwargs["qty"],
                           kwargs["order_type"])

    def submit_exit_order(self, symbol, qty, client_order_id, side="sell"):
        self.exits.append((symbol, qty, client_order_id, side))
        return self._order(client_order_id, symbol, side, qty)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        for order in self.orders.values():
            if order["id"] == order_id:
                order["status"] = "canceled"

    def cancel_all_orders(self):
        return []


@pytest.fixture()
def env(tmp_path, monkeypatch):
    database = create_engine(f"sqlite:///{tmp_path / 'short-options.db'}")
    Base.metadata.create_all(database)
    sessions = sessionmaker(bind=database, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(engine_module, "datetime", FixedDateTime)
    monkeypatch.setattr(risk_module, "datetime", FixedDateTime)

    async def no_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)
    broker = Broker()
    engine = TradingEngine(Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")), broker, user_id=7)
    with sessions() as db:
        db.add_all([EngineState(user_id=7, status="running", reason="test"), RiskSettings(user_id=7)])
        db.commit()
    yield engine, broker, sessions
    database.dispose()


def add_strategy(sessions, data: dict, strategy_id="s1"):
    with sessions() as db:
        db.add(Strategy(id=strategy_id, owner_user_id=7, name=data["name"], description="", is_template=False,
                        enabled=True, definition=RuleDefinition.model_validate(data).model_dump(mode="json")))
        db.commit()


def ledger(sessions, symbol):
    with sessions() as db:
        return sum(db.scalars(select(StrategyPosition.qty).where(StrategyPosition.symbol == symbol)).all())


async def fill(engine, broker, client_prefix, qty, price, symbol=None):
    order = next(order for order in broker.orders.values() if order["client_order_id"].startswith(client_prefix)
                 and (symbol is None or order["symbol"] == symbol) and order["status"] != "filled")
    order.update(status="filled", filled_qty=str(qty), filled_avg_price=str(price))
    await engine.handle_trade_update({"event": "fill", "order": dict(order)})
    return order


@pytest.mark.asyncio
async def test_short_strategy_sells_to_open_and_buys_to_cover(env):
    engine, broker, sessions = env
    add_strategy(sessions, definition(direction="short", name="short spy"))
    await engine.evaluate_active_strategies(force=True)

    assert len(broker.entries) == 1
    entry = broker.entries[0]
    assert entry["side"] == "sell" and entry["qty"] == 10
    assert entry["stop_price"] == 102.0 and entry["take_price"] == 96.0     # stop above, take below the 100 bid
    assert entry["client_order_id"].startswith("qp-x-")
    with sessions() as db:
        signal = db.scalar(select(Signal))
        record = db.scalar(select(OrderRecord))
    assert signal.action == "short" and record.side == "sell"

    await fill(engine, broker, "qp-x-", 10, 100)
    assert ledger(sessions, "SPY") == -10
    broker.positions = [{"symbol": "SPY", "qty": "-10", "market_value": "-1000", "avg_entry_price": "100"}]
    assert engine._audit_long_only_invariants(broker.get_positions(), broker.get_orders("open")) == {}
    broker.positions[0]["qty"] = "-12"     # someone else sold 2 more: unexplained short
    assert engine._audit_long_only_invariants(broker.get_positions(), []) == {"SPY": "券商空头数量超过策略归属空头"}
    broker.positions[0]["qty"] = "-10"

    # A new completed bar with the exit rule matching produces a buy-to-cover.
    with sessions() as db:
        strategy = db.get(Strategy, "s1")
        strategy.definition = RuleDefinition.model_validate(
            definition(direction="short", name="short spy", exit=condition(">", 0))).model_dump(mode="json")
        db.commit()
    engine._last_evaluated.clear()
    await engine.evaluate_active_strategies(force=True)
    assert broker.exits and broker.exits[-1][1:] == (10.0, broker.exits[-1][2], "buy")
    assert broker.exits[-1][2].startswith("qp-c-")
    await fill(engine, broker, "qp-c-", 10, 98)
    assert ledger(sessions, "SPY") == 0


@pytest.mark.asyncio
async def test_short_is_blocked_for_hard_to_borrow_or_long_holdings(env):
    engine, broker, sessions = env
    add_strategy(sessions, definition(direction="short", name="short spy"))
    broker.asset["easy_to_borrow"] = False
    await engine.evaluate_active_strategies(force=True)
    assert broker.entries == []
    broker.asset["easy_to_borrow"] = True
    broker.positions = [{"symbol": "SPY", "qty": "5", "market_value": "500"}]
    engine._last_evaluated.clear()
    await engine.evaluate_active_strategies(force=True)
    assert broker.entries == []
    with sessions() as db:
        messages = [row.message for row in db.scalars(select(EventLog)).all()]
    assert any("易借券" in message for message in messages)
    assert any("持有多头" in message for message in messages)


@pytest.mark.asyncio
async def test_cover_never_buys_more_than_the_actual_short(env):
    engine, broker, sessions = env
    broker.positions = [{"symbol": "SPY", "qty": "-4"}]
    with pytest.raises(engine_module.LongOnlyInvariantError):
        await engine._verify_cover_capacity("SPY", 10, "qp-c-test")
    await engine._verify_cover_capacity("SPY", 4, "qp-c-test")


@pytest.mark.asyncio
async def test_long_entry_rejected_while_symbol_is_short(env):
    engine, broker, sessions = env
    add_strategy(sessions, definition(name="long spy"))
    broker.positions = [{"symbol": "SPY", "qty": "-10", "market_value": "-1000"}]
    await engine.evaluate_active_strategies(force=True)
    assert broker.entries == []


@pytest.mark.asyncio
async def test_option_strategy_buys_a_listed_contract_and_stops_on_premium(env):
    engine, broker, sessions = env
    add_strategy(sessions, option_definition(name="spy calls"))
    await engine.evaluate_active_strategies(force=True)

    assert len(broker.entries) == 1
    entry = broker.entries[0]
    assert entry["symbol"] == "SPY260814C00100000"
    assert entry["qty"] == 9 and entry["stop_price"] is None and entry["take_price"] is None   # 2% of 100k / $210
    with sessions() as db:
        signal = db.scalar(select(Signal))
    assert signal.payload["instrument"] == "option" and signal.payload["underlying"] == "SPY"

    await fill(engine, broker, "qp-b-", 9, 2.1)
    assert ledger(sessions, "SPY260814C00100000") == 9
    broker.positions = [{"symbol": "SPY260814C00100000", "qty": "9", "avg_entry_price": "2.1", "market_value": "1800"}]
    assert engine._audit_long_only_invariants(broker.get_positions(), []) == {}

    broker.option_bid, broker.option_ask = 1.0, 1.1      # premium down ~52%: stop
    engine._last_evaluated.clear()
    await engine.evaluate_active_strategies(force=True)
    assert broker.exits[-1][0] == "SPY260814C00100000" and broker.exits[-1][1] == 9 and broker.exits[-1][3] == "sell"
    with sessions() as db:
        exit_signal = db.scalars(select(Signal).where(Signal.action == "sell")).one()
    assert "止损" in exit_signal.reason
    await fill(engine, broker, "qp-s-", 9, 1.0, symbol="SPY260814C00100000")
    assert ledger(sessions, "SPY260814C00100000") == 0


@pytest.mark.asyncio
async def test_option_closes_before_expiry(env):
    engine, broker, sessions = env
    add_strategy(sessions, option_definition(name="spy calls", entry=condition("<", 0)))
    contract = "SPY260716C00100000"     # expires tomorrow
    with sessions() as db:
        db.add(StrategyPosition(user_id=7, strategy_id="s1", symbol=contract, qty=2))
        db.commit()
    broker.positions = [{"symbol": contract, "qty": "2", "avg_entry_price": "2.1"}]
    await engine.evaluate_active_strategies(force=True)
    assert broker.exits and broker.exits[-1][:2] == (contract, 2.0)


@pytest.mark.asyncio
async def test_unsupported_broker_never_submits(env):
    engine, broker, sessions = env
    broker.supports_short = False
    add_strategy(sessions, definition(direction="short", name="short spy"))
    await engine.evaluate_active_strategies(force=True)
    assert broker.entries == []


# ----- backtests --------------------------------------------------------------
from app.services.backtest import run_backtest  # noqa: E402
from app.services.option_backtest import ReplayOptionHistory, run_option_backtest  # noqa: E402


def daily_frame(closes, start="2025-03-03"):
    index = pd.date_range(start=pd.Timestamp(start, tz="America/New_York"), periods=len(closes), freq="B").tz_convert("UTC")
    closes = [float(value) for value in closes]
    return pd.DataFrame({"open": closes, "high": [value * 1.005 for value in closes],
                         "low": [value * 0.995 for value in closes], "close": closes, "volume": 1e6}, index=index)


def test_short_backtest_profits_when_price_falls_and_stops_above():
    falling = daily_frame([100.0] * 31 + [100 - i for i in range(1, 11)])
    short = RuleDefinition.model_validate(definition(
        direction="short", timeframe="1Day", entry=condition(">", 0), exit=condition("<", 0),
        order={"type": "market", "limit_offset_bps": 0, "time_in_force": "day",
               "stop_loss": {"mode": "percent", "value": 50, "atr_period": 14},
               "take_profit": {"mode": "percent", "value": 5, "atr_period": 14}, "trailing_stop": None}))
    result = run_backtest(short, {"SPY": falling}, slippage_bps=0, start=falling.index[30])
    trade = result.trades[0]
    assert trade["side"] == "short" and trade["pnl"] > 0 and trade["reason"] == "止盈"
    # Sold at the next open (99); take-profit 94.05 gapped through, so it covers at the 94 open.
    assert trade["entry_price"] == pytest.approx(99.0) and trade["exit_price"] == pytest.approx(94.0)
    assert trade["pnl"] == pytest.approx(10 * (99.0 - 94.0))

    rising = daily_frame([100.0] * 31 + [100 + i for i in range(1, 11)])
    stopped = RuleDefinition.model_validate(definition(direction="short", timeframe="1Day", entry=condition(">", 0)))
    loss = run_backtest(stopped, {"SPY": rising}, slippage_bps=0, start=rising.index[30]).trades[0]
    assert loss["reason"].startswith("止损") and loss["pnl"] < 0
    assert loss["entry_price"] == pytest.approx(101.0) and loss["exit_price"] == pytest.approx(101.0 * 1.02)


def test_option_backtest_uses_listed_contracts_and_premium_take_profit():
    closes = [100.0] * 31 + [101.0, 103.0, 106.0, 108.0, 109.0]
    underlying = daily_frame(closes, start="2025-03-03")
    signal_day = underlying.index[30]
    expiry = (signal_day.tz_convert("America/New_York") + pd.Timedelta(days=31)).date()
    symbol = option_symbol("SPY", expiry, "call", 100)
    premiums = [2.0, 2.4, 3.5, 3.9, 5.0]     # the 3.9 bar trades up to 4.095, through the +100% target
    option_bars = pd.DataFrame({"open": premiums, "high": [p * 1.05 for p in premiums], "low": [p * 0.95 for p in premiums],
                                "close": premiums, "volume": 100.0}, index=underlying.index[31:36])
    definition_model = RuleDefinition.model_validate(option_definition(
        timeframe="1Day", entry=condition(">", 0),
        option={**OPTION_CONFIG, "min_dte": 20, "max_dte": 40}))
    chain = [{"symbol": symbol, "type": "call", "style": "american", "tradable": True,
              "expiration_date": expiry.isoformat(), "strike_price": "100"}]

    class History(ReplayOptionHistory):
        def contracts(self, *args):
            return chain

    result = run_option_backtest(definition_model, {"SPY": underlying}, History({}, {symbol: option_bars}),
                                 slippage_bps=0, start=signal_day)
    trade = result.trades[0]
    assert trade["symbol"] == symbol and trade["entry_price"] == 2.0
    assert trade["reason"] == "权利金止盈" and trade["exit_price"] == pytest.approx(4.0)
    assert trade["qty"] == 10 and trade["pnl"] == pytest.approx(10 * 100 * 2.0)   # 2% of 100k = $2,000 / $200


def test_option_backtest_requires_data_from_2024():
    early = daily_frame([100.0] * 40, start="2023-06-01")
    with pytest.raises(ValueError, match="2024"):
        run_option_backtest(RuleDefinition.model_validate(option_definition(timeframe="1Day")), {"SPY": early},
                            ReplayOptionHistory({}, {}))


# ----- performance ------------------------------------------------------------
def test_performance_attributes_short_and_option_pnl(env):
    _engine, _broker, sessions = env
    from app.services.performance import strategy_performance

    add_strategy(sessions, definition(direction="short", name="short spy"))
    with sessions() as db:
        db.add_all([
            Signal(id="sig-short", user_id=7, unique_key="a", strategy_id="s1", symbol="SPY", action="short",
                   bar_timestamp=NOW, price=100, reason="breakdown", status="submitted", payload={}),
            Signal(id="sig-cover", user_id=7, unique_key="b", strategy_id="s1", symbol="SPY", action="cover",
                   bar_timestamp=NOW, price=95, reason="recovered", status="submitted", payload={}),
            Signal(id="sig-opt", user_id=7, unique_key="c", strategy_id="s1", symbol="SPY260814C00100000", action="buy",
                   bar_timestamp=NOW, price=2, reason="breakout", status="submitted", payload={}),
            Signal(id="sig-opt-exit", user_id=7, unique_key="d", strategy_id="s1", symbol="SPY260814C00100000",
                   action="sell", bar_timestamp=NOW, price=3, reason="exit", status="submitted", payload={}),
        ])
        for index, (signal_id, symbol, side, qty, price) in enumerate([
                ("sig-short", "SPY", "sell", 10, 100), ("sig-cover", "SPY", "buy", 10, 95),
                ("sig-opt", "SPY260814C00100000", "buy", 2, 2.0), ("sig-opt-exit", "SPY260814C00100000", "sell", 2, 3.0)]):
            db.add(OrderRecord(id=f"o{index}", user_id=7, client_order_id=f"c{index}", strategy_id="s1", signal_id=signal_id,
                               symbol=symbol, side=side, order_type="market", qty=qty, status="filled", filled_qty=qty,
                               filled_avg_price=price, submitted_at=NOW + timedelta(minutes=index),
                               raw={"filled_at": (NOW + timedelta(minutes=index)).isoformat()}))
        db.commit()
        rows = strategy_performance(db, 7)
    trades = {trade["symbol"]: trade for trade in rows[0]["trade_list"]}
    assert trades["SPY"]["side"] == "short" and trades["SPY"]["pnl"] == pytest.approx(50.0)
    assert trades["SPY260814C00100000"]["pnl"] == pytest.approx(200.0)       # (3 - 2) x 2 contracts x 100
    assert rows[0]["realized_pnl"] == pytest.approx(250.0)
