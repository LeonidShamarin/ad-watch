"""Збережені прогони для дашборду.

Демо не запускає модель за розкладом і на кожен перегляд сторінки: прогони за кілька днів
зроблено заздалегідь (`scripts/make_runs.py`) і лежать у `static/runs/*.json`. Сторінка лише
читає їх, тож перегляд дашборду не витрачає ні запитів до моделі, ні токенів.
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

RUNS = Path(__file__).resolve().parents[2] / "static" / "runs"


def to_json(res) -> dict:
    series: dict[str, list] = {}
    fatigued = {a.entity_id for a in res.meta_alerts if a.kind == "creative_fatigue"}
    for r in res.meta_daily:
        if r.ad_id in fatigued:
            series.setdefault(r.ad_id, []).append([r.day.isoformat(), r.impressions, r.clicks])
    return {
        "as_of": res.as_of.isoformat(),
        "facts": res.facts,
        "summary": res.summary,
        "summary_source": res.summary_source,
        "unverified": res.unverified,
        "llm_text": res.llm_text,
        "meta_alerts": [a.model_dump() for a in res.meta_alerts],
        "products": [p.model_dump() | {"changed": p.changed} for p in res.products],
        "terms": [t.model_dump() for t in res.terms],
        "search_campaign_id": res.search_campaign_id,
        "series": series,
        "timings_ms": res.timings_ms,
        "llm_cost_usd": res.llm_cost_usd,
    }


@lru_cache(maxsize=1)
def index() -> list[dict]:
    p = RUNS / "index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def dates() -> list[str]:
    return [r["as_of"] for r in index()]


@lru_cache(maxsize=32)
def load(as_of: str) -> dict | None:
    if as_of not in dates():
        return None
    return json.loads((RUNS / f"{as_of}.json").read_text(encoding="utf-8"))


def latest() -> date | None:
    d = dates()
    return date.fromisoformat(d[-1]) if d else None
