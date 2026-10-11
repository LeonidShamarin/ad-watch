"""Розклад рішень Google-правил за архетипами товарів на одній даті (для налагодження)."""

import asyncio
import collections
import sys
from datetime import date

from adwatch import pipeline, world

as_of = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 10, 10)
res = asyncio.run(pipeline.run(as_of))
arch = {p.item_id: p.archetype for p in world.build_gads(as_of).products}
for k, v in sorted(collections.Counter((arch[p.item_id], p.rule) for p in res.products).items()):
    print(k, v)
print("label changes:", len(res.label_changes), "| meta alerts:", [a.kind for a in res.meta_alerts])
