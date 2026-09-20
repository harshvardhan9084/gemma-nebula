#!/usr/bin/env python3
"""Merge per-shard scrape manifests into one (last-wins per paper_url).

Usage: merge_manifests.py OUT.csv IN1.csv [IN2.csv ...]
Missing input files are skipped with a warning (first run may have only
one shard branch). Output keeps extractor-compatible 11 columns.
"""
import csv
import sys

COLS = ['course', 'semester', 'academic_year', 'paper_code', 'paper_name',
        'paper_url', 'pdf_url', 'structured_path', 'unstructured_path',
        'status', 'downloaded_at']

def main():
    if len(sys.argv) < 3:
        print('usage: merge_manifests.py OUT.csv IN1.csv [IN2.csv ...]', file=sys.stderr)
        return 2
    out, inputs = sys.argv[1], sys.argv[2:]
    merged, order = {}, []
    for path in inputs:
        try:
            with open(path, newline='', encoding='utf-8-sig') as f:
                for r in csv.DictReader(f):
                    u = (r.get('paper_url') or '').strip().lower()
                    if not u:
                        continue
                    if u not in merged:
                        order.append(u)
                    merged[u] = r
        except FileNotFoundError:
            print(f'[warn] {path} missing, skipped', file=sys.stderr)
    with open(out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for u in order:
            w.writerow({k: merged[u].get(k, '') or '' for k in COLS})
    print(f'{len(order)} unique rows -> {out}', file=sys.stderr)
    return 0

if __name__ == '__main__':
    sys.exit(main())
