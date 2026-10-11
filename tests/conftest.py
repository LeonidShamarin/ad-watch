import asyncio
from datetime import date

import httpx
import pytest

from adwatch import mockapi

AS_OF = date(2026, 10, 10)


@pytest.fixture
def mock():
    """Мок обох API і asyncio-хелпер для запитів до нього."""
    app, meta_state, gads_state = mockapi.build_app(lambda: AS_OF)

    def call(method: str, url: str, **kw) -> httpx.Response:
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(go())

    call.app, call.meta, call.gads = app, meta_state, gads_state
    return call


async def no_sleep(_s: float) -> None:
    return None
