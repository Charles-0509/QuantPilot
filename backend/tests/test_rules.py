from __future__ import annotations

import pandas as pd

from app.schemas import ConditionGroup, Operand
from app.services.indicators import indicator_series
from app.services.rules import evaluate_tree


def frame(values: list[float]) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=len(values), freq="D", tz="UTC")
    close = pd.Series(values, index=index)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000,
        },
        index=index,
    )


def test_crosses_above_only_fires_on_cross() -> None:
    data = frame([8, 9, 10, 12, 13])
    rule = ConditionGroup.model_validate(
        {
            "type": "group",
            "op": "AND",
            "children": [
                {
                    "type": "condition",
                    "left": {"kind": "price", "field": "close"},
                    "operator": "crosses_above",
                    "right": {"kind": "number", "value": 10},
                }
            ],
        }
    )
    result = evaluate_tree(data, rule)
    assert result.tolist() == [False, False, False, True, False]


def test_highest_excludes_current_bar() -> None:
    data = frame([10, 11, 12, 50])
    operand = Operand.model_validate(
        {
            "kind": "indicator",
            "indicator": "HIGHEST",
            "field": "value",
            "params": {"period": 3, "exclude_current": True},
        }
    )
    result = indicator_series(data, operand)
    assert result.iloc[-1] == 12
