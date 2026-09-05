# tests/classify_holds.py  — reads the trace, writes the verdict
import json, sys, collections

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/holds.jsonl"
run_end_s = float(sys.argv[2]) if len(sys.argv) > 2 else 10800.0

ev = [json.loads(l) for l in open(path)]
TERM = {"released", "superseded_by_regulate", "superseded_by_hold", "recycled",
        "abandoned_astern", "discharged_stand", "discharged_loop"}
issued = {e["seq"]: e for e in ev if e["writer"].startswith("issued_")}
term = {e["seq"]: e for e in ev if e["writer"] in TERM}
berthed = {e["seq"] for e in ev if e["writer"] == "berthed"}
blocked = {e["seq"] for e in ev if e["writer"] == "release_blocked"}

latched, rows = [], []
for s in sorted(issued):
    i, t = issued[s], term.get(s)
    expiry = i["sim_s"] + (i["expires_in"] or 0.0)
    if t:
        verdict = t["writer"]
    elif expiry > run_end_s:
        verdict = "in-force at run end"
    elif s in blocked:
        verdict = "held, exit blocked"
    elif s not in berthed:
        verdict = "LATCHED, no discharge path"
        latched.append((s, i, expiry))
    else:
        verdict = "LATCHED, berthed"
        latched.append((s, i, expiry))
    rows.append((s, i, t, expiry, verdict))

print(f"{len(issued)} issued  {len(term)} terminated  "
      f"{len(latched)} latched past expiry\n")
print(collections.Counter(v for *_, v in rows).most_common())
print("\nseq train  iss@   expiry  berthed  verdict           reason")
for s, i, t, expiry, verdict in rows:
    print(f"{s:>3} {i['train']:>6} t{i['tick']:<5} {expiry:>8.0f}  "
          f"{'yes' if s in berthed else 'no ':>7}  {verdict:<17} "
          f"{(t or {}).get('reason','')}")

if latched:
    print("\nHOLD-FAIL: " + "; ".join(
        f"seq {s} {i['train']} issued t{i['tick']} expired {e:.0f}s, "
        f"{'berthed' if s in berthed else 'never berthed'}, no terminal event"
        for s, i, e in latched))
    sys.exit(1)
print("\nHOLD-PASS")