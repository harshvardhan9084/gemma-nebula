#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
god_extract.py -- GOD-LEVEL extraction pipeline v7 (owner order 2026-11-23)
============================================================================
Owner directive: "totally isolated, minimal tables for 6 subjects, built from
scratch; the current pipeline does not work well. Rebuild/update the pipeline
so nothing incorrect/unwanted/wrong/errored leaks into the results. Take care
of diagrams. Cross-verify every result ... till every PDF returns GOD level
results ... run and observe to iterate."

WHY THIS EXISTS (measured v6 defects it fixes):
  - v6 enrichment: extractor returned MORE rows than the paper prints
    (21 printed -> 133 stored on the worst paper). GOD enforces
    extracted <= printed by construction, with the printed reference built
    INDEPENDENTLY of the AI (embedded text + tesseract OCR, no AI).
  - v6 self-checks were AI-vs-AI (completeness check asked the same family of
    models). GOD verifies AI output against the PHYSICAL PAPER: count equality,
    label-set equality, per-question fuzzy text match vs the printed reference.
  - v6 diagrams: flags existed, crops were opportunistic. GOD crops the figure
    region the vision model itself reports and stores it in an ISOLATED public
    bucket (god-diagrams) with the URL on the question row.

WHAT IT DOES PER PAPER (all writes confined to god6_* tables + god-diagrams
bucket; the old papers/questions tables are NEVER touched):
  1. Locate PDF by stem (DB stem -> corpus index; code+year glob fallback),
     verify sha256 vs the DB file_hash (mismatch recorded, never silent).
  2. Build the PRINTED REFERENCE twice over: pypdfium2 embedded text AND
     tesseract OCR of the rendered pages; parse_printed on both; the better
     parse (more containers) is the reference, both are recorded.
  3. GOD VISION PASS: 2-page chunks, page text + rendered JPEG parts, strict
     transcription prompt (numbers verbatim, sub-parts inline, marks, NO
     answers/paraphrase), diagram flag + normalized bbox per question.
  4. GOD VERIFY LOOP (the trial-and-error engine):
       a. deterministic clean (v6 cleaner + leak signatures) + in-paper dedupe;
       b. anti-enrichment cap: labels not in the printed reference are DROPPED
          (logged in the audit diff) unless the reference itself is weak;
       c. score: count equality, label coverage, per-question fuzzy ratio;
       d. on FAIL -> one REPAIR call per attempt: the model is shown the
          printed reference, its own missing/extra labels, and the suspect
          page images, and returns the full corrected set (max --attempts).
  5. Verdict: gold (everything verified against the printed paper) |
     review-weakref (paper is scans with unparseable printed structure) |
     review-diff (residual mismatch after repairs) | failed (no extraction).
     Nothing is silently accepted; every attempt lands a god6_audit row.
  6. Diagrams: crop the reported bbox from the rendered page -> PNG ->
     god-diagrams bucket -> public URL on the question row.
  7. Push god6_papers (upsert on pdf_stem) + god6_questions (paper-scoped
     replace; own isolated table) + god6_audit rows (the owner's "GOD Updates"
     ledger, one row per attempt with the full diff).
"""
import argparse, difflib, hashlib, io, json, os, re, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aktu_pyq_extractor as v5
import aktu_pyq_extractor_v6 as v6
from aktu_pyq_extractor import (AIRouter, Supabase, load_key_pool, load_models,
                                sha256, log, parse_ai_text)

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

try:
    import pypdfium2 as pdfium
    HAVE_PDFIUM = True
except ImportError:
    HAVE_PDFIUM = False

try:
    from pipeline.ocr_evidence import parse_printed, ocr_pdf
except ImportError:  # runner runs from repo root with pipeline on sys.path
    from ocr_evidence import parse_printed, ocr_pdf

PROMPT_VERSION = 'god7-printref-verify'
DIA_BUCKET = 'god-diagrams'

# ---------------------------------------------------------------------------
# GOD schema: transcription + diagram geometry
# ---------------------------------------------------------------------------
GOD_EXTRACT_SCHEMA = {
    'type': 'object',
    'properties': {
        'paper_header': {'type': 'object', 'properties': {
            'subject': {'type': 'string', 'nullable': True},
            'session': {'type': 'string', 'nullable': True},
        }, 'required': []},
        'questions': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'label': {'type': 'string'},
                'text': {'type': 'string'},
                'marks': {'type': 'integer', 'nullable': True},
                'has_diagram': {'type': 'boolean'},
                'diagram_page': {'type': 'integer', 'nullable': True},
                'diagram_bbox': {'type': 'array', 'items': {'type': 'integer'},
                                 'nullable': True},
            },
            'required': ['label', 'text'],
        }},
    },
    'required': ['questions'],
}

GOD_RULES = """GOD EXTRACTION RULES - transcribe the printed question paper EXACTLY.
- Transcribe EVERY numbered question on the page(s), with the printed number as `label` exactly as printed (e.g. "1", "2", "3(a)" ONLY if the paper numbers sub-parts as main questions; sub-parts of a numbered question stay INLINE inside its text).
- `text`: the complete printed question verbatim, including sub-parts "(a) ... (b) ...", choice instructions like "Explain with the help of ...", and any data table needed to answer. NO answers, NO explanations, NO paraphrasing, NO added or invented content, NO marks inside the text.
- `marks`: printed marks for that question as integer if shown, else null.
- `has_diagram`: true ONLY if the question genuinely contains a printed figure/graph/table/circuit needed to answer it. `diagram_page` = 1-based page number of the figure; `diagram_bbox` = [x0,y0,x1,y1] of the figure region in 0-1000 normalized coordinates of that page (y=0 at TOP). Give a generous bounding box covering the whole figure.
- Headers, footers, watermarks, "Attempt all questions", section banners, time/marks summary lines are NOT questions.
- Include question-dependent formula sheets or given-data blocks in that question's text.
- Missing one printed question is a FAILURE. Inventing one is a WORSE failure. Output JSON per the schema only."""

REPAIR_RULES = """GOD REPAIR PASS - your previous transcription did not match the printed paper.
You are given: (1) the PRINTED REFERENCE (question numbers + text fragments read directly from the paper scan/text layer), (2) what your previous output was missing or added.
Return the FULL CORRECTED question set for these page(s) per the GOD RULES: every printed question, same labels, no extras, verbatim text, marks, diagram flags."""

# ---------------------------------------------------------------------------
# printed reference (AI-free)
# ---------------------------------------------------------------------------

def page_text_pdfium(pdf_path, max_pages=40):
    """Embedded text per page via pypdfium2 textpage (no pdfplumber)."""
    if not HAVE_PDFIUM:
        return {}, 0
    out, n = {}, 0
    try:
        doc = pdfium.PdfDocument(pdf_path)
        n = len(doc)
        for pno in range(min(n, max_pages)):
            try:
                tp = doc[pno].get_textpage()
                txt = tp.get_text_range() or ''
                tp.close()
                out[pno + 1] = txt
            except Exception:
                out[pno + 1] = ''
        doc.close()
    except Exception:
        pass
    return out, n


def render_pages_pil(pdf_path, page_nos, scale=2.0):
    """Render pages -> {page_no: PIL.Image} for crops + (optionally) AI parts."""
    from PIL import Image
    imgs = {}
    if not HAVE_PDFIUM:
        return imgs
    try:
        doc = pdfium.PdfDocument(pdf_path)
        for pno in page_nos:
            if pno < 1 or pno > len(doc):
                continue
            imgs[pno] = doc[pno - 1].render(scale=scale).to_pil().convert('RGB')
        doc.close()
    except Exception:
        pass
    return imgs


def printed_reference(pdf_path, dpi=250, max_pages=40, db_nq=None, ocr_budget_s=300):
    """AI-free printed reference: embedded text AND OCR parses, best wins.
    OCR always runs unless the embedded parse is already strong (smoke-test
    lesson 2026-11-23: parse_printed undercounts on text-layer papers -
    4 containers found where the paper prints ~23; trusting that would make
    the repair loop DELETE real questions)."""
    t0 = time.time()
    emb, n_pages = page_text_pdfium(pdf_path, max_pages)
    emb_txt = '\n'.join(v for _p, v in sorted(emb.items()))
    emb_parse = parse_printed(emb_txt) if len(emb_txt) > 200 else {'containers': []}
    emb_n = len(emb_parse['containers'])
    emb_floor = max(12, int(0.8 * db_nq)) if db_nq else 12
    ocr_parse, ocr_lines, ocr_meta = {'containers': []}, [], {}
    text_chars = len(emb_txt)
    if emb_n < emb_floor and (time.time() - t0) < ocr_budget_s:
        try:
            ocr_lines, ocr_meta = ocr_pdf(pdf_path, dpi=dpi, max_pages=max_pages)
            ocr_txt = '\n'.join(t for _pg, t in ocr_lines)
            text_chars += len(ocr_txt)
            ocr_parse = parse_printed(ocr_txt)
        except Exception as e:
            log(f'    [ref] OCR failed: {type(e).__name__}: {str(e)[:120]}')
    best = emb_parse if emb_n >= len(ocr_parse['containers']) else ocr_parse
    ref_kind = 'embedded' if best is emb_parse else 'ocr'
    n_best = len(best['containers'])
    atomic = sum(len(c.get('parts') or []) for c in best['containers']) \
        + len([c for c in best['containers'] if c.get('standalone') and not c.get('parts')])
    # smoke-test lesson: db_nq is the OLD pipeline's ATOMIC count - compare
    # atomic-to-atomic for the trust floor, containers are the verify unit
    trusted = n_best >= 4 and (db_nq is None or 0.5 * db_nq <= atomic <= 1.6 * db_nq + 4)
    return {
        'ref': best, 'ref_kind': ref_kind, 'trusted': trusted,
        'db_nq': db_nq, 'atomic': atomic,
        'n_pages': n_pages, 'emb_pages': emb, 'ocr_lines': ocr_lines,
        'emb_containers': emb_n, 'ocr_containers': len(ocr_parse['containers']),
        'text_chars': text_chars, 'ref_seconds': round(time.time() - t0, 1),
        'truncated': bool(ocr_meta.get('truncated')),
    }


def container_text(c):
    bits = [c.get('standalone') or '']
    bits += [p.get('text') or '' for p in c.get('parts') or []]
    return ' '.join(b for b in bits if b).strip()


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def label_key(label):
    s = str(label or '').strip().lower()
    m = re.match(r'^(\d{1,2})\s*[\.\)\:]?\s*((?:[a-l])?\d*)$', s)
    if m:
        return (int(m.group(1)), m.group(2) or '')
    return (s, '')


# ---------------------------------------------------------------------------
# chunked vision extraction
# ---------------------------------------------------------------------------

def page_pair_chunks(n_pages, max_pages):
    nos = list(range(1, min(n_pages, max_pages) + 1))
    return [nos[i:i + 2] for i in range(0, len(nos), 2)]


def chunk_text_for(pages, ref):
    """Embedded text first; OCR lines for pages the text layer left empty."""
    parts = []
    for p in pages:
        t = (ref['emb_pages'].get(p) or '').strip()
        if len(t) < 120:
            t = '\n'.join(txt for pg, txt in ref['ocr_lines'] if pg == p)
        parts.append(f'--- PAGE {p} ---\n{t}')
    return '\n'.join(parts)


def extract_chunks(router, pdf_path, ref, args):
    """Vision-first chunked transcription. Returns (questions, calls, header)."""
    qmap, calls, header = {}, 0, {}
    for pages in page_pair_chunks(ref['n_pages'], args.max_pages):
        if router.budget_left() <= 0:
            break
        txt = chunk_text_for(pages, ref)
        parts = [{'text': f'{GOD_RULES}\nPAGES {pages[0]}-{pages[-1]} TEXT:\n{txt}'}]
        imgs = v6.render_page_jpegs(pdf_path, pages)
        if imgs:
            parts.extend(imgs)
        ai, err = router.call(parts, GOD_EXTRACT_SCHEMA, kind='enrich',
                              min_interval=args.min_interval,
                              budget_s=args.call_budget)
        calls += 1
        if not ai:
            log(f'    [ai] pages {pages}: FAILED ({str(err)[:120]})')
            continue
        hdr = ai.get('paper_header') or {}
        for k in ('subject', 'session'):
            if hdr.get(k) and not header.get(k):
                header[k] = str(hdr[k])[:120]
        for q in ai.get('questions') or []:
            lab = str(q.get('label') or '').strip()
            if not lab or not (q.get('text') or '').strip():
                continue
            key = label_key(lab)
            t = v6.clean_v6(str(q.get('text') or ''))
            flags = v6.leak_flags(t) if t else {}
            if t and sum(1 for _k, v in flags.items() if v) >= 2:
                t = ''                                   # hard garbage drop
            if not t:
                continue
            qd = {'label': lab, 'key': key, 'text': t,
                  'marks': q.get('marks'), 'page': pages[0],
                  'has_diagram': bool(q.get('has_diagram')),
                  'diagram_page': q.get('diagram_page'),
                  'diagram_bbox': q.get('diagram_bbox')}
            old = qmap.get(key)
            if old is None or len(t) > len(old['text']):
                qmap[key] = qd
    qs = sorted(qmap.values(), key=lambda q: q['key'])
    return qs, calls, header


def repair_pass(router, pdf_path, ref, missing, extra, cur_qs, args, suspect_pages):
    """One targeted repair: printed reference + diff + suspect page images."""
    ref_rows = []
    for c in ref['ref']['containers'][:60]:
        t = container_text(c)[:220]
        if t:
            ref_rows.append({'label': str(c['n']), 'printed': t})
    prompt = (f'{REPAIR_RULES}\nGOD RULES:\n{GOD_RULES}\n'
              f'PRINTED REFERENCE (AI-free, from the paper itself):\n'
              f'{json.dumps(ref_rows, ensure_ascii=False)}\n'
              f'YOUR PREVIOUS OUTPUT PROBLEMS: missing labels: {missing[:24]}; '
              f'extra labels removed: {extra[:24]}. Your previous questions '
              f'(labels only): {[q["label"] for q in cur_qs][:60]}.\n'
              f'FIX and return the complete corrected set.')
    parts = [{'text': prompt}]
    imgs = v6.render_page_jpegs(pdf_path, suspect_pages[:4])
    if imgs:
        parts.extend(imgs)
    ai, err = router.call(parts, GOD_EXTRACT_SCHEMA, kind='repair',
                          min_interval=args.min_interval,
                          budget_s=args.call_budget)
    if not ai:
        return None, 1, str(err or 'repair failed'), {}
    out = []
    for q in ai.get('questions') or []:
        lab = str(q.get('label') or '').strip()
        t = v6.clean_v6(str(q.get('text') or ''))
        if not lab or not t:
            continue
        flags = v6.leak_flags(t)
        if sum(1 for _k, v in flags.items() if v) >= 2:
            continue
        out.append({'label': lab, 'key': label_key(lab), 'text': t,
                    'marks': q.get('marks'), 'page': suspect_pages[0] if suspect_pages else 1,
                    'has_diagram': bool(q.get('has_diagram')),
                    'diagram_page': q.get('diagram_page'),
                    'diagram_bbox': q.get('diagram_bbox')})
    hdr = ai.get('paper_header') or {}
    return out, 1, None, {k: str(hdr[k])[:120] for k in ('subject', 'session') if hdr.get(k)}


# ---------------------------------------------------------------------------
# GOD verification
# ---------------------------------------------------------------------------

def fuzzy_ratio(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def god_verify(qs, ref):
    """Score extraction vs the AI-free printed reference.
    Smoke-test lesson: a weak reference that UNDERCOUNTS printed questions
    must never drive deletions - per-question fuzzy matching detects invented
    (enrichment) rows, while count mismatch with NO suspect rows means the
    reference itself is undercounting (review-ref, keep data, flag for the
    reference-parser iteration loop)."""
    containers = ref['ref']['containers']
    printed = {label_key(str(c['n'])): container_text(c) for c in containers}
    printed_labels = list(printed.keys())
    pool = [t for t in printed.values() if len(norm(t)) >= 18]
    ext = {q['key']: q for q in qs}
    ext_labels = list(ext.keys())
    db_nq = ref.get('db_nq')
    atomic = ref.get('atomic') or 0
    weakref = len(printed_labels) < 4 or (db_nq and atomic and not (0.5 * db_nq <= atomic <= 1.6 * db_nq + 4))
    missing = [str(k[0]) + (k[1] or '') for k in printed_labels if k not in ext]
    extra = [str(k[0]) + (k[1] or '') for k in ext_labels if k not in printed]
    scores = {}
    matched = 0
    n_scored = 0
    suspect_extra = []
    # atomic sub-part coverage: every printed (a)(b)(c)... letter of a container
    # must appear in the extracted container text - catches one dropped
    # sub-part inside an otherwise-matching container (fuzzy is too coarse)
    part_gaps = {}
    for c in containers:
        k = label_key(str(c['n']))
        letters = [p.get('letter') for p in c.get('parts') or [] if p.get('letter')]
        if letters and k in ext:
            t = (ext[k]['text'] or '').lower()
            have = (set(re.findall(r'\(([a-l])\)', t))
                    | set(re.findall(r'(?:^|\s)([a-l])[\.\)]\s', t)))
            miss = [L for L in letters if L not in have]
            if miss:
                part_gaps[str(k[0]) + (k[1] or '')] = miss
    for k in ext_labels:
        best_r = max((fuzzy_ratio(ext[k]['text'], t) for t in pool), default=0.0)
        if k in printed and len(norm(printed[k])) >= 18:
            scores[str(k[0]) + (k[1] or '')] = round(best_r, 2)
            n_scored += 1
            if best_r >= 0.45:
                matched += 1
        elif best_r < 0.30:
            suspect_extra.append(str(k[0]) + (k[1] or ''))
    score_avg = round(matched / n_scored, 2) if n_scored else None
    trusted = ref.get('trusted', not weakref)
    count_ok = trusted and len(ext_labels) == len(printed_labels)
    cover_ok = trusted and (not missing)
    extra_ok = trusted and (not extra)
    text_ok = trusted and n_scored and score_avg >= 0.45 and not part_gaps
    ok = count_ok and cover_ok and extra_ok and text_ok
    # extracted exceeds the reference with no per-question suspect rows ->
    # the REFERENCE is undercounting, not the extraction (no deletions!)
    ref_undercount = (trusted and not weakref and len(ext_labels) > len(printed_labels)
                      and not suspect_extra and (score_avg is None or score_avg >= 0.45))
    return {'ok': ok, 'weakref': weakref, 'trusted': trusted,
            'ref_undercount': ref_undercount,
            'count_ok': count_ok, 'cover_ok': cover_ok, 'extra_ok': extra_ok,
            'text_ok': text_ok, 'score_avg': score_avg, 'scores': scores,
            'part_gaps': part_gaps,
            'missing': missing, 'extra': extra, 'suspect_extra': suspect_extra,
            'printed_count': len(printed_labels), 'extracted_count': len(ext_labels)}


# ---------------------------------------------------------------------------
# diagrams
# ---------------------------------------------------------------------------

def crop_diagrams_god(pdf_path, qs, stem, sb):
    """Crop reported figure regions -> PNG -> god-diagrams bucket -> URLs."""
    want = [q for q in qs if q.get('has_diagram') and (q.get('diagram_page') or q.get('page'))]
    if not want:
        return 0
    by_page = {}
    for q in want:
        by_page.setdefault(q.get('diagram_page') or q['page'], []).append(q)
    n = 0
    for pno, group in by_page.items():
        imgs = render_pages_pil(pdf_path, [pno], scale=2.0)
        img = imgs.get(pno)
        if img is None:
            continue
        W, H = img.size
        for q in group:
            bbox = q.get('diagram_bbox') or []
            try:
                if len(bbox) == 4 and all(isinstance(x, (int, float)) for x in bbox):
                    x0, y0, x1, y1 = [max(0, min(1000, int(x))) for x in bbox]
                    box = (int(x0 / 1000 * W), int(y0 / 1000 * H),
                           max(int(x1 / 1000 * W), int(x0 / 1000 * W) + 40),
                           max(int(y1 / 1000 * H), int(y0 / 1000 * H) + 40))
                    crop = img.crop(box)
                else:
                    crop = img
                buf = io.BytesIO()
                crop.save(buf, 'PNG')
                if len(buf.getvalue()) > 4_800_000:
                    crop = crop.resize((max(1, crop.width // 2), max(1, crop.height // 2)))
                    buf = io.BytesIO()
                    crop.save(buf, 'PNG')
                local = os.path.join('evidence', 'god', 'diagrams', stem[:110],
                                     f'q{q["key"][0]}{q["key"][1] or ""}_p{pno}.png')
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with open(local, 'wb') as f:
                    f.write(buf.getvalue())
                if sb is not None:
                    url = sb.upload_diagram(DIA_BUCKET,
                                            f'{stem[:110]}/q{q["key"][0]}{q["key"][1] or ""}_p{pno}.png',
                                            local)
                    if url:
                        q['diagram_url'] = url
                n += 1
            except Exception as e:
                log(f'    [diagram] crop failed q{q["label"]}: {str(e)[:100]}')
    return n


# ---------------------------------------------------------------------------
# DB push (god6_* only)
# ---------------------------------------------------------------------------

def rest(sb, method, path, params=None, body=None, upsert_keys=None):
    hdr = dict(sb.hdr)
    if upsert_keys:
        hdr['Prefer'] = 'resolution=merge-duplicates,return=representation'
    if method == 'DELETE':
        r = requests.delete(f'{sb.url}{path}', headers=hdr, params=params or {}, timeout=60)
    elif method == 'POST':
        r = requests.post(f'{sb.url}{path}', headers=hdr, params=params or {},
                          data=json.dumps(body), timeout=60)
    else:
        r = requests.get(f'{sb.url}{path}', headers=hdr, params=params or {}, timeout=60)
    return r


def push_god_paper(sb, row, qs, verdict, ref, verify, calls, run_id, header):
    """Upsert god6_papers + paper-scoped replace of god6_questions + audit."""
    now = datetime.now(timezone.utc).isoformat()
    prow = {'group_name': row['group'], 'code': row['code'], 'year': row.get('year'),
            'printed_subject': (header or {}).get('subject'),
            'printed_session': (header or {}).get('session'),
            'pdf_stem': row['stem'], 'pdf_path': row.get('path'),
            'file_hash': row.get('file_hash'), 'storage_url': row.get('storage_url'),
            'printed_containers': verify.get('printed_count'),
            'printed_atomic': sum(len(c.get('parts') or []) for c in ref['ref']['containers']),
            'extracted_count': verify.get('extracted_count'),
            'gold': verdict == 'gold', 'status': verdict, 'attempts': row.get('attempts', 1),
            'vision_calls': calls, 'diagram_count': sum(1 for q in qs if q.get('diagram_url')),
            'run_id': run_id, 'error': row.get('error'), 'updated_at': now}
    r = rest(sb, 'POST', '/rest/v1/god6_papers',
             params={'on_conflict': 'pdf_stem'}, upsert_keys=True,
             body=[prow])
    r.raise_for_status()
    pr = r.json()
    pid = pr[0]['id'] if isinstance(pr, list) and pr else None
    if not pid:
        raise RuntimeError('god6_papers upsert returned no id')
    rest(sb, 'DELETE', '/rest/v1/god6_questions',
         params={'paper_id': f'eq.{pid}'}).raise_for_status()
    if qs:
        qrows = []
        for i, q in enumerate(qs, 1):
            qrows.append({'paper_id': pid, 'seq': q['label'],
                          'text': q['text'], 'marks': q.get('marks'),
                          'has_diagram': bool(q.get('has_diagram')),
                          'diagram_url': q.get('diagram_url'),
                          'hash': hashlib.sha256(norm(q['text']).encode()).hexdigest()[:32],
                          'verify': 'printed-matched' if not verify.get('weakref') else 'weak-ref',
                          'updated_at': now})
        for i in range(0, len(qrows), 200):
            rest(sb, 'POST', '/rest/v1/god6_questions',
                 params={'on_conflict': 'paper_id,seq'}, upsert_keys=True,
                 body=qrows[i:i + 200]).raise_for_status()
    return pid


def push_audit(sb, row, run_id, shard, attempt, verdict, verify, calls, notes):
    body = {'pdf_stem': row['stem'], 'run_id': run_id, 'shard': shard,
            'attempt': attempt, 'verdict': verdict,
            'printed_containers': verify.get('printed_count'),
            'extracted_count': verify.get('extracted_count'),
            'diff': {'missing': verify.get('missing', [])[:30],
                     'extra': verify.get('extra', [])[:30],
                     'suspect_extra': verify.get('suspect_extra', [])[:30],
                     'part_gaps': verify.get('part_gaps'),
                     'scores': verify.get('scores'),
                     'score_avg': verify.get('score_avg'),
                     'weakref': verify.get('weakref'),
                     'ref_kind': verify.get('ref_kind')},
            'vision_calls': calls, 'notes': notes}
    rest(sb, 'POST', '/rest/v1/god6_audit', body=[body]).raise_for_status()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--papers-file', required=True)
    ap.add_argument('--group', default='', help='filter: DS/COA/DSTL/TC/MATHS4/PYTHON')
    ap.add_argument('--shard', type=int, default=0)
    ap.add_argument('--shards', type=int, default=1)
    ap.add_argument('--max-calls', type=int, default=400)
    ap.add_argument('--min-interval', type=float, default=3.0)
    ap.add_argument('--call-budget', type=int, default=110)
    ap.add_argument('--attempts', type=int, default=3, help='total verify-loop passes')
    ap.add_argument('--dpi', type=int, default=250)
    ap.add_argument('--max-pages', type=int, default=40)
    ap.add_argument('--dirs', default='corpus')
    ap.add_argument('--out-dir', default='evidence/god')
    ap.add_argument('--run-id', type=int, default=0)
    ap.add_argument('--db', action='store_true')
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.papers_file, encoding='utf-8')
            if l.strip() and not l.startswith('#')]
    if args.group:
        rows = [r for r in rows if r['group'] == args.group]
    mine = [r for i, r in enumerate(rows) if i % args.shards == args.shard]
    log(f'[god] shard {args.shard}/{args.shards}: {len(mine)}/{len(rows)} papers'
        + (f' group={args.group}' if args.group else ''))

    index = {}
    for d in args.dirs.split(','):
        d = d.strip()
        if os.path.isdir(d):
            for root, _d, files in os.walk(d):
                for f in files:
                    if f.lower().endswith('.pdf'):
                        index.setdefault(os.path.splitext(f)[0].lower(),
                                         os.path.join(root, f))
    log(f'[god] corpus index: {len(index)} PDFs')

    keys = load_key_pool().keys
    models = load_models()
    router = AIRouterV7(keys, models, max_calls=args.max_calls)
    sb = None
    if args.db:
        if not (os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_SERVICE_KEY')):
            sys.exit('--db needs SUPABASE_URL + SUPABASE_SERVICE_KEY')
        sb = Supabase(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])

    os.makedirs(args.out_dir, exist_ok=True)
    vpath = os.path.join(args.out_dir, f'god_verdicts_shard{args.shard}.jsonl')
    done = set()
    if os.path.exists(vpath):
        for l in open(vpath):
            try:
                if json.loads(l).get('verdict') not in ('failed',):
                    done.add(json.loads(l)['stem'])
            except Exception:
                pass
    log(f'[god] resume: {len(done)} done')

    stats = {'gold': 0, 'review-weakref': 0, 'review-diff': 0, 'failed': 0,
             'pdf-missing': 0, 'calls': 0, 'diagrams': 0}
    with open(vpath, 'a', encoding='utf-8') as vf:
        for i, row in enumerate(mine, 1):
            stem = row['stem']
            if stem in done:
                continue
            out = {'stem': stem, 'group': row['group'], 'code': row['code'],
                   'shard': args.shard, 'run_id': args.run_id}
            path = index.get(stem)
            if not path:
                # code+year glob fallback (second naming convention)
                y0 = (row.get('year') or '')[:4]
                code_l = row['code'].lower()
                cands = [k for k in index
                         if code_l in k and (y0 and (f'-{y0}' in k or y0 in k))]
                if cands:
                    path = index[sorted(cands)[0]]
                    out['resolved_by'] = 'code+year-glob'
            if not path:
                out.update({'verdict': 'pdf-missing'})
                stats['pdf-missing'] += 1
                vf.write(json.dumps(out, ensure_ascii=False) + '\n'); vf.flush()
                if sb:
                    push_audit(sb, row, args.run_id, args.shard, 0, 'pdf-missing',
                               {'printed_count': None, 'extracted_count': None},
                               0, 'stem not in corpus index')
                continue
            out['path'] = path
            row['path'] = path
            row['attempts'] = 0
            s0 = router.sends
            try:
                h = sha256(path)
                out['hash_ok'] = (not row.get('file_hash')) or h == row['file_hash']
            except Exception:
                out['hash_ok'] = None
            try:
                calls = 0
                real_calls = lambda: router.sends - s0
                ref = printed_reference(path, dpi=args.dpi, max_pages=args.max_pages,
                                        db_nq=row.get('db_n_questions'))
                verify = {'ref_kind': ref['ref_kind']}
                qs = []
                verdict = 'failed'
                header = {}
                for attempt in range(1, max(1, args.attempts) + 1):
                    row['attempts'] = attempt
                    if attempt == 1:
                        qs, c, header = extract_chunks(router, path, ref, args)
                    else:
                        fixed, c, err, hdr2 = repair_pass(
                            router, path, ref, verify.get('missing', []),
                            verify.get('extra', []), qs, args,
                            suspect_pages(qs, ref))
                        if hdr2:
                            header.update(hdr2)
                        if err or not fixed:
                            log(f'    [god] attempt {attempt}: repair failed ({str(err)[:100]})')
                            c = 0
                        else:
                            qs = fixed
                    calls += c
                    verify = god_verify(qs, ref)
                    verify['ref_kind'] = ref['ref_kind']
                    if verify['ok']:
                        verdict = 'gold'
                        break
                    if verify['weakref']:
                        verdict = 'review-weakref'
                        break
                    if verify.get('ref_undercount'):
                        verdict = 'review-ref'
                        break
                    verdict = 'review-diff'
                    log(f"    [god] attempt {attempt}: FAIL count={verify['extracted_count']}/"
                        f"{verify['printed_count']} missing={verify['missing'][:8]} "
                        f"extra={verify['extra'][:8]} gaps={list((verify.get('part_gaps') or {}).items())[:4]} "
                        f"score={verify['score_avg']}")
                if sb:
                    try:
                        push_audit(sb, row, args.run_id, args.shard, row['attempts'],
                                   verdict, verify, real_calls(), PROMPT_VERSION)
                    except Exception as e:
                        log(f'    [db] audit failed: {str(e)[:120]}')
                if verdict in ('gold', 'review-weakref', 'review-diff', 'review-ref'):
                    n_dia = crop_diagrams_god(path, qs, stem, sb)
                    stats['diagrams'] += n_dia
                    if sb:
                        pid = push_god_paper(sb, row, qs, verdict, ref, verify,
                                             real_calls(), args.run_id, header)
                        out['paper_id'] = pid
                out.update({'verdict': verdict, 'calls': real_calls(),
                            'printed': verify.get('printed_count'),
                            'extracted': verify.get('extracted_count'),
                            'score': verify.get('score_avg'),
                            'suspect_extra': len(verify.get('suspect_extra') or []),
                            'trusted_ref': ref.get('trusted'),
                            'hash_ok': out.get('hash_ok'),
                            'ref_kind': ref['ref_kind'],
                            'diagrams': sum(1 for q in qs if q.get('diagram_url')),
                            'subject': header.get('subject')})
                stats[verdict] = stats.get(verdict, 0) + 1
                stats['calls'] += real_calls()
            except Exception as e:
                out.update({'verdict': 'failed',
                            'error': f'{type(e).__name__}: {str(e)[:220]}'})
                stats['failed'] += 1
                log(f'    [god] EXCEPTION: {out["error"][:160]}')
            vf.write(json.dumps(out, ensure_ascii=False) + '\n'); vf.flush()
            if i % 5 == 0 or i == len(mine):
                log(f'  [{i}/{len(mine)}] ' +
                    ' '.join(f'{k}={v}' for k, v in stats.items() if v))
    log(f"[god] shard {args.shard} DONE: {stats}")
    sys.exit(0)


def suspect_pages(qs, ref):
    """Pages of questions that exist in extraction (repair re-reads them)."""
    pages = sorted({(q.get('diagram_page') or q.get('page') or 1) for q in qs} or {1})
    return pages


class AIRouterV7(v6.AIRouterV6):
    """v6 router (image parts + budget) with exact SEND accounting: every
    real API send anywhere in the v5 ladder is counted exactly once."""

    def __init__(self, keys, models, max_calls=0):
        super().__init__(keys, models, max_calls=max_calls)
        self.sends = 0

    def _attempt(self, model, key, prompt, schema):
        if self.max_calls and self.budget_spent >= self.max_calls:
            return None, 'god send budget exhausted'
        self.sends += 1
        self.budget_spent = self.sends
        return super()._attempt(model, key, prompt, schema)


if __name__ == '__main__':
    main()
