from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import signals
from app.database import Base
from app.models import Signal, OrderRecord


def test_signal_exposes_partial_fill_even_if_order_was_canceled(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'signals.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            Signal(
                id="s",
                user_id=1,
                strategy_id="strategy",
                symbol="SPY",
                unique_key="s",
                action="buy",
                price=100,
                reason="breakout",
                status="submitted",
                bar_timestamp=datetime.now(timezone.utc),
            )
        )
        db.add(
            OrderRecord(
                id="o",
                user_id=1,
                strategy_id="strategy",
                signal_id="s",
                client_order_id="qp-b-test",
                symbol="SPY",
                side="buy",
                order_type="market",
                qty=20,
                filled_qty=12,
                status="OrderStatus.CANCELED",
            )
        )
        db.commit()
        row = signals(limit=50, db=db, user_id=1)[0]
        assert row["execution_status"] == "partially_filled_canceled"
        assert row["filled_qty"] == 12
        assert signals(limit=50, db=db, user_id=2) == []
    engine.dispose()
