from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.schemas import Condition, ConditionGroup, ConditionNode, Operand

from .indicators import ensure_frame, indicator_series


@dataclass(slots=True)
class RuleResult:
    matched: bool
    timestamp: pd.Timestamp | None
    labels: list[str]


def _evaluate_condition(frame: pd.DataFrame, condition: Condition) -> pd.Series:
    left = indicator_series(frame, condition.left)
    right = indicator_series(frame, condition.right)
    operator = condition.operator

    if operator == ">":
        result = left > right
    elif operator == ">=":
        result = left >= right
    elif operator == "<":
        result = left < right
    elif operator == "<=":
        result = left <= right
    elif operator == "==":
        result = left == right
    elif operator == "crosses_above":
        result = (left > right) & (left.shift(1) <= right.shift(1))
    elif operator == "crosses_below":
        result = (left < right) & (left.shift(1) >= right.shift(1))
    else:
        raise ValueError(f"不支持的比较运算: {operator}")
    return result.fillna(False)


def evaluate_tree(frame: pd.DataFrame, node: ConditionNode | dict) -> pd.Series:
    frame = ensure_frame(frame)
    if isinstance(node, dict):
        if node.get("type") == "condition":
            node = Condition.model_validate(node)
        else:
            node = ConditionGroup.model_validate(node)

    if isinstance(node, Condition):
        return _evaluate_condition(frame, node)

    if not node.children:
        result = pd.Series(False, index=frame.index)
    else:
        children = [evaluate_tree(frame, child) for child in node.children]
        result = children[0].copy()
        for child in children[1:]:
            result = result & child if node.op == "AND" else result | child
    if node.negate:
        result = ~result
    return result.fillna(False)


def _matched_labels(frame: pd.DataFrame, node: ConditionNode | dict) -> list[str]:
    if isinstance(node, dict):
        if node.get("type") == "condition":
            node = Condition.model_validate(node)
        else:
            node = ConditionGroup.model_validate(node)
    if isinstance(node, Condition):
        result = _evaluate_condition(frame, node)
        return [node.label or _condition_text(node)] if bool(result.iloc[-1]) else []
    labels: list[str] = []
    for child in node.children:
        labels.extend(_matched_labels(frame, child))
    return labels


def evaluate_latest(frame: pd.DataFrame, node: ConditionNode | dict) -> RuleResult:
    frame = ensure_frame(frame)
    if frame.empty:
        return RuleResult(False, None, [])
    result = evaluate_tree(frame, node)
    return RuleResult(
        matched=bool(result.iloc[-1]),
        timestamp=frame.index[-1],
        labels=_matched_labels(frame, node),
    )


def _operand_text(operand: Operand) -> str:
    if operand.kind == "number":
        return str(operand.value)
    if operand.kind == "price":
        return str(operand.field)
    return f"{operand.indicator}({','.join(f'{k}={v}' for k, v in operand.params.items())})"


def _condition_text(condition: Condition) -> str:
    return f"{_operand_text(condition.left)} {condition.operator} {_operand_text(condition.right)}"
