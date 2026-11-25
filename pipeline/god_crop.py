#!/usr/bin/env python3
"""god_crop: crop-only diagram stage for god6 (cycle #89, 2026-11-26).

Takes a committed manifest of anchored diagram questions
  [{qid, code, stem, url, page}]  (url = public direct PDF, page = 1-based)
and for each (url, page) group:
  1. downloads the PDF, renders the page (pdfium, scale 2.0)
  2. ONE vision call per group -> per-question bbox (0-1000 normalized, y top)
  3. crops with generous padding, size-caps, uploads PNG to god-diagrams bucket
  4. PATCHes god6_questions.diagram_url = public crop URL
NEVER downgrades: on any failure the existing diagram_url is left untouched.

Laws honoured: workflow_dispatch only (no schedule, no auto-retry), exact send
accounting via AIRouterV7 (max_calls budget), evidence jsonl committed, exit 0
always (evidence is the product, not the exit code).

Usage:
  python3 pipeline/god_crop.py --input pipeline/god_lists/crop_manifest_20261126.json \
      [--limit 4] [--max-calls 300] [--min-interval 2.0] [--db]
"""
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import god_extract as g
import aktu_pyq_extractor_v6 as v6
from aktu_pyq_extractor import Supabase, load_key_pool, load_models


def log(*a):
    print(*a, flush=True)


BBOX_SCHEMA = {
    'type': 'object',
    'properties': {
        'bboxes': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'qid': {'type': 'string'},
                    'found': {'type': 'boolean'},
                    'bbox': {'type': 'array',
                             'items': {'type': 'integer'}},
                },
                'required': ['qid', 'found'],
            },
        },
    },
    'required': ['bboxes'],
}

BBOX_RULES = """You locate printed figures in an exam paper page image.
INPUT: one page image + a list of question tags with their text snippets.
TASK: for each tag, find the printed figure/diagram/graph/table/circuit that
belongs to that question ON THIS PAGE and report its bounding box.
COORDS: [x0, y0, x1, y1] in 0-1000 normalized coordinates of THIS page image,
y=0 at TOP, x=0 at LEFT. Give a GENEROUS box covering the whole figure and its
axis labels/captions, but NOT the running text of other questions.
If the figure is not visible on this page, set found=false.
Return JSON: {"bboxes":[{"qid":"<tag>","found":true,"bbox":[x0,y0,x1,y1]}]}"""


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--limit', type=int, default=0, help='max groups (0=all)')
    ap.add_argument('--max-calls', type=int, default=300)
    ap.add_argument('--min-interval', type=float, default=2.0)
    ap.add_argument('--call-budget', type=int, default=120)
    ap.add_argument('--db', action='store_true')
    args = ap.parse_args()

    rows = json.load(open(args.input))
    log(f'[crop] manifest rows: {len(rows)}')

    # group by (url, page)
    groups = {}
    for r in rows:
        groups.setdefault((r['url'], int(r['page'])), []).append(r)
    glist = list(groups.items())
    if args.limit:
        glist = glist[:args.limit]
    log(f'[crop] groups: {len(groups)} -> running {len(glist)}')

    keys = load_key_pool().keys
    models = load_models()
    router = AIRouterV7Crop(keys, models, max_calls=args.max_calls,
                            min_interval=args.min_interval,
                            call_budget=args.call_budget)  # noqa: defined below
    sb = None
    if args.db:
        if not (os.environ.get('SUPABASE_URL') and
                os.environ.get('SUPABASE_SERVICE_KEY')):
            sys.exit('--db needs SUPABASE_URL + SUPABASE_SERVICE_KEY')
        sb = Supabase(os.environ['SUPABASE_URL'],
                      os.environ['SUPABASE_SERVICE_KEY'])

    os.makedirs('evidence/god/crops', exist_ok=True)
    ev = open('evidence/god/crops/crop_evidence.jsonl', 'a', encoding='utf-8')
    scoreboard = {'groups': 0, 'calls': 0, 'cropped': 0, 'patched': 0,
                  'kept_old': 0, 'fail': 0}
    cache_dir = 'pdfcache_crop'
    os.makedirs(cache_dir, exist_ok=True)

    for gi, ((url, page), grp) in enumerate(glist, 1):
        scoreboard['groups'] += 1
        stem = grp[0].get('stem') or re.sub(r'[^a-z0-9]+', '-', url)[-60:]
        tagbase = {f'q{r["qid"][:8]}': r for r in grp}
        tagline = '\n'.join(f'- q{r["qid"][:8]}: '
                            + ' '.join((r.get('stem') or '')[:150].split())
                            for r in grp)
        cache_pdf = os.path.join(cache_dir, re.sub(r'[^A-Za-z0-9]', '', url)[-50:] + '.pdf')
        try:
            if not (os.path.exists(cache_pdf) and
                    os.path.getsize(cache_pdf) > 5000):
                import requests
                rr = requests.get(url.split('#')[0], timeout=90)
                if rr.status_code != 200 or rr.content[:5] != b'%PDF-':
                    raise RuntimeError(f'http-{rr.status_code}')
                open(cache_pdf, 'wb').write(rr.content)
            imgs = g.render_pages_pil(cache_pdf, [page], scale=2.0)
            img = imgs.get(page)
            if img is None:
                raise RuntimeError('render-fail')
            img_parts = v6.render_page_jpegs(cache_pdf, [page])
            if not img_parts:
                raise RuntimeError('render_page_jpegs-fail')
            parts = [{'text': f'{BBOX_RULES}\nPAGE {page}.\nQUESTIONS:\n{tagline}'}]
            parts.extend(img_parts)
            ai = router.call_crop(parts, BBOX_SCHEMA)
            scoreboard['calls'] = router.sends
            if not ai:
                raise RuntimeError('ai-fail')
            if not (ai.get('bboxes') or []):
                log(f'    [debug] raw ai head: '
                    f"{json.dumps(ai, ensure_ascii=False)[:400]}")
            found = 0
            W, H = img.size
            for b in ai.get('bboxes') or []:
                tag = str(b.get('qid') or '')
                r = tagbase.get(tag)
                if not r or not b.get('found'):
                    continue
                bb = b.get('bbox') or []
                if len(bb) != 4:
                    continue
                try:
                    x0, y0, x1, y1 = [max(0, min(1000, int(v))) for v in bb]
                except Exception:
                    continue
                x0, x1 = sorted((x0, x1))
                y0, y1 = sorted((y0, y1))
                box = (int(x0 / 1000 * W), int(y0 / 1000 * H),
                       min(W, max(int(x1 / 1000 * W), int(x0 / 1000 * W) + 40)),
                       min(H, max(int(y1 / 1000 * H), int(y0 / 1000 * H) + 40)))
                crop = img.crop(box)
                iobuf = io.BytesIO()
                crop.save(iobuf, 'PNG')
                if len(iobuf.getvalue()) > 4_800_000:
                    crop = crop.resize((max(1, crop.width // 2),
                                        max(1, crop.height // 2)))
                    iobuf = io.BytesIO()
                    crop.save(iobuf, 'PNG')
                qid8 = r['qid'][:8]
                local = f'evidence/god/crops/{stem[:90]}/q_{qid8}_p{page}.png'
                os.makedirs(os.path.dirname(local), exist_ok=True)
                open(local, 'wb').write(iobuf.getvalue())
                new_url = None
                if sb:
                    obj_stem = re.sub(r'[^A-Za-z0-9._-]+', '-', stem)[:70]
                    obj = f'crops-20261126/{obj_stem}/q_{qid8}_p{page}.png'
                    new_url = sb.upload_diagram('god-diagrams', obj, local)
                found += 1
                patched = False
                if sb and new_url:
                    sb.patch('god6_questions',
                             {'id': f'eq.{r["qid"]}'},
                             {'diagram_url': new_url})
                    patched = True
                    scoreboard['patched'] += 1
                ev.write(json.dumps({'qid': r['qid'], 'stem': stem[:90],
                                     'page': page, 'bbox_norm': bb,
                                     'upload': new_url,
                                     'patched': patched}) + '\n')
                scoreboard['cropped'] += 1
            miss = len(grp) - found
            if miss:
                scoreboard['kept_old'] += miss
            log(f'  [{gi}/{len(glist)}] {stem[:50]} p{page}: '
                f'{found}/{len(grp)} cropped')
        except Exception as e:
            scoreboard['fail'] += 1
            scoreboard['kept_old'] += len(grp)
            log(f'  [{gi}/{len(glist)}] {stem[:50]} p{page}: FAIL '
                f'{type(e).__name__}: {e}')
            ev.write(json.dumps({'stem': stem[:90], 'page': page,
                                 'error': f'{type(e).__name__}: {e}'}) + '\n')
        ev.flush()

    ev.close()
    scoreboard['calls'] = router.sends
    log(f"[crop] SCOREBOARD {json.dumps(scoreboard)}")
    with open('evidence/god/crops/scoreboard.json', 'w') as f:
        json.dump(scoreboard, f, indent=1)
    log('[crop] done (exit 0 always - evidence is the product)')


class AIRouterV7Crop(v6.AIRouterV6):
    """v6 router (image parts + budget) with exact send accounting + pace."""

    def __init__(self, keys, models, max_calls=0, min_interval=2.0,
                 call_budget=120):
        super().__init__(keys, models, max_calls=max_calls)
        self.sends = 0
        self.min_interval = min_interval
        self.call_budget = call_budget
        self._last = 0.0

    def call_crop(self, parts, schema):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        if self.max_calls and self.sends >= self.max_calls:
            return None
        self.sends += 1
        try:
            return self.call(parts, schema, kind='enrich',
                             min_interval=self.min_interval,
                             budget_s=self.call_budget)
        except Exception:
            return None


if __name__ == '__main__':
    main()
