"""Клієнти: повтори, межі циклів, нормалізація формату."""

import asyncio
from datetime import date

import httpx
import pytest

from adwatch.gads_client import GadsClient, flatten, num
from adwatch.meta_client import MetaAuthError, MetaClient, MetaError
from tests.conftest import no_sleep


def _client(handler, cls="meta"):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if cls == "meta":
        return MetaClient(http, "http://x", "t", "1", sleep=no_sleep)
    return GadsClient(http, "http://x", developer_token="d", access_token="a", customer_id="123-456-7890",
                      sleep=no_sleep)


def test_meta_retries_rate_limit_then_succeeds():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(400, json={"error": {"code": 613, "message": "Calls within one hour"}})
        return httpx.Response(200, json={"data": [], "paging": {"cursors": {"after": "a"}}})

    assert asyncio.run(_client(h).insights_daily(date(2026, 10, 1), date(2026, 10, 2))) == []
    assert calls["n"] == 3


def test_meta_auth_error_is_not_retried():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"code": 190, "message": "expired"}})

    with pytest.raises(MetaAuthError):
        asyncio.run(_client(h).account())
    assert calls["n"] == 1


def test_meta_stuck_cursor_does_not_loop_forever():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(200, json={"data": [{"date_start": "2026-10-01", "ad_id": "1"}],
                                         "paging": {"cursors": {"after": "same"}, "next": "x"}})

    rows = asyncio.run(_client(h).insights_daily(date(2026, 10, 1), date(2026, 10, 1)))
    assert calls["n"] == 2 and len(rows) == 2          # другий раз той самий курсор: стоп


def test_meta_gives_up_after_retries():
    def h(req):
        return httpx.Response(500, json={"error": {"code": 2, "message": "unknown"}})

    with pytest.raises(MetaError):
        asyncio.run(_client(h).ads())


def test_gads_flatten_and_missing_zero():
    row = flatten({"metrics": {"costMicros": "1500000"}, "segments": {"productItemId": "sku-1"}})
    assert num(row, "metrics.costMicros") == 1_500_000 and num(row, "metrics.clicks") == 0


def test_gads_retries_resource_exhausted():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}})
        assert req.headers["developer-token"] == "d" and "/customers/1234567890/" in str(req.url)
        return httpx.Response(200, json=[{"results": [{"campaign": {"id": "5"}}]}])

    assert asyncio.run(_client(h, "gads").search("SELECT campaign.id FROM campaign")) == [{"campaign.id": "5"}]


def test_gads_add_negatives_skips_existing(mock):
    from adwatch import pipeline

    async def go():
        c = pipeline.make_clients(date(2026, 10, 10), mock.app)
        try:
            first = await c.gads.add_negatives("20000000002", ["jobs", "free", "Jobs"])
            second = await c.gads.add_negatives("20000000002", ["jobs", "manual"])
            return first, second
        finally:
            await c.http.aclose()

    first, second = asyncio.run(go())
    assert first == {"added": 2, "skipped": 1, "failed": []}
    assert second == {"added": 1, "skipped": 1, "failed": []}
