from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import api
from app.database import Base
from app.models import BacktestRun, Strategy
from app.schemas import BacktestRequest
from app.templates import TEMPLATES


def test_backtest_request_normalizes_override_symbols_and_benchmark() -> None:
    request = BacktestRequest(
        strategy_id="strategy",
        symbols=[" googl ", "msft"],
        benchmark=" qqq ",
        start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end=datetime(2025, 2, 1, tzinfo=timezone.utc),
    )
    assert request.symbols == ["GOOGL", "MSFT"]
    assert request.benchmark == "QQQ"


@pytest.mark.asyncio
async def test_background_job_batches_symbols_and_persists_result(monkeypatch, tmp_path) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'backtest-job.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    definition = dict(TEMPLATES["sma_cross"])
    definition["symbols"] = ["GOOGL"]
    with sessions() as db:
        strategy = Strategy(
            id="strategy-id",
            name="测试策略",
            description="",
            is_template=False,
            definition=definition,
        )
        run = BacktestRun(id="run-id", strategy_id=strategy.id, status="queued", parameters={})
        db.add_all([strategy, run])
        db.commit()

    index = pd.date_range("2025-01-01", periods=80, freq="D", tz="UTC")
    prices = pd.Series(range(100, 180), index=index, dtype=float)
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": prices + 1,
            "low": prices - 1,
            "close": prices,
            "volume": 1000,
        },
        index=index,
    )

    class FakeAlpaca:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], bool]] = []

        def get_bars(self, symbols, _timeframe, _start, _end, *, use_cache=False):
            self.calls.append((symbols, use_cache))
            return {symbol: frame.copy() for symbol in symbols}

    fake_alpaca = FakeAlpaca()
    monkeypatch.setattr(api, "SessionLocal", sessions)
    payload = BacktestRequest(
        strategy_id="strategy-id",
        symbols=["GOOGL"],
        benchmark="QQQ",
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
    )

    await api.execute_backtest_job(
        1,
        "run-id",
        definition,
        payload.model_dump(mode="json"),
        fake_alpaca,
    )

    assert fake_alpaca.calls == [(["GOOGL", "QQQ"], True)]
    with sessions() as db:
        saved = db.get(BacktestRun, "run-id")
        assert saved is not None
        assert saved.status == "completed"
        assert saved.metrics["trade_count"] >= 0
        assert saved.equity_curve
        assert saved.benchmark_curve
    database_engine.dispose()
