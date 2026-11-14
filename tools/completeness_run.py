#!/usr/bin/env python3
"""COMPLETENESS CAMPAIGN - PHASE 1 RUNNER (owner rule 2026-11-14)
Verify EVERY measurable paper: printed atomic question count vs stored rows.
Order: RETRY-FROZEN -> AMNESTY-FROZEN -> RETRY-ACCEPT -> thin -> complete bulk.
PDFs: raw.githubusercontent CDN first (high limit), contents API fallback,
disk cache scripts/out/pdf_cache/ (survives sandbox recycle). Resume via JSONL.
Read-only on the DB (zero-churn). Scans are phase 2 (local OCR), not here.
"""
import re, sys, json, os, time, difflib, tempfile, subprocess
import urllib.parse, urllib.request
import concurrent.futures as cf
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN, HDR, _OPENER, api

import psycopg2

REPO_A = "harshvardhan9084/gemma-nebula"
BRANCH = "aktu-runner"
OUT = "/home/z/my-project/scripts/out/completeness_full.jsonl"
CACHE = "/home/z/my-project/scripts/out/pdf_cache"
PATHMAP = "/home/z/my-project/scripts/out/repoA_pdf_pathmap.json"

CONT = re.compile(r"^\s*(?:Q(?:ues[tion]*)?[\.\:\-]?\s*)?(\d{1,2})\s*[\.\)\:]\s*(.*?)(?:\s{2,}|\s*\((\d+)\s*[xX×]\s*(\d+)\s*=\s*(\d+)\)\s*$|\s+(\d+\s*[xX×]\s*\d+\s*=\s*\d+)\s*$|$)")
FORMULA = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*=\s*(\d+)\s*$")
NOISE = re.compile(r"^\s*[^A-Za-z0-9(\[]*[-–—_=~|.,;:'\"^]*\s*$")

def build_pathmap():
    if os.path.exists(PATHMAP):
        return json.load(open(PATHMAP))
    br = api(f"/repos/{REPO_A}/branches/{BRANCH}")
    tr = api(f"/repos/{REPO_A}/git/trees/{br['commit']['commit']['tree']['sha']}?recursive=1")
    m = {}
    for e in tr["tree"]:
        if e["type"] == "blob" and e["path"].lower().endswith(".pdf"):
            m[e["path"].rsplit("/", 1)[-1].lower().replace(".pdf", "")] = e["path"]
    json.dump(m, open(PATHMAP, "w"))
    return m

def fetch_pdf_cached(storage_url, stem, pathmap):
    os.makedirs(CACHE, exist_ok=True)
    cp = os.path.join(CACHE, re.sub(r"[^A-Za-z0-9_.-]", "_", stem.lower()) + ".pdf")
    if os.path.exists(cp) and os.path.getsize(cp) > 1000:
        return cp
    p = pathmap.get(stem.lower())
    data = None
    if p:
        raw = f"https://raw.githubusercontent.com/{REPO_A}/{BRANCH}/{urllib.parse.quote(p)}"
        try:
            req = urllib.request.Request(raw, headers={"Authorization": HDR["Authorization"]})
            d = _OPENER.open(req, timeout=60).read()
            if d[:4] == b"%PDF":
                data = d
        except Exception:
            data = None
        if data is None:
            try:
                import base64
                r = api(f"/repos/{REPO_A}/contents/{urllib.parse.quote(p)}?ref={BRANCH}")
                d2 = base64.b64decode(r["content"]) if r.get("content") else None
                if d2 and d2[:4] == b"%PDF":
                    data = d2
            except Exception:
                data = None
    if data is None and "drive.google" in (storage_url or ""):
        try:
            data = _OPENER.open(storage_url, timeout=60).read()
        except Exception:
            data = None
    if data:
        with open(cp, "wb") as f:
            f.write(data)
        return cp
    return None

def text_of(path):
    try:
        return subprocess.run(["pdftotext", "-layout", path, "-"],
                              capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    except Exception:
        return ""

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def parse_printed(txt):
    containers, cur_c, cur_p = [], None, None
    for raw in txt.splitlines():
        line = raw.rstrip()
        if not line.strip() or NOISE.match(line):
            continue
        cm = re.match(r"^\s*(\d{1,2})\s*[\.\)]\s+(.*?)(?:\s{2,}.*)?$", line)
        if cm and len(cm.group(1)) <= 2:
            rest = cm.group(2) or ""
            if FORMULA.search(rest) or re.search(r"attempt\s+(all|any|five|two|three)", rest, re.I):
                cur_c = {"n": int(cm.group(1)), "parts": []}
                containers.append(cur_c); cur_p = None
                continue
            cur_c = {"n": int(cm.group(1)), "parts": [], "standalone": rest.strip()[:80]}
            containers.append(cur_c); cur_p = None
            continue
        pm = re.match(r"^\s*\(?([a-l])\)\s*(.+)$|^\s*([a-l])\.\s+(.+)$", line)
        if pm and cur_c is not None:
            letter = (pm.group(1) or pm.group(3)).lower()
            cur_p = {"letter": letter, "text": (pm.group(2) or pm.group(4) or "").strip()}
            cur_c["parts"].append(cur_p)
            continue
        if cur_p is not None and cur_c is not None:
            s = line.strip()
            if len(s) > 15 and re.search(r"[a-z]{3,}", s, re.I):
                cur_p["text"] = (cur_p["text"] + " " + s).strip()[:400]
    return {"containers": containers,
            "n_parts": sum(len(c["parts"]) for c in containers),
            "n_standalone": len([c for c in containers if c.get("standalone") and not c["parts"]])}

def verify(pid, sc, yr, nq, klass, url, stored_rows, pathmap):
    stem = (url or "").rsplit("/", 1)[-1].replace(".html", "")
    r = {"pid": pid, "sc": sc, "yr": yr, "nq": nq, "class": klass, "stem": stem}
    pdf = fetch_pdf_cached(url, stem, pathmap)
    if not pdf:
        r["verdict"] = "PDF-ABSENT" if stem.lower() not in pathmap else "FETCH-FAIL"
        return r
    txt = text_of(pdf)
    if len(txt) < 400:
        r["verdict"] = "UNMEASURABLE(scan)"
        r["text_chars"] = len(txt)
        return r
    printed = parse_printed(txt)
    garbage = [x for x in stored_rows if (not norm(x[2])) or FORMULA.match(x[2] or "")]
    seen = {}
    for x in stored_rows:
        k = norm(x[2])[:120]
        if k and len(k) > 8:
            seen[k] = seen.get(k, 0) + 1
    dups = sum(v - 1 for v in seen.values() if v > 1)
    atomic = printed["n_parts"] + printed["n_standalone"]
    stored_sub = [x for x in stored_rows if x[1]]
    stored_top = [x for x in stored_rows if not x[1]]
    printed_texts = [norm(pt["text"]) for c in printed["containers"]
                     for pt in c["parts"] if len(norm(pt["text"])) > 10]
    corrupt = 0
    for x in stored_sub:
        t = norm(x[2])[:150]
        if len(t) > 12 and printed_texts:
            best = max(difflib.SequenceMatcher(None, t, pt).ratio()
                       for pt in printed_texts[:60])
            if best < 0.75:
                corrupt += 1
    clean_stored = len(stored_sub) + len([x for x in stored_top
                                          if norm(x[2]) and not FORMULA.match(x[2] or "")])
    missing = max(0, atomic - clean_stored)
    over = max(0, clean_stored - atomic - 2)
    r.update({"text_chars": len(txt), "atomic_printed": atomic,
              "stored_occ": len(stored_rows), "stored_sub": len(stored_sub),
              "stored_top": len(stored_top), "garbage_rows": len(garbage),
              "dup_texts": dups, "corrupt_texts": corrupt,
              "missing_atomic": missing, "over_atomic": over})
    flags = []
    if atomic == 0:
        flags.append("PARSE-FAIL(printed=0)")
    else:
        if missing > 0: flags.append(f"MISSING-{missing}")
        if over > 0: flags.append(f"OVER-{over}")
    if garbage: flags.append(f"GARBAGE-{len(garbage)}")
    if dups: flags.append(f"DUPLICATE-{dups}")
    if corrupt: flags.append(f"CORRUPT-{corrupt}")
    r["verdict"] = "COMPLETE-RULE-OK" if not flags else ";".join(flags)
    return r

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0, help="stop after N new verifications (0 = all)")
    args = ap.parse_args()
    done = set()
    if os.path.exists(OUT):
        with open(OUT) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["pid"])
                except Exception:
                    pass
    print(f"resume: {len(done)} already verified", flush=True)
    pathmap = build_pathmap()
    print(f"pathmap: {len(pathmap)} pdfs in corpus tree", flush=True)
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    order = [
        ("RETRY-FROZEN", "extraction_status='review' AND extraction_error LIKE 'RETRY-FROZEN%'"),
        ("AMNESTY-FROZEN", "extraction_status='review' AND extraction_error LIKE 'AMNESTY-FROZEN%'"),
        ("RETRY-ACCEPT", "extraction_status='complete' AND extraction_error LIKE 'RETRY-ACCEPT%'"),
        ("thin<20", "extraction_status='complete' AND n_questions<20 AND (extraction_error IS NULL OR extraction_error NOT LIKE 'RETRY-%' AND extraction_error NOT LIKE 'AMNESTY%')"),
        ("complete>=20", "extraction_status='complete' AND n_questions>=20"),
    ]
    queue = []
    for klass, cond in order:
        cur.execute(f"SELECT id, subject_code, year, n_questions, source_url FROM papers WHERE {cond}")
        for pid, sc, yr, nq, url in cur.fetchall():
            queue.append((pid, sc, yr, nq, klass, url))
    cur.execute("""SELECT o.paper_id, (q.parent_id IS NOT NULL), q.text
                    FROM occurrences o JOIN questions q ON q.id=o.question_id""")
    stored = {}
    for paper_id, is_sub, text in cur.fetchall():
        stored.setdefault(paper_id, []).append((None, is_sub, (text or "")[:300], None))
    conn.close()
    todo = [q for q in queue if q[0] not in done]
    print(f"queue: {len(queue)} papers, todo {len(todo)}", flush=True)
    t0 = time.time()
    n_done = n_ok = n_viol = 0
    with open(OUT, "a", encoding="utf-8") as fout, \
         cf.ThreadPoolExecutor(max_workers=8) as ex:
        chunk = todo[:args.max] if args.max else todo
        futs = {ex.submit(verify, pid, sc, yr, nq, klass, url, stored.get(pid, []), pathmap):
                (pid, sc, klass) for (pid, sc, yr, nq, klass, url) in chunk}
        for fut in cf.as_completed(futs):
            try:
                rec = fut.result()
            except Exception as e:
                pid, sc, klass = futs[fut]
                rec = {"pid": pid, "sc": sc, "class": klass, "verdict": f"ERROR-{type(e).__name__}"}
            fout.write(json.dumps(rec) + "\n")
            n_done += 1
            v = rec.get("verdict", "")
            if v == "COMPLETE-RULE-OK": n_ok += 1
            elif not v.startswith(("PDF-ABSENT", "FETCH-FAIL", "UNMEASURABLE", "ERROR")):
                n_viol += 1
            if n_done % 100 == 0:
                fout.flush()
                rate = n_done / max(1e-9, time.time() - t0)
                print(f"progress {n_done}/{len(todo)} ok={n_ok} viol={n_viol} "
                      f"({rate:.1f}/s, eta {int((len(todo)-n_done)/max(rate,0.01)/60)} min)",
                      flush=True)
    rate = n_done / max(1e-9, time.time() - t0)
    print(f"DONE chunk {n_done} ok={n_ok} viol={n_viol} in {int(time.time()-t0)}s "
          f"({rate:.1f}/s) -> {OUT}", flush=True)

if __name__ == "__main__":
    main()
