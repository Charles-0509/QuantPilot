import json

import pandas as pd
import pytest

from app.schemas import RuleDefinition
from app.services.backtest import calculate_metrics, run_backtest


def rule(**updates):
    data = {
        "name": "audit",
        "symbols": ["AAA"],
        "timeframe": "1Day",
        "warmup_bars": 30,
        "entry": {
            "children": [
                {
                    "type": "condition",
                    "left": {"kind": "price", "field": "close"},
                    "operator": ">",
                    "right": {"kind": "number", "value": 100},
                }
            ]
        },
        "exit": {"children": []},
        "position": {"mode": "percent_equity", "value": 10},
        "risk": {"max_symbol_pct": 100, "max_positions": 1, "cooldown_bars": 0},
    }
    data.update(updates)
    return RuleDefinition.model_validate(data)


def bars(n=36):
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
        index=pd.bdate_range("2025-01-06", periods=n, tz="America/New_York").tz_convert("UTC"),
    )
    if n > 30:
        frame.iloc[30, frame.columns.get_loc("close")] = 101.0
    return frame


def atr_trade(high, period):
    frame = bars()
    frame.iloc[31, frame.columns.get_loc("high")] = high
    frame.iloc[31, frame.columns.get_loc("low")] = 99.5
    # Different recent ranges make ATR periods observably different at signal time.
    frame.iloc[28:31, frame.columns.get_loc("high")] = 103.0
    definition = rule(
        position={"mode": "risk_based", "value": 1},
        order={
            "stop_loss": {"mode": "atr", "value": 2, "atr_period": period},
            "take_profit": {"mode": "atr", "value": 20, "atr_period": period},
        },
    )
    return run_backtest(definition, {"AAA": frame}, slippage_bps=0).trades[0]


def test_entry_qty_cannot_see_fill_bar_high():
    assert atr_trade(110, 14)["qty"] == atr_trade(130, 14)["qty"]


def test_protection_respects_atr_period():
    assert atr_trade(110, 2)["qty"] != atr_trade(110, 28)["qty"]


def test_shared_cash_enforces_max_positions():
    result = run_backtest(
        rule(symbols=["AAA", "BBB"]), {"AAA": bars(), "BBB": bars()}, slippage_bps=0
    )
    assert len(result.trades) == 1


def test_empty_schedule_never_trades():
    result = run_backtest(rule(schedule={"weekdays": []}), {"AAA": bars()}, slippage_bps=0)
    assert not result.trades


def test_warmup_is_required_even_for_price_only_rule():
    frame = bars(6)
    frame["close"] = 101.0
    result = run_backtest(rule(), {"AAA": frame}, slippage_bps=0)
    assert not result.trades


def test_limit_price_is_a_hard_ceiling():
    frame = bars()
    frame.iloc[31, frame.columns.get_loc("open")] = 101.0
    result = run_backtest(
        rule(order={"type": "limit", "limit_offset_bps": 0}), {"AAA": frame}, slippage_bps=5
    )
    assert result.trades[0]["entry_price"] <= 101.0


def test_undefined_ratios_are_null_and_json_serializable():
    curve = [
        {"timestamp": t.isoformat(), "equity": e}
        for t, e in zip(pd.date_range("2025-01-01", periods=3, tz="UTC"), [100, 101, 102])
    ]
    trades = [{"pnl": 2, "bars_held": 2}]
    metrics = calculate_metrics(curve, trades, 100, [0, 1, 1], "1Day")
    assert metrics["sortino"] is None
    assert metrics["profit_factor"] is None
    assert metrics["payoff_ratio"] is None
    json.dumps(metrics, allow_nan=False)


def test_sortino_uses_downside_deviation_from_zero_target():
    curve = [
        {"timestamp": t.isoformat(), "equity": e}
        for t, e in zip(pd.date_range("2025-01-01", periods=4, tz="UTC"), [100, 90, 81, 97.2])
    ]
    metrics = calculate_metrics(curve, [], 100, [0] * 4, "1Day")
    # Mean returns are zero, and downside deviation is nonzero even for equal losses.
    assert metrics["sortino"] == pytest.approx(0, abs=0.001)


def test_warmup_does_not_trade_or_extend_report_before_requested_start():
    frame = bars()
    frame["close"] = 101.0
    start = frame.index[30]
    result = run_backtest(rule(), {"AAA": frame}, start=start, slippage_bps=0)
    assert result.equity_curve[0]["timestamp"] == start.isoformat()
    assert result.trades[0]["entry_time"] == frame.index[31].isoformat()


def test_global_exposure_and_symbol_limits_apply():
    result = run_backtest(
        rule(position={"mode": "percent_equity", "value": 80}),
        {"AAA": bars()},
        slippage_bps=0,
        risk_settings={"max_symbol_pct": 30, "max_total_exposure_pct": 20},
    )
    assert result.trades[0]["qty"] == 200


def test_day_limit_expires_but_gtc_can_fill_later_session():
    frame = bars()
    frame.iloc[31, frame.columns.get_loc("low")] = 102.0
    frame.iloc[31, frame.columns.get_loc("open")] = 103.0
    frame.iloc[31, frame.columns.get_loc("high")] = 104.0
    frame.iloc[31, frame.columns.get_loc("close")] = 103.0
    frame.iloc[32:, frame.columns.get_loc("close")] = 100.0
    # Only a single signal, so expiration cannot be hidden by a replacement order.
    entry = {
        "children": [
            {
                "type": "condition",
                "left": {"kind": "price", "field": "close"},
                "operator": "==",
                "right": {"kind": "number", "value": 101},
            }
        ]
    }
    day = run_backtest(
        rule(entry=entry, order={"type": "limit", "limit_offset_bps": 0, "time_in_force": "day"}),
        {"AAA": frame},
        slippage_bps=0,
    )
    gtc = run_backtest(
        rule(entry=entry, order={"type": "limit", "limit_offset_bps": 0, "time_in_force": "gtc"}),
        {"AAA": frame},
        slippage_bps=0,
    )
    assert not day.trades
    assert gtc.trades[0]["entry_time"] == frame.index[32].isoformat()


def test_global_drawdown_halts_further_entries():
    frame = bars(40)
    frame["close"] = 101.0
    frame.iloc[32:, frame.columns.get_loc("open")] = 80.0
    frame.iloc[32:, frame.columns.get_loc("high")] = 81.0
    frame.iloc[32:, frame.columns.get_loc("low")] = 79.0
    frame.iloc[32:, frame.columns.get_loc("close")] = 80.0
    definition = rule(
        position={
            "mode": "percent_equity",
            "value": 50,
            "allow_pyramiding": True,
            "max_additions": 10,
        },
        entry={
            "children": [
                {
                    "type": "condition",
                    "left": {"kind": "price", "field": "close"},
                    "operator": ">",
                    "right": {"kind": "number", "value": 0},
                }
            ]
        },
    )
    limited = run_backtest(
        definition,
        {"AAA": frame},
        start=frame.index[30],
        slippage_bps=0,
        risk_settings={"max_daily_loss_pct": 3, "max_intraday_drawdown_pct": 3},
    )
    assert limited.trades[0]["qty"] == 500


def test_benchmark_never_backfills_from_future():
    from app.services.backtest import build_benchmark_curve

    frame = bars()
    with pytest.raises(ValueError, match="未来价格"):
        build_benchmark_curve(frame.iloc[1:], 100000, list(frame.index))
