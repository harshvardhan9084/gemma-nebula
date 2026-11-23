#!/usr/bin/env python3
"""SCORE the 559 newly-found scan-band papers against their OCR printed reference.
Evidence source: evidence/ocr_scan559/verdicts_shard{0,1}.jsonl (local CPU tesseract,
zero AI calls). Stored side: DB occurrences (read-only). Verdict semantics mirror
completeness_run.verify(): missing = max(0, atomic_printed - clean_stored).
FIX band = missing >= 3 (same rule as the 119 real-gap band).
Output: /home/z/my-project/scripts/out/scan559_scored.jsonl + fix_list_scan559.json"""
import json, os, re, sys, collections
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN
from completeness_run import norm, FORMULA
import psycopg2

REPO = "/home/z/my-project/repo_b"
EVD = os.path.join(REPO, "evidence/ocr_scan559")

# stem -> pid + corpus_path from the census
rows = [json.loads(l) for l in open(os.path.join(REPO, "tools/completeness_reverify2523_20261118.jsonl"))]
scan_map = {}
for r in rows:
    if r["verdict"] == "UNMEASURABLE(scan)":
        stem = os.path.splitext(os.path.basename(r["corpus_path"]))[0].lower()
        scan_map[stem] = r

# OCR verdicts from both shards
ocr = {}
for s in (0, 1):
    p = os.path.join(EVD, f"verdicts_shard{s}.jsonl")
    if not os.path.exists(p):
        continue
    for l in open(p):
        v = json.loads(l)
        ocr[v["stem"]] = v
print("OCR verdicts:", len(ocr), "| scans:", len(scan_map))

pids = [r["pid"] for r in scan_map.values()]
conn = psycopg2.connect(DSN, connect_timeout=30)
cur = conn.cursor()
cur.execute("""SELECT o.paper_id, (q.parent_id IS NOT NULL), q.text
               FROM occurrences o JOIN questions q ON q.id = o.question_id
               WHERE o.paper_id = ANY(%s::uuid[])""", (pids,))
stored = collections.defaultdict(list)
for pid, is_sub, text in cur.fetchall():
    stored[pid].append((None, is_sub, (text or "")[:300], None))
cur.execute("SELECT id, extraction_status, coalesce(n_questions,0) FROM papers WHERE id = ANY(%s::uuid[])", (pids,))
meta = {str(r[0]): r[1:] for r in cur.fetchall()}
conn.close()

out = open("/home/z/my-project/scripts/out/scan559_scored.jsonl", "w")
tally = collections.Counter()
fix_list = []
for stem, r in scan_map.items():
    v = ocr.get(stem, {})
    rec = {"pid": r["pid"], "stem": stem, "corpus_path": r["corpus_path"],
           "status": meta.get(r["pid"], ("?", 0))[0], "n_questions": meta.get(r["pid"], (0, "?"))[1]}
    if v.get("verdict") != "OK":
        rec["verdict"] = "OCR-" + str(v.get("verdict", "MISSING-EVIDENCE"))
    elif (v.get("text_chars") or 0) < 400 or (v.get("n_containers") or 0) == 0:
        # OCR text too sparse to trust as a printed reference: no fix claim either way
        rec.update({"ocr_atomic": v.get("printed_atomic", 0),
                    "ocr_text_chars": v.get("text_chars"), "ocr_pages": v.get("n_pages")})
        rec["verdict"] = "OCR-UNSTRUCTURED"
    else:
        atomic = v.get("printed_atomic", 0)
        srows = stored.get(r["pid"], [])
        garbage = sum(1 for x in srows if (not norm(x[2])) or FORMULA.match(x[2] or ""))
        clean_stored = len([x for x in srows if x[1]]) + \
            len([x for x in srows if not x[1] and norm(x[2]) and not FORMULA.match(x[2] or "")])
        missing = max(0, atomic - clean_stored)
        over = max(0, clean_stored - atomic - 2)
        rec.update({"ocr_atomic": atomic, "ocr_text_chars": v.get("text_chars"),
                    "ocr_pages": v.get("n_pages"), "ocr_sec": v.get("ocr_seconds"),
                    "clean_stored": clean_stored, "garbage_rows": garbage,
                    "missing_atomic": missing, "over_atomic": over})
        flags = []
        if missing >= 3:
            flags.append(f"MISSING-{missing}")
        if over > 0:
            flags.append(f"OVER-{over}")
        rec["verdict"] = "+".join(flags) if flags else "COMPLETE-RULE-OK"
        if missing >= 3:
            fix_list.append({"pid": r["pid"], "stem": stem, "corpus_path": r["corpus_path"],
                             "missing_atomic": missing, "ocr_atomic": atomic,
                             "clean_stored": clean_stored, "status": rec["status"]})
    tally[rec["verdict"]] += 1
    out.write(json.dumps(rec) + "\n")
out.close()
json.dump(fix_list, open("/home/z/my-project/scripts/out/fix_list_scan559.json", "w"))
print("tally:", dict(tally.most_common(10)))
print("FIX-NEEDED band (missing>=3):", len(fix_list),
      "| total atomic deficit:", sum(f["missing_atomic"] for f in fix_list))
st = collections.Counter(f["status"] for f in fix_list)
print("fix-band DB statuses:", dict(st))
