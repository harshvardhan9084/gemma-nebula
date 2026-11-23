#!/usr/bin/env python3
"""FULL CORPUS HASH SWEEP + exact reclassification of the 2,585 PDF-ABSENT rows.
1. sha256 all 11,025 local corpus PDFs -> scripts/out/corpus_hashmap.json (hash -> repo path).
   This is the asset the 11-14 verifier lacked (it joined by filename only).
2. Reclassify: row.file_hash in corpus hashes = file IS in repo (A); else C (absent);
   content-confirmed twin (B) kept from the earlier resolution.
3. Stats for the whole DB: how many of all 10,883 papers are corpus-linked by file_hash.
READ-ONLY on DB."""
import json, hashlib, os, sys, collections
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
import psycopg2

REPO = "/home/z/my-project/repo_b"
HM = "/home/z/my-project/scripts/out/corpus_hashmap.json"

# ---- 1. hash every corpus pdf ----
hmap = {}
paths = []
for root, _, fns in os.walk(os.path.join(REPO, "corpus")):
    for fn in fns:
        if fn.lower().endswith(".pdf"):
            paths.append(os.path.join(root, fn))
print("corpus pdfs on disk:", len(paths), flush=True)
for i, p in enumerate(paths):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    hmap[h.hexdigest()] = os.path.relpath(p, REPO)
    if (i + 1) % 2000 == 0:
        print(f"  hashed {i+1}", flush=True)
json.dump(hmap, open(HM, "w"))
print("hash map written:", len(hmap), "entries", flush=True)

# ---- 2. exact reclassification ----
rows = [json.loads(l) for l in open("/home/z/my-project/scripts/out/completeness_full.jsonl")]
pdfless = [r for r in rows if r["verdict"] == "PDF-ABSENT"]
res = json.load(open("/home/z/my-project/scripts/out/pdfless_content_resolution_20261118.json"))
verdict_by_pid = {r["pid"]: r["verdict"] for r in res["results"]}
twin_by_pid = {r["pid"]: r.get("twin") for r in res["results"] if r.get("twin")}
pids = [r["pid"] for r in pdfless]

conn = psycopg2.connect(DSN, connect_timeout=30); conn.autocommit = True
cur = conn.cursor()
cur.execute("""SELECT id, coalesce(file_hash,''), extraction_status, coalesce(n_questions,0),
                      coalesce(source_url,'') FROM papers WHERE id = ANY(%s::uuid[])""", (pids,))
db = {str(r[0]): r[1:] for r in cur.fetchall()}

cls = collections.Counter()
nq_cls = collections.Counter()
st_cls = collections.Counter()
paths_A = {}
for r in pdfless:
    pid = r["pid"]
    fh, st, nq, url = db.get(pid, ("", "?", 0, ""))
    if fh and fh in hmap:
        c = "A-file-in-repo"
        paths_A[pid] = hmap[fh]
    elif verdict_by_pid.get(pid) == "CONFIRMED":
        c = "B-duplicate-identity"
    else:
        c = "C-no-file-in-repo"
    cls[c] += 1; nq_cls[c] += nq; st_cls[(c, st)] += 1
print("\nEXACT reclassification of the 2,585:")
for k, v in cls.most_common():
    print(f"  {k}: {v} papers | {nq_cls[k]} question rows")
    for kk, vv in sorted(st_cls.items()):
        if kk[0] == k: print(f"      {kk[1]}: {vv}")
json.dump(paths_A, open("/home/z/my-project/scripts/out/pdfless_A_paths_20261118.json", "w"))

# ---- 3. whole-DB corpus linkage ----
cur.execute("SELECT count(*), count(distinct file_hash) FROM papers")
tot, dist = cur.fetchone()
cur.execute("SELECT count(*) FROM papers WHERE coalesce(file_hash,'') = ANY(%s)", (list(hmap.keys())[:0],))
cur.execute("""SELECT count(*) FROM papers WHERE file_hash = ANY(%s)""", (list(hmap.keys()),))
linked = cur.fetchone()[0]
cur.execute("SELECT coalesce(sum(n_questions),0) FROM papers WHERE file_hash = ANY(%s)", (list(hmap.keys()),))
lq = cur.fetchone()[0]
print(f"\nwhole DB: {tot} papers, {dist} distinct file_hash | corpus-linked by file_hash: {linked} papers")
print("question rows in corpus-linked papers:", lq)
# orphan hashes (hashes in DB but not in corpus)
cur.execute("SELECT count(*) FROM papers WHERE coalesce(file_hash,'') <> '' AND (file_hash = ANY(%s)) = false", (list(hmap.keys()),))
print("papers whose file_hash is NOT in the corpus (transient-only files):", cur.fetchone()[0])
