"""Модель для двох вузьких задач: оцінка сумнівних пошукових запитів і текст тижневого звіту.

Обидві задачі не вирішують нічого самі: запити лише пропонуються людині, а числа звіту
рахує код і перевіряє `check_numbers` (перенесено з Shop Pulse). Модель — Groq
`openai/gpt-oss-120b`, бо ключ і ліміти вже перевірені в Shop Pulse.
"""

from __future__ import annotations

import json
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
наданого JSON, не рахуй нових (жодних власних відсотків чи сум). Валюта долари, знак $ перед числом.
Без вступу, без списків, без порад, яких немає в даних."""


def write_summary(chat: Chat, facts: dict) -> tuple[str, dict]:
    text, usage = chat.complete([{"role": "system", "content": REPORT_SYSTEM},
                                 {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
                                json_mode=False)
    return text.strip(), usage


_NUM = re.compile(r"(?<![\w.])-?\d[\d\s  ]*(?:[.,]\d+)?")
_TYPO = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-",
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
        if not any(abs(v - k) <= max(tol + 1e-9, abs(k) * 0.005 if abs(k) >= 1000 else 0) for k in known):
            bad.append(tok)
    return {"checked": checked, "unverified": bad}
