"""SPY intraday momentum: trade breakouts of the "noise area".

Based on Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective Intraday
Momentum Strategy for S&P500 ETF (SPY)". Pure functions only (no API calls) so
the live bot and the backtester run the exact same rules.

Idea: most of the time SPY wanders inside a band around its open ("noise").
How wide that band normally is at each time of day can be measured from the
last 14 sessions. When price leaves the band, supply and demand are out of
balance, and intraday moves like that tend to continue into the close.

Rules:
  1. sigma[m] = average over the last 14 sessions of |price at minute m / open - 1|.
  2. Upper band = max(today's open, yesterday's close) x (1 + sigma[m]);
     lower band = min(today's open, yesterday's close) x (1 - sigma[m]).
     (Using yesterday's close absorbs overnight gaps.)
  3. Decide only every 30 minutes (10:00, 10:30, ... 15:30 ET):
     long if price > upper band, short if price < lower band.
  4. Trailing stop: long = max(upper band, VWAP), short = min(lower band, VWAP).
     It is updated at each decision time and held at the broker in between.
  5. Size the position so that it targets ~2% daily volatility of equity
     (capped at max_leverage). Everything is closed before the bell.
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from .config import MomentumConfig
from .strategy import regular_session

SESSION_MINUTES = 390


def minutes_since_open(index: pd.DatetimeIndex) -> np.ndarray:
    """Minutes from 9:30 ET to each bar's CLOSE (the 9:30 bar -> 1, the 9:59 bar -> 30)."""
    return np.asarray((index.hour - 9) * 60 + index.minute - 30 + 1)


def split_sessions(bars: pd.DataFrame) -> dict:
    """{date: that day's regular-session 1-min bars (ET index)}."""
    rs = regular_session(bars)
    return {d: g for d, g in rs.groupby(rs.index.date)}


def open_moves(session: pd.DataFrame) -> pd.Series:
    """|close / day open - 1| for every minute of the session (gaps forward-filled)."""
    s = (session["close"] / session["open"].iloc[0] - 1).abs()
    s.index = minutes_since_open(session.index)
    s = s[~s.index.duplicated()]
    return s.reindex(range(1, SESSION_MINUTES + 1)).ffill().bfill()


@dataclass
class DayPlan:
    day: date
    open: float
    prev_close: float
    sigma: pd.Series    # typical |move from open| by minute, from PRIOR sessions only
    daily_vol: float    # stdev of daily close-to-close returns over the lookback

    def bands(self, minute: int, cfg: MomentumConfig) -> tuple:
        s = cfg.band_mult * float(self.sigma.iloc[min(max(minute, 1), SESSION_MINUTES) - 1])
        upper = max(self.open, self.prev_close) * (1 + s)
        lower = min(self.open, self.prev_close) * (1 - s)
        return upper, lower


def build_plan(prior_sessions: list, today_open: float, cfg: MomentumConfig,
               day: Optional[date] = None) -> Optional[DayPlan]:
    """`prior_sessions` = session DataFrames for days BEFORE today, oldest first."""
    prior = [s for s in prior_sessions if not s.empty]
    if len(prior) < cfg.lookback_days + 1 or today_open <= 0:
        return None
    recent = prior[-cfg.lookback_days:]
    sigma = pd.concat([open_moves(s) for s in recent], axis=1).mean(axis=1)
    closes = pd.Series([float(s["close"].iloc[-1]) for s in prior[-(cfg.lookback_days + 1):]])
    daily_vol = float(closes.pct_change().dropna().std())
    return DayPlan(day, float(today_open), float(closes.iloc[-1]), sigma, daily_vol)


@dataclass
class Decision:
    action: str               # "long", "short", "exit", "hold", "none"
    stop: Optional[float]     # stop to hold at the broker (for long/short/hold)
    upper: float
    lower: float


def is_checkpoint(minute: int, cfg: MomentumConfig) -> bool:
    return minute >= cfg.first_check_min and minute % cfg.check_every_min == 0


def decide(plan: DayPlan, minute: int, price: float, vwap_now: float, position: int,
           cfg: MomentumConfig, can_enter: bool = True) -> Decision:
    """What to do at a checkpoint. `position` is +1 long, -1 short, 0 flat."""
    upper, lower = plan.bands(minute, cfg)
    long_stop = max(upper, vwap_now) if not np.isnan(vwap_now) else upper
    short_stop = min(lower, vwap_now) if not np.isnan(vwap_now) else lower

    if position > 0:
        return Decision("exit" if price <= long_stop else "hold", long_stop, upper, lower)
    if position < 0:
        return Decision("exit" if price >= short_stop else "hold", short_stop, upper, lower)
    if can_enter and minute <= cfg.last_entry_min:
        # Price must also be beyond the stop, otherwise we'd be stopped out immediately.
        if price > long_stop:
            return Decision("long", long_stop, upper, lower)
        if cfg.allow_shorts and price < short_stop:
            return Decision("short", short_stop, upper, lower)
    return Decision("none", None, upper, lower)


def leverage(daily_vol: float, cfg: MomentumConfig) -> float:
    if daily_vol <= 0 or np.isnan(daily_vol):
        return 0.0
    return min(cfg.max_leverage, cfg.target_daily_vol_pct / 100 / daily_vol)


def position_qty(equity: float, buying_power: float, price: float, daily_vol: float,
                 cfg: MomentumConfig) -> int:
    """Shares so the position's daily volatility is ~target_daily_vol_pct of equity."""
    if price <= 0:
        return 0
    notional = min(equity * leverage(daily_vol, cfg), buying_power * 0.95)
    return max(0, int(notional / price))
