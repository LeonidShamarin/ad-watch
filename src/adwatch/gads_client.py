"""Клієнт Google Ads API (REST): GAQL через searchStream і мінус-слова через mutate.

Вбудована нода n8n Google Ads вміє лише читати кампанії, тому тут прямі HTTP-виклики,
як роблять у продакшні. Формат відповіді нормалізується тут: camelCase-ключі
сплющуються в 'metrics.costMicros', int64-рядки стають числами, відсутні нулі
стають нулями.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx

MAX_RETRIES = 3
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GadsError(Exception):
    def __init__(self, message: str, status: int | None = None, codes: list[dict] | None = None) -> None:
        super().__init__(message)
        self.status, self.codes = status, codes or []


def flatten(obj: dict, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if depth > 6:
        return out
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + ".", depth + 1))
        else:
            out[key] = v
    return out


def num(row: dict, key: str) -> float:
    """Метрика як число. Нуль у відповіді Google просто відсутній, int64 приходить рядком."""
    v = row.get(key)
    return float(v) if v not in (None, "") else 0.0


async def refresh_access_token(http: httpx.AsyncClient, client_id: str, client_secret: str,
                               refresh_token: str) -> str:
    """Справжній режим: OAuth refresh token -> access token на годину. У моку не потрібен."""
    r = await http.post(TOKEN_URL, data={"client_id": client_id, "client_secret": client_secret,
                                         "refresh_token": refresh_token, "grant_type": "refresh_token"})
    r.raise_for_status()
    return r.json()["access_token"]


class GadsClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, *, developer_token: str, access_token: str,
                 customer_id: str, login_customer_id: str | None = None, version: str = "v22",
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.http, self.base, self.version = http, base_url.rstrip("/"), version
        self.customer = customer_id.replace("-", "")
        self.headers = {"developer-token": developer_token, "Authorization": f"Bearer {access_token}"}
        if login_customer_id:
            self.headers["login-customer-id"] = login_customer_id.replace("-", "")
        self.sleep = sleep

    async def _post(self, path: str, body: dict) -> Any:
        url = f"{self.base}/{self.version}/customers/{self.customer}/{path}"
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = await self.http.post(url, json=body, headers=self.headers, timeout=60.0)
            except httpx.TransportError as e:
                last = GadsError(f"network: {e}")
            else:
                if r.status_code < 400:
                    return r.json()
                err = r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
                codes = [e.get("errorCode", {}) for d in err.get("details", []) for e in d.get("errors", [])]
                if r.status_code in (429, 500, 503) or err.get("status") == "RESOURCE_EXHAUSTED":
                    last = GadsError(err.get("message", f"HTTP {r.status_code}"), r.status_code, codes)
                else:
                    raise GadsError(err.get("message", f"HTTP {r.status_code}"), r.status_code, codes)
            await self.sleep(min(2 ** attempt, 8))
        raise last or GadsError("unknown")

    async def search(self, query: str) -> list[dict[str, Any]]:
        batches = await self._post("googleAds:searchStream", {"query": query})
        return [flatten(row) for b in batches for row in b.get("results", [])]

    async def existing_negatives(self, campaign_id: str) -> set[str]:
        rows = await self.search(
            "SELECT campaign_criterion.keyword.text, campaign_criterion.negative FROM campaign_criterion "
            f"WHERE campaign.id = {campaign_id} AND campaign_criterion.negative = TRUE")
        return {str(r.get("campaignCriterion.keyword.text", "")).lower() for r in rows}

    async def add_negatives(self, campaign_id: str, texts: list[str], match_type: str = "PHRASE") -> dict:
        """Додати мінус-слова без дублів: спершу читаємо наявні, потім mutate з partialFailure."""
        have = await self.existing_negatives(campaign_id)
        todo = [t for t in dict.fromkeys(x.strip().lower() for x in texts) if t and t not in have]
        skipped = len(texts) - len(todo)
        if not todo:
            return {"added": 0, "skipped": skipped, "failed": []}
        ops = [{"create": {"campaign": f"customers/{self.customer}/campaigns/{campaign_id}", "negative": True,
                           "keyword": {"text": t, "matchType": match_type}}} for t in todo]
        res = await self._post("campaignCriteria:mutate", {"operations": ops, "partialFailure": True})
        failed = [e.get("message", "") for d in (res.get("partialFailureError") or {}).get("details", [])
                  for e in d.get("errors", [])]
        return {"added": len(res.get("results", [])), "skipped": skipped, "failed": failed}
