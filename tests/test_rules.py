"""Правила Meta і Google на синтетичних даних і на ручних прикладах."""

from datetime import date

from adwatch import rules_gads, rules_meta, world
from adwatch.meta_client import DayRow

AS_OF = date(2026, 10, 10)


def _rows(w: world.MetaWorld) -> list[DayRow]:
    names = {a.id: a for a in w.ads}
    return [DayRow(r.day, r.ad_id, names[r.ad_id].name, names[r.ad_id].campaign_id, names[r.ad_id].campaign_name,
                   r.impressions, r.reach, r.clicks, r.spend, r.purchases, r.purchase_value)
            for r in w.rows if r.impressions or r.spend]


def _run(w):
    acc = {"spend_cap_cents": w.spend_cap_cents, "amount_spent_cents": w.amount_spent_cents}
    return rules_meta.run_all(_rows(w), {a.id: a.status for a in w.ads}, acc, w.as_of)


def test_clean_account_raises_nothing():
    for seed in range(5):
        assert _run(world.build_meta(AS_OF, seed=seed, incidents=[])) == [], seed


def test_each_planted_incident_is_found_alone():
    ads = world.build_meta(AS_OF, incidents=[]).ads
    for inc, kind in [(world.Incident("fatigue", ads[0].id, {"days": 12, "depth": 0.55}), "creative_fatigue"),
                      (world.Incident("cost_spike", ads[0].campaign_id, {"days": 4, "factor": 2.6}), "cost_spike"),
                      (world.Incident("zero_delivery", ads[3].id, {"days": 3}), "zero_delivery"),
                      (world.Incident("low_balance", "account", {"days": 1.5}), "low_balance")]:
        kinds = [a.kind for a in _run(world.build_meta(AS_OF, incidents=[inc]))]
        assert kinds == [kind], (inc.kind, kinds)


def test_fatigue_alert_offers_confirmable_pause():
    alerts = _run(world.build_meta(AS_OF))
    f = next(a for a in alerts if a.kind == "creative_fatigue")
    assert f.action.kind == "pause_ad" and f.action.requires_confirmation


def test_poisson_cdf_is_sane():
    assert abs(rules_meta.poisson_cdf(0, 2.0) - 0.1353) < 1e-3
    assert rules_meta.poisson_cdf(1000, 900) > 0.99


def _win(**kw):
    base = {"title": "x", "label": 20, **{f"{m}_{w}": 0.0 for w in (7, 14, 30)
                                         for m in ("impr", "clicks", "cost", "conv", "value")}}
    return base | kw


def test_burner_threshold_scales_with_order_value():
    cheap = _win(cost_14=12, conv_30=4, value_30=120, impr_7=900)      # чек $30: поріг max(10, 2*0.15*30)=10
    pricey = _win(cost_14=12, conv_30=4, value_30=1200, impr_7=900)    # чек $300: поріг $90
    assert rules_gads.decide("a", cheap).rule == "burner"
    assert rules_gads.decide("b", pricey).rule != "burner"


def test_one_sale_tolerance_keeps_borderline_product():
    # 2 продажі по $100 і $40 витрат: ACOS 20%, але з ще одним продажем 13%, тож рішення не тримається.
    x = _win(conv_14=2, cost_14=40, value_14=200, impr_7=900, conv_30=4, value_30=400)
    assert rules_gads.decide("a", x).rule != "expensive"
    assert rules_gads.decide("a", x, rules_gads.GadsRules(sale_tolerance=False)).rule == "expensive"


def test_quarantine_release_and_dead():
    assert rules_gads.decide("a", _win(label=5, conv_30=2, cost_30=10, value_30=200)).label_to == 10
    assert rules_gads.decide("b", _win(label=5, clicks_30=3)).label_to == 99


def test_supplemental_feed_lists_only_changes():
    ds = [rules_gads.decide("a", _win(label=5, clicks_30=3)), rules_gads.decide("b", _win(impr_7=900))]
    assert rules_gads.supplemental_feed(ds) == "id,custom_label_0\na,99\n"


def test_terms_patterns_brands_and_purchase_intent():
    rows = [{"searchTermView.searchTerm": t, "metrics.clicks": str(c), "metrics.conversions": v,
             "metrics.costMicros": str(c * 500_000)} for t, c, v in
            [("garden hose jobs", 12, 0), ("gardena hose", 47, 0), ("garden hose free shipping", 22, 1),
             ("hose timer app", 23, 0), ("garden hose", 200, 9)]]
    plan, unclear = rules_gads.judge_terms(rows)
    v = {x.term: x for x in plan.verdicts}
    assert v["garden hose jobs"].negative and v["gardena hose"].negative
    assert not v["garden hose free shipping"].negative          # «free», але є продажі
    assert v["hose timer app"].source == "performance" and [u.term for u in unclear] == ["hose timer app"]
    assert not v["garden hose"].negative
