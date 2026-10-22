"""BTC 1-minute candles from Massive's crypto Custom Bars (OHLC) REST endpoint.

Docs: https://massive.com/docs/rest/crypto/aggregates/custom-bars.md
The key is read from MASSIVE_API_KEY (get one at https://massive.com/dashboard/keys).
"""

from __future__ import annotations

import os
import time

import requests

from .data import Candle

BASE = "https://api.massive.com"


def api_key() -> str:
    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        raise RuntimeError("Set MASSIVE_API_KEY (sign up at https://massive.com/dashboard/signup, "
                           "key at https://massive.com/dashboard/keys)")
    return key


def fetch_massive(start_ts: int, end_ts: int, ticker: str = "X:BTCUSD",
                  session: requests.Session | None = None) -> list[Candle]:
    """1m candles in [start_ts, end_ts), UTC. Follows `next_url` pagination."""
    s = session or requests.Session()
    s.headers["Authorization"] = f"Bearer {api_key()}"
    url = f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{start_ts * 1000}/{(end_ts - 1) * 1000}"
    params: dict | None = {"adjusted": "true", "sort": "asc", "limit": 50000}
    out: dict[int, Candle] = {}
    while url:
        for attempt in range(5):
            r = s.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + 2 * attempt)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"Massive rejected the key or plan ({r.status_code}): {r.text[:200]}")
            r.raise_for_status()
            break
        body = r.json()
        for b in body.get("results") or []:
            ts = int(b["t"]) // 1000
            if start_ts <= ts < end_ts:
                out[ts] = Candle(ts, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]))
        url, params = body.get("next_url"), None  # next_url already carries the query
    return [out[k] for k in sorted(out)]
