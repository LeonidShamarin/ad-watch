"""HTTP: дашборд, публічний мок обох API, щоденний прогін за розкладом і вебхук Telegram.

Розклад: Vercel Cron викликає /api/cron/daily щодня і /api/cron/weekly щопонеділка
з заголовком `Authorization: Bearer $CRON_SECRET`. Без секрету ці ендпоінти мовчать,
тож сторонній відвідувач не може засипати чат повідомленнями.
"""

from __future__ import annotations

import hmac
import os
import threading
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from adwatch import mockapi, pipeline, rules_gads, telegram, world
from adwatch.llm import GroqChat

STATIC = Path(__file__).resolve().parents[2] / "static"

app = FastAPI(title="Ad Watch", docs_url=None, redoc_url=None)
mock_app, meta_state, gads_state = mockapi.build_app()
app.mount("/mock", mock_app)

_lock = threading.Lock()


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _secret() -> str:
    # Підпис кнопок: окремий секрет, а якщо його немає, похідний від токена бота.
    return _env("TELEGRAM_WEBHOOK_SECRET") or _env("TELEGRAM_BOT_TOKEN")[-24:] or "dev-secret"


def _llm():
    key = _env("GROQ_API_KEY")
    return GroqChat(key) if key else None


def _as_of(d: str | None) -> date:
    if not d:
        return world.default_as_of()
    try:
        x = date.fromisoformat(d)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD") from None
    if abs((x - world.default_as_of()).days) > 30:
        raise HTTPException(400, "date must be within 30 days of yesterday")
    return x


async def _run(as_of: date, **kw) -> pipeline.RunResult:
    return await pipeline.run(as_of, clients=pipeline.make_clients(as_of, mock_app), **kw)


def _state_json(res: pipeline.RunResult) -> dict:
    ctr: dict[str, list] = {}
    for r in res.meta_daily:
        ctr.setdefault(r.ad_id, []).append([r.day.isoformat(), r.impressions, r.clicks, r.spend, r.purchases])
    fatigued = [a.entity_id for a in res.meta_alerts if a.kind == "creative_fatigue"]
    return {
        "as_of": res.as_of.isoformat(),
        "facts": res.facts,
        "summary": res.summary,
        "meta_alerts": [a.model_dump() for a in res.meta_alerts],
        "paused": sorted(meta_state.paused),
        "products": [p.model_dump() | {"changed": p.changed} for p in res.products],
        "terms": [t.model_dump() for t in res.terms],
        "negatives_added": sorted(gads_state.negatives.get(res.search_campaign_id, set())),
        "series": {k: ctr.get(k, []) for k in fatigued},
        "timings_ms": res.timings_ms,
        "api_calls": {"meta": dict(meta_state.calls), "google": dict(gads_state.calls)},
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": os.environ.get("ADS_MODE", "mock")}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/og.png")
def og() -> FileResponse:
    return FileResponse(STATIC / "og.png")


_terms_cache: dict[date, list] = {}


@app.get("/api/state")
async def state(date: str | None = None) -> dict:
    """Дані дашборду. Вердикти моделі по пошукових запитах рахуються раз на день на інстанс
    і кешуються, щоб відвідувачі сторінки не витрачали ключ; пауза й мінус-слова з Telegram
    видно одразу, бо стан моку читається при кожному запиті."""
    as_of = _as_of(date)
    with _lock:
        cached_terms = _terms_cache.get(as_of)
    res = await _run(as_of, llm=None if cached_terms else _llm())
    if cached_terms:
        res.terms = cached_terms
    elif any(t.source == "llm" for t in res.terms):
        with _lock:
            if len(_terms_cache) > 40:
                _terms_cache.clear()
            _terms_cache[as_of] = res.terms
    return _state_json(res)


@app.get("/api/feed.csv")
async def feed(date: str | None = None) -> PlainTextResponse:
    res = await _run(_as_of(date))
    return PlainTextResponse(rules_gads.supplemental_feed(res.products), media_type="text/csv")


def _cron_ok(request: Request) -> bool:
    want = _env("CRON_SECRET")
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bool(want) and hmac.compare_digest(want, got)


async def _send_all(msgs: list[telegram.Message]) -> list[bool]:
    token, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not (token and chat):
        raise HTTPException(503, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    async with httpx.AsyncClient() as http:
        bot = telegram.Bot(token, http)
        return [bool((await bot.send(chat, m)).get("ok")) for m in msgs]


def _public_url(request: Request) -> str:
    # На Vercel адреса запиту в cron це технічний домен деплою за логіном Vercel,
    # тому беремо основний домен проєкту, який Vercel дає в оточенні.
    prod = _env("VERCEL_PROJECT_PRODUCTION_URL")
    return _env("PUBLIC_URL") or (f"https://{prod}" if prod else str(request.base_url).rstrip("/"))


@app.get("/api/cron/daily")
async def cron_daily(request: Request) -> JSONResponse:
    if not _cron_ok(request):
        raise HTTPException(401, "cron secret required")
    res = await _run(world.default_as_of(), llm=_llm())
    sent = await _send_all(telegram.daily_messages(res, _secret(), _public_url(request)))
    return JSONResponse({"as_of": res.as_of.isoformat(), "messages": len(sent), "ok": all(sent),
                         "llm_cost_usd": res.llm_cost_usd})


@app.get("/api/cron/weekly")
async def cron_weekly(request: Request) -> JSONResponse:
    if not _cron_ok(request):
        raise HTTPException(401, "cron secret required")
    res = await _run(world.default_as_of(), llm=_llm(), with_summary=True, llm_terms=False)
    sent = await _send_all([telegram.weekly_message(res)])
    return JSONResponse({"summary_source": res.summary_source, "unverified": res.unverified, "ok": all(sent),
                         "llm_cost_usd": res.llm_cost_usd})


async def execute(kind: str, target: str, as_of: str) -> str:
    """Дія після натискання кнопки. Працює з моком у цьому ж процесі (у live: зі справжнім API)."""
    d = date.fromisoformat(as_of)
    c = pipeline.make_clients(d, mock_app)
    try:
        if kind == "p":
            ok = await c.meta.pause_ad(target)
            return f"Оголошення {target} призупинено" if ok else "Meta відповіла без success"
        if kind == "n":
            res = await pipeline.run(d, clients=c, llm=None)
            out = await c.gads.add_negatives(target, res.negatives)
            return f"Мінус-слова: додано {out['added']}, вже були {out['skipped']}, помилок {len(out['failed'])}"
        return "Невідома дія"
    finally:
        await c.http.aclose()


@app.post("/telegram/webhook")
async def tg_webhook(request: Request) -> dict:
    want = _env("TELEGRAM_WEBHOOK_SECRET")
    if not want or not hmac.compare_digest(want, request.headers.get("x-telegram-bot-api-secret-token", "")):
        raise HTTPException(401, "bad secret")
    update = await request.json()
    async with httpx.AsyncClient() as http:
        bot = telegram.Bot(_env("TELEGRAM_BOT_TOKEN"), http)
        what = await telegram.handle_update(update, bot=bot, secret=_secret(),
                                            allowed_chat=_env("TELEGRAM_CHAT_ID"), execute=execute)
    return {"ok": True, "handled": what}
