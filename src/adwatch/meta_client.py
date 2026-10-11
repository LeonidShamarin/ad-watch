"""Клієнт Meta Marketing API: insights по днях, бюджет акаунта, статуси, пауза оголошення.

Той самий код ходить і в мок, і в graph.facebook.com: різниця лише в `base_url`
(або в транспорті httpx). Тому все, що стосується формату (рядки замість чисел,
`actions`, курсори), обробляється тут, а не в правилах.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable

import httpx

MAX_PAGES = 50          # стеля сторінок: курсорний цикл не може крутитися вічно
MAX_RETRIES = 3
RETRY_CODES = {4, 17, 32, 613, 80000, 80004}   # ліміти частоти запитів у Meta


class MetaError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code, self.status = code, status


class MetaAuthError(MetaError):
    """Токен протух чи відкликаний: повторювати марно, треба людина."""


@dataclass
class DayRow:
    day: date
    ad_id: str
    ad_name: str
    campaign_id: str
    campaign_name: str
    impressions: int
    reach: int
    clicks: int
    spend: float
    purchases: int
    purchase_value: float


def _action(items: list[dict] | None, kind: str) -> float:
    for a in items or []:
        if a.get("action_type") == kind:
            return float(a.get("value") or 0)
    return 0.0


def parse_row(d: dict) -> DayRow:
    return DayRow(
        day=date.fromisoformat(d["date_start"]), ad_id=d["ad_id"], ad_name=d.get("ad_name", ""),
        campaign_id=d.get("campaign_id", ""), campaign_name=d.get("campaign_name", ""),
        impressions=int(d.get("impressions") or 0), reach=int(d.get("reach") or 0),
        clicks=int(d.get("clicks") or 0), spend=float(d.get("spend") or 0),
        purchases=int(_action(d.get("actions"), "purchase")),
        purchase_value=_action(d.get("action_values"), "purchase"),
    )


class MetaClient:
    FIELDS = "ad_id,ad_name,campaign_id,campaign_name,impressions,reach,clicks,spend,actions,action_values"

    def __init__(self, http: httpx.AsyncClient, base_url: str, token: str, account: str,
                 version: str = "v23.0", sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.http, self.base, self.token, self.account_id = http, base_url.rstrip("/"), token, account
        self.version, self.sleep = version, sleep

    async def _call(self, method: str, path: str, params: dict | None = None, data: dict | None = None) -> dict:
        url = f"{self.base}/{self.version}/{path}"
        params = {**(params or {}), "access_token": self.token}
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = await self.http.request(method, url, params=params, data=data, timeout=30.0)
            except httpx.TransportError as e:
                last = MetaError(f"network: {e}")
            else:
                if r.status_code < 400:
                    return r.json()
                err = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error", {})
                code = err.get("code")
                if code == 190 or r.status_code == 401:
                    raise MetaAuthError(err.get("message", "auth"), code, r.status_code)
                if r.status_code < 500 and r.status_code != 429 and code not in RETRY_CODES:
                    raise MetaError(err.get("message", f"HTTP {r.status_code}"), code, r.status_code)
                last = MetaError(err.get("message", f"HTTP {r.status_code}"), code, r.status_code)
            await self.sleep(min(2 ** attempt, 8))
        raise last or MetaError("unknown")

    async def insights_daily(self, since: date, until: date) -> list[DayRow]:
        params = {"level": "ad", "fields": self.FIELDS, "time_increment": "1", "limit": "500",
                  "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()})}
        out: list[DayRow] = []
        after = None
        for _ in range(MAX_PAGES):
            page = await self._call("GET", f"act_{self.account_id}/insights", {**params, **({"after": after} if after else {})})
            out.extend(parse_row(d) for d in page.get("data", []))
            paging = page.get("paging", {})
            nxt = paging.get("cursors", {}).get("after")
            if "next" not in paging or not nxt or nxt == after:
                return out                       # кінець, або курсор не зрушив: далі тільки по колу
            after = nxt
        raise MetaError(f"insights: more than {MAX_PAGES} pages, stopping")

    async def account(self) -> dict:
        d = await self._call("GET", f"act_{self.account_id}", {"fields": "name,currency,spend_cap,amount_spent"})
        return {"name": d.get("name"), "currency": d.get("currency", "USD"),
                "spend_cap_cents": int(d.get("spend_cap") or 0), "amount_spent_cents": int(d.get("amount_spent") or 0)}

    async def ads(self) -> dict[str, str]:
        d = await self._call("GET", f"act_{self.account_id}/ads", {"fields": "id,name,effective_status", "limit": "500"})
        return {a["id"]: a.get("effective_status", "") for a in d.get("data", [])}

    async def pause_ad(self, ad_id: str) -> bool:
        d = await self._call("POST", ad_id, data={"status": "PAUSED"})
        return bool(d.get("success"))
