#!/usr/bin/env python3
"""Health-check: verify Supabase secrets are present and which GEMMA keys are
alive + which gemma models exist. Used by the workflow 'health' job."""
import os, sys
import requests

GEMMA_BASE = 'https://generativelanguage.googleapis.com/v1beta'

def main():
    ok = True
    if not os.environ.get('SUPABASE_URL'):
        print('::error::SUPABASE_URL secret missing'); ok = False
    if not os.environ.get('SUPABASE_SERVICE_KEY'):
        print('::error::SUPABASE_SERVICE_KEY secret missing'); ok = False
    keys = [k.strip() for k in
            (os.environ.get('GEMMA_API_KEYS') or os.environ.get('GEMMA_API_KEY') or '').split(',')
            if k.strip()]
    if not keys:
        print('::warning::GEMMA_API_KEYS empty -> AI stages (enrich/Tier-2 repair) will skip')
    alive = 0
    for i, k in enumerate(keys, 1):
        tag = f'key #{i} ({k[:10]}...)'
        try:
            r = requests.get(f'{GEMMA_BASE}/models', params={'key': k}, timeout=30)
            if r.status_code == 200:
                models = [m['name'].split('/')[-1] for m in r.json().get('models', [])
                          if 'gemma' in m['name'].lower()]
                print(f'{tag}: OK ({len(models)} gemma models visible)')
                print(f'   gemma models: {", ".join(models[:12]) or "(none)"}')
                alive += 1
            else:
                msg = r.json().get('error', {}).get('message', '')[:100]
                print(f'{tag}: HTTP {r.status_code} {msg}')
        except requests.RequestException as e:
            print(f'{tag}: FAILED {str(e)[:100]}')
    model = os.environ.get('GEMMA_MODEL', 'gemma-4-31b-it')
    print(f'GEMMA_MODEL={model} | alive keys: {alive}/{len(keys)}')
    if keys and alive == 0:
        print('::warning::no GEMMA key responded -> pipeline continues without AI stages')
    sys.exit(0 if ok else 1)   # supabase secrets are mandatory; AI keys are not

if __name__ == '__main__':
    main()
