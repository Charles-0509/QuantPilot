from __future__ import annotations

import pandas as pd

from app.schemas import RuleDefinition
from app.services.backtest import run_backtest


def definition(stop: float | None = None, take: float | None = None) -> RuleDefinition:
    return RuleDefinition.model_validate(
        {
            "version": 1,
            "name": "测试策略",
            "description": "",
            "symbols": ["SPY"],
            "timeframe": "1Day",
            "warmup_bars": 30,
            "schedule": {"session": "regular", "weekdays": [0, 1, 2, 3, 4]},
            "entry": {
                "type": "group",
                "op": "AND",
                "children": [
                    {
                        "type": "condition",
                        "left": {"kind": "number", "value": 1},
                        "operator": "==",
                        "right": {"kind": "number", "value": 1},
                    }
                ],
            },
            "exit": {
                "type": "group",
                "op": "AND",
                "children": [
                    {
                        "type": "condition",
                        "left": {"kind": "number", "value": 1},
                        "operator": "==",
                        "right": {"kind": "number", "value": 0},
                    }
                ],
            },
            "position": {"mode": "percent_equity", "value": 10, "allow_pyramiding": False, "max_additions": 1},
            "order": {
                "type": "market",
                "limit_offset_bps": 10,
                "time_in_force": "day",
                "stop_loss": {"mode": "percent", "value": stop, "atr_period": 14} if stop else None,
                "take_profit": {"mode": "percent", "value": take, "atr_period": 14} if take else None,
                "trailing_stop": None,
            },
            "risk": {"max_symbol_pct": 20, "max_positions": 8, "cooldown_bars": 1},
        }
    )


def test_signal_executes_on_next_bar_open() -> None:
    index = pd.date_range("2025-01-01", periods=4, freq="D", tz="UTC")
    bars = pd.DataFrame(
        {"open": [100, 101, 102, 103], "high": [101, 102, 103, 104], "low": [99, 100, 101, 102], "close": [100, 101, 102, 103], "volume": 1000},
        index=index,
    )
    result = run_backtest(definition(), {"SPY": bars}, initial_cash=10_000, slippage_bps=0)
    assert result.trades[0]["entry_time"] == index[1].isoformat()
    assert result.trades[0]["entry_price"] == 101


def test_stop_is_conservative_when_stop_and_target_hit_same_bar() -> None:
    index = pd.date_range("2025-01-01", periods=3, freq="D", tz="UTC")
    bars = pd.DataFrame(
        {"open": [100, 100, 100], "high": [101, 110, 101], "low": [99, 90, 99], "close": [100, 100, 100], "volume": 1000},
        index=index,
    )
    result = run_backtest(definition(stop=2, take=4), {"SPY": bars}, initial_cash=10_000, slippage_bps=0)
    assert result.trades[0]["reason"] == "止损/移动止损"
    assert result.trades[0]["exit_price"] == 98
