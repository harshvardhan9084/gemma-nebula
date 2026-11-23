#!/usr/bin/env python3
"""Do the CONFIRMED twins already exist as papers rows (by file_hash)?
If yes, the 'pdf-less' rows are cross-era DUPLICATES, not missing files.
READ-ONLY. Uses the spotcheck/ dumps (same bytes as repo blobs)."""
import json, hashlib, os, re, sys, collections
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
import psycopg2

res = json.load(open("/home/z/my-project/scripts/out/pdfless_content_resolution_20261118.json"))
BUCK = json.load(open("/home/z/my-project/scripts/out/pdfless_strict_buckets_20261118.json"))["rows"]
hits_by_pid = {r["pid"]: (r.get("hits") or [r.get("hit")]) for r in BUCK}
OUTDIR = "/home/z/my-project/scripts/out/spotcheck"
files = os.listdir(OUTDIR)
# path in repo -> local dump filename mapping: dump name = re.sub(r'[^A-Za-z0-9_.-]','_',path)
import subprocess
gitfiles = subprocess.run(["git", "ls-files"], cwd="/home/z/my-project/repo_b",
                          capture_output=True, text=True).stdout.splitlines()
def dumpname(path):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", path)

conf = [r for r in res["results"] if r["verdict"] == "CONFIRMED"]
weak = [r for r in res["results"] if r["verdict"] == "WEAK"]
nott = [r for r in res["results"] if r["verdict"] == "NOT-SCORABLE"]

# hash every dumped candidate pdf
hash_of = {}
for f in files:
    if f.endswith(".pdf"):
        h = hashlib.sha256()
        with open(os.path.join(OUTDIR, f), "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        hash_of[f] = h.hexdigest()

def twin_hashes(r):
    hits = [h for h in hits_by_pid.get(r["pid"], []) if h]
    out = []
    for h in hits:
        if not h: continue
        # find repo path by stem (case-insensitive)
        for g in gitfiles:
            if g.lower().endswith(".pdf") and g.rsplit("/", 1)[-1][:-4].lower() == h:
                dn = dumpname(g)
                if dn in hash_of:
                    out.append((h, hash_of[dn]))
                break
    return out

rows = conf + weak + nott
twinmap = {}
for r in rows:
    twinmap[r["pid"]] = twin_hashes(r)
all_hashes = list({h for hs in twinmap.values() for _, h in hs})
print("distinct twin file hashes to check:", len(all_hashes))

conn = psycopg2.connect(DSN, connect_timeout=30); conn.autocommit = True
cur = conn.cursor()
found = {}
for i in range(0, len(all_hashes), 100):
    cur.execute("""SELECT file_hash, id, extraction_status, coalesce(n_questions,0)
                   FROM papers WHERE file_hash = ANY(%s)""", (all_hashes[i:i+100],))
    for fh, pid, st, nq in cur.fetchall():
        found[fh] = (str(pid), st, nq)

twin_row_exists = collections.Counter()
status_of_twin = collections.Counter()
for cls, group in (("CONFIRMED", conf), ("WEAK", weak), ("NOT-SCORABLE", nott)):
    for r in group:
        has = False
        for stem, fh in twinmap[r["pid"]]:
            if fh in found:
                has = True
                status_of_twin[(cls, found[fh][1])] += 1
        twin_row_exists[(cls, has)] += 1
print("\nrows whose twin file_hash already HAS a papers row:")
for k, v in sorted(twin_row_exists.items()):
    print("  ", k, v)
print("\n(corr) status of the twin papers rows:")
for k, v in sorted(status_of_twin.items()):
    print("  ", k, v)

# question rows in the pdf-less rows vs in twin rows
conf_pids = [r["pid"] for r in conf]
cur.execute("SELECT count(*) FROM occurrences WHERE paper_id = ANY(%s::uuid[])", (conf_pids,))
print("\nquestion rows inside CONFIRMED pdf-less rows:", cur.fetchone()[0])
conf_twin_ids = [found[fh][0] for r in conf for _, fh in twinmap[r["pid"]] if fh in found]
conf_twin_ids = list(set(conf_twin_ids))
cur.execute("SELECT count(*) FROM occurrences WHERE paper_id = ANY(%s::uuid[])", (conf_twin_ids,))
print("question rows inside their twin rows:", cur.fetchone()[0])
# sample pair
if conf:
    r = conf[0]
    for stem, fh in twinmap[r["pid"]]:
        if fh in found:
            cur.execute("""SELECT id, source_url, extraction_status, coalesce(n_questions,0), year, subject_code
                           FROM papers WHERE id=%s""", (r["pid"],))
            a = cur.fetchone()
            cur.execute("""SELECT id, source_url, extraction_status, coalesce(n_questions,0), year, subject_code
                           FROM papers WHERE file_hash=%s""", (fh,))
            b = cur.fetchone()
            print("\nSAMPLE PAIR (pdf-less row -> twin row):")
            print("  pdf-less:", a)
            print("  twin    :", b)
