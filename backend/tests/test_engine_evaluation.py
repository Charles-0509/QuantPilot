from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    EventLog,
    OrderRecord,
    Signal,
    Strategy,
    StrategyPosition,
    WatchlistItem,
)
from app.services import engine as engine_module
from app.services.engine import TradingEngine
from app.config import Settings


def _definition(
    name: str,
    *,
    symbols: list[str] | None = None,
    timeframe: str = "15Min",
    warmup_bars: int = 30,
) -> dict:
    return {
        "version": 1,
        "name": name,
        "description": "engine snapshot test",
        "symbols": symbols or ["SPY", "QQQ"],
        "timeframe": timeframe,
        "warmup_bars": warmup_bars,
        "schedule": {
            "session": "regular",
            "weekdays": [0, 1, 2, 3, 4],
        },
        "entry": {
            "type": "group",
            "op": "AND",
            "negate": False,
            "children": [
                {
                    "type": "condition",
                    "left": {"kind": "price", "field": "close", "offset": 0},
                    "operator": ">",
                    "right": {"kind": "number", "value": 1_000_000_000},
                    "label": "never enter",
                }
            ],
        },
        "exit": {
            "type": "group",
            "op": "AND",
            "negate": False,
            "children": [
                {
                    "type": "condition",
                    "left": {"kind": "price", "field": "close", "offset": 0},
                    "operator": "<",
                    "right": {"kind": "number", "value": -1},
                    "label": "never exit",
                }
            ],
        },
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
        "risk": {
            "max_symbol_pct": 10,
            "max_positions": 8,
            "cooldown_bars": 1,
        },
    }


class CountingAlpaca:
    configured = True

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.recent_calls: list[tuple[list[str], str, int]] = []
        self.clock_failures = 0
        self.account_failures = 0
        self.bars_failures = 0
        self.market_open = True
        self.all_orders: list[dict] = []
        self.stream_healthy = True
        self.stream_starts: list[list[str]] = []
        self.connection_state = "connected"
        self.failed_operations: list[dict] = []
        self.positions: list[dict] = []

    def stop_streams(self) -> None:
        return None

    def streams_healthy(self) -> bool:
        return self.stream_healthy

    async def start_streams(self, symbols, _bar_callback, _trade_callback) -> None:
        self.stream_starts.append(list(symbols))
        self.stream_healthy = True

    def get_clock(self) -> dict:
        self.calls["clock"] += 1
        if self.clock_failures:
            self.clock_failures -= 1
            raise ConnectionError("simulated TLS EOF")
        return {"is_open": self.market_open}

    def get_account(self) -> dict:
        self.calls["account"] += 1
        if self.account_failures:
            self.account_failures -= 1
            raise ConnectionError("simulated account TLS EOF")
        return {"equity": "100000", "buying_power": "200000"}

    def get_positions(self) -> list[dict]:
        self.calls["positions"] += 1
        return [dict(position) for position in self.positions]

    def get_orders(self, status: str = "all") -> list[dict]:
        self.calls[f"orders:{status}"] += 1
        return list(self.all_orders) if status == "all" else []

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        self.calls["order_by_client_id"] += 1
        return next(
            (
                dict(order)
                for order in self.all_orders
                if str(order.get("client_order_id")) == client_order_id
            ),
            None,
        )

    def connection_status(self) -> dict:
        return {
            "state": self.connection_state,
            "connected": self.connection_state == "connected",
            "message": "test connection state",
            "health": {
                "trading": {
                    "state": "open" if self.connection_state == "circuit_open" else "closed"
                },
                "failed_operations": list(self.failed_operations),
            },
        }

    def recent_bars(
        self, symbols: list[str], timeframe: str, bars: int
    ) -> dict[str, pd.DataFrame]:
        self.calls["bars"] += 1
        self.recent_calls.append((list(symbols), timeframe, bars))
        if self.bars_failures:
            self.bars_failures -= 1
            raise ConnectionError("simulated data connection reset")
        index = pd.date_range(
            end=datetime.now(timezone.utc), periods=max(40, bars), freq="15min"
        )
        frame = pd.DataFrame(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1000.0,
            },
            index=index,
        )
        return {symbol: frame.copy() for symbol in symbols}


def _sessions(tmp_path, monkeypatch, now_ref: dict[str, datetime] | None = None):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'engine-evaluation.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = (
                now_ref["value"]
                if now_ref is not None
                else datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
            )
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(engine_module, "datetime", FixedDateTime)

    async def no_broadcast(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(engine_module.websocket_manager, "broadcast", no_broadcast)
    return database_engine, sessions


def _add_strategies(sessions, count: int = 9) -> None:
    with sessions() as db:
        for index in range(count):
            db.add(
                Strategy(
                    id=f"strategy-{index:02d}",
                    owner_user_id=7,
                    name=f"Shared strategy {index}",
                    description="",
                    is_template=False,
                    enabled=True,
                    definition=_definition(
                        f"Shared strategy {index}", warmup_bars=30 + index
                    ),
                )
            )
        db.commit()


@pytest.mark.asyncio
async def test_nine_strategies_share_trading_snapshot_and_timeframe_bars(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions)
    alpaca = CountingAlpaca()
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    await engine.evaluate_active_strategies(force=True)

    assert alpaca.calls == Counter(
        {
            "clock": 1,
            "account": 1,
            "positions": 1,
            "orders:open": 1,
            "bars": 1,
        }
    )
    assert alpaca.recent_calls == [(["QQQ", "SPY"], "15Min", 58)]
    assert len(engine._last_evaluated) == 18
    database_engine.dispose()


@pytest.mark.asyncio
async def test_connection_failure_is_rate_limited_and_recovery_logged_once(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=2)
    alpaca = CountingAlpaca()
    alpaca.clock_failures = 2
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    await engine.evaluate_active_strategies(force=True)
    await engine.evaluate_active_strategies(force=True)
    assert engine._last_evaluated == {}

    alpaca.market_open = False
    await engine.evaluate_active_strategies(force=True)
    await engine.evaluate_active_strategies(force=True)

    with sessions() as db:
        events = db.scalars(
            select(EventLog)
            .where(EventLog.user_id == 7, EventLog.category == "connection")
            .order_by(EventLog.created_at)
        ).all()
    assert [event.level for event in events] == ["error", "info"]
    assert "本轮全部策略已安全跳过" in events[0].message
    assert "连接已恢复" in events[1].message
    assert alpaca.calls["clock"] == 4
    assert alpaca.calls["account"] == 0
    database_engine.dispose()


@pytest.mark.asyncio
async def test_first_trading_snapshot_failure_stops_later_calls(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    alpaca = CountingAlpaca()
    alpaca.account_failures = 1
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    await engine.evaluate_active_strategies(force=True)

    assert alpaca.calls["clock"] == 1
    assert alpaca.calls["account"] == 1
    assert alpaca.calls["positions"] == 0
    assert alpaca.calls["orders:open"] == 0
    assert alpaca.calls["bars"] == 0
    assert engine._last_evaluated == {}
    database_engine.dispose()


@pytest.mark.asyncio
async def test_auxiliary_degradation_does_not_block_engine_but_critical_failure_does(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    alpaca = CountingAlpaca()
    alpaca.connection_state = "degraded"
    alpaca.failed_operations = [
        {"channel": "data", "operation": "latest_quotes"}
    ]
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    await engine.evaluate_active_strategies(force=True)
    assert len(engine._last_evaluated) == 2

    engine._last_evaluated.clear()
    alpaca.failed_operations = [{"channel": "trading", "operation": "orders:open"}]
    await engine.evaluate_active_strategies(force=True)
    assert engine._last_evaluated == {}
    database_engine.dispose()


@pytest.mark.asyncio
async def test_closed_market_clock_success_does_not_hide_unrecovered_orders_failure(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    alpaca = CountingAlpaca()
    alpaca.account_failures = 1
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )
    await engine.evaluate_active_strategies(force=True)

    alpaca.market_open = False
    alpaca.connection_state = "degraded"
    alpaca.failed_operations = [
        {"channel": "trading", "operation": "orders:open"}
    ]
    await engine.evaluate_active_strategies(force=True)

    with sessions() as db:
        events = db.scalars(
            select(EventLog).where(
                EventLog.user_id == 7,
                EventLog.category == "connection",
            )
        ).all()
    assert [event.level for event in events] == ["error"]
    database_engine.dispose()


@pytest.mark.asyncio
async def test_failed_bar_snapshot_does_not_consume_latest_bar(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    alpaca = CountingAlpaca()
    alpaca.bars_failures = 1
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    await engine.evaluate_active_strategies(force=True)
    assert engine._last_evaluated == {}

    await engine.evaluate_active_strategies(force=True)
    assert len(engine._last_evaluated) == 2
    database_engine.dispose()


@pytest.mark.asyncio
async def test_intraday_bar_is_evaluated_only_after_its_interval_closes(
    tmp_path, monkeypatch
) -> None:
    now_ref = {"value": datetime(2026, 7, 15, 14, 7, tzinfo=timezone.utc)}
    database_engine, sessions = _sessions(tmp_path, monkeypatch, now_ref)
    with sessions() as db:
        db.add(
            Strategy(
                id="boundary-strategy",
                owner_user_id=7,
                name="Boundary strategy",
                description="",
                is_template=False,
                enabled=True,
                definition=_definition(
                    "Boundary strategy",
                    symbols=["SPY"],
                    timeframe="15Min",
                    warmup_bars=30,
                ),
            )
        )
        db.commit()

    class BoundaryAlpaca(CountingAlpaca):
        def recent_bars(self, symbols, timeframe, bars):
            self.calls["bars"] += 1
            index = pd.date_range(
                end="2026-07-15T14:00:00Z",
                periods=31,
                freq="15min",
            )
            frame = pd.DataFrame(
                {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1000.0,
                },
                index=index,
            )
            return {symbol: frame.copy() for symbol in symbols}

    evaluations: list[pd.Timestamp] = []
    original_evaluate_latest = engine_module.evaluate_latest

    def counted_evaluate_latest(frame, tree):
        evaluations.append(frame.index[-1])
        return original_evaluate_latest(frame, tree)

    monkeypatch.setattr(engine_module, "evaluate_latest", counted_evaluate_latest)
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        BoundaryAlpaca(),
        user_id=7,
    )

    await engine.evaluate_active_strategies(force=True)
    assert engine._last_evaluated[("boundary-strategy", "SPY")] == (
        "2026-07-15T13:45:00+00:00"
    )
    assert evaluations == [
        pd.Timestamp("2026-07-15T13:45:00Z"),
        pd.Timestamp("2026-07-15T13:45:00Z"),
    ]

    now_ref["value"] = datetime(2026, 7, 15, 14, 15, tzinfo=timezone.utc)
    await engine.evaluate_active_strategies(force=True)
    await engine.evaluate_active_strategies(force=True)
    assert engine._last_evaluated[("boundary-strategy", "SPY")] == (
        "2026-07-15T14:00:00+00:00"
    )
    assert evaluations == [
        pd.Timestamp("2026-07-15T13:45:00Z"),
        pd.Timestamp("2026-07-15T13:45:00Z"),
        pd.Timestamp("2026-07-15T14:00:00Z"),
        pd.Timestamp("2026-07-15T14:00:00Z"),
    ]
    database_engine.dispose()


@pytest.mark.asyncio
async def test_insufficient_warmup_history_is_not_consumed_and_log_is_rate_limited(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)

    class ShortHistoryAlpaca(CountingAlpaca):
        def recent_bars(self, symbols, timeframe, bars):
            self.calls["bars"] += 1
            index = pd.date_range(
                end=datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc),
                periods=10,
                freq="15min",
            )
            frame = pd.DataFrame(
                {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1000.0,
                },
                index=index,
            )
            return {symbol: frame.copy() for symbol in symbols}

    evaluations = 0

    def fail_if_evaluated(*_args, **_kwargs):
        nonlocal evaluations
        evaluations += 1
        raise AssertionError("insufficient history must not reach rule evaluation")

    monkeypatch.setattr(engine_module, "evaluate_latest", fail_if_evaluated)
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        ShortHistoryAlpaca(),
        user_id=7,
    )

    await engine.evaluate_active_strategies(force=True)
    await engine.evaluate_active_strategies(force=True)

    with sessions() as db:
        events = db.scalars(
            select(EventLog).where(
                EventLog.user_id == 7,
                EventLog.category == "market_data",
            )
        ).all()
    assert evaluations == 0
    assert engine._last_evaluated == {}
    assert len(events) == 2  # one rate-limited warning per strategy/symbol
    assert all("历史K线不足" in event.message for event in events)
    database_engine.dispose()


@pytest.mark.asyncio
async def test_indicator_period_can_require_more_history_than_configured_warmup(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    definition = _definition(
        "Long indicator strategy", symbols=["SPY"], warmup_bars=30
    )
    definition["entry"]["children"][0]["left"] = {
        "kind": "indicator",
        "indicator": "SMA",
        "field": "value",
        "params": {"period": 50},
        "offset": 0,
    }
    with sessions() as db:
        db.add(
            Strategy(
                id="long-indicator-strategy",
                owner_user_id=7,
                name="Long indicator strategy",
                description="",
                is_template=False,
                enabled=True,
                definition=definition,
            )
        )
        db.commit()

    class FortyBarsAlpaca(CountingAlpaca):
        def recent_bars(self, symbols, timeframe, bars):
            self.calls["bars"] += 1
            self.recent_calls.append((list(symbols), timeframe, bars))
            index = pd.date_range(
                end=datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc),
                periods=41,
                freq="15min",
            )
            frame = pd.DataFrame(
                {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1000.0,
                },
                index=index,
            )
            return {symbol: frame.copy() for symbol in symbols}

    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        FortyBarsAlpaca(),
        user_id=7,
    )
    await engine.evaluate_active_strategies(force=True)

    with sessions() as db:
        event = db.scalar(
            select(EventLog).where(
                EventLog.user_id == 7,
                EventLog.category == "market_data",
            )
        )
    assert engine._last_evaluated == {}
    assert event is not None
    assert event.details["available_bars"] == 40
    assert event.details["required_bars"] == 50
    assert engine.alpaca.recent_calls == [(["SPY"], "15Min", 70)]
    database_engine.dispose()


@pytest.mark.asyncio
async def test_evaluation_interval_is_fifteen_seconds_and_closed_market_is_sixty(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    fixed_now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)

    open_alpaca = CountingAlpaca()
    open_engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        open_alpaca,
        user_id=7,
    )
    await open_engine.evaluate_active_strategies()
    await open_engine.evaluate_active_strategies()
    assert open_alpaca.calls["clock"] == 1
    assert (open_engine._next_strategy_evaluation - fixed_now).total_seconds() == 15

    closed_alpaca = CountingAlpaca()
    closed_alpaca.market_open = False
    closed_engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused-2.db")),
        closed_alpaca,
        user_id=7,
    )
    await closed_engine.evaluate_active_strategies()
    assert closed_alpaca.calls["clock"] == 1
    assert (closed_engine._next_strategy_evaluation - fixed_now).total_seconds() == 60
    database_engine.dispose()


@pytest.mark.asyncio
async def test_pending_reconciliation_reserves_exposure_and_recovers_idempotently(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    with sessions() as db:
        db.add(
            Signal(
                id="pending-signal",
                user_id=7,
                unique_key="pending-signal-key",
                strategy_id="strategy-00",
                symbol="SPY",
                bar_timestamp=datetime(2026, 7, 15, 14, 45, tzinfo=timezone.utc),
                action="buy",
                price=100,
                reason="entry",
                status="pending_reconciliation",
                payload={
                    "client_order_id": "qp-pending-order",
                    "qty": 1,
                    "notional": 100,
                },
            )
        )
        db.commit()

    alpaca = CountingAlpaca()
    alpaca.positions = [{"symbol": "SPY", "qty": "1"}]
    alpaca.all_orders = [
        {
            "id": "alpaca-order-1",
            "client_order_id": "qp-pending-order",
            "symbol": "SPY",
            "side": "buy",
            "type": "market",
            "qty": "1",
            "filled_qty": "1",
            "filled_avg_price": "100",
            "status": "filled",
        }
    ]
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    enriched = engine._enrich_open_orders([])
    assert enriched == [
        {
            "id": "pending:pending-signal",
            "client_order_id": "qp-pending-order",
            "symbol": "SPY",
            "side": "buy",
            "qty": 1,
            "status": "pending_reconciliation",
            "_estimated_notional": 100,
        }
    ]
    assert engine._has_pending_reconciliation("strategy-00", "SPY") is True

    await engine.reconcile_orders()
    await engine.reconcile_orders()

    with sessions() as db:
        signal = db.get(Signal, "pending-signal")
        records = db.scalars(select(OrderRecord)).all()
        position = db.scalar(select(StrategyPosition))
    assert signal is not None and signal.status == "submitted"
    assert signal.payload["reconciled_order_id"] == "alpaca-order-1"
    assert len(records) == 1
    assert records[0].client_order_id == "qp-pending-order"
    assert position is not None and position.qty == 1
    assert engine._has_pending_reconciliation("strategy-00", "SPY") is False
    database_engine.dispose()


@pytest.mark.asyncio
async def test_nested_bracket_sell_leg_fill_reduces_strategy_position_once(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    with sessions() as db:
        db.add_all(
            [
                Signal(
                    id="bracket-signal",
                    user_id=7,
                    unique_key="bracket-signal-key",
                    strategy_id="strategy-00",
                    symbol="SPY",
                    bar_timestamp=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
                    action="buy",
                    price=100,
                    reason="entry",
                    status="submitted",
                    payload={},
                ),
                OrderRecord(
                    id="bracket-parent",
                    user_id=7,
                    client_order_id="bracket-parent-client",
                    strategy_id="strategy-00",
                    signal_id="bracket-signal",
                    symbol="SPY",
                    side="buy",
                    order_type="market",
                    qty=10,
                    notional=1000,
                    status="filled",
                    filled_qty=10,
                ),
                StrategyPosition(
                    user_id=7,
                    strategy_id="strategy-00",
                    symbol="SPY",
                    qty=10,
                ),
            ]
        )
        db.commit()
    alpaca = CountingAlpaca()
    alpaca.positions = [{"symbol": "SPY", "qty": "7"}]
    alpaca.all_orders = [
        {
            "id": "bracket-parent",
            "client_order_id": "bracket-parent-client",
            "symbol": "SPY",
            "side": "buy",
            "type": "market",
            "qty": "10",
            "filled_qty": "10",
            "status": "filled",
            "legs": [
                {
                    "id": "bracket-stop-leg",
                    "client_order_id": "bracket-stop-client",
                    "symbol": "SPY",
                    "side": "sell",
                    "type": "stop",
                    "qty": "10",
                    "filled_qty": "3",
                    "filled_avg_price": "95",
                    "status": "partially_filled",
                }
            ],
        }
    ]
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    await engine.reconcile_orders()
    await engine.reconcile_orders()

    with sessions() as db:
        position = db.scalar(select(StrategyPosition))
        leg = db.get(OrderRecord, "bracket-stop-leg")
    assert position is not None and position.qty == 7
    assert leg is not None and leg.filled_qty == 3
    database_engine.dispose()


@pytest.mark.asyncio
async def test_dead_stream_is_restarted_even_when_symbol_signature_is_unchanged(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    alpaca = CountingAlpaca()
    alpaca.stream_healthy = False
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )
    engine._stream_signature = ("QQQ", "SPY")

    await engine._sync_streams()
    await engine._sync_streams()

    assert alpaca.stream_starts == [["QQQ", "SPY"]]
    database_engine.dispose()


@pytest.mark.asyncio
async def test_legacy_database_over_stream_limit_never_starts_partial_subscription(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    symbols = ["SPY", "QQQ"] + [f"S{index:02d}" for index in range(29)]
    with sessions() as db:
        db.add_all(
            [WatchlistItem(user_id=7, symbol=symbol) for symbol in symbols]
        )
        db.commit()
    alpaca = CountingAlpaca()
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    await engine._sync_streams()
    await engine._sync_streams()

    with sessions() as db:
        errors = db.scalars(
            select(EventLog).where(
                EventLog.user_id == 7,
                EventLog.category == "market_data",
                EventLog.level == "error",
            )
        ).all()
        db.query(WatchlistItem).filter(
            WatchlistItem.user_id == 7,
            WatchlistItem.symbol == symbols[-1],
        ).delete()
        db.commit()
    assert alpaca.stream_starts == []
    assert len(errors) == 1
    assert errors[0].details["symbol_count"] == 31

    await engine._sync_streams()
    assert len(alpaca.stream_starts) == 1
    assert len(alpaca.stream_starts[0]) == 30
    database_engine.dispose()


@pytest.mark.asyncio
async def test_missing_ambiguous_order_unblocks_after_safe_reconciliation_window(
    tmp_path, monkeypatch
) -> None:
    database_engine, sessions = _sessions(tmp_path, monkeypatch)
    _add_strategies(sessions, count=1)
    with sessions() as db:
        db.add(
            Signal(
                id="missing-pending-signal",
                user_id=7,
                unique_key="missing-pending-signal-key",
                strategy_id="strategy-00",
                symbol="SPY",
                bar_timestamp=datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc),
                action="buy",
                price=100,
                reason="entry",
                status="pending_reconciliation",
                payload={
                    "client_order_id": "qp-missing-order",
                    "qty": 1,
                    "notional": 100,
                },
                created_at=datetime(2026, 7, 15, 14, 50, tzinfo=timezone.utc),
            )
        )
        db.commit()

    alpaca = CountingAlpaca()
    engine = TradingEngine(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")),
        alpaca,
        user_id=7,
    )

    await engine.reconcile_orders()
    await engine.reconcile_orders()
    assert engine._has_pending_reconciliation("strategy-00", "SPY") is True
    await engine.reconcile_orders()

    with sessions() as db:
        signal = db.get(Signal, "missing-pending-signal")
    assert signal is not None and signal.status == "rejected"
    assert signal.payload["reconciliation_misses"] == 3
    assert signal.payload["reconciliation_resolution"] == "not_found"
    assert engine._has_pending_reconciliation("strategy-00", "SPY") is False
    database_engine.dispose()
