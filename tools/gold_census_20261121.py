#!/usr/bin/env python3
"""DEFINITIVE GOLD CENSUS 2026-11-21 — re-verify every hash-linked paper against
the CURRENT (post duplicate-cleanup, post run-#108 fixes) DB.

Bands:
  A. is_text_layer=true  + hash-linked  -> local pipe verify (git cat-file blob | pdftotext)
                                           verdict: COMPLETE-RULE-OK = zero flags (strict)
  B. is_text_layer=false + hash-linked, plus A's UNMEASURABLE papers
                                           -> score vs stored tesseract printed_atomic
                                           verdict: COMPLETE-RULE-OK only if missing==0 and over==0
  C. hash absent from corpus            -> PDF-ABSENT (unverifiable)

Zero AI calls, zero DB writes, zero GHA dispatch. Resumable: text verdicts appended
incrementally; on rerun, already-done pids are skipped.
Outputs:
  scripts/out/gold_text_20261121.jsonl   (phase A rows)
  scripts/out/gold_scan_20261121.jsonl   (phase B rows, rewritten each run)
  scripts/out/gold_census_20261121.jsonl (merged one row per paper)
  scripts/out/gold_pids_20261121.json    (gold pid list + stats)
"""
import json, os, re, sys, subprocess, collections, time
import concurrent.futures as cf
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
from completeness_run import norm, parse_printed, FORMULA
import psycopg2

REPO = "/home/z/my-project/repo_b"
OUTDIR = "/home/z/my-project/scripts/out"
TEXT_OUT = os.path.join(OUTDIR, "gold_text_20261121.jsonl")
SCAN_OUT = os.path.join(OUTDIR, "gold_scan_20261121.jsonl")
MERGE_OUT = os.path.join(OUTDIR, "gold_census_20261121.jsonl")
GOLD_OUT = os.path.join(OUTDIR, "gold_pids_20261121.json")
TIME_BUDGET = int(os.environ.get("GOLD_TIME_BUDGET", "500"))
DEADLINE = time.time() + TIME_BUDGET

def difflib_sim(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()

# ---------------- inputs ----------------
hm = json.load(open(os.path.join(OUTDIR, "corpus_hashmap.json")))
# path -> blob sha (one ls-tree pass)
ls = subprocess.run("git ls-tree -r HEAD -- corpus", cwd=REPO, shell=True,
                    capture_output=True, timeout=300)
blobmap = {}
for line in ls.stdout.decode("utf-8", "replace").splitlines():
    m = re.match(r"^\d+ blob ([0-9a-f]{40})\t(.+)$", line)
    if m:
        blobmap[m.group(2)] = m.group(1)
print("blobmap paths:", len(blobmap), flush=True)

conn = psycopg2.connect(DSN, connect_timeout=30)
cur = conn.cursor()
cur.execute("""SELECT id, coalesce(file_hash,''), coalesce(is_text_layer, true),
                      extraction_status, coalesce(n_questions,0), subject_code, year FROM papers""")
papers = {}
for pid, fh, itl, st, nq, sc, yr in cur.fetchall():
    papers[str(pid)] = {"fh": fh, "itl": bool(itl), "st": st, "nq": nq, "sc": sc, "yr": yr}
print("papers:", len(papers), flush=True)

# stored rows for every hash-linked paper (current DB state)
linked = {pid: v for pid, v in papers.items() if v["fh"] and v["fh"] in hm}
print("hash-linked papers:", len(linked), flush=True)
pid_list = list(linked.keys())
stored = collections.defaultdict(list)
CH = 2000
for i in range(0, len(pid_list), CH):
    cur.execute("""SELECT o.paper_id, (q.parent_id IS NOT NULL), q.text
                   FROM occurrences o JOIN questions q ON q.id = o.question_id
                   WHERE o.paper_id = ANY(%s::uuid[])""", (pid_list[i:i + CH],))
    for pid, is_sub, text in cur.fetchall():
        stored[str(pid)].append((None, is_sub, (text or "")[:300], None))
conn.close()
print("papers with stored rows:", len(stored), flush=True)

# ---------------- phase A: text verify (resumable) ----------------
done = set()
if os.path.exists(TEXT_OUT):
    for l in open(TEXT_OUT):
        try:
            done.add(json.loads(l)["pid"])
        except Exception:
            pass
todo = [pid for pid, v in linked.items()
        if v["itl"] and pid not in done and v["fh"] in hm and hm[v["fh"]] in blobmap]
print("phase A: done", len(done), "todo", len(todo), flush=True)

def text_of(path):
    try:
        blob = blobmap.get(path)
        if not blob:
            return ""
        r = subprocess.run(f"git cat-file blob {blob} | pdftotext -layout - -",
                           cwd=REPO, shell=True, capture_output=True, timeout=90)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""

def verify(pid):
    v = linked[pid]
    path = os.path.join(REPO, hm[v["fh"]])
    r = {"pid": pid, "file_hash": v["fh"][:16], "status": v["st"], "corpus_path": hm[v["fh"]]}
    txt = text_of(hm[v["fh"]])
    if len(txt) < 400:
        r["verdict"] = "UNMEASURABLE(scan)"
        r["text_chars"] = len(txt)
        return r
    printed = parse_printed(txt)
    rows = stored.get(pid, [])
    garbage = [x for x in rows if (not norm(x[2])) or FORMULA.match(x[2] or "")]
    seen = {}
    for x in rows:
        k = norm(x[2])[:120]
        if k and len(k) > 8:
            seen[k] = seen.get(k, 0) + 1
    dups = sum(vv - 1 for vv in seen.values() if vv > 1)
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
            if best < 0.75:
                corrupt += 1
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
    if atomic == 0:
        flags.append("PARSE-FAIL(printed=0)")
    else:
        if missing > 0:
            flags.append(f"MISSING-{missing}")
        if over > 0:
            flags.append(f"OVER-{over}")
    if garbage:
        flags.append(f"GARBAGE-{len(garbage)}")
    if dups:
        flags.append(f"DUPLICATE-{dups}")
    if corrupt:
        flags.append(f"CORRUPT-{corrupt}")
    r["verdict"] = "COMPLETE-RULE-OK" if not flags else ";".join(flags)
    return r

fout = open(TEXT_OUT, "a", encoding="utf-8")
n_done = 0
if todo:
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(verify, pid): pid for pid in todo}
        for fut in cf.as_completed(futs):
            pid = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"pid": pid, "verdict": f"ERROR-{type(e).__name__}"}
            fout.write(json.dumps(r) + "\n")
            fout.flush()
            n_done += 1
            if time.time() > DEADLINE:
                print("TIME BUDGET hit mid-phase-A; resumable — stopping early", flush=True)
                ex.shutdown(wait=False, cancel_futures=True)
                break
fout.close()
print(f"phase A: wrote {n_done} new verdicts", flush=True)

# ---------------- phase B: scan scoring vs stored tesseract evidence ----------------
ocr = {}
for p in ["evd_scan559_shard0.jsonl", "evd_scan559_shard1.jsonl", "evd_parsefail_750.jsonl"]:
    fp = os.path.join(OUTDIR, p)
    if os.path.exists(fp):
        for l in open(fp):
            try:
                v = json.loads(l)
                if v.get("stem"):
                    ocr[v["stem"].lower()] = v
            except Exception:
                pass
# local parse-fail runs #1-#3 (500 stems)
fp = os.path.join(OUTDIR, "ocr_verify_parsefail_750.jsonl")
if os.path.exists(fp):
    for l in open(fp):
        try:
            v = json.loads(l)
            if v.get("stem"):
                ocr[v["stem"].lower()] = v
        except Exception:
            pass
print("tesseract evidence stems:", len(ocr), flush=True)

def stem_of(path):
    return os.path.splitext(os.path.basename(path))[0].lower()

scan_pids = [pid for pid, v in linked.items() if not v["itl"]]
# also phase-A UNMEASURABLE papers (they are scans in disguise)
for l in open(TEXT_OUT):
    try:
        r = json.loads(l)
    except Exception:
        continue
    if r.get("verdict") == "UNMEASURABLE(scan)" and r["pid"] in linked:
        scan_pids.append(r["pid"])
scan_pids = list(dict.fromkeys(scan_pids))
print("phase B scan papers:", len(scan_pids), flush=True)

sout = open(SCAN_OUT, "w", encoding="utf-8")
tally = collections.Counter()
for pid in scan_pids:
    v = linked[pid]
    stem = stem_of(hm[v["fh"]])
    ev = ocr.get(stem, {})
    rec = {"pid": pid, "stem": stem, "corpus_path": hm[v["fh"]],
           "status": v["st"], "n_questions": v["nq"], "band": "scan"}
    if ev.get("verdict") != "OK":
        rec["verdict"] = "SCAN-UNSCORED" if not ev else "OCR-" + str(ev.get("verdict"))
    elif (ev.get("text_chars") or 0) < 400 or (ev.get("n_containers") or 0) == 0:
        rec["verdict"] = "OCR-UNSTRUCTURED"
        rec["ocr_atomic"] = ev.get("printed_atomic", 0)
    else:
        atomic = ev.get("printed_atomic", 0)
        srows = stored.get(pid, [])
        garbage = sum(1 for x in srows if (not norm(x[2])) or FORMULA.match(x[2] or ""))
        seen = {}
        for x in srows:
            k = norm(x[2])[:120]
            if k and len(k) > 8:
                seen[k] = seen.get(k, 0) + 1
        dups = sum(vv - 1 for vv in seen.values() if vv > 1)
        clean_stored = len([x for x in srows if x[1]]) + \
            len([x for x in srows if not x[1] and norm(x[2]) and not FORMULA.match(x[2] or "")])
        missing = max(0, atomic - clean_stored)
        over = max(0, clean_stored - atomic - 2)
        rec.update({"ocr_atomic": atomic, "ocr_pages": ev.get("n_pages"),
                    "clean_stored": clean_stored, "garbage_rows": garbage,
                    "dup_texts": dups, "missing_atomic": missing, "over_atomic": over})
        flags = []
        if missing > 0:
            flags.append(f"MISSING-{missing}")
        if over > 0:
            flags.append(f"OVER-{over}")
        if garbage:
            flags.append(f"GARBAGE-{garbage}")
        if dups:
            flags.append(f"DUPLICATE-{dups}")
        rec["verdict"] = "COMPLETE-RULE-OK" if not flags else ";".join(flags)
    tally[rec["verdict"]] += 1
    sout.write(json.dumps(rec) + "\n")
sout.close()
print("phase B tally:", dict(tally.most_common(12)), flush=True)

# ---------------- phase C: absent ----------------
absent = [pid for pid, v in papers.items() if not v["fh"] or v["fh"] not in hm]
print("phase C absent papers:", len(absent), flush=True)

# ---------------- merge + gold list ----------------
gold, stats = [], collections.Counter()
with open(MERGE_OUT, "w", encoding="utf-8") as f:
    for l in open(TEXT_OUT):
        try:
            r = json.loads(l)
        except Exception:
            continue
        r["band"] = "text"
        if r["verdict"] == "COMPLETE-RULE-OK":
            gold.append(r["pid"])
        stats["text_" + r["verdict"].split(";")[0]] += 1
        f.write(json.dumps(r) + "\n")
    for l in open(SCAN_OUT):
        try:
            r = json.loads(l)
        except Exception:
            continue
        r["band"] = "scan"
        if r["verdict"] == "COMPLETE-RULE-OK":
            gold.append(r["pid"])
        stats["scan_" + r["verdict"].split(";")[0]] += 1
        f.write(json.dumps(r) + "\n")
    for pid in absent:
        f.write(json.dumps({"pid": pid, "band": "absent", "verdict": "PDF-ABSENT"}) + "\n")
        stats["absent"] += 1

# errors/retries leftover: any hash-linked text paper without a verdict
seen_pids = set()
for l in open(MERGE_OUT):
    try:
        seen_pids.add(json.loads(l)["pid"])
    except Exception:
        pass
uncovered = [pid for pid in linked if pid not in seen_pids]
json.dump({"gold_pids": gold, "stats": dict(stats), "uncovered": uncovered},
          open(GOLD_OUT, "w"), indent=1)
print("UNCOVERED (need rerun):", len(uncovered), flush=True)
print("GOLD papers:", len(gold), "of", len(papers), flush=True)
for k, v in sorted(stats.items()):
    print("  ", k, v, flush=True)
