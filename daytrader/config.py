"""All tunable settings in one place. Change numbers here, then re-run the backtest."""
from dataclasses import dataclass, field
from datetime import time


@dataclass
class Config:
    # ---- Scanner: which stocks are worth watching today ----
    min_price: float = 10.0            # avoid penny stocks (wide spreads, erratic)
    max_price: float = 500.0
    # Avg daily shares over lookback, measured on YOUR data feed. The free IEX feed only
    # sees ~2-3% of real volume, so liquidity mostly comes from the universe/screener
    # (large caps + most-active stocks) rather than this number.
    min_avg_volume: int = 0
    min_atr_pct: float = 1.5           # daily ATR as % of price; stock must actually move
    min_gap_pct: float = 0.0           # |gap| vs prior close; 0 = don't require a gap
    min_rvol: float = 1.5              # opening-range volume vs normal for that window
    opening_range_volume_share: float = 0.09  # typical share of a day's volume traded in
                                              # the first 15 min; used to estimate "normal"
    max_watchlist: int = 10            # trade only the best N candidates each day
    lookback_days: int = 20            # daily bars used for avg volume / ATR

    # ---- Strategy: opening range breakout ----
    opening_range_minutes: int = 15    # range = first 15 min (9:30-9:45 ET)
    entry_cutoff: time = time(11, 30)  # no new entries after this (ET); ORB edge fades
    flatten_time: time = time(15, 50)  # close everything before the bell (ET)
    reward_risk: float = 2.0           # target = entry + 2 x risk
    max_risk_atr_frac: float = 0.5     # skip if stop distance > 0.5 x daily ATR (too wide)
    min_risk_pct: float = 0.15         # skip if stop distance < 0.15% of price (noise)
    require_market_filter: bool = True # longs only when SPY > its VWAP (shorts: SPY < VWAP)
    allow_shorts: bool = False         # shorting needs a margin account and borrowable shares

    # ---- Risk management ----
    risk_per_trade_pct: float = 0.5    # % of equity lost if the stop is hit
    max_position_pct: float = 20.0     # cap any single position at 20% of equity
    max_open_positions: int = 3
    max_trades_per_day: int = 4
    daily_loss_limit_pct: float = 2.0  # stop trading for the day after -2%

    # ---- Backtest cost assumptions ----
    slippage_pct: float = 0.03         # per side, % of price
    commission_per_share: float = 0.0  # Alpaca is commission-free for stocks

    # ---- Universe used when the live screener is unavailable, and for backtests ----
    universe: list = field(default_factory=lambda: [
        "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL", "NFLX", "AVGO",
        "PLTR", "COIN", "MU", "INTC", "SMCI", "UBER", "SHOP", "CRWD", "PANW", "SNOW",
        "BA", "DIS", "JPM", "BAC", "XOM", "CVX", "WMT", "COST", "LLY", "UNH",
        "SOFI", "MARA", "RIVN", "HOOD", "ARM", "DELL", "ORCL", "CRM", "ADBE", "QCOM",
    ])
    market_symbol: str = "SPY"


@dataclass
class MomentumConfig:
    """SPY intraday momentum ("noise area" breakout). See daytrader/momentum.py."""
    symbol: str = "SPY"
    lookback_days: int = 14            # past sessions used for the noise area and volatility
    band_mult: float = 1.0             # boundary = open +/- band_mult x avg move from open
    check_every_min: int = 30          # only decide at :00 and :30 (less noise, fewer trades)
    first_check_min: int = 30          # first decision at 10:00 ET
    last_entry_min: int = 360          # no new entries after 15:30 ET
    flatten_time: time = time(15, 55)  # close everything before the bell (ET)
    allow_shorts: bool = True          # the edge is symmetric; needs a margin account
    max_trades_per_day: int = 4        # caps whipsaw on choppy days

    # ---- Sizing: aim for a fixed daily volatility instead of a fixed share count ----
    target_daily_vol_pct: float = 2.0  # position = equity x min(max_leverage, 2% / SPY daily vol)
    max_leverage: float = 2.0          # the paper allowed 4x; 2x keeps drawdowns survivable
    daily_loss_limit_pct: float = 3.0  # stop trading for the day after -3%

    # ---- Backtest cost assumptions (SPY spread is usually $0.01) ----
    slippage_per_share: float = 0.01   # per side
    commission_per_share: float = 0.0
