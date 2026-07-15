from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.models import EngineState, RiskSettings


ET = ZoneInfo("America/New_York")


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
    def update_equity_state(
        self,
        *,
        account: dict[str, Any],
        state: EngineState,
        now: datetime | None = None,
    ) -> None:
        """Persist the session baseline independently from entry signals.

        Alpaca's ``last_equity`` is the previous regular-session close and is a
        safer day-start reference than whichever intraday value happens to be
        observed when the first strategy signal arrives.  The high-water mark
        is refreshed by the engine throughout the session, including while the
        user has new entries paused.
        """
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        equity = as_float(account.get("equity"))
        if equity <= 0:
            return
        session_date = observed_at.astimezone(ET).date().isoformat()
        if state.session_date != session_date:
            previous_close = as_float(account.get("last_equity"))
            day_start = previous_close if previous_close > 0 else equity
            state.session_date = session_date
            state.day_start_equity = day_start
            state.daily_high_equity = max(day_start, equity)
            return
        state.day_start_equity = state.day_start_equity or equity
        state.daily_high_equity = max(state.daily_high_equity or equity, equity)

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
        open_orders: list[dict[str, Any]] | None = None,
        reference_price: float = 0,
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

        self.update_equity_state(account=account, state=state, now=now)

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
        pending_by_symbol = self.pending_buy_exposure(open_orders or [], reference_price)
        pending_symbol_value = pending_by_symbol.get(symbol, 0.0)
        global_symbol_pct = settings.max_symbol_pct or 10.0
        symbol_limit_pct = min(global_symbol_pct, strategy_max_symbol_pct)
        symbol_limit_value = equity * symbol_limit_pct / 100
        projected_symbol_value = current_symbol_value + pending_symbol_value + desired_notional
        if projected_symbol_value > symbol_limit_value + 0.01:
            return RiskDecision(
                False,
                "单只股票仓位将超过 "
                f"{symbol_limit_pct:.1f}%（全局 {global_symbol_pct:.1f}%，"
                f"策略 {strategy_max_symbol_pct:.1f}%；当前 ${current_symbol_value:,.2f} + "
                f"待成交 ${pending_symbol_value:,.2f} + 本次 ${desired_notional:,.2f} > "
                f"上限 ${symbol_limit_value:,.2f}）",
            )

        total_exposure = sum(abs(as_float(position.get("market_value"))) for position in positions)
        pending_total_exposure = sum(pending_by_symbol.values())
        max_total_exposure_pct = settings.max_total_exposure_pct or 80.0
        if (
            total_exposure + pending_total_exposure + desired_notional
            > equity * max_total_exposure_pct / 100 + 0.01
        ):
            return RiskDecision(False, f"总持仓将超过 {max_total_exposure_pct:.1f}%")

        max_positions = min(settings.max_positions or 8, strategy_max_positions)
        projected_symbols = set(position_map).union(
            pending_symbol for pending_symbol, value in pending_by_symbol.items() if value > 0
        )
        if symbol not in projected_symbols and len(projected_symbols) >= max_positions:
            return RiskDecision(False, f"持仓数量已达到 {max_positions} 只")
        if desired_notional > buying_power:
            return RiskDecision(False, "可用购买力不足")
        return RiskDecision(True)

    @staticmethod
    def pending_buy_exposure(
        orders: list[dict[str, Any]], reference_price: float
    ) -> dict[str, float]:
        exposure: dict[str, float] = {}
        for order in orders:
            side = str(order.get("side", "")).lower()
            status = str(order.get("status", "")).lower()
            if "buy" not in side or status in {"filled", "canceled", "cancelled", "expired", "rejected"}:
                continue
            symbol = str(order.get("symbol", "")).upper()
            if not symbol:
                continue
            notional = as_float(order.get("_estimated_notional") or order.get("notional"))
            total_qty = as_float(order.get("qty"))
            filled_qty = as_float(order.get("filled_qty"))
            if notional > 0 and total_qty > 0:
                notional *= max(0.0, total_qty - filled_qty) / total_qty
            if notional <= 0:
                qty = max(0.0, total_qty - filled_qty)
                price = (
                    as_float(order.get("limit_price"))
                    or as_float(order.get("stop_price"))
                    or reference_price
                )
                notional = qty * price
            exposure[symbol] = exposure.get(symbol, 0.0) + max(0.0, notional)
        return exposure
