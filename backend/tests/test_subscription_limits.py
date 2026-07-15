from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import enable_strategy, restore_strategy, update_strategy, update_watchlist
from app.database import Base
from app.models import Strategy, StrategyVersion, WatchlistItem
from app.schemas import RuleDefinition, StrategyCreate, WatchlistUpdate
from app.templates import TEMPLATES


class ConfiguredAlpaca:
    configured = True


def _database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'subscription-limit.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _symbols(prefix: str, count: int) -> list[str]:
    return [f"{prefix}{index:02d}" for index in range(count)]


def _strategy(strategy_id: str, symbols: list[str], *, enabled: bool) -> Strategy:
    definition = deepcopy(TEMPLATES["sma_cross"])
    definition["name"] = strategy_id
    definition["symbols"] = symbols
    return Strategy(
        id=strategy_id,
        owner_user_id=7,
        name=strategy_id,
        description="",
        is_template=False,
        enabled=enabled,
        definition=definition,
    )


def test_watchlist_rejects_union_over_free_iex_limit(tmp_path) -> None:
    engine, sessions = _database(tmp_path)
    with sessions() as db:
        db.add(_strategy("enabled-strategy", _symbols("A", 16), enabled=True))
        db.commit()

        with pytest.raises(HTTPException) as captured:
            update_watchlist(
                WatchlistUpdate(symbols=_symbols("B", 16)), db, user_id=7
            )

        assert captured.value.status_code == 409
        assert db.query(WatchlistItem).count() == 0
    engine.dispose()


def test_enabling_strategy_rejects_watchlist_union_over_limit(tmp_path) -> None:
    engine, sessions = _database(tmp_path)
    with sessions() as db:
        candidate = _strategy("candidate-strategy", _symbols("B", 16), enabled=False)
        db.add(candidate)
        for symbol in _symbols("A", 16):
            db.add(WatchlistItem(user_id=7, symbol=symbol))
        db.commit()

        with pytest.raises(HTTPException) as captured:
            enable_strategy(candidate.id, db, ConfiguredAlpaca(), user_id=7)

        assert captured.value.status_code == 409
        assert candidate.enabled is False
    engine.dispose()


def test_enabled_strategy_cannot_be_edited_or_restored_past_subscription_limit(
    tmp_path,
) -> None:
    engine, sessions = _database(tmp_path)
    with sessions() as db:
        strategy = _strategy("enabled-strategy", _symbols("A", 16), enabled=True)
        db.add(strategy)
        for symbol in _symbols("A", 16):
            db.add(WatchlistItem(user_id=7, symbol=symbol))
        oversized = deepcopy(TEMPLATES["sma_cross"])
        oversized["name"] = "oversized"
        oversized["symbols"] = _symbols("B", 16)
        db.add(
            StrategyVersion(
                strategy_id=strategy.id,
                version=99,
                definition=oversized,
            )
        )
        db.commit()

        payload = StrategyCreate(definition=RuleDefinition.model_validate(oversized))
        with pytest.raises(HTTPException) as edit_error:
            update_strategy(strategy.id, payload, db, user_id=7)
        with pytest.raises(HTTPException) as restore_error:
            restore_strategy(strategy.id, 99, db, user_id=7)

        assert edit_error.value.status_code == 409
        assert restore_error.value.status_code == 409
        assert strategy.definition["symbols"] == _symbols("A", 16)
    engine.dispose()
