#!/usr/bin/env python3
"""aktuonline round-3 FULL-archive downloader (expansion corpus).

Reads data/aktuonline_full_index.csv (built from the 38 course/branch
listing pages of aktuonline.com), downloads every NEW paper's PDF
(paper_url .html -> .pdf is the direct link), 1.2-2.0s polite delay,
%PDF magic validation, resume-safe (disk + manifest are truth).

Designed for GitHub Actions chunked runs:
  --for-seconds 1500  -> run for ~25 min then exit 0 (workflow commits a
                         checkpoint to the shard branch between chunks)

Manifest (headered, extractor-compatible 11 cols):
  course, semester, academic_year, paper_code, paper_name, paper_url,
  pdf_url, structured_path, unstructured_path, status, downloaded_at

Statuses: OK (pdf saved) | NO_PDF (404/not a pdf) | ERROR (transient,
retried next run) | SKIP (already ingested in DB - in_db=Y, no download)
"""
import argparse, csv, os, sys, time, re
import urllib.request, urllib.error
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--index', default='data/aktuonline_full_index.csv')
    ap.add_argument('--out', default='corpus/aktuonline3')
    ap.add_argument('--manifest', default='data/aktuonline3_manifest.csv')
    ap.add_argument('--delay', type=float, default=2.0)
    ap.add_argument('--shard', type=int, default=0)
    ap.add_argument('--shards', type=int, default=1)
    ap.add_argument('--for-seconds', type=int, default=0,
                    help='stop cleanly after N seconds (chunked GHA runs)')
    ap.add_argument('--max-retries', type=int, default=3)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(os.path.dirname(args.manifest), exist_ok=True)
    t0 = time.time()

    # ---- load index ------------------------------------------------------
    with open(args.index, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    total = len(rows)
    rows = [r for r in rows if int(r.get('shard', '0') or 0) == args.shard] \
        if 'shard' in rows[0] else rows[args.shard::args.shards]
    log(f"index rows: {total} | this shard ({args.shard}/{args.shards}): {len(rows)}")

    # ---- resume state: manifest (append-only log) + disk ------------------
    done = {}
    if os.path.exists(args.manifest) and os.path.getsize(args.manifest) > 0:
        with open(args.manifest, newline='', encoding='utf-8') as f:
            first = f.readline()
            f.seek(0)
            cols = ['course', 'semester', 'academic_year', 'paper_code', 'paper_name',
                    'paper_url', 'pdf_url', 'structured_path', 'unstructured_path',
                    'status', 'downloaded_at']
            if first.lower().startswith('course'):
                reader = csv.DictReader(f)
            else:
                reader = [dict(zip(cols, (c.strip() for c in next(csv.reader([l])))))
                          for l in f if l.strip()]
            for r in reader:
                u = (r.get('paper_url') or '').strip().lower()
                if u and r.get('status') in ('OK', 'NO_PDF', 'SKIP'):
                    done[u] = r['status']
    log(f"resume: {len(done)} already handled (per manifest)")

    hdr_row = not os.path.exists(args.manifest) or os.path.getsize(args.manifest) == 0
    mf = open(args.manifest, 'a', newline='', encoding='utf-8')
    w = csv.writer(mf)
    if hdr_row:
        w.writerow(['course', 'semester', 'academic_year', 'paper_code', 'paper_name',
                    'paper_url', 'pdf_url', 'structured_path', 'unstructured_path',
                    'status', 'downloaded_at'])
        mf.flush()

    handled = set(done)          # union of manifest-done + handled this session
    ok = no_pdf = err = skipped = 0
    for i, r in enumerate(rows):
        url = r['paper_url'].strip().lower()
        slug = url.split('/papers/')[-1].replace('.html', '')
        dest = os.path.join(args.out, slug + '.pdf')

        # resume checks
        if url in done:
            skipped += 1
            continue
        if os.path.exists(dest) and os.path.getsize(dest) > 5000:
            handled.add(url)
            skipped += 1
            continue
        if r.get('in_db') == 'Y':
            w.writerow([r['course'], r['semester'], r['academic_year'], r['paper_code'],
                        r['paper_name'], url, r['pdf_url'], '', dest, 'SKIP',
                        datetime.now(timezone.utc).isoformat(timespec='seconds')])
            mf.flush()
            handled.add(url)
            skipped += 1
            continue

        # polite delay
        time.sleep(args.delay)

        # download
        status, note = 'ERROR', ''
        for attempt in range(args.max_retries):
            try:
                req = urllib.request.Request(r['pdf_url'], headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=40) as resp:
                    data = resp.read()
                if data[:5] != b'%PDF-':
                    status, note = 'NO_PDF', f'magic={data[:8]!r}'
                    break
                if len(data) < 5000:
                    status, note = 'NO_PDF', f'too small {len(data)}B'
                    break
                with open(dest, 'wb') as pf:
                    pf.write(data)
                status = 'OK'
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    status, note = 'NO_PDF', '404'
                    break
                note = f'HTTP {e.code}'
                time.sleep(3 + 5 * attempt)
            except Exception as e:
                note = str(e)[:80]
                time.sleep(3 + 5 * attempt)

        w.writerow([r['course'], r['semester'], r['academic_year'], r['paper_code'],
                    r['paper_name'], url, r['pdf_url'], '', dest, status,
                    datetime.now(timezone.utc).isoformat(timespec='seconds')])
        mf.flush()
        handled.add(url)
        if status == 'OK':
            ok += 1
        elif status == 'NO_PDF':
            no_pdf += 1
        else:
            err += 1

        if (ok + no_pdf + err) % 25 == 0:
            el = time.time() - t0
            rate = (ok + no_pdf + err) / el * 3600 if el > 0 else 0
            log(f"shard {args.shard}: +{ok} OK, {no_pdf} no-pdf, {err} err "
                f"(row {i+1}/{len(rows)}, {rate:.0f}/h, elapsed {el/60:.0f}m)")

        if args.for_seconds and (time.time() - t0) > args.for_seconds:
            log(f"time chunk over (--for-seconds {args.for_seconds}); exiting cleanly")
            break

    mf.close()
    log(f"DONE chunk: ok={ok} no_pdf={no_pdf} err={err} skipped={skipped}")
    # completion marker: every row handled?
    remaining = sum(1 for r in rows if r['paper_url'].strip().lower() not in handled)
    if remaining == 0:
        open(f'.scrape_done_{args.shard}', 'w').write('complete')
        log(f"ALL DONE for shard {args.shard} (marker written)")

if __name__ == '__main__':
    main()
