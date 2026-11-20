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


@pytest.mark.asyncio
async def test_queued_backtest_releases_request_connection_before_background_work(tmp_path):
    from fastapi import BackgroundTasks
    from app.schemas import BacktestSummaryRead

    database_engine = create_engine(
        f"sqlite:///{tmp_path / 'small-pool.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    with sessions() as seed:
        seed.add(Strategy(id='strategy', owner_user_id=1, name='strategy',
                          definition=TEMPLATES['sma_cross'], is_template=False))
        seed.commit()

    db = sessions()
    try:
        tasks = BackgroundTasks()
        request = BacktestRequest(
            strategy_id='strategy', start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end=datetime(2025, 2, 1, tzinfo=timezone.utc),
        )
        broker = type('ConfiguredBroker', (), {'configured': True})()
        queued = await api.create_backtest(request, tasks, db=db, alpaca=broker, user_id=1)
        assert len(tasks.tasks) == 1
        assert database_engine.pool.checkedout() == 0
        # A worker must be able to use the only connection while request cleanup
        # is deferred until all background tasks finish.
        with sessions() as worker:
            assert worker.get(BacktestRun, queued.id).status == 'queued'
        assert BacktestSummaryRead.model_validate(queued).status == 'queued'
    finally:
        db.close()
        database_engine.dispose()


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
        assert saved.parameters['strategy_definition'] == definition
        assert saved.parameters['dataset_sha256']
        assert saved.parameters['engine']['source_sha256']
    database_engine.dispose()


def test_frozen_bundle_replays_without_network_and_detects_tampering():
    import gzip
    import json
    from app.services.backtest_artifacts import pack_experiment, replay_experiment
    from app.services.backtest import run_backtest
    from test_backtest_correctness import rule, bars
    definition = rule()
    frame = bars()
    frames = {'AAA': frame, 'SPY': frame}
    payload = BacktestRequest(strategy_id='s', start=frame.index[30].to_pydatetime(),
                              end=frame.index[-1].to_pydatetime(), slippage_bps=0)
    expected = run_backtest(definition, {'AAA': frame}, slippage_bps=0, start=payload.start,
                            benchmark=frame.loc[frame.index >= payload.start])
    content, _ = pack_experiment(definition.model_dump(mode='json'), payload.model_dump(mode='json'), frames, {})
    replay = replay_experiment(content)
    assert replay.metrics == expected.metrics
    assert replay.trades == expected.trades
    assert replay.equity_curve == expected.equity_curve
    bundle = json.loads(gzip.decompress(content))
    bundle['data']['AAA']['frame']['data'][0][0] += 1
    with pytest.raises(ValueError, match='数据校验失败'):
        replay_experiment(gzip.compress(json.dumps(bundle).encode()))


def test_artifact_export_is_scoped_to_run_owner(tmp_path):
    from fastapi import HTTPException
    from app.models import BacktestArtifact
    from app.api import export_backtest_artifact
    database_engine = create_engine(f"sqlite:///{tmp_path / 'artifact.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine)
    with sessions() as db:
        db.add(BacktestRun(id='run', user_id=1, strategy_id='s', status='completed'))
        db.add(BacktestArtifact(run_id='run', content=b'private experiment'))
        db.commit()
        assert export_backtest_artifact('run', db=db, user_id=1).body == b'private experiment'
        with pytest.raises(HTTPException) as error:
            export_backtest_artifact('run', db=db, user_id=2)
        assert error.value.status_code == 404
    database_engine.dispose()
