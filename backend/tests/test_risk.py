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


def test_strategy_limit_is_stricter_than_global_limit_and_explained() -> None:
    decision = RiskManager().check_entry(
        symbol="SPY",
        desired_notional=12000,
        account={"equity": "100000", "buying_power": "200000", "trading_blocked": False},
        positions=[],
        asset={"tradable": True, "status": "active"},
        clock={"is_open": True},
        bar_timestamp=datetime.now(timezone.utc),
        max_data_age_seconds=900,
        settings=RiskSettings(id=1, max_symbol_pct=50),
        state=EngineState(id=1),
        strategy_max_symbol_pct=10,
        strategy_max_positions=8,
    )
    assert decision.allowed is False
    assert "全局 50.0%" in decision.reason
    assert "策略 10.0%" in decision.reason
    assert "本次 $12,000.00" in decision.reason


def test_open_buy_orders_are_included_in_projected_exposure() -> None:
    decision = RiskManager().check_entry(
        symbol="SPY",
        desired_notional=1500,
        account={"equity": "10000", "buying_power": "20000", "trading_blocked": False},
        positions=[],
        asset={"tradable": True, "status": "active"},
        clock={"is_open": True},
        bar_timestamp=datetime.now(timezone.utc),
        max_data_age_seconds=900,
        settings=RiskSettings(id=1, max_symbol_pct=30),
        state=EngineState(id=1),
        strategy_max_symbol_pct=30,
        strategy_max_positions=8,
        open_orders=[
            {
                "symbol": "SPY",
                "side": "buy",
                "status": "accepted",
                "qty": "4",
                "filled_qty": "0",
                "limit_price": "500",
            }
        ],
        reference_price=500,
    )
    assert decision.allowed is False
    assert "待成交 $2,000.00" in decision.reason
