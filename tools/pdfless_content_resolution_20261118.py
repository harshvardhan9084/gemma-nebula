#!/usr/bin/env python3
"""FULL CONTENT RESOLUTION of pdf-less relink candidates (READ-ONLY DB, local git blobs).
For each of the 1,172 name-candidate papers: dump every candidate PDF from repo_b's git
index (case-insensitive lookup), pdftotext, score stored DB questions against printed text.
CONFIRMED = found>=40% (ambiguous buckets need best-second>=15 margin).
Output: scripts/out/pdfless_content_resolution_20261118.json
No DB writes. No downloads. No new files added to any repo."""
import json, re, subprocess, sys, difflib, os, collections
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
import psycopg2
import concurrent.futures as cf

OUTDIR = "/home/z/my-project/scripts/out/spotcheck"
os.makedirs(OUTDIR, exist_ok=True)
BUCKETS = json.load(open("/home/z/my-project/scripts/out/pdfless_strict_buckets_20261118.json"))["rows"]
rows = [r for r in BUCKETS if r["bucket"] != "NO-MATCH"]
print("candidate papers:", len(rows), flush=True)

# ---- case-preserving path index (stems lowercased as keys) ----
files = subprocess.run(["git", "ls-files"], cwd="/home/z/my-project/repo_b",
                       capture_output=True, text=True).stdout.splitlines()
path_of_stem = {}
for f in files:
    if f.lower().endswith(".pdf"):
        path_of_stem[f.rsplit("/", 1)[-1][:-4].lower()] = f

# ---- PDF text cache (dump once per distinct stem) ----
def dump_txt(stem):
    path = path_of_stem.get(stem)
    if not path: return None
    fn = os.path.join(OUTDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", path))
    if not os.path.exists(fn) or os.path.getsize(fn) < 1000:
        p = subprocess.run(["git", "show", f"HEAD:{path}"], cwd="/home/z/my-project/repo_b",
                           capture_output=True)
        if p.returncode != 0 or p.stdout[:4] != b"%PDF": return None
        open(fn, "wb").write(p.stdout)
    tfn = fn + ".txt"
    if not os.path.exists(tfn):
        subprocess.run(["pdftotext", fn, tfn], timeout=90, capture_output=True)
    if not os.path.exists(tfn): return ""
    return open(tfn, encoding="utf-8", errors="replace").read()

def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()

# ---- DB stored questions per paper ----
conn = psycopg2.connect(DSN, connect_timeout=30); conn.autocommit = True
cur = conn.cursor()
pids = [r["pid"] for r in rows]
cur.execute("""SELECT o.paper_id, q.text FROM occurrences o JOIN questions q ON q.id=o.question_id
               WHERE o.paper_id = ANY(%s::uuid[])""", (pids,))
stored = collections.defaultdict(list)
for pid, text in cur.fetchall():
    t = norm(text)
    if len(t) >= 15: stored[pid].append(t)
conn.close()
print("papers with stored questions:", len(stored), flush=True)

# ---- distinct candidate stems to dump ----
want = set()
for r in rows:
    for h in (r.get("hits") or [r.get("hit")]):
        if h: want.add(h)
print("distinct candidate PDFs:", len(want), flush=True)

def dump_many(stems):
    return {s: dump_txt(s) for s in stems}
texts = {}
with cf.ThreadPoolExecutor(max_workers=8) as ex:
    futs = []
    stems = sorted(want)
    CH = 60
    for i in range(0, len(stems), CH):
        futs.append(ex.submit(dump_many, stems[i:i+CH]))
    for f in cf.as_completed(futs):
        texts.update(f.result())
    print("pdfs dumped+parsed:", sum(1 for v in texts.values() if v), flush=True)

def score(pid, txt):
    qs = stored.get(pid, [])
    if len(qs) < 3 or not txt or len(txt) < 200:
        return None
    ntxt = norm(txt)
    lines = [norm(l) for l in txt.splitlines() if len(norm(l)) >= 15]
    found = 0
    for q in qs[:80]:
        if q in ntxt: found += 1; continue
        best = 0.0
        for pl in lines[:400]:
            if abs(len(pl) - len(q)) > 60: continue
            r = difflib.SequenceMatcher(None, q, pl).ratio()
            if r > best: best = r
        if best >= 0.80: found += 1
    n = min(80, len(qs))
    return 100.0 * found / n

results = []
def resolve(rec):
    hits = rec.get("hits") or [rec.get("hit")]
    scores = []
    for h in hits:
        if not h: continue
        sc = score(rec["pid"], texts.get(h))
        if sc is not None: scores.append((h, sc))
    out = {"pid": rec["pid"], "stem": rec["stem"], "bucket": rec["bucket"], "scores": scores}
    if not scores:
        out["verdict"] = "NOT-SCORABLE"
    else:
        scores.sort(key=lambda x: -x[1])
        best, second = scores[0], (scores[1] if len(scores) > 1 else None)
        margin = (best[1] - second[1]) if second else 100.0
        if best[1] >= 40.0 and (len(scores) == 1 or margin >= 15.0):
            out["verdict"] = "CONFIRMED"; out["twin"] = best[0]; out["pct"] = best[1]
        elif best[1] >= 20.0:
            out["verdict"] = "WEAK"; out["twin"] = best[0]; out["pct"] = best[1]
        else:
            out["verdict"] = "REJECTED"
    return out

with cf.ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(resolve, rows))

tally = collections.Counter(r["verdict"] for r in results)
print("verdicts:", dict(tally), flush=True)
json.dump({"tally": dict(tally), "results": results},
          open("/home/z/my-project/scripts/out/pdfless_content_resolution_20261118.json", "w"), indent=1)
print("\nsample CONFIRMED:")
shown = 0
for r in results:
    if r["verdict"] == "CONFIRMED":
        print(f"  {r['stem'][:52]:52s} -> {r['twin'][:52]:52s} {r['pct']}%")
        shown += 1
        if shown >= 8: break
print("\nsample REJECTED (name matched, content disagreed):")
shown = 0
for r in results:
    if r["verdict"] == "REJECTED":
        print(f"  {r['stem'][:52]:52s} best={r['scores'][0][1] if r['scores'] else None}")
        shown += 1
        if shown >= 5: break
