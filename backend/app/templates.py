from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AuthUser, EngineState, RiskSettings, Strategy, StrategyVersion, WatchlistItem
from .schemas import RuleDefinition


def price(field: str, offset: int = 0) -> dict:
    return {"kind": "price", "field": field, "offset": offset}


def number(value: float) -> dict:
    return {"kind": "number", "value": value}


def indicator(name: str, params: dict, field: str = "value", offset: int = 0) -> dict:
    return {
        "kind": "indicator",
        "indicator": name,
        "params": params,
        "field": field,
        "offset": offset,
    }


def condition(left: dict, operator: str, right: dict, label: str) -> dict:
    return {
        "type": "condition",
        "left": left,
        "operator": operator,
        "right": right,
        "label": label,
    }


def group(*children: dict, op: str = "AND", negate: bool = False) -> dict:
    return {"type": "group", "op": op, "negate": negate, "children": list(children)}


BASE = {
    "version": 1,
    "symbols": ["SPY"],
    "timeframe": "15Min",
    "warmup_bars": 220,
    "schedule": {"session": "regular", "weekdays": [0, 1, 2, 3, 4]},
    "position": {
        "mode": "percent_equity",
        "value": 10,
        "allow_pyramiding": False,
        "max_additions": 1,
    },
    "order": {
        "type": "market",
        "limit_offset_bps": 10,
        "time_in_force": "day",
        "stop_loss": {"mode": "percent", "value": 2, "atr_period": 14},
        "take_profit": {"mode": "percent", "value": 4, "atr_period": 14},
        "trailing_stop": None,
    },
    "risk": {"max_symbol_pct": 10, "max_positions": 8, "cooldown_bars": 2},
}


def make_template(name: str, description: str, entry: dict, exit_: dict, **overrides) -> dict:
    result = deepcopy(BASE)
    result.update({"name": name, "description": description, "entry": entry, "exit": exit_})
    for key, value in overrides.items():
        result[key] = value
    return RuleDefinition.model_validate(result).model_dump(mode="json")


TEMPLATES: dict[str, dict] = {
    "sma_cross": make_template(
        "SMA 双均线趋势",
        "短期均线上穿长期均线入场，下穿离场。适合趋势较明显的市场。",
        group(
            condition(
                indicator("SMA", {"period": 20}),
                "crosses_above",
                indicator("SMA", {"period": 50}),
                "SMA20 上穿 SMA50",
            )
        ),
        group(
            condition(
                indicator("SMA", {"period": 20}),
                "crosses_below",
                indicator("SMA", {"period": 50}),
                "SMA20 下穿 SMA50",
            )
        ),
    ),
    "rsi_reversion": make_template(
        "RSI 趋势过滤均值回归",
        "长期趋势向上且 RSI 进入超卖区域时入场，RSI 修复后离场。",
        group(
            condition(indicator("RSI", {"period": 14}), "<", number(30), "RSI 低于 30"),
            condition(price("close"), ">", indicator("SMA", {"period": 200}), "价格位于 SMA200 上方"),
        ),
        group(condition(indicator("RSI", {"period": 14}), ">", number(55), "RSI 高于 55")),
        timeframe="1Day",
        warmup_bars=260,
    ),
    "bollinger_reversion": make_template(
        "布林带均值回归",
        "收盘价跌破布林带下轨时入场，回到中轨时离场。",
        group(
            condition(
                price("close"),
                "crosses_below",
                indicator("BOLLINGER", {"period": 20, "std": 2}, "lower"),
                "价格下穿布林带下轨",
            )
        ),
        group(
            condition(
                price("close"),
                "crosses_above",
                indicator("BOLLINGER", {"period": 20, "std": 2}, "middle"),
                "价格上穿布林带中轨",
            )
        ),
    ),
    "macd_momentum": make_template(
        "MACD 动量",
        "MACD 线上穿信号线且价格高于长期均线时入场，反向交叉时离场。",
        group(
            condition(
                indicator("MACD", {"fast": 12, "slow": 26, "signal": 9}, "macd"),
                "crosses_above",
                indicator("MACD", {"fast": 12, "slow": 26, "signal": 9}, "signal"),
                "MACD 上穿信号线",
            ),
            condition(price("close"), ">", indicator("EMA", {"period": 100}), "价格高于 EMA100"),
        ),
        group(
            condition(
                indicator("MACD", {"fast": 12, "slow": 26, "signal": 9}, "macd"),
                "crosses_below",
                indicator("MACD", {"fast": 12, "slow": 26, "signal": 9}, "signal"),
                "MACD 下穿信号线",
            )
        ),
    ),
    "donchian_breakout": make_template(
        "Donchian 通道突破",
        "突破前20根K线最高价入场，跌破前10根K线最低价离场。",
        group(
            condition(
                price("close"),
                "crosses_above",
                indicator("HIGHEST", {"period": 20, "exclude_current": True}),
                "突破20周期高点",
            )
        ),
        group(
            condition(
                price("close"),
                "crosses_below",
                indicator("LOWEST", {"period": 10, "exclude_current": True}),
                "跌破10周期低点",
            )
        ),
        order={
            "type": "market",
            "limit_offset_bps": 10,
            "time_in_force": "day",
            "stop_loss": {"mode": "atr", "value": 2, "atr_period": 14},
            "take_profit": {"mode": "atr", "value": 4, "atr_period": 14},
            "trailing_stop": None,
        },
    ),
    "volume_breakout": make_template(
        "价格成交量联合突破",
        "价格突破20周期高点且成交量超过均量1.5倍时入场。",
        group(
            condition(
                price("close"),
                "crosses_above",
                indicator("HIGHEST", {"period": 20, "exclude_current": True}),
                "价格突破20周期高点",
            ),
            condition(
                price("volume"),
                ">",
                indicator("VOLUME_SMA", {"period": 20, "multiplier": 1.5}),
                "成交量高于20周期均量1.5倍",
            ),
        ),
        group(
            condition(price("close"), "crosses_below", indicator("EMA", {"period": 20}), "跌破 EMA20")
        ),
    ),
    "dca": make_template(
        "定时定额买入",
        "按日线每隔5根K线追加固定金额，适合用于演示规则和长期定投。",
        group(condition(number(1), "==", number(1), "每个评估周期允许买入")),
        group(condition(number(1), "==", number(0), "不设置主动离场")),
        timeframe="1Day",
        position={
            "mode": "fixed_notional",
            "value": 500,
            "allow_pyramiding": True,
            "max_additions": 20,
        },
        order={
            "type": "market",
            "limit_offset_bps": 10,
            "time_in_force": "day",
            "stop_loss": None,
            "take_profit": None,
            "trailing_stop": None,
        },
        risk={"max_symbol_pct": 80, "max_positions": 8, "cooldown_bars": 5},
    ),
    "googl_daily_trend_breakout": make_template(
        "GOOGL 日线趋势突破",
        "日线突破55周期高点、位于200日均线上方且动量确认后入场；适合研究 GOOGL 的中期趋势段。",
        group(
            condition(
                price("close"),
                "crosses_above",
                indicator("HIGHEST", {"period": 55, "exclude_current": True}),
                "收盘价突破55日高点",
            ),
            condition(price("close"), ">", indicator("SMA", {"period": 200}), "价格高于 SMA200"),
            condition(indicator("RSI", {"period": 14}), ">", number(55), "RSI 高于 55"),
        ),
        group(
            condition(price("close"), "crosses_below", indicator("SMA", {"period": 50}), "收盘价跌破 SMA50")
        ),
        symbols=["GOOGL"],
        timeframe="1Day",
        warmup_bars=280,
        position={"mode": "percent_equity", "value": 7, "allow_pyramiding": False, "max_additions": 1},
        order={
            "type": "market",
            "limit_offset_bps": 10,
            "time_in_force": "day",
            "stop_loss": {"mode": "atr", "value": 2, "atr_period": 14},
            "take_profit": {"mode": "atr", "value": 4, "atr_period": 14},
            "trailing_stop": None,
        },
        risk={"max_symbol_pct": 8, "max_positions": 1, "cooldown_bars": 5},
    ),
    "googl_intraday_pullback": make_template(
        "GOOGL 15分钟趋势回调",
        "短线回调后 RSI 重回40上方，且价格仍处于 EMA20 与 EMA100 上方时入场；用于研究顺势回补。",
        group(
            condition(
                indicator("RSI", {"period": 14}),
                "crosses_above",
                number(40),
                "RSI 从回调区重回40上方",
            ),
            condition(price("close"), ">", indicator("EMA", {"period": 20}), "价格高于 EMA20"),
            condition(price("close"), ">", indicator("EMA", {"period": 100}), "价格高于 EMA100"),
        ),
        group(
            condition(price("close"), "crosses_below", indicator("EMA", {"period": 20}), "收盘价跌破 EMA20")
        ),
        symbols=["GOOGL"],
        timeframe="15Min",
        warmup_bars=240,
        position={"mode": "percent_equity", "value": 5, "allow_pyramiding": False, "max_additions": 1},
        order={
            "type": "market",
            "limit_offset_bps": 10,
            "time_in_force": "day",
            "stop_loss": {"mode": "atr", "value": 1.5, "atr_period": 14},
            "take_profit": {"mode": "atr", "value": 3, "atr_period": 14},
            "trailing_stop": None,
        },
        risk={"max_symbol_pct": 6, "max_positions": 1, "cooldown_bars": 6},
    ),
    "googl_intraday_volume_breakout": make_template(
        "GOOGL 15分钟量价突破",
        "突破前20根15分钟K线高点且成交量达到均量1.8倍，并由长期均线和动量共同过滤。",
        group(
            condition(
                price("close"),
                "crosses_above",
                indicator("HIGHEST", {"period": 20, "exclude_current": True}),
                "价格突破20周期高点",
            ),
            condition(
                price("volume"),
                ">",
                indicator("VOLUME_SMA", {"period": 20, "multiplier": 1.8}),
                "成交量高于20周期均量1.8倍",
            ),
            condition(price("close"), ">", indicator("EMA", {"period": 100}), "价格高于 EMA100"),
            condition(indicator("ROC", {"period": 12}), ">", number(0), "12周期动量为正"),
        ),
        group(
            condition(price("close"), "crosses_below", indicator("EMA", {"period": 20}), "收盘价跌破 EMA20")
        ),
        symbols=["GOOGL"],
        timeframe="15Min",
        warmup_bars=240,
        position={"mode": "percent_equity", "value": 5, "allow_pyramiding": False, "max_additions": 1},
        order={
            "type": "market",
            "limit_offset_bps": 10,
            "time_in_force": "day",
            "stop_loss": {"mode": "atr", "value": 1.5, "atr_period": 14},
            "take_profit": {"mode": "atr", "value": 3, "atr_period": 14},
            "trailing_stop": None,
        },
        risk={"max_symbol_pct": 6, "max_positions": 1, "cooldown_bars": 8},
    ),
    "googl_daily_bollinger_reversion": make_template(
        "GOOGL 日线布林带回归",
        "仅在长期趋势仍向上时，研究 GOOGL 日线超卖后的均值回归；不适合追逐下跌趋势。",
        group(
            condition(
                price("close"),
                "crosses_below",
                indicator("BOLLINGER", {"period": 20, "std": 2}, "lower"),
                "收盘价下穿布林带下轨",
            ),
            condition(price("close"), ">", indicator("SMA", {"period": 200}), "价格高于 SMA200"),
            condition(indicator("RSI", {"period": 14}), "<", number(35), "RSI 低于 35"),
        ),
        group(
            condition(
                price("close"),
                "crosses_above",
                indicator("BOLLINGER", {"period": 20, "std": 2}, "middle"),
                "收盘价回到布林带中轨上方",
            )
        ),
        symbols=["GOOGL"],
        timeframe="1Day",
        warmup_bars=280,
        position={"mode": "percent_equity", "value": 6, "allow_pyramiding": False, "max_additions": 1},
        order={
            "type": "market",
            "limit_offset_bps": 10,
            "time_in_force": "day",
            "stop_loss": {"mode": "percent", "value": 3, "atr_period": 14},
            "take_profit": {"mode": "percent", "value": 6, "atr_period": 14},
            "trailing_stop": None,
        },
        risk={"max_symbol_pct": 7, "max_positions": 1, "cooldown_bars": 5},
    ),
}


def seed_templates(db: Session) -> None:
    for key, definition in TEMPLATES.items():
        existing = db.scalar(select(Strategy).where(Strategy.template_key == key))
        if existing:
            continue
        strategy = Strategy(
            owner_user_id=None,
            name=definition["name"],
            description=definition["description"],
            template_key=key,
            is_template=True,
            enabled=False,
            version=1,
            definition=definition,
        )
        db.add(strategy)
        db.flush()
        db.add(StrategyVersion(strategy_id=strategy.id, version=1, definition=definition))

    db.commit()


def seed_user_defaults(db: Session, user_id: int) -> None:
    if db.scalar(select(RiskSettings).where(RiskSettings.user_id == user_id)) is None:
        db.add(RiskSettings(user_id=user_id))
    if db.scalar(select(EngineState).where(EngineState.user_id == user_id)) is None:
        db.add(EngineState(user_id=user_id))

    settings = get_settings()
    existing_symbols = set(
        db.scalars(select(WatchlistItem.symbol).where(WatchlistItem.user_id == user_id)).all()
    )
    for symbol in settings.default_symbols:
        if symbol not in existing_symbols:
            db.add(WatchlistItem(user_id=user_id, symbol=symbol))
    db.commit()


def seed_defaults(db: Session) -> None:
    """Compatibility entry point used by tests and older integrations."""
    seed_templates(db)
    admin = db.get(AuthUser, 1)
    if admin is not None:
        seed_user_defaults(db, admin.id)
