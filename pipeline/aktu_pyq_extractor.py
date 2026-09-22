#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aktu_pyq_extractor.py  --  AKTU PYQ COMPLETE pipeline (v4, PAPER-WISE)
=======================================================================
PAPER-WISE ONE-PASS DESIGN (v4): every PDF that enters the extract loop
leaves FULLY done in Supabase - questions + marks + unit_topic +
question_type + parent links + diagram crops + embeddings + status.
There is no separate enrich phase left to starve when a run hits its
time budget: enrichment happens INSIDE the per-paper loop.

Stages (run one, or --stage all):
  extract  Tier0 cleanup -> Tier1 fuzzy parse -> confidence gate -> Tier2 AI
           repair -> IN-PASS AI enrich (topics/types/parents/marks) ->
           diagram crops -> Supabase push -> per-paper embeddings.
           DB-DRIVEN RESUME: every rerun hashes each PDF and skips any paper
           already extraction_status='complete' in the papers table - even if
           the local state file was deleted.
  enrich   BACKFILL: AI-tags questions of legacy papers still pending topics
           (papers with enriched_at NULL); also fills NULL marks opportunistically.
  repair   BACKFILL: AI infers marks for questions with marks IS NULL
           (VALID_MARKS-gated, idempotent, only fills NULLs).
  embed    local fastembed (all-MiniLM-L6-v2, 384-dim) vectors for every
           question with embedding IS NULL. Zero API cost, deterministic.
  cluster  per-subject cosine >= 0.88 union-find grouping -> clusters table
           (freq_count = distinct years, importance 0-100). This powers the
           "most repeated questions" ranking (repeated_questions view).

Idempotency at every layer (reruns never duplicate or redo work):
  papers.file_hash UNIQUE + extraction_status  -> extract skips done papers
  questions (subject_code, question_hash) UNIQUE -> upsert, never duplicate
  occurrences (question_id, paper_id) UNIQUE     -> upsert, never duplicate
  papers.enriched_at                             -> enrich skips done papers
  questions.embedding IS NULL                    -> embed skips done questions
  clusters recomputed per subject (delete+insert) -> always consistent

Env vars:
  GEMMA_API_KEYS         comma-separated AI Studio keys (fallback chain;
                         GEMMA_API_KEY single key still accepted)
  GEMMA_MODEL            optional extra candidate model (hardcoded ladder in
                         AIRouter is the default; ordering is per call-kind)
  GEMMA_FALLBACK_MODELS  optional full ladder override (CSV), else hardcoded
  SUPABASE_URL           https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY   service_role key (server-side only, NEVER in browser)

Usage:
  python3 aktu_pyq_extractor.py --stage extract --dir corpus/unstructured --db --ai \
              --manifest data/ryzenstudy_download_manifest.csv
  python3 aktu_pyq_extractor.py --stage enrich --db
  python3 aktu_pyq_extractor.py --stage embed --db
  python3 aktu_pyq_extractor.py --stage cluster --db
  python3 aktu_pyq_extractor.py --stage all --dir corpus/unstructured --db --ai
  python3 aktu_pyq_extractor.py --list-models
"""
import argparse, hashlib, json, os, re, sys, time, uuid, difflib
from collections import defaultdict
from datetime import datetime, timezone

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber missing: pip install pdfplumber pypdfium2 pillow requests ftfy")

try:
    import ftfy
    HAVE_FTFY = True
except ImportError:
    HAVE_FTFY = False

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
    import pytesseract
    from pytesseract import Output
    HAVE_TESSERACT = True
except ImportError:
    HAVE_TESSERACT = False

PIL_OK = True
try:
    from PIL import Image
except ImportError:
    PIL_OK = False

# google-genai SDK (owner-specified structured-output path for Tier 2 / enrich)
try:
    from google import genai as ggenai
    from google.genai import types as gtypes
    HAVE_GENAI = True
except ImportError:
    HAVE_GENAI = False

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
DEV_RANGE = re.compile(r'[\u0900-\u097F]')
CID_JUNK  = re.compile(r'\(cid:\d+\)')
MARKS_MATH = re.compile(r'(\d{1,2})\s*[xX\u00d7]\s*(\d{1,2})\s*=\s*(\d{1,3})')
SECTION_RX = re.compile(r'^\s*SECTION\s*[-.:]?\s*([A-Z])\b', re.I)
UNIT_RX    = re.compile(r'^\s*UNIT\s*[-.\s]?\s*(\d+|I{1,3}|IV|V|VI{1,3})\b', re.I)
OR_RX      = re.compile(r'^\s*(?:OR|Or|oR|0R)\s*:?\s*$')
ATTEMPT_RX = re.compile(r'^\s*(?:Q?\s*(\d{1,2})\s*[.:)]?\s*)?Attempt\s*(all|any|the)\b', re.I)
SUB_PAREN_RX = re.compile(r'^\s*\(?([a-jA-J])\s*[).:\]]\s*')          # (a) / a. / a)
MAIN_NUM_RX  = re.compile(r'^\s*(\d{1,2})\s*[.)]\s+')
VALID_MARKS  = {1,2,3,4,5,6,7,8,9,10,12,14,15,16,20}
FOOTER_PAT   = re.compile(r'(AKTU_QP|Printed Page|Printed Pages)', re.I)
PROMPT_VERSION = 'v3-pipeline'

# --- question-text junk stripper (Sept-2026 DB clean-up parity) ------------
# Mirrors scripts/fixes/02_p1_text.py: removes QP print tokens, timestamps,
# IP watermarks, 'P a g e' footers, glued next-page headers, CO/K marker
# clusters and letter-stutter artifacts BEFORE normalising/hashing.
SUBJECT_GLUE_RX = re.compile(r'\s+Subject\s+Code\s*:.*$', re.S | re.I)
ROLL_GLUE_RX    = re.compile(r'\s+Roll\s+No\s*:.*$', re.S | re.I)
QP_TOKEN_RX     = re.compile(r'\s*\|?\s*QP\d{2}[A-Z0-9_]*')
TS_ALPHA_RX     = re.compile(r'\s*\|?\s*\d{1,2}-[A-Za-z]{3}-\d{4}'
                             r'(\s+\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM)?)?', re.I)
TS_NUM_RX       = re.compile(r'\s*\|?\s*\d{1,2}-\d{1,2}-\d{4}'
                             r'(\s+\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM)?)?', re.I)
IP_RS_RX        = re.compile(r'\s*\|?\s*117\.55\.242\.\d{1,3}\b')
IP_PIPE_RX      = re.compile(r'\s*\|\s*(\d{1,3}\.){3}\d{1,3}\b')
FOOTER_RX       = re.compile(r'[\|\s]*\d{0,2}\s*\|?\s*P\s*a\s*g\s*e\b\s*\|?[\s\d]{0,4}', re.I)
PRINTED_RX      = re.compile(r'Printed\s+Pages?\s*[:.]?\s*\d{0,2}', re.I)
CO_K_PAIR_RX    = re.compile(r'\bCO\s?[-\u2013]?\s?[0-6]\b[\s,/&-]{0,3}\bK\s?[1-6]\b')
K_CO_PAIR_RX    = re.compile(r'\bK\s?[1-6]\b[\s,/&-]{0,3}\bCO\s?[-\u2013]?\s?[0-6]\b')
DIGIT_CO_K_RX   = re.compile(r'(?:(?<=[\s.:\)])\d{1,2})?\s*[\(\[]?(?:CO\s?[-\u2013]?\s?[0-6]|K\s?[1-6])[\)\]]?')
CO_STANDALONE_RX= re.compile(r'\bCO\s?[-\u2013]?\s?[0-6]\b')
K_STANDALONE_RX = re.compile(r'\bK\s?[1-6]\b')
GIBBERISH_RX    = re.compile(r'(?:[\s._]+[A-Za-z0-9](?=[\s._]|$)){5,}')
MIDPIPE_RX      = re.compile(r'([a-z])\|([a-z])')
STUTTER_RX      = re.compile(r'([A-Za-z])\1{2,}')
TAIL_PIPES_RX   = re.compile(r'[\s|]+$')
LEAD_PIPES_RX   = re.compile(r'^[\s|]+')
TAIL_DIGITS_RX  = re.compile(r'(?<=[.!?])\s*\d{1,2}([,|\s]+\d{0,2})?$')
MATH_CTX_RX     = re.compile(r'automat|regular expression|\bNFA\b|\bDFA\b|grammar', re.I)
GUARD_WIN_RX    = re.compile(r'(carbon\s*dioxide|co2|emissions?\s+of\s*$|laser|ppm|'
                             r'dioxide|vitamin|potassium|kelvin|k-?nearest|'
                             r'k-?means|k-map|karnaugh)', re.I)
EMPTY_BRACKETS_RX = re.compile(r'\(\s*\)|\[\s*\]')
MULTISPACE_RX   = re.compile(r'\s{2,}')
EDGE_PUNCT_RX   = re.compile(r'^[\s.,;:|-]+|[\s,;:|-]+$')


def clean_question_text(text):
    """Strip exam-format junk; keep science content (CO2, Vitamin K1, K-map)."""
    out = text
    if STUTTER_RX.search(out):
        collapsed = STUTTER_RX.sub(r'\1', out)
        if len(collapsed) < 0.8 * len(out):
            out = collapsed
    for rx in (GIBBERISH_RX, SUBJECT_GLUE_RX, ROLL_GLUE_RX, PRINTED_RX,
               FOOTER_RX, QP_TOKEN_RX, TS_ALPHA_RX, TS_NUM_RX, IP_RS_RX,
               IP_PIPE_RX):
        out = rx.sub('', out)
    if CO_K_PAIR_RX.search(out) or K_CO_PAIR_RX.search(out):
        out = CO_K_PAIR_RX.sub('', out)
        out = K_CO_PAIR_RX.sub('', out)
    out = DIGIT_CO_K_RX.sub('', out)
    for rx in (CO_STANDALONE_RX, K_STANDALONE_RX):
        hits = list(rx.finditer(out))
        for m in reversed(hits):
            ctx = out[max(0, m.start() - 28):m.end()]
            if not GUARD_WIN_RX.search(ctx):
                out = out[:m.start()] + out[m.end():]
    if not MATH_CTX_RX.search(out):
        out = MIDPIPE_RX.sub(r'\1\2', out)
    out = EMPTY_BRACKETS_RX.sub('', out)
    out = TAIL_PIPES_RX.sub('', out)
    out = LEAD_PIPES_RX.sub('', out)
    out = MULTISPACE_RX.sub(' ', out)
    out = EDGE_PUNCT_RX.sub('', out)
    return out.strip()
EMBED_MODEL = 'sentence-transformers/all-MiniLM-L6-v2'   # 384-dim (matches schema)
EMBED_DIM = 384
CLUSTER_SIM = 0.88   # cosine threshold for near-duplicate question grouping

# Kruti-Dev signature tokens (legacy Devanagari fonts map to latin-ish chars)
KRUTI_TOKENS = [' iz\u201d', 'iz\u201d', 'mRrj', 'fuEu', 'lHkh', 'vFkok', 'fgUnh', 'vaxzsth',
                'la{ksi', 'mi;qDr', 'iz;ksx', 'nhft,', 'dja', 'esa ', ' dk ', ' ds ',
                ' dks ', ',oa ', 'gSa', 'gks']
def _kruti_hits(s):
    t = ' ' + s + ' '
    n = 0
    for tok in set(KRUTI_TOKENS):
        n += t.count(tok)
    return n

def log(msg):
    print(msg, flush=True)

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

# ---------------------------------------------------------------------------
# Tier 0 : text cleanup
# ---------------------------------------------------------------------------
def keep_char(o):
    """Char-level surgical filter: drop diagonal-watermark letters (tall+isolated),
    zero-width junk, control chars."""
    if o.get('object_type') != 'char':
        return True
    t = o.get('text', '')
    if not t or t.isspace() or ord(t[0]) < 32:
        return False
    h = o.get('bottom', 0) - o.get('top', 0)
    return h <= 20.0 or o.get('size', 0) > 15  # big title fonts stay; tall strays go

def assemble_lines(page):
    """Words -> lines by top clustering (tol 2.8pt). Returns [(top,bottom,text)]."""
    flt = page.filter(keep_char)
    words = flt.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.6) \
        if hasattr(flt, 'extract_words') else page.extract_words(x_tolerance=1.6)
    if not words:
        return []
    words.sort(key=lambda w: (round(w['top'], 1), w['x0']))
    lines, cur, cur_top = [], [], None
    for w in words:
        if cur_top is None or abs(w['top'] - cur_top) <= 2.8:
            cur.append(w); cur_top = w['top'] if cur_top is None else cur_top
        else:
            cur.sort(key=lambda x: x['x0'])
            lines.append((cur[0]['top'], max(x['bottom'] for x in cur),
                          ' '.join(x['text'] for x in cur)))
            cur, cur_top = [w], w['top']
    if cur:
        cur.sort(key=lambda x: x['x0'])
        lines.append((cur[0]['top'], max(x['bottom'] for x in cur),
                      ' '.join(x['text'] for x in cur)))
    return lines

def is_hindi_line(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    dev = sum(1 for c in s if DEV_RANGE.match(c))
    if dev / max(1, len(letters)) > 0.25:
        return True
    return _kruti_hits(s) >= 2

def ocr_lines(pdf_path, dpi=200):
    """Scanned-paper fallback: rasterize + tesseract. Returns same line dicts."""
    if not (HAVE_PDFIUM and HAVE_TESSERACT):
        return [], {'ocr': 'unavailable'}
    doc = pdfium.PdfDocument(pdf_path)
    out = []
    try:
        for pno in range(len(doc)):
            img = doc[pno].render(scale=dpi / 72).to_pil()
            data = pytesseract.image_to_data(img, output_type=Output.DICT)
            rows = defaultdict(list)
            for i, txt in enumerate(data['text']):
                if not txt.strip() or int(data.get('conf', ['0'])[i]) < 35:
                    continue
                key = round(data['top'][i] / (14 * dpi / 72))   # ~14pt line buckets
                rows[key].append((data['left'][i], txt))
            for key in sorted(rows):
                ws = sorted(rows[key])
                out.append({'page': pno + 1,
                            'top': key * (14 * dpi / 72),
                            'bottom': key * (14 * dpi / 72) + 14 * dpi / 72,
                            'text': ' '.join(t for _, t in ws)})
    finally:
        doc.close()
    return out, {'ocr': 'done'}

def tier0(pdf_path):
    """Extract clean lines + metadata + per-page geometry."""
    meta, pages = {}, []
    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, 1):
            raw = page.extract_text() or ''
            m = re.search(r'(Sub\s*Code|Subject\s*Code)\s*[:\-]?\s*([A-Z]{2,4}[- ]?\d{3}[A-Z]?)', raw)
            if m and 'code' not in meta:
                meta['code'] = m.group(2).replace(' ', '-')
            m = re.search(r'THEORY\s+EXAMINATION\s+(\d{4}-\d{2})', raw)
            if m and 'year' not in meta:
                meta['year'] = m.group(1)
            m = re.search(r'Total\s*Marks\s*[:%]?\s*(\d+)', raw)
            if m and 'total_marks' not in meta:
                meta['total_marks'] = int(m.group(1))
            m = re.search(r'Paper\s*Id\s*[:\-]?\s*(\d+)', raw)
            if m and 'paper_id' not in meta:
                meta['paper_id'] = m.group(1)
            drawings = list(page.images) + list(page.curves) + list(page.lines) + list(page.rects)
            pages.append({'no': pno, 'lines': assemble_lines(page), 'drawings': drawings,
                          'w': page.width, 'h': page.height})
    # flatten with page tracking, drop junk lines
    out, stats = [], {'hindi': 0, 'junk': 0, 'cid': 0}
    for pg in pages:
        for (top, bot, text) in pg['lines']:
            s = CID_JUNK.sub(' ', text)
            if s != text:
                stats['cid'] += 1
            s = re.sub(r'\s+', ' ', s).strip()
            if not s:
                continue
            if FOOTER_PAT.match(s) or re.fullmatch(r'[\W\d\s]{1,4}', s):
                stats['junk'] += 1
                continue
            if is_hindi_line(s):
                stats['hindi'] += 1
                continue
            if HAVE_FTFY:
                s = ftfy.fix_text(s)
            out.append({'page': pg['no'], 'top': top, 'bottom': bot, 'text': s})
    meta['n_pages'] = n_pages
    if len(out) < 25:
        # no usable text layer -> OCR fallback (scanned papers)
        ocr, ostats = ocr_lines(pdf_path)
        if ocr:
            cleaned = []
            for l in ocr:
                s = re.sub(r'\s+', ' ', l['text']).strip()
                if not s or FOOTER_PAT.match(s) or re.fullmatch(r'[\W\d\s]{1,4}', s):
                    continue
                if is_hindi_line(s):
                    stats['hindi'] += 1
                    continue
                cleaned.append(l)
            stats.update(ostats)
            meta['is_text_layer'] = False
            return cleaned, meta, stats, pages
        stats['ocr'] = ostats.get('ocr', 'unavailable')
    return out, meta, stats, pages

# ---------------------------------------------------------------------------
# Tier 1 : fuzzy parser
# ---------------------------------------------------------------------------
def close_section(line):
    """Fuzzy SECTION-A header match (handles 'SECTIOEN B' class typos).
    Single source of truth: SECTION_RX only - v3 had a mismatch between the
    pre-check regex and SECTION_RX (e.g. 'SECTION A2' passed the pre-check
    but SECTION_RX's \\b failed) -> AttributeError crash on that paper."""
    m = SECTION_RX.match(line)
    if m:
        return m.group(1).upper()
    head = line[:12].upper()
    if 'SECTI' in head and len(line) < 30:
        m2 = re.search(r'([A-Z])\s*$', line.strip())
        if m2:
            return m2.group(1)
    return None

def parse_paper(lines, meta):
    """Fuzzy multi-signal parse -> dict(paper, questions, checks, confidence)."""
    questions, sections, notes = [], [], []
    section, unit, marks_spec = None, None, None
    pending_choice = None
    pairing_open = False
    cur = None

    def flush():
        nonlocal cur, pairing_open
        if cur and len(cur['text']) >= 15:
            if pairing_open and questions and questions[-1].get('choice_group') is None \
               and cur.get('choice_group') is None \
               and questions[-1].get('section') == cur.get('section'):
                g = uuid.uuid4()
                questions[-1]['choice_group'] = g
                cur['choice_group'] = g
                pairing_open = False
            questions.append(cur)
        elif cur:
            # too short: prepend to previous question (continuation junk)
            if questions and cur['text']:
                questions[-1]['text'] = (questions[-1]['text'] + ' ' + cur['text']).strip()
        cur = None

    for ln in lines:
        s = ln['text']
        sec = close_section(s)
        if sec:
            flush(); section, marks_spec = sec, None
            sections.append(sec); continue
        um = UNIT_RX.match(s)
        if um:
            flush()
            u = um.group(1)
            unit = int(u) if u.isdigit() else {'I':1,'II':2,'III':3,'IV':4,'V':5,'VI':6,'VII':7,'VIII':8}.get(u.upper())
            continue
        if section is None and re.search(r'THEORY EXAM|B\.?\s*TECH|Time\s*:|Total Marks|'
                                         r'Sub\s*Code|Paper\s*Id|Roll\s*No|\bSEM\b', s, re.I):
            continue
        if OR_RX.match(s):
            pending_choice = uuid.uuid4()          # next question shares group w/ prev
            continue
        if re.match(r'^\s*(Note|uksV)\b', s, re.I):
            continue
        am = ATTEMPT_RX.match(s)
        if am:
            flush()
            pairing_open = 'any one' in s.lower()
            mm = MARKS_MATH.search(s)
            if mm:
                marks_spec = int(mm.group(1))       # "2 x 7 = 14" -> each part 2 marks
            notes.append(s)
            continue
        sm = re.match(r'^\s*\(([a-jA-J])\s*[).:\]]\s*(.+)$', s)          # (a) text
        sm2 = re.match(r'^\s*([a-jA-J])\s*[.:]\s+(.+)$', s) if (not sm and section) else None  # a. text
        mm = MAIN_NUM_RX.match(s)
        is_table_row = bool(re.search(r'\s(\d{1,2})\s+\d{1,2}\s*$', s)) and not sm and not sm2
        if sm or sm2:
            letter, rest = (sm.group(1), sm.group(2)) if sm else (sm2.group(1), sm2.group(2))
            rest = rest.strip()
            qmarks = None
            tn = re.search(r'\s(\d{1,2})\s+(\d{1,2})\s*$', rest)   # trailing "10 3" marks+CO cols
            if tn:
                cand = int(tn.group(1))
                if cand in VALID_MARKS:
                    qmarks, rest = cand, rest[:tn.start()].rstrip()
            flush()
            cur = {'label': letter.lower(), 'text': rest, 'marks': qmarks or marks_spec,
                   'section': section, 'unit': unit,
                   'choice_group': pending_choice, 'lines': [ln], 'page': ln['page'],
                   'top': ln['top']}
            pending_choice = None
            continue
        if mm and section and not cur:
            # main numbered question without letter (older format / FAM-3)
            cur = {'label': mm.group(1), 'text': s[mm.end():].strip(), 'marks': None,
                   'section': section, 'unit': unit, 'choice_group': None,
                   'lines': [ln], 'page': ln['page'], 'top': ln['top']}
            continue
        if cur is not None:
            # continuation of current question
            if is_table_row and len(cur['text']) > 30:
                trail = re.search(r'(\d{1,2})\s+\d{1,2}\s*$', s)
                if trail and cur.get('marks') is None:
                    cur['marks'] = int(trail.group(1))   # trailing "10 3" cols
                continue                                  # drop CO col noise
            cur['text'] += ' ' + s
            cur['lines'].append(ln)
        elif is_table_row or re.fullmatch(r'Q\s*no\.?.*Question.*Marks.*', s, re.I):
            continue                                       # table header / stray row
        elif len(s) < 25 and not any(c.isalpha() for c in s):
            continue
        else:
            # FAM-3: "1. Question text ... (2 marks)" as its own start
            m3 = re.match(r'^\s*(\d{1,2})\s*[.)]\s+(.+)$', s)
            if m3 and section and len(m3.group(2)) > 12:
                flush()
                cur = {'label': m3.group(1), 'text': m3.group(2).strip(), 'marks': None,
                       'section': section, 'unit': unit, 'choice_group': None,
                       'lines': [ln], 'page': ln['page'], 'top': ln['top']}
                mm3 = re.search(r'[\(\[]\s*(\d{1,2})\s*(?:marks?|mks?)?\s*[\)\]]', s, re.I)
                if mm3:
                    cur['marks'] = int(mm3.group(1))
    flush()

    # ---- self checks + confidence ----
    n = len(questions)
    with_marks = [q for q in questions if q['marks'] in VALID_MARKS]
    frac_marks = len(with_marks) / n if n else 0.0
    count_ok = 5 <= n <= 60
    len_ok = sum(1 for q in questions if len(q['text']) >= 15) / n if n else 0
    sec_ok = 1.0 if sections else 0.0
    conf = round(0.40 * frac_marks + 0.25 * (1.0 if count_ok else max(0, 1 - abs(n - 20) / 40))
                 + 0.20 * len_ok + 0.15 * sec_ok, 3)
    checks = {'questions': n, 'frac_marks': round(frac_marks, 2), 'count_ok': count_ok,
              'len_ok': round(len_ok, 2), 'sections': sections, 'notes': len(notes)}
    return {'questions': questions, 'checks': checks, 'confidence': conf,
            'meta': meta, 'notes': notes}

def infer_diagram_flags(parsed, lines_by_page):
    """Keyword heuristic -> has_diagram; keep per-question band for cropping."""
    KW = re.compile(r'\b(draw|sketch|figure|diagram|graph|circuit|waveform|plot|'
                    r'characteristic curve|block diagram|flow ?chart|label)\b', re.I)
    for q in parsed['questions']:
        q['has_diagram'] = bool(KW.search(q['text']))
    return parsed

# ---------------------------------------------------------------------------
# Tier 2 : AI layer -- Gemma structured output with 3-key fallback chain
# ---------------------------------------------------------------------------
GEMMA_BASE = 'https://generativelanguage.googleapis.com/v1beta'

REPAIR_SCHEMA = {
    'type': 'object',
    'properties': {
        'questions': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'label': {'type': 'string'},
                'text': {'type': 'string'},
                'marks': {'type': 'integer', 'nullable': True},
                'has_diagram': {'type': 'boolean', 'nullable': True},
            },
            'required': ['label', 'text'],
        }},
    },
    'required': ['questions'],
}

ENRICH_SCHEMA = {
    'type': 'object',
    'properties': {
        'items': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'n': {'type': 'string'},
                'topic': {'type': 'string'},
                'type': {'type': 'string'},
                'parent': {'type': 'string', 'nullable': True},
                'marks': {'type': 'integer', 'nullable': True},
            },
            'required': ['n', 'topic', 'type'],
        }},
    },
    'required': ['items'],
}

QTYPES_ALLOWED = {'mcq', 'numerical', 'diagram', 'short', 'theory'}

def load_models():
    """CANDIDATE model set (ordering is decided per-call-kind by AIRouter).

    Hardcoded from LIVE probe evidence (model-probe runs 34629096844 +
    34629736035) + the user's AI Studio rate-limit dashboard (2026-09-12):
      gemma-4-26b-a4b-it / gemma-4-31b-it : RPM 30, TPM 16K,  RPD 14,400
      gemini-3.1/3.5-flash-lite           : RPM 15, TPM 250K, RPD 500
      gemini-3.x-flash / 3-flash-preview  : RPM 5,  TPM 250K, RPD 20
      gemini-2.5-flash                    : RPM 5,  TPM 250K, RPD 20
    NOTE: 'gemini-flash-lite-latest' is an ALIAS of 3.5-flash-lite -> excluded
    so one physical model can't burn quota under two names.
    """
    # v5.6 ladder = ONLY models that passed the live strict-JSON probe
    # (keyprobe run 35764793813, 4 keys). Dropped: 2.5-flash-lite (404 on all
    # keys), 3-flash-preview / 3.8-flash / 3.7-flash (flaky JSON or 0/4 PASS,
    # RPD 20). gemma-4-26b: 4/4 PASS ~0.8s, RPD 14.4k. flash-lites: RPD 500,
    # 250K TPM -> first for every kind (user directive).
    default_ladder = (
        'gemini-3.1-flash-lite,gemini-3.5-flash-lite,'   # lites: 500 RPD, 250K TPM
        'gemma-4-26b-a4b-it,gemma-4-31b-it,'             # 14.4k RPD workhorses
        'gemini-2.5-flash,'                              # smart fallback (old keys)
        'gemini-3.6-flash')                              # bench fill
    models = [m.strip() for m in os.environ.get(
        'GEMMA_FALLBACK_MODELS', default_ladder).split(',') if m.strip()]
    # GEMMA_MODEL stays honored as an extra candidate (never reorders anything)
    primary = os.environ.get('GEMMA_MODEL', '').strip()
    if primary and primary not in models:
        models.append(primary)
    return models


class KeyPool:
    """Round-robin over one or more Google AI Studio keys (multi-project
    fallback). Rotates on 401/403/429/quota so one dead or exhausted key
    never stops the pipeline."""

    def __init__(self, raw):
        keys = []
        for chunk in re.split(r'[,;\s]+', (raw or '').strip()):
            k = chunk.strip()
            if k and k not in keys:
                keys.append(k)
        self.keys, self.idx = keys, 0

    def alive(self):
        return bool(self.keys)

    def current(self):
        return self.keys[self.idx % len(self.keys)] if self.keys else None

    def step(self):
        """Round-robin advance BEFORE each call (per-call key rotation so
        consecutive AI calls spread RPM across every key in the pool)."""
        if len(self.keys) > 1:
            self.idx = (self.idx + 1) % len(self.keys)
        return self.current()

    def rotate(self, why=''):
        if not self.keys:
            return None
        self.idx = (self.idx + 1) % len(self.keys)
        log(f"    [ai] -> key #{self.idx + 1}/{len(self.keys)} ({str(why)[:60]})")
        return self.current()


def load_key_pool():
    raw = os.environ.get('GEMMA_API_KEYS') or os.environ.get('GEMMA_API_KEY') or ''
    return KeyPool(raw)


def list_models(pool):
    if not HAVE_REQUESTS:
        sys.exit('requests missing')
    keys = pool.keys or ['YOUR_KEY']
    for i, k in enumerate(keys, 1):
        tag = f'key #{i} ({k[:10]}...)'
        try:
            r = requests.get(f'{GEMMA_BASE}/models', params={'key': k}, timeout=30)
            r.raise_for_status()
            print(f'{tag}: OK')
            for m in r.json().get('models', []):
                if 'gemma' in m['name'].lower() and \
                        'generateContent' in m.get('supportedGenerationMethods', []):
                    print(f"  {m['name']}   in={m.get('inputTokenLimit', '?')} "
                          f"out={m.get('outputTokenLimit', '?')}")
        except requests.RequestException as e:
            print(f'{tag}: FAILED {str(e)[:120]}')


def parse_ai_text(txt):
    """Tolerant JSON extraction: models sometimes emit trailing commentary or
    a second blob after the JSON object. Take the FIRST balanced JSON value."""
    txt = txt.strip()
    txt = re.sub(r'^```(json)?\s*', '', txt, flags=re.M)
    txt = re.sub(r'```\s*$', '', txt).strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(txt)
        return obj
    except json.JSONDecodeError:
        raise

class AIRouter:
    """Quota-aware model x key router (v5) -- replaces the old
    'always start at model #1' ladder.

    Strategy (hardcoded, from REAL free-tier quotas + model intelligence):
      * each CALL KIND has its own preference ladder:
          repair -> smartest first  (3.x-flash, 250K TPM, best reasoning)
          enrich -> volume first    (gemma 14.4k RPD workhorses, then 500-RPD lites)
          marks  -> cheapest first  (flash-lites)
      * PER-CALL ROUND-ROBIN: the ring start advances every call, so no single
        model (or key) ever serves two consecutive calls while siblings live;
      * HEALTH BENCHES: a combo that 429s (RPM -> 75s, RPD -> rest of run),
        overloads (500/503 -> 45s, 3 strikes -> 15 min), rejects the request
        (400 -> rest of run) or whose key is dead (401/403 -> key dropped) is
        SKIPPED until recovery instead of being retried every call. The walk
        order inside one call is: key-sibling first, then model-sibling.
    """

    # smartest-first / volume-first / cheapest-first per call kind
    KIND_ORDER = {
        # v5.6: flash-lites lead EVERY kind (user directive + probe: lites =
        # RPD 500 / 250K TPM; gemma-26b = 4/4 strict-JSON PASS, RPD 14.4k).
        'repair': ['gemini-3.1-flash-lite', 'gemini-3.5-flash-lite',
                   'gemma-4-26b-a4b-it', 'gemma-4-31b-it',
                   'gemini-2.5-flash', 'gemini-3.6-flash'],
        'enrich': ['gemini-3.1-flash-lite', 'gemini-3.5-flash-lite',
                   'gemma-4-26b-a4b-it', 'gemma-4-31b-it',
                   'gemini-2.5-flash', 'gemini-3.6-flash'],
        'marks':  ['gemini-3.1-flash-lite', 'gemini-3.5-flash-lite',
                   'gemma-4-26b-a4b-it', 'gemma-4-31b-it',
                   'gemini-2.5-flash', 'gemini-3.6-flash'],
    }
    BENCH_429_DAY = 24 * 3600     # RPD gone -> hard cap (until-reset wins)
    BENCH_429_MIN = 75            # RPM/TPM -> outlive the per-minute window
    BENCH_OVERLOAD = 45           # single 500/503 strike
    OVERLOAD_STRIKES = 3          # strikes before a long overload bench
    BENCH_OVERLOAD_LONG = 300     # 5 min after repeated overloads (v5.6)
    BENCH_BAD = 24 * 3600         # 400 reject -> not usable, rest of run

    def __init__(self, keys, models):
        self.models = [m for m in models if m]
        self.keys = [k for k in keys if k]
        self.combos = [(m, k) for m in self.models for k in self.keys]
        self.benched = {}          # (model, key) -> (until_epoch, reason)
        self.strikes = {}          # (model, key) -> consecutive 5xx count
        self.dead_keys = set()
        self.counter = 0           # round-robin cursor (advances every call)
        self.calls_ok = 0
        self._next_ok = 0.0        # global pacing timestamp

    def alive(self):
        return bool(self.combos)

    def _ring(self, kind):
        """All (model, key) combos of this kind, tier-ordered, filtered to
        healthy (not dead-key, not benched). Unknown/custom models go last."""
        order = self.KIND_ORDER.get(kind) or self.KIND_ORDER['enrich']
        known = sorted((c for c in self.combos if c[0] in order),
                       key=lambda c: order.index(c[0]))
        ring = known + [c for c in self.combos if c[0] not in order]
        now = time.time()
        return [c for c in ring
                if c[1] not in self.dead_keys
                and self.benched.get(c, (0, ''))[0] <= now]

    @staticmethod
    def _secs_to_reset(now=None):
        # Free-tier RPD resets ~midnight Pacific = 07:00 UTC (DST-safe margin
        # +5 min). v5.6: a 429-day combo sleeps until the reset, not 6h.
        t = time.gmtime(now if now is not None else time.time())
        secs = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
        return (7 * 3600 + 300 - secs) % 86400 or 86400

    @staticmethod
    def _quota_window(err):
        t = str(err).lower()
        if 'perday' in t or 'per_day' in t or 'requestsperday' in t:
            return 'day'
        return 'minute'

    @staticmethod
    def _suggested_wait(err):
        m = re.search(r'retry(?:-after|delay)[^0-9]{0,20}(\d+)', str(err).lower())
        return int(m.group(1)) if m else 0

    def _bench(self, model, key, secs, why):
        self.benched[(model, key)] = (time.time() + secs, why)
        log(f'    [ai] bench {model} k{self.keys.index(key) + 1} for {int(secs)}s ({why})')

    def _on_failure(self, model, key, err):
        low = str(err).lower()
        if '429' in low or 'quota' in low or 'exhausted' in low:
            if self._quota_window(err) == 'day':
                self._bench(model, key, self._secs_to_reset(),
                            f'RPD exhausted -> bench until 07:05 UTC reset '
                            f'({self._secs_to_reset() // 3600}h{(self._secs_to_reset() % 3600) // 60}m)')
            else:
                wait = self._suggested_wait(err) or self.BENCH_429_MIN
                self._bench(model, key, min(max(wait, self.BENCH_429_MIN), 1800),
                            'RPM/TPM window')
        elif any(t in low for t in ('401', '403', 'api key', 'unregistered',
                                    'permission')):
            self.dead_keys.add(key)
            log(f'    [ai] key #{self.keys.index(key) + 1} DEAD ({str(err)[:70]}) '
                f'-> dropped for the rest of the run')
        elif '400' in low:
            self._bench(model, key, self.BENCH_BAD, 'model rejects request')
        elif ('404' in low or 'not found' in low or
              'no longer available' in low):
            # model gone for this key/project (e.g. 2.5-flash on new keys)
            self._bench(model, key, self.BENCH_BAD, 'model unavailable (404)')
        else:                       # 500/503/overload/timeout/network
            s = self.strikes.get((model, key), 0) + 1
            self.strikes[(model, key)] = s
            if s >= self.OVERLOAD_STRIKES:
                self._bench(model, key, self.BENCH_OVERLOAD_LONG,
                            f'{s}x overload strikes')
            else:
                wait = self._suggested_wait(err) or self.BENCH_OVERLOAD
                self._bench(model, key, wait, f'overload strike {s}')

    def _pace(self, min_interval):
        wait = self._next_ok - time.time()
        if wait > 0:
            time.sleep(wait)
        self._next_ok = time.time() + min_interval

    def _attempt(self, model, key, prompt, schema):
        """One combo: SDK structured output first, then REST JSON-mime.
        Returns (dict_result, None) or (None, error_string)."""
        if HAVE_GENAI:
            try:
                opts = None
                try:                       # hard 45s ceiling on slow 5xx models
                    opts = gtypes.HttpOptions(timeout=45000)
                except Exception:
                    opts = None
                client = (ggenai.Client(api_key=key, http_options=opts)
                          if opts is not None else ggenai.Client(api_key=key))
                cfg = gtypes.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type='application/json',
                    response_schema=schema,
                )
                resp = client.models.generate_content(model=model,
                                                      contents=prompt, config=cfg)
                v = parse_ai_text(resp.text)
                if isinstance(v, dict):
                    return v, None
                return None, 'AI returned non-object JSON (SDK)'
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                fatal = any(t in low for t in ('429', 'quota', 'exhausted',
                                               '401', '403', 'api key',
                                               'unregistered', 'permission'))
                retryable = any(t in low for t in ('500', '503', 'unavailable',
                                                   'overload', 'deadline',
                                                   'internal error'))
                if fatal or retryable:
                    return None, msg      # bench + ladder move; REST would burn the same dead combo
                # unknown SDK hiccup -> let REST try the same combo once
        try:
            body = {'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 8192,
                                         'responseMimeType': 'application/json'}}
            r = requests.post(f'{GEMMA_BASE}/models/{model}:generateContent',
                              params={'key': key}, json=body, timeout=45)
            if r.status_code == 400 and 'mime' in r.text.lower():
                body['generationConfig'].pop('responseMimeType', None)   # plain mode
                r = requests.post(f'{GEMMA_BASE}/models/{model}:generateContent',
                                  params={'key': key}, json=body, timeout=45)
            if r.status_code == 200:
                try:
                    txt = r.json()['candidates'][0]['content']['parts'][0]['text']
                    v = parse_ai_text(txt)
                    if isinstance(v, dict):
                        return v, None
                    return None, 'AI returned non-object JSON (REST)'
                except (KeyError, IndexError, ValueError) as e:
                    return None, f'bad REST payload: {e}'
            return None, f'HTTP {r.status_code}: {r.text[:220]}'
        except requests.RequestException as e:
            return None, str(e)

    def call(self, prompt, schema, kind='enrich', min_interval=2.2, budget_s=110):
        """Walk this kind's hardcoded ladder (rotating start every call) until
        one healthy combo answers. Every failure benches its combo, so dead or
        quota-burned models are never retried call after call (the v4 bug).
        Returns a dict, or None if every healthy combo failed.
        v5.1: the WHOLE call (including any all-benched recovery wait) runs
        under one deadline - a sick ladder can add at most budget_s latency."""
        if not self.combos:
            return None
        deadline = time.time() + budget_s
        healthy = self._ring(kind)
        if not healthy:
            if self.benched:
                soonest = min(u for u, _ in self.benched.values())
                remaining = max(deadline - time.time(), 0)
                wait = min(max(soonest - time.time(), 1), 90, remaining)
                if wait < 1:
                    return None
                log(f'    [ai] all {len(self.combos)} combos benched -> '
                    f'wait {wait:.0f}s for nearest recovery')
                time.sleep(wait)
                healthy = self._ring(kind)
            if not healthy or time.time() >= deadline:
                return None
        n = len(healthy)
        start = self.counter % n
        self.counter += 1
        tried = 0
        last_err = 'no attempt made'
        for off in range(n):
            if time.time() > deadline or tried >= 12:
                break
            model, key = healthy[(start + off) % n]
            self._pace(min_interval)
            tried += 1
            t0 = time.time()
            res, err = self._attempt(model, key, prompt, schema)
            dt = time.time() - t0
            if res is not None:
                self.strikes.pop((model, key), None)
                self.calls_ok += 1
                log(f'    [ai] {kind} c#{self.calls_ok} -> {model} '
                    f'k{self.keys.index(key) + 1} OK {dt:.1f}s')
                return res
            last_err = err or 'unknown'
            self._on_failure(model, key, last_err)
        log(f'    [ai] call gave up after {tried} healthy combos ({kind}): '
            f'{str(last_err)[:110]}')
        return None


def ai_json(router, prompt, schema, kind='enrich'):
    """Structured-JSON AI call through the quota-aware router.
    Anti-hallucination: caller re-validates output before trusting it."""
    if router is None or not router.alive():
        return None
    return router.call(prompt, schema, kind)


def gemma_repair(text, router):
    """One paper -> strict JSON questions (Tier 2 repair, smartest models first)."""
    prompt = f"""You are given text extracted from an AKTU university exam paper. Some text is noisy (stray digits/letters from watermarks, broken words). Hindi lines were already removed on purpose - ignore any remaining Hindi.
Reconstruct the list of exam QUESTIONS as strict JSON only, no markdown, schema:
{{"questions":[{{"label":"<as printed: 1, 2, a, b...>","text":"<clean English question text, broken words repaired, meaning unchanged>","marks":<int or null>,"has_diagram":<bool>}}]}}
Rules: marks only when printed or clearly inferable (AKTU sections use 2/7/10/15); NEVER invent marks or questions; do not translate anything; do not merge OR-alternatives.
PAPER TEXT:
{text[:26000]}"""
    return ai_json(router, prompt, REPAIR_SCHEMA, kind='repair')

def validate_ai_result(ai_json, parsed):
    """AI output must pass the SAME checks or it goes to review (anti-hallucination)."""
    if not isinstance(ai_json, dict) or 'questions' not in ai_json:
        return None                    # covers None AND list-shaped AI output
    qs = []
    for q in ai_json['questions'][:60]:
        if not isinstance(q, dict):
            continue
        t = re.sub(r'\s+', ' ', str(q.get('text', ''))).strip()
        if len(t) < 15:
            continue
        marks = q.get('marks')
        if not isinstance(marks, int) or marks not in VALID_MARKS:
            marks = None
        qs.append({'label': str(q.get('label') or '')[:6], 'text': t, 'marks': marks,
                   'section': None, 'unit': None, 'choice_group': None,
                   'has_diagram': bool(q.get('has_diagram')), 'page': None, 'top': None})
    n = len(qs)
    frac = sum(1 for q in qs if q['marks']) / n if n else 0
    if n < 5 or frac < 0.4:
        return None
    return qs

# ---------------------------------------------------------------------------
# Enrichment helpers (question_type / unit_topic) -- extract + enrich stages
# ---------------------------------------------------------------------------
NUM_HINT = re.compile(r'\b(calculate|compute|evaluate|find the value|determine the '
                      r'value|solve|deri[vg]e|how many|how much|convert|what is the '
                      r'(?:value|output|result|current|voltage|gain))\b', re.I)
MCQ_HINT = re.compile(r'(?:\([a-dA-D1-4]\)\s*\S+[^\n(]*){3,}')


def classify_qtype(text, has_diagram=False):
    """Deterministic question_type; never returns None or empty."""
    t = text or ''
    if MCQ_HINT.search(t):
        return 'mcq'
    if has_diagram:
        return 'diagram'
    if NUM_HINT.search(t):
        return 'numerical'
    if len(t) < 70:
        return 'short'
    return 'theory'


def sanitize_topic(t):
    """Force AI topic output into a safe 2-6 word label (fallback 'general')."""
    t = re.sub(r'[^A-Za-z0-9 /&+\-()]', '', str(t or '')).strip()
    t = re.sub(r'\s+', ' ', t)
    if len(t) < 3:
        return 'general'
    return t[:48].strip() or 'general'


def enrich_parsed_questions(router, parsed):
    """IN-PASS enrichment (v4): ONE AI call per paper BEFORE pushing, so every
    question leaves the extract loop with unit_topic / refined question_type /
    parent link / printed marks. Returns True ONLY if the AI answered; on
    failure NOTHING is stamped (no 'general' junk, no enriched_at) so the
    finalize enrich backfill retries it on a later run. Anti-hallucination:
    labels must match exactly, types validated, marks VALID_MARKS-gated and
    only filled when the parser found none."""
    qs = parsed.get('questions') or []
    if len(qs) < 3:
        return False
    items = []
    for i, q in enumerate(qs):
        items.append((str(q.get('label') or f'#{i + 1}'), q))
    by_label = {}
    for n, q in items:
        by_label.setdefault(n, []).append(q)
    listing = '\n'.join(f'{n}. {q["text"][:400]}' for n, q in items)
    m = parsed.get('meta', {})
    prompt = f"""You are tagging exam questions of ONE AKTU university paper (subject code: {m.get('code', '?')}, session: {m.get('year', '?')}).
For EACH question below return strict JSON:
{{"items":[{{"n":"<the question's number/label exactly as given>","topic":"<2-6 word AKTU syllabus topic>","type":"theory|numerical|short|mcq|diagram","parent":"<label of the question this is a sub-part of, or null>","marks":<integer marks or null>}}]}}
Rules: use the exact labels given; never invent labels; topic uses standard textbook terminology; numerical only if a computation is required; diagram only if a drawing/sketch is explicitly required; mcq only if answer options are printed; short if the expected answer fits in ~5 lines; otherwise theory. marks ONLY when printed in the text or clearly inferable, else null.
QUESTIONS:
{listing[:24000]}"""
    ai = ai_json(router, prompt, ENRICH_SCHEMA, kind='enrich')
    if not isinstance(ai, dict) or not isinstance(ai.get('items'), list):
        return False                   # covers None AND list-shaped AI output
    ok = False
    for it in ai['items']:
        if not isinstance(it, dict):
            continue
        n = str(it.get('n', '')).strip().rstrip('.):')
        targets = by_label.get(n)
        if not targets:
            continue
        ok = True
        topic = sanitize_topic(it.get('topic'))
        qtype = it.get('type')
        par = str(it.get('parent') or '').strip().rstrip('.):')
        mk = it.get('marks')
        for q in targets:
            if topic and topic != 'general':
                q['unit_topic'] = topic
            if qtype in QTYPES_ALLOWED:
                q['question_type'] = qtype
            if par and par in by_label:
                q['enrich_parent'] = par
            if isinstance(mk, int) and mk in VALID_MARKS and q.get('marks') is None:
                q['marks'] = mk
    return ok


def parse_vec(v):
    """PostgREST returns pgvector as a '[0.1,0.2,...]' string; accept both."""
    if v is None:
        return None
    if isinstance(v, str):
        return json.loads(v)
    return list(v)

# ---------------------------------------------------------------------------
# Diagram cropping
# ---------------------------------------------------------------------------
def crop_diagrams(pdf_path, parsed, outdir, file_hash, pages=None):
    """Crop ONLY questions whose band actually contains a figure.

    Figure detection (calibrated Sept-2026): embedded images, curve objects
    (arcs/circles/graphs) or DIAGONAL lines inside the question's band.
    Horizontal/vertical rects+lines are table borders - NOT figures.
    has_diagram stays the keyword flag ('asks to draw'); diagram_kind carries
    the real semantics ('figure' | 'asks'). Non-figure questions get NO crop
    (previously every flagged question got a useless text screenshot).
    """
    if not (HAVE_PDFIUM and PIL_OK):
        return 0
    if not parsed['meta'].get('is_text_layer', True):
        return 0          # OCR'd papers: line coords are pixels, not points (v2 feature)
    saved = 0
    doc = pdfium.PdfDocument(pdf_path)
    try:
        for idx, q in enumerate(parsed['questions']):
            if not q.get('has_diagram') or q.get('top') is None:
                continue
            try:
                pg_no = q.get('page')
                if not pg_no or (pages and pg_no > len(pages)) or pg_no > len(doc):
                    continue
                pg = pages[pg_no - 1] if pages else None
                nxt = None
                for q2 in parsed['questions'][idx + 1:]:
                    if q2.get('page') == pg_no and q2.get('top') is not None:
                        nxt = q2['top']; break
                bot = min(pg['h'] - 20, nxt) if (pg and nxt) else (nxt or (pg['h'] - 20 if pg else 160 + q['top']))
                band_h = bot - q['top']
                if not (30 <= band_h <= 700):
                    continue
                # ---- real-figure detection on the band ----
                n_img = n_curve = n_diag = 0
                if pg is not None:
                    W = pg['w']
                    for o in pg.get('drawings', []):
                        if o.get('bottom', 0) < q['top'] or o.get('top', 0) > bot:
                            continue
                        if o.get('width', 0) > 0.7 * W or \
                           (o.get('x1', 0) - o.get('x0', 0)) > 0.7 * W:
                            continue
                        if o.get('height', 0) > 0.5 * pg['h']:
                            continue
                        t = o.get('object_type', '')
                        if t == 'image':
                            if o.get('width', 0) >= 12 and o.get('height', 0) >= 12:
                                n_img += 1
                        elif t == 'curve':
                            n_curve += 1
                        elif t == 'line':
                            dy = abs(o.get('bottom', 0) - o.get('top', 0))
                            dx = o.get('x1', 0) - o.get('x0', 0)
                            if dy > 3 and dx > 3:
                                n_diag += 1
                is_figure = n_img >= 1 or n_curve >= 6 or n_diag >= 4
                q['diagram_kind'] = 'figure' if is_figure else 'asks'
                if not is_figure:
                    continue
                img = page_render(doc, pg_no)
                x0, x1 = 60, min(img.width - 30, int(0.92 * img.width))
                top_px = max(0, int(q['top'] * 2) - 6)
                bot_px = min(img.height, int(bot * 2))
                if bot_px - top_px < 40:
                    continue
                crop = img.crop((x0, top_px, x1, bot_px))
                g = crop.convert('L').resize((80, 60))
                if sum(1 for p in g.getdata() if p < 128) < 40:
                    continue
                fname = f"{file_hash[:12]}_q{idx}_{q['label']}.png"
                crop.save(os.path.join(outdir, fname))
                q['diagram_file'] = fname
                saved += 1
            except Exception:
                continue
    finally:
        doc.close()
    return saved


def page_render(doc, pg_no, scale=2.0):
    return doc[pg_no - 1].render(scale=scale).to_pil()

# ---------------------------------------------------------------------------
# Supabase push (PostgREST + Storage REST)
# ---------------------------------------------------------------------------
class Supabase:
    def __init__(self, url, key):
        if not HAVE_REQUESTS:
            sys.exit('requests missing for --db')
        self.url, self.key = url.rstrip('/'), key
        self.hdr = {'apikey': key, 'Authorization': f'Bearer {key}',
                    'Content-Type': 'application/json'}

    def _send(self, fn, *a, **kw):
        """Transient-failure shield for Supabase REST: 500 (incl. statement
        timeouts / load blips), 502/503/504 gateways and network resets are
        retried (4 attempts, 5s/15s/30s) instead of failing the whole paper.
        Non-5xx errors raise immediately."""
        last = None
        for attempt in range(4):
            try:
                r = fn(*a, **kw)
                if r.status_code in (500, 502, 503, 504) and attempt < 3:
                    wait = (5, 15, 30)[attempt]
                    log(f'    [db] HTTP {r.status_code} from Supabase, retry {attempt + 1}/3 in {wait}s')
                    time.sleep(wait)
                    continue
                return r
            except requests.RequestException as e:
                last = e
                if attempt < 3:
                    time.sleep(4 + 8 * attempt)
                    continue
        if last:
            raise last
        return r

    def upsert(self, table, rows, on_conflict, returns=True):
        h = dict(self.hdr)
        h['Prefer'] = ('resolution=merge-duplicates,return=representation' if returns
                       else 'resolution=merge-duplicates')
        r = self._send(lambda: requests.post(
            f'{self.url}/rest/v1/{table}', headers=h,
            params={'on_conflict': on_conflict}, data=json.dumps(rows), timeout=60))
        r.raise_for_status()
        return r.json() if returns else None

    def upload_diagram(self, bucket, path, file_path):
        with open(file_path, 'rb') as f:
            r = requests.post(f'{self.url}/storage/v1/object/{bucket}/{path}',
                              headers={'apikey': self.key,          # required for new sb_secret_* keys
                                       'Authorization': f'Bearer {self.key}',
                                       'x-upsert': 'true', 'Content-Type': 'image/png'},
                              data=f.read(), timeout=60)
            if r.status_code >= 300:
                log(f"    [storage] {path}: {r.status_code} {r.text[:80]}")
                return None
            return f'{self.url}/storage/v1/object/public/{bucket}/{path}'

    def get_paginated(self, table, select, filters=None, page=500, order=None):
        """GET all matching rows with offset pagination (pages capped at 1000).
        `order` (e.g. 'id') gives a stable ordering so offset pages never skip
        or repeat rows under concurrent writes."""
        rows, off = [], 0
        while True:
            p = {'select': select, 'limit': str(page), 'offset': str(off)}
            if order:
                p['order'] = order
            if filters:
                p.update(filters)
            r = self._send(lambda: requests.get(
                f'{self.url}/rest/v1/{table}', headers=self.hdr,
                params=p, timeout=90))
            r.raise_for_status()
            chunk = r.json()
            rows.extend(chunk)
            if len(chunk) < page:
                return rows
            off += page

    def patch(self, table, filters, payload):
        r = self._send(lambda: requests.patch(
            f'{self.url}/rest/v1/{table}', headers=self.hdr,
            params=filters, data=json.dumps(payload), timeout=60))
        r.raise_for_status()

    def insert(self, table, rows):
        r = self._send(lambda: requests.post(
            f'{self.url}/rest/v1/{table}', headers=self.hdr,
            data=json.dumps(rows), timeout=60))
        r.raise_for_status()

    def delete(self, table, filters):
        r = self._send(lambda: requests.delete(
            f'{self.url}/rest/v1/{table}', headers=self.hdr,
            params=filters, timeout=60))
        r.raise_for_status()

    def rpc(self, name, payload):
        r = self._send(lambda: requests.post(
            f'{self.url}/rest/v1/rpc/{name}', headers=self.hdr,
            data=json.dumps(payload), timeout=120))
        r.raise_for_status()
        return r.json()


class Embedder:
    """Lazy fastembed singleton shared by the per-paper pass (v4) and stage_embed.
    Local CPU, zero API cost, deterministic 384-dim vectors."""
    _model = None

    @classmethod
    def get(cls):
        if cls._model is None:
            from fastembed import TextEmbedding
            cls._model = TextEmbedding(EMBED_MODEL)
        return cls._model

    @classmethod
    def embed_pairs(cls, sb, pairs, batch=128):
        """pairs = [(question_id, text)] -> vectors written via set_embeddings RPC.
        Returns count written. Never raises for empty input."""
        if not sb or not pairs:
            return 0
        written = 0
        for i in range(0, len(pairs), batch):
            chunk = pairs[i:i + batch]
            vecs = list(cls.get().embed([(t or '')[:2000] for _, t in chunk]))
            vp = [{'id': qid, 'embedding': [round(float(x), 6) for x in v]}
                  for (qid, _), v in zip(chunk, vecs)]
            written += sb.rpc('set_embeddings', {'pairs': vp}) or 0
        return written


_ALIAS_CACHE = {}


def canonical_code(sb, code):
    """Map alias subject codes (dash variants, scheme duplicates) to their
    canonical code via subject_aliases. Self-heals: an unknown dash-variant of
    an existing canonical code is registered as an alias on first sight, so a
    re-ingested old filename can never re-split a merged subject."""
    if not sb or not code:
        return code
    if code in _ALIAS_CACHE:
        return _ALIAS_CACHE[code]
    mapped = code
    try:
        rows = sb.get_paginated('subject_aliases', select='canonical_code',
                                filters={'alias_code': f'eq.{code}'}) or []
        if rows:
            mapped = rows[0]['canonical_code']
        elif '-' in code:
            nodash = code.replace('-', '')
            hit = sb.get_paginated('subjects', select='code',
                                   filters={'code': f'eq.{nodash}'}) or []
            if hit:
                mapped = nodash
                try:
                    sb.insert('subject_aliases', [{'alias_code': code,
                                                   'canonical_code': nodash,
                                                   'basis': 'dash-variant'}])
                except Exception:
                    pass
    except Exception:
        pass
    _ALIAS_CACHE[code] = mapped
    return mapped


def source_from_url(url):
    """Provenance label from the paper's source URL (multi-source ready)."""
    u = (url or '').lower()
    if 'ryzenstudy' in u: return 'ryzenstudy'
    if 'aktuonline' in u: return 'aktuonline'
    if 'pooripadhai' in u: return 'pooripadhai'
    if 'lastmomenttuitions' in u: return 'lastmomenttuitions'
    if 'aktu.ac.in' in u: return 'aktu-official'
    return 'external' if u else 'unknown'


def push_paper(sb, fname, file_hash, parsed, mark_review, diagrams_dir=None,
               enrich_ok=False):
    """Upsert subject, paper, questions (+ topics/types), occurrences, parent
    links (+ diagram crops to Storage). Returns (paper_id, question_ids)."""
    meta = parsed['meta']
    code = canonical_code(sb, meta.get('code'))
    if not code:
        return None, []
    name = parsed.get('subject_name') or code
    sb.upsert('subjects', [{'code': code, 'name': name,
                            'course': meta.get('course', 'BTech'),
                            'semester': meta.get('semester'), 'is_active': True}], 'code')
    pr = sb.upsert('papers', [{'subject_code': code, 'year': meta.get('year'),
                               'file_hash': file_hash, 'source_url': meta.get('source_url'),
                               'storage_url': meta.get('source_url'),
                               'source': source_from_url(meta.get('source_url')),
                               'is_text_layer': meta.get('is_text_layer', True)}],
                   'file_hash')
    paper_id = pr[0]['id'] if pr else None
    if not paper_id:
        return None, []
    qids = []
    for q in parsed['questions']:
        if q.get('diagram_file'):
            local = os.path.join(diagrams_dir, q['diagram_file']) if diagrams_dir else q['diagram_file']
            q['diagram_url'] = sb.upload_diagram('question-diagrams', q['diagram_file'], local)
        else:
            q.setdefault('diagram_url', None)
        txt = clean_question_text(q['text'])
        norm = re.sub(r'[^\w\s]', '', txt.lower())
        norm = re.sub(r'\s+', ' ', norm).strip()
        qh = hashlib.sha256(norm.encode()).hexdigest()
        row = {'subject_code': code, 'text': txt, 'text_normalized': norm,
               'question_hash': qh, 'language': 'en',
               'question_type': q.get('question_type') or classify_qtype(q['text'], q.get('has_diagram', False)),
               'marks': q.get('marks'), 'unit': q.get('unit'),
               'unit_topic': q.get('unit_topic'),
               'choice_group': str(q['choice_group']) if q.get('choice_group') else None,
               'has_diagram': bool(q.get('has_diagram')),
               'diagram_kind': q.get('diagram_kind'),
               'diagram_url': q.get('diagram_url'),
               'extraction_confidence': parsed['confidence'],
               'needs_review': mark_review}
        qr = sb.upsert('questions', [row], 'subject_code,question_hash')
        qid = qr[0]['id'] if qr else None
        qids.append(qid)
        if qid:
            occ = {'question_id': qid, 'paper_id': paper_id, 'year': meta.get('year'),
                   'q_no': str(q.get('label') or ''), 'marks': q.get('marks'),
                   'extraction_confidence': parsed['confidence']}
            sb.upsert('occurrences', [occ], 'question_id,paper_id', returns=False)
    # parent links (second pass: sibling ids must exist first)
    label_map = {}
    for q, qid in zip(parsed['questions'], qids):
        lbl = str(q.get('label') or '')
        if qid and lbl:
            label_map.setdefault(lbl, qid)
    for q, qid in zip(parsed['questions'], qids):
        par = q.get('enrich_parent')
        if qid and par and par in label_map and label_map[par] != qid:
            try:
                sb.patch('questions', {'id': f'eq.{qid}'}, {'parent_id': label_map[par]})
            except Exception as e:
                log(f'    [db] parent patch failed: {str(e)[:70]}')
    patch = {'extraction_status': 'review' if mark_review else 'complete',
             'extracted_at': datetime.now(timezone.utc).isoformat(),
             'n_questions': len(parsed['questions']),
             'extraction_error': None}
    if enrich_ok:      # never stamp enriched_at when the AI tagging failed
        patch['enriched_at'] = datetime.now(timezone.utc).isoformat()
    try:
        sb.patch('papers', {'id': f'eq.{paper_id}'}, patch)
    except Exception as e:
        log(f'    [db] paper status patch failed: {str(e)[:80]}')
    return paper_id, qids

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_filename(fname):
    """Course__SemNN__year__CODE__slug.pdf"""
    base = os.path.splitext(os.path.basename(fname))[0]
    parts = base.split('__')
    out = {}
    if len(parts) >= 5:
        out['course'] = parts[0].upper().replace('BTECH', 'BTech').replace('BPHARM', 'BPharm')
        out['semester'] = int(re.sub(r'\D', '', parts[1]) or 0) or None
        out['year'] = parts[2]
        out['code'] = parts[3]
        out['subject_name'] = parts[4].replace('-', ' ').title()
    return out

def find_pdfs(root):
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            if f.lower().endswith('.pdf'):
                found.append(os.path.join(dirpath, f))
    return found

def load_pipeline_env():
    """Local-run convenience: fill missing env vars from aktu_pipeline.env (or
    .env) sitting next to this script. Real environment variables always win,
    so on GitHub Actions the repo Secrets take precedence automatically."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, 'aktu_pipeline.env'), os.path.join(here, '.env')):
        if not os.path.exists(cand):
            continue
        for line in open(cand, encoding='utf-8'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and not os.environ.get(k):
                os.environ[k] = v
        break

# ---------------------------------------------------------------------------
# Stage: extract
# ---------------------------------------------------------------------------
def load_manifest(path):
    """basename of unstructured_path -> {paper_url, paper_name}.
    Handles BOTH headered and HEADERLESS manifests (the scraper's file has
    no header; column order is fixed):
      course, semester, academic_year, paper_code, paper_name, paper_url,
      pdf_url, structured_path, unstructured_path, status, downloaded_at"""
    import csv
    mapping = {}
    with open(path, newline='', encoding='utf-8') as f:
        first = f.readline()
        f.seek(0)
        headered = first.lower().startswith('course')
        if headered:
            reader = csv.DictReader(f)
            rows = list(reader)
        else:
            rows = [dict(zip(['course', 'semester', 'academic_year', 'paper_code',
                              'paper_name', 'paper_url', 'pdf_url', 'structured_path',
                              'unstructured_path', 'status', 'downloaded_at'],
                             (c.strip() for c in next(csv.reader([line])))))
                    for line in f if line.strip()]
    for row in rows:
        p = (row.get('unstructured_path') or '').replace('\\', '/').split('/')[-1]
        if p:
            mapping[p.lower()] = {'url': row.get('paper_url') or '',
                                  'name': row.get('paper_name') or ''}
    return mapping


def stage_extract(args, sb, router):
    pdfs = find_pdfs(args.dir)
    if not pdfs:
        log(f'[!] WARNING: 0 PDFs found in "{args.dir}" -- check the pdf_dir input!')
    pdfs = [p for i, p in enumerate(pdfs) if i % args.shards == args.shard]
    if args.offset: pdfs = pdfs[args.offset:]

    manifest = load_manifest(args.manifest) if (args.manifest and os.path.exists(args.manifest)) else {}

    _cache = {}
    def get_hash(path):
        if path not in _cache:
            _cache[path] = sha256(path)
        return _cache[path]

    # DB-driven resume runs BEFORE --pilot/--limit slicing, so pilot N means
    # "next N UNFINISHED papers" (always does visible work) instead of
    # "first N files" (already complete after the first run -> silent no-op).
    # Papers already 'complete' in the DB are skipped even if the local state
    # file was deleted. Hash once, look up in chunks.
    n_done = 0
    if sb and pdfs:
        hlist = [get_hash(p) for p in pdfs]
        done = set()
        for i in range(0, len(hlist), 100):
            rows = sb.get_paginated('papers', select='file_hash,extraction_status',
                                    filters={'file_hash': 'in.(' + ','.join(hlist[i:i + 100]) + ')'},
                                    page=1000)
            done |= {r['file_hash'] for r in rows if r.get('extraction_status') == 'complete'}
        n_done = len(done)
        pdfs = [p for p in pdfs if get_hash(p) not in done]
        log(f'[i] resume: {n_done} already complete in DB -> skip')
    if args.limit: pdfs = pdfs[:args.limit]
    if args.pilot: pdfs = pdfs[:args.pilot]
    log(f'[i] {len(pdfs)} PDFs to process now (shard {args.shard}/{args.shards}'
        + (f', pilot={args.pilot}' if args.pilot else '')
        + (f', limit={args.limit}' if args.limit else '') + ')')

    state_path = os.path.join(args.out, 'extract_state.json')
    state = {}
    if os.path.exists(state_path):
        state = json.load(open(state_path))
    t0 = time.time()
    summary = []
    enriched_n = 0
    outbox = open(os.path.join(args.out, 'outbox.jsonl'), 'a', encoding='utf-8')

    for i, path in enumerate(pdfs):
        if (time.time() - t0) > args.soft_deadline * 60:
            log(f'[!] soft deadline reached at paper {i}; checkpoint and exit')
            break
        fname = os.path.basename(path)
        if state.get(fname, {}).get('status') in ('accepted', 'ai_repaired') or \
           (state.get(fname, {}).get('status') == 'review' and not args.ai):
            summary.append((fname, state[fname]['status'], state[fname].get('conf')))
            continue
        fhash = sha256(path)
        try:
            lines, meta0, stats, pages = tier0(path)
            fmap = parse_filename(path)
            meta = {**fmap, **{k: v for k, v in meta0.items() if v is not None}}
            meta.setdefault('is_text_layer', len(lines) > 30)
            if fname.lower() in manifest:
                m = manifest[fname.lower()]
                meta.setdefault('source_url', m['url'])
                meta.setdefault('subject_name', m['name'].title())
            parsed = parse_paper(lines, meta)
            parsed['subject_name'] = meta.get('subject_name') or fmap.get('subject_name')
            infer_diagram_flags(parsed, pages)
            conf = parsed['confidence']
            status, mark_review = ('accepted', False) if conf >= args.confidence else ('gate_fail', False)

            # Tier 2
            if status == 'gate_fail' and args.ai and router.alive():
                cache_f = os.path.join(args.out, 'ai_cache', fhash[:16] + '.json')
                if os.path.exists(cache_f):
                    ai = json.load(open(cache_f))
                else:
                    raw_text = '\n'.join(l['text'] for l in lines)
                    ai = gemma_repair(raw_text, router)
                    if ai:
                        json.dump(ai, open(cache_f, 'w'))
                qs = validate_ai_result(ai, parsed)
                if qs:
                    parsed['questions'] = qs
                    status, mark_review = 'ai_repaired', False
                else:
                    status, mark_review = 'review', True
            elif status == 'gate_fail':
                status, mark_review = 'review', True

            enrich_ok = False
            if args.ai and router.alive() and status in ('accepted', 'ai_repaired') \
                    and len(parsed['questions']) >= 3:
                enrich_ok = enrich_parsed_questions(router, parsed)
                if enrich_ok:
                    enriched_n += 1

            crops = 0
            if not args.no_crop:
                crops = crop_diagrams(path, parsed, os.path.join(args.out, 'diagrams'), fhash, pages=pages)

            json.dump(parsed, open(os.path.join(args.out, 'parsed',
                      fhash[:16] + '.json'), 'w'), ensure_ascii=False, default=str)
            for q in parsed['questions']:
                outbox.write(json.dumps({'file': fname, 'file_hash': fhash, 'status': status,
                                         'subject_code': meta.get('code'),
                                         'year': meta.get('year'),
                                         'label': q.get('label'), 'text': q['text'],
                                         'marks': q.get('marks'),
                                         'question_type': classify_qtype(q['text'], q.get('has_diagram', False)),
                                         'has_diagram': q.get('has_diagram', False),
                                         'needs_review': mark_review,
                                         'confidence': conf}, ensure_ascii=False) + '\n')
            if sb:
                pushed = push_paper(sb, fname, fhash, parsed, mark_review,
                                    diagrams_dir=os.path.join(args.out, 'diagrams'),
                                    enrich_ok=enrich_ok)
                if pushed and pushed[0]:
                    pairs = [(qid, q['text']) for qid, q in zip(pushed[1], parsed['questions']) if qid]
                    try:
                        emb = Embedder.embed_pairs(sb, pairs)
                        if emb:
                            log(f'      [vectors] {emb} embeddings written')
                    except Exception as e:
                        # embeddings are re-runnable (stage embed) - never fail the paper
                        log(f'      [vectors] failed (paper still complete): {str(e)[:80]}')
            state[fname] = {'status': status, 'conf': conf, 'hash': fhash,
                            'at': datetime.now(timezone.utc).isoformat()}
            summary.append((fname, status, conf))
            log(f'  [{i+1}/{len(pdfs)}] {status:12} conf={conf:.2f} q={len(parsed["questions"]):2} '
                f'hindi={stats["hindi"]:2} diagrams={crops} enrich={"ai" if enrich_ok else "-"}  {fname[:52]}')
        except Exception as e:
            state[fname] = {'status': 'failed', 'error': str(e)[:200]}
            summary.append((fname, 'failed', None))
            log(f'  [{i+1}/{len(pdfs)}] FAILED {type(e).__name__}: {str(e)[:90]} | {fname[:48]}')
            if sb:
                try:
                    sb.patch('papers', {'file_hash': f'eq.{fhash}'},
                             {'extraction_status': 'failed', 'extraction_error': str(e)[:200]})
                except Exception:
                    pass
        json.dump(state, open(state_path, 'w'))
    outbox.close()

    log('\n===== EXTRACT SUMMARY =====')
    from collections import Counter
    cnt = Counter(s for _, s, _ in summary)
    log(f'total={len(summary)} ' + ' '.join(f'{k}={v}' for k, v in cnt.items()))
    if not summary and n_done:
        log(f'[i] nothing to process: all {n_done} in-scope papers already complete in DB '
            '-> correct no-op; the next run automatically picks the next unfinished papers')
    acc = [c for _, s, c in summary if s in ('accepted', 'ai_repaired') and c is not None]
    if acc:
        log(f'mean confidence (accepted) = {sum(acc) / len(acc):.3f}')
    if enriched_n:
        log(f'in-pass enriched (topics written at extract time): {enriched_n} papers')

# ---------------------------------------------------------------------------
# Stage: enrich  (unit_topic + question_type + parent_id via Gemma, per paper)
# ---------------------------------------------------------------------------
def stage_enrich(args, sb, router):
    if not sb:
        sys.exit('enrich stage needs Supabase env (SUPABASE_URL/SERVICE_KEY)')
    if not router.alive():
        log('[!] no GEMMA key(s) configured -> enrich skipped')
        return
    t0 = time.time()
    papers = sb.get_paginated('papers', select='id,subject_code,year',
                              filters={'extraction_status': 'eq.complete',
                                       'enriched_at': 'is.null'})
    log(f'[i] enrich: {len(papers)} papers pending')
    patched_q = enriched_ok = 0
    for pi, p in enumerate(papers):
        if (time.time() - t0) > args.soft_deadline * 60:
            log(f'[!] soft deadline at enrich paper {pi}; DB-driven resume later')
            return
        try:
            occ = sb.get_paginated(
                'occurrences',
                select='q_no,question_id,questions(id,text,question_type,marks)',
                filters={'paper_id': f'eq.{p["id"]}'}, page=1000)
            items, seen = [], set()
            for o in occ:
                qq = o.get('questions')
                if not qq or qq['id'] in seen:
                    continue
                seen.add(qq['id'])
                items.append((str(o.get('q_no') or '?'), qq['id'], qq['text'] or '',
                              qq.get('marks')))
            if len(items) < 3:      # nothing worth an AI call
                sb.patch('papers', {'id': f'eq.{p["id"]}'},
                         {'enriched_at': datetime.now(timezone.utc).isoformat(),
                          'n_questions': len(items)})
                continue
            listing = '\n'.join(f'{n}. {t[:400]}' for n, _, t, _m in items)
            prompt = f"""You are tagging exam questions of ONE AKTU university paper (subject code: {p['subject_code']}, session: {p['year']}).
For EACH question below return strict JSON:
{{"items":[{{"n":"<the question's number/label exactly as given>","topic":"<2-6 word AKTU syllabus topic>","type":"theory|numerical|short|mcq|diagram","parent":"<label of the question this is a sub-part of, or null>"}}]}}
Rules: use the exact labels given; never invent labels; topic uses standard textbook terminology; numerical only if a computation is required; diagram only if a drawing/sketch is explicitly required; mcq only if answer options are printed; short if the expected answer fits in ~5 lines; otherwise theory.
QUESTIONS:
{listing[:24000]}"""
            ai = ai_json(router, prompt, ENRICH_SCHEMA, kind='enrich')
            if not isinstance(ai, dict) or not isinstance(ai.get('items'), list):
                # AI unavailable (quota/overload): write NOTHING and do NOT
                # stamp enriched_at -> the paper stays pending-tag and the
                # next run retries it. Never leave 'general'-junk topics.
                log(f'  [{pi + 1}/{len(papers)}] enrich SKIPPED (AI unavailable -> retry next run)  '
                    f'{p["subject_code"]} {p["year"]}')
                continue
            label_map = {n: qid for n, qid, _, _m in items}
            ok_items = {}
            if isinstance(ai, dict) and isinstance(ai.get('items'), list):
                for it in ai['items']:
                    if not isinstance(it, dict):
                        continue
                    n = str(it.get('n', '')).strip().rstrip('.):')
                    if n in label_map:
                        ok_items[n] = it
            for n, qid, text, cur_marks in items:
                it = ok_items.get(n, {})
                topic = sanitize_topic(it.get('topic'))
                qtype = it.get('type')
                if qtype not in QTYPES_ALLOWED:
                    qtype = classify_qtype(text)
                patch = {'unit_topic': topic, 'question_type': qtype}
                mk = it.get('marks')
                if isinstance(mk, int) and mk in VALID_MARKS and cur_marks is None:
                    patch['marks'] = mk      # backfill NULL marks only, never overwrite
                par = str(it.get('parent') or '').strip().rstrip('.):')
                if par and par in label_map and label_map[par] != qid:
                    patch['parent_id'] = label_map[par]
                sb.patch('questions', {'id': f'eq.{qid}'}, patch)
                patched_q += 1
            sb.patch('papers', {'id': f'eq.{p["id"]}'},
                     {'enriched_at': datetime.now(timezone.utc).isoformat(),
                      'n_questions': len(items)})
            enriched_ok += 1
            log(f'  [{pi + 1}/{len(papers)}] enriched {len(ok_items)}/{len(items)}  '
                f'{p["subject_code"]} {p["year"]}')
        except Exception as e:
            log(f'  [{pi + 1}/{len(papers)}] enrich FAILED {type(e).__name__}: {str(e)[:90]} '
                f'{p.get("subject_code")} {p.get("year")}')
    log(f'[i] enrich done: {enriched_ok} papers, {patched_q} questions tagged')


# ---------------------------------------------------------------------------
# Stage: repair  (AI backfill for questions with marks IS NULL)
# ---------------------------------------------------------------------------
MARKS_SCHEMA = {'type': 'object',
                'properties': {'items': {'type': 'array', 'items': {
                    'type': 'object',
                    'properties': {'id': {'type': 'string'},
                                   'marks': {'type': 'integer', 'nullable': True}},
                    'required': ['id']}}},
                'required': ['items']}


def stage_repair(args, sb, router):
    """Marks backfill for legacy rows: AI may only return marks that are
    printed or clearly inferable; values are VALID_MARKS-gated; NULLs only
    are filled; idempotent (rows with marks set are never touched)."""
    if not sb:
        sys.exit('repair stage needs Supabase env (SUPABASE_URL/SERVICE_KEY)')
    if not router.alive():
        log('[!] no GEMMA key(s) configured -> repair skipped')
        return
    rows = sb.get_paginated('questions', select='id,subject_code,text',
                            filters={'marks': 'is.null'}, page=500, order='id')
    log(f'[i] repair: {len(rows)} questions with marks IS NULL')
    if not rows:
        return
    t0 = time.time()
    fixed = 0
    for i in range(0, len(rows), 40):
        if (time.time() - t0) > args.soft_deadline * 60:
            log('[!] soft deadline at repair; rest self-heals next run')
            return
        chunk = rows[i:i + 40]
        listing = '\n'.join(f'{r["id"]}|{r["subject_code"]}|{r["text"][:220]}'
                            for r in chunk)
        prompt = f"""Each line below is one exam question as "id|subject|text". Marks conventions in AKTU papers: "2x7=14" means each of 7 parts carries 2 marks; a bare "(10)" or "10 marks" means 10; "5+5" means two 5-mark parts so the question totals 10.
Return strict JSON {{"items":[{{"id":"<same id>","marks":<integer or null>}}]}} covering EVERY id.
Rule: marks ONLY when printed in the text or clearly inferable; otherwise null. Never guess.
QUESTIONS:
{listing[:20000]}"""
        ai = ai_json(router, prompt, MARKS_SCHEMA, kind='marks')
        if not isinstance(ai, dict) or not isinstance(ai.get('items'), list):
            log(f'  batch {i // 40}: AI unavailable -> retry next run')
            continue
        by_id = {str(r['id']): r for r in chunk}
        n_batch = 0
        for it in ai['items']:
            if not isinstance(it, dict):
                continue
            r = by_id.get(str(it.get('id', '')).strip())
            mk = it.get('marks')
            if r and isinstance(mk, int) and mk in VALID_MARKS:
                sb.patch('questions', {'id': f'eq.{r["id"]}'}, {'marks': mk})
                try:
                    sb.patch('occurrences',
                             {'question_id': f'eq.{r["id"]}', 'marks': 'is.null'},
                             {'marks': mk})
                except Exception:
                    pass
                fixed += 1
                n_batch += 1
        log(f'  batch {i // 40}: +{n_batch} marks filled (total {fixed})')
    log(f'[i] repair done: {fixed} questions got marks')

# ---------------------------------------------------------------------------
# Stage: embed  (fastembed all-MiniLM-L6-v2, 384-dim, local CPU, 0 API cost)
# ---------------------------------------------------------------------------
def stage_embed(args, sb):
    if not sb:
        sys.exit('embed stage needs Supabase env (SUPABASE_URL/SERVICE_KEY)')
    try:
        Embedder.get()
    except ImportError:
        sys.exit('fastembed missing: pip install fastembed numpy')
    rows = sb.get_paginated('questions', select='id,text',
                            filters={'embedding': 'is.null'})
    log(f'[i] embed: {len(rows)} questions missing vectors')
    if not rows:
        return
    written = 0
    for i in range(0, len(rows), 128):
        chunk = rows[i:i + 128]
        written += Embedder.embed_pairs(sb, [(r['id'], r['text'] or '') for r in chunk])
        log(f'    ... {written} embedded')
    log(f'[i] embed done: {written} vectors written')

# ---------------------------------------------------------------------------
# Stage: cluster  (near-duplicate grouping across years -> frequency ranking)
# ---------------------------------------------------------------------------
def stage_cluster(args, sb, sim=CLUSTER_SIM):
    if not sb:
        sys.exit('cluster stage needs Supabase env (SUPABASE_URL/SERVICE_KEY)')
    import numpy as np
    # Cross-code clustering: the same subject lives under several codes across
    # AKTU scheme revisions (KCS051 ~ KCS-051 ~ BAS102/KAS102T ...). subject_aliases
    # maps alias_code -> canonical_code; clusters pool across the whole group and
    # are stored under the representative code (most papers, B-series preferred).
    alias = {}
    try:
        for r in sb.get_paginated('subject_aliases', select='alias_code,canonical_code'):
            alias[r['alias_code']] = r['canonical_code']
        if alias:
            log(f'[i] subject_aliases: {len(alias)} codes map into canonical groups')
    except Exception:
        log('[i] subject_aliases not present -> per-code clustering')

    def canon(c):
        return alias.get(c, c)

    subjects = sb.get_paginated('subjects', select='code,coverage_target')
    papers = sb.get_paginated('papers', select='id,subject_code,year')
    occ = sb.get_paginated('occurrences', select='question_id,paper_id,year,marks')
    occ_by_q = defaultdict(list)
    for o in occ:
        occ_by_q[o['question_id']].append(o)

    papers_per_code = defaultdict(int)
    for p in papers:
        papers_per_code[p['subject_code']] += 1
    group_codes = defaultdict(set)
    for p in papers:
        group_codes[canon(p['subject_code'])].add(p['subject_code'])
    for c in alias:
        group_codes[canon(c)].add(c)
    rep = {}
    for g, codes in group_codes.items():
        rep[g] = sorted(codes, key=lambda c: (-papers_per_code.get(c, 0),
                                              0 if c.startswith('B') else 1, c))[0]
    target_of = {s['code']: (s.get('coverage_target') or 8) for s in subjects}
    subject_years = defaultdict(set)
    for p in papers:
        subject_years[rep[canon(p['subject_code'])]].add(p['year'])

    qs = sb.get_paginated('questions',
                          select='id,subject_code,text,unit_topic,marks,embedding',
                          filters={'embedding': 'not.is.null'})
    groups = defaultdict(list)
    for q in qs:
        groups[canon(q['subject_code'])].append(q)
    log(f'[i] {len(qs)} embedded questions in {len(groups)} canonical groups')

    rows = []
    for g, members in groups.items():
        if len(members) < 2:
            continue
        M = np.array([parse_vec(q['embedding']) for q in members], dtype=np.float32)
        M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-12)
        S = M @ M.T
        n = len(members)
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i, j in zip(*np.where(np.triu(S, 1) >= sim)):
            pi, pj = find(int(i)), find(int(j))
            if pi != pj:
                parent[pi] = pj
        cl = defaultdict(list)
        for i in range(n):
            cl[find(i)].append(i)
        gcode = rep[g]
        yrs_sorted = sorted(subject_years.get(gcode, set()))
        yr_rank = {y: r + 1 for r, y in enumerate(yrs_sorted)}
        tgt = target_of.get(gcode, 8)
        for idxs in cl.values():
            if len(idxs) < 2:
                continue
            member_ids = [members[i]['id'] for i in idxs]
            years, marks = set(), []
            for qid in member_ids:
                for o in occ_by_q.get(qid, []):
                    if o.get('year'):
                        years.add(o['year'])
                    if o.get('marks'):
                        marks.append(o['marks'])
            freq = len(years)
            avg_m = (sum(marks) / len(marks)) if marks else 0.0
            recency = (max((yr_rank.get(y, 0) for y in years), default=0) /
                       max(1, len(yrs_sorted)))
            importance = round(45 * min(1.0, freq / max(1, tgt)) +
                               25 * min(1.0, avg_m / 15.0) + 30 * recency, 2)
            topics = defaultdict(int)
            for i in idxs:
                if members[i].get('unit_topic'):
                    topics[members[i]['unit_topic']] += 1
            if topics:
                label = max(topics.items(), key=lambda kv: kv[1])[0]
            else:
                label = min((members[i]['text'] for i in idxs), key=len)[:60]
            rows.append({'subject_code': gcode, 'label': label, 'member_ids': member_ids,
                         'freq_count': freq, 'importance': importance,
                         'method': f'minilm-cos{sim}-crosscode-unionfind',
                         'model_version': PROMPT_VERSION,
                         'last_scored_at': datetime.now(timezone.utc).isoformat()})
    sb.delete('clusters', {'id': 'not.is.null'})
    for k in range(0, len(rows), 500):
        sb.insert('clusters', rows[k:k + 500])
    total_clusters = len(rows)
    total_grouped = sum(len(r['member_ids']) for r in rows)
    log(f'[i] cluster done (cross-code): {total_clusters} clusters, {total_grouped} questions grouped')

# ---------------------------------------------------------------------------
# main dispatcher
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description='AKTU PYQ complete pipeline (v3)')
    ap.add_argument('--stage', choices=['extract', 'enrich', 'repair', 'embed', 'cluster', 'all'],
                    default='extract',
                    help='extract=ONE-PASS paper-wise (parse+AI repair+enrich+push+vectors) | '
                         'enrich=backfill topics for legacy papers | repair=backfill NULL marks | '
                         'embed=vectors for stragglers | cluster=near-dup groups | all')
    ap.add_argument('--dir', default='aktu-pyq/unstructured')
    ap.add_argument('--out', default='extraction_out')
    ap.add_argument('--manifest', default='',
                    help='ryzenstudy_download_manifest.csv -> fills source_url/storage_url/subject_name')
    ap.add_argument('--pilot', type=int, metavar='N', help='run on next N UNFINISHED PDFs (resume filter applies first)')
    ap.add_argument('--confidence', type=float, default=0.62)
    ap.add_argument('--ai', action='store_true', help='enable Tier-2 Gemma repair (extract stage)')
    ap.add_argument('--db', action='store_true', help='use Supabase (required for enrich/embed/cluster)')
    ap.add_argument('--no-crop', action='store_true', help='disable diagram PNG crops')
    ap.add_argument('--limit', type=int), ap.add_argument('--offset', type=int, default=0)
    ap.add_argument('--shard', type=int, default=0), ap.add_argument('--shards', type=int, default=1)
    ap.add_argument('--soft-deadline', type=int, default=320, help='minutes; exit cleanly before GHA 6h kill')
    ap.add_argument('--list-models', action='store_true')
    args = ap.parse_args()
    load_pipeline_env()

    pool = load_key_pool()
    models = load_models()
    router = AIRouter(pool.keys, models)
    if args.list_models:
        list_models(pool)
        print(f'AI ladder: {len(models)} models x {len(pool.keys)} keys = '
              f'{len(router.combos)} combos, kind-ordered rings')
        for kind, order in AIRouter.KIND_ORDER.items():
            print(f'  {kind:7s}: ' + ' > '.join(order))
        return

    stages = ['extract', 'enrich', 'repair', 'embed', 'cluster'] if args.stage == 'all' else [args.stage]
    need_db = args.db or args.stage in ('enrich', 'repair', 'embed', 'cluster', 'all')
    if need_db and not (os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_SERVICE_KEY')):
        sys.exit('this stage needs SUPABASE_URL + SUPABASE_SERVICE_KEY '
                 '(env vars or aktu_pipeline.env next to the script)')
    sb = Supabase(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY']) if need_db else None

    if 'extract' in stages:
        os.makedirs(args.out, exist_ok=True)
        os.makedirs(os.path.join(args.out, 'diagrams'), exist_ok=True)
        os.makedirs(os.path.join(args.out, 'ai_cache'), exist_ok=True)
        os.makedirs(os.path.join(args.out, 'parsed'), exist_ok=True)
        stage_extract(args, sb, router)
    if 'enrich' in stages:
        stage_enrich(args, sb, router)
    if 'repair' in stages:
        stage_repair(args, sb, router)
    if 'embed' in stages:
        stage_embed(args, sb)
    if 'cluster' in stages:
        stage_cluster(args, sb)

if __name__ == '__main__':
    main()
