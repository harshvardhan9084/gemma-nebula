#!/usr/bin/env python3
"""Cycle76 (2026-11-20 23:30Z fire = 2026-11-21 05:00 IST) dispatch:
evidence-backed PAPERS-FIX on the 100%-gold census real-gaps band (text-band
papers with MISSING>=3 or OVER>=3 vs printed reference from the in-hand corpus
PDF). Owner authorization: 2026-11-20/21 100%-gold directive ("eliminate the
~2% bad data -> 100% gold") + documented execution order (c) in
OWNER-PROD-GOLD-20261121 worklog entry.
Exclusions (law): RETRY-FROZEN = anti-loop terminal (2 attempts used, human
review queue forever). Shell-safety asserted on every stem (quote-fix lesson
from run #105). Budget: ~445 est calls < 1900/run < 5700/day; cadence slot 1/3.
"""
import json, re, urllib.request
import psycopg2

PAT = open('/home/z/my-project/.secrets/gh_pat.txt').read().strip()
REPO = 'harshvardhan9084/gemma-nebula'
API = f'https://api.github.com/repos/{REPO}/actions/workflows'
DSN = "postgresql://postgres.qnakqtiokspzoopivfyl:repeatrankpyq@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"
OUT = '/home/z/my-project/scripts/out/fix_list_gold_realgaps_20261121.json'
CENSUS = '/home/z/my-project/scripts/out/gold_census_20261121.jsonl'

SAFE = re.compile(r'^[a-z0-9._-]+$')

def parse_flags(verdict):
    out = {}
    for m in re.finditer(r'(MISSING|OVER|GARBAGE|CORRUPT|DUPLICATE)-(\d+)', verdict or ''):
        out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
    return out

# ---- 1. real-gaps band from the census (text band only) ----
band = []
for line in open(CENSUS):
    r = json.loads(line)
    if r.get('band') != 'text':
        continue
    f = parse_flags(r.get('verdict'))
    if f.get('MISSING', 0) >= 3 or f.get('OVER', 0) >= 3:
        r['_flags'] = f
        band.append(r)
strict = [r for r in band if f.get('MISSING', 0) >= 3 or f.get('OVER', 0) >= 3]
print('real-gaps band (text, MISSING>=3 or OVER>=3):', len(band))
heavy = [r for r in band if max(r['_flags'].get('GARBAGE', 0), r['_flags'].get('CORRUPT', 0)) >= 3]
print('  of which garbage>=3/corrupt>=3 (heavier classes):', len(heavy))

# ---- 2. DB check: fresh status + ledger stamps ----
pids = [r['pid'] for r in band]
conn = psycopg2.connect(DSN, connect_timeout=30)
conn.autocommit = True
cu = conn.cursor()
cu.execute("select id::text, extraction_status, coalesce(extraction_error,'') from papers where id::text = any(%s)", (pids,))
db = {row[0]: (row[1], row[2]) for row in cu.fetchall()}
frozen, absent_db = [], []
kept = []
for r in band:
    st, err = db.get(r['pid'], (None, None))
    if st is None:
        absent_db.append(r['pid']); continue
    if 'RETRY-FROZEN' in err:
        frozen.append(r['pid']); continue
    r['db_status'] = st
    r['stamp'] = err[:60]
    r['stem'] = r['corpus_path'].rsplit('/', 1)[-1].rsplit('.', 1)[0].lower()
    kept.append(r)
print('db lookup:', len(db), '| kept:', len(kept), '| RETRY-FROZEN excluded:', len(frozen), '| row vanished:', len(absent_db))
amn = sum(1 for r in kept if 'AMNESTY-FROZEN' in r.get('stamp', ''))
print('  of kept, AMNESTY-FROZEN-stamped (evidence-backed exception to blind-rule gate):', amn)
unsafe = [r['pid'] for r in kept if not SAFE.match(r['stem'])]
assert not unsafe, f'unsafe stems: {unsafe[:5]}'
assert len({r["stem"] for r in kept}) == len(kept), 'duplicate stems in list'
print('shell-safety + uniqueness asserts passed')

json.dump([{'pid': r['pid'], 'stem': r['stem'], 'db_status': r['db_status'],
            'missing': r['_flags'].get('MISSING', 0), 'over': r['_flags'].get('OVER', 0),
            'garbage': r['_flags'].get('GARBAGE', 0), 'corrupt': r['_flags'].get('CORRUPT', 0)}
           for r in kept], open(OUT, 'w'), indent=1)
print('fix list saved:', OUT, len(kept), 'stems', flush=True)
cu.close(); conn.close()

# ---- 3. dispatch: chunk by 30k-char budget (long canonical stems; 2 runs =
# cadence slots 1-2/3 of quota day 2026-11-20, legal) ----
chunks, cur, size = [], [], 0
for r in kept:
    s = len(r['stem']) + 1
    if cur and size + s > 30000:
        chunks.append(cur); cur, size = [], 0
    cur.append(r['stem']); size += s
if cur:
    chunks.append(cur)
print('chunks:', [len(c) for c in chunks], 'chars:', [sum(len(x)+1 for x in c) for c in chunks], flush=True)
assert len(chunks) <= 3, f'cadence law: max 3 full runs/day (0 used today); got {len(chunks)} chunks'

for i, ch in enumerate(chunks, 1):
    fix_pids = ','.join(ch)
    req = urllib.request.Request(
        f'{API}/extraction.yml/dispatches',
        data=json.dumps({'ref': 'aktu-runner', 'inputs': {
            'stage': 'extract', 'extractor': 'v6',
            'fix_pids': fix_pids,
            'reprocess': 'true', 'status_filter': 'complete,review',
            'total_calls': '1900', 'route': 'auto',
            'qc_min_questions': '10', 'chunk_pages': '2',
        }}).encode(),
        headers={'Authorization': f'token {PAT}', 'Accept': 'application/vnd.github+json'},
        method='POST')
    with urllib.request.urlopen(req) as resp:
        print(f'chunk {i}: {len(ch)} stems -> extraction.yml dispatch HTTP', resp.status, flush=True)
    import time; time.sleep(8)

# ---- 4. resolve run ids (latest 2 workflow_dispatch runs on aktu-runner) ----
req2 = urllib.request.Request(
    f'https://api.github.com/repos/{REPO}/actions/runs?event=workflow_dispatch&per_page=4',
    headers={'Authorization': f'token {PAT}', 'Accept': 'application/vnd.github+json'})
with urllib.request.urlopen(req2) as resp2:
    runs = json.load(resp2)['workflow_runs'][:len(chunks)]
for r in runs:
    print('RUN: #%s id=%s status=%s created=%s' % (r['run_number'], r['id'], r['status'], r['created_at']), flush=True)
json.dump({'runs': [{'run_id': r['id'], 'run_number': r['run_number'],
                     'status': r['status'], 'created_at': r['created_at']} for r in runs],
           'stems_total': len(kept), 'stems_per_run': [len(c) for c in chunks],
           'frozen_excluded': len(frozen), 'amnesty_included': amn},
          open('/home/z/my-project/scripts/out/last_dispatch_gold_20261121.json', 'w'))
print('saved last_dispatch_gold_20261121.json', flush=True)
