#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_models.py -- VERIFIED model discovery for the AI Studio keys.
Designed to run INSIDE GitHub Actions (US runners), because dev machines in
some regions are geo-blocked from generativelanguage.googleapis.com.

For every key in GEMMA_API_KEYS (comma-separated):
  1. ListModels (paginated)  -> the REAL catalog; nothing below is hard-coded
  2. Filter to text models supporting generateContent
     (skips embeddings/tts/imagen/veo/audio/live variants)
  3. LIVE JSON check: generateContent with responseMimeType=application/json +
     responseSchema -> proves structured output works on THIS key+model pair
  4. Light burst (default 4 rapid tiny calls on JSON-OK models) -> EMPIRICAL
     rate-limit signal (e.g. [200,200,200,429] = tight RPM; all 200 = comfy)

Output:
  - probe_result.json (uploaded as artifact by model-probe.yml)
  - markdown table appended to $GITHUB_STEP_SUMMARY
  - stdout table (visible in the Actions log)

Honesty note: Google's API does NOT expose numeric RPM/TPM/RPD quotas.
Anything claimed about quota here comes from OBSERVED burst behavior only.
"""
import json, os, re, sys, time
import requests

BASE = 'https://generativelanguage.googleapis.com/v1beta'
SKIP_RX = re.compile(r'(embedding|aqa|imagen|veo|tts|audio|image|video|live'
                     r'|native|computer-use|robotics)', re.I)
# minimal structured-output probe schema (cheapest possible JSON answer)
SCHEMA = {'type': 'object',
          'properties': {'ok': {'type': 'boolean'}},
          'required': ['ok']}
PROMPT = 'Return exactly this JSON object: {"ok": true}'


def hdr(key):
    return {'x-goog-api-key': key, 'Content-Type': 'application/json'}


def list_models(key):
    out, token = [], None
    while True:
        p = {'pageSize': 200}
        if token:
            p['pageToken'] = token
        r = requests.get(f'{BASE}/models', headers=hdr(key), params=p, timeout=60)
        r.raise_for_status()
        d = r.json()
        out += d.get('models', [])
        token = d.get('nextPageToken')
        if not token:
            return out


def json_call(key, model):
    """One tiny structured-output call. Returns dict(status, ok, s, note)."""
    t0 = time.time()
    body = {'contents': [{'parts': [{'text': PROMPT}]}],
            'generationConfig': {'temperature': 0, 'maxOutputTokens': 4000,
                                 'responseMimeType': 'application/json',
                                 'responseSchema': SCHEMA}}
    try:
        r = requests.post(f'{BASE}/models/{model}:generateContent',
                          headers=hdr(key), json=body, timeout=90)
    except requests.RequestException as e:
        return {'status': 0, 'ok': False, 's': round(time.time() - t0, 2),
                'note': f'net:{str(e)[:50]}'}
    dt = round(time.time() - t0, 2)
    if r.status_code == 200:
        try:
            cands = r.json().get('candidates', [])
            txt = cands[0]['content']['parts'][0]['text']
            ok = bool(json.loads(txt).get('ok'))
            return {'status': 200, 'ok': ok, 's': dt, 'note': 'json-ok'}
        except Exception as e:
            return {'status': 200, 'ok': False, 's': dt,
                    'note': f'parse:{str(e)[:40]}'}
    try:
        note = r.json().get('error', {}).get('status', '') or r.text[:60]
    except Exception:
        note = r.text[:60]
    return {'status': r.status_code, 'ok': False, 's': dt, 'note': str(note)[:60]}


def burst(key, model, n):
    """n rapid tiny calls -> list of status codes (rate-limit signal)."""
    out = []
    for _ in range(n):
        out.append(json_call(key, model)['status'])
        time.sleep(0.7)
    return out


def main():
    ap_burst, ap_max = 4, 14
    argv = sys.argv[1:]
    if '--burst' in argv:
        ap_burst = int(argv[argv.index('--burst') + 1])
    if '--max-models' in argv:
        ap_max = int(argv[argv.index('--max-models') + 1])
    keys = [k.strip() for k in re.split(r'[,;\s]+',
            os.environ.get('GEMMA_API_KEYS', '')) if k.strip()]
    if not keys:
        sys.exit('GEMMA_API_KEYS not set (comma-separated AI Studio keys)')

    result = {}
    for ki, key in enumerate(keys, 1):
        hint = key[:10] + '...'
        print(f'\n=== KEY {ki} ({hint}) ===', flush=True)
        try:
            catalog = list_models(key)
        except requests.RequestException as e:
            msg = str(e)[:200]
            print(f'  ListModels FAILED: {msg}')
            result[hint] = {'error': msg}
            continue
        cands = []
        for m in catalog:
            mid = m.get('name', '').removeprefix('models/')
            if 'generateContent' not in m.get('supportedGenerationMethods', []):
                continue
            if SKIP_RX.search(mid):
                continue
            cands.append((mid, m.get('inputTokenLimit'), m.get('outputTokenLimit')))
        print(f'  catalog: {len(catalog)} models total, '
              f'{len(cands)} generateContent text candidates', flush=True)
        rows = []
        for mid, tin, tout in cands[:ap_max]:
            c = json_call(key, mid)
            b = burst(key, mid, ap_burst) if c['ok'] else []
            rows.append({'model': mid, 'json_ok': c['ok'], 'latency_s': c['s'],
                         'note': c['note'], 'burst': b,
                         'in_limit': tin, 'out_limit': tout})
            flag = 'JSON-OK ' if c['ok'] else 'NO-JSON '
            print(f"  [{flag}] {mid:44} {c['s']:6.2f}s http={c['status']:<4} "
                  f"{c['note'][:38]:38} burst={b}", flush=True)
        result[hint] = {'catalog_size': len(catalog),
                        'candidates': len(cands), 'probed': rows}

    with open('probe_result.json', 'w') as f:
        json.dump(result, f, indent=1)

    md = ['## Verified model probe (live API responses only, nothing hard-coded)', '',
          '| key | model | JSON structured out | latency | burst (rate-limit signal) | in/out token limit |',
          '|---|---|---|---|---|---|']
    for hint, d in result.items():
        if 'error' in d:
            md.append(f'| {hint} | ListModels FAILED | {d["error"]} | | | |')
            continue
        md.append(f'| {hint} | catalog={d["catalog_size"]} candidates={d["candidates"]} | | | | |')
        for r in d.get('probed', []):
            md.append(f"| {hint} | {r['model']} | {'YES' if r['json_ok'] else 'no'} ({r['note']}) "
                      f"| {r['latency_s']}s | {r['burst']} | {r['in_limit']}/{r['out_limit']} |")
    out = os.environ.get('GITHUB_STEP_SUMMARY')
    if out:
        open(out, 'a').write('\n'.join(md) + '\n')
    print('\n'.join(md))


if __name__ == '__main__':
    main()
