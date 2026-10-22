"""Market-relative model: start from the Kalshi price and learn only its systematic errors.

Every scored minute becomes a row: logit(market mid) plus features the market
might under-react to (recent BTC returns, vol regime, distance to strike in
sigmas, spread, time left, time of day). A logistic regression fit on those rows
can only beat the market if some feature carries information the price misses.

Evaluation is walk-forward: fit on the earliest markets, score and trade on the
later ones it never saw.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .backtest import MarketRecord, Quote
from .data import Candle
from .model import ewma_vol_series, prob_up
from .strategy import Params, decide, settle_pnl_cents

FEATURES = ["logit_mid", "logit_model", "z_strike", "ret_1m", "ret_3m", "ret_5m", "ret_15m",
            "vol_ratio", "minutes_left", "spread", "hour_sin", "hour_cos"]


def _logit(p: float) -> float:
    p = min(max(p, 1e-3), 1 - 1e-3)
    return math.log(p / (1 - p))


@dataclass
class Row:
    ticker: str
    close_ts: int
    ts: int
    minutes_left: float
    yes_ask: int
    no_ask: int
    mid: float
    x: list[float]
    y: int


def build_rows(markets: list[MarketRecord], quotes: dict[str, list[Quote]],
               candles: list[Candle], vol_mult: float = 0.9) -> list[Row]:
    closes = [c.close for c in candles]
    sig = ewma_vol_series(closes, 30.0)
    sig_fast = ewma_vol_series(closes, 5.0)
    by_end = {c.ts + 60: i for i, c in enumerate(candles)}
    rows = []
    for mk in sorted(markets, key=lambda m: m.close_ts):
        y = int(mk.result == "yes")
        for q in sorted(quotes.get(mk.ticker, []), key=lambda q: q.ts):
            if q.yes_ask is None or q.no_ask is None or q.yes_ask + q.no_ask < 100:
                continue  # missing or crossed quote
            if not (mk.open_ts < q.ts < mk.close_ts) or q.ts not in by_end:
                continue
            i = by_end[q.ts]
            if i < 15:
                continue
            left = (mk.close_ts - q.ts) / 60
            s = closes[i]
            mid = (q.yes_ask + 100 - q.no_ask) / 200
            p = prob_up(s, mk.strike, sig[i], left, vol_mult)
            hour = (q.ts % 86400) / 3600
            x = [_logit(mid), _logit(p),
                 math.log(s / mk.strike) / (sig[i] * math.sqrt(left)),
                 *(math.log(s / closes[i - k]) / sig[i] for k in (1, 3, 5, 15)),
                 sig_fast[i] / sig[i], left, (q.yes_ask + q.no_ask - 100) / 100,
                 math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24)]
            rows.append(Row(mk.ticker, mk.close_ts, q.ts, left, q.yes_ask, q.no_ask, mid, x, y))
    return rows


@dataclass
class Logistic:
    l2: float = 1.0
    mean: np.ndarray = field(default=None)
    std: np.ndarray = field(default=None)
    w: np.ndarray = field(default=None)

    def _design(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self.mean) / self.std
        Z[:, 0] = X[:, 0]  # keep logit(mid) unscaled so w=[1,0,...] means "trust the market"
        return np.hstack([np.ones((len(X), 1)), Z])

    def fit(self, X: np.ndarray, y: np.ndarray, sw: np.ndarray | None = None,
            iters: int = 50) -> "Logistic":
        """sw: per-row weights; pass 1/rows-per-market so each market counts once."""
        sw = np.ones(len(y)) if sw is None else sw
        self.mean, self.std = X.mean(0), X.std(0) + 1e-9
        A = self._design(X)
        prior = np.zeros(A.shape[1])
        prior[1] = 1.0  # shrink toward the market price, not toward 0.5
        w = prior.copy()
        reg = self.l2 * np.eye(len(w))  # intercept too: an up/down bias is usually luck
        for _ in range(iters):
            p = 1 / (1 + np.exp(-A @ w))
            g = A.T @ (sw * (p - y)) + reg @ (w - prior)
            H = (A * (sw * p * (1 - p))[:, None]).T @ A + reg
            step = np.linalg.solve(H, g)
            w -= step
            if np.abs(step).max() < 1e-8:
                break
        self.w = w
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return 1 / (1 + np.exp(-self._design(X) @ self.w))


def _scores(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    q = np.clip(p, 1e-6, 1 - 1e-6)
    return float(np.mean((p - y) ** 2)), float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


@dataclass
class Research:
    train_markets: int
    test_markets: int
    test_rows: int
    market_brier: float
    market_logloss: float
    model_brier: float
    model_logloss: float
    weights: dict[str, float]
    trades: int
    win_rate: float
    pnl_dollars: float
    roi: float


def walk_forward(rows: list[Row], params: Params = Params(), train_frac: float = 0.7,
                 l2: float = 1.0) -> Research:
    tickers = sorted({(r.close_ts, r.ticker) for r in rows})
    cut = tickers[int(len(tickers) * train_frac)][0]
    train = [r for r in rows if r.close_ts < cut]
    test = [r for r in rows if r.close_ts >= cut]
    if not train or not test:
        raise ValueError("not enough markets for a train/test split")

    counts: dict[str, int] = {}
    for r in train:
        counts[r.ticker] = counts.get(r.ticker, 0) + 1
    m = Logistic(l2).fit(np.array([r.x for r in train]), np.array([r.y for r in train], float),
                         np.array([1.0 / counts[r.ticker] for r in train]))
    p_test = m.predict(np.array([r.x for r in test]))
    y_test = np.array([r.y for r in test], float)
    mkt_b, mkt_l = _scores(np.array([r.mid for r in test]), y_test)
    mod_b, mod_l = _scores(p_test, y_test)

    pnl = risked = wins = 0
    traded: set[str] = set()
    n = 0
    for r, p in zip(test, p_test):
        if r.ticker in traded:
            continue
        d = decide(float(p), r.yes_ask, r.no_ask, r.minutes_left, params)
        if d:
            traded.add(r.ticker)
            n += 1
            result = "yes" if r.y else "no"
            pnl += settle_pnl_cents(d, result, params.contracts)
            risked += d.price * params.contracts
            wins += result == d.side

    return Research(len({r.ticker for r in train}), len({r.ticker for r in test}), len(test),
                    mkt_b, mkt_l, mod_b, mod_l,
                    dict(zip(["intercept", *FEATURES], map(float, m.w))),
                    n, wins / n if n else 0.0, pnl / 100, pnl / risked if risked else 0.0)
