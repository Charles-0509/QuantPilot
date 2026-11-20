"""Broker-compatible quantity precision shared by historical and Paper execution."""

import math

from app.schemas import RuleDefinition


def normalize_entry_qty(
    qty: float, definition: RuleDefinition, *, fractionable: bool = True
) -> float:
    if not math.isfinite(qty) or qty <= 0:
        return 0.0
    order = definition.order
    # QuantPilot uses regular hours only. Protective orders and GTC entries
    # use whole shares; never round up beyond the already-approved budget.
    whole = (
        not fractionable
        or order.stop_loss is not None
        or order.trailing_stop is not None
        or order.time_in_force == "gtc"
    )
    return float(math.floor(qty)) if whole else math.floor(qty * 1_000_000) / 1_000_000
