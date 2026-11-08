"""Модель для двох вузьких задач: оцінка сумнівних пошукових запитів і текст тижневого звіту.

Обидві задачі не вирішують нічого самі: запити лише пропонуються людині, а числа звіту
рахує код і перевіряє `check_numbers` (перенесено з Shop Pulse). Модель — Groq
`openai/gpt-oss-120b`, бо ключ і ліміти вже перевірені в Shop Pulse.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Protocol

import httpx
from pydantic import BaseModel, ValidationError

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"
PRICE_PER_M = (0.15, 0.60)          # $ за 1M токенів: вхід, вихід
MAX_RETRIES = 3
MAX_REPAIRS = 1


class LLMError(Exception):
    pass


class Chat(Protocol):
    def complete(self, messages: list[dict], json_mode: bool) -> tuple[str, dict]:
        """Повертає (текст, usage)."""


class GroqChat:
    def __init__(self, api_key: str, *, model: str = MODEL, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.key, self.model = api_key, model
        self.http = http or httpx.Client(timeout=40.0)
        self.sleep = sleep

    def complete(self, messages: list[dict], json_mode: bool) -> tuple[str, dict]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.0,
                                "max_completion_tokens": 1500, "reasoning_effort": "low"}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = self.http.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {self.key}"})
            except httpx.TransportError as e:
                last = e
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = LLMError(f"HTTP {r.status_code}")
                else:
                    r.raise_for_status()
                    d = r.json()
                    return d["choices"][0]["message"].get("content") or "", d.get("usage", {})
            self.sleep(min(2 ** attempt, 8))
        raise LLMError(str(last))


def cost_usd(usage: dict) -> float:
    return (usage.get("prompt_tokens", 0) * PRICE_PER_M[0] + usage.get("completion_tokens", 0) * PRICE_PER_M[1]) / 1e6


# --- multi-provider fallback -------------------------------------------------
#
# Chain: current provider (Groq) -> the four ai-keys.env keys -> ключі2.txt last.
# Groq/OpenAI/xAI are natively OpenAI-compatible; Gemini has an OpenAI-compatible
# endpoint. A fallback provider ignores the Groq model id and uses its own default.
PROVIDER_ENDPOINTS: dict[str, tuple[str, str]] = {
    "groq": ("https://api.groq.com/openai/v1", MODEL),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash-lite"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "xai": ("https://api.x.ai/v1", "grok-2-latest"),
}
_PRIMARY_ENV = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY",
                "openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY"}
_AIKEYS_ENV = [("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY"),
               ("openai", "OPENAI_API_KEY"), ("xai", "XAI_API_KEY")]
_KLYUCHI2_ENV = [("gemini", "GEMINI_API_KEY_2"), ("gemini", "GEMINI_API_KEY_3"),
                 ("groq", "GROQ_API_KEY_2")]


class OpenAICompatChat:
    """OpenAI-compatible chat for any fallback provider. Same `complete` contract
    as GroqChat, but with the provider's base URL and default model, `max_tokens`
    (portable) and no `reasoning_effort` (Groq-only)."""

    def __init__(self, provider: str, api_key: str, *, model: str | None = None,
                 http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        base, model_default = PROVIDER_ENDPOINTS.get(provider, ("", ""))
        self.provider, self.key = provider, api_key
        self.base_url = base.rstrip("/")
        self.model = model or model_default
        self.http = http or httpx.Client(timeout=40.0)
        self.sleep = sleep

    def complete(self, messages: list[dict], json_mode: bool) -> tuple[str, dict]:
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "temperature": 0.0, "max_tokens": 1500}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = self.http.post(f"{self.base_url}/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {self.key}"})
            except httpx.TransportError as e:
                last = e
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = LLMError(f"HTTP {r.status_code}")
                elif r.status_code >= 400:
                    raise LLMError(f"{self.provider} HTTP {r.status_code}: {r.text[:200]}")
                else:
                    d = r.json()
                    return d["choices"][0]["message"].get("content") or "", d.get("usage", {})
            self.sleep(min(2 ** attempt, 8))
        raise LLMError(str(last))


class FallbackChat:
    """Try each provider in order; switch only on a request failure (`LLMError`
    or an httpx error). A validation/repair problem stays with the one provider,
    handled by `judge_terms`, so a bad answer never burns the whole chain."""

    def __init__(self, members: list[tuple[str, Chat]]) -> None:
        if not members:
            raise LLMError("fallback chain is empty")
        self.members = members

    def complete(self, messages: list[dict], json_mode: bool) -> tuple[str, dict]:
        errors: list[str] = []
        for name, chat in self.members:
            try:
                return chat.complete(messages, json_mode)
            except (LLMError, httpx.HTTPError) as e:
                errors.append(f"{name}: {str(e)[:150]}")
        raise LLMError("all providers failed -> " + " | ".join(errors))


def _make_chat(provider: str, key: str, http: httpx.Client | None = None) -> Chat:
    if provider == "groq":
        return GroqChat(key, http=http)
    return OpenAICompatChat(provider, key, http=http)


def build_chat(primary: str = "groq", *, env: dict | None = None,
               http: httpx.Client | None = None) -> Chat | None:
    """Build the provider fallback chain from the environment, or `None` when no
    key is set (so the caller falls back to rules/template, as before).

    Order: current provider, then the ai-keys.env four (gemini, groq, openai,
    xai), then the ключі2.txt keys last; unset keys skipped, duplicates collapsed.
    A single configured provider returns a bare client."""
    env = os.environ if env is None else env
    ordered: list[tuple[str, str]] = [(primary, _PRIMARY_ENV[primary])]
    ordered += _AIKEYS_ENV + _KLYUCHI2_ENV
    members: list[tuple[str, Chat]] = []
    seen: set[tuple[str, str]] = set()
    for provider, var in ordered:
        key = (env.get(var) or "").strip()
        if not key or (provider, key) in seen:
            continue
        seen.add((provider, key))
        members.append((f"{provider}:{var}", _make_chat(provider, key, http=http)))
    if not members:
        return None
    if len(members) == 1:
        return members[0][1]
    return FallbackChat(members)


# --- сумнівні запити ---------------------------------------------------------------

class TermJudgement(BaseModel):
    term: str
    negative: bool
    reason: str


class TermJudgements(BaseModel):
    items: list[TermJudgement]


TERMS_SYSTEM = """You review Google Ads search terms for an online garden-supplies store.
Catalog: garden hoses and reels, spray guns, sprinklers, drip irrigation, connectors, tap timers,
rain barrels, garden tools, pots, lights, garden furniture, pond pumps, pressure washers (own brand).
The store does NOT sell: Gardena, Karcher, Hozelock; car parts; firefighting equipment; repair services;
software or apps; used goods.
For every term decide whether it should become a NEGATIVE keyword: true when the searcher is unlikely to
buy a product from this catalog (job seekers, freebies, repair/how-to info, other categories, brands we
don't sell, apps). Purchase intent with delivery or price words ("free shipping", "cheap", "near me",
"buy") is NOT negative. Reason: max 12 words, in Ukrainian.
Return JSON: {"items": [{"term": "...", "negative": true, "reason": "..."}]} with exactly the given terms."""


def judge_terms(chat: Chat, terms: list[str]) -> tuple[dict[str, TermJudgement], dict]:
    """Оцінити запити. Невалідна відповідь: одна спроба самовиправлення з показом помилки."""
    if not terms:
        return {}, {}
    messages = [{"role": "system", "content": TERMS_SYSTEM},
                {"role": "user", "content": json.dumps({"terms": terms}, ensure_ascii=False)}]
    usage_total: dict = {}
    for _ in range(MAX_REPAIRS + 1):
        text, usage = chat.complete(messages, json_mode=True)
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                usage_total[k] = usage_total.get(k, 0) + v
        try:
            parsed = TermJudgements.model_validate_json(text)
            got = {j.term.strip().lower(): j for j in parsed.items}
            missing = [t for t in terms if t not in got]
            if missing:
                raise ValueError(f"missing terms: {missing}")
            return {t: got[t] for t in terms}, usage_total
        except (ValidationError, ValueError) as e:
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"Invalid answer: {str(e)[:400]}. Return the JSON again."}]
    raise LLMError("term judgement failed validation after repair")


# --- тижневий звіт ---------------------------------------------------------------

REPORT_SYSTEM = """Ти аналітик реклами. Напиши 3-5 коротких речень українською для власника магазину:
що змінилось за тиждень у Meta Ads і Google Ads і на що звернути увагу. Використовуй ЛИШЕ числа з
наданого JSON, не рахуй нових (жодних власних відсотків чи сум). Зміни бери з полів change_vs_prev_week_pct
і acos_change_percentage_points. Напрям пиши словом, а число без знака: «впали на 29.0%», не «впали на -29.0%».
Виручку з реклами (value, revenue) називай «виручка». Валюта долари, знак $ перед числом. ACOS це частка витрат на рекламу від
виручки, не «вартість продажу».
Без вступу, без списків, без порад, яких немає в даних."""


def write_summary(chat: Chat, facts: dict) -> tuple[str, dict]:
    text, usage = chat.complete([{"role": "system", "content": REPORT_SYSTEM},
                                 {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
                                json_mode=False)
    return text.strip(), usage


_NUM = re.compile(r"(?<![\w.])-?\d[\d\s  ]*(?:[.,]\d+)?")
_TYPO = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "−": "-", "—": "-",
                       " ": " ", " ": " ", " ": " "})


def _numbers_in(obj: Any, out: list[float], depth: int = 0) -> None:
    if depth > 8 or isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out, depth + 1)


def check_numbers(text: str, facts: dict) -> dict:
    """Кожне число в тексті має бути в фактах з точністю до округлення написаного розряду.
    Маленькі цілі до 10 (\"3 дні\") і роки не перевіряються."""
    known: list[float] = []
    _numbers_in(facts, known)
    text = re.sub(r"\b20\d\d-\d\d-\d\d\b", " ", text.translate(_TYPO))
    checked, bad = 0, []
    for m in _NUM.finditer(text):
        tok = m.group().strip()
        try:
            v = float(re.sub(r"\s", "", tok).replace(",", "."))
        except ValueError:
            continue
        if v.is_integer() and (0 <= v <= 10 or 2020 <= v <= 2030):
            continue
        checked += 1
        frac = re.search(r"[.,](\d+)$", tok)
        tol = 0.5 * 10 ** -len(frac.group(1)) if frac else 0.5
        # Порівнюємо модулі: «впали на 22.7%» у даних лежить як -22.7. Напрям («зросли/впали»)
        # ця перевірка не контролює, це обмеження, описане в README.
        if not any(abs(abs(v) - abs(k)) <= max(tol + 1e-9, abs(k) * 0.005 if abs(k) >= 1000 else 0) for k in known):
            bad.append(tok)
    return {"checked": checked, "unverified": bad}
