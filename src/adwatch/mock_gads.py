"""Імітація Google Ads API (REST, v22) у тому самому форматі, що й справжній.

Що відтворено, бо саме на цьому спотикаються інтеграції:
- запити мовою GAQL через `googleAds:searchStream`, відповідь — МАСИВ пачок
  `[{"results": [...], "fieldMask": "...", "requestId": "..."}]`;
- ключі в camelCase (`costMicros`, `productItemId`);
- int64 приходять рядками ("costMicros": "1230000"), double — числами;
- нульові значення НЕ приходять зовсім (proto3 JSON): товар без кліків не має ключа `clicks`;
- якщо `segments.date` є лише у WHERE, метрики сумуються за весь період;
- `LAST_7_DAYS` / `LAST_30_DAYS` не включають сьогодні;
- обов'язкові заголовки `developer-token` і `Authorization`, для MCC ще `login-customer-id`;
- мінус-слова: `campaignCriteria:mutate`, дубль повертає помилку, як і справжній API.

Підтримано підмножину GAQL, потрібну демо: SELECT ... FROM ресурс WHERE умови через AND
(BETWEEN, DURING, =, !=, >, <, IN) ORDER BY ... LIMIT n. Решта дає 400 INVALID_ARGUMENT.
"""

from __future__ import annotations

import re
import threading
import zlib
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from adwatch.world import GADS_CUSTOMER, GADS_MANAGER, GadsWorld

BATCH = 500


class GaqlError(ValueError):
    pass


def _camel(s: str) -> str:
    head, *rest = s.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


_INT64 = {"metrics.impressions", "metrics.clicks", "metrics.cost_micros", "campaign.id",
          "campaign_criterion.criterion_id"}


def _error(status: int, message: str, code: dict | None = None) -> JSONResponse:
    st = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED"}.get(status, "INTERNAL")
    return JSONResponse({"error": {"code": status, "message": message, "status": st, "details": [{
        "@type": "type.googleapis.com/google.ads.googleads.v22.errors.GoogleAdsFailure",
        "errors": [{"errorCode": code or {"queryError": "UNEXPECTED"}, "message": message}]}]}},
        status_code=status)


# --- GAQL --------------------------------------------------------------------------

_QUERY = re.compile(r"^\s*SELECT\s+(?P<sel>.+?)\s+FROM\s+(?P<res>\w+)"
                    r"(?:\s+WHERE\s+(?P<where>.+?))?(?:\s+ORDER\s+BY\s+(?P<order>.+?))?"
                    r"(?:\s+LIMIT\s+(?P<limit>\d+))?\s*$", re.I | re.S)
_BETWEEN = re.compile(r"segments\.date\s+BETWEEN\s+'(\d{4}-\d\d-\d\d)'\s+AND\s+'(\d{4}-\d\d-\d\d)'", re.I)
_DURING = re.compile(r"segments\.date\s+DURING\s+(\w+)", re.I)
_COND = re.compile(r"^\s*([\w.]+)\s*(=|!=|>=|<=|>|<|IN)\s*(.+?)\s*$", re.I)
_DURING_DAYS = {"YESTERDAY": 1, "LAST_7_DAYS": 7, "LAST_14_DAYS": 14, "LAST_30_DAYS": 30}


def parse(query: str, as_of: date) -> dict:
    m = _QUERY.match(query.replace("\n", " "))
    if not m:
        raise GaqlError("Error in query: unexpected input. Expected SELECT ... FROM ...")
    fields = [f.strip() for f in m.group("sel").split(",") if f.strip()]
    where = m.group("where") or ""
    since, until = None, None
    if (b := _BETWEEN.search(where)):
        since, until = date.fromisoformat(b.group(1)), date.fromisoformat(b.group(2))
        where = _BETWEEN.sub("", where)
    elif (d := _DURING.search(where)):
        n = _DURING_DAYS.get(d.group(1).upper())
        if n is None:
            raise GaqlError(f"Unsupported date range: {d.group(1)}")
        since, until = as_of - timedelta(days=n - 1), as_of
        where = _DURING.sub("", where)
    conds = []
    for part in re.split(r"\s+AND\s+", where, flags=re.I):
        if not part.strip():
            continue
        c = _COND.match(part)
        if not c:
            raise GaqlError(f"Error in query: cannot parse condition '{part.strip()}'")
        field, op, raw = c.group(1), c.group(2).upper(), c.group(3)
        if op == "IN":
            val = [v.strip().strip("'\"") for v in raw.strip("() ").split(",")]
        else:
            raw = raw.strip()
            if raw.upper() in ("TRUE", "FALSE"):
                val = raw.upper() == "TRUE"
            elif raw[:1] in "'\"":
                val = raw.strip("'\"")
            else:
                try:
                    val = float(raw)
                except ValueError:
                    raise GaqlError(f"Error in query: bad literal '{raw}'") from None
        conds.append((field, op, val))
    order = None
    if m.group("order"):
        o = m.group("order").split()
        order = (o[0], len(o) > 1 and o[1].upper() == "DESC")
    return {"fields": fields, "resource": m.group("res"), "since": since, "until": until,
            "conds": conds, "order": order, "limit": int(m.group("limit")) if m.group("limit") else None}


def _ok(v, op: str, target) -> bool:
    if op == "IN":
        return str(v) in target
    if isinstance(target, float):
        v = float(v or 0)
    return {"=": v == target, "!=": v != target, ">": v > target, "<": v < target,
            ">=": v >= target, "<=": v <= target}[op]


# --- джерела рядків ------------------------------------------------------------------

def _flat_rows(w: GadsWorld, resource: str, since: date, until: date, by_date: bool,
               negatives: dict[str, set[str]]) -> list[dict]:
    """Плоскі рядки {'segments.date': ..., 'metrics.clicks': ...} для ресурсу."""
    cid = GADS_CUSTOMER
    if resource == "shopping_performance_view":
        sc_id, sc_name = w.shopping_campaign
        prod = {p.item_id: p for p in w.products}
        acc: dict[tuple, dict] = {}
        for r in w.rows:
            if not (since <= r.day <= until):
                continue
            key = (r.item_id, r.day if by_date else None)
            a = acc.setdefault(key, {"metrics.impressions": 0, "metrics.clicks": 0, "metrics.cost_micros": 0,
                                     "metrics.conversions": 0.0, "metrics.conversions_value": 0.0})
            a["metrics.impressions"] += r.impressions
            a["metrics.clicks"] += r.clicks
            a["metrics.cost_micros"] += r.cost_micros
            a["metrics.conversions"] += r.conversions
            a["metrics.conversions_value"] = round(a["metrics.conversions_value"] + r.conversions_value, 2)
        out = []
        for (item, day), a in acc.items():
            p = prod[item]
            row = {"segments.product_item_id": item, "segments.product_title": p.title,
                   "segments.product_custom_attribute0": str(p.label), "campaign.id": sc_id,
                   "campaign.name": sc_name, **a}
            if day:
                row["segments.date"] = day.isoformat()
            out.append(row)
        return out
    if resource == "search_term_view":
        s_id, s_name = w.search_campaign
        # Пошукові запити в демо зберігаються як підсумок за 30 днів, тож дата-фільтр
        # не дробить їх по днях (обмеження моку, описане в README).
        return [{"search_term_view.search_term": t.term, "search_term_view.status": "NONE",
                 "campaign.id": s_id, "campaign.name": s_name, "metrics.clicks": t.clicks,
                 "metrics.impressions": t.impressions, "metrics.cost_micros": t.cost_micros,
                 "metrics.conversions": t.conversions} for t in w.terms]
    if resource == "campaign_criterion":
        s_id, _ = w.search_campaign
        return [{"campaign.id": s_id, "campaign_criterion.negative": True,
                 "campaign_criterion.keyword.text": text, "campaign_criterion.keyword.match_type": "PHRASE",
                 "campaign_criterion.resource_name": f"customers/{cid}/campaignCriteria/{s_id}~{i}"}
                for i, text in enumerate(sorted(negatives.get(s_id, ())), start=1)]
    if resource == "campaign":
        out = []
        for c_id, c_name, kind in ((*w.shopping_campaign, "SHOPPING"), (*w.search_campaign, "SEARCH")):
            row = {"campaign.id": c_id, "campaign.name": c_name, "campaign.status": "ENABLED",
                   "campaign.advertising_channel_type": kind}
            if kind == "SHOPPING":
                rs = [r for r in w.rows if since <= r.day <= until]
                row |= {"metrics.impressions": sum(r.impressions for r in rs),
                        "metrics.clicks": sum(r.clicks for r in rs),
                        "metrics.cost_micros": sum(r.cost_micros for r in rs),
                        "metrics.conversions": sum(r.conversions for r in rs),
                        "metrics.conversions_value": round(sum(r.conversions_value for r in rs), 2)}
            else:
                row |= {"metrics.clicks": sum(t.clicks for t in w.terms),
                        "metrics.impressions": sum(t.impressions for t in w.terms),
                        "metrics.cost_micros": sum(t.cost_micros for t in w.terms),
                        "metrics.conversions": sum(t.conversions for t in w.terms)}
            out.append(row)
        return out
    raise GaqlError(f"Resource '{resource}' is not supported by this demo")


def _nest(flat: dict, fields: list[str]) -> dict:
    """{'metrics.cost_micros': 5} -> {'metrics': {'costMicros': '5'}}; нулі не віддаються."""
    out: dict = {}
    for f in fields:
        v = flat.get(f)
        if v in (None, 0, 0.0, "", False):
            continue                              # proto3 JSON не серіалізує значення за замовчуванням
        if f in _INT64:
            v = str(v)
        parts = f.split(".")
        node = out
        for p in parts[:-1]:
            node = node.setdefault(_camel(p), {})
        node[_camel(parts[-1])] = v
    return out


def run_query(w: GadsWorld, query: str, negatives: dict[str, set[str]], clock: date | None = None) -> list[dict]:
    clock = clock or w.as_of
    q = parse(query, clock)
    since = q["since"] or (clock - timedelta(days=29))
    until = min(q["until"] or clock, clock)
    by_date = "segments.date" in q["fields"]
    rows = _flat_rows(w, q["resource"], since, until, by_date, negatives)
    known = set().union(*(r.keys() for r in rows)) if rows else set()
    for f in q["fields"]:
        if rows and f not in known and not f.startswith("metrics."):
            raise GaqlError(f"Unrecognized field in the query: '{f}'")
    for field, op, val in q["conds"]:
        rows = [r for r in rows if _ok(r.get(field), op, val)]
    if q["order"]:
        key, desc = q["order"]
        rows.sort(key=lambda r: (r.get(key) is None, r.get(key) or 0), reverse=desc)
    if q["limit"]:
        rows = rows[:q["limit"]]
    return [_nest(r, q["fields"]) for r in rows]


# --- HTTP ----------------------------------------------------------------------------

class GadsMockState:
    def __init__(self, world_for: callable, clock_for: callable | None = None) -> None:
        self.world_for = world_for
        self.clock_for = clock_for
        self.negatives: dict[str, set[str]] = defaultdict(set)
        self.lock = threading.Lock()
        self.calls: defaultdict[str, int] = defaultdict(int)


def make_router(state: GadsMockState) -> APIRouter:
    r = APIRouter()

    def _check(request: Request, customer: str) -> JSONResponse | None:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return _error(401, "Request is missing required authentication credential.",
                          {"authenticationError": "NOT_ADS_USER"})
        if not request.headers.get("developer-token"):
            return _error(401, "The developer token is not on the allowlist.",
                          {"authenticationError": "DEVELOPER_TOKEN_NOT_APPROVED"})
        if customer != GADS_CUSTOMER:
            return _error(403, f"User doesn't have permission to access customer {customer}.",
                          {"authorizationError": "USER_PERMISSION_DENIED"})
        if request.headers.get("login-customer-id", GADS_MANAGER) != GADS_MANAGER:
            return _error(403, "The login-customer-id header does not manage this customer.",
                          {"authorizationError": "USER_PERMISSION_DENIED"})
        return None

    @r.post("/{version}/customers/{customer}/googleAds:searchStream")
    async def search_stream(version: str, customer: str, request: Request):
        if (e := _check(request, customer)):
            return e
        body = await request.json()
        with state.lock:
            state.calls["searchStream"] += 1
            negatives = {k: set(v) for k, v in state.negatives.items()}
        try:
            results = run_query(state.world_for(), body.get("query", ""), negatives,
                                state.clock_for() if state.clock_for else None)
        except GaqlError as ex:
            return _error(400, str(ex), {"queryError": "BAD_FIELD_NAME" if "field" in str(ex) else "UNEXPECTED"})
        fmask = ",".join(_camel(f) for f in parse(body["query"], state.world_for().as_of)["fields"])
        batches = [{"results": results[i:i + BATCH], "fieldMask": fmask, "requestId": f"mock-{i}"}
                   for i in range(0, max(len(results), 1), BATCH)]
        return batches

    @r.post("/{version}/customers/{customer}/campaignCriteria:mutate")
    async def mutate(version: str, customer: str, request: Request):
        if (e := _check(request, customer)):
            return e
        body = await request.json()
        validate_only = bool(body.get("validateOnly"))
        results, errors = [], []
        with state.lock:
            for idx, op in enumerate(body.get("operations", [])):
                c = op.get("create") or {}
                camp = str(c.get("campaign", "")).rsplit("/", 1)[-1]
                text = ((c.get("keyword") or {}).get("text") or "").strip().lower()
                if not (camp and text and c.get("negative") is True):
                    errors.append({"errorCode": {"criterionError": "INVALID_KEYWORD_TEXT"},
                                   "message": "Only negative keyword creates are supported", "location": idx})
                    continue
                if text in state.negatives[camp]:
                    errors.append({"errorCode": {"criterionError": "DUPLICATE_CRITERION"},
                                   "message": f"Duplicate criterion: '{text}'", "location": idx})
                    continue
                if not validate_only:
                    state.negatives[camp].add(text)
                results.append({"resourceName": f"customers/{customer}/campaignCriteria/{camp}~{zlib.crc32(text.encode())}"})
            state.calls["mutate"] += 1
        if errors and not body.get("partialFailure"):
            return _error(400, errors[0]["message"], errors[0]["errorCode"])
        out: dict = {"results": results}
        if errors:
            out["partialFailureError"] = {"code": 3, "message": f"{len(errors)} operation(s) failed",
                                          "details": [{"errors": errors}]}
        return out

    return r
