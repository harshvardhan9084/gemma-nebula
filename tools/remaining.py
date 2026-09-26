#!/usr/bin/env python3
"""Count PDFs under --dir whose file_hash is NOT extraction_status='complete'.

Drives the unattended expansion chain: extraction.yml's post job runs this
after every round; remaining > 0 -> re-dispatch the next round (the
extractor's resume-by-hash makes rounds idempotent).

v2: the old version built one PostgREST in.(...) filter with hundreds of
64-char hashes; the gateway rejects ~26KB URLs with HTTP 400, which killed
the chain's post job on the first big round (run 35532765328). Now we do a
full paginated scan of papers(extraction_status=complete) and diff locally -
no URL limits, scales to any corpus size. Transient 5xx are retried.

Prints the single number on stdout (last line). Needs SUPABASE_URL +
SUPABASE_SERVICE_KEY in the environment.
"""
import argparse
import hashlib
import os
import sys
import time

import requests

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True)
    ap.add_argument('--page', type=int, default=1000)
    args = ap.parse_args()

    url = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_KEY', '')
    if not url or not key:
        print('SUPABASE_URL / SUPABASE_SERVICE_KEY missing', file=sys.stderr)
        return 2
    hdr = {'apikey': key, 'Authorization': f'Bearer {key}'}

    hashes = []
    for root, _, files in os.walk(args.dir):
        for f in files:
            if not f.lower().endswith('.pdf'):
                continue
            p = os.path.join(root, f)
            h = hashlib.sha256()
            with open(p, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b''):
                    h.update(chunk)
            hashes.append(h.hexdigest())

    # full scan of completed papers, offset pagination (stable order=id)
    # v3: this script is the CHAIN'S SPINE - a single Supabase blip window
    # (measured: 60s+ of straight 503 on run 36095328616's post job at
    # 2026-09-25 10:23Z) burned the old 4-attempt/50s ladder and silently
    # killed the whole unattended chain for 32h. A post job that runs once
    # per ~6h round can afford 7.7 min of patience: 6 attempts, backoff
    # 10/30/60/120/240s. Same policy as the extractor's db._send shield.
    done = set()
    off = 0
    while True:
        r = None
        for attempt in range(6):
            try:
                r = requests.get(
                    f'{url}/rest/v1/papers',
                    params={'select': 'file_hash,extraction_status',
                            'extraction_status': 'eq.complete',
                            'order': 'id', 'limit': args.page, 'offset': off},
                    headers=hdr, timeout=90)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == 5:
                    print(f'[!] Supabase unreachable after 6 attempts '
                          f'(~7.7 min): {e}', file=sys.stderr)
                    raise
                wait = (10, 30, 60, 120, 240)[attempt]
                print(f'[db] HTTP error on papers scan (offset {off}), '
                      f'retry {attempt + 1}/5 in {wait}s: {str(e)[:90]}',
                      file=sys.stderr)
                time.sleep(wait)
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            fh = row.get('file_hash')
            if fh:
                done.add(fh)
        off += args.page
        if len(rows) < args.page:
            break

    remaining = sum(1 for h in hashes if h not in done)
    print(f'[i] pdfs on disk: {len(hashes)} | complete in DB: {len(done & set(hashes))} | remaining: {remaining}',
          file=sys.stderr)
    print(remaining)
    return 0

if __name__ == '__main__':
    sys.exit(main())
