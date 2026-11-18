#!/usr/bin/env python3
"""Batch#2 scoreboard (fire cycle68, 2026-11-18): fresh verify() of the papers
touched by runs #105/#106/#107 (extracted_at > 2026-11-17 16:45Z), diffed
against the 11-14 phase-1 verdicts in completeness_full.jsonl.
READ-ONLY on DB, local compute only (no GHA, no vision calls, no writes).
"""
import json, sys, time
import concurrent.futures as cf
sys.path.insert(0, "/home/z/my-project/scripts")
import psycopg2
from completeness_run import DSN, build_pathmap, verify, OUT as FULL

CUT = "2026-11-17 16:45:00+00"
OUTF = "/home/z/my-project/scripts/out/batch2_scoreboard.jsonl"

def cls(v):
    if v == "COMPLETE-RULE-OK": return "OK"
    for k in ("MISSING", "OVER", "PARSE-FAIL", "GARBAGE", "DUPLICATE", "CORRUPT",
              "PDF-ABSENT", "FETCH-FAIL", "UNMEASURABLE", "ERROR"):
        if v.startswith(k) or f";{k}" in v or f"{k}(" in v:
            return k
    return v.split("-")[0] if v else "?"

def main():
    conn = psycopg2.connect(DSN, connect_timeout=30); conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""SELECT id, subject_code, year, coalesce(n_questions,0), source_url,
                          extraction_status, coalesce(left(extraction_error,40),'')
                   FROM papers WHERE extracted_at > %s ORDER BY id""", (CUT,))
    papers = cur.fetchall()
    pids = [p[0] for p in papers]
    print(f"touched papers: {len(pids)}", flush=True)
    stored = {}
    cur.execute("""SELECT o.paper_id, (q.parent_id IS NOT NULL), q.text
                   FROM occurrences o JOIN questions q ON q.id=o.question_id
                   WHERE o.paper_id = ANY(%s::uuid[])""", (pids,))
    for paper_id, is_sub, text in cur.fetchall():
        stored.setdefault(paper_id, []).append((None, is_sub, (text or "")[:300], None))
    conn.close()
    old = {}
    with open(FULL) as f:
        for line in f:
            try: rec = json.loads(line)
            except Exception: continue
            if rec.get("pid") in set(pids):
                old[rec["pid"]] = rec.get("verdict") or "-"
    print(f"old (11-14) verdicts found: {len(old)}", flush=True)
    pathmap = build_pathmap()
    print(f"pathmap: {len(pathmap)} pdfs in corpus tree", flush=True)
    t0 = time.time()
    with open(OUTF, "w", encoding="utf-8") as fout, \
         cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(verify, p[0], p[1], p[2], p[3], f"{p[5]}|{p[6]}",
                          p[4], stored.get(p[0], []), pathmap): p for p in papers}
        n = 0
        for fut in cf.as_completed(futs):
            try:
                rec = fut.result()
            except Exception as e:
                p = futs[fut]
                rec = {"pid": p[0], "sc": p[1], "yr": p[2], "nq": p[3],
                       "verdict": f"ERROR-{type(e).__name__}"}
            fout.write(json.dumps(rec) + "\n"); n += 1
            if n % 30 == 0:
                print(f"  {n}/{len(papers)} ({time.time()-t0:.0f}s)", flush=True)
    rows = [json.loads(l) for l in open(OUTF)]
    def agg(rs):
        c = {}
        for r in rs: c[cls(r.get("verdict", "?"))] = c.get(cls(r.get("verdict", "?")), 0) + 1
        miss = sum(int(r.get("missing_atomic", 0) or 0) for r in rs)
        return c, miss
    cn, mn = agg(rows)
    oldrecs = []
    with open(FULL) as f:
        for line in f:
            try: rec = json.loads(line)
            except Exception: continue
            if rec.get("pid") in set(pids): oldrecs.append(rec)
    co, mo = agg(oldrecs)
    print("\n== SCOREBOARD (old 11-14 -> new today, same 132 papers) ==")
    keys = sorted(set(cn) | set(co))
    print(f"  {'class':<14}{'old':>6}{'new':>6}")
    for k in keys:
        print(f"  {k:<14}{co.get(k,0):>6}{cn.get(k,0):>6}")
    print(f"  total MISSING questions: {mo} -> {mn}")
    print("\n== per-paper diffs (old -> new), first 40 ==")
    diff_n = 0
    for r in sorted(rows, key=lambda x: (cls(x.get("verdict","?")) != "OK", x.get("sc",""))):
        o = old.get(r["pid"], "-")
        if o != r.get("verdict", "?"):
            diff_n += 1
            if diff_n <= 40:
                print(f"  {r['sc']} {r['yr']} [{r.get('nq','?')}q]: {o}  ->  {r.get('verdict','?')}")
    print(f"changed verdicts: {diff_n}/{len(rows)} (full data: {OUTF})")

if __name__ == "__main__":
    main()
