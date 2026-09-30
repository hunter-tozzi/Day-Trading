"""Opening Range Breakout (ORB) with VWAP + market filter.

Pure functions only (no API calls) so the live bot and the backtester run the
exact same rules.

Rules (long side; short side is the mirror image):
  1. Scanner picks liquid stocks that move (ATR%) and are unusually active
     today (opening-range relative volume).
  2. Opening range = high/low of the first N minutes (default 9:30-9:45 ET).
  3. Enter when a completed 1-minute bar closes above the range high,
     above the stock's VWAP, and SPY is above its own VWAP.
  4. Stop = range low. Target = entry + reward_risk x risk.
  5. Skip if the stop is too wide (> max_risk_atr_frac x daily ATR), which also
     prevents chasing a stock that already ran far past the range.
  6. No new entries after entry_cutoff; everything is closed at flatten_time.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

import pandas as pd

from .config import Config
from .indicators import atr, vwap

ET = "America/New_York"
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass
class Candidate:
    symbol: str
    prev_close: float
    atr: float
    avg_volume: float
    or_high: float
    or_low: float
    or_volume: float
    gap_pct: float
    rvol: float


@dataclass
class Signal:
    symbol: str
    side: str          # "buy" or "sell" (short)
    entry: float
    stop: float
    target: float
    time: datetime

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)


def regular_session(bars: pd.DataFrame) -> pd.DataFrame:
    """Keep only 9:30-16:00 ET bars, index converted to ET."""
    if bars.empty:
        return bars
    b = bars.copy()
    b.index = b.index.tz_convert(ET)
    t = b.index.time
    return b[(t >= MARKET_OPEN) & (t < MARKET_CLOSE)]


def _or_end(day_bars: pd.DataFrame, cfg: Config) -> datetime:
    d = day_bars.index[0]
    return d.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(
        minutes=cfg.opening_range_minutes)


def opening_range(day_bars: pd.DataFrame, cfg: Config) -> Optional[tuple]:
    """(high, low, volume) of the opening range, or None if it isn't complete yet."""
    if day_bars.empty:
        return None
    end = _or_end(day_bars, cfg)
    rng = day_bars[day_bars.index < end]
    # Range is complete only once we have a bar at/after its end.
    if rng.empty or day_bars.index[-1] < end - timedelta(minutes=1):
        return None
    return float(rng["high"].max()), float(rng["low"].min()), float(rng["volume"].sum())


def score_candidate(symbol: str, daily_hist: pd.DataFrame, day_bars: pd.DataFrame,
                    cfg: Config) -> Optional[Candidate]:
    """Apply scanner filters. `daily_hist` must contain only days BEFORE today."""
    if len(daily_hist) < 15:
        return None
    daily_hist = daily_hist.tail(cfg.lookback_days)
    prev_close = float(daily_hist["close"].iloc[-1])
    avg_vol = float(daily_hist["volume"].mean())
    day_atr = atr(daily_hist)
    orng = opening_range(day_bars, cfg)
    if orng is None or prev_close <= 0 or avg_vol <= 0:
        return None
    or_high, or_low, or_vol = orng

    price = float(day_bars["close"].iloc[-1])
    atr_pct = day_atr / prev_close * 100
    gap_pct = (float(day_bars["open"].iloc[0]) - prev_close) / prev_close * 100
    share = cfg.opening_range_volume_share * cfg.opening_range_minutes / 15
    rvol = or_vol / (avg_vol * share)

    if not (cfg.min_price <= price <= cfg.max_price):
        return None
    if avg_vol < cfg.min_avg_volume or atr_pct < cfg.min_atr_pct:
        return None
    if abs(gap_pct) < cfg.min_gap_pct or rvol < cfg.min_rvol:
        return None
    return Candidate(symbol, prev_close, day_atr, avg_vol, or_high, or_low, or_vol,
                     gap_pct, rvol)


def rank_candidates(cands: list, cfg: Config) -> list:
    return sorted(cands, key=lambda c: c.rvol, reverse=True)[: cfg.max_watchlist]


def market_bias(market_bars: Optional[pd.DataFrame]) -> Optional[str]:
    """'up' if SPY's last close is above its VWAP, 'down' if below, None if unknown."""
    if market_bars is None or market_bars.empty:
        return None
    v = vwap(market_bars).iloc[-1]
    c = market_bars["close"].iloc[-1]
    if pd.isna(v):
        return None
    return "up" if c > v else "down" if c < v else None


def check_entry(cand: Candidate, day_bars: pd.DataFrame, cfg: Config,
                market_bars: Optional[pd.DataFrame] = None) -> Optional[Signal]:
    """Evaluate the most recent COMPLETED 1-minute bar in `day_bars`."""
    if day_bars.empty:
        return None
    ts = day_bars.index[-1]
    bar_close_time = (ts + timedelta(minutes=1)).time()
    if ts < _or_end(day_bars, cfg) or bar_close_time > cfg.entry_cutoff:
        return None

    close = float(day_bars["close"].iloc[-1])
    v = float(vwap(day_bars).iloc[-1])
    bias = market_bias(market_bars) if cfg.require_market_filter else None
    or_range = cand.or_high - cand.or_low
    if or_range <= 0:
        return None

    side = None
    if close > cand.or_high and close > v:
        if not cfg.require_market_filter or bias == "up":
            side, stop = "buy", cand.or_low
    elif cfg.allow_shorts and close < cand.or_low and close < v:
        if not cfg.require_market_filter or bias == "down":
            side, stop = "sell", cand.or_high
    if side is None:
        return None

    risk = abs(close - stop)
    if risk > cfg.max_risk_atr_frac * cand.atr or risk < close * cfg.min_risk_pct / 100:
        return None
    target = close + cfg.reward_risk * risk if side == "buy" else close - cfg.reward_risk * risk
    return Signal(cand.symbol, side, close, stop, target, ts.to_pydatetime())


def position_size(equity: float, buying_power: float, sig: Signal, cfg: Config) -> int:
    """Shares such that hitting the stop loses ~risk_per_trade_pct of equity."""
    if sig.risk_per_share <= 0:
        return 0
    by_risk = equity * cfg.risk_per_trade_pct / 100 / sig.risk_per_share
    by_size = equity * cfg.max_position_pct / 100 / sig.entry
    by_bp = buying_power * 0.95 / sig.entry
    return max(0, int(min(by_risk, by_size, by_bp)))
