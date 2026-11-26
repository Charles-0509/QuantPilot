"""Short-stock and option lifecycle acceptance (development checklist item 06).

Every scenario records its input, the expected ledger entries, the actual
result and the final position. Set QP_ACCEPTANCE_OUT=<file.json> to write the
records; docs/ACCEPTANCE-SHORT-OPTIONS.md tracks which layer each scenario has
passed: offline model, broker paper API, or live Paper.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from app.models import EventLog, ExecutionIncident, OrderRecord, Signal, StrategyPosition
from app.schemas import RuleDefinition
from app.services import engine as engine_module
from app.services.backtest import run_backtest
from app.services.instruments import option_symbol
from app.services.option_backtest import ReplayOptionHistory, run_option_backtest
from app.services.performance import strategy_performance
from tests.test_short_and_options import (  # noqa: F401  (env is a fixture)
    NOW, OPTION_CONFIG, add_strategy, condition, daily_frame, definition, env, fill, ledger, option_definition,
)

RECORDS: list[dict] = []


def record(scenario: str, *, inputs: dict, expected: dict, actual: dict, final: dict, layer: str = "离线模型") -> None:
    RECORDS.append({"scenario": scenario, "layer": layer, "passed": True, "input": inputs,
                    "expected_entries": expected, "actual": actual, "final_position": final})


# ----- short ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s1_short_rejected_without_borrow(env):
    engine, broker, sessions = env
    add_strategy(sessions, definition(direction="short", name="short spy"))
    broker.asset.update(shortable=True, easy_to_borrow=False)
    await engine.evaluate_active_strategies(force=True)
    broker.asset.update(shortable=False)
    engine._last_evaluated.clear()
    await engine.evaluate_active_strategies(force=True)
    with sessions() as db:
        logs = [row.message for row in db.scalars(select(EventLog)).all()]
    assert broker.entries == [] and ledger(sessions, "SPY") == 0
    assert any("易借券" in line for line in logs) and any("不可做空" in line for line in logs)
    record("S1 无券可借拒绝开仓", inputs={"symbol": "SPY", "easy_to_borrow": False, "then_shortable": False},
           expected={"orders": 0, "ledger": 0}, actual={"orders": len(broker.entries), "logs": logs[-2:]},
           final={"SPY": ledger(sessions, "SPY")})


@pytest.mark.asyncio
async def test_s2_partial_cover_keeps_remaining_short_and_pnl(env):
    engine, broker, sessions = env
    add_strategy(sessions, definition(direction="short", name="short spy",
                                      order={"type": "market", "limit_offset_bps": 0, "time_in_force": "day",
                                             "stop_loss": None, "take_profit": None, "trailing_stop": None}))
    await engine.evaluate_active_strategies(force=True)
    with sessions() as db:
        entry_signal = db.scalar(select(Signal))
    assert entry_signal.payload["borrow"]["easy_to_borrow"] is True and entry_signal.payload["borrow"]["locate_fee"] == 0
    await fill(engine, broker, "qp-x-", 10, 100)
    broker.positions = [{"symbol": "SPY", "qty": "-10", "market_value": "-1000"}]
    strategy = _strategy(sessions)
    await engine._create_exit_signal(
        strategy, RuleDefinition.model_validate(strategy.definition), "SPY",
        broker.recent_bars(["SPY"], "15Min", 60)["SPY"], engine_module.RuleResult(True, NOW, ["回补"]),
        10, broker.get_orders("open"), broker.get_positions(), action="cover")
    cover = next(order for order in broker.orders.values() if order["client_order_id"].startswith("qp-c-"))
    cover.update(status="partially_filled", filled_qty="4", filled_avg_price="95")
    await engine.handle_trade_update({"event": "partial_fill", "order": dict(cover)})
    broker.positions = [{"symbol": "SPY", "qty": "-6", "market_value": "-570"}]
    assert ledger(sessions, "SPY") == -6
    assert engine._audit_long_only_invariants(broker.get_positions(), broker.get_orders("open")) == {}
    with sessions() as db:
        perf = strategy_performance(db, 7, {"SPY": 95.0})[0]
    assert perf["realized_pnl"] == pytest.approx(4 * (100 - 95))
    assert perf["open_lots"][0]["qty"] == 6 and perf["open_lots"][0]["side"] == "short"
    assert perf["unrealized_pnl"] == pytest.approx(6 * (100 - 95))
    record("S2 部分回补", inputs={"short": "10 @ 100", "cover_fill": "4 @ 95 (of 10)"},
           expected={"ledger": -6, "realized_pnl": 20.0, "open_short": 6, "unrealized_at_95": 30.0, "fees": 0.0},
           actual={"ledger": ledger(sessions, "SPY"), "realized_pnl": perf["realized_pnl"],
                   "open_short": perf["open_lots"][0]["qty"], "unrealized": perf["unrealized_pnl"]},
           final={"SPY": -6})


def _strategy(sessions):
    with sessions() as db:
        return db.get(engine_module.Strategy, "s1")


def test_s3_overnight_gap_up_stops_at_the_open():
    closes = [100.0] * 31 + [100.0, 106.0]
    frame = daily_frame(closes)
    frame.iloc[-1, frame.columns.get_loc("open")] = 106.0      # gaps above the 102 stop
    short = RuleDefinition.model_validate(definition(direction="short", timeframe="1Day", entry=condition(">", 0)))
    trade = run_backtest(short, {"SPY": frame}, slippage_bps=0, start=frame.index[30]).trades[0]
    assert trade["entry_price"] == 100.0 and trade["exit_price"] == pytest.approx(106.0)
    assert trade["pnl"] == pytest.approx(-60.0) and trade["reason"] == "止损"
    record("S3 空头隔夜跳空止损", inputs={"short": "10 @ 100", "stop": 102, "next_open": 106},
           expected={"cover_price": 106.0, "pnl": -60.0}, actual={"cover_price": trade["exit_price"], "pnl": trade["pnl"]},
           final={"SPY": 0})


# ----- options ----------------------------------------------------------------
def _chain(symbols):
    return [{"symbol": symbol, "type": "call", "style": "american", "tradable": True} for symbol in symbols]


def test_o1_historical_choice_skips_contracts_listed_after_the_signal():
    underlying = daily_frame([100.0] * 31 + [101.0, 102.0, 103.0])
    signal_day = underlying.index[30]
    expiry = (signal_day.tz_convert("America/New_York") + pd.Timedelta(days=30)).date()
    later_listed = option_symbol("SPY", expiry, "call", 100)             # preferred, but first prints after the signal
    listed = option_symbol("SPY", expiry, "call", 101)
    bars = lambda index, price: pd.DataFrame({"open": price, "high": price, "low": price, "close": price, "volume": 10.0},  # noqa: E731
                                             index=index)

    class History(ReplayOptionHistory):
        def contracts(self, *args):
            return _chain([later_listed, listed])

    history = History({}, {later_listed: bars(underlying.index[31:], 3.0), listed: bars(underlying.index[25:], 2.5)})
    model = RuleDefinition.model_validate(option_definition(timeframe="1Day", option={**OPTION_CONFIG, "min_dte": 20, "max_dte": 40,
                                                                                         "take_profit_pct": None, "stop_loss_pct": None}))
    trades = run_option_backtest(model, {"SPY": underlying}, history, slippage_bps=0, start=signal_day).trades
    assert trades[0]["symbol"] == listed
    record("O1 历史选约只用已挂牌合约", inputs={"preferred": later_listed, "preferred_first_bar": "signal+1",
                                       "fallback": listed},
           expected={"contract": listed}, actual={"contract": trades[0]["symbol"]}, final={listed: 0})


def test_o2_no_print_between_signal_and_bar_means_no_fill():
    underlying = daily_frame([100.0] * 31 + [101.0, 102.0])
    signal_day = underlying.index[30]
    expiry = (signal_day.tz_convert("America/New_York") + pd.Timedelta(days=30)).date()
    symbol = option_symbol("SPY", expiry, "call", 100)
    stale = pd.DataFrame({"open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0, "volume": 1.0}, index=underlying.index[25:31])

    class History(ReplayOptionHistory):
        def contracts(self, *args):
            return _chain([symbol])

    model = RuleDefinition.model_validate(option_definition(timeframe="1Day", option={**OPTION_CONFIG, "min_dte": 20, "max_dte": 40}))
    result = run_option_backtest(model, {"SPY": underlying}, History({}, {symbol: stale}), slippage_bps=0, start=signal_day)
    assert result.trades == []
    record("O2 过期报价不触发成交（回测）", inputs={"last_option_print": "signal day", "later_prints": 0},
           expected={"trades": 0}, actual={"trades": len(result.trades)}, final={})


@pytest.mark.asyncio
async def test_o2_stale_live_quote_neither_enters_nor_stops(env):
    engine, broker, sessions = env
    add_strategy(sessions, option_definition(name="spy calls"))
    stale = (NOW - pd.Timedelta(hours=2)).isoformat()
    broker.get_option_quotes = lambda symbols: {s: {"bid_price": 0.5, "ask_price": 0.6, "timestamp": stale} for s in symbols}
    await engine.evaluate_active_strategies(force=True)
    assert broker.entries == []
    with sessions() as db:
        pending = db.scalar(select(Signal))
        pending_status = pending.status
        db.add(StrategyPosition(user_id=7, strategy_id="s1", symbol="SPY260814C00100000", qty=3))
        db.commit()
    broker.positions = [{"symbol": "SPY260814C00100000", "qty": "3", "avg_entry_price": "2.1"}]
    engine._last_evaluated.clear()
    await engine.evaluate_active_strategies(force=True)
    assert broker.exits == []          # bid 0.5 is -76%, but the quote is two hours old
    record("O2 过期报价不触发成交（实盘引擎）", inputs={"quote_age": "2h", "bid": 0.5, "entry_premium": 2.1},
           expected={"entry_orders": 0, "stop_orders": 0, "intent": "pending_submission"},
           actual={"entry_orders": len(broker.entries), "stop_orders": len(broker.exits), "intent": pending_status},
           final={"SPY260814C00100000": 3})


@pytest.mark.asyncio
async def test_o3_worthless_expiry_settles_instead_of_quarantine(env):
    engine, broker, sessions = env
    add_strategy(sessions, option_definition(name="spy calls", entry=condition("<", 0)))
    contract = "SPY260710C00120000"     # expired five days ago, out of the money
    with sessions() as db:
        db.add_all([
            Signal(id="sig", user_id=7, unique_key="k", strategy_id="s1", symbol=contract, action="buy", bar_timestamp=NOW,
                   price=1.5, reason="breakout", status="submitted", payload={}),
            OrderRecord(id="buy-1", user_id=7, client_order_id="qp-b-1", strategy_id="s1", signal_id="sig", symbol=contract,
                        side="buy", order_type="market", qty=2, status="filled", filled_qty=2, filled_avg_price=1.5,
                        submitted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), raw={"filled_at": "2026-07-01T15:00:00+00:00"}),
            StrategyPosition(user_id=7, strategy_id="s1", symbol=contract, qty=2),
        ])
        db.commit()
    broker.get_option_activities = lambda after: [{"activity_type": "OPEXP", "symbol": contract, "qty": "2"}]
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        perf = strategy_performance(db, 7)[0]
    assert incident is None and ledger(sessions, contract) == 0
    trade = perf["trade_list"][0]
    assert trade["exit_label"] == "期权到期作废" and trade["pnl"] == pytest.approx(-300.0)
    record("O3 价外到期", inputs={"contract": contract, "qty": 2, "premium": 1.5, "activity": "OPEXP"},
           expected={"ledger": 0, "pnl": -300.0, "quarantine": False, "cash": "无变化（权利金已在买入时支付）"},
           actual={"ledger": ledger(sessions, contract), "pnl": trade["pnl"], "quarantine": incident is not None},
           final={contract: 0})


@pytest.mark.asyncio
async def test_o4_exercise_closes_at_intrinsic_and_leaves_shares_unowned(env):
    engine, broker, sessions = env
    add_strategy(sessions, option_definition(name="spy calls", entry=condition("<", 0)))
    contract = "SPY260710C00095000"     # strike 95, underlying closed at 100 on expiry
    with sessions() as db:
        db.add_all([
            Signal(id="sig", user_id=7, unique_key="k", strategy_id="s1", symbol=contract, action="buy", bar_timestamp=NOW,
                   price=2.0, reason="breakout", status="submitted", payload={}),
            OrderRecord(id="buy-1", user_id=7, client_order_id="qp-b-1", strategy_id="s1", signal_id="sig", symbol=contract,
                        side="buy", order_type="market", qty=1, status="filled", filled_qty=1, filled_avg_price=2.0,
                        submitted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), raw={"filled_at": "2026-07-01T15:00:00+00:00"}),
            StrategyPosition(user_id=7, strategy_id="s1", symbol=contract, qty=1),
        ])
        db.commit()
    broker.get_option_activities = lambda after: [{"activity_type": "OPEXC", "symbol": contract, "qty": "1"}]
    expiry_close = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
                                index=pd.DatetimeIndex([pd.Timestamp("2026-07-10", tz="America/New_York").tz_convert("UTC")]))
    broker.recent_bars = lambda symbols, timeframe, bars: {symbol: expiry_close for symbol in symbols}
    broker.positions = [{"symbol": "SPY", "qty": "100", "market_value": "10000"}]   # shares delivered by exercise
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
        perf = strategy_performance(db, 7)[0]
        settlement = db.get(OrderRecord, next(iter(db.scalars(select(OrderRecord.id).where(
            OrderRecord.order_type == "option_exercise")))))
    trade = perf["trade_list"][0]
    assert incident is None and ledger(sessions, contract) == 0 and ledger(sessions, "SPY") == 0
    assert settlement.filled_avg_price == pytest.approx(5.0) and trade["pnl"] == pytest.approx(300.0)
    record("O4 行权（看涨价内）", inputs={"contract": contract, "premium": 2.0, "expiry_close": 100.0, "activity": "OPEXC"},
           expected={"option_ledger": 0, "pnl": 300.0, "stock": "100 股 SPY 出现在账户，未归属任何策略，需人工处理",
                     "cash": "行权支付 95×100 由券商记账"},
           actual={"option_ledger": ledger(sessions, contract), "pnl": trade["pnl"], "settlement_price": settlement.filled_avg_price,
                   "quarantine": incident is not None},
           final={contract: 0, "SPY(unowned)": 100})


@pytest.mark.asyncio
async def test_o4_put_exercise_short_shares_are_quarantined(env):
    engine, broker, sessions = env
    add_strategy(sessions, option_definition(name="spy puts", entry=condition("<", 0)))
    contract = "SPY260710P00105000"
    with sessions() as db:
        db.add(StrategyPosition(user_id=7, strategy_id="s1", symbol=contract, qty=1))
        db.commit()
    broker.get_option_activities = lambda after: [{"activity_type": "OPEXC", "symbol": contract}]
    broker.positions = [{"symbol": "SPY", "qty": "-100", "market_value": "-10000"}]
    await engine.reconcile_orders()
    with sessions() as db:
        incident = db.scalar(select(ExecutionIncident))
    assert ledger(sessions, contract) == 0 and incident is not None and incident.symbol == "SPY"
    record("O4 行权（看跌价内）", inputs={"contract": contract, "activity": "OPEXC"},
           expected={"option_ledger": 0, "stock": "-100 股 SPY 为未归属空头，必须隔离并人工回补"},
           actual={"option_ledger": ledger(sessions, contract), "quarantine_symbol": incident.symbol},
           final={contract: 0, "SPY(unowned)": -100})


def test_o5_backtest_settles_held_contract_at_intrinsic():
    underlying = daily_frame([100.0] * 31 + [104.0] * 25)
    signal_day = underlying.index[30]
    expiry = underlying.index[45].tz_convert("America/New_York").date()
    symbol = option_symbol("SPY", expiry, "call", 100)
    option = pd.DataFrame({"open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0, "volume": 1.0}, index=underlying.index[25:32])

    class History(ReplayOptionHistory):
        def contracts(self, *args):
            return _chain([symbol])

    model = RuleDefinition.model_validate(option_definition(
        timeframe="1Day", option={**OPTION_CONFIG, "min_dte": 14, "max_dte": 40, "close_dte": 0,
                                  "stop_loss_pct": None, "take_profit_pct": None}))
    trade = run_option_backtest(model, {"SPY": underlying}, History({}, {symbol: option}), slippage_bps=0,
                                start=signal_day).trades[0]
    assert trade["reason"] == "到期按内在价值结算" and trade["exit_price"] == pytest.approx(4.0)
    record("O5 回测到期结算", inputs={"strike": 100, "spot_at_expiry": 104, "premium": 2.0},
           expected={"exit": 4.0, "reason": "到期按内在价值结算"}, actual={"exit": trade["exit_price"], "reason": trade["reason"]},
           final={symbol: 0})


def test_zz_write_acceptance_records():
    path = os.environ.get("QP_ACCEPTANCE_OUT")
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "records": RECORDS},
                      handle, ensure_ascii=False, indent=2, default=str)
    assert RECORDS
