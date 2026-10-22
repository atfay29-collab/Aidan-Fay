"""Log the open KXBTC15M window's order book and BTC spot every few seconds.

Kalshi only publishes 1-minute quote history, so second-level books (depth,
imbalance, how fast quotes catch up to spot) have to be recorded going forward.
One CSV row per poll, one file per UTC day under data/recordings/.
"""

from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

from .data import coinbase_spot
from .kalshi import Kalshi, parse_ts

FIELDS = ["ts", "ticker", "close_ts", "strike", "spot", "yes_bids", "no_bids"]


def _levels(ob: dict, side: str) -> str:
    """Top 5 bid levels as 'price:qty|...' in cents."""
    if ob.get(f"{side}_dollars"):
        lv = [(round(float(p) * 100), float(q)) for p, q in ob[f"{side}_dollars"]]
    else:
        lv = [(int(p), float(q)) for p, q in (ob.get(side) or [])]
    lv.sort(reverse=True)
    return "|".join(f"{p}:{q:g}" for p, q in lv[:5])


def run(client: Kalshi, out_dir: Path, poll: float = 2.0, log=print) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    market, market_until = None, 0.0
    n = 0
    while True:
        try:
            now = time.time()
            if market is None or now >= market_until:
                market = client.open_market()
                market_until = parse_ts(market["close_time"]) if market else now + 10
            if market and market.get("floor_strike") is not None:
                ob = client._req("GET", f"/markets/{market['ticker']}/orderbook").get("orderbook") or {}
                row = {"ts": round(now, 3), "ticker": market["ticker"],
                       "close_ts": parse_ts(market["close_time"]),
                       "strike": market["floor_strike"], "spot": coinbase_spot(),
                       "yes_bids": _levels(ob, "yes"), "no_bids": _levels(ob, "no")}
                path = out_dir / f"{datetime.now(timezone.utc):%Y-%m-%d}.csv"
                new = not path.exists()
                with path.open("a", newline="") as f:
                    w = csv.DictWriter(f, FIELDS)
                    if new:
                        w.writeheader()
                    w.writerow(row)
                n += 1
                if n % 150 == 0:
                    log(f"{n} rows recorded (latest {row['ticker']} spot {row['spot']:,.2f})")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"error: {e!r}")
            market = None
        time.sleep(poll)
