# Alpaca Day Trader

Two rules-based intraday strategies for [Alpaca](https://alpaca.markets), each with a **backtester that runs the exact same code** as the live bot, so you can check whether the rules would have made money before you trust them with anything.

| Strategy | Trades | Start with |
|---|---|---|
| **[SPY intraday momentum](#strategy-1-spy-intraday-momentum-recommended)** (recommended) | SPY only, 0–2 trades/day, long and short | `python -m daytrader.momentum_backtest` |
| [Opening range breakout](#strategy-2-opening-range-breakout) | top 10 "stocks in play", long | `python -m daytrader.backtest` |

> **Please read this first.** No strategy is guaranteed to make money, these included. Most retail day traders lose money. Both bots are built to **lose small and survive** (stops held at the broker, volatility-based sizing, a daily loss limit, everything closed before the bell). Whether a strategy has an edge in the current market is something **you** have to verify: backtest it over several years, then paper trade it for several weeks. Keep `ALPACA_PAPER=true` until both look good.

## Setup

```bash
cd Day-Trading
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste your PAPER API key + secret into .env
```

Get paper keys at app.alpaca.markets → switch to **Paper Trading** → *API Keys*.

## Strategy 1: SPY intraday momentum (recommended)

Based on a published, long-history study: Zarattini, Aziz & Barbon (2024), *"Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)"*. The authors report roughly 19–20% a year after costs from 2007 to early 2024 (Sharpe ~1.3) with up to 4x leverage. Treat that as a claim to verify, not a promise: it was measured on the past, and edges often shrink once they are published.

**The idea.** Most of the day, SPY wanders inside a band around its open: noise. How wide that band normally is at 10:00, 10:30, and so on can be measured from the last 14 sessions. When SPY leaves the band, buyers or sellers are in control, and intraday moves like that tend to run into the close. So the bot follows the move and exits quickly if it fails.

| Step | Rule |
|---|---|
| Noise band | For each minute of the day: the average \|move from the open\| over the last 14 sessions. Upper band = max(open, yesterday's close) × (1 + that), lower band = min(open, yesterday's close) × (1 − that). |
| Decide | Only at 10:00, 10:30, … 15:30 ET. **Long** if SPY is above the upper band (and VWAP); **short** if below the lower band (and VWAP). |
| Stop | Long: max(upper band, VWAP). Short: min(lower band, VWAP). Moved every 30 min and **held at Alpaca** in between, so it works even if the bot dies. |
| Size | Aims for ~2% daily volatility of equity: equity × min(2, 2% ÷ SPY's recent daily volatility). Calm market → bigger position, wild market → smaller. |
| Exit | 15:55 ET, everything closed. At most 4 trades a day; trading stops for the day at −3%. |

**What to expect.** Most trades are small losers (expect a win rate well under 50%); the profit comes from a handful of big trend days, often in volatile markets. Choppy, calm months are usually flat to slightly negative. That is the shape of the strategy, not a malfunction, but it means you need months, not days, to judge it.

**Why it's a better fit than a typical AI trading bot:**
- One of the most liquid instruments in the world: a $0.01 spread, reliable fills, no scanner, no penny stocks, no borrow problems on shorts.
- Very few parameters (14-day lookback, 30-min checks), so there's little to overfit.
- Makes money when the market *trends* in either direction, including selloffs.
- Few trades: costs stay small.

### Run it

```bash
python -m daytrader.momentum_backtest --years 5          # SIP minute data from Alpaca (free plan OK)
python -m daytrader.momentum_backtest --years 9          # data goes back to 2016: use it
python -m daytrader.momentum_backtest --long-only        # if your account can't short
python -m daytrader.momentum_backtest --symbol QQQ       # same rules on the Nasdaq-100
python -m daytrader.momentum_bot                         # paper trading; start before 10:00 ET
```

The backtest prints total return, CAGR, Sharpe, max drawdown and a **year-by-year table against buy & hold**. Before paper trading, look for: profit factor > 1.1, Sharpe > 0.8, and **most years positive**, not one lucky year carrying the rest. Logs from the bot go to `logs/momentum.log`.

**Account requirements:** at least **$25,000** equity. The strategy trades most days, and the Pattern Day Trader rule caps accounts under $25k at 3 day trades per 5 business days (the bot detects this and stops entering). Shorting needs a margin account; otherwise use `--long-only`.

**Honest limitations:**
- The backtest fills at the next minute's open plus $0.01/share and fills stops at the worse of the stop and the bar's open. Real fills are usually close for SPY, but not identical.
- The paper checked stops only every 30 minutes; this bot keeps a live stop at Alpaca (safer, slightly more whipsaw). The backtest models what the bot actually does.
- On a pure random walk the backtest breaks even before costs and loses a little after (see `tests/test_momentum.py`). That's the check that the backtester isn't peeking at future prices.

## Strategy 2: Opening range breakout

### How it works

| Step | Time (ET) | What happens |
|---|---|---|
| Opening range | 9:30–9:45 | The bot records each stock's high, low and volume. |
| **Scan** | 9:45 | Candidates = Alpaca's most-active stocks plus a list of liquid large caps. It keeps stocks priced $10–$500 whose daily ATR is ≥ 1.5% of price (they actually move) and whose opening volume is ≥ 1.5× normal (something is happening today). It watches the top 10 by relative volume. |
| **Entry** | 9:45–11:30 | **Buy** when a 1-minute bar closes above the range high, above the stock's VWAP, **and** SPY is above its own VWAP (don't fight the market). |
| **Stop / target** | at entry | Stop = range low. Target = 2 × risk. Both are sent as a **bracket order**, so Alpaca holds them server-side and they still fire if the bot crashes. |
| **Filters** | at entry | It skips a trade if the stop would be more than 0.5 × daily ATR away (too wide, or the price has already run too far, i.e. chasing) or less than 0.15% away (noise). |
| **Exit** | 15:50 | Everything still open is closed. No overnight positions. |

**Risk controls:** 0.5% of equity at risk per trade, at most 20% of equity in one position, at most 3 positions open at once, at most 4 trades per day, and a daily stop at −2%. Each symbol is traded at most once per day. It also detects the Pattern Day Trader limit on accounts under $25k.

All of these numbers are in [`daytrader/config.py`](daytrader/config.py).

#### Why bots like your Grok one tend to bleed money
The usual reasons are: trading all day in chop, no market filter, stops that are too tight or missing, sizing that ignores volatility, chasing moves that already happened, too many trades (spread and slippage add up), and never testing on historical data. Each rule above addresses one of these.

### 1. Backtest first

```bash
python -m daytrader.backtest --days 90                 # uses your feed (iex by default)
python -m daytrader.backtest --days 180 --feed sip     # fuller data; free plan allows SIP history
python -m daytrader.backtest --days 90 --shorts --csv trades.csv
```

Example output:
```
Trades             : 212  (1.9/day)
Win rate           : 41.0%
Avg R per trade    : +0.12R
Profit factor      : 1.24
Total P&L          : ...
Max drawdown       : ...
SPY buy & hold     : ...
```
(These numbers only show the format. Run it yourself.)

**How to read it:** with a 2R target, a win rate around 35–45% can still be profitable. What matters is **Avg R > 0** and **profit factor > ~1.2** across *several different periods*. Try `--days 60`, `120` and `250`. If it only works in one window, it doesn't work. Compare the result with SPY buy & hold: if the bot can't beat simply holding the index, the extra risk isn't worth it.

Honest limitations of the backtest:
- Fills are simulated at the next bar's open plus 0.03% slippage. Real fills in fast breakouts can be worse.
- The fixed universe is a list of stocks chosen today (survivorship bias). The live bot also scans most-actives, which the backtest can't replay.
- If a bar touches both the stop and the target, the backtest assumes the stop was hit. That is deliberately pessimistic.
- A sanity check was run on pure random-walk prices. The strategy lost slightly there (about the cost of trading), which is what an honest backtester should show. If you edit the code and random data starts showing profits, you've introduced look-ahead bias.

### 2. Paper trade

```bash
python -m daytrader.bot            # runs every day; sleeps while the market is closed
python -m daytrader.bot --once     # one session, then exit
```

Start it before 9:45 ET and leave it running. Logs go to `logs/bot.log`. Paper trade for **at least 4–6 weeks** (roughly 50+ trades) and compare the results with the backtest. Paper fills are optimistic, so expect real trading to be somewhat worse.

### Tuning (in `config.py`)
Change **one thing at a time**, then re-run the backtest over several periods. Tuning until one backtest looks great is how you overfit.
- `min_rvol` higher → fewer, more selective trades.
- `entry_cutoff` earlier (e.g. 10:30) → only the strongest part of the morning.
- `reward_risk` 1.5 vs 2 vs 3 → trades win rate against payoff.
- `min_gap_pct` = 2 → only trade stocks that gapped (news-driven movers).
- `allow_shorts` / `--shorts` → trade breakdowns too (requires margin and borrowable shares).

## Project layout
```
daytrader/
  config.py             all settings (Config = ORB, MomentumConfig = SPY momentum)
  momentum.py           SPY momentum rules: noise band, decisions, sizing (pure logic, shared)
  momentum_bot.py       SPY momentum live/paper loop
  momentum_backtest.py  SPY momentum historical simulation + report
  strategy.py           ORB scanner scoring, entry rules, position sizing (pure logic, shared)
  bot.py                ORB live/paper loop
  backtest.py           ORB historical simulation + report
  indicators.py         VWAP, ATR
  broker.py             Alpaca data + orders
tests/           unit tests (pytest), no API keys needed
```

*This is educational software, not financial advice. You are responsible for any trades it places.*
