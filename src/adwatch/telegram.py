"""Telegram: щоденне повідомлення, кнопки підтвердження і обробка натискань.

Будь-яка дія в кабінеті (пауза оголошення, мінус-слова) йде лише після натискання кнопки
людиною. Кнопка несе дату, ID і підпис HMAC: чужий callback без секрету не підробити, а
вебхук приймає тільки запити з заголовком `X-Telegram-Bot-Api-Secret-Token` і тільки з
дозволеного чату.
"""

from __future__ import annotations

import hashlib
import hmac
import html
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from adwatch.models import Alert

API = "https://api.telegram.org"
MAX_LEN = 3900          # Telegram ріже на 4096, лишаємо запас на HTML-теги
SEVERITY = {"high": "🔴", "medium": "🟠", "low": "🟡"}


# --- підпис кнопок -----------------------------------------------------------------

def sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:10]


def button(secret: str, kind: str, target: str, as_of: str) -> str:
    payload = f"{kind}|{target}|{as_of.replace('-', '')}"
    data = f"{payload}|{sign(secret, payload)}"
    assert len(data.encode()) <= 64, "callback_data у Telegram не довше 64 байт"
    return data


def verify(secret: str, data: str) -> tuple[str, str, str] | None:
    parts = data.split("|")
    if len(parts) != 4:
        return None
    payload = "|".join(parts[:3])
    if not hmac.compare_digest(sign(secret, payload), parts[3]):
        return None
    d = parts[2]
    return parts[0], parts[1], f"{d[:4]}-{d[4:6]}-{d[6:]}"


# --- тексти ------------------------------------------------------------------------

def _e(s: Any) -> str:
    return html.escape(str(s), quote=False)


def alert_text(a: Alert) -> str:
    return f"{SEVERITY[a.severity]} <b>{_e(a.title)}</b>\n{_e(a.details)}"


@dataclass
class Message:
    text: str
    buttons: list[list[dict]] | None = None


def daily_messages(res, secret: str, dashboard_url: str = "") -> list[Message]:
    """Повідомлення за день: огляд, по одному на кожну проблему з дією, мінус-слова."""
    d = res.as_of.isoformat()
    m = res.facts["meta"]["this_week"]
    g = res.facts["google_shopping"]["this_week"]
    head = (f"📊 <b>Ad Watch за {d}</b> (демо на синтетичному кабінеті)\n"
            f"Meta за 7 днів: ${m['spend']:.2f}, {m['purchases']} покупок, ROAS {m['roas']:.2f}\n"
            f"Google Shopping за 7 днів: ${g['cost']:.2f}, ACOS {g['acos_pct']:.1f}%\n\n"
            f"Проблем у Meta: <b>{len(res.meta_alerts)}</b> · змін ставок: <b>{len(res.label_changes)}</b> · "
            f"кандидатів у мінус-слова: <b>{len(res.negatives)}</b>")
    if dashboard_url:
        head += f'\n<a href="{_e(dashboard_url)}">Дашборд</a>'
    out = [Message(head)]
    plain = [a for a in res.meta_alerts if not a.action]
    if plain:
        out.append(Message("\n\n".join(alert_text(a) for a in plain)))
    for a in res.meta_alerts:
        if a.action and a.action.kind == "pause_ad":
            out.append(Message(alert_text(a), [[
                {"text": "⏸ Призупинити", "callback_data": button(secret, "p", a.entity_id, d)},
                {"text": "Пропустити", "callback_data": button(secret, "s", a.entity_id, d)}]]))
    if res.label_changes:
        lines = [f"• {_e(p.title)}: {p.label_from} → {p.label_to} ({_e(p.reason)})" for p in res.label_changes[:12]]
        more = f"\n…і ще {len(res.label_changes) - 12}" if len(res.label_changes) > 12 else ""
        out.append(Message("🛒 <b>Google Shopping: зміни кроку ставки</b> (custom_label_0)\n"
                           + "\n".join(lines) + more + "\n\nФід для Merchant Center: /api/feed.csv"))
    negs = [t for t in res.terms if t.negative]
    if negs and res.search_campaign_id:
        lines = [f"• «{_e(t.term)}»: {_e(t.reason)}" for t in negs]
        out.append(Message("🔎 <b>Google Search: кандидати в мінус-слова</b> (фразова відповідність)\n"
                           + "\n".join(lines), [[
                               {"text": f"➖ Додати всі ({len(negs)})",
                                "callback_data": button(secret, "n", res.search_campaign_id, d)},
                               {"text": "Пропустити", "callback_data": button(secret, "s", "neg", d)}]]))
    return [Message(x.text[:MAX_LEN], x.buttons) for x in out]


def weekly_message(res) -> Message:
    tag = {"llm": "текст моделі, усі числа звірені з даними", "template": "шаблон",
           "llm_rejected": "текст моделі відхилено: числа не збіглися з даними, показано шаблон"}[res.summary_source]
    return Message(f"🗓 <b>Тиждень {_e(res.facts['period'])}</b>\n{_e(res.summary)}\n\n<i>{tag}</i>")


# --- Bot API -----------------------------------------------------------------------

class Bot:
    def __init__(self, token: str, http: httpx.AsyncClient, base: str = API) -> None:
        self.url = f"{base}/bot{token}"
        self.http = http

    async def call(self, method: str, **params) -> dict:
        """Виклик Bot API. 429 чекає `retry_after`, але не більше двох разів і не довше 10 с."""
        last = {}
        for _ in range(3):
            r = await self.http.post(f"{self.url}/{method}", json=params, timeout=20.0)
            last = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 429:
                return last
            wait = min(float((last.get("parameters") or {}).get("retry_after", 1)), 10.0)
            await _sleep(wait)
        return last

    async def send(self, chat_id: str, msg: Message) -> dict:
        p: dict = {"chat_id": chat_id, "text": msg.text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if msg.buttons:
            p["reply_markup"] = {"inline_keyboard": msg.buttons}
        return await self.call("sendMessage", **p)


async def _sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(s)


# --- обробка натискань ---------------------------------------------------------------

Executor = Callable[[str, str, str], Awaitable[str]]     # (kind, target, as_of) -> текст результату


async def handle_update(update: dict, *, bot: Bot, secret: str, allowed_chat: str, execute: Executor) -> str:
    """Повертає коротку назву того, що сталося (для логів і тестів)."""
    if (msg := update.get("message")):
        chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            note = ("Цей чат отримує звіти." if chat == allowed_chat
                    else f"Ваш chat_id: <code>{_e(chat)}</code>. Додайте його в TELEGRAM_CHAT_ID.")
            await bot.call("sendMessage", chat_id=chat, text=f"Ad Watch на зв'язку. {note}", parse_mode="HTML")
            return "start"
        return "ignored_message"
    cq = update.get("callback_query")
    if not cq:
        return "ignored"
    chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
    if chat != allowed_chat:
        await bot.call("answerCallbackQuery", callback_query_id=cq["id"], text="Немає доступу")
        return "forbidden_chat"
    parsed = verify(secret, cq.get("data", ""))
    if not parsed:
        await bot.call("answerCallbackQuery", callback_query_id=cq["id"], text="Кнопка недійсна")
        return "bad_signature"
    kind, target, as_of = parsed
    if kind == "s":
        result = "Пропущено, нічого не змінено."
    else:
        try:
            result = await execute(kind, target, as_of)
        except Exception as e:                       # помилка API не має губитися мовчки
            result = f"Не вдалося: {type(e).__name__}: {str(e)[:200]}"
    await bot.call("answerCallbackQuery", callback_query_id=cq["id"], text=result[:190])
    m = cq.get("message") or {}
    who = (cq.get("from") or {}).get("first_name", "")
    await bot.call("editMessageText", chat_id=chat, message_id=m.get("message_id"), parse_mode="HTML",
                   text=f"{_e(m.get('text', ''))[:3500]}\n\n✅ <b>{_e(result)}</b> ({_e(who)})")
    return f"done:{kind}"

