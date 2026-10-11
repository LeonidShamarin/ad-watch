"""Правила для Google Ads: кроки ставок для товарів Shopping і мінус-слова.

Кроки ставок. У Shopping-кампанії групи товарів поділені за `custom_label_0`, і кожна
група має свою ставку: 10 — низька, 35 — максимальна, 5 — карантин (мінімальна ставка),
99 — виключено. Правило не міняє ставку в кабінеті, воно міняє мітку товару.
Мітки живуть у Merchant Center, а не в Google Ads API, тому результат — додатковий
фід (CSV: id, custom_label_0), який Merchant Center забирає за розкладом.

Мінус-слова. Спершу словник явних ознак (робота, безкоштовно, ремонт, вживане,
бренди, яких магазин не продає), потім запити, що їдять гроші без продажів. Словник
помиляється на запитах на кшталт «free shipping», тому кандидати, які не очевидні,
можна віддати моделі (`term_judge.py`), а додає мінус-слова тільки людина.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from adwatch.gads_client import num
from adwatch.models import ProductDecision, TermVerdict

LADDER = [5, 10, 15, 20, 25, 30, 35]
QUARANTINE, EXCLUDED = 5, 99


@dataclass(frozen=True)
class GadsRules:
    target_acos: float = 0.15
    burn_cost: float = 10.0          # 14 днів: витратив стільки без продажу (коли середній чек невідомий)
    burn_sales: float = 2.0          # або: витратив вартість двох продажів за цільовим ACOS
    burn_clicks: int = 30            # 14 днів: стільки кліків без продажу
    acos_window: int = 14            # вікно для правил ACOS
    min_conv_down: int = 2           # «дорого» лише від двох продажів: одна покупка це шум
    min_conv_up: int = 3
    expensive_band: float = 1.2      # дорожче цілі на 20%+, щоб товар не стрибав біля межі
    winner_acos_share: float = 0.6   # ACOS нижче 60% цільового: можна дати більше показів
    sale_tolerance: bool = True      # рішення має витримати ±1 продаж (інакше це шум малих чисел)
    invisible_impr: int = 100        # 7 днів: менше показів — ставка не проходить аукціон
    low_ctr_impr: int = 500          # 7 днів: показів багато, а кліків < 5 — проблема в картці
    dead_clicks: int = 10
    term_min_clicks: int = 15
    term_min_cost: float = 10.0
    blocked_brands: tuple[str, ...] = ("gardena", "karcher", "kärcher", "hozelock")


def _step(label: int, delta: int) -> int:
    if label not in LADDER:
        return label
    i = max(0, min(len(LADDER) - 1, LADDER.index(label) + delta))
    return LADDER[max(i, 1)] if label != QUARANTINE else LADDER[i]


def product_windows(rows: list[dict], as_of: date) -> dict[str, dict]:
    """Звести рядки shopping_performance_view по товару у вікна 7/14/30 днів."""
    acc: dict[str, dict] = defaultdict(lambda: {"title": "", "label": 0, **{f"{m}_{w}": 0.0 for w in (7, 14, 30)
                                                for m in ("impr", "clicks", "cost", "conv", "value")}})
    for r in rows:
        item = r.get("segments.productItemId")
        if not item:
            continue
        back = (as_of - date.fromisoformat(r["segments.date"])).days
        a = acc[item]
        a["title"] = r.get("segments.productTitle", item)
        a["label"] = int(r.get("segments.productCustomAttribute0") or 0)
        vals = {"impr": num(r, "metrics.impressions"), "clicks": num(r, "metrics.clicks"),
                "cost": num(r, "metrics.costMicros") / 1_000_000, "conv": num(r, "metrics.conversions"),
                "value": num(r, "metrics.conversionsValue")}
        for w in (7, 14, 30):
            if 0 <= back < w:
                for m, v in vals.items():
                    a[f"{m}_{w}"] += v
    return dict(acc)


def decide(item: str, a: dict, cfg: GadsRules = GadsRules()) -> ProductDecision:
    lbl = a["label"]

    def acos(w: int) -> float:
        return a[f"cost_{w}"] / a[f"value_{w}"] if a[f"value_{w}"] else float("inf")

    def d(to: int, rule: str, reason: str) -> ProductDecision:
        m = {k: round(v, 2) for k, v in a.items() if isinstance(v, float)}
        return ProductDecision(item_id=item, title=a["title"], label_from=lbl, label_to=to, rule=rule,
                               reason=reason, metrics=m)

    if lbl == EXCLUDED:
        return d(lbl, "excluded", "Виключений товар не чіпаємо автоматично.")
    if lbl == QUARANTINE:
        if a["conv_30"] >= 1 and acos(30) <= cfg.target_acos:
            return d(10, "release", f"Карантин: за 30 днів {a['conv_30']:.0f} продажів, ACOS {acos(30) * 100:.0f}%. Повертаємо.")
        if a["clicks_30"] < cfg.dead_clicks and a["conv_30"] == 0:
            return d(EXCLUDED, "dead", f"Карантин без руху: {a['clicks_30']:.0f} кліків і 0 продажів за 30 днів.")
        return d(lbl, "hold", "Ще в карантині, даних для рішення мало.")
    aov = a["value_30"] / a["conv_30"] if a["conv_30"] else 0.0
    burn_at = max(cfg.burn_cost, cfg.burn_sales * cfg.target_acos * aov) if aov else cfg.burn_cost
    if a["conv_14"] == 0 and a["cost_14"] >= burn_at:
        return d(QUARANTINE, "burner", f"14 днів: ${a['cost_14']:.2f} витрат і 0 продажів (поріг ${burn_at:.2f}).")
    if a["conv_14"] == 0 and a["clicks_14"] >= cfg.burn_clicks and not aov:
        return d(QUARANTINE, "click_sink", f"14 днів: {a['clicks_14']:.0f} кліків і 0 продажів.")
    w = cfg.acos_window
    conv, cost, value = a[f"conv_{w}"], a[f"cost_{w}"], a[f"value_{w}"]
    unit = value / conv if conv else 0.0
    # Допуск в один продаж: «дорого» мусить лишитись дорогим і з ще одним продажем,
    # «вигідно» мусить лишитись вигідним і без одного продажу.
    acos_hi = cost / (value + unit) if cfg.sale_tolerance and unit else acos(w)
    acos_lo = cost / (value - unit) if cfg.sale_tolerance and conv > 1 else acos(w)
    if conv >= cfg.min_conv_down and acos_hi > cfg.target_acos * cfg.expensive_band:
        return d(_step(lbl, -1), "expensive", f"ACOS {acos(w) * 100:.0f}% за {w} днів при цілі "
                                              f"{cfg.target_acos * 100:.0f}%: ставку на крок нижче.")
    if conv >= cfg.min_conv_up and acos_lo < cfg.target_acos * cfg.winner_acos_share and lbl < LADDER[-1]:
        return d(_step(lbl, +1), "winner", f"ACOS {acos(w) * 100:.0f}% і {a[f'conv_{w}']:.0f} продажів за {w} днів: "
                                           "даємо більше показів.")
    if a["impr_7"] < cfg.invisible_impr and lbl < LADDER[-1]:
        return d(_step(lbl, +1), "invisible", f"Лише {a['impr_7']:.0f} показів за 7 днів: ставка не проходить аукціон.")
    if a["impr_7"] >= cfg.low_ctr_impr and a["clicks_7"] < 5:
        return d(lbl, "low_ctr", f"{a['impr_7']:.0f} показів і {a['clicks_7']:.0f} кліків: ставка тут не допоможе, "
                                 "перевірте фото, назву і ціну.")
    return d(lbl, "keep", "У нормі.")


def decide_all(rows: list[dict], as_of: date, cfg: GadsRules = GadsRules()) -> list[ProductDecision]:
    return [decide(i, a, cfg) for i, a in sorted(product_windows(rows, as_of).items())]


def supplemental_feed(decisions: list[ProductDecision]) -> str:
    """CSV для Merchant Center: лише товари, у яких мітка змінилась."""
    lines = ["id,custom_label_0"] + [f"{x.item_id},{x.label_to}" for x in decisions if x.changed]
    return "\n".join(lines) + "\n"


# --- мінус-слова -------------------------------------------------------------------

_PATTERNS = [
    (r"\bjobs?\b|\bcareers?\b|\bvacanc", "шукають роботу"),
    (r"\bfree\b", "шукають безкоштовне"),
    (r"\bused\b|\bsecond[- ]hand\b", "вживане"),
    (r"\brepair\b|\bfix\b|\bleak\b", "ремонт, а не покупка"),
    (r"\bmanual\b|\bpdf\b|\bdiy\b|\bwiki(pedia)?\b|\bmeme\b|\bhow to\b", "інформаційний запит"),
    (r"\bcar\b|\bradiator\b|\bfire hose\b", "інша категорія"),
]


@dataclass
class TermPlan:
    verdicts: list[TermVerdict] = field(default_factory=list)

    @property
    def negatives(self) -> list[str]:
        return [v.term for v in self.verdicts if v.negative]


def judge_terms(rows: list[dict], cfg: GadsRules = GadsRules()) -> tuple[TermPlan, list[TermVerdict]]:
    """Повертає план за правилами і список «сумнівних» (без явної ознаки, але без продажів),
    які можна віддати моделі."""
    plan, unclear = TermPlan(), []
    brand_re = re.compile(r"\b(" + "|".join(map(re.escape, cfg.blocked_brands)) + r")\b", re.I)
    for r in rows:
        term = str(r.get("searchTermView.searchTerm", "")).lower()
        clicks, conv = int(num(r, "metrics.clicks")), num(r, "metrics.conversions")
        cost = num(r, "metrics.costMicros") / 1_000_000
        base = dict(term=term, clicks=clicks, cost=round(cost, 2), conversions=conv)
        if (b := brand_re.search(term)):
            plan.verdicts.append(TermVerdict(negative=True, source="pattern",
                                             reason=f"бренд, якого немає в асортименті ({b.group(1)})", **base))
            continue
        hit = next((why for pat, why in _PATTERNS if re.search(pat, term)), None)
        if hit and conv == 0:
            plan.verdicts.append(TermVerdict(negative=True, source="pattern", reason=hit, **base))
        elif hit:
            plan.verdicts.append(TermVerdict(negative=False, source="pattern",
                                             reason=f"схоже на «{hit}», але є продажі", **base))
        elif conv == 0 and clicks >= cfg.term_min_clicks and cost >= cfg.term_min_cost:
            v = TermVerdict(negative=True, source="performance",
                            reason=f"{clicks} кліків, ${cost:.2f} і 0 продажів за 30 днів", **base)
            plan.verdicts.append(v)
            unclear.append(v)
        else:
            plan.verdicts.append(TermVerdict(negative=False, source="none", reason="працює або замало даних", **base))
    return plan, unclear
