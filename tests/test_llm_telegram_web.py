"""Модель (фейкова), Telegram (фейковий Bot API) і HTTP-ендпоінти."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from adwatch import llm, pipeline, telegram
from tests.conftest import AS_OF


class FakeChat:
    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    def complete(self, messages, json_mode):
        self.seen.append(messages)
        a = self.answers.pop(0)
        return (a(messages) if callable(a) else a), {"prompt_tokens": 1000, "completion_tokens": 100}


def test_term_judge_repairs_invalid_json_once():
    good = json.dumps({"items": [{"term": "hose timer app", "negative": True, "reason": "шукають застосунок"}]})
    chat = FakeChat(["not json", good])
    out, usage = llm.judge_terms(chat, ["hose timer app"])
    assert out["hose timer app"].negative and usage["prompt_tokens"] == 2000
    assert "Invalid answer" in chat.seen[1][-1]["content"]


def test_term_judge_fails_after_repair_budget():
    with pytest.raises(llm.LLMError):
        llm.judge_terms(FakeChat(["{}", "{}"]), ["x"])


def test_check_numbers_catches_invented_figure():
    facts = {"meta": {"spend": 4661.58, "roas": 2.54}}
    assert llm.check_numbers("Витрати $4661.58, ROAS 2.54.", facts)["unverified"] == []
    assert llm.check_numbers("ROAS впав на 30%.", facts)["unverified"] == ["30"]
    # типографський мінус U+2212 і модуль: «−22.7 %» збігається з -22.7 у даних
    assert llm.check_numbers("Покупки −22.7 %", {"pct": -22.7})["unverified"] == []


def _judge_all(messages):
    """Фейкова модель: вважає мінус-словом усе, де є 'app' або 'free'."""
    terms = json.loads(messages[-1]["content"])["terms"]
    return json.dumps({"items": [{"term": t, "negative": "app" in t or "free" in t, "reason": "тест"} for t in terms]})


def test_pipeline_llm_judges_all_terms_sales_guard_and_rejects_bad_summary():
    chat = FakeChat([_judge_all, "Витрати зросли на 999%."])
    res = asyncio.run(pipeline.run(AS_OF, llm=chat, with_summary=True))
    v = {t.term: t for t in res.terms}
    assert all(t.source == "llm" for t in res.terms)
    assert v["hose timer app"].negative
    assert not v["garden hose free shipping"].negative          # модель сказала «мінус», але є продажі
    assert not v["garden hose jobs"].negative                    # модель має останнє слово над словником
    assert res.summary_source == "llm_rejected" and "999" in res.unverified
    assert res.summary.startswith("Meta:")                      # пішов шаблон


def test_button_signature_roundtrip_and_tamper():
    data = telegram.button("s3cret", "p", "238520000000007", "2026-10-10")
    assert len(data.encode()) <= 64
    assert telegram.verify("s3cret", data) == ("p", "238520000000007", "2026-10-10")
    assert telegram.verify("s3cret", data.replace("p|", "n|", 1)) is None
    assert telegram.verify("other", data) is None


def test_daily_messages_have_pause_and_negative_buttons():
    res = asyncio.run(pipeline.run(AS_OF))
    msgs = telegram.daily_messages(res, "k")
    kinds = [b["callback_data"][0] for m in msgs if m.buttons for b in m.buttons[0]]
    assert "p" in kinds and "n" in kinds and all(len(m.text) <= telegram.MAX_LEN for m in msgs)


def _bot(log):
    def h(req):
        log.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {}})
    return telegram.Bot("T", httpx.AsyncClient(transport=httpx.MockTransport(h)), base="http://tg")


def _cb(data, chat="42"):
    return {"callback_query": {"id": "1", "data": data, "from": {"first_name": "Leo"},
                               "message": {"message_id": 7, "chat": {"id": int(chat)}, "text": "Втома"}}}


def test_callback_from_other_chat_is_refused():
    log, called = [], []

    async def ex(*a):
        called.append(a)
        return "x"
    out = asyncio.run(telegram.handle_update(_cb(telegram.button("k", "p", "1", "2026-10-10"), chat="99"),
                                             bot=_bot(log), secret="k", allowed_chat="42", execute=ex))
    assert out == "forbidden_chat" and not called


def test_callback_executes_and_edits_message():
    log = []

    async def ex(kind, target, as_of):
        return f"{kind}:{target}:{as_of}"
    out = asyncio.run(telegram.handle_update(_cb(telegram.button("k", "p", "123", "2026-10-10")),
                                             bot=_bot(log), secret="k", allowed_chat="42", execute=ex))
    assert out == "done:p"
    assert [m for m, _ in log] == ["answerCallbackQuery", "editMessageText"]
    assert "p:123:2026-10-10" in log[1][1]["text"] and "reply_markup" not in log[1][1]


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "c")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "w")
    from adwatch import web as w
    return TestClient(w.app)


def test_web_state_and_feed(web):
    s = web.get("/api/state").json()
    assert s["meta_alerts"] and s["products"] and s["terms"]
    assert web.get("/api/feed.csv").text.startswith("id,custom_label_0")
    assert web.get("/mock/meta/v23.0/act_1000001?fields=spend_cap&access_token=x").json()["spend_cap"]


def test_cron_and_webhook_need_secrets(web):
    assert web.get("/api/cron/daily").status_code == 401
    assert web.get("/api/cron/daily", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert web.post("/telegram/webhook", json={}).status_code == 401
