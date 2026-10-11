"""Зробити прогони за кілька днів і зберегти їх для дашборду (static/runs).

Один кабінет з історією до END, прогін на кожен день бачить дані лише до свого дня:
видно, як проблеми наростають. Модель (Groq) викликається тут один раз на прогін, а не
на кожен перегляд сторінки. Запуск: .\\scripts\\py_capped.ps1 scripts\\make_runs.py -Network
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adwatch import mockapi, pipeline, snapshots  # noqa: E402
from adwatch.llm import GroqChat  # noqa: E402

END = date(2026, 10, 10)
DAYS = 10


async def main() -> None:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    llm = GroqChat(key) if key else None
    snapshots.RUNS.mkdir(parents=True, exist_ok=True)
    index, total = [], 0.0
    for i in range(DAYS - 1, -1, -1):
        d = END - timedelta(days=i)
        app = mockapi.build_app(lambda d=d: d, lambda: END)[0]
        res = await pipeline.run(d, clients=pipeline.make_clients(d, app), llm=llm, with_summary=True)
        snap = snapshots.to_json(res)
        (snapshots.RUNS / f"{d.isoformat()}.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        total += res.llm_cost_usd
        index.append({"as_of": d.isoformat(), "meta_alerts": len(res.meta_alerts),
                      "kinds": [a.kind for a in res.meta_alerts], "label_changes": len(res.label_changes),
                      "negatives": len(res.negatives), "summary_source": res.summary_source,
                      "llm_cost_usd": res.llm_cost_usd})
        print(d, [a.kind for a in res.meta_alerts], len(res.label_changes), len(res.negatives),
              res.summary_source, res.unverified)
    (snapshots.RUNS / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"runs: {len(index)}, model cost total ${total:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
