"""Download settled KXBTC15M markets and their 1-minute quotes, cached as JSON."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .backtest import MarketRecord, Quote
from .kalshi import SERIES, Kalshi, cents, parse_ts


def to_record(m: dict) -> MarketRecord | None:
    if m.get("floor_strike") is None or m.get("result") not in ("yes", "no"):
        return None
    return MarketRecord(m["ticker"], parse_ts(m["open_time"]), parse_ts(m["close_time"]),
                        float(m["floor_strike"]), m["result"])


def to_quotes(candles: list[dict]) -> list[Quote]:
    out = []
    for c in candles:
        yes_bid = cents(c.get("yes_bid"), "close")
        out.append(Quote(int(c["end_period_ts"]), cents(c.get("yes_ask"), "close"),
                         100 - yes_bid if yes_bid else None))
    return out


def load_history(client: Kalshi, start_ts: int, end_ts: int, cache_dir: Path,
                 series: str = SERIES, log=print) -> tuple[list[MarketRecord], dict[str, list[Quote]]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw = list(client.markets(series, status="settled", min_close_ts=start_ts, max_close_ts=end_ts))
    records = [r for r in map(to_record, raw) if r]
    log(f"{len(raw)} settled markets, {len(records)} usable")

    quotes: dict[str, list[Quote]] = {}
    for n, r in enumerate(records, 1):
        path = cache_dir / f"{r.ticker}.json"
        if path.exists():
            candles = json.loads(path.read_text())
        else:
            candles = client.candlesticks(r.ticker, r.open_ts, r.close_ts, series)
            path.write_text(json.dumps(candles))
            time.sleep(0.06)
        quotes[r.ticker] = to_quotes(candles)
        if n % 100 == 0:
            log(f"  quotes for {n}/{len(records)} markets")
    return records, quotes
