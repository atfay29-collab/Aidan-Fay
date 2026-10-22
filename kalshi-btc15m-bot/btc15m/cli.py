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


def cmd_research(a) -> None:
    from .history import load_history
    from .learn import build_rows, walk_forward

    start, end = _range(a.days)
    markets, quotes = load_history(Kalshi(), start, end, DATA / "kalshi_candles")
    if not markets:
        sys.exit("no settled KXBTC15M markets returned for that range")
    lo = min(m.open_ts for m in markets) - 3 * 3600
    hi = max(m.close_ts for m in markets) + 60
    candles = _btc(a, lo // 60 * 60, hi)
    rows = build_rows(markets, quotes, candles, a.vol_mult)
    r = walk_forward(rows, _params(a), a.train_frac, a.l2)
    print(f"\ntrain {r.train_markets} markets, test {r.test_markets} markets ({r.test_rows} scored minutes)")
    print(f"\n                 Brier    log loss   (test set only; lower is better)")
    print(f"  Kalshi price   {r.market_brier:.4f}   {r.market_logloss:.4f}")
    print(f"  model          {r.model_brier:.4f}   {r.model_logloss:.4f}")
    verdict = "BEATS" if r.model_logloss < r.market_logloss else "does NOT beat"
    print(f"\nmodel {verdict} the Kalshi price on unseen markets.")
    print("\nweights (logit_mid near 1 and the rest near 0 means the market already knows it):")
    for k, v in r.weights.items():
        print(f"  {k:14s} {v:+.4f}")
    print(f"\ntest trades {r.trades}  win rate {r.win_rate:.3f}  "
          f"P&L ${r.pnl_dollars:+.2f}  ROI {r.roi:+.4f}")


def cmd_record(a) -> None:
    from .record import run

    print("recording to data/recordings/ - leave running; Ctrl-C to stop")
    run(Kalshi(), DATA / "recordings", poll=a.poll)


def cmd_weather(a) -> None:
    from datetime import datetime, timezone

    from .weather import CITIES, backtest_city, fetch_forecast_highs, load_city

    end = int(time.time())
    start = end - int(a.days * 86400)
    client = Kalshi()
    params = _params(a)
    totals = {"trades": 0, "pnl": 0.0, "model": 0.0, "market": 0.0, "n": 0}
    for name in a.cities.split(","):
        city = CITIES[name.strip().upper()]
        brackets, quotes, rules = load_city(client, city, start, end, a.decision_hour,
                                            DATA / "kalshi_weather")
        if not brackets:
            print(f"  no settled markets for {city.series}; skipping\n")
            continue
        days = sorted({b.day for b in brackets})
        forecasts = fetch_forecast_highs(city, days[0], days[-1], a.model)
        r = backtest_city(name, brackets, quotes, forecasts, params, a.window)
        s = r.summary(params.contracts)
        print(f"\n== {name}  {city.series}  assumed station: {city.station}")
        if rules:
            print(f"   Kalshi rules: {rules[:220]}")
        for k, v in s.items():
            print(f"   {k:16s} {v:.4f}" if isinstance(v, float) else f"   {k:16s} {v}")
        if a.trades:
            for t in r.trades:
                print(f"     {t.day} {t.label:>8s} {t.decision.side:3s} @{t.decision.price:2d}c "
                      f"p={t.decision.p_win:.2f} -> {t.result:3s} {t.pnl_cents / 100:+.2f}")
        totals["trades"] += s["trades"]
        totals["pnl"] += s["pnl_dollars"]
        totals["model"] += r.model_brier
        totals["market"] += r.market_brier
        totals["n"] += r.scored
    n = max(totals["n"], 1)
    print(f"\nALL CITIES  trades {totals['trades']}  P&L ${totals['pnl']:+.2f}  "
          f"model Brier {totals['model'] / n:.4f}  market Brier {totals['market'] / n:.4f}")
    verdict = "BEATS" if totals["model"] < totals["market"] else "does NOT beat"
    print(f"model {verdict} the Kalshi price at {a.decision_hour}:00 local.")


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

    sp = sub.add_parser("research", help="learn where the Kalshi price is wrong (walk-forward)")
    sp.add_argument("--days", type=float, default=30)
    sp.add_argument("--csv")
    sp.add_argument("--source", choices=["coinbase", "massive"], default="massive")
    sp.add_argument("--train-frac", type=float, default=0.7)
    sp.add_argument("--l2", type=float, default=10.0, help="higher = stay closer to the market")
    common(sp)
    sp.set_defaults(func=cmd_research, vol_mult=0.9)

    sp = sub.add_parser("weather-backtest", help="daily high-temperature markets vs forecasts")
    sp.add_argument("--cities", default="NY,CHI,MIA,AUS", help="comma list: NY,CHI,MIA,AUS,DEN,LAX,PHIL")
    sp.add_argument("--days", type=float, default=120)
    sp.add_argument("--decision-hour", type=int, default=10, help="local hour on the day to trade")
    sp.add_argument("--window", type=int, default=45, help="days of forecast errors to calibrate on")
    sp.add_argument("--model", help="Open-Meteo model, e.g. gfs_seamless, ecmwf_ifs025")
    sp.add_argument("--trades", action="store_true")
    common(sp)
    sp.set_defaults(func=cmd_weather, min_price=5, max_price=95)

    sp = sub.add_parser("record", help="log Kalshi order book + BTC spot every few seconds")
    sp.add_argument("--poll", type=float, default=2.0)
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("live", help="run the bot (dry run unless --trade)")
    sp.add_argument("--trade", action="store_true", help="send real orders")
    sp.add_argument("--demo", action="store_true", help="use Kalshi's demo exchange")
    sp.add_argument("--poll", type=float, default=5.0)
    sp.add_argument("--max-daily-spend", type=float, default=50.0, help="dollars")
    common(sp)
    sp.set_defaults(func=cmd_live)

    a = p.parse_args(argv)
    a.func(a)
