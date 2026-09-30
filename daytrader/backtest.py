"""Backtest the exact same scanner + strategy on historical minute bars.

Usage:
    python -m daytrader.backtest --days 60
    python -m daytrader.backtest --days 120 --feed sip --shorts

Run this BEFORE paper trading, and again after changing any setting in config.py.
"""
import argparse
import hashlib
import logging
import os
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from .config import Config
from .strategy import (ET, check_entry, position_size, rank_candidates,
                       regular_session, score_candidate)

log = logging.getLogger(__name__)


@dataclass
class Trade:
    day: object
    symbol: str
    side: str
    qty: int
    entry_time: datetime
    entry: float
    stop: float
    target: float
    exit_time: datetime = None
    exit: float = None
    reason: str = ""

    @property
    def pnl(self) -> float:
        d = 1 if self.side == "buy" else -1
        return d * (self.exit - self.entry) * self.qty

    @property
    def r_multiple(self) -> float:
        risk = abs(self.entry - self.stop) * self.qty
        return self.pnl / risk if risk else 0.0


def _slip(price: float, side: str, entering: bool, cfg: Config) -> float:
    """Worse price by slippage_pct: pay up when buying, receive less when selling."""
    buying = (side == "buy") == entering
    s = price * cfg.slippage_pct / 100
    return price + s if buying else price - s


def _check_exit(t: Trade, bar, cfg: Config):
    """Return (price, reason) if the bracket stop/target is hit on this bar.
    If both could be hit in the same bar we assume the stop (conservative)."""
    o, h, l = bar["open"], bar["high"], bar["low"]
    if t.side == "buy":
        if o <= t.stop:
            return _slip(o, t.side, False, cfg), "stop(gap)"
        if l <= t.stop:
            return _slip(t.stop, t.side, False, cfg), "stop"
        if o >= t.target:
            return o, "target"
        if h >= t.target:
            return t.target, "target"
    else:
        if o >= t.stop:
            return _slip(o, t.side, False, cfg), "stop(gap)"
        if h >= t.stop:
            return _slip(t.stop, t.side, False, cfg), "stop"
        if o <= t.target:
            return o, "target"
        if l <= t.target:
            return t.target, "target"
    return None


def simulate_day(day, sessions: dict, daily: dict, market: pd.DataFrame,
                 equity: float, cfg: Config) -> list:
    """Simulate one trading day. `sessions` = {sym: that day's regular-session
    1-min bars (ET index)}, `daily` = {sym: daily bars strictly before `day`}."""
    # 1) Scan at the end of the opening range, using only data available then.
    cands = []
    for sym, bars in sessions.items():
        if sym == cfg.market_symbol or bars.empty or sym not in daily:
            continue
        or_end = bars.index[0].replace(hour=9, minute=30) + timedelta(
            minutes=cfg.opening_range_minutes)
        c = score_candidate(sym, daily[sym], bars[bars.index < or_end], cfg)
        if c:
            cands.append(c)
    watch = {c.symbol: c for c in rank_candidates(cands, cfg)}
    if not watch:
        return []

    # 2) Walk minute by minute across all watched symbols in time order.
    timeline = sorted(set().union(*[sessions[s].index for s in watch]))
    open_trades, done, traded = {}, [], set()
    pending = {}           # sym -> Signal waiting for next bar's open
    realized, halted = 0.0, False
    loss_limit = -equity * cfg.daily_loss_limit_pct / 100

    for ts in timeline:
        flatten = ts.time() >= cfg.flatten_time
        for sym in list(watch):
            bars = sessions[sym]
            if ts not in bars.index:
                continue
            bar = bars.loc[ts]

            # Fill a pending entry at this bar's open (market order after signal).
            if sym in pending:
                sig = pending.pop(sym)
                if not flatten and not halted:
                    qty = position_size(equity, equity, sig, cfg)
                    if qty > 0:
                        fill = _slip(bar["open"], sig.side, True, cfg)
                        open_trades[sym] = Trade(day, sym, sig.side, qty, ts, fill,
                                                 sig.stop, sig.target)

            # Manage an open position.
            if sym in open_trades:
                t = open_trades[sym]
                hit = None if flatten else _check_exit(t, bar, cfg)
                if flatten:
                    hit = (_slip(bar["open"], t.side, False, cfg), "eod")
                if hit:
                    t.exit, t.reason, t.exit_time = hit[0], hit[1], ts
                    realized += t.pnl - cfg.commission_per_share * t.qty * 2
                    done.append(open_trades.pop(sym))
                    if realized <= loss_limit and not halted:
                        halted = True

        if halted and open_trades:
            for sym, t in list(open_trades.items()):
                bar = sessions[sym].loc[ts] if ts in sessions[sym].index else None
                px = bar["close"] if bar is not None else t.entry
                t.exit, t.reason, t.exit_time = _slip(px, t.side, False, cfg), "daily-limit", ts
                done.append(open_trades.pop(sym))
        if flatten and not open_trades:
            break

        # Look for new entries on bars that just closed at `ts`.
        if halted or len(traded) >= cfg.max_trades_per_day:
            continue
        mkt = market[market.index <= ts] if market is not None else None
        for sym, cand in watch.items():
            if sym in traded or sym in open_trades or sym in pending:
                continue
            if len(open_trades) + len(pending) >= cfg.max_open_positions:
                break
            bars = sessions[sym]
            if ts not in bars.index:
                continue
            sig = check_entry(cand, bars[bars.index <= ts], cfg, mkt)
            if sig:
                pending[sym] = sig
                traded.add(sym)

    # Anything still open (e.g. data ended early) closes at its last price.
    for sym, t in open_trades.items():
        last = sessions[sym].iloc[-1]
        t.exit, t.reason, t.exit_time = _slip(last["close"], t.side, False, cfg), "eod", sessions[sym].index[-1]
        done.append(t)
    return done


def run_backtest(minute: dict, daily: dict, cfg: Config, start_equity: float = 25_000):
    """minute/daily = {sym: DataFrame} covering the whole test period."""
    sessions_by_day = {}
    for sym, df in minute.items():
        rs = regular_session(df)
        for day, g in rs.groupby(rs.index.date):
            sessions_by_day.setdefault(day, {})[sym] = g
    daily_et = {s: d.tz_convert(ET) if d.index.tz is not None else d for s, d in daily.items()}

    equity, curve, trades = start_equity, [], []
    for day in sorted(sessions_by_day):
        sessions = sessions_by_day[day]
        hist = {s: d[d.index.date < day] for s, d in daily_et.items()}
        market = sessions.get(cfg.market_symbol)
        day_trades = simulate_day(day, sessions, hist, market, equity, cfg)
        pnl = sum(t.pnl - cfg.commission_per_share * t.qty * 2 for t in day_trades)
        equity += pnl
        trades += day_trades
        curve.append((day, equity))
    return trades, pd.Series(dict(curve), dtype=float)


def summarize(trades: list, curve: pd.Series, start_equity: float) -> str:
    if not trades:
        return "No trades. Loosen scanner filters (min_rvol, min_atr_pct) or test more days."
    pnl = pd.Series([t.pnl for t in trades])
    r = pd.Series([t.r_multiple for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() else float("inf")
    peak = curve.cummax()
    max_dd = ((curve - peak) / peak).min() * 100
    reasons = pd.Series([t.reason for t in trades]).value_counts().to_dict()
    lines = [
        f"Days tested        : {len(curve)}",
        f"Trades             : {len(trades)}  ({len(trades)/max(len(curve),1):.2f}/day)",
        f"Win rate           : {len(wins)/len(trades)*100:.1f}%",
        f"Avg R per trade    : {r.mean():+.2f}R   (needs to be > 0 after costs)",
        f"Profit factor      : {pf:.2f}   (> 1.2 is worth paper trading; < 1 loses money)",
        f"Total P&L          : ${pnl.sum():,.2f}  ({(curve.iloc[-1]/start_equity-1)*100:+.2f}%)",
        f"Max drawdown       : {max_dd:.2f}%",
        f"Exit reasons       : {reasons}",
    ]
    return "\n".join(lines)


def _load_or_fetch(broker, symbols, start, end, cache_dir, feed_name):
    os.makedirs(cache_dir, exist_ok=True)
    digest = hashlib.md5(",".join(sorted(symbols)).encode()).hexdigest()[:10]
    key = f"{feed_name}_{start:%Y%m%d}_{end:%Y%m%d}_{digest}.pkl"
    path = os.path.join(cache_dir, key)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    minute, daily = {}, {}
    for i in range(0, len(symbols), 10):   # chunk to keep requests reasonable
        chunk = symbols[i:i + 10]
        print(f"  fetching {', '.join(chunk)} ...", flush=True)
        minute.update(broker.minute_bars(chunk, start, end))
        daily.update(broker.bars(chunk, _day_tf(), start - timedelta(days=45), end))
    with open(path, "wb") as f:
        pickle.dump((minute, daily), f)
    return minute, daily


def _day_tf():
    from alpaca.data.timeframe import TimeFrame
    return TimeFrame.Day


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60, help="calendar days to test")
    ap.add_argument("--equity", type=float, default=25_000)
    ap.add_argument("--feed", choices=["iex", "sip"], help="override ALPACA_DATA_FEED")
    ap.add_argument("--shorts", action="store_true", help="also test short breakouts")
    ap.add_argument("--symbols", help="comma-separated list instead of config universe")
    ap.add_argument("--csv", help="write every trade to this CSV file")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.feed:
        os.environ["ALPACA_DATA_FEED"] = args.feed
    from .broker import Broker
    broker = Broker()
    cfg = Config(allow_shorts=args.shorts)
    symbols = args.symbols.split(",") if args.symbols else list(cfg.universe)
    if cfg.market_symbol not in symbols:
        symbols.append(cfg.market_symbol)

    end = datetime.now().astimezone() - timedelta(minutes=20)  # free plan: data >15 min old
    start = end - timedelta(days=args.days)
    print(f"Backtesting {len(symbols)} symbols, {start:%Y-%m-%d} -> {end:%Y-%m-%d}, feed={broker.feed.value}")
    minute, daily = _load_or_fetch(broker, symbols, start, end, "data_cache", broker.feed.value)

    trades, curve = run_backtest(minute, daily, cfg, args.equity)
    print("\n" + summarize(trades, curve, args.equity))
    if cfg.market_symbol in daily:
        spy = daily[cfg.market_symbol]
        spy = spy[spy.index >= pd.Timestamp(start)]
        if len(spy) > 1:
            print(f"SPY buy & hold     : {(spy['close'].iloc[-1]/spy['close'].iloc[0]-1)*100:+.2f}%  (benchmark)")
    if args.csv and trades:
        pd.DataFrame([{**t.__dict__, "pnl": t.pnl, "R": t.r_multiple} for t in trades]).to_csv(args.csv, index=False)
        print(f"Trades written to {args.csv}")


if __name__ == "__main__":
    main()
