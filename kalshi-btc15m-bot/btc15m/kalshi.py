"""Minimal Kalshi Trade API v2 client: public market data plus signed order placement.

Prices are normalised to integer cents. Kalshi has been moving fields from
`yes_ask` (cents) to `yes_ask_dollars` (string dollars); both are accepted.
"""

from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
SERIES = "KXBTC15M"


def cents(obj: dict | None, key: str) -> int | None:
    """Read a price as cents from `key` or `key_dollars`; None/0-size quotes -> None."""
    if not obj:
        return None
    if obj.get(f"{key}_dollars") not in (None, ""):
        v = round(float(obj[f"{key}_dollars"]) * 100)
    elif obj.get(key) is not None:
        v = round(float(obj[key]))
    else:
        return None
    return v if 0 < v < 100 else None


def parse_ts(value: str | int | float) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class Kalshi:
    def __init__(self, key_id: str | None = None, private_key_path: str | None = None,
                 base: str = BASE, session: requests.Session | None = None):
        self.base = base.rstrip("/")
        self.s = session or requests.Session()
        self.key_id = key_id
        self._key = None
        if private_key_path:
            from cryptography.hazmat.primitives import serialization
            self._key = serialization.load_pem_private_key(
                Path(private_key_path).read_bytes(), password=None)

    # -- auth ---------------------------------------------------------------
    def sign_headers(self, method: str, path: str) -> dict[str, str]:
        """RSA-PSS(SHA256) signature over timestamp_ms + METHOD + path (no query string)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        if not (self.key_id and self._key):
            raise RuntimeError("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH to trade")
        ts = str(int(time.time() * 1000))
        full_path = "/trade-api/v2" + path.split("?")[0]
        sig = self._key.sign((ts + method.upper() + full_path).encode(),
                             padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                         salt_length=padding.PSS.DIGEST_LENGTH),
                             hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def _req(self, method: str, path: str, auth: bool = False, **kw) -> Any:
        headers = self.sign_headers(method, path) if auth else {}
        for attempt in range(5):
            r = self.s.request(method, self.base + path, headers=headers, timeout=15, **kw)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    # -- market data --------------------------------------------------------
    def markets(self, series: str = SERIES, **params) -> Iterator[dict]:
        cursor = None
        while True:
            q = {"series_ticker": series, "limit": 1000, **params}
            if cursor:
                q["cursor"] = cursor
            page = self._req("GET", "/markets", params=q)
            yield from page.get("markets", [])
            cursor = page.get("cursor")
            if not cursor:
                return

    def open_market(self, series: str = SERIES) -> dict | None:
        """The currently trading window: opened already, closes soonest."""
        now = time.time()
        live = [m for m in self.markets(series, status="open")
                if parse_ts(m["open_time"]) <= now < parse_ts(m["close_time"])]
        return min(live, key=lambda m: parse_ts(m["close_time"]), default=None)

    def orderbook_asks(self, ticker: str) -> tuple[int | None, int | None]:
        """Best (yes_ask, no_ask) in cents. The book lists bids only: yes ask = 100 - best no bid."""
        ob = self._req("GET", f"/markets/{ticker}/orderbook").get("orderbook") or {}

        def best_bid(side: str) -> int | None:
            if ob.get(f"{side}_dollars"):
                levels = [round(float(p) * 100) for p, _ in ob[f"{side}_dollars"]]
            else:
                levels = [int(p) for p, _ in (ob.get(side) or [])]
            return max(levels) if levels else None

        yb, nb = best_bid("yes"), best_bid("no")
        return (100 - nb if nb else None), (100 - yb if yb else None)

    def candlesticks(self, ticker: str, start_ts: int, end_ts: int,
                     series: str = SERIES) -> list[dict]:
        page = self._req("GET", f"/series/{series}/markets/{ticker}/candlesticks",
                         params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": 1})
        return page.get("candlesticks", [])

    # -- trading ------------------------------------------------------------
    def buy(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        """Immediate-or-cancel limit buy at `price_cents`; never rests on the book."""
        body = {"ticker": ticker, "action": "buy", "side": side, "count": count,
                "type": "limit", f"{side}_price": price_cents,
                "time_in_force": "immediate_or_cancel",
                "client_order_id": str(uuid.uuid4())}
        return self._req("POST", "/portfolio/orders", auth=True, json=body)

    def balance_cents(self) -> int:
        return int(self._req("GET", "/portfolio/balance", auth=True)["balance"])
