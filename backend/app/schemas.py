from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Timeframe = Literal["5Min", "15Min", "30Min", "1Hour", "1Day"]
IndicatorName = Literal[
    "SMA",
    "EMA",
    "RSI",
    "MACD",
    "BOLLINGER",
    "ATR",
    "ROC",
    "HIGHEST",
    "LOWEST",
    "VOLUME_SMA",
    "DEVIATION",
]


class Operand(BaseModel):
    kind: Literal["price", "indicator", "number"]
    field: str | None = None
    indicator: IndicatorName | None = None
    params: dict[str, float | int | bool] = Field(default_factory=dict)
    value: float | None = None
    offset: int = Field(default=0, ge=0, le=20)

    @model_validator(mode="after")
    def validate_kind(self) -> "Operand":
        if self.kind == "number" and self.value is None:
            raise ValueError("常数操作数必须填写数值")
        if self.kind == "price" and self.field not in {"open", "high", "low", "close", "volume"}:
            raise ValueError("价格字段必须是 open/high/low/close/volume")
        if self.kind == "indicator" and self.indicator is None:
            raise ValueError("指标操作数必须选择指标")
        return self


class Condition(BaseModel):
    type: Literal["condition"] = "condition"
    left: Operand
    operator: Literal[">", ">=", "<", "<=", "==", "crosses_above", "crosses_below"]
    right: Operand
    label: str = ""


class ConditionGroup(BaseModel):
    type: Literal["group"] = "group"
    op: Literal["AND", "OR"] = "AND"
    negate: bool = False
    children: list["ConditionNode"] = Field(default_factory=list)


ConditionNode = Annotated[Union[Condition, ConditionGroup], Field(discriminator="type")]
ConditionGroup.model_rebuild()


class ScheduleConfig(BaseModel):
    session: Literal["regular"] = "regular"
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]) -> list[int]:
        cleaned = sorted(set(value))
        if any(day < 0 or day > 4 for day in cleaned):
            raise ValueError("仅支持周一至周五")
        return cleaned


class PositionSizing(BaseModel):
    mode: Literal["percent_equity", "fixed_notional", "fixed_qty", "risk_based"] = "percent_equity"
    value: float = Field(default=10.0, gt=0)
    allow_pyramiding: bool = False
    max_additions: int = Field(default=1, ge=1, le=20)


class PriceGuard(BaseModel):
    mode: Literal["percent", "atr"] = "percent"
    value: float = Field(gt=0)
    atr_period: int = Field(default=14, ge=2, le=200)


class TrailingStopConfig(BaseModel):
    mode: Literal["percent", "price"] = "percent"
    value: float = Field(gt=0)


class OrderConfig(BaseModel):
    type: Literal["market", "limit"] = "market"
    limit_offset_bps: float = Field(default=10.0, ge=0, le=1000)
    time_in_force: Literal["day", "gtc"] = "day"
    stop_loss: PriceGuard | None = None
    take_profit: PriceGuard | None = None
    trailing_stop: TrailingStopConfig | None = None

    @model_validator(mode="after")
    def validate_exit_orders(self) -> "OrderConfig":
        if self.trailing_stop and (self.stop_loss or self.take_profit):
            raise ValueError("移动止损不能与 bracket 止损止盈同时启用")
        if bool(self.stop_loss) != bool(self.take_profit):
            raise ValueError("bracket 订单必须同时配置止损和止盈")
        return self


class StrategyRiskConfig(BaseModel):
    max_symbol_pct: float = Field(default=10.0, gt=0, le=100)
    max_positions: int = Field(default=8, ge=1, le=30)
    cooldown_bars: int = Field(default=1, ge=0, le=1000)


class RuleDefinition(BaseModel):
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    symbols: list[str] = Field(min_length=1, max_length=30)
    timeframe: Timeframe = "15Min"
    warmup_bars: int = Field(default=220, ge=30, le=2000)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    entry: ConditionGroup
    exit: ConditionGroup
    position: PositionSizing = Field(default_factory=PositionSizing)
    order: OrderConfig = Field(default_factory=OrderConfig)
    risk: StrategyRiskConfig = Field(default_factory=StrategyRiskConfig)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: list[str]) -> list[str]:
        symbols = [symbol.strip().upper() for symbol in value if symbol.strip()]
        if not symbols:
            raise ValueError("至少需要一个股票代码")
        if len(set(symbols)) != len(symbols):
            raise ValueError("股票池不能包含重复代码")
        return symbols


class StrategyCreate(BaseModel):
    definition: RuleDefinition


class StrategyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    template_key: str | None
    is_template: bool
    enabled: bool
    version: int
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class StrategyVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    strategy_id: str
    version: int
    definition: dict[str, Any]
    created_at: datetime


class BacktestRequest(BaseModel):
    strategy_id: str
    start: datetime
    end: datetime
    initial_cash: float = Field(default=100_000, gt=0)
    slippage_bps: float = Field(default=5, ge=0, le=1000)
    commission: float = Field(default=0, ge=0)
    benchmark: str = "SPY"

    @model_validator(mode="after")
    def validate_range(self) -> "BacktestRequest":
        if self.end <= self.start:
            raise ValueError("回测结束时间必须晚于开始时间")
        return self


class BacktestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    strategy_id: str
    status: str
    parameters: dict[str, Any]
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    benchmark_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class RiskSettingsUpdate(BaseModel):
    max_symbol_pct: float = Field(gt=0, le=100)
    max_total_exposure_pct: float = Field(gt=0, le=100)
    max_positions: int = Field(ge=1, le=30)
    max_daily_loss_pct: float = Field(gt=0, le=100)
    max_intraday_drawdown_pct: float = Field(gt=0, le=100)
    stale_data_seconds: int = Field(ge=60, le=86400)


class RiskSettingsRead(RiskSettingsUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class EngineAction(BaseModel):
    reason: str = Field(default="用户操作", max_length=300)


class ConnectionConfigUpdate(BaseModel):
    # Deliberately avoid Field validators here: Pydantic includes invalid input
    # in validation responses, which must never expose an API secret.
    api_key_id: str = ""
    api_secret_key: str = ""
    data_feed: Literal["iex"] = "iex"


class ConnectionConfigRead(BaseModel):
    configured: bool
    paper: Literal[True] = True
    source: Literal["web", "env", "none"]
    api_key_hint: str | None = None
    feed: Literal["iex"] = "iex"
    updated_at: datetime | None = None


class WatchlistUpdate(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=30)

    @field_validator("symbols")
    @classmethod
    def normalize(cls, value: list[str]) -> list[str]:
        return sorted(set(symbol.strip().upper() for symbol in value if symbol.strip()))
