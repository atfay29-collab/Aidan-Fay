"""Kalshi daily-high-temperature markets: forecast-based fair value and backtest.

Each Kalshi event (e.g. KXHIGHNY-26OCT22) is a set of brackets on one city's
official high for one day: "between" brackets like 72-73°, plus two tails.
They settle on the NWS Daily Climate Report for one station, which covers
midnight to midnight local *standard* time.

Model: high ~ Normal(forecast + bias, sd), where forecast is the max of the
hourly forecast issued ~24h earlier (Open-Meteo Previous Runs API,
`temperature_2m_previous_day1`), and bias/sd are fit on the city's trailing
residuals (actual - forecast) from days already settled. No look-ahead: the
forecast for every hour of day D was issued before the decision time on D,
and calibration only uses days before D.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

import requests

from .backtest import Quote
from .history import to_quotes
from .kalshi import Kalshi, parse_ts
from .model import norm_cdf
from .strategy import Decision, Params, decide, settle_pnl_cents

OPEN_METEO = "https://previous-runs-api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class City:
    series: str
    lat: float
    lon: float
    tz: str
    std_offset: int  # hours from UTC in standard time (the climate-report day)
    station: str


# Stations as best known; `weather-backtest` prints each series' settlement
# rules so you can check them against Kalshi's own wording.
CITIES = {
    "NY": City("KXHIGHNY", 40.7789, -73.9692, "America/New_York", -5, "Central Park (KNYC)"),
    "CHI": City("KXHIGHCHI", 41.7868, -87.7522, "America/Chicago", -6, "Chicago Midway (KMDW)"),
    "MIA": City("KXHIGHMIA", 25.7959, -80.2870, "America/New_York", -5, "Miami Intl (KMIA)"),
    "AUS": City("KXHIGHAUS", 30.1945, -97.6699, "America/Chicago", -6, "Austin-Bergstrom (KAUS)"),
    "DEN": City("KXHIGHDEN", 39.8561, -104.6737, "America/Denver", -7, "Denver Intl (KDEN)"),
    "LAX": City("KXHIGHLAX", 33.9416, -118.4085, "America/Los_Angeles", -8, "Los Angeles Intl (KLAX)"),
    "PHIL": City("KXHIGHPHIL", 39.8729, -75.2437, "America/New_York", -5, "Philadelphia Intl (KPHL)"),
}

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def event_date(event_ticker: str) -> date | None:
    """KXHIGHNY-26OCT22 -> 2026-10-22."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))


def _num(v) -> float | None:
    if v is None:
        return None
    m = re.search(r"-?\d+(\.\d+)?", str(v))
    return float(m.group()) if m else None


@dataclass(frozen=True)
class Bracket:
    ticker: str
    event: str
    day: date
    strike_type: str       # "between", "greater", "less"
    floor: float | None
    cap: float | None
    result: str            # "yes"/"no"
    actual: float | None   # observed high from expiration_value, if published
    open_ts: int
    close_ts: int

    def wins(self, t: float) -> bool:
        if self.strike_type == "between":
            return self.floor <= t <= self.cap
        if self.strike_type == "greater":
            return t > self.floor
        return t < self.cap

    def prob(self, mu: float, sd: float) -> float:
        """P(bracket) for an integer-reported high ~ N(mu, sd)."""
        if self.strike_type == "between":
            lo, hi = self.floor - 0.5, self.cap + 0.5
        elif self.strike_type == "greater":
            lo, hi = self.floor + 0.5, math.inf
        else:
            lo, hi = -math.inf, self.cap - 0.5
        cdf = lambda x: 0.0 if x == -math.inf else 1.0 if x == math.inf else norm_cdf((x - mu) / sd)
        return min(max(cdf(hi) - cdf(lo), 1e-4), 1 - 1e-4)


def to_bracket(m: dict) -> Bracket | None:
    day = event_date(m.get("event_ticker", ""))
    st = m.get("strike_type")
    if day is None or m.get("result") not in ("yes", "no"):
        return None
    if st not in ("between", "greater", "less"):
        return None
    b = Bracket(m["ticker"], m["event_ticker"], day, st, _num(m.get("floor_strike")),
                _num(m.get("cap_strike")), m["result"], _num(m.get("expiration_value")),
                parse_ts(m["open_time"]), parse_ts(m["close_time"]))
    if (st in ("between", "greater") and b.floor is None) or (st in ("between", "less") and b.cap is None):
        return None
    return b


def actual_highs(brackets: list[Bracket]) -> dict[date, float]:
    """Observed high per day: expiration_value, else the midpoint of the winning bracket."""
    out: dict[date, float] = {}
    for b in brackets:
        if b.actual is not None:
            out[b.day] = b.actual
    for b in brackets:
        if b.day not in out and b.result == "yes" and b.strike_type == "between":
            out[b.day] = (b.floor + b.cap) / 2
    return out


# -- data download ------------------------------------------------------------

def fetch_forecast_highs(city: City, start: date, end: date, model: str | None = None,
                         session: requests.Session | None = None) -> dict[date, float]:
    """Daily max (climate-report day) of the hourly forecast issued ~24h earlier."""
    params = {"latitude": city.lat, "longitude": city.lon,
              "hourly": "temperature_2m_previous_day1", "temperature_unit": "fahrenheit",
              "timezone": "GMT", "start_date": start.isoformat(), "end_date": end.isoformat()}
    if model:
        params["models"] = model
    r = (session or requests).get(OPEN_METEO, params=params, timeout=60)
    r.raise_for_status()
    hourly = r.json()["hourly"]
    temps = hourly.get("temperature_2m_previous_day1") or next(
        v for k, v in hourly.items() if k.startswith("temperature_2m_previous_day1"))
    highs: dict[date, float] = {}
    for t, v in zip(hourly["time"], temps):
        if v is None:
            continue
        lst = datetime.fromisoformat(t) + timedelta(hours=city.std_offset)
        d = lst.date()
        highs[d] = max(highs.get(d, -999.0), float(v))
    return highs


def load_city(client: Kalshi, city: City, start_ts: int, end_ts: int, decision_hour: int,
              cache_dir: Path, log=print) -> tuple[list[Bracket], dict[str, Quote | None], str]:
    raw = list(client.markets(city.series, status="settled", min_close_ts=start_ts, max_close_ts=end_ts))
    brackets = [b for b in map(to_bracket, raw) if b]
    rules = next((m.get("rules_primary", "") for m in raw if m.get("rules_primary")), "")
    log(f"{city.series}: {len(raw)} settled markets, {len(brackets)} usable")
    cache_dir.mkdir(parents=True, exist_ok=True)
    quotes: dict[str, Quote | None] = {}
    for b in brackets:
        dts = decision_ts(city, b.day, decision_hour)
        if b.open_ts >= dts:
            quotes[b.ticker] = None
            continue
        path = cache_dir / f"{b.ticker}_h{decision_hour}.json"
        if path.exists():
            candles = json.loads(path.read_text())
        else:
            candles = client.candlesticks(b.ticker, b.open_ts, dts, city.series, period=60)
            path.write_text(json.dumps(candles))
            time.sleep(0.06)
        qs = [q for q in to_quotes(candles) if q.ts <= dts and q.yes_ask and q.no_ask]
        quotes[b.ticker] = qs[-1] if qs else None
    return brackets, quotes, rules


def decision_ts(city: City, day: date, hour: int) -> int:
    local = datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(city.tz))
    return int(local.astimezone(timezone.utc).timestamp())


# -- backtest -----------------------------------------------------------------

@dataclass
class WTrade:
    ticker: str
    day: date
    label: str
    decision: Decision
    result: str
    pnl_cents: int


@dataclass
class CityResult:
    name: str
    events: int = 0
    trades: list[WTrade] = field(default_factory=list)
    model_brier: float = 0.0
    market_brier: float = 0.0
    scored: int = 0
    rule_mismatches: int = 0
    rule_checked: int = 0
    bias: float = 0.0
    sd: float = 0.0

    def summary(self, contracts: int) -> dict:
        pnl = sum(t.pnl_cents for t in self.trades)
        risked = sum(t.decision.price * contracts for t in self.trades)
        wins = sum(t.result == t.decision.side for t in self.trades)
        n = len(self.trades)
        return {"events": self.events, "trades": n, "win_rate": wins / n if n else 0.0,
                "pnl_dollars": pnl / 100, "roi": pnl / risked if risked else 0.0,
                "model_brier": self.model_brier / max(self.scored, 1),
                "market_brier": self.market_brier / max(self.scored, 1),
                "brackets_scored": self.scored, "fcst_bias": self.bias, "fcst_sd": self.sd,
                "rule_mismatch": f"{self.rule_mismatches}/{self.rule_checked}"}


def _label(b: Bracket) -> str:
    if b.strike_type == "between":
        return f"{b.floor:g}-{b.cap:g}"
    return f">{b.floor:g}" if b.strike_type == "greater" else f"<{b.cap:g}"


def backtest_city(name: str, brackets: list[Bracket], quotes: dict[str, Quote | None],
                  forecasts: dict[date, float], params: Params, window: int = 45,
                  min_history: int = 15, min_sd: float = 1.2) -> CityResult:
    res = CityResult(name)
    params = dataclasses.replace(params, min_minutes_left=-1.0, max_minutes_left=math.inf)
    actual = actual_highs(brackets)
    for b in brackets:
        if b.actual is not None:
            res.rule_checked += 1
            res.rule_mismatches += b.wins(b.actual) != (b.result == "yes")

    by_day: dict[date, list[Bracket]] = {}
    for b in brackets:
        by_day.setdefault(b.day, []).append(b)
    resid_days = sorted(d for d in actual if d in forecasts)

    for day in sorted(by_day):
        if day not in forecasts:
            continue
        hist = [actual[d] - forecasts[d] for d in resid_days if d < day][-window:]
        if len(hist) < min_history:
            continue
        bias, sd = mean(hist), max(pstdev(hist), min_sd)
        mu = forecasts[day] + bias
        res.events += 1
        res.bias, res.sd = bias, sd  # latest values, for the report
        for b in by_day[day]:
            q = quotes.get(b.ticker)
            if q is None:
                continue
            p = b.prob(mu, sd)
            y = int(b.result == "yes")
            res.scored += 1
            res.model_brier += (p - y) ** 2
            res.market_brier += ((q.yes_ask + 100 - q.no_ask) / 200 - y) ** 2
            d = decide(p, q.yes_ask, q.no_ask, 0.0, params)
            if d:
                res.trades.append(WTrade(b.ticker, day, _label(b), d, b.result,
                                         settle_pnl_cents(d, b.result, params.contracts)))
    return res
