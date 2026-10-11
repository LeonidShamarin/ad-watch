"""Правила для Meta Ads: що зламалось у кабінеті за останні дні.

Кожне правило має дві умови: «помітно» (відсоток зміни) і «не випадково»
(статистичний тест на рахунках кліків чи покупок). Без другої умови маленьке
оголошення з 40 кліками на день піднімало б тривогу через звичайний шум.
Пороги винесені в `MetaRules`, щоб їх можна було підкрутити під кабінет.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from adwatch.meta_client import DayRow
from adwatch.models import Action, Alert


@dataclass(frozen=True)
class MetaRules:
    recent_days: int = 3            # «останні дні», з якими порівнюємо
    baseline_from: int = 8          # база: дні з 8-го по 28-й назад (без свіжого тижня)
    baseline_to: int = 28
    fatigue_drop: float = 0.30      # CTR упав щонайменше на 30%
    fatigue_z: float = 3.0
    fatigue_min_recent_impr: int = 3000
    spike_ratio: float = 1.8        # ціна покупки зросла щонайменше в 1.8 раза
    spike_p: float = 0.01
    spike_min_base_purchases: int = 10
    zero_days: int = 2
    zero_min_base_impr: float = 500.0
    runway_warn_days: float = 3.0
    runway_high_days: float = 2.0


def _window(rows: list[DayRow], as_of: date, lo: int, hi: int) -> list[DayRow]:
    """Рядки, що лежать від lo до hi днів назад включно (0 = as_of)."""
    return [r for r in rows if lo <= (as_of - r.day).days <= hi]


def _sum(rows: list[DayRow], attr: str) -> float:
    return sum(getattr(r, attr) for r in rows)


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) для Пуассона. Цикл обмежений k+1 кроками; для великих k нормальне наближення."""
    if lam <= 0:
        return 1.0
    if k > 200:
        return 0.5 * (1 + math.erf((k + 0.5 - lam) / math.sqrt(2 * lam)))
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(total, 1.0)


def two_prop_z(x1: float, n1: float, x2: float, n2: float) -> float:
    """z-статистика різниці двох частот (база мінус свіже)."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) if 0 < p < 1 else 0.0
    return (x1 / n1 - x2 / n2) / se if se else 0.0


def fatigue(rows: list[DayRow], as_of: date, cfg: MetaRules = MetaRules()) -> list[Alert]:
    by_ad: dict[str, list[DayRow]] = defaultdict(list)
    for r in rows:
        by_ad[r.ad_id].append(r)
    out = []
    for ad_id, rs in by_ad.items():
        recent = _window(rs, as_of, 0, cfg.recent_days - 1)
        base = _window(rs, as_of, cfg.baseline_from - 1, cfg.baseline_to - 1)
        ri, rc = _sum(recent, "impressions"), _sum(recent, "clicks")
        bi, bc = _sum(base, "impressions"), _sum(base, "clicks")
        if ri < cfg.fatigue_min_recent_impr or bi <= 0 or bc <= 0:
            continue
        ctr_r, ctr_b = rc / ri, bc / bi
        drop = 1 - ctr_r / ctr_b
        z = two_prop_z(bc, bi, rc, ri)
        if drop < cfg.fatigue_drop or z < cfg.fatigue_z:
            continue
        freq = ri / max(_sum(recent, "reach"), 1)
        name = rs[0].ad_name
        out.append(Alert(
            source="meta", kind="creative_fatigue", severity="high" if drop >= 0.45 else "medium",
            entity_id=ad_id, entity_name=name,
            title=f"Втома креативу: «{name}»",
            details=(f"CTR за {cfg.recent_days} дні {ctr_r * 100:.2f}% проти {ctr_b * 100:.2f}% у базі "
                     f"(-{drop * 100:.0f}%), частота {freq:.1f}. Варто замінити креатив або призупинити."),
            metrics={"ctr_recent": round(ctr_r * 100, 2), "ctr_base": round(ctr_b * 100, 2),
                     "drop_pct": round(drop * 100), "frequency": round(freq, 1), "z": round(z, 1)},
            action=Action(kind="pause_ad", target=ad_id, payload={"ad_name": name}),
        ))
    return out


def cost_spike(rows: list[DayRow], as_of: date, cfg: MetaRules = MetaRules()) -> list[Alert]:
    by_c: dict[str, list[DayRow]] = defaultdict(list)
    for r in rows:
        by_c[r.campaign_id].append(r)
    out = []
    for cid, rs in by_c.items():
        recent = _window(rs, as_of, 0, cfg.recent_days - 1)
        base = _window(rs, as_of, cfg.baseline_from - 1, cfg.baseline_to - 1)
        bp, bs = _sum(base, "purchases"), _sum(base, "spend")
        rp, rsp = _sum(recent, "purchases"), _sum(recent, "spend")
        if bp < cfg.spike_min_base_purchases or rsp <= 0:
            continue
        cpa_b = bs / bp
        expected = rsp / cpa_b                        # скільки покупок мало б бути за ці гроші
        p = poisson_cdf(int(rp), expected)
        cpa_r = rsp / rp if rp else float("inf")
        if cpa_r < cfg.spike_ratio * cpa_b or p > cfg.spike_p:
            continue
        name = rs[0].campaign_name
        cpa_txt = f"${cpa_r:.2f}" if rp else "покупок 0"
        out.append(Alert(
            source="meta", kind="cost_spike", severity="high",
            entity_id=cid, entity_name=name,
            title=f"Покупка подорожчала: «{name}»",
            details=(f"За {cfg.recent_days} дні ${rsp:.2f} витрат і {int(rp)} покупок ({cpa_txt}) "
                     f"проти ${cpa_b:.2f} за покупку в базі; очікувалось ~{expected:.0f}. "
                     "Перевірте сторінку, ціну, наявність і трекінг покупок."),
            metrics={"spend_recent": round(rsp, 2), "purchases_recent": rp, "cpa_base": round(cpa_b, 2),
                     "expected_purchases": round(expected, 1), "p_value": round(p, 4)},
        ))
    return out


def zero_delivery(rows: list[DayRow], statuses: dict[str, str], names: dict[str, str], as_of: date,
                  cfg: MetaRules = MetaRules()) -> list[Alert]:
    out = []
    for ad_id, status in statuses.items():
        if status != "ACTIVE":
            continue
        rs = [r for r in rows if r.ad_id == ad_id]
        recent = _window(rs, as_of, 0, cfg.zero_days - 1)
        base = _window(rs, as_of, cfg.zero_days, cfg.zero_days + 13)
        avg = _sum(base, "impressions") / 14
        if _sum(recent, "impressions") == 0 and avg >= cfg.zero_min_base_impr:
            name = names.get(ad_id, ad_id)
            out.append(Alert(
                source="meta", kind="zero_delivery", severity="high", entity_id=ad_id, entity_name=name,
                title=f"Оголошення активне, але без показів: «{name}»",
                details=(f"{cfg.zero_days} дні 0 показів при статусі ACTIVE, раніше ~{avg:.0f} на день. "
                         "Зазвичай це відхилене оголошення, вичерпаний ліміт ad set або проблема з оплатою."),
                metrics={"impr_recent": 0, "impr_base_avg": round(avg)},
            ))
    return out


def spend_runway(rows: list[DayRow], account: dict, as_of: date, cfg: MetaRules = MetaRules()) -> list[Alert]:
    cap = account.get("spend_cap_cents", 0)
    if not cap:
        return []                                     # ліміту немає: рахувати нічого
    remaining = (cap - account.get("amount_spent_cents", 0)) / 100
    daily = _sum(_window(rows, as_of, 0, 6), "spend") / 7
    if daily <= 0:
        return []
    runway = remaining / daily
    if runway >= cfg.runway_warn_days:
        return []
    return [Alert(
        source="meta", kind="low_balance", severity="high" if runway < cfg.runway_high_days else "medium",
        entity_id="account", entity_name=account.get("name") or "Рекламний акаунт",
        title="Гроші на акаунті закінчуються",
        details=(f"До ліміту витрат лишилось ${remaining:.2f}, це ~{runway:.1f} дня при середніх "
                 f"${daily:.2f} на день. Після цього всі кампанії зупиняться."),
        metrics={"remaining": round(remaining, 2), "daily_spend": round(daily, 2), "runway_days": round(runway, 1)},
    )]


def run_all(rows: list[DayRow], statuses: dict[str, str], account: dict, as_of: date,
            cfg: MetaRules = MetaRules()) -> list[Alert]:
    names = {r.ad_id: r.ad_name for r in rows}
    alerts = (spend_runway(rows, account, as_of, cfg) + zero_delivery(rows, statuses, names, as_of, cfg)
              + cost_spike(rows, as_of, cfg) + fatigue(rows, as_of, cfg))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(alerts, key=lambda a: order[a.severity])
