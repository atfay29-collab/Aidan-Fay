"""BTC 1-minute candles: Coinbase download, CSV cache, and a synthetic generator.

A candle's ts is its *start* (UTC seconds); its close is the price at ts + 60.
"""

from __future__ import annotations

import csv
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

COINBASE = "https://api.exchange.coinbase.com"


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_coinbase(start_ts: int, end_ts: int, product: str = "BTC-USD",
                   session: requests.Session | None = None) -> list[Candle]:
    """1m candles in [start_ts, end_ts). Coinbase returns at most 300 per call."""
    s = session or requests.Session()
    out: dict[int, Candle] = {}
    t = start_ts - start_ts % 60
    while t < end_ts:
        chunk_end = min(t + 300 * 60, end_ts)
        for attempt in range(5):
            r = s.get(f"{COINBASE}/products/{product}/candles",
                      params={"granularity": 60, "start": _iso(t), "end": _iso(chunk_end - 60)},
                      timeout=15)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            break
        for ts, low, high, open_, close, _vol in r.json():
            if start_ts <= ts < end_ts:
                out[int(ts)] = Candle(int(ts), float(open_), float(high), float(low), float(close))
        t = chunk_end
        time.sleep(0.12)
    return [out[k] for k in sorted(out)]


def coinbase_spot(product: str = "BTC-USD", session: requests.Session | None = None) -> float:
    r = (session or requests).get(f"{COINBASE}/products/{product}/ticker", timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def save_csv(candles: list[Candle], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close"])
        for c in candles:
            w.writerow([c.ts, c.open, c.high, c.low, c.close])


def load_csv(path: Path) -> list[Candle]:
    with path.open() as f:
        return [Candle(int(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]),
                       float(r["close"])) for r in csv.DictReader(f)]


def synthetic(days: float = 30, seed: int = 7, start_price: float = 100_000.0,
              start_ts: int = 1_760_000_400) -> list[Candle]:
    """Driftless random walk with regime-switching vol (no edge exists by construction)."""
    rng = random.Random(seed)
    n = int(days * 1440)
    sigma, price, out = 0.0008, start_price, []
    for i in range(n):
        if rng.random() < 0.002:
            sigma = rng.choice([0.0004, 0.0008, 0.0015])
        o = price
        price = o * math.exp(rng.gauss(0.0, sigma))
        out.append(Candle(start_ts + 60 * i, o, max(o, price), min(o, price), price))
    return out
