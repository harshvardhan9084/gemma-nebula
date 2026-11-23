#!/usr/bin/env python3
"""Single poll iteration for the cycle76 gold sweep. State via files.
Guards: re-dispatch cancelled chunk 2 ONLY when no run is active/queued."""
import json, os, sys, time, urllib.request
import psycopg2

PAT = open('/home/z/my-project/.secrets/gh_pat.txt').read().strip()
REPO = 'harshvardhan9084/gemma-nebula'
DSN = "postgresql://postgres.qnakqtiokspzoopivfyl:repeatrankpyq@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"
OUTD = '/home/z/my-project/scripts/out'
STATE = f'{OUTD}/watch_state.json'
CHUNK2 = f'{OUTD}/fix_list_gold_realgaps_20261121_chunk2.json'
MARK = f'{OUTD}/.chunk2_redispatched'

def runs():
    req = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/actions/runs?per_page=6',
        headers={'Authorization': f'token {PAT}', 'Accept': 'application/vnd.github+json'})
    return json.load(urllib.request.urlopen(req))['workflow_runs']

def dbcounts():
    c = psycopg2.connect(DSN, connect_timeout=25)
    c.set_session(readonly=True, autocommit=True)
    cu = c.cursor()
    cu.execute("select count(*) from questions")
    q = cu.fetchone()[0]
    cu.execute("select extraction_status, count(*) from papers group by 1")
    st = dict(cu.fetchall())
    c.close()
    return q, st

def redispatch():
    stems = json.load(open(CHUNK2))
    fix = ','.join(s['stem'] for s in stems)
    assert len(fix) < 32000
    req = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/actions/workflows/extraction.yml/dispatches',
        data=json.dumps({'ref': 'aktu-runner', 'inputs': {
            'stage': 'extract', 'extractor': 'v6', 'fix_pids': fix,
            'reprocess': 'true', 'status_filter': 'complete,review',
            'total_calls': '1900', 'route': 'auto',
            'qc_min_questions': '10', 'chunk_pages': '2'}}).encode(),
        headers={'Authorization': f'token {PAT}', 'Accept': 'application/vnd.github+json'},
        method='POST')
    with urllib.request.urlopen(req) as r:
        print(f'CHUNK2 RE-DISPATCH ({len(stems)} stems): HTTP', r.status)

rl = runs()
q, st = dbcounts()
state = json.load(open(STATE)) if os.path.exists(STATE) else {'q0': 246571, 'seen': {}}  # 246571 = pre-sweep audit baseline (cron obs)
if state['q0'] is None:
    state['q0'] = q
compact = {r['id']: [r['run_number'], r['status'], r['conclusion']] for r in rl}
for rid, cur in compact.items():
    prev = state['seen'].get(str(rid))
    if prev != cur:
        print(f"RUN #{cur[0]} id={rid}: {cur[1]}/{cur[2]}" + (f"  (was {prev[1]}/{prev[2]})" if prev else ""))
        state['seen'][str(rid)] = cur
active = [r for r in rl if r['status'] in ('in_progress', 'queued', 'pending', 'waiting')]
print(f"DB: questions={q} ({q - state['q0']:+d}) status={st}")
print(f"ACTIVE/QUEUED: {[r['run_number'] for r in active] or 'none'}")
if not active and not os.path.exists(MARK):
    print("queue empty -> re-dispatching cancelled chunk 2 (578 stems)")
    redispatch()
    open(MARK, 'w').write('done')
    time.sleep(10)
    rl = runs()
    r0 = rl[0]
    print(f"CHUNK2 RUN: #{r0['run_number']} id={r0['id']} {r0['status']}")
    state['seen'][str(r0['id'])] = [r0['run_number'], r0['status'], r0['conclusion']]
    state['chunk2_run'] = r0['id']
done = all(v[1] == 'completed' for v in state['seen'].values())
json.dump(state, open(STATE, 'w'))
print('ALL-COMPLETED' if done and os.path.exists(MARK) else 'IN-FLIGHT')
