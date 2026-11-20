"""Deliberately high-trigger Paper validation rules, not return-seeking strategies."""

from datetime import datetime

from app.schemas import RuleDefinition

SYMBOLS = ["QQQ", "SPCX", "AAPL", "GOOGL", "SCHD", "NVDA", "VOO", "TSLA"]


def price(field="close"):
    return {"kind": "price", "field": field}


def number(value):
    return {"kind": "number", "value": value}


def indicator(name, **params):
    return {"kind": "indicator", "indicator": name, "params": params, "field": "value"}


def condition(left, operator, right, label):
    return {"type": "condition", "left": left, "operator": operator, "right": right, "label": label}


def group(*children, op="OR"):
    return {"type": "group", "op": op, "children": list(children)}


def validation_definitions(run_id: str, expires_at: datetime):
    c = condition
    rsi = indicator("RSI", period=2)
    rules = {
        "QQQ": (
            "宽松动量",
            group(
                c(rsi, ">=", number(10), "RSI2≥10"),
                c(price(), ">=", indicator("EMA", period=3), "价格≥EMA3"),
            ),
            group(c(rsi, ">=", number(0), "下一根有效RSI即离场")),
        ),
        "SPCX": (
            "活跃成交",
            group(
                c(price(), ">", number(0), "有效价格"),
                c(price("volume"), ">", number(0), "存在成交"),
                op="AND",
            ),
            group(
                c(price(), ">=", price("open"), "收盘不低于开盘"),
                c(price(), "<", price("open"), "收盘低于开盘"),
            ),
        ),
        "AAPL": (
            "短线低点回归",
            group(
                c(price(), ">=", indicator("LOWEST", period=3), "价格≥前三根低收盘"),
                c(rsi, "<", number(90), "RSI2<90"),
            ),
            group(
                c(price(), ">", indicator("EMA", period=2), "价格高于EMA2"),
                c(price(), "<=", indicator("EMA", period=2), "价格不高于EMA2"),
            ),
        ),
        "GOOGL": (
            "快速动量保护单",
            group(
                c(indicator("MACD", fast=2, slow=3, signal=2), ">", number(0), "快速MACD>0"),
                c(rsi, "<", number(90), "RSI2<90"),
            ),
            group(
                c(price("volume"), ">=", indicator("VOLUME_SMA", period=2), "放量"),
                c(price("volume"), "<", indicator("VOLUME_SMA", period=2), "缩量"),
            ),
        ),
        "SCHD": (
            "有效价格循环",
            group(c(price(), ">", number(0), "有效价格即入场")),
            group(
                c(price(), ">=", indicator("HIGHEST", period=2), "突破两根高收盘"),
                c(price(), "<", indicator("HIGHEST", period=2), "未突破即离场"),
            ),
        ),
        "NVDA": (
            "极短周期强势",
            group(c(rsi, ">", number(5), "RSI2>5"), c(price(), ">=", price("open"), "阳线或平盘")),
            group(c(price("high"), ">=", price(), "下一根有效K线离场")),
        ),
        "VOO": (
            "宽松均线",
            group(
                c(price(), ">=", indicator("SMA", period=2), "价格≥SMA2"),
                c(rsi, "<=", number(90), "RSI2≤90"),
            ),
            group(c(price("low"), "<=", price(), "下一根有效K线离场")),
        ),
        "TSLA": (
            "短线波动移动止损",
            group(
                c(indicator("ROC", period=1), ">", number(-0.2), "单根涨跌幅>-0.2%"),
                c(rsi, "<", number(80), "RSI2<80"),
            ),
            group(
                c(rsi, ">", number(10), "RSI2>10"), c(price(), "<=", price("open"), "阴线或平盘")
            ),
        ),
    }
    result = []
    for symbol, (name, entry, exit_) in rules.items():
        order = {"type": "market", "time_in_force": "day"}
        if symbol == "GOOGL":
            order.update(
                stop_loss={"mode": "percent", "value": 1},
                take_profit={"mode": "percent", "value": 1},
            )
        if symbol == "TSLA":
            order["trailing_stop"] = {"mode": "percent", "value": 0.5}
        result.append(
            RuleDefinition.model_validate(
                {
                    "name": f"Paper验证 {run_id} {symbol} {name}",
                    "description": "高触发频率功能测试：每次最多1股，最多2次买入；只卖出本策略持仓；到期停止开仓并退出测试持仓。",
                    "symbols": [symbol],
                    "timeframe": "5Min",
                    "warmup_bars": 30,
                    "entry": entry,
                    "exit": exit_,
                    "order": order,
                    "position": {"mode": "fixed_qty", "value": 1, "allow_pyramiding": False},
                    "risk": {"max_symbol_pct": 2, "max_positions": 10, "cooldown_bars": 1},
                    "paper_validation": {
                        "expires_at": expires_at,
                        "max_entries": 2,
                        "max_quantity": 1,
                        "max_notional": 1000,
                    },
                }
            ).model_dump(mode="json")
        )
    return result
