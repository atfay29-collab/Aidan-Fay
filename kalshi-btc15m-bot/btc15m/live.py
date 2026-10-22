"""Live loop: price the open KXBTC15M window every few seconds and trade real edges.

Dry run unless `trade=True`. Risk limits: one entry per window, fixed size,
and a daily cap on premium spent.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .data import coinbase_spot, fetch_coinbase
from .kalshi import Kalshi, parse_ts
from .model import ewma_vol, prob_up
from .strategy import Params, decide, taker_fee_cents


def run(client: Kalshi, params: Params, trade: bool = False, poll: float = 5.0,
        max_daily_spend_cents: int = 5_000, halflife: float = 30.0, vol_mult: float = 1.0,
        log=print) -> None:
    traded: set[str] = set()
    spent_day, spent = None, 0
    vol, vol_at = None, 0.0

    while True:
        try:
            now = time.time()
            day = datetime.now(timezone.utc).date()
            if day != spent_day:
                spent_day, spent = day, 0
            if vol is None or now - vol_at > 60:
                bars = fetch_coinbase(int(now) - 180 * 60, int(now))
                vol, vol_at = ewma_vol([b.close for b in bars], halflife), now

            m = client.open_market()
            if not m or m.get("floor_strike") is None:
                log("no open window with a strike yet")
                time.sleep(poll)
                continue
            ticker, strike = m["ticker"], float(m["floor_strike"])
            left = (parse_ts(m["close_time"]) - now) / 60
            spot = coinbase_spot()
            p = prob_up(spot, strike, vol, left, vol_mult)
            yes_ask, no_ask = client.orderbook_asks(ticker)
            d = decide(p, yes_ask, no_ask, left, params)
            log(f"{ticker} {left:5.2f}m left  spot {spot:,.2f}  strike {strike:,.2f}  "
                f"P(up) {p:.3f}  yes_ask {yes_ask}  no_ask {no_ask}  "
                + (f"-> BUY {d.side.upper()} @{d.price}c edge {d.edge:+.3f}" if d else "-> pass"))

            if d and ticker not in traded:
                cost = d.price * params.contracts + taker_fee_cents(d.price, params.contracts)
                if spent + cost > max_daily_spend_cents:
                    log(f"daily spend cap reached ({spent}c); not trading")
                elif trade:
                    resp = client.buy(ticker, d.side, params.contracts, d.price)
                    log(f"ORDER {resp.get('order', resp)}")
                    traded.add(ticker)
                    spent += cost
                else:
                    log("(dry run - pass --trade to send orders)")
                    traded.add(ticker)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # keep the loop alive through API hiccups
            log(f"error: {e!r}")
        time.sleep(poll)
