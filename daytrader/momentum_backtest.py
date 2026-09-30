"""Backtest the SPY intraday momentum strategy on historical 1-minute bars.

Usage:
    python -m daytrader.momentum_backtest --years 5 --feed sip
    python -m daytrader.momentum_backtest --years 3 --symbol QQQ --long-only
    python -m daytrader.momentum_backtest --csv spy_data.csv     # your own minute data

Alpaca's free plan includes SIP (all exchanges) minute history back to 2016, so
test as many years as you can. A strategy that only works in one year doesn't work.
"""
import argparse
import os
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .config import MomentumConfig
from .indicators import vwap
from .momentum import (build_plan, decide, is_checkpoint, minutes_since_open,
                       position_qty, split_sessions)


@dataclass
class MTrade:
    day: object
    side: int                # +1 long, -1 short
    qty: int
    entry_time: object
    entry: float
    stop: float
    exit_time: object = None
    exit: float = None
    reason: str = ""

    @property
    def pnl(self) -> float:
        return self.side * (self.exit - self.entry) * self.qty


def simulate_day(plan, session: pd.DataFrame, equity: float, cfg: MomentumConfig) -> list:
    """One session. Decisions use only bars that have closed; orders fill at the
    next bar's open plus slippage; the stop is live between checkpoints (it sits
    at the broker in the real bot) and fills at the worse of stop and open."""
    mins = minutes_since_open(session.index)
    vw = vwap(session).to_numpy()
    o, h, l, c = (session[k].to_numpy() for k in ("open", "high", "low", "close"))
    slip = cfg.slippage_per_share

    trades, pos, pending, exit_next = [], None, None, False

    def close(i, px, reason):
        nonlocal pos
        pos.exit, pos.exit_time, pos.reason = px - pos.side * slip, session.index[i], reason
        trades.append(pos)
        pos = None

    for i in range(len(session)):
        ts = session.index[i]
        if ts.time() >= cfg.flatten_time:
            if pos:
                close(i, o[i], "eod")
            pending = None
            break
        if exit_next and pos:
            close(i, o[i], "exit")
        exit_next = False
        if pending is not None:
            side, stop = pending
            pending = None
            qty = position_qty(equity, equity * cfg.max_leverage, o[i], plan.daily_vol, cfg)
            if qty > 0:
                pos = MTrade(plan.day, side, qty, ts, o[i] + side * slip, stop)
        if pos:
            if pos.side > 0 and l[i] <= pos.stop:
                close(i, min(o[i], pos.stop), "stop")
            elif pos.side < 0 and h[i] >= pos.stop:
                close(i, max(o[i], pos.stop), "stop")

        m = int(mins[i])
        if not is_checkpoint(m, cfg):
            continue
        side_now = pos.side if pos else 0
        d = decide(plan, m, c[i], vw[i], side_now, cfg,
                   can_enter=len(trades) < cfg.max_trades_per_day)
        if d.action == "exit":
            exit_next = True
        elif d.action == "hold":
            pos.stop = d.stop
        elif d.action in ("long", "short"):
            pending = (1 if d.action == "long" else -1, d.stop)

    if pos:  # data ended early (e.g. half day)
        close(len(session) - 1, c[-1], "eod")
    return trades


def run_backtest(bars: pd.DataFrame, cfg: MomentumConfig, start_equity: float = 25_000):
    """`bars` = 1-min bars for one symbol (tz-aware index). Returns (trades, daily equity)."""
    sessions = split_sessions(bars)
    days = sorted(sessions)
    equity, curve, trades = start_equity, {}, []
    for k, day in enumerate(days):
        prior = [sessions[d] for d in days[max(0, k - cfg.lookback_days - 1):k]]
        today = sessions[day]
        plan = build_plan(prior, float(today["open"].iloc[0]), cfg, day)
        if plan is None:
            continue
        day_trades = simulate_day(plan, today, equity, cfg)
        equity += sum(t.pnl - 2 * cfg.commission_per_share * t.qty for t in day_trades)
        trades += day_trades
        curve[day] = equity
    return trades, pd.Series(curve, dtype=float)


def summarize(trades: list, curve: pd.Series, start_equity: float, bars: pd.DataFrame = None,
              cfg: MomentumConfig = None) -> str:
    if not trades or curve.empty:
        return "No trades."
    pnl = pd.Series([t.pnl for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() else float("inf")
    rets = curve.pct_change().fillna(curve.iloc[0] / start_equity - 1)
    years = max(len(curve) / 252, 1e-9)
    total = curve.iloc[-1] / start_equity - 1
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
    dd = (curve / curve.cummax().clip(lower=start_equity) - 1).min()
    reasons = pd.Series([t.reason for t in trades]).value_counts().to_dict()
    lines = [
        f"Days tested        : {len(curve)}  ({curve.index[0]} -> {curve.index[-1]})",
        f"Trades             : {len(trades)}  ({len(trades)/len(curve):.2f}/day, "
        f"{sum(t.side > 0 for t in trades)} long / {sum(t.side < 0 for t in trades)} short)",
        f"Win rate           : {len(wins)/len(trades)*100:.1f}%   (low is normal: small losses, bigger wins)",
        f"Profit factor      : {pf:.2f}   (< 1 loses money)",
        f"Total return       : {total*100:+.1f}%   CAGR {cagr*100:+.1f}%",
        f"Sharpe (daily)     : {sharpe:.2f}",
        f"Max drawdown       : {dd*100:.1f}%",
        f"Exit reasons       : {reasons}",
    ]
    if bars is not None:
        sess = split_sessions(bars)
        closes = pd.Series({d: s["close"].iloc[-1] for d, s in sess.items()})
        closes = closes[closes.index >= curve.index[0]]
        bh = closes.iloc[-1] / closes.iloc[0] - 1
        lines.append(f"Buy & hold         : {bh*100:+.1f}%  (benchmark, same period)")
        years_idx = pd.Index([d.year for d in curve.index])
        strat_year_end = curve.groupby(years_idx).last()
        bh_year_end = closes.groupby(pd.Index([d.year for d in closes.index])).last()
        prev_s, prev_b = start_equity, closes.iloc[0]
        lines.append("By year            : strategy  vs  buy & hold")
        for y, v in strat_year_end.items():
            lines.append(f"  {y}             : {(v/prev_s-1)*100:+7.1f}%  vs {(bh_year_end[y]/prev_b-1)*100:+7.1f}%")
            prev_s, prev_b = v, bh_year_end[y]
    return "\n".join(lines)


def fetch(symbol: str, start: datetime, end: datetime, cache_dir="data_cache") -> pd.DataFrame:
    """Download 1-min bars from Alpaca in 30-day chunks, cached on disk."""
    from alpaca.data.timeframe import TimeFrame
    from .broker import Broker
    broker = Broker()
    os.makedirs(cache_dir, exist_ok=True)
    parts, t = [], start
    while t < end:
        u = min(t + timedelta(days=30), end)
        path = os.path.join(cache_dir, f"{symbol}_{broker.feed.value}_{t:%Y%m%d}_{u:%Y%m%d}.pkl")
        if os.path.exists(path):
            with open(path, "rb") as f:
                df = pickle.load(f)
        else:
            print(f"  fetching {symbol} {t:%Y-%m-%d} -> {u:%Y-%m-%d} ...", flush=True)
            df = broker.bars([symbol], TimeFrame.Minute, t, u).get(symbol, pd.DataFrame())
            if u < end:   # never cache the still-growing last chunk
                with open(path, "wb") as f:
                    pickle.dump(df, f)
        parts.append(df)
        t = u
    df = pd.concat([p for p in parts if not p.empty])
    return df[~df.index.duplicated()].sort_index()


def load_csv(path: str) -> pd.DataFrame:
    """CSV with a timestamp column (first column) and open/high/low/close/volume."""
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    ts = pd.to_datetime(df.iloc[:, 0], utc=True)
    df = df.set_index(ts)[["open", "high", "low", "close", "volume"]]
    return df.sort_index()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=float, default=5)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--equity", type=float, default=25_000)
    ap.add_argument("--feed", choices=["iex", "sip"], default="sip")
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--max-leverage", type=float)
    ap.add_argument("--csv", help="use 1-min bars from this CSV instead of downloading")
    ap.add_argument("--trades-csv", help="write every trade to this CSV")
    args = ap.parse_args()

    cfg = MomentumConfig(symbol=args.symbol, allow_shorts=not args.long_only)
    if args.max_leverage:
        cfg.max_leverage = args.max_leverage
    if args.csv:
        bars = load_csv(args.csv)
    else:
        os.environ["ALPACA_DATA_FEED"] = args.feed
        end = datetime.now().astimezone() - timedelta(minutes=20)   # free plan: data > 15 min old
        start = end - timedelta(days=int(args.years * 365.25) + 30)
        print(f"Loading {cfg.symbol} 1-min bars {start:%Y-%m-%d} -> {end:%Y-%m-%d} (feed={args.feed})")
        bars = fetch(cfg.symbol, start, end)
    trades, curve = run_backtest(bars, cfg, args.equity)
    print("\n" + summarize(trades, curve, args.equity, bars, cfg))
    if args.trades_csv and trades:
        pd.DataFrame([{**t.__dict__, "pnl": t.pnl} for t in trades]).to_csv(args.trades_csv, index=False)
        print(f"Trades written to {args.trades_csv}")


if __name__ == "__main__":
    main()
