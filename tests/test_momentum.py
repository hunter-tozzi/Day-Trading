import numpy as np
import pandas as pd
import pytest

from daytrader.config import MomentumConfig
from daytrader.momentum import (DayPlan, build_plan, decide, open_moves,
                                position_qty, split_sessions)
from daytrader.momentum_backtest import run_backtest, simulate_day

CFG = MomentumConfig()


def session(closes, day="2026-03-10"):
    idx = pd.date_range(f"{day} 09:30", periods=len(closes), freq="1min", tz="America/New_York")
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.01, "low": np.minimum(o, c) - 0.01,
                         "close": c, "volume": 1000}, index=idx)


def flat_plan(sigma=0.002, open_=100.0, prev_close=100.0, vol=0.01):
    return DayPlan(None, open_, prev_close, pd.Series(sigma, index=range(1, 391)), vol)


def random_walk(days, seed, momentum=0.0):
    rng = np.random.default_rng(seed)
    out, p = [], 400.0
    for d in pd.bdate_range("2022-01-03", periods=days):
        p *= np.exp(rng.normal(0, 0.004))
        r = rng.normal(0, 0.0005, 390)
        r[30:] += np.sign(rng.normal()) * momentum
        c = p * np.exp(np.cumsum(r))
        out.append(session(np.r_[c], day=str(d.date())))
        p = c[-1]
    return pd.concat(out).tz_convert("UTC")


def test_bands_absorb_overnight_gap():
    up, lo = flat_plan(open_=101, prev_close=99).bands(30, CFG)
    assert up == pytest.approx(101 * 1.002) and lo == pytest.approx(99 * 0.998)


def test_open_moves_by_minute():
    m = open_moves(session([100, 101, 99]))
    assert m[1] == 0 and m[2] == pytest.approx(0.01) and m[3] == pytest.approx(0.01)
    assert len(m) == 390 and m[390] == pytest.approx(0.01)       # forward-filled


def test_build_plan_needs_history_and_uses_only_prior_days():
    prior = [session(np.linspace(100, 100 + k % 3, 390), day=str(d.date()))
             for k, d in enumerate(pd.bdate_range("2026-02-02", periods=CFG.lookback_days + 1))]
    assert build_plan(prior[:-1], 100, CFG) is None
    plan = build_plan(prior, 100, CFG)
    assert plan.prev_close == pytest.approx(prior[-1]["close"].iloc[-1])
    assert plan.daily_vol > 0 and plan.sigma.notna().all()


def test_decide_entries_and_stops():
    p = flat_plan()                                   # bands 99.8 / 100.2
    assert decide(p, 30, 100.5, 100.1, 0, CFG).action == "long"
    assert decide(p, 30, 100.5, 100.1, 0, CFG).stop == pytest.approx(100.2)
    assert decide(p, 30, 100.5, 100.7, 0, CFG).action == "none"      # below VWAP: would stop at once
    assert decide(p, 30, 100.1, 100.0, 0, CFG).action == "none"      # inside the noise area
    assert decide(p, 30, 99.5, 99.9, 0, CFG).action == "short"
    assert decide(p, 30, 99.5, 99.9, 0, MomentumConfig(allow_shorts=False)).action == "none"
    assert decide(p, 390, 100.5, 100.1, 0, CFG).action == "none"     # past last entry
    assert decide(p, 60, 100.5, 100.1, 0, CFG, can_enter=False).action == "none"
    hold = decide(p, 60, 100.9, 100.4, 1, CFG)
    assert hold.action == "hold" and hold.stop == pytest.approx(100.4)  # trails VWAP
    assert decide(p, 60, 100.3, 100.4, 1, CFG).action == "exit"


def test_position_size_targets_volatility():
    assert position_qty(25_000, 100_000, 500, 0.01, CFG) == 100      # 2%/1% = 2x -> $50k
    assert position_qty(25_000, 100_000, 500, 0.02, CFG) == 50       # 1x
    assert position_qty(25_000, 100_000, 500, 0.001, CFG) == 100     # capped at 2x
    assert position_qty(25_000, 20_000, 500, 0.01, CFG) == 38        # buying power


def test_trend_day_is_caught_and_stop_gap_fills_at_open():
    up = session([100] * 30 + list(np.linspace(100.3, 103, 360)))
    t = simulate_day(flat_plan(), up, 25_000, CFG)
    assert len(t) == 1 and t[0].side == 1 and t[0].reason == "eod" and t[0].pnl > 0
    assert t[0].entry_time.strftime("%H:%M") == "10:30"   # seen at the 10:30 check, filled next bar

    crash = session([100] * 29 + [100.5] * 6 + [99.0] * 355)   # long at 10:00, then a gap
    crash.iloc[35, crash.columns.get_loc("open")] = 99.0       # opens below the 100.2 stop
    t = simulate_day(flat_plan(), crash, 25_000, CFG)[0]
    assert t.side == 1 and t.stop == pytest.approx(100.2)
    assert t.reason == "stop" and t.exit == pytest.approx(99.0 - CFG.slippage_per_share)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_edge_on_random_walk(seed):
    """If random prices show a real profit, the backtest is peeking at the future."""
    cfg = MomentumConfig(slippage_per_share=0)
    trades, curve = run_backtest(random_walk(260, seed), cfg, 25_000)
    rets = curve.pct_change().dropna()
    t_stat = rets.mean() / rets.std() * np.sqrt(len(rets))
    assert len(trades) > 100 and abs(t_stat) < 3


def test_real_momentum_is_detected():
    trades, curve = run_backtest(random_walk(120, 7, momentum=0.00006), CFG, 25_000)
    assert curve.iloc[-1] > 25_000 * 1.2


def test_split_sessions_drops_extended_hours():
    idx = pd.date_range("2026-03-10 08:00", "2026-03-10 17:00", freq="1min", tz="America/New_York")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}, index=idx)
    (s,) = split_sessions(df).values()
    assert len(s) == 390
