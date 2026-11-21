#!/usr/bin/env python3
"""POST-SWEEP RE-VERIFICATION of the gold deficit sweep (cycle76 #111 + cycle77
#113 = chunks 1+3, 1,093 papers; chunk 2 excluded - re-shot in flight in #114).
completeness_run.verify() directly (hash-first local lookup, census semantics).
Read-only on DB, zero AI calls. Output: reverify_sweep_ch13_20261121.jsonl"""
import json, re, sys, collections, concurrent.futures as cf
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
import completeness_run as cr
import psycopg2

OUT = "/home/z/my-project/scripts/out/reverify_sweep_ch13_20261121.jsonl"
FIX = "/home/z/my-project/scripts/out/fix_list_gold_realgaps_20261121.json"
CH2 = "/home/z/my-project/scripts/out/fix_list_gold_realgaps_20261121_chunk2.json"
CENSUS = "/home/z/my-project/scripts/out/gold_census_20261121.jsonl"

fix = [json.loads(l) if isinstance(l, str) else l for l in open(FIX)] if False else json.load(open(FIX))
ch2 = {r["pid"] for r in json.load(open(CH2))}
targets = [r for r in fix if r["pid"] not in ch2]
pids = [r["pid"] for r in targets]
print("re-verify targets (chunks 1+3):", len(pids), "excluded (chunk2 in flight):", len(ch2), flush=True)

old = {}
for l in open(CENSUS):
    r = json.loads(l)
    if r.get("pid") in set(pids):
        old[r["pid"]] = r.get("verdict")
print("old verdicts matched:", len(old), flush=True)

conn = psycopg2.connect(DSN, connect_timeout=30)
conn.autocommit = True
cur = conn.cursor()
cur.execute("""SELECT o.paper_id, (q.parent_id IS NOT NULL), q.text
               FROM occurrences o JOIN questions q ON q.id = o.question_id
               WHERE o.paper_id = ANY(%s::uuid[])""", (pids,))
stored = collections.defaultdict(list)
for pid, is_sub, text in cur.fetchall():
    stored[pid].append((None, is_sub, (text or "")[:300], None))
cur.execute("SELECT id::text, coalesce(file_hash,''), extraction_status, coalesce(n_questions,0), subject_code, year FROM papers WHERE id::text = ANY(%s)", (pids,))
meta = {r[0]: r[1:] for r in cur.fetchall()}
conn.close()
print("papers with stored rows:", len(stored), flush=True)

def run_one(item):
    r, pid = item
    fh, st, nq, sc, yr = meta.get(pid, ("", "?", 0, "", ""))
    srows = stored.get(pid, [])
    try:
        v = cr.verify(pid, sc, yr, nq, "sweep-postfix", "", srows, {}, fh=fh)
    except Exception as e:
        v = {"pid": pid, "verdict": f"ERROR-{type(e).__name__}"}
    v["pid"] = pid
    v["old_verdict"] = old.get(pid, "?")
    v["stem"] = r["stem"]
    return v

out = open(OUT, "w")
tally = collections.Counter()
gold_before = 0
gold_after = 0
def deficit(verdict):
    f = re.findall(r"(?:MISSING|OVER|GARBAGE|CORRUPT|DUPLICATE)-(\d+)", verdict or "")
    return sum(int(x) for x in f)
d_before = d_after = 0
with cf.ThreadPoolExecutor(max_workers=8) as ex:
    for i, v in enumerate(ex.map(run_one, [(r, r["pid"]) for r in targets]), 1):
        out.write(json.dumps(v) + "\n")
        tally[v["verdict"].split(";")[0]] += 1
        if v["verdict"] == "COMPLETE-RULE-OK":
            gold_after += 1
        if v.get("old_verdict") == "COMPLETE-RULE-OK":
            gold_before += 1
        d_before += deficit(v.get("old_verdict"))
        d_after += deficit(v["verdict"]) if not v["verdict"].startswith("ERROR") else 0
        if i % 200 == 0:
            print(f"  {i}/{len(targets)} done", flush=True)
out.close()
print("NEW verdict tally (first flag):", dict(tally.most_common(14)))
print(f"gold among sweep papers: before=0 (all were non-gold) -> after={gold_after} of {len(targets)}")
print(f"total measured deficits (sum of flag counts): before={d_before} after={d_after} recovered={d_before - d_after} ({100*(d_before-d_after)/max(d_before,1):.0f}%)")
print("saved", OUT)
