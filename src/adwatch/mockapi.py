"""Мок-сервер обох API в одному ASGI-застосунку.

На Vercel він змонтований у той самий сервіс (`/mock/meta/...`, `/mock/gads/...`), а конвеєр
ходить у нього через `httpx.ASGITransport` без мережі. Світ будується по даті й кешується:
у межах дня всі запити бачать однакові цифри.
"""

from __future__ import annotations

import threading
from datetime import date

from fastapi import FastAPI

from adwatch import mock_gads, mock_meta, world

_lock = threading.Lock()
_cache: dict[tuple, object] = {}


def _cached(key: tuple, build):
    with _lock:
        if key not in _cache:
            if len(_cache) > 16:                 # кеш не росте без меж: кілька дат, не більше
                _cache.clear()
            _cache[key] = build()
        return _cache[key]


def meta_world(as_of: date) -> world.MetaWorld:
    return _cached(("meta", as_of), lambda: world.build_meta(as_of))


def gads_world(as_of: date) -> world.GadsWorld:
    return _cached(("gads", as_of), lambda: world.build_gads(as_of))


def build_app(as_of_fn=world.default_as_of) -> tuple[FastAPI, mock_meta.MetaMockState, mock_gads.GadsMockState]:
    meta_state = mock_meta.MetaMockState(lambda: meta_world(as_of_fn()))
    gads_state = mock_gads.GadsMockState(lambda: gads_world(as_of_fn()))
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(mock_meta.make_router(meta_state), prefix="/meta")
    app.include_router(mock_gads.make_router(gads_state), prefix="/gads")
    return app, meta_state, gads_state
