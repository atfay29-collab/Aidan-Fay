from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import data
from .backtest import market_backtest, proxy_backtest
from .kalshi import BASE, DEMO_BASE, Kalshi
from .strategy import Params

DATA = Path("data")


def _params(a) -> Params:
    return Params(min_edge=a.edge, min_minutes_left=a.min_left, max_minutes_left=a.max_left,
                  min_price=a.min_price, max_price=a.max_price, contracts=a.contracts)


def _btc(a, start: int, end: int) -> list[data.Candle]:
    if getattr(a, "synthetic", False):
        return data.synthetic(days=a.days)
    path = Path(a.csv) if a.csv else DATA / f"btc_1m_{a.source}_{start}_{end}.csv"
    if path.exists():
        return data.load_csv(path)
    print(f"downloading {a.source} BTC-USD 1m candles -> {path}")
    if a.source == "massive":
        from .massive import fetch_massive
        candles = fetch_massive(start, end)
    else:
        candles = data.fetch_coinbase(start, end)
    data.save_csv(candles, path)
    return candles


def _range(days: float) -> tuple[int, int]:
    end = int(time.time()) // 900 * 900 - 900
    return end - int(days * 86400), end


def cmd_proxy(a) -> None:
    start, end = _range(a.days)
    candles = _btc(a, start - 3 * 3600, end)
    r = proxy_backtest(candles, a.halflife, a.vol_mult)
    print(f"\n{r.windows} windows, up-rate {r.up_rate:.3f}  (coin-flip Brier = 0.250)\n")
    print(" min_in  min_left   n      accuracy  brier   logloss")
    for m, s in r.by_minute.items():
        x = s.row()
        print(f"  {m:4d}    {15 - m:4d}   {x['n']:6d}   {x['accuracy']:.3f}    "
              f"{x['brier']:.4f}  {x['logloss']:.4f}")
    print("\ncalibration (predicted -> observed, n)")
    for p, o, n in r.calibration:
        print(f"  {p:.3f} -> {o:.3f}  ({n})")


def cmd_kalshi(a) -> None:
    from .history import load_history

    start, end = _range(a.days)
    client = Kalshi()
    markets, quotes = load_history(client, start, end, DATA / "kalshi_candles")
    if not markets:
        sys.exit("no settled KXBTC15M markets returned for that range")
    lo = min(m.open_ts for m in markets) - 3 * 3600
    hi = max(m.close_ts for m in markets) + 60
    candles = _btc(a, lo // 60 * 60, hi)
    params = _params(a)
    r = market_backtest(markets, quotes, candles, params, a.halflife, a.vol_mult)
    s = r.summary(params.contracts)
    print()
    for k, v in s.items():
        print(f"  {k:18s} {v:.4f}" if isinstance(v, float) else f"  {k:18s} {v}")
    verdict = "BEATS" if s["model_brier"] < s["market_brier"] else "does NOT beat"
    print(f"\nmodel {verdict} the Kalshi mid-price on Brier score.")
    if a.trades:
        for t in r.trades:
            print(f"  {t.ticker} {t.minutes_left:5.1f}m  {t.decision.side:3s} @{t.decision.price:2d}c "
                  f"p={t.decision.p_win:.2f} -> {t.result:3s} {t.pnl_cents / 100:+.2f}")


def cmd_live(a) -> None:
    from .live import run

    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if a.trade and not (key_id and key_path):
        sys.exit("--trade needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
    client = Kalshi(key_id, key_path, base=DEMO_BASE if a.demo else BASE)
    run(client, _params(a), trade=a.trade, poll=a.poll,
        max_daily_spend_cents=int(a.max_daily_spend * 100), halflife=a.halflife,
        vol_mult=a.vol_mult)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="btc15m", description="Kalshi KXBTC15M fair-value bot")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, strategy=True):
        sp.add_argument("--halflife", type=float, default=30.0, help="EWMA vol half-life, minutes")
        sp.add_argument("--vol-mult", type=float, default=1.0)
        if strategy:
            sp.add_argument("--edge", type=float, default=0.05, help="min EV/contract after fees, $")
            sp.add_argument("--min-left", type=float, default=2.0)
            sp.add_argument("--max-left", type=float, default=14.0)
            sp.add_argument("--min-price", type=int, default=10)
            sp.add_argument("--max-price", type=int, default=90)
            sp.add_argument("--contracts", type=int, default=10)

    sp = sub.add_parser("backtest-proxy", help="score predictions on BTC candles only")
    sp.add_argument("--days", type=float, default=30)
    sp.add_argument("--csv")
    sp.add_argument("--source", choices=["coinbase", "massive"], default="coinbase",
                    help="where to download BTC 1m candles (massive needs MASSIVE_API_KEY)")
    sp.add_argument("--synthetic", action="store_true", help="offline random-walk data")
    common(sp, strategy=False)
    sp.set_defaults(func=cmd_proxy)

    sp = sub.add_parser("backtest-kalshi", help="trade real settled Kalshi markets")
    sp.add_argument("--days", type=float, default=7)
    sp.add_argument("--csv")
    sp.add_argument("--source", choices=["coinbase", "massive"], default="coinbase")
    sp.add_argument("--trades", action="store_true", help="print every trade")
    common(sp)
    sp.set_defaults(func=cmd_kalshi)

    sp = sub.add_parser("live", help="run the bot (dry run unless --trade)")
    sp.add_argument("--trade", action="store_true", help="send real orders")
    sp.add_argument("--demo", action="store_true", help="use Kalshi's demo exchange")
    sp.add_argument("--poll", type=float, default=5.0)
    sp.add_argument("--max-daily-spend", type=float, default=50.0, help="dollars")
    common(sp)
    sp.set_defaults(func=cmd_live)

    a = p.parse_args(argv)
    a.func(a)
