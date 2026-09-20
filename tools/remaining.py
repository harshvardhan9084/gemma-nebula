#!/usr/bin/env python3
"""Count PDFs under --dir whose file_hash is NOT extraction_status='complete'.

Drives the unattended expansion chain: extraction.yml's post job runs this
after every round; remaining > 0 -> re-dispatch the next round (the
extractor's resume-by-hash makes rounds idempotent).

Prints the single number on stdout (last line). Needs SUPABASE_URL +
SUPABASE_SERVICE_KEY in the environment.
"""
import argparse
import hashlib
import os
import sys

import requests

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True)
    ap.add_argument('--page', type=int, default=400)
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

    done = set()
    for i in range(0, len(hashes), args.page):
        chunk = hashes[i:i + args.page]
        r = requests.get(f'{url}/rest/v1/papers',
                         params={'select': 'file_hash,extraction_status',
                                 'file_hash': f'in.({",".join(chunk)})'},
                         headers=hdr, timeout=90)
        r.raise_for_status()
        for row in r.json():
            if row.get('extraction_status') == 'complete':
                done.add(row['file_hash'])

    remaining = len(hashes) - len(done)
    print(f'[i] pdfs on disk: {len(hashes)} | complete in DB: {len(done)} | remaining: {remaining}',
          file=sys.stderr)
    print(remaining)
    return 0

if __name__ == '__main__':
    sys.exit(main())
