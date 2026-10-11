"""Синтетичні рекламні кабінети: Meta Ads і Google Ads.

Справжнього кабінету з витратами в демо немає, тому дані генеруються детерміновано
від `as_of` (останній повний день) і `seed`. У дані навмисно закладені проблеми
(`Incident`), і саме їх мають знайти правила. Список закладеного віддається окремо,
щоб eval міг порахувати, що знайдено, що пропущено і що піднято хибно.

Числа живуть у тих самих одиницях, що й у справжніх API: Meta рахує бюджет акаунта
в центах, Google Ads рахує вартість у мікро (1 USD = 1 000 000 micros).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

DAYS = 60
META_ACCOUNT = "1000001"
GADS_CUSTOMER = "1234567890"
GADS_MANAGER = "9876543210"
CURRENCY = "USD"


# --- допоміжне -------------------------------------------------------------------

def _poisson(rng: random.Random, lam: float) -> int:
    """Пуассон без numpy. Для великих λ нормальне наближення, для малих Кнут із межею
    ітерацій: цикл не може крутитися довше, ніж 10·λ+50 кроків."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, p, k = math.exp(-lam), 1.0, 0
    for _ in range(int(10 * lam) + 50):
        p *= rng.random()
        if p <= limit:
            return k
        k += 1
    return k


def _noise(rng: random.Random, sigma: float) -> float:
    return math.exp(rng.gauss(0, sigma) - sigma * sigma / 2)


_WEEKDAY = [1.0, 1.02, 1.0, 0.98, 0.95, 1.08, 1.1]   # пн..нд, вихідні трохи сильніші


# --- Meta ----------------------------------------------------------------------------

@dataclass
class MetaAd:
    id: str
    name: str
    adset_id: str
    adset_name: str
    campaign_id: str
    campaign_name: str
    base_impr: float
    ctr: float
    cvr: float
    cpm: float
    aov: float
    status: str = "ACTIVE"


@dataclass
class MetaRow:
    """Один день одного оголошення, як рядок insights з level=ad, time_increment=1."""
    day: date
    ad_id: str
    impressions: int
    reach: int
    clicks: int
    spend: float
    purchases: int
    purchase_value: float


@dataclass
class Incident:
    kind: str           # fatigue | cost_spike | zero_delivery | low_balance
    entity: str         # ad_id, campaign_id або account
    detail: dict = field(default_factory=dict)


@dataclass
class MetaWorld:
    as_of: date
    ads: list[MetaAd]
    rows: list[MetaRow]
    spend_cap_cents: int
    amount_spent_cents: int
    incidents: list[Incident]

    def ad(self, ad_id: str) -> MetaAd:
        return next(a for a in self.ads if a.id == ad_id)


_META_CAMPAIGNS = [
    ("23850000000000001", "Prospecting | Broad", [("23851000000000011", "Broad 25-54")]),
    ("23850000000000002", "Retargeting | 30d", [("23851000000000021", "Site visitors 30d"),
                                                ("23851000000000022", "Add to cart 14d")]),
    ("23850000000000003", "Catalog | Advantage+", [("23851000000000031", "All products")]),
]
_AD_NAMES = ["UGC video - hose test", "Static - spring sale", "Carousel - bestsellers", "Reel - 15s unboxing",
             "Static - free delivery", "Video - customer review", "Carousel - new arrivals", "Static - bundle -20%",
             "Reel - before/after", "DPA - viewed products", "DPA - cart abandoners", "Static - gift guide"]


def _make_meta_ads(rng: random.Random) -> list[MetaAd]:
    ads, n = [], 0
    adsets = [(c_id, c_name, s_id, s_name) for c_id, c_name, sets in _META_CAMPAIGNS for s_id, s_name in sets]
    for i, name in enumerate(_AD_NAMES):
        c_id, c_name, s_id, s_name = adsets[i % len(adsets)]
        retarget = "Retargeting" in c_name
        n += 1
        ads.append(MetaAd(
            id=f"2385200000000{n:04d}", name=name, adset_id=s_id, adset_name=s_name,
            campaign_id=c_id, campaign_name=c_name,
            base_impr=rng.uniform(3000, 14000) * (0.45 if retarget else 1.0),
            ctr=rng.uniform(0.009, 0.021) * (1.3 if retarget else 1.0),
            cvr=rng.uniform(0.018, 0.035) * (1.6 if retarget else 1.0),
            cpm=rng.uniform(7.0, 12.0) * (1.4 if retarget else 1.0),
            aov=rng.uniform(38, 72),
        ))
    return ads


def build_meta(as_of: date, seed: int = 7, incidents: list[Incident] | None = None) -> MetaWorld:
    """Збудувати кабінет Meta. Без `incidents` закладається дефолтний демо-набір."""
    rng = random.Random(f"meta-{seed}")
    ads = _make_meta_ads(rng)
    if incidents is None:
        incidents = default_meta_incidents(ads)
    fatigue = {i.entity: i.detail for i in incidents if i.kind == "fatigue"}
    spikes = {i.entity: i.detail for i in incidents if i.kind == "cost_spike"}
    zeros = {i.entity: i.detail for i in incidents if i.kind == "zero_delivery"}

    start = as_of - timedelta(days=DAYS - 1)
    rows: list[MetaRow] = []
    for d in range(DAYS):
        day = start + timedelta(days=d)
        back = (as_of - day).days                      # 0 = вчора
        wk = _WEEKDAY[day.weekday()]
        for ad in ads:
            impr_f, ctr_f, cvr_f, freq = 1.0, 1.0, 1.0, 1.35
            if ad.id in fatigue:
                f = fatigue[ad.id]
                if back < f["days"]:
                    progress = (f["days"] - back) / f["days"]
                    ctr_f = 1 - f["depth"] * progress
                    freq += 2.4 * progress
            if ad.campaign_id in spikes and back < spikes[ad.campaign_id]["days"]:
                cvr_f = 1 / spikes[ad.campaign_id]["factor"]
            if ad.id in zeros and back < zeros[ad.id]["days"]:
                impr_f = 0.0
            impr = max(0, round(ad.base_impr * wk * impr_f * _noise(rng, 0.12)))
            clicks = min(impr, _poisson(rng, impr * ad.ctr * ctr_f))
            spend = round(impr / 1000 * ad.cpm * _noise(rng, 0.08), 2)
            purchases = min(clicks, _poisson(rng, clicks * ad.cvr * cvr_f))
            value = round(sum(ad.aov * _noise(rng, 0.25) for _ in range(purchases)), 2)
            reach = round(impr / freq) if impr else 0
            rows.append(MetaRow(day, ad.id, impr, reach, clicks, spend, purchases, value))

    for z in zeros:
        next(a for a in ads if a.id == z).status = "ACTIVE"   # активне, але не крутиться
    last7 = sum(r.spend for r in rows if (as_of - r.day).days < 7) / 7
    runway = next((i.detail["days"] for i in incidents if i.kind == "low_balance"), 9.0)
    spent = round(sum(r.spend for r in rows) * 100) + 1_250_000
    cap = spent + round(last7 * runway * 100)
    return MetaWorld(as_of, ads, rows, cap, spent, incidents)


def default_meta_incidents(ads: list[MetaAd]) -> list[Incident]:
    """Демо-набір: втома креативу, дорожчання в ретаргетингу, оголошення без показів,
    гроші на рахунку закінчуються."""
    return [
        Incident("fatigue", ads[6].id, {"days": 12, "depth": 0.55}),
        Incident("cost_spike", "23850000000000002", {"days": 4, "factor": 2.6}),
        Incident("zero_delivery", ads[10].id, {"days": 3}),
        Incident("low_balance", "account", {"days": 1.8}),
    ]


# --- Google Ads ----------------------------------------------------------------------

@dataclass
class Product:
    item_id: str
    title: str
    price: float
    label: int              # поточний крок ставки в custom_label_0 (5 = карантин)
    archetype: str          # як товар поводиться, тільки для eval і тестів
    impr: float
    ctr: float
    cvr: float
    cpc: float


@dataclass
class ProductRow:
    day: date
    item_id: str
    impressions: int
    clicks: int
    cost_micros: int
    conversions: float
    conversions_value: float


@dataclass
class TermRow:
    term: str
    clicks: int
    impressions: int
    cost_micros: int
    conversions: float


@dataclass
class GadsWorld:
    as_of: date
    products: list[Product]
    rows: list[ProductRow]
    terms: list[TermRow]
    shopping_campaign: tuple[str, str] = ("20000000001", "Shopping | All products")
    search_campaign: tuple[str, str] = ("20000000002", "Search | Garden generic")


_TITLES = ["Garden hose 25 m", "Garden hose 50 m", "Hose reel cart", "Spray gun 8 modes", "Drip irrigation kit",
           "Lawn sprinkler", "Hose connector set", "Rain barrel 200 L", "Pruning shears", "Hedge trimmer cordless",
           "Leaf blower 2-in-1", "Garden gloves", "Kneeling pad", "Raised bed 120x60", "Compost bin 300 L",
           "Seed starter tray", "Grow light LED", "Plant pots set 10", "Watering can 10 L", "Soil moisture meter",
           "Garden cart 80 L", "Wheelbarrow", "Lawn mower robot", "Grass trimmer", "Fertilizer spreader",
           "Bird feeder", "Solar path lights", "Patio heater", "Garden bench", "Hammock with stand",
           "Pressure washer", "Hose holder wall", "Tap timer", "Pond pump", "Greenhouse 2x3 m",
           "Garden tool set", "Rake steel", "Shovel pointed", "Garden kneeler seat", "Pest repeller"]

# архетип: (кількість, label, impr/день, ctr, цільовий ACOS або None = не продається, cpc)
# Конверсія рахується з ACOS і ціни: ACOS = cpc / (cvr * ціна). Так тип товару
# визначає його економіку, а не випадкова ціна.
_ARCHETYPES = [
    ("steady", 14, 20, 450, 0.018, 0.12, 0.30),
    ("winner", 6, 25, 900, 0.022, 0.06, 0.35),
    ("expensive", 4, 30, 800, 0.020, 0.30, 0.55),
    ("burner", 3, 20, 500, 0.012, None, 0.45),
    ("click_sink", 3, 25, 900, 0.028, None, 0.18),
    ("low_ctr", 2, 15, 140, 0.004, 0.10, 0.30),
    ("invisible", 3, 10, 4, 0.02, 0.10, 0.30),
    ("recovering", 2, 5, 120, 0.03, 0.08, 0.25),
    ("dead", 3, 5, 18, 0.004, None, 0.30),
]


def _make_products(rng: random.Random) -> list[Product]:
    out, i = [], 0
    for arch, n, label, impr, ctr, acos, cpc in _ARCHETYPES:
        for _ in range(n):
            price = round(rng.uniform(40, 220), 2)
            c = cpc * rng.uniform(0.9, 1.1)
            cvr = min(0.5, c / (acos * price)) if acos else 0.0
            out.append(Product(f"sku-{1001 + i}", _TITLES[i], price, label, arch,
                               impr * rng.uniform(0.85, 1.15), ctr, cvr, c))
            i += 1
    return out


# Пошукові запити кампанії Search: (запит, кліки за 30 днів, конверсії). Частина явно
# нерелевантна (робота, безкоштовно, ремонт, вживане), частина неоднозначна.
SEARCH_TERMS = [
    ("garden hose", 210, 9), ("best garden hose 50m", 64, 4), ("expandable hose", 88, 3),
    ("hose reel with wheels", 41, 2), ("drip irrigation kit for raised beds", 37, 3),
    ("garden hose free shipping", 22, 1), ("hose connector 1/2 inch", 30, 2), ("lawn sprinkler buy", 26, 2),
    ("garden hose repair", 34, 0), ("how to fix garden hose leak", 41, 0), ("garden hose jobs", 12, 0),
    ("free garden hose", 19, 0), ("used garden hose", 15, 0), ("garden hose wikipedia", 6, 0),
    ("garden hose diy pdf", 9, 0), ("gardena hose", 47, 0), ("karcher pressure washer manual", 21, 0),
    ("hose clamp car radiator", 17, 0), ("fire hose", 11, 0), ("garden hose meme", 4, 0),
    ("rain barrel diverter", 14, 1), ("hose timer app", 23, 0), ("garden hose for drinking water", 18, 1),
    ("garden hose 25m cheap", 52, 2), ("hose reel wall mounted", 28, 1), ("garden hose near me", 33, 1),
]


def build_gads(as_of: date, seed: int = 11) -> GadsWorld:
    rng = random.Random(f"gads-{seed}")
    products = _make_products(rng)
    start = as_of - timedelta(days=DAYS - 1)
    rows: list[ProductRow] = []
    for d in range(DAYS):
        day = start + timedelta(days=d)
        back = (as_of - day).days
        for p in products:
            cvr = p.cvr
            if p.archetype == "recovering" and back >= 30:
                cvr = 0.0                                 # місяць тому не продавався, зараз ожив
            impr = _poisson(rng, p.impr * _WEEKDAY[day.weekday()])
            clicks = min(impr, _poisson(rng, impr * p.ctr))
            cost = round(clicks * p.cpc * _noise(rng, 0.15) * 1_000_000)
            conv = float(min(clicks, _poisson(rng, clicks * cvr)))
            value = round(conv * p.price * _noise(rng, 0.05), 2)
            rows.append(ProductRow(day, p.item_id, impr, clicks, cost, conv, value))
    terms = []
    for term, clicks, conv in SEARCH_TERMS:
        cpc = rng.uniform(0.35, 0.9)
        terms.append(TermRow(term, clicks, round(clicks / rng.uniform(0.03, 0.09)),
                             round(clicks * cpc * 1_000_000), float(conv)))
    return GadsWorld(as_of, products, rows, terms)


def default_as_of(today: date | None = None) -> date:
    """Останній повний день: як і справжні кабінети, «сьогодні» ще не дорахований."""
    return (today or date.today()) - timedelta(days=1)
