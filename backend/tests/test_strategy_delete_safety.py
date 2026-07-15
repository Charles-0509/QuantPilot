from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import delete_strategy, update_strategy
from app.database import Base
from app.models import OrderRecord, Signal, Strategy, StrategyPosition
from app.schemas import RuleDefinition, StrategyCreate
from app.templates import TEMPLATES


def _database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'delete-safety.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _strategy() -> Strategy:
    return Strategy(
        id="strategy-delete-test",
        owner_user_id=7,
        name="Delete safety",
        description="",
        is_template=False,
        enabled=False,
        definition={},
    )


@pytest.mark.parametrize(
    "kind",
    ["pending_signal", "pending_trailing", "active_order", "owned_position"],
)
def test_strategy_with_unresolved_order_evidence_cannot_be_deleted(
    tmp_path, kind: str
) -> None:
    engine, sessions = _database(tmp_path)
    with sessions() as db:
        strategy = _strategy()
        db.add(strategy)
        if kind in {"pending_signal", "pending_trailing"}:
            db.add(
                Signal(
                    id="pending-signal",
                    user_id=7,
                    unique_key="pending-delete-safety",
                    strategy_id=strategy.id,
                    symbol="SPY",
                    bar_timestamp=datetime(2026, 7, 15, tzinfo=timezone.utc),
                    action="buy",
                    price=100,
                    reason="test",
                    status=(
                        "pending_reconciliation"
                        if kind == "pending_signal"
                        else "submitted"
                    ),
                    payload=(
                        {"client_order_id": "qp-pending-delete"}
                        if kind == "pending_signal"
                        else {
                            "trailing_stop_intent": {
                                "status": "pending_submission",
                                "client_order_id": "qp-trailing-delete",
                            }
                        }
                    ),
                )
            )
        elif kind == "active_order":
            db.add(
                OrderRecord(
                    id="active-order",
                    user_id=7,
                    client_order_id="qp-active-delete",
                    strategy_id=strategy.id,
                    signal_id=None,
                    symbol="SPY",
                    side="buy",
                    order_type="market",
                    qty=1,
                    notional=None,
                    status="accepted",
                )
            )
        else:
            db.add(
                StrategyPosition(
                    user_id=7,
                    strategy_id=strategy.id,
                    symbol="SPY",
                    qty=1,
                )
            )
        db.commit()

        with pytest.raises(HTTPException) as captured:
            delete_strategy(strategy.id, db, user_id=7)

        definition = dict(TEMPLATES["sma_cross"])
        definition["name"] = "Mutated while pending"
        with pytest.raises(HTTPException) as update_error:
            update_strategy(
                strategy.id,
                StrategyCreate(
                    definition=RuleDefinition.model_validate(definition)
                ),
                db,
                user_id=7,
            )

        assert captured.value.status_code == 409
        assert update_error.value.status_code == 409
        assert db.get(Strategy, strategy.id) is not None
    engine.dispose()


def test_strategy_can_be_deleted_after_orders_are_terminal(tmp_path) -> None:
    engine, sessions = _database(tmp_path)
    with sessions() as db:
        strategy = _strategy()
        db.add(strategy)
        db.add(
            OrderRecord(
                id="filled-order",
                user_id=7,
                client_order_id="qp-filled-delete",
                strategy_id=strategy.id,
                signal_id=None,
                symbol="SPY",
                side="buy",
                order_type="market",
                qty=1,
                notional=None,
                status="filled",
            )
        )
        db.commit()

        delete_strategy(strategy.id, db, user_id=7)

        assert db.get(Strategy, strategy.id) is None
    engine.dispose()
