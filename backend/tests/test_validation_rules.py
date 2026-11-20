from datetime import datetime, timedelta, timezone

import pandas as pd

from app.schemas import RuleDefinition
from app.services.rules import evaluate_tree
from app.validation_rules import SYMBOLS, validation_definitions


def test_eight_bounded_rules_trigger_in_short_window():
    definitions = validation_definitions(
        "test-run", datetime.now(timezone.utc) + timedelta(minutes=45)
    )
    assert [d["symbols"][0] for d in definitions] == SYMBOLS
    frame = pd.DataFrame(
        {
            "open": [100 + i * 0.01 for i in range(40)],
            "close": [100.005 + i * 0.01 for i in range(40)],
            "high": [100.02 + i * 0.01 for i in range(40)],
            "low": [99.99 + i * 0.01 for i in range(40)],
            "volume": 1000,
        },
        index=pd.date_range("2026-10-21T13:30:00Z", periods=40, freq="5min"),
    )
    for definition in definitions:
        rule = RuleDefinition.model_validate(definition)
        assert evaluate_tree(frame, rule.entry).tail(10).any()
        assert evaluate_tree(frame, rule.exit).tail(10).any()
        assert rule.position.value == 1 and not rule.position.allow_pyramiding
        assert rule.paper_validation.max_entries == 2
        assert rule.paper_validation.max_quantity == 1
        assert rule.paper_validation.max_notional == 1000
