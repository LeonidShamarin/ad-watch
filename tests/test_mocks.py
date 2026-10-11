"""Моки мусять говорити форматом справжніх API: інакше клієнт перевірено на вигадці."""

import json

GQ = "/gads/v22/customers/1234567890/googleAds:searchStream"
H = {"developer-token": "x", "Authorization": "Bearer y", "login-customer-id": "9876543210"}


def test_meta_insights_numbers_are_strings_and_purchases_in_actions(mock):
    r = mock("GET", "/meta/v23.0/act_1000001/insights", params={
        "access_token": "t", "level": "ad", "fields": "ad_id,impressions,clicks,spend,actions",
        "time_range": json.dumps({"since": "2026-10-01", "until": "2026-10-10"}), "limit": "500"})
    row = r.json()["data"][0]
    assert isinstance(row["impressions"], str) and isinstance(row["spend"], str)
    assert any(a["action_type"] == "purchase" for a in row["actions"])


def test_meta_pagination_cursor_moves_and_ends(mock):
    p = {"access_token": "t", "fields": "ad_id,spend", "time_increment": "1", "limit": "100",
         "time_range": json.dumps({"since": "2026-10-01", "until": "2026-10-10"})}
    first = mock("GET", "/meta/v23.0/act_1000001/insights", params=p).json()
    assert "next" in first["paging"] and len(first["data"]) == 100
    after = first["paging"]["cursors"]["after"]
    seen, pages = len(first["data"]), 1
    while True:
        page = mock("GET", "/meta/v23.0/act_1000001/insights", params={**p, "after": after}).json()
        seen += len(page["data"]); pages += 1
        if "next" not in page["paging"]:
            break
        after = page["paging"]["cursors"]["after"]
        assert pages < 20
    assert seen == 10 * 12 - 3          # 12 оголошень × 10 днів, мінус 3 дні без показів


def test_meta_missing_token_is_oauth_190(mock):
    r = mock("GET", "/meta/v23.0/act_1000001/insights")
    assert r.status_code == 400 and r.json()["error"]["code"] == 190


def test_gads_zero_metrics_are_omitted_and_int64_are_strings(mock):
    q = ("SELECT segments.product_item_id, metrics.clicks, metrics.cost_micros, metrics.conversions "
         "FROM shopping_performance_view WHERE segments.date DURING LAST_7_DAYS")
    res = mock("POST", GQ, json={"query": q}, headers=H).json()[0]["results"]
    # товар без жодної метрики не має навіть ключа metrics, тож .get(..., {})
    assert all(isinstance(r.get("metrics", {}).get("costMicros", "0"), str) for r in res)
    assert any("conversions" not in r.get("metrics", {}) for r in res), "товари без продажів не мають ключа conversions"


def test_gads_dates_aggregate_unless_selected(mock):
    q = "SELECT segments.product_item_id, metrics.clicks FROM shopping_performance_view WHERE segments.date BETWEEN '2026-10-04' AND '2026-10-10'"
    flat = mock("POST", GQ, json={"query": q}, headers=H).json()[0]["results"]
    q2 = q.replace("SELECT ", "SELECT segments.date, ")
    daily = mock("POST", GQ, json={"query": q2}, headers=H).json()[0]["results"]
    assert len(flat) == 40 and len(daily) > len(flat)


def test_gads_bad_field_and_auth_errors(mock):
    r = mock("POST", GQ, json={"query": "SELECT campaign.nope FROM campaign"}, headers=H)
    assert r.status_code == 400 and r.json()["error"]["status"] == "INVALID_ARGUMENT"
    r = mock("POST", GQ, json={"query": "SELECT campaign.id FROM campaign"}, headers={"Authorization": "Bearer y"})
    assert r.status_code == 401
    r = mock("POST", GQ, json={"query": "SELECT campaign.id FROM campaign"}, headers={**H, "login-customer-id": "111"})
    assert r.status_code == 403


def test_gads_mutate_duplicate_is_partial_failure(mock):
    op = {"create": {"campaign": "customers/1234567890/campaigns/20000000002", "negative": True,
                     "keyword": {"text": "jobs", "matchType": "PHRASE"}}}
    url = "/gads/v22/customers/1234567890/campaignCriteria:mutate"
    first = mock("POST", url, json={"operations": [op], "partialFailure": True}, headers=H).json()
    again = mock("POST", url, json={"operations": [op], "partialFailure": True}, headers=H).json()
    assert len(first["results"]) == 1 and "partialFailureError" in again
