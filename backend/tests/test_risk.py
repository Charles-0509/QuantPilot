from __future__ import annotations

from datetime import datetime, timezone

from app.models import EngineState, RiskSettings
from app.services.risk import RiskManager


def test_risk_rejects_position_above_symbol_limit() -> None:
    decision = RiskManager().check_entry(
        symbol="SPY",
        desired_notional=1500,
        account={"equity": "10000", "buying_power": "20000", "trading_blocked": False},
        positions=[],
        asset={"tradable": True, "status": "active"},
        clock={"is_open": True},
        bar_timestamp=datetime.now(timezone.utc),
        max_data_age_seconds=900,
        settings=RiskSettings(id=1),
        state=EngineState(id=1),
        strategy_max_symbol_pct=10,
        strategy_max_positions=8,
    )
    assert decision.allowed is False
    assert "单只股票" in decision.reason
