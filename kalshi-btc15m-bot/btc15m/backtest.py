"""Two backtests.

1. `proxy_backtest` - BTC candles only. Rebuilds every 15-minute window
   (:00/:15/:30/:45 UTC, strike = price at the window open) and scores the
   model's probability at each minute: accuracy, Brier, log loss, calibration.
   This says how predictable the outcome is, not whether you can make money.

2. `market_backtest` - BTC candles + real settled Kalshi KXBTC15M markets and
   their 1-minute quote history. Trades against the ask the strategy would
   actually have seen, pays taker fees, settles on Kalshi's real result, and
   compares model Brier with the market's own mid-price Brier.

Both are look-ahead free: at time t they use only the BTC close at t and
quotes from the candle ending at t.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .data import Candle
from .model import ewma_vol_series, prob_up
from .strategy import Decision, Params, decide, settle_pnl_cents

WINDOW = 15


def _logloss(p: float, y: int) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -math.log(p if y else 1 - p)


@dataclass
class Score:
    n: int = 0
    correct: int = 0
    brier: float = 0.0
    logloss: float = 0.0

    def add(self, p: float, y: int) -> None:
        self.n += 1
        self.correct += int((p >= 0.5) == bool(y))
        self.brier += (p - y) ** 2
        self.logloss += _logloss(p, y)

    def row(self) -> dict:
        n = max(self.n, 1)
        return {"n": self.n, "accuracy": self.correct / n, "brier": self.brier / n,
                "logloss": self.logloss / n}


@dataclass
class ProxyResult:
    by_minute: dict[int, Score]
    calibration: list[tuple[float, float, int]]  # (mean predicted, observed freq, count)
    up_rate: float
    windows: int


def proxy_backtest(candles: list[Candle], halflife: float = 30.0, vol_mult: float = 1.0,
                   warmup: int = 120, bins: int = 10) -> ProxyResult:
    closes = [c.close for c in candles]
    sig = ewma_vol_series(closes, halflife)
    idx = {c.ts: i for i, c in enumerate(candles)}
    by_minute = {m: Score() for m in range(1, WINDOW)}
    cal = [[0.0, 0, 0] for _ in range(bins)]
    ups = windows = 0

    for c in candles:
        if c.ts % (WINDOW * 60):
            continue
        i0 = idx[c.ts]
        if i0 < warmup or i0 + WINDOW > len(candles):
            continue
        if candles[i0 + WINDOW - 1].ts != c.ts + (WINDOW - 1) * 60:
            continue  # gap in data
        strike = c.open
        y = int(candles[i0 + WINDOW - 1].close >= strike)
        ups += y
        windows += 1
        for m in range(1, WINDOW):
            j = i0 + m - 1  # candle whose close is the price m minutes into the window
            p = prob_up(closes[j], strike, sig[j], WINDOW - m, vol_mult)
            by_minute[m].add(p, y)
            b = min(int(p * bins), bins - 1)
            cal[b][0] += p
            cal[b][1] += y
            cal[b][2] += 1

    calibration = [(s / n, k / n, n) for s, k, n in cal if n]
    return ProxyResult(by_minute, calibration, ups / max(windows, 1), windows)


# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketRecord:
    ticker: str
    open_ts: int
    close_ts: int
    strike: float
    result: str  # "yes" / "no"


@dataclass(frozen=True)
class Quote:
    ts: int  # end of the 1-minute candle
    yes_ask: int | None
    no_ask: int | None


@dataclass
class Trade:
    ticker: str
    ts: int
    minutes_left: float
    decision: Decision
    result: str
    pnl_cents: int


@dataclass
class MarketResult:
    trades: list[Trade] = field(default_factory=list)
    markets: int = 0
    model_score: Score = field(default_factory=Score)
    market_score: Score = field(default_factory=Score)

    def summary(self, contracts: int) -> dict:
        pnl = sum(t.pnl_cents for t in self.trades)
        risked = sum(t.decision.price * contracts for t in self.trades)
        wins = sum(t.result == t.decision.side for t in self.trades)
        n = len(self.trades)
        return {"markets": self.markets, "trades": n, "win_rate": wins / n if n else 0.0,
                "pnl_dollars": pnl / 100, "roi_on_risked": pnl / risked if risked else 0.0,
                "avg_edge_expected": sum(t.decision.edge for t in self.trades) / n if n else 0.0,
                "model_brier": self.model_score.row()["brier"],
                "market_brier": self.market_score.row()["brier"],
                "scored_points": self.model_score.n}


def market_backtest(markets: list[MarketRecord], quotes: dict[str, list[Quote]],
                    candles: list[Candle], params: Params = Params(),
                    halflife: float = 30.0, vol_mult: float = 1.0) -> MarketResult:
    closes = [c.close for c in candles]
    sig = ewma_vol_series(closes, halflife)
    by_end = {c.ts + 60: i for i, c in enumerate(candles)}  # close-time -> index
    res = MarketResult()

    for mk in sorted(markets, key=lambda m: m.close_ts):
        qs = sorted(quotes.get(mk.ticker, []), key=lambda q: q.ts)
        if not qs or mk.result not in ("yes", "no"):
            continue
        res.markets += 1
        y = int(mk.result == "yes")
        traded = False
        for q in qs:
            if not (mk.open_ts < q.ts < mk.close_ts) or q.ts not in by_end:
                continue
            i = by_end[q.ts]
            left = (mk.close_ts - q.ts) / 60
            p = prob_up(closes[i], mk.strike, sig[i], left, vol_mult)
            if q.yes_ask is not None and q.no_ask is not None:
                res.model_score.add(p, y)
                res.market_score.add((q.yes_ask + (100 - q.no_ask)) / 200, y)
            if traded:
                continue
            d = decide(p, q.yes_ask, q.no_ask, left, params)
            if d:
                traded = True
                res.trades.append(Trade(mk.ticker, q.ts, left, d, mk.result,
                                        settle_pnl_cents(d, mk.result, params.contracts)))
    return res
