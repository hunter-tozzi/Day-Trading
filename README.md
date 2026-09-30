# Alpaca Day Trader: Opening Range Breakout

A rules-based intraday bot for [Alpaca](https://alpaca.markets). It scans for active stocks, trades breakouts of the first 15 minutes' range, and manages risk strictly. It also includes a **backtester that runs the exact same code**, so you can check whether the rules would have made money before you trust them with anything.

> **Please read this first.** No strategy is guaranteed to make money, this one included. Most retail day traders lose money. This bot is built to **lose small and survive** (fixed risk per trade, a daily loss limit, stops held at the broker, everything closed before the bell). Whether it has an edge in the current market is something **you** have to verify: backtest it, then paper trade it for several weeks. Keep `ALPACA_PAPER=true` until both look good.

## How it works

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

### Why bots like your Grok one tend to bleed money
The usual reasons are: trading all day in chop, no market filter, stops that are too tight or missing, sizing that ignores volatility, chasing moves that already happened, too many trades (spread and slippage add up), and never testing on historical data. Each rule above addresses one of these.

## Setup

```bash
cd alpaca-daytrader
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste your PAPER API key + secret into .env
```

Get paper keys at app.alpaca.markets → switch to **Paper Trading** → *API Keys*.

## 1. Backtest first

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

## 2. Paper trade

```bash
python -m daytrader.bot            # runs every day; sleeps while the market is closed
python -m daytrader.bot --once     # one session, then exit
```

Start it before 9:45 ET and leave it running. Logs go to `logs/bot.log`. Paper trade for **at least 4–6 weeks** (roughly 50+ trades) and compare the results with the backtest. Paper fills are optimistic, so expect real trading to be somewhat worse.

## Tuning (in `config.py`)
Change **one thing at a time**, then re-run the backtest over several periods. Tuning until one backtest looks great is how you overfit.
- `min_rvol` higher → fewer, more selective trades.
- `entry_cutoff` earlier (e.g. 10:30) → only the strongest part of the morning.
- `reward_risk` 1.5 vs 2 vs 3 → trades win rate against payoff.
- `min_gap_pct` = 2 → only trade stocks that gapped (news-driven movers).
- `allow_shorts` / `--shorts` → trade breakdowns too (requires margin and borrowable shares).

## Project layout
```
daytrader/
  config.py      all settings
  strategy.py    scanner scoring, entry rules, position sizing (pure logic, shared)
  indicators.py  VWAP, ATR
  broker.py      Alpaca data + bracket orders
  bot.py         live/paper loop
  backtest.py    historical simulation + report
tests/           unit tests (pytest), no API keys needed
```

*This is educational software, not financial advice. You are responsible for any trades it places.*
