import random
from datetime import date, timedelta

import pytest

from btc15m.backtest import Quote
from btc15m.strategy import Params
from btc15m.weather import (CITIES, Bracket, backtest_city, event_date, fetch_forecast_highs,
                            to_bracket)


def test_event_date_and_bracket_parsing():
    assert event_date("KXHIGHNY-26OCT22") == date(2026, 10, 22)
    assert event_date("garbage") is None
    m = {"ticker": "KXHIGHNY-26OCT22-B72.5", "event_ticker": "KXHIGHNY-26OCT22",
         "strike_type": "between", "floor_strike": 72, "cap_strike": 73, "result": "yes",
         "expiration_value": "72.0", "open_time": "2026-10-20T14:00:00Z",
         "close_time": "2026-10-23T04:59:00Z"}
    b = to_bracket(m)
    assert b.day == date(2026, 10, 22) and b.actual == 72.0 and b.wins(72) and b.wins(73)
    assert not b.wins(74)
    assert to_bracket({**m, "strike_type": "weird"}) is None


def _event(day, center):
    """Kalshi-style ladder: <c-4, 2-degree brackets, >c+5."""
    bs = [Bracket(f"{day}-T{center-4}", str(day), day, "less", None, center - 4, "no", None, 0, 1)]
    for lo in range(center - 4, center + 6, 2):
        bs.append(Bracket(f"{day}-B{lo}", str(day), day, "between", lo, lo + 1, "no", None, 0, 1))
    bs.append(Bracket(f"{day}-T{center+5}", str(day), day, "greater", center + 5, None, "no", None, 0, 1))
    return bs


def test_bracket_probs_sum_to_one():
    bs = _event(date(2026, 1, 1), 70)
    assert sum(b.prob(71.3, 2.2) for b in bs) == pytest.approx(1.0, abs=1e-3)


def _world(mispricing: float, days=150, seed=1):
    rng = random.Random(seed)
    brackets, quotes, forecasts = [], {}, {}
    for i in range(days):
        day = date(2026, 1, 1) + timedelta(days=i)
        fc = 60 + 10 * rng.random()
        actual = round(fc + 1.0 + rng.gauss(0, 2.0))  # forecasts run 1F cold, sd 2
        forecasts[day] = fc
        center = round(fc)
        for b in _event(day, center):
            res = "yes" if b.wins(actual) else "no"
            b = Bracket(b.ticker, b.event, day, b.strike_type, b.floor, b.cap, res, float(actual), 0, 1)
            brackets.append(b)
            fair = b.prob(fc + 1.0, 2.0)
            # mispriced market ignores the cold bias: prices off the raw forecast
            mkt = b.prob(fc + 1.0 - mispricing, 2.0)
            mid = min(max(round(100 * mkt), 2), 98)
            quotes[b.ticker] = Quote(0, min(mid + 1, 99), min(101 - mid, 99))
    return brackets, quotes, forecasts


def test_finds_edge_when_market_ignores_forecast_bias():
    b, q, f = _world(mispricing=1.5, days=200)
    r = backtest_city("T", b, q, f, Params(min_edge=0.05, min_price=5, max_price=95))
    s = r.summary(10)
    assert s["rule_mismatch"] == "0/" + str(len(b))
    assert s["trades"] > 50 and s["pnl_dollars"] > 0
    assert s["model_brier"] < s["market_brier"]
    assert 0.3 < s["fcst_bias"] < 1.7


def test_no_edge_when_market_is_fair():
    b, q, f = _world(mispricing=0.0)
    s = backtest_city("T", b, q, f, Params(min_edge=0.05, min_price=5, max_price=95)).summary(10)
    assert s["trades"] < 0.1 * s["brackets_scored"]


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class _Sess:
    def __init__(self, body):
        self.body, self.params = body, None

    def get(self, url, params=None, timeout=None):
        self.params = params
        return _Resp(self.body)


def test_forecast_highs_use_local_standard_time_day():
    # NY is UTC-5 in standard time: 04:00Z on the 2nd still belongs to the 1st.
    body = {"hourly": {"time": ["2026-07-01T20:00", "2026-07-02T04:00", "2026-07-02T06:00"],
                       "temperature_2m_previous_day1": [80.0, 91.0, 70.0]}}
    s = _Sess(body)
    highs = fetch_forecast_highs(CITIES["NY"], date(2026, 7, 1), date(2026, 7, 2), session=s)
    assert highs == {date(2026, 7, 1): 91.0, date(2026, 7, 2): 70.0}
    assert s.params["temperature_unit"] == "fahrenheit"
