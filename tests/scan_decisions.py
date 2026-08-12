import sys, json
from collections import Counter

rows = exceeded = 0
per_conflict = {}
epochs = Counter()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        continue
    epochs[d.get("epoch", "-")] += 1
    for s in d["scenarios"]:
        rows += 1
        bad = bool(s["policy_exceeded"])
        exceeded += bad
        agg = per_conflict.setdefault(
            d["conflict_id"], {"n": 0, "bad": 0, "dirs": 0, "max": 0}
        )
        agg["n"] += 1
        agg["bad"] += bad
        agg["dirs"] = max(agg["dirs"], len(s["directives"]))
        holds = [x.get("max_hold_seconds") or 0 for x in s["directives"]]
        agg["max"] = max(agg["max"], max(holds, default=0))

print(f"epochs: {dict(epochs)}")
print(f"scenario rows: {rows}   EXCEEDED: {exceeded} ({exceeded / max(rows, 1) * 100:.0f}%)")
print()
for cid, a in sorted(per_conflict.items(), key=lambda kv: -kv[1]["bad"]):
    print(f"{cid}  rows={a['n']:3d}  exceeded={a['bad']:3d}  "
          f"max_directives={a['dirs']}  max_hold={a['max']}s")
