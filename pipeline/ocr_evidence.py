#!/usr/bin/env python3
"""OCR EVIDENCE BUILDER (phase-2a, owner go 2026-11-18: "work on already got PDFs").

For papers the 11-14 completeness verifier could not measure (PARSE-FAIL /
UNMEASURABLE-scan pools), build the printed-paper reference that tool lacked:
rasterize each PDF (pypdfium2) and OCR it (tesseract), then parse the printed
question structure with the SAME parser the verifier uses (parse_printed copy).
Output per paper: OCR text file + a verdict JSONL line (printed_atomic count,
text_chars, truncation flag, timings). CPU-only: zero AI calls, zero DB writes
(read-only phase), zero RPD consumption - it builds EVIDENCE, which the
anti-loop law requires before any fix dispatch.

Runs inside GHA (ocr_evidence.yml) or locally. Never touches repo A or PROD.
"""
import argparse, json, os, re, sys, time
import collections

# ---- printed-structure parser: faithful copy from completeness_run.py ----
FORMULA = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*=\s*(\d+)\s*$")
NOISE = re.compile(r"^\s*[^A-Za-z0-9(\[]*[-–—_=~|.,;:'\"^]*\s*$")


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


# ---- OCR: rasterize + tesseract (same technique as v5.ocr_lines, dpi param) ----
def ocr_pdf(pdf_path, dpi=250, max_pages=40):
    import pypdfium2 as pdfium
    from pytesseract import Output
    import pytesseract
    doc = pdfium.PdfDocument(pdf_path)
    lines, n_pages, truncated = [], 0, False
    t0 = time.time()
    try:
        n = len(doc)
        use = min(n, max_pages)
        truncated = n > max_pages
        n_pages = use
        for pno in range(use):
            img = doc[pno].render(scale=dpi / 72).to_pil()
            data = pytesseract.image_to_data(img, output_type=Output.DICT)
            rows = collections.defaultdict(list)
            for i, txt in enumerate(data['text']):
                if not txt.strip() or int(data.get('conf', ['0'])[i] or 0) < 35:
                    continue
                key = round(data['top'][i] / (14 * dpi / 72))
                rows[key].append((data['left'][i], txt))
            for key in sorted(rows):
                ws = sorted(rows[key])
                lines.append((pno + 1, ' '.join(t for _, t in ws)))
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return lines, {"n_pages": n_pages, "truncated": truncated, "ocr_seconds": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stems-file', required=True, help='file with one corpus stem per line')
    ap.add_argument('--dirs', default='corpus', help='comma-separated corpus dirs to index')
    ap.add_argument('--shard', type=int, default=0)
    ap.add_argument('--shards', type=int, default=1)
    ap.add_argument('--dpi', type=int, default=250)
    ap.add_argument('--max-pages', type=int, default=40)
    ap.add_argument('--out-dir', default='evidence/ocr')
    args = ap.parse_args()

    stems = [l.strip().lower() for l in open(args.stems_file)
             if l.strip() and not l.strip().startswith('#')]
    mine = [s for i, s in enumerate(stems) if i % args.shards == args.shard]
    print(f'[i] shard {args.shard}/{args.shards}: {len(mine)}/{len(stems)} stems in scope')

    # index corpus basenames -> path
    index = {}
    for d in args.dirs.split(','):
        d = d.strip()
        if not d or not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith('.pdf'):
                    index.setdefault(os.path.splitext(f)[0].lower(), os.path.join(root, f))
    print(f'[i] corpus index: {len(index)} PDFs')

    os.makedirs(args.out_dir, exist_ok=True)
    vpath = os.path.join(args.out_dir, f'verdicts_shard{args.shard}.jsonl')
    done = set()
    if os.path.exists(vpath):                       # resume-safe per shard
        for l in open(vpath):
            try:
                done.add(json.loads(l)['stem'])
            except Exception:
                pass
    print(f'[i] resume: {len(done)} already done in shard')

    ok = miss = fail = 0
    with open(vpath, 'a', encoding='utf-8') as vf:
        for n, stem in enumerate(mine, 1):
            if stem in done:
                continue
            v = {'stem': stem, 'shard': args.shard}
            p = index.get(stem)
            if not p:
                v['verdict'] = 'FILE-NOT-FOUND'
                miss += 1
            else:
                try:
                    v['path'] = p
                    lines, meta = ocr_pdf(p, dpi=args.dpi, max_pages=args.max_pages)
                    txt = '\n'.join(t for _pg, t in lines)
                    pr = parse_printed(txt)
                    v.update({'verdict': 'OK', 'text_chars': len(txt),
                              'printed_atomic': pr['n_parts'] + pr['n_standalone'],
                              'n_containers': len(pr['containers']),
                              **meta})
                    with open(os.path.join(args.out_dir, stem[:120] + '.txt'), 'w',
                              encoding='utf-8') as tf:
                        tf.write(txt)
                    ok += 1
                except Exception as e:
                    v['verdict'] = 'OCR-ERROR'
                    v['error'] = f'{type(e).__name__}: {str(e)[:200]}'
                    fail += 1
            vf.write(json.dumps(v, ensure_ascii=False) + '\n')
            vf.flush()
            if n % 10 == 0:
                print(f'  [{n}/{len(mine)}] ok={ok} miss={miss} fail={fail}', flush=True)
    print(f'[i] shard done: ok={ok} miss={miss} fail={fail}')
    # ALWAYS exit 0: FILE-NOT-FOUND / OCR-ERROR are legitimate evidence rows
    # (recorded in the verdicts JSONL), not job failures - a nonzero exit here
    # skips the upload step (default success() gate) and drops the shard's
    # evidence from the gather merge (run #1 lesson: 250 verdicts lost).
    sys.exit(0)


if __name__ == '__main__':
    main()
