"""Fair-value model for "will BTC be at or above the strike at close?".

BTC log returns over minutes are close to driftless, so the probability of
finishing at or above the strike K with m minutes left is

    P(up) = Phi( ln(S / K) / (sigma * sqrt(m)) )

where sigma is per-minute volatility. sigma comes from an EWMA of recent
1-minute log returns. That is the whole model: the edge, if there is one,
comes from Kalshi quotes lagging spot, not from forecasting direction.
"""

from __future__ import annotations

import math
from typing import Sequence

MIN_SIGMA = 1e-5  # per-minute floor so a dead-quiet hour can't produce p = 0 or 1
P_CLAMP = 1e-4


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ewma_vol_series(closes: Sequence[float], halflife: float = 30.0) -> list[float]:
    """Causal per-minute EWMA volatility. out[i] uses returns up to closes[i] only."""
    lam = 0.5 ** (1.0 / halflife)
    out = [MIN_SIGMA] * len(closes)
    var = None
    for i in range(1, len(closes)):
        r = math.log(closes[i] / closes[i - 1])
        var = r * r if var is None else lam * var + (1.0 - lam) * r * r
        out[i] = max(math.sqrt(var), MIN_SIGMA)
    return out


def ewma_vol(closes: Sequence[float], halflife: float = 30.0) -> float:
    return ewma_vol_series(closes, halflife)[-1] if len(closes) > 1 else MIN_SIGMA


def prob_up(spot: float, strike: float, sigma_per_min: float, minutes_left: float,
            vol_mult: float = 1.0) -> float:
    if minutes_left <= 0:
        return 1.0 if spot >= strike else 0.0
    s = max(sigma_per_min, MIN_SIGMA) * vol_mult * math.sqrt(minutes_left)
    p = norm_cdf(math.log(spot / strike) / s)
    return min(max(p, P_CLAMP), 1.0 - P_CLAMP)
