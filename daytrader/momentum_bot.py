"""Live / paper trading loop for the SPY intraday momentum strategy.

    python -m daytrader.momentum_bot              # paper trading (default)
    python -m daytrader.momentum_bot --once       # one session, then exit
    python -m daytrader.momentum_bot --long-only  # no shorting

Start it any time before 10:00 ET and leave it running. Between decisions the
stop-loss sits at Alpaca, so a position stays protected if this script dies.
Rules are in daytrader/momentum.py; settings in MomentumConfig (config.py).
"""
import argparse
import logging
import os
from dataclasses import replace
from datetime import timedelta

from .bot import completed, now_et, sleep_until
from .broker import Broker
from .config import MomentumConfig
from .indicators import vwap
from .momentum import build_plan, decide, position_qty, split_sessions
from .strategy import regular_session

log = logging.getLogger("daytrader")


def today_bars(broker: Broker, sym: str, since):
    b = broker.minute_bars([sym], since).get(sym)
    return regular_session(b) if b is not None else None


def run_session(broker: Broker, cfg: MomentumConfig):
    sym = cfg.symbol
    now = now_et()
    today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_at = broker.clock().next_close.astimezone(now.tzinfo)
    flatten_at = min(now.replace(hour=cfg.flatten_time.hour, minute=cfg.flatten_time.minute,
                                 second=0, microsecond=0), close_at - timedelta(minutes=5))

    acct = broker.account()
    start_equity = float(acct.last_equity)
    allow_shorts = cfg.allow_shorts and bool(getattr(acct, "shorting_enabled", True))
    if cfg.allow_shorts and not allow_shorts:
        log.warning("Shorting is not enabled on this account; trading long only.")
        cfg = replace(cfg, allow_shorts=False)
    pdt_limited = float(acct.equity) < 25_000
    if pdt_limited:
        log.warning("Equity < $25k: the Pattern Day Trader rule allows only 3 day trades per "
                    "5 business days. This strategy needs ~1-2 per day, so it can't run properly.")

    # History for the noise area: the last few weeks of 1-min bars, before today.
    hist = broker.minute_bars([sym], today_open - timedelta(days=cfg.lookback_days * 2 + 14),
                              today_open).get(sym)
    prior = [s for d, s in sorted(split_sessions(hist).items()) if d < today_open.date()] \
        if hist is not None else []

    sleep_until(today_open + timedelta(minutes=1, seconds=10))
    today = today_bars(broker, sym, today_open)
    if today is None or today.empty:
        log.error("No bars for %s today; sitting out.", sym)
        return
    plan = build_plan(prior, float(today["open"].iloc[0]), cfg, today_open.date())
    if plan is None:
        log.error("Not enough history (%d sessions) to build the noise area; sitting out.", len(prior))
        return
    log.info("%s open %.2f, prev close %.2f, daily vol %.2f%%, leverage %.2fx",
             sym, plan.open, plan.prev_close, plan.daily_vol * 100,
             min(cfg.max_leverage, cfg.target_daily_vol_pct / 100 / plan.daily_vol))

    trades, halted = 0, False
    checkpoints = [today_open + timedelta(minutes=m)
                   for m in range(cfg.first_check_min, 391, cfg.check_every_min)]
    for cp in checkpoints:
        if cp + timedelta(seconds=10) >= flatten_at:
            break
        if cp + timedelta(seconds=10) < now_et():
            continue
        sleep_until(cp + timedelta(seconds=10))    # let the bar ending at `cp` publish
        minute = int((cp - today_open).total_seconds() // 60)

        bars = today_bars(broker, sym, today_open)
        bars = completed(bars, cp) if bars is not None else None
        if bars is None or bars.empty or bars.index[-1] < cp - timedelta(minutes=3):
            log.warning("%s: stale data at %s; skipping this check.", sym, cp.strftime("%H:%M"))
            continue
        price, vw = float(bars["close"].iloc[-1]), float(vwap(bars).iloc[-1])
        qty_now = broker.position_qty(sym)
        side_now = (qty_now > 0) - (qty_now < 0)

        acct = broker.account()
        if not halted and float(acct.equity) - start_equity <= -start_equity * cfg.daily_loss_limit_pct / 100:
            log.warning("Daily loss limit hit. Flattening and stopping for today.")
            broker.close_symbol(sym)
            halted, side_now = True, 0
        can_enter = (not halted and trades < cfg.max_trades_per_day
                     and not (pdt_limited and int(acct.daytrade_count or 0) >= 3))

        d = decide(plan, minute, price, vw, side_now, cfg, can_enter)
        log.info("%s %s price %.2f  bands %.2f / %.2f  vwap %.2f  pos %d -> %s",
                 cp.strftime("%H:%M"), sym, price, d.lower, d.upper, vw, qty_now, d.action)

        try:
            if d.action == "exit":
                broker.close_symbol(sym)
            elif d.action == "hold":
                broker.set_stop(sym, abs(qty_now), "sell" if qty_now > 0 else "buy", d.stop)
            elif d.action in ("long", "short"):
                qty = position_qty(float(acct.equity), float(acct.buying_power), price,
                                   plan.daily_vol, cfg)
                if qty < 1:
                    continue
                o = broker.market(sym, qty, "buy" if d.action == "long" else "sell")
                filled = abs(broker.position_qty(sym))
                if not filled:
                    log.error("Entry order not filled (status %s).", o.status)
                    continue
                trades += 1
                broker.set_stop(sym, filled, "sell" if d.action == "long" else "buy", d.stop)
                log.info("ENTER %s %d %s @~%s  stop %.2f", d.action.upper(), filled, sym,
                         o.filled_avg_price, d.stop)
        except Exception as e:
            log.error("Order handling failed: %s. Flattening %s to be safe.", e, sym)
            broker.close_symbol(sym)

    sleep_until(flatten_at)
    log.info("Flatten time: closing %s.", sym)
    broker.close_symbol(sym)
    log.info("Session done. Day P&L: $%.2f", float(broker.account().equity) - start_equity)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="exit after one session")
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--symbol", default="SPY")
    args = ap.parse_args()

    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("logs/momentum.log")])
    broker = Broker()
    if not broker.paper:
        ans = input("ALPACA_PAPER is false: this will trade REAL money. Type 'I understand': ")
        if ans.strip() != "I understand":
            raise SystemExit("Aborted.")
    cfg = MomentumConfig(symbol=args.symbol, allow_shorts=not args.long_only)
    log.info("Mode: %s | feed: %s | %s momentum", "PAPER" if broker.paper else "LIVE",
             broker.feed.value, cfg.symbol)

    while True:
        clock = broker.clock()
        now = now_et()
        if clock.is_open and now < clock.next_close.astimezone(now.tzinfo) - timedelta(minutes=10):
            run_session(broker, cfg)
            if args.once:
                return
            sleep_until(now_et() + timedelta(minutes=20))   # past the close
        else:
            nxt = clock.next_open.astimezone(now.tzinfo)
            log.info("Market closed. Sleeping until %s ET.", nxt.strftime("%a %Y-%m-%d %H:%M"))
            sleep_until(nxt + timedelta(seconds=5))


if __name__ == "__main__":
    main()
