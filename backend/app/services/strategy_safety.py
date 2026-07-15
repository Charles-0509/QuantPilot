from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord, Signal, Strategy, StrategyPosition


UNRESOLVED_SIGNAL_STATUSES = {"pending_submission", "pending_reconciliation"}
TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "replaced",
}


def strategy_has_unresolved_execution(
    db: Session, user_id: int, strategy_id: str
) -> bool:
    signals = db.scalars(
        select(Signal).where(
            Signal.user_id == user_id,
            Signal.strategy_id == strategy_id,
        )
    ).all()
    unresolved_signal = any(
        signal.status in UNRESOLVED_SIGNAL_STATUSES
        or ((signal.payload or {}).get("trailing_stop_intent") or {}).get("status")
        in UNRESOLVED_SIGNAL_STATUSES
        for signal in signals
    )
    active_order = db.scalar(
        select(OrderRecord.id).where(
            OrderRecord.user_id == user_id,
            OrderRecord.strategy_id == strategy_id,
            ~OrderRecord.status.in_(TERMINAL_ORDER_STATUSES),
        )
    )
    owned_position = db.scalar(
        select(StrategyPosition.id).where(
            StrategyPosition.user_id == user_id,
            StrategyPosition.strategy_id == strategy_id,
            StrategyPosition.qty > 0,
        )
    )
    return unresolved_signal or active_order is not None or owned_position is not None


def user_has_unresolved_execution(db: Session, user_id: int) -> bool:
    strategy_ids = db.scalars(
        select(Strategy.id).where(Strategy.owner_user_id == user_id)
    ).all()
    return any(
        strategy_has_unresolved_execution(db, user_id, strategy_id)
        for strategy_id in strategy_ids
    )
