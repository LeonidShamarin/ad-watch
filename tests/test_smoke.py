"""Наскрізний прогін на моку: клієнти -> мок API -> правила -> звіт."""

import asyncio
from datetime import date

from adwatch import pipeline

AS_OF = date(2026, 10, 10)


def test_pipeline_end_to_end_finds_planted_problems():
    res = asyncio.run(pipeline.run(AS_OF))
    kinds = {a.kind for a in res.meta_alerts}
    assert {"creative_fatigue", "cost_spike", "zero_delivery", "low_balance"} <= kinds, kinds
    assert res.label_changes, "мають бути зміни міток у Google Shopping"
    assert "garden hose jobs" in res.negatives
    assert res.facts["meta"]["this_week"]["spend"] > 0
    print({a.kind: a.entity_name for a in res.meta_alerts})
    print([(p.item_id, p.rule, p.label_from, p.label_to) for p in res.products])
    print([(t.term, t.negative, t.source) for t in res.terms])
    print(res.summary, res.timings_ms)
