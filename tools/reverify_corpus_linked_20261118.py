#!/usr/bin/env python3
"""LOCAL RE-VERIFICATION of the 2,523 corpus-linked 'pdf-less' papers (owner rule:
work on already-got PDFs; zero vision calls; zero DB writes).
For each paper: pdftotext its (now local) corpus PDF, compare printed atomic questions
vs stored rows, emit the same verdict semantics as completeness_run.py.
Output: scripts/out/completeness_reverify2523_20261118.jsonl"""
import json, os, re, sys, subprocess, collections
import concurrent.futures as cf
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
from completeness_run import norm, parse_printed, FORMULA
import psycopg2

PATHS = json.load(open("/home/z/my-project/scripts/out/pdfless_A_paths_20261118.json"))
OUT = "/home/z/my-project/scripts/out/completeness_reverify2523_20261118.jsonl"
REPO = "/home/z/my-project/repo_b"

conn = psycopg2.connect(DSN, connect_timeout=30)
cur = conn.cursor()
pids = list(PATHS.keys())
cur.execute("""SELECT o.paper_id, (q.parent_id IS NOT NULL), q.text
               FROM occurrences o JOIN questions q ON q.id=o.question_id
               WHERE o.paper_id = ANY(%s::uuid[])""", (pids,))
stored = collections.defaultdict(list)
for pid, is_sub, text in cur.fetchall():
    stored[pid].append((None, is_sub, (text or "")[:300], None))
cur.execute("""SELECT id, coalesce(file_hash,''), extraction_status, coalesce(n_questions,0)
               FROM papers WHERE id = ANY(%s::uuid[])""", (pids,))
meta = {str(r[0]): r[1:] for r in cur.fetchall()}
# also find the one non-pdf-less paper whose file_hash is not in corpus (63 vs 62 reconciliation)
cur.execute("""SELECT id, subject_code, year, extraction_status FROM papers
               WHERE coalesce(file_hash,'') <> '' AND extraction_status IS NOT NULL
               AND id NOT IN (SELECT id::text FROM papers WHERE false)""") if False else None
conn.close()
print("papers:", len(pids), "with stored rows:", len(stored), flush=True)

def text_of(path):
    try:
        return subprocess.run(["pdftotext", "-layout", path, "-"],
                              capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    except Exception:
        return ""

def verify(pid):
    fh, st, nq = meta.get(pid, ("", "?", 0))
    path = os.path.join(REPO, PATHS[pid])
    r = {"pid": pid, "file_hash": fh[:16], "status": st, "corpus_path": PATHS[pid]}
    txt = text_of(path)
    if len(txt) < 400:
        r["verdict"] = "UNMEASURABLE(scan)"; r["text_chars"] = len(txt); return r
    printed = parse_printed(txt)
    rows = stored.get(pid, [])
    garbage = [x for x in rows if (not norm(x[2])) or FORMULA.match(x[2] or "")]
    seen = {}
    for x in rows:
        k = norm(x[2])[:120]
        if k and len(k) > 8:
            seen[k] = seen.get(k, 0) + 1
    dups = sum(v - 1 for v in seen.values() if v > 1)
    atomic = printed["n_parts"] + printed["n_standalone"]
    stored_sub = [x for x in rows if x[1]]
    stored_top = [x for x in rows if not x[1]]
    printed_texts = [norm(pt["text"]) for c in printed["containers"]
                     for pt in c["parts"] if len(norm(pt["text"])) > 10]
    corrupt = 0
    for x in stored_sub:
        t = norm(x[2])[:150]
        if len(t) > 12 and printed_texts:
            best = max(difflib_sim(t, pt) for pt in printed_texts[:60])
            if best < 0.75: corrupt += 1
    clean_stored = len(stored_sub) + len([x for x in stored_top
                                          if norm(x[2]) and not FORMULA.match(x[2] or "")])
    missing = max(0, atomic - clean_stored)
    over = max(0, clean_stored - atomic - 2)
    r.update({"text_chars": len(txt), "atomic_printed": atomic,
              "stored_occ": len(rows), "stored_sub": len(stored_sub),
              "stored_top": len(stored_top), "garbage_rows": len(garbage),
              "dup_texts": dups, "corrupt_texts": corrupt,
              "missing_atomic": missing, "over_atomic": over})
    flags = []
    if atomic == 0: flags.append("PARSE-FAIL(printed=0)")
    else:
        if missing > 0: flags.append(f"MISSING-{missing}")
        if over > 0: flags.append(f"OVER-{over}")
    if garbage: flags.append(f"GARBAGE-{len(garbage)}")
    if dups: flags.append(f"DUPLICATE-{dups}")
    if corrupt: flags.append(f"CORRUPT-{corrupt}")
    r["verdict"] = "COMPLETE-RULE-OK" if not flags else ";".join(flags)
    return r

def difflib_sim(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()

results = []
with cf.ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(verify, pid): pid for pid in pids}
    n = 0
    for fut in cf.as_completed(futs):
        try:
            results.append(fut.result())
        except Exception as e:
            results.append({"pid": futs[fut], "verdict": f"ERROR-{type(e).__name__}"})
        n += 1
        if n % 250 == 0:
            print(f"  {n}/{len(pids)}", flush=True)

with open(OUT, "w", encoding="utf-8") as f:
    for r in results:
        f.write(json.dumps(r) + "\n")

tally = collections.Counter()
def fam(v):
    if v == "COMPLETE-RULE-OK": return "clean"
    if v.startswith("UNMEASURABLE"): return "scan"
    if v.startswith("PARSE-FAIL"): return "parse-fail"
    if v.startswith("ERROR"): return "error"
    m = re.search(r"MISSING-(\d+)", v)
    if m and int(m.group(1)) >= 3: return "real-gaps"
    return "minor-gaps"
for r in results:
    tally[fam(r["verdict"])] += 1
print("\nVERDICT TALLY for the 2,523 corpus-linked papers:")
for k, v in tally.most_common():
    print(f"  {k}: {v}")
tot = sum(tally.values())
print("total:", tot)
clean = tally.get("clean", 0) + tally.get("minor-gaps", 0)
print(f"verifiable-and-trustworthy-band: {clean}/{tot} ({100.0*clean/tot:.1f}%)")
