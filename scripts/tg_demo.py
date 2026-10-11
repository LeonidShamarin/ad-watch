"""Сценарій для запису демо: повідомлення в чат і натискання кнопок через справжній вебхук.

Натискання імітується тим самим запитом, який шле Telegram (callback_query з секретним
заголовком) на задеплоєний сервіс, тож сервер реально виконує дію і редагує повідомлення.
Друкує моменти натискань (секунди від старту) для накладення кружечка на відео.

    python scripts/tg_demo.py [pause_delay] [neg_delay]
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tg_setup import env  # noqa: E402

from adwatch import mockapi, pipeline, telegram  # noqa: E402

URL = os.environ.get("PUBLIC_URL") or "https://ad-watch-six.vercel.app"
END = date(2026, 10, 10)


async def main() -> None:
    t0 = time.monotonic()
    secret, chat = env("TELEGRAM_WEBHOOK_SECRET"), env("TELEGRAM_CHAT_ID")
    app = mockapi.build_app(lambda: END)[0]
    res = await pipeline.run(END, clients=pipeline.make_clients(END, app))
    msgs = telegram.daily_messages(res, secret, URL)
    head = msgs[0]
    pause = next(m for m in msgs if m.buttons and m.buttons[0][0]["callback_data"].startswith("p|")
                 and "Втома" in m.text)
    neg = next(m for m in msgs if m.buttons and m.buttons[0][0]["callback_data"].startswith("n|"))
    async with httpx.AsyncClient(timeout=60) as http:
        bot = telegram.Bot(env("TELEGRAM_BOT_TOKEN"), http)

        async def tap(sent: dict, msg: telegram.Message, label: str, wait: float) -> None:
            await asyncio.sleep(wait)
            upd = {"update_id": int(time.time()), "callback_query": {
                "id": "demo", "data": msg.buttons[0][0]["callback_data"], "from": {"first_name": "Leonid"},
                "message": {"message_id": sent["result"]["message_id"], "chat": {"id": int(chat)},
                            "text": sent["result"].get("text", "")}}}
            print(f"TAP {label} at {time.monotonic() - t0:.1f}s", flush=True)
            r = await http.post(f"{URL}/telegram/webhook", json=upd,
                                headers={"X-Telegram-Bot-Api-Secret-Token": secret})
            print(f"  -> {r.status_code} {r.text[:120]}", flush=True)

        await bot.send(chat, head)
        print(f"SENT head at {time.monotonic() - t0:.1f}s", flush=True)
        await asyncio.sleep(2.5)
        s1 = await bot.send(chat, pause)
        print(f"SENT pause at {time.monotonic() - t0:.1f}s", flush=True)
        await tap(s1, pause, "pause", float(sys.argv[1]) if len(sys.argv) > 1 else 5)
        await asyncio.sleep(4)
        s2 = await bot.send(chat, neg)
        print(f"SENT negatives at {time.monotonic() - t0:.1f}s", flush=True)
        await tap(s2, neg, "negatives", float(sys.argv[2]) if len(sys.argv) > 2 else 6)


if __name__ == "__main__":
    asyncio.run(main())
