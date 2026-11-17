"""vrbo/fetch.py on the pooled sessions: no network (a fake curl session
inside a real fetch._Session; the pool's mint is faked).

    python -m pytest vrbo/test_fetch.py -q
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrbo import fetch  # noqa: E402


class Resp:
    def __init__(self, status=200, text="", url="https://www.vrbo.com/", payload=None):
        self.status_code, self.text, self.url, self._payload = status, text, url, payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeCurl:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = {}
        self.closed = False

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self.responses.pop(0)

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(fetch.config, "VRBO_EXIT_ATTEMPTS", 1)
    monkeypatch.setattr(fetch.config, "MAX_RETRIES", 1)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetch, "_fallback_until", 0.0)
    monkeypatch.setattr(fetch, "_target_index", 0)
    monkeypatch.setattr(fetch, "_direct_refusals", 0)
    queue, minted = [], []

    def fake_mint(key, hint=None):
        sess = fetch._Session(None)
        sess.curl.close()
        sess.curl = FakeCurl(queue.pop(0) if queue else [])
        minted.append(sess)
        return sess

    p = fetch._sessions
    p.clear()
    p._mint_fn = fake_mint
    p.minted = minted
    p.script = lambda *per_session: queue.extend(list(r) for r in per_session)
    yield p
    p.clear()
    p._mint_fn = fetch._mint


def calendar(days):
    return Resp(200, "{}", payload=[{"data": {"propertyRatesDateSelector": {"days": days}}}])


def test_three_threads_share_one_warmed_session_and_a_block_retires_it(pool):
    """Three sequential GraphQL calls on three different threads ride ONE
    pooled session: the homepage warm-up happens once (before: once per
    thread). A 429 then retires it (closed, nothing idle) and, on the direct
    tier, advances the fingerprint ladder."""
    pool.script([Resp(200, "<html>home</html>"), calendar([1]), calendar([2]), calendar([3]),
                 Resp(429, "Bot or Not?")])
    results = []
    for _ in range(3):
        t = threading.Thread(target=lambda: results.append(fetch.fetch_calendar("123")))
        t.start()
        t.join()
    assert [r["days"] for r in results] == [[1], [2], [3]]
    assert len(pool.minted) == 1
    sess = pool.minted[0]
    assert sess.curl.calls[0] == ("GET", "https://www.vrbo.com/")
    assert [m for m, _ in sess.curl.calls].count("GET") == 1 and sess.vrbo_warm
    assert pool.stats()["idle"] == 1 and not sess.closed
    with pytest.raises(fetch.VrboBlocked):
        fetch.fetch_calendar("123")
    assert sess.dead and sess.closed and pool.stats()["idle"] == 0
    assert fetch._target_index == 1          # the next mint rides the other fingerprint


def test_property_page_candidates_ride_one_session(pool):
    """A listing-shaped id that 404s as a listing is retried as an Expedia id
    (/pdp/lo/) on the SAME borrowed session, not a second one."""
    pool.script([Resp(404, "", "https://www.vrbo.com/113891154"),
                 Resp(200, "<html>PropertyInfo:1</html>", "https://www.vrbo.com/2165911")])
    html, listing = fetch.fetch_property_page("113891154")
    assert listing == "2165911" and len(pool.minted) == 1
    assert [u for _, u in pool.minted[0].curl.calls] == ["https://www.vrbo.com/113891154",
                                                        "https://www.vrbo.com/pdp/lo/113891154"]
