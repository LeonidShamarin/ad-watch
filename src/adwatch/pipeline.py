"""Щоденний прогін: зібрати дані з обох кабінетів, прогнати правила, зібрати звіт.

Режим задається оточенням:
- `ADS_MODE=mock` (за замовчуванням): мок у тому ж процесі через ASGITransport;
- `ADS_MODE=live`: справжні graph.facebook.com і googleads.googleapis.com, ключі з оточення
  (META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, GADS_DEVELOPER_TOKEN, GADS_ACCESS_TOKEN,
  GADS_CUSTOMER_ID, GADS_LOGIN_CUSTOMER_ID). Правила і звіт однакові в обох режимах.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from adwatch import mockapi, rules_gads, rules_meta, world
from adwatch.gads_client import GadsClient, num
from adwatch.llm import Chat, check_numbers, cost_usd, judge_terms, write_summary
from adwatch.meta_client import DayRow, MetaClient
from adwatch.models import Alert, ProductDecision, TermVerdict


@dataclass
class Clients:
    meta: MetaClient
    gads: GadsClient
    http: httpx.AsyncClient


def make_clients(as_of: date, mock_app=None) -> Clients:
    if os.environ.get("ADS_MODE", "mock") == "live":
        http = httpx.AsyncClient()
        meta = MetaClient(http, "https://graph.facebook.com", os.environ["META_ACCESS_TOKEN"],
                          os.environ["META_AD_ACCOUNT_ID"].removeprefix("act_"))
        gads = GadsClient(http, "https://googleads.googleapis.com",
                          developer_token=os.environ["GADS_DEVELOPER_TOKEN"],
                          access_token=os.environ["GADS_ACCESS_TOKEN"],
                          customer_id=os.environ["GADS_CUSTOMER_ID"],
                          login_customer_id=os.environ.get("GADS_LOGIN_CUSTOMER_ID"))
        return Clients(meta, gads, http)
    app = mock_app or mockapi.build_app(lambda: as_of)[0]
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")
    meta = MetaClient(http, "http://mock/meta", "mock-token", world.META_ACCOUNT)
    gads = GadsClient(http, "http://mock/gads", developer_token="mock-dev", access_token="mock-access",
                      customer_id=world.GADS_CUSTOMER, login_customer_id=world.GADS_MANAGER)
    return Clients(meta, gads, http)


@dataclass
class RunResult:
    as_of: date
    meta_alerts: list[Alert]
    products: list[ProductDecision]
    terms: list[TermVerdict]
    search_campaign_id: str
    facts: dict
    summary: str = ""
    summary_source: str = "template"        # llm | template | llm_rejected
    unverified: list[str] = field(default_factory=list)
    llm_cost_usd: float = 0.0
    timings_ms: dict = field(default_factory=dict)
    meta_daily: list[DayRow] = field(default_factory=list)

    @property
    def negatives(self) -> list[str]:
        return [t.term for t in self.terms if t.negative]

    @property
    def label_changes(self) -> list[ProductDecision]:
        return [p for p in self.products if p.changed]


SHOPPING_Q = ("SELECT segments.date, segments.product_item_id, segments.product_title, "
              "segments.product_custom_attribute0, metrics.impressions, metrics.clicks, metrics.cost_micros, "
              "metrics.conversions, metrics.conversions_value FROM shopping_performance_view "
              "WHERE segments.date BETWEEN '{since}' AND '{until}'")
TERMS_Q = ("SELECT search_term_view.search_term, campaign.id, metrics.clicks, metrics.impressions, "
           "metrics.cost_micros, metrics.conversions FROM search_term_view "
           "WHERE segments.date DURING LAST_30_DAYS")


def _sum(rows, attr):
    return round(sum(getattr(r, attr) for r in rows), 2)


def weekly_facts(meta_rows: list[DayRow], gads_rows: list[dict], as_of: date) -> dict:
    def meta_week(lo, hi):
        rs = [r for r in meta_rows if lo <= (as_of - r.day).days <= hi]
        spend, purch, rev = _sum(rs, "spend"), _sum(rs, "purchases"), _sum(rs, "purchase_value")
        return {"spend": spend, "purchases": int(purch), "revenue": rev,
                "roas": round(rev / spend, 2) if spend else 0.0, "cpa": round(spend / purch, 2) if purch else 0.0}

    def gads_week(lo, hi):
        rs = [r for r in gads_rows if lo <= (as_of - date.fromisoformat(r["segments.date"])).days <= hi]
        cost = round(sum(num(r, "metrics.costMicros") for r in rs) / 1e6, 2)
        conv = round(sum(num(r, "metrics.conversions") for r in rs), 1)
        value = round(sum(num(r, "metrics.conversionsValue") for r in rs), 2)
        return {"cost": cost, "conversions": conv, "value": value,
                "acos_pct": round(cost / value * 100, 1) if value else 0.0}

    m_now, m_prev = meta_week(0, 6), meta_week(7, 13)
    g_now, g_prev = gads_week(0, 6), gads_week(7, 13)
    pct = lambda a, b: round((a - b) / b * 100, 1) if b else 0.0   # noqa: E731
    return {
        "period": f"{(as_of - timedelta(days=6)).isoformat()}..{as_of.isoformat()}",
        "meta": {"this_week": m_now, "prev_week": m_prev,
                 "change_pct": {k: pct(m_now[k], m_prev[k]) for k in ("spend", "purchases", "revenue", "roas")}},
        "google_shopping": {"this_week": g_now, "prev_week": g_prev,
                            "change_pct": {k: pct(g_now[k], g_prev[k]) for k in ("cost", "conversions", "value")}},
    }


def template_summary(f: dict, alerts: list[Alert], changes: int, negatives: int) -> str:
    m, g = f["meta"], f["google_shopping"]
    s = (f"Meta: ${m['this_week']['spend']:.2f} витрат, {m['this_week']['purchases']} покупок, "
         f"ROAS {m['this_week']['roas']:.2f} (минулого тижня {m['prev_week']['roas']:.2f}). "
         f"Google Shopping: ${g['this_week']['cost']:.2f}, ACOS {g['this_week']['acos_pct']:.1f}% "
         f"(було {g['prev_week']['acos_pct']:.1f}%). ")
    if m["this_week"]["spend"] == 0 and g["this_week"]["cost"] == 0:
        return "Цього тижня реклама не крутилась: витрат немає ні в Meta, ні в Google."
    return s + f"Проблем у Meta: {len(alerts)}, змін ставок у Google: {changes}, кандидатів у мінус-слова: {negatives}."


async def run(as_of: date | None = None, *, clients: Clients | None = None, llm: Chat | None = None,
              llm_terms: bool = True, with_summary: bool = False) -> RunResult:
    as_of = as_of or world.default_as_of()
    own = clients is None
    c = clients or make_clients(as_of)
    t0 = time.perf_counter()
    try:
        since = as_of - timedelta(days=world.DAYS - 1)
        meta_rows = await c.meta.insights_daily(since, as_of)
        statuses = await c.meta.ads()
        account = await c.meta.account()
        t1 = time.perf_counter()
        gads_rows = await c.gads.search(SHOPPING_Q.format(since=(as_of - timedelta(days=29)).isoformat(),
                                                          until=as_of.isoformat()))
        term_rows = await c.gads.search(TERMS_Q)
        t2 = time.perf_counter()
    finally:
        if own:
            await c.http.aclose()

    alerts = rules_meta.run_all(meta_rows, statuses, account, as_of)
    products = rules_gads.decide_all(gads_rows, as_of)
    plan, unclear = rules_gads.judge_terms(term_rows)
    search_cid = str(next((r.get("campaign.id") for r in term_rows if r.get("campaign.id")), ""))
    llm_cost = 0.0
    if llm and llm_terms and plan.verdicts:
        # Замір (eval/results.json, terms): модель на всіх запитах 85% проти 66% у словника,
        # бо словник не бачить «rental», «aquarium», «song». Поверх моделі правило в коді:
        # запит, що вже продавав, мінус-словом не стає.
        try:
            judged, usage = judge_terms(llm, [v.term for v in plan.verdicts])
            llm_cost += cost_usd(usage)
            for v in plan.verdicts:
                j = judged[v.term]
                v.negative, v.source, v.reason = j.negative and v.conversions == 0, "llm", j.reason
                if j.negative and v.conversions > 0:
                    v.reason += " (але є продажі, тому лишаємо)"
        except Exception as e:                      # модель не валить прогін: лишаються правила
            for v in unclear:
                v.reason += f" (модель недоступна: {type(e).__name__})"
    facts = weekly_facts(meta_rows, gads_rows, as_of)
    res = RunResult(as_of, alerts, products, plan.verdicts, search_cid, facts, meta_daily=meta_rows)
    res.summary = template_summary(facts, alerts, len(res.label_changes), len(res.negatives))
    if with_summary and llm:
        try:
            text, usage = write_summary(llm, facts)
            llm_cost += cost_usd(usage)
            chk = check_numbers(text, facts)
            if chk["unverified"]:
                res.summary_source, res.unverified = "llm_rejected", chk["unverified"]
            else:
                res.summary, res.summary_source = text, "llm"
        except Exception:
            res.summary_source = "template"
    res.llm_cost_usd = round(llm_cost, 6)
    res.timings_ms = {"meta_api": round((t1 - t0) * 1000), "google_api": round((t2 - t1) * 1000),
                      "total": round((time.perf_counter() - t0) * 1000)}
    return res
