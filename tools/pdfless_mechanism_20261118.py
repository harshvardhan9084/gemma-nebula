#!/usr/bin/env python3
"""MECHANISM PROOF for owner question 'how can 2,585 pdf-less be counted if they don't exist?'
READ-ONLY. Shows exactly what is counted: DB paper rows whose source_url filename
does not match any of the 11,025 corpus PDF filenames (join = last path segment).
Also: URL-shape census of the 2,585, question-row split fuzzy-recoverable vs truly-absent,
and 5 concrete example rows. Zero writes."""
import json, re, sys, collections
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
import psycopg2

rows = [json.loads(l) for l in open("/home/z/my-project/scripts/out/completeness_full.jsonl")]
def bucket(v):
    if v == "PDF-ABSENT": return "pdf-less"
    if v.startswith("UNMEASURABLE"): return "scan-grade"
    if v.startswith("PARSE-FAIL"): return "parse-fail"
    return "other"
pdfless = [r for r in rows if bucket(r["verdict"]) == "pdf-less"]

# corpus stems (from git index of gemma-nebula clone)
import subprocess
files = subprocess.run(["git", "ls-files"], cwd="/home/z/my-project/repo_b",
                       capture_output=True, text=True).stdout.splitlines()
corpus_stems = {f.rsplit("/", 1)[-1][:-4].lower() for f in files if f.lower().endswith(".pdf")}

pids = [r["pid"] for r in pdfless]
conn = psycopg2.connect(DSN, connect_timeout=30); conn.autocommit = True
cur = conn.cursor()
cur.execute("""SELECT id, coalesce(source_url,''), extraction_status, coalesce(n_questions,0),
                      to_char(ingested_at,'YYYY-MM-DD')
               FROM papers WHERE id = ANY(%s::uuid[])""", (pids,))
db = {str(r[0]): r[1:] for r in cur.fetchall()}

# sanity: reproduce the verifier's stem rule and PDF-ABSENT condition
repro_absent, repro_present = 0, 0
for r in pdfless:
    url = db.get(r["pid"], ("",))[0]
    stem = (url or "").rsplit("/", 1)[-1].replace(".html", "")
    if stem.lower() not in corpus_stems: repro_absent += 1
    else: repro_present += 1

shapes = collections.Counter()
for r in pdfless:
    u = db.get(r["pid"], ("",))[0]
    if u.startswith("http"): pass
    if "drive.google" in u or "docs.google" in u: shapes["drive.google link"] += 1
    elif u.endswith(".pdf"): shapes["direct .pdf file url"] += 1
    elif u.endswith(".html"): shapes[".html listing-page url"] += 1
    elif u.strip() == "": shapes["EMPTY source_url"] += 1
    else: shapes["other"] += 1

# fuzzy-recoverable split (re-run the strict-ish matcher from pdf_owner_answers)
import difflib
CODE = re.compile(r"([a-z]{2,4})-?(\d{3})([a-z]?)")
def code_of(stem):
    m = CODE.search(stem)
    return (m.group(1) + m.group(2) + (m.group(3) or "")) if m else None
YEARTOK = re.compile(r"(20\d{2})(?:-(\d{2}))?")
def year_span(stem):
    spans = []
    for m in YEARTOK.finditer(stem):
        a = int(m.group(1)); b = 2000 + int(m.group(2)) if m.group(2) else a
        spans.append((min(a, b), max(a, b)))
    if not spans: return None
    return (min(s[0] for s in spans), max(s[1] for s in spans))
by_code = collections.defaultdict(list)
for k in corpus_stems:
    c = code_of(k)
    if c: by_code[c].append(k)
fuzzy_pids, absent_pids = set(), set()
for r in pdfless:
    st = r["stem"].lower()
    yr = year_span(r.get("yr") or "") or year_span(st)
    c = code_of(st)
    hit = None
    for cand in by_code.get(c, []):
        cs = year_span(cand)
        if yr and cs and cs[0] <= yr[1] and cs[1] >= yr[0]:
            hit = cand; break
    if hit is None:
        pool = [k for k in by_code.get(c, [])
                if (lambda cs: yr and cs and cs[0] <= yr[1] and cs[1] >= yr[0])(year_span(k))]
        best = difflib.get_close_matches(st, pool, n=1, cutoff=0.90) if pool else []
        hit = best[0] if best else None
    (fuzzy_pids if hit else absent_pids).add(r["pid"])

def qrows(pidset):
    cur.execute("""SELECT count(*) FROM occurrences o WHERE o.paper_id = ANY(%s::uuid[])""",
                (list(pidset),))
    return cur.fetchone()[0]

print("PDF-ABSENT rows in completeness_full.jsonl:", len(pdfless))
print("repro: stem-rule says absent for", repro_absent, "of them | stem-rule says PRESENT for", repro_present)
print("source_url shapes:", dict(shapes))
print("fuzzy-recoverable (file under another name):", len(fuzzy_pids),
      "| question rows in them:", qrows(fuzzy_pids))
print("truly-absent (no same-code+year file anywhere):", len(absent_pids),
      "| question rows in them:", qrows(absent_pids))
st = collections.Counter(db[r["pid"]][1] for r in pdfless if r["pid"] in db)
print("status mix:", dict(st))
print("\n5 concrete example rows (id[:8], source_url, status, n_questions, ingested):")
for r in pdfless[:5]:
    u, stt, nq, ing = db.get(r["pid"], ("?", "?", "?", "?"))
    print(f"  {r['pid'][:8]}  nq={nq:3d}  {stt:8s}  ingested={ing}\n     url: {u[:110]}")
# also one fuzzy-recoverable example and one truly-absent example with urls
fp = sorted(fuzzy_pids)[0]; ap = sorted(absent_pids)[0]
for tag, pid in (("FUZZY-RECOVERABLE", fp), ("TRULY-ABSENT", ap)):
    u, stt, nq, ing = db.get(pid, ("?", "?", "?", "?"))
    print(f"\n{tag}: {pid[:8]} nq={nq} {stt} ingested={ing}\n  url: {u[:110]}")
