from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.schemas import Operand


def ensure_frame(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"K线缺少字段: {', '.join(sorted(missing))}")
    result = frame.copy().sort_index()
    for column in required:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.where(avg_loss != 0, 100.0).where(avg_gain != 0, 0.0)


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    return pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def indicator_series(frame: pd.DataFrame, operand: Operand) -> pd.Series:
    frame = ensure_frame(frame)
    if operand.kind == "number":
        series = pd.Series(float(operand.value), index=frame.index, dtype=float)
    elif operand.kind == "price":
        series = frame[str(operand.field)]
    else:
        params: dict[str, Any] = dict(operand.params)
        name = str(operand.indicator)
        field = operand.field or "value"
        close = frame["close"]

        if name == "SMA":
            series = sma(close, int(params.get("period", 20)))
        elif name == "EMA":
            series = ema(close, int(params.get("period", 20)))
        elif name == "RSI":
            series = rsi(close, int(params.get("period", 14)))
        elif name == "MACD":
            fast = ema(close, int(params.get("fast", 12)))
            slow = ema(close, int(params.get("slow", 26)))
            macd_line = fast - slow
            signal_line = ema(macd_line, int(params.get("signal", 9)))
            outputs = {
                "macd": macd_line,
                "signal": signal_line,
                "histogram": macd_line - signal_line,
                "value": macd_line,
            }
            series = outputs.get(field, macd_line)
        elif name == "BOLLINGER":
            period = int(params.get("period", 20))
            deviations = float(params.get("std", 2))
            middle = sma(close, period)
            std = close.rolling(period, min_periods=period).std(ddof=0)
            outputs = {
                "upper": middle + deviations * std,
                "middle": middle,
                "lower": middle - deviations * std,
                "value": middle,
            }
            series = outputs.get(field, middle)
        elif name == "ATR":
            series = atr(frame, int(params.get("period", 14)))
        elif name == "ROC":
            period = int(params.get("period", 12))
            series = close.pct_change(periods=period) * 100
        elif name == "HIGHEST":
            period = int(params.get("period", 20))
            source = close.shift(1) if bool(params.get("exclude_current", True)) else close
            series = source.rolling(period, min_periods=period).max()
        elif name == "LOWEST":
            period = int(params.get("period", 20))
            source = close.shift(1) if bool(params.get("exclude_current", True)) else close
            series = source.rolling(period, min_periods=period).min()
        elif name == "VOLUME_SMA":
            period = int(params.get("period", 20))
            multiplier = float(params.get("multiplier", 1.0))
            series = sma(frame["volume"], period) * multiplier
        elif name == "DEVIATION":
            period = int(params.get("period", 20))
            average = sma(close, period)
            series = (close / average - 1) * 100
        else:
            raise ValueError(f"不支持的指标: {name}")

    if operand.offset:
        series = series.shift(operand.offset)
    return series.astype(float)


def latest_atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    values = atr(ensure_frame(frame), period).dropna()
    if values.empty:
        return None
    return float(values.iloc[-1])
