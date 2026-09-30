"""Small, dependency-free indicator helpers operating on pandas DataFrames.

Minute/daily bar frames are expected to have columns: open, high, low, close, volume
and a tz-aware DatetimeIndex.
"""
import pandas as pd


def vwap(bars: pd.DataFrame) -> pd.Series:
    """Cumulative intraday VWAP using typical price. Pass one session of bars."""
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    cum_vol = bars["volume"].cumsum()
    return (typical * bars["volume"]).cumsum() / cum_vol.where(cum_vol > 0)


def atr(daily: pd.DataFrame, period: int = 14) -> float:
    """Average true range of the last `period` daily bars (simple mean)."""
    prev_close = daily["close"].shift(1)
    tr = pd.concat([
        daily["high"] - daily["low"],
        (daily["high"] - prev_close).abs(),
        (daily["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.tail(period).mean())
