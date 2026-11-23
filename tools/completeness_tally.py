#!/usr/bin/env python3
"""Tally completeness_full.jsonl: per-pool clean/violating breakdown + violation types."""
import json, re
from collections import defaultdict

OUT = "/home/z/my-project/scripts/out/completeness_full.jsonl"
recs = [json.loads(l) for l in open(OUT)]

per_class = defaultdict(lambda: defaultdict(int))
vt = defaultdict(int)
missing_total = dup_total = corrupt_total = garbage_total = 0
worst = []

for r in recs:
    k = r["class"]; v = r.get("verdict", "?")
    if v == "COMPLETE-RULE-OK":
        per_class[k]["CLEAN"] += 1
    elif v.startswith(("PDF-ABSENT", "FETCH-FAIL")):
        per_class[k]["NO-PDF"] += 1
    elif v.startswith("UNMEASURABLE"):
        per_class[k]["SCAN"] += 1
    elif v.startswith("PARSE-FAIL"):
        per_class[k]["PARSE-FAIL"] += 1
    elif v.startswith("ERROR-"):
        per_class[k]["ERROR"] += 1
    else:
        per_class[k]["VIOLATING"] += 1
        m = re.search(r"MISSING-(\d+)", v); d = re.search(r"DUPLICATE-(\d+)", v)
        c = re.search(r"CORRUPT-(\d+)", v); g = re.search(r"GARBAGE-(\d+)", v)
        o = re.search(r"OVER-(\d+)", v)
        if m: missing_total += int(m.group(1))
        if d: dup_total += int(d.group(1))
        if c: corrupt_total += int(c.group(1))
        if g: garbage_total += int(g.group(1))
        if o: vt["over"] += 1
        for tag in ("MISSING", "GARBAGE", "DUPLICATE", "CORRUPT"):
            if tag in v: vt[tag] += 1
        score = (int(m.group(1)) if m else 0) + (int(d.group(1)) if d else 0)*2 \
                + (int(g.group(1)) if g else 0)
        worst.append((score, k, r["sc"], r["yr"], v[:110]))

print(f"total records: {len(recs)}")
print(f"{'pool':16s} {'CLEAN':>6s} {'VIOLATING':>10s} {'SCAN':>6s} {'NO-PDF':>7s} {'PARSE-FAIL':>11s}")
order = ["RETRY-FROZEN", "AMNESTY-FROZEN", "RETRY-ACCEPT", "thin<20", "complete>=20"]
for k in order:
    d = per_class[k]
    tot = sum(d.values())
    print(f"{k:16s} {d['CLEAN']:6d} {d['VIOLATING']:10d} {d['SCAN']:6d} "
          f"{d['NO-PDF']:7d} {d['PARSE-FAIL']:11d}  (n={tot})")
print()
print("violation papers by type:", dict(vt))
print(f"missing questions total: {missing_total}, dup rows: {dup_total}, "
      f"corrupt texts: {corrupt_total}, garbage rows: {garbage_total}")
print()
worst.sort(key=lambda t: (-t[0], str(t[2]), str(t[3])))
print("worst 12 (missing+2*dup+garbage):")
for s, k, sc, yr, v in worst[:12]:
    print(f"  [{s:3d}] {sc} {yr} [{k}] {v}")

# the decision table for the owner: truly-short vs truncated
print()
print("POOL DECISION TABLE (rule: extracted == printed):")
for k in order:
    d = per_class[k]
    meas = d["CLEAN"] + d["VIOLATING"]
    if meas:
        print(f"  {k:16s}: measured {meas:5d} | truly-short/complete (CLEAN, stop) "
              f"{d['CLEAN']:5d} ({100*d['CLEAN']/meas:.0f}%) | evidence-backed violations "
              f"(must fix) {d['VIOLATING']:5d} ({100*d['VIOLATING']/meas:.0f}%)")

json.dump({"worst": worst[:200]}, open("/home/z/my-project/scripts/out/completeness_worst.json", "w"), indent=1)
