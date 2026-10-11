"""Імітація Meta Marketing API (Graph API) у тому самому форматі, що й справжній.

Що відтворено, бо саме на цьому спотикаються інтеграції:
- числа в insights приходять РЯДКАМИ ("spend": "12.34");
- покупки лежать не в полі, а в масиві `actions` з `action_type`;
- сторінки йдуть курсором `paging.cursors.after`, наступна сторінка є, лише поки є `paging.next`;
- бюджет акаунта (`spend_cap`, `amount_spent`) у центах;
- помилка токена: HTTP 400 з `OAuthException`, code 190.

Клієнт (`meta_client.py`) не знає, що говорить з моком: міняється тільки base URL
або транспорт.
"""

from __future__ import annotations

import base64
import json
import threading
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from adwatch.world import MetaRow, MetaWorld

MAX_LIMIT = 500


def _err(code: int, message: str, status: int = 400, etype: str = "OAuthException") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": etype, "code": code,
                                   "fbtrace_id": "AdWatchMock"}}, status_code=status)


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode()


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode().split(":")[1])
    except (ValueError, IndexError):
        return -1


def _num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


class MetaMockState:
    """Стан моку: світ кабінету і зміни, які клієнт уже зробив (призупинені оголошення)."""

    def __init__(self, world_for: callable) -> None:
        self.world_for = world_for            # () -> MetaWorld
        self.paused: set[str] = set()
        self.calls: defaultdict[str, int] = defaultdict(int)
        self.lock = threading.Lock()


def _agg(rows: list[MetaRow]) -> dict:
    impr = sum(r.impressions for r in rows)
    clicks = sum(r.clicks for r in rows)
    spend = round(sum(r.spend for r in rows), 2)
    reach = sum(r.reach for r in rows)
    purchases = sum(r.purchases for r in rows)
    value = round(sum(r.purchase_value for r in rows), 2)
    return {"impressions": impr, "reach": reach, "clicks": clicks, "spend": spend,
            "purchases": purchases, "purchase_value": value}


def _row_json(w: MetaWorld, ad_id: str, a: dict, since: date, until: date, fields: set[str]) -> dict:
    ad = w.ad(ad_id)
    full = {
        "account_id": "1000001",
        "ad_id": ad.id, "ad_name": ad.name, "adset_id": ad.adset_id, "adset_name": ad.adset_name,
        "campaign_id": ad.campaign_id, "campaign_name": ad.campaign_name,
        "impressions": str(a["impressions"]), "reach": str(a["reach"]), "clicks": str(a["clicks"]),
        "spend": _num(a["spend"]),
        "ctr": _num(round(a["clicks"] / a["impressions"] * 100, 6)) if a["impressions"] else "0",
        "cpm": _num(round(a["spend"] / a["impressions"] * 1000, 6)) if a["impressions"] else "0",
        "frequency": _num(round(a["impressions"] / a["reach"], 6)) if a["reach"] else "0",
    }
    out = {k: v for k, v in full.items() if k in fields}
    if "actions" in fields and a["purchases"]:
        out["actions"] = [{"action_type": "link_click", "value": str(a["clicks"])},
                          {"action_type": "purchase", "value": str(a["purchases"])}]
    elif "actions" in fields and a["clicks"]:
        out["actions"] = [{"action_type": "link_click", "value": str(a["clicks"])}]
    if "action_values" in fields and a["purchases"]:
        out["action_values"] = [{"action_type": "purchase", "value": _num(a["purchase_value"])}]
    out["date_start"], out["date_stop"] = since.isoformat(), until.isoformat()
    return out


def make_router(state: MetaMockState) -> APIRouter:
    r = APIRouter()

    def _auth(request: Request) -> JSONResponse | None:
        tok = request.query_params.get("access_token") or \
            request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not tok:
            return _err(190, "An access token is required to request this resource.")
        return None

    @r.get("/{version}/act_{account}/insights")
    def insights(version: str, account: str, request: Request):
        if (e := _auth(request)):
            return e
        w = state.world_for()
        if account != "1000001":
            return _err(100, f"Unsupported get request. Object with ID 'act_{account}' does not exist",
                        etype="GraphMethodException")
        q = request.query_params
        if q.get("level", "ad") != "ad":
            return _err(100, "(#100) This demo supports level=ad only")
        try:
            tr = json.loads(q.get("time_range") or "{}")
            since = date.fromisoformat(tr.get("since") or w.as_of.isoformat())
            until = date.fromisoformat(tr.get("until") or w.as_of.isoformat())
        except (ValueError, TypeError):
            return _err(100, "(#100) param time_range must be a JSON object with since and until")
        fields = {f.strip() for f in q.get("fields", "impressions,spend").split(",") if f.strip()}
        limit = min(max(int(q.get("limit", 25)), 1), MAX_LIMIT)
        daily = q.get("time_increment", "all_days") == "1"
        offset = _offset(q.get("after"))
        if offset < 0:
            return _err(100, "(#100) Invalid cursor")
        with state.lock:
            state.calls["insights"] += 1

        rows = [x for x in w.rows if since <= x.day <= until]
        items: list[dict] = []
        if daily:
            for x in rows:
                if x.impressions or x.spend:            # Meta не віддає порожні дні
                    items.append(_row_json(w, x.ad_id, _agg([x]), x.day, x.day, fields))
        else:
            by_ad: dict[str, list[MetaRow]] = defaultdict(list)
            for x in rows:
                by_ad[x.ad_id].append(x)
            for ad_id, rs in by_ad.items():
                a = _agg(rs)
                if a["impressions"] or a["spend"]:
                    items.append(_row_json(w, ad_id, a, since, until, fields))
        page = items[offset:offset + limit]
        body: dict = {"data": page, "paging": {"cursors": {"before": _cursor(offset),
                                                           "after": _cursor(offset + len(page))}}}
        if offset + limit < len(items):
            body["paging"]["next"] = f"{request.url.path}?after={_cursor(offset + limit)}"
        return body

    @r.get("/{version}/act_{account}/ads")
    def ads(version: str, account: str, request: Request):
        if (e := _auth(request)):
            return e
        w = state.world_for()
        data = [{"id": a.id, "name": a.name, "campaign_id": a.campaign_id, "adset_id": a.adset_id,
                 "effective_status": "PAUSED" if a.id in state.paused else a.status} for a in w.ads]
        return {"data": data, "paging": {"cursors": {"before": _cursor(0), "after": _cursor(len(data))}}}

    @r.get("/{version}/act_{account}")
    def account_info(version: str, account: str, request: Request):
        if (e := _auth(request)):
            return e
        w = state.world_for()
        full = {"id": f"act_{account}", "account_id": account, "name": "Demo Garden Store",
                "currency": "USD", "account_status": 1, "timezone_name": "Europe/Kyiv",
                "spend_cap": str(w.spend_cap_cents), "amount_spent": str(w.amount_spent_cents)}
        fields = {f.strip() for f in request.query_params.get("fields", "id,name").split(",")}
        return {k: v for k, v in full.items() if k in fields or k == "id"}

    @r.post("/{version}/{object_id}")
    async def update(version: str, object_id: str, request: Request):
        if (e := _auth(request)):
            return e
        form = dict(await request.form()) or dict(request.query_params)
        status = form.get("status")
        w = state.world_for()
        if not any(a.id == object_id for a in w.ads):
            return _err(100, f"Unsupported post request. Object with ID '{object_id}' does not exist",
                        etype="GraphMethodException")
        if status not in ("PAUSED", "ACTIVE"):
            return _err(100, "(#100) Param status must be one of {ACTIVE, PAUSED}")
        with state.lock:
            (state.paused.add if status == "PAUSED" else state.paused.discard)(object_id)
            state.calls["update"] += 1
        return {"success": True}

    return r
