from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.models import EngineState, RiskSettings


def as_float(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str = ""
    halt_engine: bool = False


class RiskManager:
    def check_entry(
        self,
        *,
        symbol: str,
        desired_notional: float,
        account: dict[str, Any],
        positions: list[dict[str, Any]],
        asset: dict[str, Any],
        clock: dict[str, Any],
        bar_timestamp: datetime,
        max_data_age_seconds: int,
        settings: RiskSettings,
        state: EngineState,
        strategy_max_symbol_pct: float,
        strategy_max_positions: int,
    ) -> RiskDecision:
        if not bool(clock.get("is_open")):
            return RiskDecision(False, "当前不在美股常规交易时段")
        if bool(account.get("trading_blocked")):
            return RiskDecision(False, "Alpaca 账户当前禁止交易")
        if not bool(asset.get("tradable")):
            return RiskDecision(False, f"{symbol} 当前不可交易")
        if str(asset.get("status", "active")).lower() not in {"active", "assetstatus.active"}:
            return RiskDecision(False, f"{symbol} 资产状态不是 active")

        now = datetime.now(timezone.utc)
        timestamp = bar_timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if (now - timestamp).total_seconds() > max_data_age_seconds:
            return RiskDecision(False, "行情数据已过期，拒绝下单")

        equity = as_float(account.get("equity"))
        buying_power = as_float(account.get("buying_power"))
        if equity <= 0:
            return RiskDecision(False, "账户净值无效")

        current_day = now.date().isoformat()
        if state.session_date != current_day:
            state.session_date = current_day
            state.day_start_equity = equity
            state.daily_high_equity = equity
        state.day_start_equity = state.day_start_equity or equity
        state.daily_high_equity = max(state.daily_high_equity or equity, equity)

        daily_loss_pct = (state.day_start_equity - equity) / state.day_start_equity * 100
        drawdown_pct = (state.daily_high_equity - equity) / state.daily_high_equity * 100
        max_daily_loss_pct = settings.max_daily_loss_pct or 3.0
        max_intraday_drawdown_pct = settings.max_intraday_drawdown_pct or 5.0
        if daily_loss_pct >= max_daily_loss_pct:
            return RiskDecision(False, f"单日亏损达到 {daily_loss_pct:.2f}%", True)
        if drawdown_pct >= max_intraday_drawdown_pct:
            return RiskDecision(False, f"日内回撤达到 {drawdown_pct:.2f}%", True)

        position_map = {str(position.get("symbol")): position for position in positions}
        current_symbol_value = abs(as_float(position_map.get(symbol, {}).get("market_value")))
        symbol_limit_pct = min(settings.max_symbol_pct or 10.0, strategy_max_symbol_pct)
        if current_symbol_value + desired_notional > equity * symbol_limit_pct / 100 + 0.01:
            return RiskDecision(False, f"单只股票仓位将超过 {symbol_limit_pct:.1f}%")

        total_exposure = sum(abs(as_float(position.get("market_value"))) for position in positions)
        max_total_exposure_pct = settings.max_total_exposure_pct or 80.0
        if total_exposure + desired_notional > equity * max_total_exposure_pct / 100 + 0.01:
            return RiskDecision(False, f"总持仓将超过 {max_total_exposure_pct:.1f}%")

        max_positions = min(settings.max_positions or 8, strategy_max_positions)
        if symbol not in position_map and len(position_map) >= max_positions:
            return RiskDecision(False, f"持仓数量已达到 {max_positions} 只")
        if desired_notional > buying_power:
            return RiskDecision(False, "可用购买力不足")
        return RiskDecision(True)
