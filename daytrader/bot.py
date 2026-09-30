"""Live / paper trading loop.

    python -m daytrader.bot            # paper trading (default)
    python -m daytrader.bot --once     # run one session then exit

Start it any time; it sleeps until the market opens. Keep it running during the
session. Stop-loss and take-profit orders sit at Alpaca, so open positions stay
protected even if this script dies.
"""
import argparse
import logging
import os
import time as systime
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from .broker import Broker
from .config import Config
from .strategy import (ET, check_entry, position_size, rank_candidates,
                       regular_session, score_candidate)

log = logging.getLogger("daytrader")


def now_et() -> datetime:
    return datetime.now(ZoneInfo(ET))


def sleep_until(t: datetime):
    while True:
        remaining = (t - now_et()).total_seconds()
        if remaining <= 0:
            return
        systime.sleep(min(remaining, 60))


def completed(bars, now):
    """Drop a bar that is still forming (Alpaca minute bars are stamped at their start)."""
    return bars[bars.index + timedelta(minutes=1) <= now]


def build_universe(broker: Broker, cfg: Config) -> list:
    syms = broker.most_active(50) + list(cfg.universe)
    seen, out = set(), []
    for s in syms:
        if s not in seen and s.isalpha() and s != cfg.market_symbol:
            seen.add(s)
            out.append(s)
    return out


def symbols_traded_today(broker: Broker, session_open: datetime) -> set:
    """So a restart mid-day doesn't re-enter something already traded."""
    orders = broker.trading.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.ALL, after=session_open, limit=500))
    return {o.symbol for o in orders}


def run_session(broker: Broker, cfg: Config):
    now = now_et()
    today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    or_end = today_open + timedelta(minutes=cfg.opening_range_minutes)
    cutoff = now.replace(hour=cfg.entry_cutoff.hour, minute=cfg.entry_cutoff.minute, second=0, microsecond=0)
    flatten_at = now.replace(hour=cfg.flatten_time.hour, minute=cfg.flatten_time.minute, second=0, microsecond=0)

    acct = broker.account()
    start_equity = float(acct.last_equity)
    trades_left = cfg.max_trades_per_day
    if float(acct.equity) < 25_000:
        # Pattern Day Trader rule: < $25k accounts get 3 day trades per 5 business days.
        pdt_left = max(0, 3 - int(acct.daytrade_count or 0))
        trades_left = min(trades_left, pdt_left)
        log.warning("Equity < $25k: PDT rule limits you to %d day trade(s) right now.", pdt_left)
    log.info("Session start. Equity $%.2f (prev close $%.2f)", float(acct.equity), start_equity)

    # ---- 1) Scan once the opening range is complete ----
    sleep_until(or_end + timedelta(seconds=10))
    universe = build_universe(broker, cfg)
    log.info("Scanning %d symbols ...", len(universe))
    daily = broker.daily_bars(universe, cfg.lookback_days + 5, end=today_open)
    minute = broker.minute_bars(universe, today_open)
    cands = []
    for sym in universe:
        if sym not in daily or sym not in minute:
            continue
        hist = daily[sym][daily[sym].index.tz_convert(ET).date < today_open.date()]
        c = score_candidate(sym, hist, regular_session(minute[sym]).loc[lambda d: d.index < or_end], cfg)
        if c and broker.tradable(sym):
            cands.append(c)
    watch = {c.symbol: c for c in rank_candidates(cands, cfg)}
    for c in watch.values():
        log.info("  WATCH %-5s rvol %.1f  gap %+.1f%%  range %.2f-%.2f  ATR %.2f",
                 c.symbol, c.rvol, c.gap_pct, c.or_low, c.or_high, c.atr)
    if not watch:
        log.info("Nothing passed the scanner today. Sitting out (that's a valid outcome).")

    traded = symbols_traded_today(broker, today_open)
    halted = False

    # ---- 2) Minute loop ----
    while True:
        now = now_et()
        if now >= flatten_at:
            log.info("Flatten time: closing all positions and orders.")
            broker.flatten_all()
            break

        acct = broker.account()
        day_pnl = float(acct.equity) - start_equity
        if not halted and day_pnl <= -start_equity * cfg.daily_loss_limit_pct / 100:
            log.warning("Daily loss limit hit (%.2f). Flattening and stopping for today.", day_pnl)
            broker.flatten_all()
            halted = True

        positions = broker.positions()
        can_enter = (not halted and now < cutoff and trades_left > 0 and watch
                     and len(positions) < cfg.max_open_positions)
        if can_enter:
            todo = [s for s in watch if s not in traded and s not in positions]
            if todo:
                bars = broker.minute_bars(todo + [cfg.market_symbol], today_open)
                mkt = bars.get(cfg.market_symbol)
                if mkt is not None:
                    mkt = completed(regular_session(mkt), now)
                for sym in todo:
                    if sym not in bars:
                        continue
                    b = completed(regular_session(bars[sym]), now)
                    if b.empty or b.index[-1] < now - timedelta(minutes=3):
                        continue  # stale data; don't act on an old breakout
                    sig = check_entry(watch[sym], b, cfg, mkt)
                    if not sig:
                        continue
                    if sig.side == "sell" and not broker.tradable(sym, short=True):
                        continue
                    acct = broker.account()
                    qty = position_size(float(acct.equity), float(acct.buying_power), sig, cfg)
                    if qty < 1:
                        continue
                    try:
                        broker.submit_bracket(sig, qty)
                    except Exception as e:
                        log.error("Order for %s failed: %s", sym, e)
                        traded.add(sym)
                        continue
                    traded.add(sym)
                    trades_left -= 1
                    log.info("ENTER %s %d %s @~%.2f  stop %.2f  target %.2f",
                             sig.side.upper(), qty, sym, sig.entry, sig.stop, sig.target)
                    if trades_left <= 0 or len(broker.positions()) >= cfg.max_open_positions:
                        break

        # Wake ~10s after the next minute closes so that bar is published.
        nxt = (now + timedelta(minutes=1)).replace(second=10, microsecond=0)
        sleep_until(nxt)

    acct = broker.account()
    log.info("Session done. Day P&L: $%.2f", float(acct.equity) - start_equity)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="exit after one session")
    ap.add_argument("--shorts", action="store_true", help="also trade short breakouts")
    args = ap.parse_args()

    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log")])
    broker = Broker()
    if not broker.paper:
        ans = input("ALPACA_PAPER is false: this will trade REAL money. Type 'I understand': ")
        if ans.strip() != "I understand":
            raise SystemExit("Aborted.")
    cfg = Config(allow_shorts=args.shorts)
    log.info("Mode: %s | feed: %s", "PAPER" if broker.paper else "LIVE", broker.feed.value)

    while True:
        clock = broker.clock()
        now = now_et()
        flatten_at = now.replace(hour=cfg.flatten_time.hour, minute=cfg.flatten_time.minute)
        if clock.is_open and now < flatten_at:
            run_session(broker, cfg)
            if args.once:
                return
            sleep_until(now_et() + timedelta(minutes=20))  # past the close
        else:
            nxt = clock.next_open.astimezone(now.tzinfo)
            log.info("Market closed. Sleeping until %s ET.", nxt.strftime("%a %Y-%m-%d %H:%M"))
            sleep_until(nxt + timedelta(seconds=5))


if __name__ == "__main__":
    main()
