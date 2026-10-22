import math
import random

from btc15m.backtest import MarketRecord, Quote
from btc15m.data import synthetic
from btc15m.learn import build_rows, walk_forward
from btc15m.model import ewma_vol_series, prob_up
from btc15m.strategy import Params


def _markets(candles, bias_fn, seed=0):
    """Kalshi-like quotes = fair price distorted by bias_fn(ret_5m_in_sigmas)."""
    rng = random.Random(seed)
    closes = {c.ts + 60: c.close for c in candles}
    cl = [c.close for c in candles]
    sig = ewma_vol_series(cl)
    idx = {c.ts + 60: i for i, c in enumerate(candles)}
    markets, quotes = [], {}
    for c in candles[300:-20]:
        if c.ts % 900:
            continue
        close_ts = c.ts + 900
        mk = MarketRecord(f"T{c.ts}", c.ts, close_ts, c.open,
                          "yes" if closes[close_ts] >= c.open else "no")
        qs = []
        for t in range(c.ts + 60, close_ts, 60):
            i = idx[t]
            fair = prob_up(cl[i], c.open, sig[i], (close_ts - t) / 60)
            r5 = math.log(cl[i] / cl[i - 5]) / sig[i]
            mid = min(max(round(100 * (fair + bias_fn(r5)) + rng.choice([-1, 0, 1])), 3), 97)
            qs.append(Quote(t, min(mid + 1, 99), min(101 - mid, 99)))
        markets.append(mk)
        quotes[mk.ticker] = qs
    return markets, quotes


def test_no_edge_when_market_is_fair():
    candles = synthetic(days=20, seed=5)
    m, q = _markets(candles, lambda r5: 0.0)
    r = walk_forward(build_rows(m, q, candles, vol_mult=1.0), Params(min_edge=0.05), l2=10)
    assert r.model_logloss > r.market_logloss - 0.002  # claims no real edge
    assert abs(r.weights["ret_5m"]) < 0.05


def test_learns_planted_momentum_underreaction():
    candles = synthetic(days=20, seed=6)
    # market under-reacts to the last 5 minutes: shades price against recent moves
    m, q = _markets(candles, lambda r5: -0.04 * max(min(r5, 3), -3))
    r = walk_forward(build_rows(m, q, candles, vol_mult=1.0), Params(min_edge=0.03), l2=10)
    assert r.model_logloss < r.market_logloss
    assert r.weights["ret_5m"] > 0
    assert r.pnl_dollars > 0
