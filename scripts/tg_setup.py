"""Налаштування Telegram без показу секретів: усе читається з .env, у вивід іде лише результат.

    python scripts/tg_setup.py chat              # знайти chat_id з останнього /start і записати в .env
    python scripts/tg_setup.py webhook <url>     # під'єднати бота до <url>/telegram/webhook з секретом
    python scripts/tg_setup.py test              # надіслати щоденний звіт демо в чат (кнопки запрацюють після webhook)
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ENV = ROOT / ".env"


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v and ENV.exists():
        m = re.search(rf"^{name}=(.*)$", ENV.read_text(encoding="utf-8"), re.M)
        v = m.group(1).strip() if m else ""
    return v


def set_env(name: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8")
    text = re.sub(rf"^{name}=.*$", f"{name}={value}", text, flags=re.M) if re.search(rf"^{name}=", text, re.M) \
        else text.rstrip("\n") + f"\n{name}={value}\n"
    ENV.write_text(text, encoding="utf-8")


def api(method: str, **params) -> dict:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN порожній у .env")
    r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=20)
    d = r.json()
    if not d.get("ok"):
        sys.exit(f"{method}: {d.get('error_code')} {d.get('description')}")
    return d["result"]


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "chat":
        me = api("getMe")
        info = api("getWebhookInfo")
        if info.get("url"):
            sys.exit("У бота вже стоїть webhook, getUpdates не працює. Спершу: deleteWebhook.")
        ups = api("getUpdates", limit=50)
        chats = [u["message"]["chat"] for u in ups if u.get("message")]
        if not chats:
            sys.exit(f"Повідомлень боту @{me['username']} немає: відкрийте бота і натисніть Start.")
        chat = chats[-1]
        set_env("TELEGRAM_CHAT_ID", str(chat["id"]))
        print(f"Бот @{me['username']}; chat_id {chat['id']} ({chat.get('first_name', '')}) записано в .env")
    elif cmd == "webhook":
        url = sys.argv[2].rstrip("/") + "/telegram/webhook"
        api("setWebhook", url=url, secret_token=env("TELEGRAM_WEBHOOK_SECRET"),
            allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        info = api("getWebhookInfo")
        print(f"webhook: {info.get('url')} | pending {info.get('pending_update_count')} | "
              f"last error: {info.get('last_error_message') or '-'}")
    elif cmd == "test":
        from adwatch import pipeline, telegram
        os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", env("TELEGRAM_WEBHOOK_SECRET"))

        async def go():
            res = await pipeline.run()
            msgs = telegram.daily_messages(res, env("TELEGRAM_WEBHOOK_SECRET"), env("PUBLIC_URL"))
            async with httpx.AsyncClient() as http:
                bot = telegram.Bot(env("TELEGRAM_BOT_TOKEN"), http)
                return [(await bot.send(env("TELEGRAM_CHAT_ID"), m)).get("ok") for m in msgs]
        print("надіслано:", asyncio.run(go()))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
