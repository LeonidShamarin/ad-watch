"""HTTP: дашборд на збережених прогонах, публічний мок обох API, ручний запуск і вебхук Telegram.

Автоматичного розкладу немає (рішення власника демо: модель не має витрачати токени щодня).
Дашборд читає прогони з static/runs, зроблені заздалегідь `scripts/make_runs.py`, тож перегляд
сторінки моделі не викликає. Звіт у Telegram надсилається лише вручну: GET /api/cron/daily
з `Authorization: Bearer $CRON_SECRET` (або `scripts/tg_setup.py cron`).
"""

from __future__ import annotations

import hmac
import os
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from adwatch import mockapi, pipeline, snapshots, telegram, world
from adwatch.llm import GroqChat
from adwatch.rules_gads import supplemental_feed
from adwatch.models import ProductDecision

STATIC = Path(__file__).resolve().parents[2] / "static"
END = snapshots.latest() or world.default_as_of()     # останній збережений прогін = «сьогодні» демо

app = FastAPI(title="Ad Watch", docs_url=None, redoc_url=None)
mock_app, meta_state, gads_state = mockapi.build_app(lambda: END)
app.mount("/mock", mock_app)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _secret() -> str:
    # Підпис кнопок: окремий секрет, а якщо його немає, похідний від токена бота.
    return _env("TELEGRAM_WEBHOOK_SECRET") or _env("TELEGRAM_BOT_TOKEN")[-24:] or "dev-secret"


def _llm():
    key = _env("GROQ_API_KEY")
    return GroqChat(key) if key else None


async def _run(as_of: date, **kw) -> pipeline.RunResult:
    return await pipeline.run(as_of, clients=pipeline.make_clients(as_of, mock_app), **kw)


def _snapshot(d: str | None) -> dict:
    as_of = d or END.isoformat()
    snap = snapshots.load(as_of)
    if snap is None:
        raise HTTPException(404, f"no saved run for {as_of}; available: {', '.join(snapshots.dates())}")
    return snap


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": os.environ.get("ADS_MODE", "mock"), "runs": len(snapshots.dates())}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/og.png")
def og() -> FileResponse:
    return FileResponse(STATIC / "og.png")


@app.get("/api/runs")
def runs() -> list[dict]:
    return snapshots.index()


@app.get("/api/state")
def state(date: str | None = None) -> dict:
    """Збережений прогін + те, що змінилось з Telegram (пауза, мінус-слова) у моці цього інстансу."""
    snap = _snapshot(date)
    return snap | {"paused": sorted(meta_state.paused),
                   "negatives_added": sorted(gads_state.negatives.get(snap["search_campaign_id"], set()))}


@app.get("/api/feed.csv")
def feed(date: str | None = None) -> PlainTextResponse:
    products = [ProductDecision.model_validate(p) for p in _snapshot(date)["products"]]
    return PlainTextResponse(supplemental_feed(products), media_type="text/csv")

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
    res = await _run(END, llm=_llm())
    sent = await _send_all(telegram.daily_messages(res, _secret(), _public_url(request)))
    return JSONResponse({"as_of": res.as_of.isoformat(), "messages": len(sent), "ok": all(sent),
                         "llm_cost_usd": res.llm_cost_usd})


@app.get("/api/cron/weekly")
async def cron_weekly(request: Request) -> JSONResponse:
    if not _cron_ok(request):
        raise HTTPException(401, "cron secret required")
    res = await _run(END, llm=_llm(), with_summary=True, llm_terms=False)
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
