#!/usr/bin/env python3
"""POST-FIX RE-VERIFICATION of the 119 real-gap papers fixed by run #109, using
completeness_run.verify() DIRECTLY (hash-first local lookup, same semantics as
the census). Read-only on DB. Output: postfix_reverify119_20261119.jsonl"""
import json, re, sys, collections
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
import completeness_run as cr
import psycopg2

OUT = "/home/z/my-project/scripts/out/postfix_reverify119_20261119.jsonl"

rows = [json.loads(l) for l in open("/home/z/my-project/repo_b/tools/completeness_reverify2523_20261118.jsonl")]
targets = []
for r in rows:
    miss = max([int(m) for m in re.findall(r"MISSING-(\d+)", r["verdict"])] + [0])
    if miss >= 3:
        r["old_missing"] = miss
        targets.append(r)
pids = [r["pid"] for r in targets]

conn = psycopg2.connect(DSN, connect_timeout=30)
cur = conn.cursor()
cur.execute("""SELECT o.paper_id, (q.parent_id IS NOT NULL), q.text
               FROM occurrences o JOIN questions q ON q.id = o.question_id
               WHERE o.paper_id = ANY(%s::uuid[])""", (pids,))
stored = collections.defaultdict(list)
for pid, is_sub, text in cur.fetchall():
    stored[pid].append((None, is_sub, (text or "")[:300], None))
cur.execute("SELECT id, coalesce(file_hash,''), extraction_status, coalesce(n_questions,0), subject_code, year FROM papers WHERE id = ANY(%s::uuid[])", (pids,))
meta = {str(r[0]): r[1:] for r in cur.fetchall()}
conn.close()
print("papers:", len(pids), "with stored rows:", len(stored), flush=True)

out = open(OUT, "w")
tally = collections.Counter()
recovery = collections.Counter()
for r in targets:
    pid = r["pid"]
    fh, st, nq, sc, yr = meta.get(pid, ("", "?", 0, "", ""))
    srows = stored.get(pid, [])
    v = cr.verify(pid, sc, yr, nq, "cron-postfix", "", srows, {}, fh=fh)
    v["old_verdict"] = r["verdict"]; v["old_missing"] = r["old_missing"]
    out.write(json.dumps(v) + "\n")
    miss = v.get("missing_atomic", 0)
    tally[v["verdict"]] += 1
    if miss == 0:
        recovery["clean"] += 1
    elif miss < r["old_missing"]:
        recovery["improved"] += 1
    elif miss == r["old_missing"]:
        recovery["unchanged"] += 1
    else:
        recovery["worse"] += 1
out.close()
print("tally:", dict(tally.most_common(12)))
print("recovery:", dict(recovery))
sum_old = sum(r["old_missing"] for r in targets)
sum_new = 0
for l in open(OUT):
    sum_new += json.loads(l).get("missing_atomic", 0)
print(f"atomic deficits: before={sum_old} after={sum_new} recovered={sum_old - sum_new}")
