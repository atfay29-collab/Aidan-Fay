import pytest

from btc15m import massive


class FakeResp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, str(body)

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls, self.headers = list(pages), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.pages.pop(0)


def bar(ts, c):
    return {"t": ts * 1000, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1.0}


def test_fetch_paginates_and_uses_bearer(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "k123")
    s = FakeSession([
        FakeResp({"results": [bar(60, 1.0), bar(120, 2.0)], "next_url": "https://api.massive.com/next?cursor=x"}),
        FakeResp({"results": [bar(180, 3.0), bar(240, 9.0)]}),
    ])
    out = massive.fetch_massive(60, 240, session=s)
    assert [c.ts for c in out] == [60, 120, 180]            # end is exclusive
    assert out[1].close == 2.0 and out[1].high == 3.0
    assert s.headers["Authorization"] == "Bearer k123"
    assert "/v2/aggs/ticker/X:BTCUSD/range/1/minute/60000/239000" in s.calls[0][0]
    assert s.calls[1] == ("https://api.massive.com/next?cursor=x", None)


def test_missing_key_and_401(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MASSIVE_API_KEY"):
        massive.fetch_massive(0, 60, session=FakeSession([]))
    monkeypatch.setenv("MASSIVE_API_KEY", "bad")
    with pytest.raises(RuntimeError, match="401"):
        massive.fetch_massive(0, 60, session=FakeSession([FakeResp({}, 401)]))
