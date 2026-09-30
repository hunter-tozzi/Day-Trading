"""Thin wrapper around alpaca-py for data and orders."""
import logging
import os
from datetime import datetime, timedelta

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MostActivesRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (MarketOrderRequest, StopLossRequest,
                                     TakeProfitRequest)

from .strategy import Signal

log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes")


class Broker:
    def __init__(self):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (see .env.example)")
        self.paper = _env_bool("ALPACA_PAPER", True)
        feed = os.getenv("ALPACA_DATA_FEED", "iex").lower()
        self.feed = DataFeed.SIP if feed == "sip" else DataFeed.IEX
        self.trading = TradingClient(key, secret, paper=self.paper)
        self.data = StockHistoricalDataClient(key, secret)
        self.screener = ScreenerClient(key, secret)

    # ---------- market data ----------
    def bars(self, symbols, timeframe: TimeFrame, start: datetime,
             end: datetime = None) -> dict:
        """Return {symbol: DataFrame[open, high, low, close, volume]}."""
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=timeframe,
                               start=start, end=end, feed=self.feed)
        df = self.data.get_stock_bars(req).df
        out = {}
        if df.empty:
            return out
        for sym, g in df.groupby(level="symbol"):
            g = g.droplevel("symbol")[["open", "high", "low", "close", "volume"]]
            out[sym] = g.sort_index()
        return out

    def daily_bars(self, symbols, days: int, end: datetime = None) -> dict:
        end = end or datetime.now().astimezone()
        return self.bars(symbols, TimeFrame.Day, end - timedelta(days=int(days * 1.6) + 5), end)

    def minute_bars(self, symbols, start: datetime, end: datetime = None) -> dict:
        return self.bars(symbols, TimeFrame.Minute, start, end)

    def most_active(self, top: int = 50) -> list:
        try:
            res = self.screener.get_most_actives(MostActivesRequest(top=top))
            return [a.symbol for a in res.most_actives]
        except Exception as e:  # screener may be unavailable on some plans
            log.warning("Most-actives screener unavailable (%s); using fixed universe", e)
            return []

    # ---------- account / orders ----------
    def account(self):
        return self.trading.get_account()

    def clock(self):
        return self.trading.get_clock()

    def positions(self) -> dict:
        return {p.symbol: p for p in self.trading.get_all_positions()}

    def tradable(self, symbol: str, short: bool = False) -> bool:
        try:
            a = self.trading.get_asset(symbol)
        except Exception:
            return False
        ok = a.tradable and a.status == AssetStatus.ACTIVE
        return ok and (not short or (a.shortable and a.easy_to_borrow))

    def submit_bracket(self, sig: Signal, qty: int):
        """Market entry with server-side stop-loss and take-profit.

        The protective orders live at Alpaca, so they still work if this bot crashes.
        """
        req = MarketOrderRequest(
            symbol=sig.symbol,
            qty=qty,
            side=OrderSide.BUY if sig.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(sig.target, 2)),
            stop_loss=StopLossRequest(stop_price=round(sig.stop, 2)),
        )
        return self.trading.submit_order(req)

    def flatten_all(self):
        """Cancel every open order and close every position."""
        self.trading.close_all_positions(cancel_orders=True)
