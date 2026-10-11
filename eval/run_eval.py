"""Заміри для README. Запуск: .\\scripts\\py_capped.ps1 eval\\run_eval.py  (без мережі: meta + google)
                         .\\scripts\\py_capped.ps1 eval\\run_eval.py -ScriptArgs terms -Network  (з Groq)

meta   — 300 синтетичних кабінетів з випадковими проблемами: знайдено / пропущено / хибні тривоги,
         правила зі статистичним тестом проти правил «лише відсоток».
google — 100 магазинів: точність кроку ставки за типом товару для трьох версій правил.
terms  — розмічені пошукові запити: правила, правила + модель, лише модель.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adwatch import mock_gads, rules_gads, rules_meta, world  # noqa: E402
from adwatch.gads_client import flatten  # noqa: E402
from adwatch.meta_client import DayRow  # noqa: E402

AS_OF = date(2026, 10, 10)
OUT = ROOT / "eval" / "results.json"


def _meta_rows(w: world.MetaWorld) -> list[DayRow]:
    a = {x.id: x for x in w.ads}
    return [DayRow(r.day, r.ad_id, a[r.ad_id].name, a[r.ad_id].campaign_id, a[r.ad_id].campaign_name,
                   r.impressions, r.reach, r.clicks, r.spend, r.purchases, r.purchase_value)
            for r in w.rows if r.impressions or r.spend]


def eval_meta(n: int = 300) -> dict:
    """Кожен тип проблеми закладається з імовірністю 0.5 у випадкове оголошення/кампанію
    з випадковою силою. Правильне спрацювання = той самий тип і та сама сутність."""
    configs = {"final": rules_meta.MetaRules(),
               "pct_only": rules_meta.MetaRules(fatigue_z=0.0, spike_p=1.0)}
    kinds = {"fatigue": "creative_fatigue", "cost_spike": "cost_spike", "zero_delivery": "zero_delivery",
             "low_balance": "low_balance"}
    stats = {c: defaultdict(Counter) for c in configs}
    clean_false = {c: 0 for c in configs}
    clean_n = 0
    for seed in range(n):
        rng = random.Random(seed)
        ads = world.build_meta(AS_OF, seed=seed, incidents=[]).ads
        inc = []
        if rng.random() < 0.5:
            inc.append(world.Incident("fatigue", rng.choice(ads).id,
                                      {"days": rng.randint(8, 14), "depth": rng.uniform(0.35, 0.65)}))
        if rng.random() < 0.5:
            inc.append(world.Incident("cost_spike", rng.choice(ads).campaign_id,
                                      {"days": rng.randint(3, 5), "factor": rng.uniform(1.9, 3.0)}))
        if rng.random() < 0.5:
            inc.append(world.Incident("zero_delivery", rng.choice(ads).id, {"days": rng.randint(2, 4)}))
        if rng.random() < 0.5:
            inc.append(world.Incident("low_balance", "account", {"days": rng.uniform(0.8, 2.8)}))
        w = world.build_meta(AS_OF, seed=seed, incidents=inc)
        rows = _meta_rows(w)
        acc = {"spend_cap_cents": w.spend_cap_cents, "amount_spent_cents": w.amount_spent_cents}
        planted = {(kinds[i.kind], i.entity) for i in inc}
        clean_n += not inc
        for cname, cfg in configs.items():
            found = {(a.kind, a.entity_id) for a in rules_meta.run_all(rows, {x.id: x.status for x in w.ads}, acc, AS_OF, cfg)}
            for k in kinds.values():
                p = {x for x in planted if x[0] == k}
                f = {x for x in found if x[0] == k}
                stats[cname][k]["tp"] += len(p & f)
                stats[cname][k]["fn"] += len(p - f)
                stats[cname][k]["fp"] += len(f - p)
            if not inc:
                clean_false[cname] += len(found)
    out = {"accounts": n, "clean_accounts": clean_n}
    for cname in configs:
        rows = {}
        for k, s in stats[cname].items():
            rows[k] = {**s, "recall": round(s["tp"] / max(s["tp"] + s["fn"], 1), 3),
                       "precision": round(s["tp"] / max(s["tp"] + s["fp"], 1), 3)}
        out[cname] = {"by_kind": rows, "false_alerts_on_clean_accounts": clean_false[cname]}
    return out


EXPECTED = {"steady": {"keep"}, "winner": {"winner"}, "expensive": {"expensive"}, "burner": {"burner", "click_sink"},
            "click_sink": {"burner", "click_sink"}, "low_ctr": {"low_ctr", "keep"}, "invisible": {"invisible"},
            "recovering": {"release"}, "dead": {"dead"}}

Q = ("SELECT segments.date, segments.product_item_id, segments.product_title, segments.product_custom_attribute0, "
     "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, metrics.conversions_value "
     "FROM shopping_performance_view WHERE segments.date BETWEEN '{s}' AND '{u}'")


def eval_google(n: int = 100) -> dict:
    final = rules_gads.GadsRules()
    configs = {
        "naive_7d": replace(final, acos_window=7, min_conv_down=1, min_conv_up=2, expensive_band=1.0,
                            sale_tolerance=False, burn_sales=0.0),
        "window_14d": replace(final, sale_tolerance=False),
        "final_14d_tolerance": final,
    }
    res = {c: Counter() for c in configs}
    per_arch = {c: defaultdict(Counter) for c in configs}
    for seed in range(n):
        as_of = AS_OF - timedelta(days=seed % 20)
        w = world.build_gads(as_of, seed=seed)
        rows = [flatten(r) for r in mock_gads.run_query(
            w, Q.format(s=(as_of - timedelta(days=29)).isoformat(), u=as_of.isoformat()), {})]
        arch = {p.item_id: p.archetype for p in w.products}
        win = rules_gads.product_windows(rows, as_of)
        for cname, cfg in configs.items():
            for item, a in win.items():
                d = rules_gads.decide(item, a, cfg)
                ok = d.rule in EXPECTED[arch[item]]
                res[cname]["ok" if ok else "wrong"] += 1
                res[cname]["changes"] += d.changed
                per_arch[cname][arch[item]]["ok" if ok else "wrong"] += 1
                if arch[item] == "steady":
                    res[cname]["steady_total"] += 1
                    res[cname]["steady_changed"] += d.changed
    out = {"shops": n, "products": n * 40}
    for c in configs:
        r = res[c]
        out[c] = {"accuracy": round(r["ok"] / (r["ok"] + r["wrong"]), 3),
                  "steady_wrongly_changed": round(r["steady_changed"] / r["steady_total"], 3),
                  "label_changes_per_shop": round(r["changes"] / n, 1),
                  "by_archetype": {k: round(v["ok"] / (v["ok"] + v["wrong"]), 3) for k, v in sorted(per_arch[c].items())}}
    return out


def eval_terms() -> dict:
    from adwatch.llm import GroqChat, cost_usd, judge_terms
    data = json.loads((ROOT / "eval" / "terms_labeled.json").read_text(encoding="utf-8"))["items"]
    rows = [{"searchTermView.searchTerm": d["term"], "metrics.clicks": str(d["clicks"]),
             "metrics.conversions": d["conversions"], "metrics.costMicros": str(int(d["clicks"] * 0.6e6))} for d in data]
    truth = {d["term"]: d["negative"] for d in data}
    traps = {d["term"] for d in data if d.get("trap")}
    plan, unclear = rules_gads.judge_terms(rows)
    rules = {v.term: v.negative for v in plan.verdicts}
    key = os.environ.get("GROQ_API_KEY", "").strip()
    out = {"terms": len(data), "traps": len(traps), "rules": _score(rules, truth, traps)}
    if not key:
        out["llm"] = "skipped: GROQ_API_KEY not set"
        return out
    chat = GroqChat(key)
    variants = {"rules_plus_llm_on_unclear": [u.term for u in unclear],
                "rules_plus_llm_on_all_candidates": [v.term for v in plan.verdicts if v.negative or v.source == "pattern"],
                "llm_only": list(truth)}
    for name, todo in variants.items():
        t0 = time.perf_counter()
        judged, usage = judge_terms(chat, todo)
        pred = dict(rules) if name != "llm_only" else {}
        pred.update({t: j.negative for t, j in judged.items()})
        if name == "llm_only":
            # той самий прогін моделі + правило коду «запит із продажами не мінусуємо» (так у pipeline)
            conv = {d["term"]: d["conversions"] for d in data}
            guarded = {t: v and conv[t] == 0 for t, v in pred.items()}
            out["llm_all_plus_sales_guard"] = {**_score(guarded, truth, traps), "llm_terms": len(todo),
                                               "errors": sorted(t for t in truth if guarded[t] != truth[t])}
        out[name] = {**_score(pred, truth, traps), "llm_terms": len(todo), "seconds": round(time.perf_counter() - t0, 1),
                     "cost_usd": round(cost_usd(usage), 5),
                     "errors": sorted(t for t in truth if pred.get(t) != truth[t])}
    out["rules"]["errors"] = sorted(t for t in truth if rules[t] != truth[t])
    return out


def _score(pred: dict, truth: dict, traps: set) -> dict:
    tp = sum(pred[t] and truth[t] for t in truth)
    fp = sum(pred[t] and not truth[t] for t in truth)
    fn = sum(not pred[t] and truth[t] for t in truth)
    return {"accuracy": round(sum(pred[t] == truth[t] for t in truth) / len(truth), 3),
            "precision": round(tp / max(tp + fp, 1), 3), "recall": round(tp / max(tp + fn, 1), 3),
            "traps_correct": f"{sum(pred[t] == truth[t] for t in traps)}/{len(traps)}"}


if __name__ == "__main__":
    which = sys.argv[1:] or ["meta", "google"]
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    t0 = time.perf_counter()
    if "meta" in which:
        results["meta"] = eval_meta()
    if "google" in which:
        results["google"] = eval_google()
    if "terms" in which:
        results["terms"] = eval_terms()
    results["_generated"] = {"as_of": AS_OF.isoformat(), "seconds": round(time.perf_counter() - t0, 1)}
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in results.items() if k in which}, ensure_ascii=False, indent=1)[:6000])
