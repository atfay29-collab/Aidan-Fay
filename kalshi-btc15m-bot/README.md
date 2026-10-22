# Kalshi BTC 15-minute bot (KXBTC15M)

A fair-value trading bot and backtester for Kalshi's "BTC up or down in the
next 15 minutes" markets.

## Read this first: nothing predicts these markets "right every time"

Over 15 minutes, BTC moves close to a random walk. With 14 minutes left, even
a perfect volatility model calls the direction about 58% of the time (see the
table below). By the last minute it calls it about 92% of the time, but the
Kalshi price already shows that too, so you'd pay about 92¢ for a contract
that wins about 92% of the time. Anyone selling a bot that wins every time is
selling you a scam.

What *can* work is **pricing, not predicting**:

1. Estimate the fair probability that BTC closes at or above the strike.
2. Compare it with the Kalshi ask.
3. Buy only when the ask is cheaper than fair by more than fees plus a safety margin.

You still lose plenty of individual trades. The goal is to make money on
average, and the backtest tells you whether that actually happens against real
Kalshi prices.

## The model

```
P(up) = Φ( ln(spot / strike) / (σ · √minutes_left) )
```

- `spot`: Coinbase BTC-USD (a proxy: Kalshi settles on CF Benchmarks' BRTI,
  averaged over the final 60 seconds).
- `strike`: the market's `floor_strike`, the price at the window open.
- `σ`: per-minute volatility from an EWMA of 1-minute log returns (30-minute
  half-life by default).

Strategy (`btc15m/strategy.py`): buy YES or NO when
`p_win − (ask + taker_fee) ≥ min_edge` (default $0.05 per contract). It only
trades with 2–14 minutes left and at prices of 10–90¢, and takes at most one
entry per window. The fee is Kalshi's taker formula,
`ceil(0.07 · C · P · (1−P))`.

## Setup

```bash
cd kalshi-btc15m-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## Backtest 1: how predictable is the outcome? (BTC data only)

```bash
python -m btc15m backtest-proxy --days 30        # downloads Coinbase 1m candles to data/
python -m btc15m backtest-proxy --synthetic      # offline random walk, no network needed
```

This rebuilds every :00/:15/:30/:45 window and scores the model at each
minute. Output on 30 days of synthetic random-walk data:

| min left | accuracy | Brier (0.25 = coin flip) |
|---:|---:|---:|
| 14 | 0.584 | 0.242 |
| 10 | 0.710 | 0.193 |
| 5  | 0.812 | 0.132 |
| 1  | 0.919 | 0.056 |

The calibration table it prints shows whether "60%" really means 60%. If real
BTC data comes out over-confident, raise `--vol-mult`, say to 1.1–1.3, because
BTC has fat tails.

### Using Massive for BTC data

[Massive](https://massive.com) sells market data for stocks, options, indices,
futures, forex and crypto. Add `--source massive` to either backtest to pull
BTC minute bars (`X:BTCUSD`) from its crypto
[Custom Bars](https://massive.com/docs/rest/crypto/aggregates/custom-bars.md)
endpoint instead of Coinbase:

```bash
# sign up: https://massive.com/dashboard/signup   key: https://massive.com/dashboard/keys
export MASSIVE_API_KEY=...        # keep it out of code and out of git
python -m btc15m backtest-proxy --source massive --days 30
python -m btc15m backtest-kalshi --source massive --days 7
```

A 401/403 means the key is wrong or your plan doesn't include crypto minute
bars.

## Backtest 2: does it make money against real Kalshi prices?

```bash
python -m btc15m backtest-kalshi --days 7 --trades
python -m btc15m backtest-kalshi --days 14 --edge 0.08 --min-left 4
```

This pulls every settled KXBTC15M market in the range and its 1-minute
bid/ask history (public endpoints, no key needed, cached in
`data/kalshi_candles/`), then trades at the ask the bot would have seen, pays
fees, and settles on Kalshi's real result. It reports:

- trades, win rate, net P&L and ROI on money risked
- **model Brier vs market Brier**. If the Kalshi mid-price scores better than
  the model, the market is smarter than the model, and any P&L is probably
  luck.

Don't tune many parameters on one week and then trust the result. Tune on one
period, then check on a later one.

## Research: learning where the Kalshi price is wrong

The plain volatility model lost to the Kalshi price (Brier 0.180 vs 0.161 over
7 days). `research` takes the market-relative approach instead: it starts from
the Kalshi mid-price and fits a logistic regression on features the market
might under-react to:

- recent BTC returns over 1, 3, 5 and 15 minutes
- the volatility regime
- distance to the strike in sigmas
- spread, time left, time of day

Every market gets equal weight, and the fit is pulled toward "trust the
market", so with no real signal it just copies the price. It trains on the
earliest 70% of markets and reports scores and trades on the remaining 30%
only.

```bash
python -m btc15m research --days 30
```

The model has found something only if it beats the Kalshi price on log loss
on unseen markets, and it has to hold up again on a different date range. On
synthetic data the tests confirm it finds a planted mispricing and finds
nothing in a fair market.

### Recording order books

Kalshi only publishes 1-minute quote history. To get second-level order books
(depth, imbalance, how fast quotes catch up to spot), you have to record them
from now on:

```bash
python -m btc15m record          # leave running; writes data/recordings/YYYY-MM-DD.csv
```

## Live bot

```bash
python -m btc15m live                       # dry run: prints prices and what it would trade
python -m btc15m live --demo --trade        # Kalshi demo exchange (fake money)
python -m btc15m live --trade --contracts 5 --max-daily-spend 25
```

Trading needs an API key from Kalshi (Account → API Keys):

```bash
export KALSHI_API_KEY_ID=...
export KALSHI_PRIVATE_KEY_PATH=~/kalshi.pem   # never commit this
```

Orders are immediate-or-cancel limit buys at the quoted ask, so they never
rest on the book. Risk limits: one entry per window, fixed size, and a daily
cap on premium spent. Try `--demo` first. The order body follows Kalshi's v2
API; if Kalshi changes a field name, the API's error message will name it.

## Layout

| File | What it does |
|---|---|
| `btc15m/model.py` | EWMA vol and P(up) |
| `btc15m/strategy.py` | fees, entry rule, settlement P&L |
| `btc15m/kalshi.py` | API client (market data, RSA-PSS signed orders) |
| `btc15m/data.py` | Coinbase candles, CSV cache, synthetic data |
| `btc15m/history.py` | settled-market + quote downloader |
| `btc15m/backtest.py` | proxy and market backtests (no look-ahead) |
| `btc15m/live.py` | live loop |

Not financial advice. Only trade money you can afford to lose.
