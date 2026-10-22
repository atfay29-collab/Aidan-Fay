import base64
import math

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from btc15m.backtest import MarketRecord, Quote, market_backtest, proxy_backtest
from btc15m.data import synthetic
from btc15m.history import to_quotes, to_record
from btc15m.kalshi import Kalshi, cents
from btc15m.model import ewma_vol_series, prob_up
from btc15m.strategy import Decision, Params, decide, settle_pnl_cents, taker_fee_cents


def test_prob_up_basics():
    assert prob_up(100, 100, 0.001, 10) == pytest.approx(0.5)
    assert prob_up(101, 100, 0.001, 10) > 0.5 > prob_up(99, 100, 0.001, 10)
    assert prob_up(101, 100, 0.001, 10) == pytest.approx(1 - prob_up(100, 101, 0.001, 10), abs=1e-3)
    # less time left -> more certain
    assert prob_up(100.2, 100, 0.001, 1) > prob_up(100.2, 100, 0.001, 10)
    assert prob_up(100, 99, 0.001, 0) == 1.0


def test_ewma_vol_is_causal():
    closes = [100 * math.exp(0.001 * (-1) ** i) for i in range(200)]
    a = ewma_vol_series(closes)
    b = ewma_vol_series(closes[:100] + [c * 2 for c in closes[100:]])
    assert a[:100] == b[:100]


def test_taker_fee():
    assert taker_fee_cents(50, 1) == 2      # 0.07 * .25 = 1.75c -> 2c
    assert taker_fee_cents(50, 100) == 175
    assert taker_fee_cents(10, 10) == 7     # 0.07 * 10 * .09 = 6.3c -> 7c


def test_decide_requires_edge_after_fees():
    p = Params(min_edge=0.05, contracts=10)
    assert decide(0.70, 60, 42, 8, p).side == "yes"
    assert decide(0.30, 60, 42, 8, p).side == "no"
    assert decide(0.62, 60, 42, 8, p) is None          # 2c raw edge, eaten by fees + threshold
    assert decide(0.90, 60, 42, 1, p) is None          # outside time window
    assert decide(0.99, 95, 7, 8, p) is None            # price band


def test_settle_pnl():
    d = Decision("yes", 40, 0.6, 0.1)
    fee = taker_fee_cents(40, 10)
    assert settle_pnl_cents(d, "yes", 10) == 600 - fee
    assert settle_pnl_cents(d, "no", 10) == -400 - fee


def test_proxy_backtest_calibrated_on_random_walk():
    r = proxy_backtest(synthetic(days=20, seed=3))
    assert r.windows > 1800
    late, early = r.by_minute[14].row(), r.by_minute[1].row()
    assert late["accuracy"] > early["accuracy"]
    assert early["brier"] < 0.25
    for pred, obs, n in r.calibration:
        if n > 1000:
            assert abs(pred - obs) < 0.05


def _fake_market(candles, mispricing):
    """Build Kalshi-like markets on synthetic BTC whose quotes are model price + mispricing."""
    closes = {c.ts + 60: c.close for c in candles}
    sig = dict(zip((c.ts + 60 for c in candles), ewma_vol_series([c.close for c in candles])))
    markets, quotes = [], {}
    for c in candles[200:-20]:
        if c.ts % 900:
            continue
        close_ts = c.ts + 900
        res = "yes" if closes[close_ts] >= c.open else "no"
        mk = MarketRecord(f"T{c.ts}", c.ts, close_ts, c.open, res)
        qs = []
        for t in range(c.ts + 60, close_ts, 60):
            fair = prob_up(closes[t], c.open, sig[t], (close_ts - t) / 60)
            mid = min(max(round(100 * (fair + mispricing)), 2), 98)
            qs.append(Quote(t, min(mid + 1, 99), min(100 - mid + 1, 99)))
        markets.append(mk)
        quotes[mk.ticker] = qs
    return markets, quotes


def test_market_backtest_finds_planted_edge_and_none_without():
    candles = synthetic(days=10, seed=11)
    fair_m, fair_q = _fake_market(candles, 0.0)
    r = market_backtest(fair_m, fair_q, candles, Params())
    assert r.trades == []  # 1c half-spread + fees < 5c threshold

    skew_m, skew_q = _fake_market(candles, 0.12)  # market overprices YES by 12c
    r = market_backtest(skew_m, skew_q, candles, Params())
    s = r.summary(10)
    assert s["trades"] > 100
    assert all(t.decision.side == "no" for t in r.trades)
    assert s["pnl_dollars"] > 0
    assert s["model_brier"] < s["market_brier"]


def test_parsing_cents_and_dollar_fields():
    assert cents({"yes_ask": 43}, "yes_ask") == 43
    assert cents({"yes_ask_dollars": "0.4300"}, "yes_ask") == 43
    assert cents({"yes_ask": 0}, "yes_ask") is None
    assert cents({"close_dollars": "0.07"}, "close") == 7
    rec = to_record({"ticker": "X", "open_time": "2026-10-01T12:00:00Z",
                     "close_time": "2026-10-01T12:15:00Z", "floor_strike": 101234.5,
                     "result": "no"})
    assert rec.close_ts - rec.open_ts == 900 and rec.strike == 101234.5
    assert to_record({"ticker": "X", "result": "", "floor_strike": 1}) is None
    q = to_quotes([{"end_period_ts": 60, "yes_ask": {"close": 55}, "yes_bid": {"close": 52}}])
    assert q[0] == Quote(60, 55, 48)


def test_signature_verifies(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "k.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    h = Kalshi("kid", str(pem)).sign_headers("POST", "/portfolio/orders?x=1")
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "POST/trade-api/v2/portfolio/orders").encode()
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())
