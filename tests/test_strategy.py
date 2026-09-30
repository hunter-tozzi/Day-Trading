from datetime import time

import numpy as np
import pandas as pd
import pytest

from daytrader.backtest import run_backtest, simulate_day
from daytrader.config import Config
from daytrader.strategy import (Candidate, Signal, check_entry, opening_range,
                                position_size, score_candidate)

DAY = "2026-03-10"


def session(prices, vol=10_000, day=DAY):
    """1-minute bars from 9:30 ET where each bar opens at the previous close."""
    idx = pd.date_range(f"{day} 09:30", periods=len(prices), freq="1min", tz="America/New_York")
    closes = np.array(prices, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + 0.02,
                         "low": np.minimum(opens, closes) - 0.02, "close": closes,
                         "volume": vol}, index=idx)


def daily(n=25, price=100.0, rng=3.0, vol=100_000, end=DAY):
    idx = pd.date_range(end=pd.Timestamp(end) - pd.Timedelta(days=1), periods=n, freq="B",
                        tz="America/New_York")
    return pd.DataFrame({"open": price, "high": price + rng / 2, "low": price - rng / 2,
                         "close": price, "volume": vol}, index=idx)


def flat_then(after, or_prices=None):
    """15 minutes chopping 99.5-100.5, then the given path."""
    or_prices = or_prices or [100, 100.5, 99.5] * 5
    return session(or_prices + after)


CFG = Config()
CAND = Candidate("TEST", 100, 3.0, 100_000, 100.52, 99.48, 150_000, 0.0, 2.0)
SPY_UP = session(list(np.linspace(500, 505, 390)))
SPY_DOWN = session(list(np.linspace(505, 500, 390)))


def test_opening_range_needs_full_window():
    bars = flat_then([100] * 5)
    assert opening_range(bars.iloc[:10], CFG) is None
    hi, lo, vol = opening_range(bars, CFG)
    assert (hi, lo) == (pytest.approx(100.52), pytest.approx(99.48))
    assert vol == 15 * 10_000


def test_long_breakout_signal():
    bars = flat_then([100.3, 100.8])
    sig = check_entry(CAND, bars, CFG, SPY_UP)
    assert sig and sig.side == "buy"
    assert sig.stop == pytest.approx(99.48)
    assert sig.target == pytest.approx(100.8 + 2 * (100.8 - 99.48))


def test_market_filter_blocks_long_when_spy_weak():
    assert check_entry(CAND, flat_then([100.3, 100.8]), CFG, SPY_DOWN) is None


def test_no_entry_inside_range_or_after_cutoff():
    assert check_entry(CAND, flat_then([100.2]), CFG, SPY_UP) is None
    late = flat_then([100.2] * 200 + [100.8])     # breakout bar at ~12:46
    assert late.index[-1].time() > time(11, 30)
    assert check_entry(CAND, late, CFG, SPY_UP) is None


def test_skips_when_stop_too_wide():
    wide = Candidate("TEST", 100, 1.0, 100_000, 100.52, 99.48, 150_000, 0, 2)  # ATR 1.0
    assert check_entry(wide, flat_then([100.8]), CFG, SPY_UP) is None


def test_short_only_when_enabled():
    bars = flat_then([99.7, 99.2])
    assert check_entry(CAND, bars, CFG, SPY_DOWN) is None
    sig = check_entry(CAND, bars, Config(allow_shorts=True), SPY_DOWN)
    assert sig and sig.side == "sell" and sig.stop == pytest.approx(100.52)


def test_position_size_respects_risk_and_caps():
    sig = Signal("X", "buy", 100, 99, 102, None)
    assert position_size(25_000, 25_000, sig, CFG) == 50        # 0.5% of 25k / $1 = 125, cap 20% -> 50
    sig = Signal("X", "buy", 100, 95, 110, None)
    assert position_size(25_000, 25_000, sig, CFG) == 25        # 125 / 5 = 25


def test_scanner_filters():
    bars = flat_then([], or_prices=[100, 100.5, 99.5] * 5)
    hist = daily(vol=100_000)                                   # expected OR vol 9,000
    c = score_candidate("TEST", hist, bars, CFG)                # actual 150,000 -> rvol ~16
    assert c and c.rvol > 10
    assert score_candidate("TEST", daily(rng=0.5), bars, CFG) is None      # ATR 0.5% too small
    assert score_candidate("TEST", daily(vol=10_000_000), bars, CFG) is None  # rvol too low
    assert score_candidate("TEST", daily(price=5, rng=0.3), session([5] * 16), CFG) is None


def _day(after):
    bars = flat_then(after + [after[-1]] * (390 - 15 - len(after)))
    return {"TEST": bars, "SPY": SPY_UP}, {"TEST": daily()}


def test_simulated_winner_hits_target():
    sessions, hist = _day([100.8] + list(np.linspace(100.9, 104, 20)))
    trades = simulate_day(pd.Timestamp(DAY).date(), sessions, hist, SPY_UP, 25_000, CFG)
    assert len(trades) == 1 and trades[0].reason == "target"
    assert trades[0].r_multiple == pytest.approx(2, abs=0.2)


def test_simulated_loser_hits_stop():
    sessions, hist = _day([100.8, 100.9] + list(np.linspace(100.5, 99.0, 10)))
    trades = simulate_day(pd.Timestamp(DAY).date(), sessions, hist, SPY_UP, 25_000, CFG)
    assert len(trades) == 1 and trades[0].reason.startswith("stop")
    assert trades[0].pnl < 0 and trades[0].r_multiple > -1.3


def test_flattens_at_end_of_day():
    sessions, hist = _day([100.8, 101.0])       # never reaches stop or target
    t = simulate_day(pd.Timestamp(DAY).date(), sessions, hist, SPY_UP, 25_000, CFG)[0]
    assert t.reason == "eod" and t.exit_time.time() == time(15, 50)


def test_run_backtest_end_to_end():
    minute = {"TEST": pd.concat([flat_then([100.8] + list(np.linspace(100.9, 104, 20)) + [104] * 354),
                                 session([100, 100.5, 99.5] * 5 + [100.8, 100.9] + [99] * 373, day="2026-03-11")
                                 ]).tz_convert("UTC"),
              "SPY": pd.concat([SPY_UP, session(list(np.linspace(500, 505, 390)), day="2026-03-11")]).tz_convert("UTC")}
    dly = {"TEST": daily(n=40, end="2026-03-12"), "SPY": daily(n=40, price=500, end="2026-03-12")}
    trades, curve = run_backtest(minute, dly, CFG, 25_000)
    assert len(trades) == 2 and len(curve) == 2
    assert trades[0].reason == "target" and trades[1].reason.startswith("stop")
