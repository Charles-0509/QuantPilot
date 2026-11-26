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
    order_statuses = db.scalars(
        select(OrderRecord.status).where(
            OrderRecord.user_id == user_id,
            OrderRecord.strategy_id == strategy_id,
        )
    ).all()
    active_order = any(str(status).lower().split('.')[-1] not in TERMINAL_ORDER_STATUSES for status in order_statuses)
    owned_position = db.scalar(
        select(StrategyPosition.id).where(
            StrategyPosition.user_id == user_id,
            StrategyPosition.strategy_id == strategy_id,
            StrategyPosition.qty != 0,
        )
    )
    return unresolved_signal or active_order or owned_position is not None


def user_has_unresolved_execution(db: Session, user_id: int) -> bool:
    strategy_ids = db.scalars(
        select(Strategy.id).where(Strategy.owner_user_id == user_id)
    ).all()
    return any(
        strategy_has_unresolved_execution(db, user_id, strategy_id)
        for strategy_id in strategy_ids
    )
