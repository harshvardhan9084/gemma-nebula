#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aktu_pyq_extractor_v6.py  --  QUALITY-FIRST extraction (v6.0)
=============================================================
CEO order 2026-10-25: "make it great ... invest more on the OCR route ...
you could use more than 2 (3/6/10) API calls on a single paper ... put more
efforts and technology and intelligence so it produces directly great results
... down the stroking speed: a ~5h GHA run should spend ~2000 calls total."

WHY v5 WAS WEAK (measured on live RAW 2026-10-25):
  - Papers passing the Tier-1 confidence gate (>= 0.62) NEVER saw the AI, so
    parser recall was the ceiling: 813 of 9,093 papers have < 10 questions
    (340 have 1-4) and 192 have ZERO, because the fuzzy parser only recognises
    a few printed formats and the gate measures format quality, not recall.
  - The AI repair path was ONE call for the whole paper, truncated at 26,000
    chars, and validate_ai_result() required >= 40% printed marks - the
    "never invent marks" prompt on unmarked sections got whole papers dumped
    to review.
  - Two-column PDFs read line-by-line interleave the columns (the "scattered"
    text class: watermark leak, marks leak, sections leak). No vision route.

WHAT v6 DOES PER PAPER (3-8 AI calls, all trustable):
  1. tier0 text (+ tesseract OCR at 300 DPI when the text layer is unusable)
     and the Tier-1 fuzzy parse as a STRUCTURE HINT (never trusted alone).
  2. Chunked VISION EXTRACTION: 1-2 page chunks; every chunk is sent with its
     page text AND rendered page images so the model reads the real layout
     (columns, marks columns, tables) instead of flattened text. This is the
     OCR investment: the locked flash-lite models are the OCR.
  3. COMPLETENESS CHECK (1 text call): label outline + section map vs the
     extracted set -> likely-missing questions; a targeted vision call then
     re-reads only the suspect pages.
  4. Deterministic cleaner (watermarks, page furniture, section banners,
     instruction lines, marks moved into the marks field) + ONE AI clean pass
     only for questions that still trip leak signatures (diff-guarded).
  5. QC gate: every question scanned for leak signatures; papers with
     < qc-min questions or residual leaks are marked review - never silently
     accepted.
  6. Push: upsert (v5 semantics) or --reprocess paper-scoped REPLACE (old
     occurrences + orphaned questions backed up to out/replaced/ then
     deleted; identity/file_hash/source_url never re-pointed).
  7. NO embeddings written by default (E6b slim-swap discipline: DB quota);
     embed/cluster stay explicit separate dispatches.

RATE GOVERNOR (owner spec): every real API send is counted; --max-calls is a
hard per-process budget and --min-interval (default 9.0 s) paces sends so a
3-shard x 5h run lands at ~2,000 calls (~40/model/hour on the 2x5 lanes).
503 = transient (bench + rotate); 429 = daily lane gone (bench to reset);
the emergency gemma lane only serves when every lite lane is benched.

MERCURY LANE (owner directive 2026-11-05, "finish the whole corpus first"):
Inception Labs mercury-3 (diffusion dLLM, OpenAI-compatible, TEXT-ONLY,
~650-800 tok/s measured) serves every TEXT job: chunks whose text layer is
solid, the completeness check and the AI clean pass. Google flash-lite lanes
are then spent almost ONLY on what only they can do - reading scans. The
route decision stays per chunk: solid text + mercury on -> mercury; no/garbled
text or a 0-question mercury answer -> Google vision (never silently thin).
Same V6_EXTRACT_SCHEMA, same deterministic clean, same QC gates (qc-min etc.);
a mercury paper that trips QC still lands in review and is vision-healed by
the next run. Mercury calls are counted in their own budget (free tier per
key: 1,000 RPM / 1M in-TPM / 100K out-TPM; 2 keys = 200M free tokens) and do
NOT consume the --max-calls Google budget. reasoning_effort is forced
(instant by default): the medium default burns ~8K reasoning tokens and
returns zero questions (measured 2026-11-05).

RE-EXTRACT ANTI-LOOP LEDGER (owner directive 2026-11-06, "papers clear-zero"):
A real AKTU paper carries ~20 questions (incl. choice variants), so papers
below the bar are re-extracted -- but a paper that GENUINELY has fewer must
never loop forever, and nothing may lose questions it already has.
papers.extraction_error doubles as the per-paper attempt ledger:
  RETRY#N:afterq(prev beforeq)  attempt N done, DB now holds afterq (progress)
  RETRY-ACCEPT ...              terminal: QC-clean after the hard cap -> a
                                genuine short paper, accepted, never re-picked
  RETRY-FROZEN ...              terminal: still QC-failing -> human review
                                queue, never re-picked (review never ships)
HARD limit: MAX_REATTEMPTS = 2 re-extract attempts per paper, ever.
SOFT limit: a next attempt is only spent if the last one improved the paper
(more questions, or QC-clean). Terminal stamps are skipped by every future
reprocess selection -> no endless loop, bounded spend.
ZERO-LOSS guard: a re-extract that finds FEWER questions than the DB already
holds is pushed upsert-only (old occurrences kept, nothing deleted);
paper-scoped REPLACE (with backup) runs only when the new set is at least as
full as the old one.

RPD LAW (owner 2026-11-06): 6 keys x 500 RPD = 3,000 vision calls/day is the
hard daily ceiling for the Google flash-lite lanes; the dispatcher budgets
strictly under it (2,800) via its own cross-run ledger. There is no fixed
2,000-calls-per-run spec anymore -- the daily RPD cap is the only governor;
pacing just needs to stay clear of the 15 RPM/model/key per-minute window.
"""
import argparse, base64, hashlib, io, json, os, re, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aktu_pyq_extractor as v5
from aktu_pyq_extractor import (  # noqa: F401  (reused engine parts)
    AIRouter, Supabase, Embedder, load_key_pool, load_models, load_pipeline_env,
    tier0, parse_paper, clean_question_text, classify_qtype, infer_diagram_flags,
    crop_diagrams, parse_filename, fill_meta_identity, canonical_code,
    source_from_url, sha256, log, VALID_MARKS, parse_ai_text, soft_time_left,
)

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

try:
    from google import genai as ggenai
    from google.genai import types as gtypes
    HAVE_GENAI = True
except ImportError:
    HAVE_GENAI = False

try:
    import pypdfium2 as pdfium
    HAVE_PDFIUM = True
except ImportError:
    HAVE_PDFIUM = False

PIL_OK = True
try:
    from PIL import Image
except ImportError:
    PIL_OK = False

GEMMA_BASE = 'https://generativelanguage.googleapis.com/v1beta'
PROMPT_VERSION = 'v6-vision-qc+mercury+retryledger'

# --- Re-extract anti-loop ledger (owner directive 2026-11-06) ---------------
MAX_REATTEMPTS = 2          # HARD cap: re-extract attempts per paper, ever
RETRY_STAMP_RX = re.compile(r'RETRY#(\d+):(\d+)q\(prev (-?\d+)q\)')


def parse_retry_stamp(err):
    """papers.extraction_error -> (attempts_done, last_after_n, last_before_n).
    No stamp -> (0, None, None)."""
    m = RETRY_STAMP_RX.search(str(err or ''))
    if not m:
        return 0, None, None
    try:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except ValueError:
        return 0, None, None


def retry_terminal(n_eff, mark_review, attempts, old_n):
    """Decide the terminal stamp for a paper that missed the bar.
    Returns (stamp, status) or None = non-terminal (keep the RETRY#N stamp and
    allow one more attempt later). Terminal fires when the HARD cap is reached
    OR the SOFT limit says the last attempt did not improve the paper (no new
    questions AND still QC-failing)."""
    improved = (n_eff > old_n) or (not mark_review)
    if attempts < MAX_REATTEMPTS and improved:
        return None
    if mark_review:
        return (f'RETRY-FROZEN n={n_eff}q after {attempts} attempt(s); QC still '
                f'failing - human review queue (anti-loop terminal)', 'review')
    return (f'RETRY-ACCEPT n={n_eff}q genuine-short after {attempts} attempt(s); '
            f'owner bar ~20q, anti-loop terminal', 'complete')

# --- Inception Labs (mercury-3) lane ---------------------------------------
INCEPTION_BASE = 'https://api.inceptionlabs.ai/v1'
MERCURY_MODEL = os.environ.get('MERCURY_MODEL', 'mercury-3')
MERCURY_EFFORT = os.environ.get('MERCURY_EFFORT', 'instant')   # instant|low|medium|high


def load_mercury_keys():
    """MERCURY_API_KEYS: comma-separated Inception keys (sk_...). Values are
    never logged; only the COUNT is printed."""
    raw = os.environ.get('MERCURY_API_KEYS') or ''
    keys = []
    for chunk in re.split(r'[,;\s]+', raw.strip()):
        k = chunk.strip()
        if k.startswith('sk_') and k not in keys:
            keys.append(k)
    return keys

# ---------------------------------------------------------------------------
# Schemas (strict JSON, used by both SDK response_schema and prompt text)
# ---------------------------------------------------------------------------
V6_EXTRACT_SCHEMA = {
    'type': 'object',
    'properties': {
        'questions': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'label': {'type': 'string'},
                'text': {'type': 'string'},
                'marks': {'type': 'integer', 'nullable': True},
                'section': {'type': 'string', 'nullable': True},
                'unit': {'type': 'string', 'nullable': True},
                'topic': {'type': 'string', 'nullable': True},
                'qtype': {'type': 'string', 'nullable': True},
                'parent': {'type': 'string', 'nullable': True},
                'page': {'type': 'integer', 'nullable': True},
            },
            'required': ['label', 'text'],
        }},
    },
    'required': ['questions'],
}

V6_CHECK_SCHEMA = {
    'type': 'object',
    'properties': {
        'missing_labels': {'type': 'array', 'items': {'type': 'string'}},
        'suspect_pages': {'type': 'array', 'items': {'type': 'integer'}},
        'remove_labels': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['missing_labels'],
}

V6_CLEAN_SCHEMA = {
    'type': 'object',
    'properties': {
        'items': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'i': {'type': 'integer'},
                'text': {'type': 'string'},
            },
            'required': ['i', 'text'],
        }},
    },
    'required': ['items'],
}

# ---------------------------------------------------------------------------
# Leak signatures (shared with the sandbox stats pass - one source of truth)
# ---------------------------------------------------------------------------
SIG_WATERMARK = re.compile(
    r'(aktu\s*online|indexofpdf|downloaded\s+from|www\s*\.\s*\w|https?://\S+|'
    r't\.me/\S+|telegram|join\s*@[\w]+|scan(?:ned)?\s+by)', re.I)
SIG_MARKS = re.compile(
    r'([\[\(]\s*\d{1,2}\s*(?:x\s*\d{1,2})?\s*(?:m|marks?|mks?)?\s*[\]\)]\s*$|'
    r'\d{1,2}\s*marks?\s*[\]\)]?\s*$)', re.I)
SIG_SECTION = re.compile(r'^\s*(section\s*[-.:]?\s*[a-d1-4]\b|attempt\s+(any|all|the)|'
                         r'all\s+questions\s+are\s+compulsory|max(imum)?\.?\s*marks\s*[:=]|'
                         r'time\s*[:：]\s*\d)', re.I)
SIG_INSTR = re.compile(r'^\s*(answer|attempt)\b.{0,40}\b(question|section|following)\b', re.I)
SIG_JUNKSYM = re.compile(r'[|¦]{2,}|_{4,}|\.{6,}|={4,}')
SIG_CONTD = re.compile(r'contd\b|p\.?\s*t\.?\s*o\.?|printed\s+page', re.I)

ALL_SIGS = ('watermark', 'marks', 'section', 'instr', 'junk', 'contd')


def leak_flags(text):
    """Return the list of leak-signature names that hit this question text."""
    t = text or ''
    out = []
    if SIG_WATERMARK.search(t):
        out.append('watermark')
    if SIG_MARKS.search(t):
        out.append('marks')
    if SIG_SECTION.search(t):
        out.append('section')
    if SIG_INSTR.search(t):
        out.append('instr')
    if SIG_JUNKSYM.search(t):
        out.append('junk')
    if SIG_CONTD.search(t):
        out.append('contd')
    return out


# ---------------------------------------------------------------------------
# Deterministic cleaner (runs BEFORE the AI ever sees/pushes text)
# ---------------------------------------------------------------------------
WM_TOKEN_RX = re.compile(
    r'(https?://\S+|www\s*\.\s*[\w.-]+|aktu\s*[-\s]?online(\s*\.\s*com?)?|'
    r'indexofpdf|downloaded\s+from\s+\S+|t\.me/\S+|telegram[\w\s-]{0,12}|'
    r'join\s+@[\w]+|scan(?:ned)?\s+by\s+[\w-]+)', re.I)
CONTD_TOKEN_RX = re.compile(r'\bcontd\s*\.?\s*(\.{2,}|\(\d+\))?\s*$', re.I)
PTO_RX = re.compile(r'\bp\.?\s*t\.?\s*o\.?\s*$', re.I)
TRAIL_BRACKET_JUNK = re.compile(r'[\s.\-|]*\[[^\[\]]{0,18}\][\s.\-|]*Contd[\s.\-|]*$', re.I)
LEAD_SECT_GLUE = re.compile(
    r'^\s*(?:section\s*[-.:]?\s*[a-d1-4]\b[\s.,:-]*|(?:attempt|answer)\s+(?:any|all|the)?'
    r'[^.:]{0,40}(?:parts|questions|following)[^.:]{0,12}[:.]\s*|'
    r'(?:all\s+questions\s+are\s+compulsory[.:]?\s*)|'
    r'(?:max(imum)?\.?\s*marks\s*[:=]\s*\d{1,3}\s*|time\s*[:：]\s*[\d.:hm\s]+\s*))', re.I)
TRAIL_MARKS_COL = re.compile(r'[\s|,]*\(?(\d{1,2})\s*marks?\)?[\s|,.]*$', re.I)
TRAIL_2COL = re.compile(r'[\s|]+(\d{1,2})\s+(\d{1,2})\s*$')       # "10 3" marks+CO cols
MULTI_SPACE = re.compile(r'\s{2,}')
EDGE_JUNK = re.compile(r'^[\s.,;:|\-]+|[\s,;:|\-]+$')


def clean_v6(text, valid_marks=VALID_MARKS):
    """Deterministic scrub -> (clean_text, marks_from_text_or_None)."""
    # 22P05 guard: Postgres text cannot store NUL (\u0000) — strip at origin
    # (run #83: btech-ic-5-sem-industrial-instrumentation-eic-502-2015 400'd here)
    t = (text or '').replace('\x00', '')
    marks = None
    t = WM_TOKEN_RX.sub(' ', t)
    t = TRAIL_BRACKET_JUNK.sub(' ', t)
    t = CONTD_TOKEN_RX.sub(' ', t)
    t = PTO_RX.sub(' ', t)
    m = TRAIL_MARKS_COL.search(t)
    if m and int(m.group(1)) in valid_marks:
        marks = int(m.group(1))
        t = t[:m.start()]
    t = LEAD_SECT_GLUE.sub(' ', t)
    m2 = TRAIL_2COL.search(t)
    if m2 and int(m2.group(1)) in valid_marks and marks is None:
        marks = int(m2.group(1))
        t = t[:m2.start()]
    t = MULTI_SPACE.sub(' ', t).strip()
    t = EDGE_JUNK.sub('', t)
    return t, marks


def ai_clean_pass(router, flagged, min_interval, budget_s=150, mrouter=None):
    """One AI call for up to 25 leak-flagged question texts. Diff-guarded:
    a cleaned text is accepted only if it keeps >= 55% of the original length
    and >= 70% of its alphanumeric content (anti-hallucination).
    Text-only job: mercury lane first, Google fallback."""
    if not flagged:
        return {}
    items = [{'i': i, 'text': q['text'][:600]} for i, q in enumerate(flagged[:25])]
    listing = '\n'.join(f'{it["i"]}. {it["text"]}' for it in items)
    prompt = f"""You are cleaning text extracted from scanned AKTU university exam papers.
Each numbered text below contains one or more leaks: watermark/site names (aktuonline, www links, "Downloaded from"),
printed-marks fragments ("[10]", "2x7=14", "10 marks"), section/instruction lines ("Attempt any two parts...", "SECTION B", "Time: 3 Hours"),
page furniture ("Contd...", "P.T.O.", page numbers), or OCR column-splice garble.
Return strict JSON only: {{"items":[{{"i":<the same number>,"text":"<the SAME question, cleaned>">}}]}}
Rules: keep the question's meaning and every technical term EXACTLY; remove ONLY the leak fragments and repair obvious
OCR letter-splices (e.g. "fun~tion"->"function", "expla" split) when the intended word is certain; never translate,
never add content, never drop the actual question. If a text is hopeless garble with no recoverable question, return
its text unchanged.
TEXTS:
{listing[:22000]}"""
    ai, _err = (mrouter.call(prompt, V6_CLEAN_SCHEMA, kind='clean',
                             budget_s=budget_s)
                if mrouter is not None and mrouter.alive()
                and mrouter.budget_left() > 0 else (None, 'mercury off'))
    if ai is None:
        ai = router.call(prompt, V6_CLEAN_SCHEMA, kind='clean',
                         min_interval=min_interval, budget_s=budget_s)
    out = {}
    if isinstance(ai, dict) and isinstance(ai.get('items'), list):
        for it in ai['items']:
            if not isinstance(it, dict):
                continue
            try:
                i = int(it.get('i'))
            except (TypeError, ValueError):
                continue
            new = re.sub(r'\s+', ' ', str(it.get('text', ''))).strip()
            if not (0 <= i < len(flagged)) or len(new) < 12:
                continue
            old = flagged[i]['text']
            alnum_old = len(re.sub(r'[^A-Za-z0-9]', '', old)) or 1
            alnum_new = len(re.sub(r'[^A-Za-z0-9]', '', new))
            if len(new) >= 0.55 * len(old) and alnum_new >= 0.70 * alnum_old:
                out[i] = new
    return out


# ---------------------------------------------------------------------------
# Router v6: parts-aware attempts + hard call budget
# ---------------------------------------------------------------------------
class AIRouterV6(AIRouter):
    """v5 AIRouter (bench/rotate/quota windows untouched) + image parts +
    a hard --max-calls budget that counts EVERY real API send."""

    def __init__(self, keys, models, max_calls=0):
        super().__init__(keys, models)
        self.max_calls = int(max_calls or 0)          # 0 = unlimited
        self.budget_spent = 0

    def budget_left(self):
        if not self.max_calls:
            return 10 ** 9
        return max(0, self.max_calls - self.budget_spent)

    def _attempt(self, model, key, prompt, schema):
        """prompt: str OR list of genai parts. Counts every send."""
        if self.budget_left() <= 0:
            return None, 'call budget exhausted'
        self.budget_spent += 1
        parts = prompt if isinstance(prompt, list) else [{'text': prompt}]
        if HAVE_GENAI:
            try:
                opts = None
                try:
                    opts = gtypes.HttpOptions(timeout=90000)
                except Exception:
                    opts = None
                client = (ggenai.Client(api_key=key, http_options=opts)
                          if opts is not None else ggenai.Client(api_key=key))
                cfg = gtypes.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type='application/json',
                    response_schema=schema,
                )
                resp = client.models.generate_content(model=model, contents=parts, config=cfg)
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
                    return None, msg
        if not HAVE_REQUESTS:
            return None, 'no requests lib'
        try:
            body = {'contents': [{'parts': parts}],
                    'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 16384,
                                         'responseMimeType': 'application/json'}}
            r = requests.post(f'{GEMMA_BASE}/models/{model}:generateContent',
                              params={'key': key}, json=body, timeout=90)
            if r.status_code == 400 and 'mime' in r.text.lower():
                body['generationConfig'].pop('responseMimeType', None)
                r = requests.post(f'{GEMMA_BASE}/models/{model}:generateContent',
                                  params={'key': key}, json=body, timeout=90)
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


# ---------------------------------------------------------------------------
# MercuryRouter: Inception Labs mercury-3 text lane (OpenAI-compatible)
# ---------------------------------------------------------------------------
class MercuryRouter:
    """Text-only diffusion lane for solid text-layer chunks + text-only passes
    (completeness check, AI clean). Contract mirrors AIRouterV6._attempt:
    call(prompt_str, schema, kind, ...) -> (dict|None, err|None).
    Own budget + token metering, separate from the Google call budget.
    Free tier per key (docs 2026-11): 1,000 RPM / 1M in-TPM / 100K out-TPM."""

    # Owner 2026-11-07: keys throw random error codes "for nothing", any time,
    # any key -> do NOT bench on a single error. Count CONSECUTIVE errors per
    # key; after CONSEC_ERR_BENCH_N in a row, rest that key BENCH_REST seconds.
    # Single errors just rotate instantly to the next healthy key (round-robin).
    CONSEC_ERR_BENCH_N = 2  # bench after n consecutive errors on the same key
    BENCH_REST = 120        # rest duration (replaces old 90s/60s/3600s tiers)
    DEAD_401_403 = True     # auth errors are never "for nothing" -> key dropped for the run
    REJOIN_MAX_WAIT = 130   # all keys benched -> wait out nearest rejoin if <= this (> BENCH_REST)

    def __init__(self, keys, max_calls=0, min_interval=0.7):
        self.keys = [k for k in keys if k]
        self.idx = 0
        self.max_calls = int(max_calls or 0)      # 0 = unlimited
        self.budget_spent = 0
        self.min_interval = float(min_interval)
        self._last = 0.0
        self._bench = {}                          # key -> (until_epoch, why)
        self._consec = {}                         # key -> consecutive-error count
        self.tok_in = 0
        self.tok_out = 0
        self.calls_ok = 0

    def alive(self):
        return bool(self.keys)

    def budget_left(self):
        if not self.max_calls:
            return 10 ** 9
        return max(0, self.max_calls - self.budget_spent)

    def stats(self):
        return {'model': MERCURY_MODEL, 'effort': MERCURY_EFFORT,
                'keys': len(self.keys), 'calls': self.budget_spent,
                'calls_ok': self.calls_ok, 'tokens_in': self.tok_in,
                'tokens_out': self.tok_out}

    def _note_err(self, k, why):
        """Consecutive-error bench (owner 2026-11-07): count errors per key;
        at CONSEC_ERR_BENCH_N in a row rest the key BENCH_REST seconds, then it
        rejoins automatically. Any success resets the counter."""
        c = self._consec.get(k, 0) + 1
        self._consec[k] = c
        if c >= self.CONSEC_ERR_BENCH_N:
            self._bench[k] = (time.time() + self.BENCH_REST, f'{why} x{c}')
            self._consec[k] = 0
            log(f'[ai] mercury key rested {self.BENCH_REST}s after {c} consecutive errors ({why}); load moves to siblings')
        else:
            log(f'[ai] mercury key error #{c} ({why}); rotating to next key')

    def _pick(self):
        now = time.time()
        for _ in range(len(self.keys)):
            k = self.keys[self.idx % len(self.keys)]
            self.idx += 1
            until, _why = self._bench.get(k, (0, ''))
            if now >= until:
                return k
        return None

    def call(self, prompt, schema, kind='extract', min_interval=None, budget_s=120):
        """prompt: plain str (no image parts on this lane).
        Instant round-robin (owner 2026-11-06) + consecutive-error benching
        (owner 2026-11-07): ANY error from a key rotates to the next healthy
        key INSIDE the same call without benching; after CONSEC_ERR_BENCH_N
        consecutive errors the key rests BENCH_REST=120s and rejoins
        automatically. 401/403 keys are dead and dropped for the run."""
        if not HAVE_REQUESTS:
            return None, 'no requests lib'
        if self.budget_left() <= 0:
            return None, 'mercury call budget exhausted'
        wait = self.min_interval if min_interval is None else float(min_interval)
        max_tries = max(4, 2 * max(1, len(self.keys)))   # bounded sweeps
        tries = 0
        last_err = 'no live mercury keys'
        while tries < max_tries:
            if not self.keys:
                return None, f'no live mercury keys ({last_err})'
            k = self._pick()
            if not k:
                # all keys benched: wait out the nearest rejoin if it is near
                now = time.time()
                pending = [until - now for until, _w in self._bench.values()
                           if until > now]
                soonest = min(pending) if pending else 0.0
                if soonest <= self.REJOIN_MAX_WAIT and self.budget_left() > 0:
                    log(f'[mercury] all {len(self.keys)} keys benched; auto-rejoin '
                        f'in {soonest:.0f}s (load re-distributes across keys)')
                    time.sleep(min(soonest + 0.5, self.REJOIN_MAX_WAIT))
                    continue
                return None, f'all mercury keys benched ({last_err})'
            gap = time.time() - self._last
            if gap < wait:
                time.sleep(wait - gap)
            self.budget_spent += 1
            self._last = time.time()
            tries += 1
            body = {'model': MERCURY_MODEL,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.5,               # mercury-3 allows 0.5-1.0 only
                    'max_completion_tokens': 8192,
                    'response_format': {'type': 'json_schema',
                                        'json_schema': {'name': 'v6_' + kind,
                                                        'strict': True,
                                                        'schema': schema}},
                    'reasoning_effort': MERCURY_EFFORT}
            try:
                r = requests.post(f'{INCEPTION_BASE}/chat/completions',
                                  headers={'Authorization': 'Bearer ' + k,
                                           'Content-Type': 'application/json'},
                                  json=body, timeout=budget_s)
            except requests.RequestException as e:
                last_err = str(e)[:120]
                self._note_err(k, 'conn')             # count, rotate instantly
                continue
            if r.status_code == 200:
                try:
                    d = r.json()
                    u = d.get('usage') or {}
                    self.tok_in += int(u.get('prompt_tokens') or 0)
                    self.tok_out += int(u.get('completion_tokens') or 0)
                    txt = ((d.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
                    v = parse_ai_text(txt)
                except Exception as e:
                    last_err = f'bad mercury payload: {str(e)[:90]}'
                    self._note_err(k, 'payload')      # rotate: another key may answer
                    continue
                if isinstance(v, list):               # bare array despite the schema
                    v = {'questions': v}
                if isinstance(v, dict):
                    self._consec[k] = 0               # success resets the consecutive counter
                    self.calls_ok += 1
                    return v, None
                last_err = 'mercury returned non-object JSON'
                self._note_err(k, 'json')
                continue
            if r.status_code == 429:
                last_err = 'HTTP 429 (mercury rate limit)'
                self._note_err(k, '429')
                continue                              # instant rotate to next key
            if r.status_code in (500, 502, 503, 504, 529):
                last_err = f'HTTP {r.status_code}'
                self._note_err(k, f'{r.status_code}')
                continue                              # instant rotate to next key
            if r.status_code in (400, 401, 403):
                if r.status_code in (401, 403) and self.DEAD_401_403:
                    self.keys = [x for x in self.keys if x != k]  # dead key; never log value
                    self._consec.pop(k, None)
                else:
                    self._note_err(k, str(r.status_code))
                last_err = f'HTTP {r.status_code}: {r.text[:140]}'
                continue                              # instant rotate to next key
            last_err = f'HTTP {r.status_code}: {r.text[:140]}'
            self._note_err(k, str(r.status_code))
            continue
        return None, f'mercury exhausted {max_tries} rotations ({last_err})'


# ---------------------------------------------------------------------------
# Page images (the vision/OCR route)
# ---------------------------------------------------------------------------
def render_page_jpegs(pdf_path, page_nos, scale=2.0, max_width=1100, quality=72,
                      max_bytes=900_000):
    """Render pages -> genai inline_data parts (JPEG). Empty list if unable."""
    if not (HAVE_PDFIUM and PIL_OK):
        return []
    out = []
    try:
        doc = pdfium.PdfDocument(pdf_path)
        for pno in page_nos:
            if pno < 1 or pno > len(doc):
                continue
            img = doc[pno - 1].render(scale=scale).to_pil().convert('RGB')
            if img.width > max_width:
                img = img.resize((max_width, int(img.height * max_width / img.width)))
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=quality)
            data = buf.getvalue()
            q = quality
            while len(data) > max_bytes and q > 40:
                q -= 8
                buf = io.BytesIO()
                img.save(buf, 'JPEG', quality=q)
                data = buf.getvalue()
            out.append({'inline_data': {'mime_type': 'image/jpeg',
                                        'data': base64.b64encode(data).decode('ascii')}})
        doc.close()
    except Exception as e:
        log(f'    [vision] render failed ({str(e)[:80]}) -> text-only chunk')
        return []
    return out


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def page_texts_from(lines, pages):
    """page_no -> text. Uses tier0 `pages` geometry; falls back to the flat
    line list (the OCR path returns lines with page numbers)."""
    pt = {}
    for pg in pages or []:
        ls = pg.get('lines') or []
        if ls:
            txts = []
            for l in ls:
                if isinstance(l, dict):
                    txts.append(l['text'])
                else:                       # (top, bottom, text) tuples
                    txts.append(l[2])
            pt[pg.get('no')] = '\n'.join(txts)
    if pt:
        return pt
    by = defaultdict(list)
    for l in lines or []:
        by[l.get('page')].append(l['text'])
    return {k: '\n'.join(v) for k, v in sorted(by.items())}


def build_chunks(pt, chunk_pages=2, chunk_chars=7000):
    """Consecutive pages -> [{pages:[n...], text:'...'}] (small chunks keep
    each vision call focused; 1-2 pages is the sweet spot for exam papers)."""
    nos = sorted(pt)
    chunks, cur, cur_pages = [], [], []
    cur_chars = 0
    for n in nos:
        t = pt[n]
        if cur_pages and (len(cur_pages) >= chunk_pages
                          or cur_chars + len(t) > chunk_chars):
            chunks.append({'pages': cur_pages, 'text': '\n'.join(cur)})
            cur, cur_pages, cur_chars = [], [], 0
        cur.append(t)
        cur_pages.append(n)
        cur_chars += len(t)
    if cur_pages:
        chunks.append({'pages': cur_pages, 'text': '\n'.join(cur)})
    return chunks


# ---------------------------------------------------------------------------
# Per-paper extraction pipeline
# ---------------------------------------------------------------------------
EXTRACT_RULES = """You are extracting EVERY exam question from ONE chunk of an AKTU university
question paper. You receive the OCR/text-layer text of the page(s) AND page images.
The images are the source of truth when the text layer is garbled (two-column splices,
watermark digits, broken words) - read the images carefully.

Return strict JSON only: {"questions":[{"label":"<as printed: 1,2,3,a,b,c or Q1>","text":"<the question>",
"marks":<int or null>,"section":"<A/B/C or null>","unit":<int 1-9 or null>,
"topic":"<2-6 word syllabus topic or null>","qtype":"theory|numerical|short|mcq|diagram or null",
"parent":"<label of the parent question for sub-parts, else null>","page":<page no>}]}

Rules:
- Extract EVERY question and EVERY sub-part you can see (AKTU papers have 10-25+ questions;
  sub-parts like (a)/(b)/(c) are separate questions with parent set).
- Transcribe the question text FAITHFULLY and CLEANLY: keep the full technical meaning; EXCLUDE
  page furniture and leaks (watermark/site names like aktuonline, www links, "Downloaded from",
  "Contd...", "P.T.O.", page numbers, roll-number/instruction lines, section banners, printed
  marks strings like "[10]" / "2x7=14" / "10 marks" - put those marks in "marks" instead).
- If a mark value is printed for a question, set marks; otherwise null. NEVER invent marks.
- Do not merge OR-alternatives; each option is its own question.
- Ignore Hindi translations of questions (English text is canonical).
- Never invent questions; if the chunk is continuation text of a question started on an earlier
  page, still emit the question with its page and the text you can see.
- qtype: mcq only if options are printed; numerical only if a computation is required;
  diagram only if a drawing/sketch is explicitly required; short if the answer fits ~5 lines;
  else theory.
"""

# Text-only variant for the mercury lane (identical extraction contract; the
# model must do its own splice/watermark repair since it cannot see images).
_ex_cut = EXTRACT_RULES.find('Return strict JSON only:')
EXTRACT_RULES_TEXT = (
    'You are extracting EVERY exam question from ONE chunk of an AKTU university\n'
    'question paper. You receive ONLY the OCR/text-layer text of the page(s) - no images.\n'
    'The text layer is usually clean, but repair obvious two-column splices, watermark\n'
    'digits and broken words yourself, and NEVER copy watermarks or page furniture into\n'
    'the question text.\n'
    + EXTRACT_RULES[_ex_cut:]) if _ex_cut > 0 else EXTRACT_RULES


def extract_chunk(router, chunk, pdf_path, args, route, mrouter=None):
    """One chunk -> (ai, lane). Lane choice, quality-first:
      * solid text + mercury lane alive -> mercury-3 (t2t, text only);
      * mercury missing/off/0-questions, or thin text -> Google vision route
        (page images when renderable, else text-only REST/SDK).
    Returns lane in {'mercury', 'vision', 'text'} so the caller can report the
    honest route mix per paper."""
    text_ok = len((chunk.get('text') or '').strip()) >= 350
    if mrouter is not None and route in ('auto', 'text') and text_ok \
            and mrouter.alive() and mrouter.budget_left() > 0:
        prompt = (f'{EXTRACT_RULES_TEXT}\nPAGES {chunk["pages"][0]}-'
                  f'{chunk["pages"][-1]} TEXT:\n{chunk["text"]}')
        ai, _err = mrouter.call(prompt, V6_EXTRACT_SCHEMA, kind='extract',
                                budget_s=args.call_budget)
        if ai and (ai.get('questions') or []):
            return ai, 'mercury'
        # 0-questions / payload failure -> fall through to the vision lane
    parts = [{'text': f'{EXTRACT_RULES}\nPAGES {chunk["pages"][0]}-{chunk["pages"][-1]} TEXT:\n{chunk["text"]}'}]
    used_images = False
    if route in ('auto', 'vision', 'ocr'):
        imgs = render_page_jpegs(pdf_path, chunk['pages'])
        if imgs:
            parts.extend(imgs)
            used_images = True
    ai = router.call(parts, V6_EXTRACT_SCHEMA, kind='extract',
                     min_interval=args.min_interval, budget_s=args.call_budget)
    return ai, ('vision' if used_images else 'text')


def merge_questions(per_chunk):
    """Concatenate chunk results; drop cross-chunk dupes (same label+page or
    same normalized text)."""
    seen_text, seen_lp = set(), set()
    out = []
    for ch in per_chunk:
        for q in ch:
            t = re.sub(r'\s+', ' ', str(q.get('text', ''))).strip()
            if len(t) < 12:
                continue
            key_t = re.sub(r'[^\w]+', '', t.lower())[:120]
            lp = (str(q.get('label') or ''), q.get('page'))
            if key_t in seen_text or (lp[0] and lp in seen_lp):
                continue
            seen_text.add(key_t)
            if lp[0]:
                seen_lp.add(lp)
            q['text'] = t
            out.append(q)
    return out


def completeness_check(router, merged, hint, args, mrouter=None):
    """1 text call: compare extracted labels against the parsed outline +
    section map; return likely-missing labels and suspect pages.
    Text-only job: mercury lane first, Google fallback (same prompt)."""
    labels = [str(q.get('label') or '') for q in merged]
    hint_labels = [str(q.get('label') or '') for q in (hint.get('questions') or [])]
    sections = hint.get('checks', {}).get('sections') or []
    pages_n = max((q.get('page') or 0) for q in merged) if merged else 0
    prompt = f"""You audit the extraction of ONE AKTU exam paper. Below: (1) the question labels we extracted,
(2) the labels a regex parser found (may be incomplete), (3) the paper's sections, (4) page count.
A typical AKTU paper has 10-30 questions across its sections. Identify labels that are PROBABLY MISSING
from our extraction (e.g. gaps in the printed numbering, missing sub-parts a/b/c, whole sections with no
questions) and the pages where they likely are. Only report what the evidence suggests - never invent labels.
Return strict JSON: {{"missing_labels":["1","3b",...],"suspect_pages":[2,4],"remove_labels":["12"]}}
EXTRACTED LABELS: {json.dumps(labels[:80])}
PARSER OUTLINE: {json.dumps(hint_labels[:80])}
SECTIONS SEEN: {json.dumps(sections[:12])}
PAGES: {pages_n or 'unknown'}"""
    ai, _err = (mrouter.call(prompt, V6_CHECK_SCHEMA, kind='check',
                             budget_s=args.call_budget)
                if mrouter is not None and mrouter.alive()
                and mrouter.budget_left() > 0 else (None, 'mercury off'))
    if ai is None:
        ai = router.call(prompt, V6_CHECK_SCHEMA, kind='check',
                         min_interval=args.min_interval, budget_s=args.call_budget)
    if not isinstance(ai, dict):
        return [], [], set()
    missing = [str(x) for x in (ai.get('missing_labels') or []) if str(x).strip()][:12]
    suspect = []
    for p in (ai.get('suspect_pages') or []):
        try:
            suspect.append(int(p))
        except (TypeError, ValueError):
            pass
    remove = {str(x) for x in (ai.get('remove_labels') or [])}
    return missing, suspect, remove


def targeted_reextract(router, pdf_path, pages, missing, args):
    """One vision call on the suspect pages to pull the missing questions."""
    if not missing:
        return []
    pages = sorted(set(p for p in pages if p))
    if not pages:
        return []
    parts = [{'text': f'{EXTRACT_RULES}\nThis page range was audited: these labels are likely MISSING: '
                      f'{json.dumps(missing[:12])}. Re-read the pages and return ONLY the missing '
                      f'questions (label + text + marks). PAGES {pages[0]}-{pages[-1]}.'}]
    imgs = render_page_jpegs(pdf_path, pages)
    parts.extend(imgs)
    ai = router.call(parts, V6_EXTRACT_SCHEMA, kind='extract',
                     min_interval=args.min_interval, budget_s=args.call_budget)
    if not isinstance(ai, dict):
        return []
    return ai.get('questions') or []


def unit_int(v):
    """Coerce AI unit to DB int4: 'Unit-3'/'UNIT 3'/'3'/3 -> 3, junk -> None.
    questions.unit is integer; raw strings 400 the whole paper (run #52, 44 papers)."""
    if v is None:
        return None
    m = re.search(r'(\d{1,2})', str(v))
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 9 else None


def apply_ai_questions(raw_qs, meta_pages):
    """Normalize AI question dicts -> internal shape (label/text/marks/...)."""
    out = []
    for q in raw_qs or []:
        if not isinstance(q, dict):
            continue
        t = re.sub(r'\s+', ' ', str(q.get('text', ''))).strip()
        if len(t) < 12:
            continue
        marks = q.get('marks')
        if not isinstance(marks, int) or marks not in VALID_MARKS:
            marks = None
        page = None
        try:
            page = int(q.get('page')) if q.get('page') else None
        except (TypeError, ValueError):
            page = None
        if page is not None and meta_pages and page not in meta_pages:
            page = None
        lbl = str(q.get('label') or '').strip()[:8]
        # mercury-3 occasionally shuffles schema field names into 'label'
        # (e.g. label="qtype"); such values are junk, never printed labels.
        if lbl.lower() in {'qtype', 'theory', 'numerical', 'short', 'mcq', 'diagram',
                           'text', 'marks', 'section', 'unit', 'topic', 'parent',
                           'page', 'label', 'questions', 'null', 'none'}:
            lbl = ''
        out.append({'label': lbl, 'text': t, 'marks': marks,
                    'section': (str(q.get('section'))[:4] if q.get('section') else None),
                    'unit': unit_int(q.get('unit')),
                    'topic': q.get('topic'), 'page': page,
                    'parent': str(q.get('parent') or '').strip()[:8] or None,
                    'has_diagram': False})
    return out


def qc_paper(questions, hint, args):
    """Paper-level QC -> (flags, residual_leak_count)."""
    flags, leaks = [], 0
    n = len(questions)
    for q in questions:
        lf = leak_flags(q['text'])
        if lf:
            leaks += 1
            q['leaks'] = lf
    if n < args.qc_min:
        flags.append(f'low_recall<{args.qc_min}')
    if leaks:
        flags.append(f'leaks={leaks}')
    hint_n = len(hint.get('questions') or [])
    if hint_n and n and n < 0.5 * hint_n:
        flags.append(f'below_parser_outline({n}<{hint_n})')
    return flags, leaks


# ---------------------------------------------------------------------------
# Push (upsert semantics + paper-scoped REPLACE for --reprocess)
# ---------------------------------------------------------------------------
def push_paper_v6(sb, fname, fhash, parsed, mark_review, diagrams_dir,
                  reprocess, outdir, used_ai, replace_ok=None,
                  keep_n=0, keep_status=None):
    """v6 push. Same DB semantics as v5.push_paper (upsert, zero identity
    churn) plus: --reprocess first BACKS UP + deletes the paper's old
    occurrences and the questions that would be orphaned.
    replace_ok=False (with --reprocess) = ZERO-LOSS mode: skip the delete,
    upsert on top of the existing rows; the papers patch then never regresses
    n_questions below keep_n and keeps keep_status='complete' standing."""
    do_replace = bool(reprocess) if replace_ok is None \
        else (bool(reprocess) and bool(replace_ok))
    meta = parsed['meta']
    # yearless scans (hunt5 nb-* notebook files have empty academic_year):
    # papers.year/occurrences.year now allow NULL (migration 2026-10-28) — never
    # fabricate a year; NULL = honestly unknown.
    yr = meta.get('year')
    if yr is not None:
        yr = str(yr).strip() or None
    code = v5.canonical_code(sb, meta.get('code'))
    if not code:
        # Identity recovery (zero-churn): the paper was selected for (re)
        # extraction, so a papers row may already exist for this file_hash
        # with the subject_code established at first ingest. Reuse it - never
        # re-point or re-derive identity from a lossy filename.
        try:
            rows = sb.get_paginated('papers', select='subject_code',
                                    filters={'file_hash': f'eq.{fhash}'}, page=1)
            if rows and rows[0].get('subject_code'):
                code = v5.canonical_code(sb, rows[0]['subject_code'])
                if code:
                    log(f'    [identity] subject_code recovered from existing paper row: {code}')
        except Exception:
            pass
    if not code:
        log(f'    [!] PUSH SKIP: no subject code for {str(fname)[:64]}')
        return None, []
    name = parsed.get('subject_name') or code
    # course_t enum guard + semester sanitize + non-fatal upsert (run #82):
    # meta['course']='AKTL' (aktl__ filename prefix) 400'd this upsert and the
    # whole paper died BEFORE papers/questions were written. A reference-table
    # side-write must never zero a paper's extraction again.
    _raw = str(meta.get('course') or '').strip()
    _vals = set(v5.COURSE_ENUM.values()) | {'Other'}
    _c = v5.COURSE_ENUM.get(_raw.upper(), _raw if _raw in _vals else 'Other')
    _sem = meta.get('semester')
    try:
        _sem = int(str(_sem)) if _sem is not None and str(_sem).strip() != '' else None
    except (TypeError, ValueError):
        _sem = None
    if _sem is not None and not (1 <= _sem <= 8):
        _sem = None                     # subjects_semester_check (1..8)
    try:
        sb.upsert('subjects', [{'code': code, 'name': name,
                                'course': _c, 'semester': _sem,
                                'is_active': True}], 'code')
    except Exception as e:
        log(f'    [db] subjects upsert failed (paper continues, code={code}): '
            f'{type(e).__name__} {str(e)[:110]}')
    try:
        pr = sb.upsert('papers', [{'subject_code': code, 'year': yr,
                                   'file_hash': fhash, 'source_url': meta.get('source_url'),
                                   'storage_url': meta.get('source_url'),
                                   'source': source_from_url(meta.get('source_url')),
                                   'is_text_layer': meta.get('is_text_layer', True)}],
                       'file_hash')
    except Exception as e:
        body = ''
        resp = getattr(e, 'response', None)
        if resp is not None:
            try: body = resp.text[:280]
            except Exception: pass
        log(f'    [db] papers upsert {type(e).__name__}: {str(e)[:90]} | body: {body}')
        raise
    paper_id = pr[0]['id'] if pr else None
    if not paper_id:
        return None, [], None

    if do_replace:
        try:
            old_occ = sb.get_paginated('occurrences', select='id,question_id,year,q_no,marks',
                                       filters={'paper_id': f'eq.{paper_id}'}, page=1000)
            if old_occ:
                qids = sorted({o['question_id'] for o in old_occ if o.get('question_id')})
                backup = {'file_hash': fhash, 'paper_id': paper_id,
                          'at': datetime.now(timezone.utc).isoformat(),
                          'occurrences': old_occ, 'questions': []}
                if qids:
                    idlist = ','.join(qids)
                    backup['questions'] = sb.get_paginated(
                        'questions', select='*',
                        filters={'id': f'in.({idlist})'}, page=1000)
                    # count occurrences OUTSIDE this paper -> orphans die
                    others = sb.get_paginated('occurrences', select='question_id,paper_id',
                                              filters={'question_id': f'in.({idlist})'}, page=1000)
                    cnt = Counter(o['question_id'] for o in others)
                    orphans = [q for q in qids if cnt.get(q, 0) <= 1]
                else:
                    orphans = []
                os.makedirs(os.path.join(outdir, 'replaced'), exist_ok=True)
                with open(os.path.join(outdir, 'replaced', fhash[:16] + '.json'), 'w',
                          encoding='utf-8') as f:
                    json.dump(backup, f, ensure_ascii=False)
                sb.delete('occurrences', {'paper_id': f'eq.{paper_id}'})
                if orphans:
                    sb.delete('questions', {'id': f'in.({",".join(orphans)})'})
                log(f'    [replace] backed up + removed {len(old_occ)} old occurrences, '
                    f'{len(orphans)} orphaned questions')
        except Exception as e:
            log(f'    [!] replace pre-clean failed ({str(e)[:90]}) -> upserting on top')

    qids = []
    for q in parsed['questions']:
        # 22P05 guard: strip NUL from every LLM-sourced string field before any
        # DB write (covers ai_clean_pass output, occ.q_no, diagram_kind).
        for _k in ('text', 'label', 'diagram_kind', 'topic'):
            _v = q.get(_k)
            if isinstance(_v, str) and '\x00' in _v:
                q[_k] = _v.replace('\x00', '')
        if q.get('diagram_file'):
            local = os.path.join(diagrams_dir, q['diagram_file']) if diagrams_dir else q['diagram_file']
            try:
                q['diagram_url'] = sb.upload_diagram('question-diagrams', q['diagram_file'], local)
            except Exception:
                q['diagram_url'] = None
        else:
            q.setdefault('diagram_url', None)
        txt, marks_from_text = clean_v6(q['text'])
        txt = clean_question_text(txt)          # v5's proven scrubber on top
        if marks_from_text and q.get('marks') is None:
            q['marks'] = marks_from_text
        norm = re.sub(r'[^\w\s]', '', txt.lower())
        norm = re.sub(r'\s+', ' ', norm).strip()
        qh = hashlib.sha256(norm.encode()).hexdigest()
        row = {'subject_code': code, 'text': txt, 'text_normalized': norm,
               'question_hash': qh, 'language': 'en',
               'question_type': q.get('question_type') or classify_qtype(q['text'], q.get('has_diagram', False)),
               'marks': q.get('marks'), 'unit': unit_int(q.get('unit')),
               'unit_topic': (sanitize_topic_v6(q.get('topic')) if q.get('topic') else None),
               'choice_group': None,
               'has_diagram': bool(q.get('has_diagram')),
               'diagram_kind': q.get('diagram_kind'),
               'diagram_url': q.get('diagram_url'),
               'extraction_confidence': parsed['confidence'],
               'needs_review': mark_review}
        try:
            qr = sb.upsert('questions', [row], 'subject_code,question_hash')
        except Exception as e:
            body = ''
            resp = getattr(e, 'response', None)
            if resp is not None:
                try: body = resp.text[:280]
                except Exception: pass
            log(f'    [db] questions upsert {type(e).__name__}: {str(e)[:90]} | body: {body}')
            raise
        qid = qr[0]['id'] if qr else None
        qids.append(qid)
        if qid:
            occ = {'question_id': qid, 'paper_id': paper_id, 'year': yr,
                   'q_no': str(q.get('label') or ''), 'marks': q.get('marks'),
                   'extraction_confidence': parsed['confidence']}
            try:
                sb.upsert('occurrences', [occ], 'question_id,paper_id', returns=False)
            except Exception as e:
                body = ''
                resp = getattr(e, 'response', None)
                if resp is not None:
                    try: body = resp.text[:280]
                    except Exception: pass
                log(f'    [db] occurrences upsert {type(e).__name__}: {str(e)[:90]} | body: {body}')
                raise

    # parent links
    label_map = {}
    for q, qid in zip(parsed['questions'], qids):
        lbl = str(q.get('label') or '')
        if qid and lbl:
            label_map.setdefault(lbl, qid)
    for q, qid in zip(parsed['questions'], qids):
        par = q.get('parent') or q.get('enrich_parent')
        if qid and par and par in label_map and label_map[par] != qid:
            try:
                sb.patch('questions', {'id': f'eq.{qid}'}, {'parent_id': label_map[par]})
            except Exception:
                pass

    n_final = len(parsed['questions'])
    if keep_n and n_final < int(keep_n):
        n_final = int(keep_n)          # zero-loss: never regress the stored count
    status = 'review' if mark_review else 'complete'
    if keep_status == 'complete' and not do_replace:
        status = 'complete'            # upsert-only on top of a complete paper
    patch = {'extraction_status': status,
             'extracted_at': datetime.now(timezone.utc).isoformat(),
             'n_questions': n_final,
             'extraction_error': None}
    if used_ai:
        patch['enriched_at'] = datetime.now(timezone.utc).isoformat()
    try:
        sb.patch('papers', {'id': f'eq.{paper_id}'}, patch)
    except Exception as e:
        log(f'    [db] paper patch failed: {str(e)[:80]}')
    return paper_id, qids, status


def sanitize_topic_v6(t):
    t = re.sub(r'[^A-Za-z0-9 /&+\-()]', '', str(t or '')).strip()
    t = re.sub(r'\s+', ' ', t)
    return (t[:48].strip() or 'general') if len(t) >= 3 else 'general'


# ---------------------------------------------------------------------------
# Stage: extract (v6)
# ---------------------------------------------------------------------------
def find_pdfs_multi(dirs):
    pdfs = []
    for d in [x for x in (dirs or '').split(',') if x.strip()]:
        for root, _dirs_, files in os.walk(d.strip()):
            for f in files:
                if f.lower().endswith('.pdf'):
                    pdfs.append(os.path.join(root, f))
    return sorted(pdfs)


def load_manifests_multi(paths):
    merged = {}
    for p in [x for x in (paths or '').split(',') if x.strip()]:
        for k, v in v5.load_manifest(p.strip()).items():
            merged.setdefault(k, v)
    return merged


def stage_extract_v6(args, sb, router):
    pdfs = find_pdfs_multi(args.dir)
    if not pdfs:
        log(f'[!] WARNING: 0 PDFs found in "{args.dir}" -- check the pdf_dir input!')
    pdfs = [p for i, p in enumerate(pdfs) if i % args.shards == args.shard]
    manifest = load_manifests_multi(args.manifest)
    log(f'[i] shard {args.shard}/{args.shards}: {len(pdfs)} PDFs in scope, '
        f'{len(manifest)} manifest rows loaded')

    # ---- DB-driven selection -------------------------------------------
    hcache = {}

    def get_hash(p):
        if p not in hcache:
            hcache[p] = sha256(p)
        return hcache[p]

    n_skipped_done = 0
    predb = {}                                  # file_hash -> stored subject_code
    paper_ctx = {}                              # path -> {status, n, err} (reprocess)
    if sb and pdfs and not args.reprocess:
        hlist = [get_hash(p) for p in pdfs]
        done = set()
        for i in range(0, len(hlist), 100):
            rows = sb.get_paginated('papers', select='file_hash,extraction_status,subject_code',
                                    filters={'file_hash': 'in.(' + ','.join(hlist[i:i + 100]) + ')'},
                                    page=1000)
            for r in rows:
                st = r.get('extraction_status')
                predb[r['file_hash']] = r.get('subject_code') or None
                if st == 'complete' or (st == 'review' and not args.ai):
                    done.add(r['file_hash'])
        n_skipped_done = len(done)
        pdfs = [p for p in pdfs if get_hash(p) not in done]
    elif sb and pdfs and args.reprocess:
        sel = []
        want = set(x.strip() for x in args.status_filter.split(',') if x.strip())
        hlist = [get_hash(p) for p in pdfs]
        for i in range(0, len(hlist), 100):
            chunk_hashes = hlist[i:i + 100]
            rows = sb.get_paginated('papers', select='file_hash,extraction_status,subject_code,n_questions,extraction_error',
                                    filters={'file_hash': 'in.(' + ','.join(chunk_hashes) + ')'},
                                    page=1000)
            stmap = {r['file_hash']: r.get('extraction_status') for r in rows}
            nqmap = {r['file_hash']: r.get('n_questions') for r in rows}
            errmap = {r['file_hash']: r.get('extraction_error') for r in rows}
            for r in rows:
                predb[r['file_hash']] = r.get('subject_code') or None
            for p, h in zip(pdfs[i:i + 100], chunk_hashes):
                st = stmap.get(h)
                if st is None or st in want:   # not ingested yet -> ingest; else status filter
                    # thin-paper completeness campaign (--thin-max N): papers already
                    # at/above the bar stay untouched (owner rule 2026-11-05: a real
                    # AKTU paper carries ~20 questions incl. choice variants; measured
                    # corpus median 21.0 / avg 20.3). null n_questions (failed/new)
                    # always kept. null-vs-0 trick: None or 0 -> 0 < N -> kept.
                    if (args.thin_max and st is not None
                            and (nqmap.get(h) or 0) >= args.thin_max):
                        continue
                    # anti-loop ledger (owner 2026-11-06): terminal stamps
                    # (RETRY-ACCEPT / RETRY-FROZEN) are never re-picked; the hard
                    # cap MAX_REATTEMPTS is enforced at write time - this skip is
                    # the belt-and-suspenders copy.
                    p_err = str(errmap.get(h) or '')
                    attempts, _la, _lb = parse_retry_stamp(p_err)
                    if 'RETRY-ACCEPT' in p_err or 'RETRY-FROZEN' in p_err \
                            or attempts >= MAX_REATTEMPTS:
                        continue
                    paper_ctx[p] = {'status': st, 'n': nqmap.get(h) or 0, 'err': p_err}
                    sel.append((p, st, nqmap.get(h)))
        # quality-first ordering: review (known-broken) -> failed -> complete-thin,
        # least questions first inside each class
        rank = {'review': 0, 'failed': 1, 'complete': 2}
        sel.sort(key=lambda t: (rank.get(t[1], 3),
                                t[2] if t[2] is not None else -1))
        pdfs = [p for p, _, _ in sel]
    if args.course:
        pdfs = [p for p in pdfs if args.course.lower() in p.lower()]
    if args.year:
        pdfs = [p for p in pdfs if args.year in os.path.basename(p)]
        log(f'[i] course/year filters -> {len(pdfs)} PDFs remain')
    if args.limit:
        pdfs = pdfs[:args.limit]
    if args.pilot:
        pdfs = pdfs[:args.pilot]
    log(f'[i] {len(pdfs)} PDFs to process now (skipped {n_skipped_done} done)'
        + (f', REPROCESS filter={args.status_filter}' if args.reprocess else ''))

    os.makedirs(args.out, exist_ok=True)
    for sub in ('parsed', 'diagrams', 'replaced'):
        os.makedirs(os.path.join(args.out, sub), exist_ok=True)
    state_path = os.path.join(args.out, 'extract_state_v6.json')
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    outbox = open(os.path.join(args.out, 'outbox_v6.jsonl'), 'a', encoding='utf-8')
    summary, budget_stops = [], 0
    retry_stats = Counter()                     # attempt / RETRY-ACCEPT / RETRY-FROZEN
    mrouter = getattr(router, 'mercury', None)
    mercury_on = bool(args.mercury != 'off' and mrouter is not None and mrouter.alive())
    if mercury_on:
        ms = mrouter.stats()
        log(f'[v6] mercury lane ON: {ms["model"]} x{ms["keys"]} keys '
            f'effort={ms["effort"]} min_interval={mrouter.min_interval}s')

    for i, path in enumerate(pdfs):
        if soft_time_left(args) <= 0:
            log(f'[!] soft deadline reached at paper {i}; checkpoint and exit')
            break
        if router.budget_left() <= 0 and not (mercury_on and mrouter.budget_left() > 0):
            budget_stops += 1
            if budget_stops == 1:
                log('[!] CALL BUDGET exhausted (Google + mercury) - no further AI '
                    'calls this run; remaining papers left for the next dispatch')
            if args.dry_run:
                continue
            # keep checkpointing progress but stop doing (AI) work
            break
        fname = os.path.basename(path)
        t_paper = time.time()
        fhash = get_hash(path)
        calls_before = router.budget_spent
        route_used, n_chunks = 'none', 0
        try:
            if args.dry_run:
                lines, meta0, stats, pages = tier0(path)
                pt = page_texts_from(lines, pages)
                chunks = build_chunks(pt, args.chunk_pages)
                hint = parse_paper(lines, {**parse_filename(path), **meta0})
                summary.append((fname, 'dry_run', None))
                log(f'  [{i + 1}/{len(pdfs)}] DRY pages={len(pt)} chunks={len(chunks)} '
                    f'hint_q={len(hint["questions"])} -> {fname[:48]}')
                continue

            lines, meta0, stats, pages = tier0(path)
            fmap = parse_filename(path)
            meta = {**fmap, **{k: v for k, v in meta0.items() if v is not None}}
            meta.setdefault('is_text_layer', bool(lines) and len(lines) > 30)
            if fname.lower() in manifest:
                m = manifest[fname.lower()]
                meta.setdefault('source_url', m['url'])
                meta.setdefault('subject_name', m['name'].title())
            fill_meta_identity(meta, fname, manifest)

            hint = parse_paper(lines, meta)
            pt = page_texts_from(lines, pages)
            meta_pages = set(pt)
            chunks = build_chunks(pt, args.chunk_pages)
            n_chunks = len(chunks)

            # identity gate (zero-churn): reuse the stored subject_code of an
            # already-ingested paper; never burn AI tokens on a file that no
            # code source (filename / PDF content / DB row) can identify.
            db_code = predb.get(fhash)
            if not meta.get('code') and db_code:
                meta['code'] = db_code
            if not meta.get('code') and fhash not in predb:
                summary.append((fname, 'identity_blocked', None))
                state[fname] = {'status': 'identity_blocked', 'hash': fhash,
                                'at': datetime.now(timezone.utc).isoformat()}
                log(f'  [{i + 1}/{len(pdfs)}] IDENTITY-BLOCKED (no code from '
                    f'filename/PDF/DB - owner decision needed on fallback) {fname[:44]}')
                continue

            # 1) chunked extraction (mercury text lane + Google vision lane)
            m_ok = mercury_on and mrouter.budget_left() > 0
            if not router.alive() and not m_ok:
                log('    [!] no AI keys - paper parked at review (never silent-accept)')
                per_chunk, used_ai = [], False
            else:
                per_chunk, used_ai = [], bool(router.alive() or m_ok)
                lanes = []
                for ch in chunks:
                    ai, lane = extract_chunk(router, ch, path, args, args.route,
                                             mrouter=mrouter if m_ok else None)
                    lanes.append(lane)
                    qs = apply_ai_questions((ai or {}).get('questions'), meta_pages) if ai else []
                    per_chunk.append(qs)
                    if router.budget_left() <= 0 and not (m_ok and mrouter.budget_left() > 0):
                        log('    [!] budget hit mid-paper - continuing with what we have')
                        break
                route_used = '+'.join(lanes) if lanes else 'none'
            merged = merge_questions(per_chunk)

            # 2) completeness audit + targeted re-extract
            remove_labels = set()
            if (router.alive() or m_ok) and merged and \
                    (router.budget_left() > 0 or (m_ok and mrouter.budget_left() > 0)):
                missing, suspect, remove_labels = completeness_check(
                    router, merged, hint, args, mrouter=mrouter if m_ok else None)
                if missing and router.budget_left() > 0:
                    extra = targeted_reextract(router, path, suspect or list(pt), missing, args)
                    for q in apply_ai_questions(extra, meta_pages):
                        merged.append(q)
                merged = merge_questions([merged])
            merged = [q for q in merged
                      if not (str(q.get('label') or '') in remove_labels)]

            # 3) deterministic clean + marks extraction
            for q in merged:
                txt, mk = clean_v6(q['text'])
                q['text'] = txt
                if mk and q.get('marks') is None:
                    q['marks'] = mk

            # 4) AI clean pass (only flagged questions, diff-guarded)
            if args.ai_clean and (router.alive() or m_ok) and \
                    (router.budget_left() > 0 or (m_ok and mrouter.budget_left() > 0)):
                flagged = [q for q in merged if leak_flags(q['text'])]
                for idx, new in ai_clean_pass(router, flagged, args.min_interval,
                                              mrouter=mrouter if m_ok else None).items():
                    flagged[idx]['text'] = new

            # 5) structure: topics/qtype/diagrams
            for q in merged:
                if q.get('qtype') not in ('theory', 'numerical', 'short', 'mcq', 'diagram'):
                    q['qtype'] = None
            infer_diagram_flags({'questions': merged}, [])
            # map parser-hint line geometry onto AI questions (label+page match)
            # so diagram crops keep working
            hint_by_lp = {}
            for hq in (hint.get('questions') or []):
                lp = (str(hq.get('label') or ''), hq.get('page'))
                if lp[0]:
                    hint_by_lp.setdefault(lp, hq)
            for q in merged:
                hq = hint_by_lp.get((str(q.get('label') or ''), q.get('page')))
                if hq and hq.get('lines'):
                    q['lines'] = hq['lines']
                    q['top'] = hq.get('top')
                else:
                    q['lines'] = []
                    q['top'] = None
            parsed = {'questions': merged, 'meta': meta, 'checks': hint.get('checks', {}),
                      'confidence': round(min(1.0, 0.5 + 0.5 * min(1.0, len(merged) / 20.0)), 3),
                      'notes': hint.get('notes', [])}

            # 6) QC gate
            flags, leaks = qc_paper(merged, hint, args)
            mark_review = bool(flags)

            # 7) push (upsert / paper-scoped replace / zero-loss upsert-only)
            status = 'review' if mark_review else 'complete'
            ctx = paper_ctx.get(path) or {}
            old_n = int(ctx.get('n') or 0)
            old_status = ctx.get('status')
            prev_attempts, _la, _lb = parse_retry_stamp(ctx.get('err'))
            n_new = len(merged)
            bar = args.thin_max or 0
            success = (not mark_review) and (bar == 0 or n_new >= bar)
            replace_ok = None
            keep_n, keep_status = 0, None
            if args.reprocess and ctx:
                # zero-loss guard: REPLACE only when the new set is at least as
                # full as what the DB already holds (review papers that came out
                # QC-clean are always taken - that is the heal).
                if old_status == 'failed':
                    replace_ok = True          # failed rows are partial junk (backed up)
                else:
                    replace_ok = (n_new >= old_n) or \
                                 (old_status == 'review' and not mark_review)
                if not replace_ok:
                    keep_n, keep_status = old_n, old_status
                    log(f'    [zero-loss] new set {n_new}q < stored {old_n}q '
                        f'-> upsert-only, old occurrences kept')
            if sb:
                _pid, _qids, status = push_paper_v6(
                    sb, fname, fhash, parsed, mark_review,
                    os.path.join(args.out, 'diagrams'), args.reprocess,
                    args.out, used_ai, replace_ok=replace_ok,
                    keep_n=keep_n, keep_status=keep_status)
                # anti-loop ledger stamp (owner 2026-11-06)
                if args.reprocess and ctx and _pid and not success:
                    attempts = prev_attempts + 1
                    n_eff = max(n_new, old_n) if not replace_ok else n_new
                    term = retry_terminal(n_eff, mark_review, attempts, old_n)
                    if term:
                        stamp, term_status = term
                        try:
                            sb.patch('papers', {'id': f'eq.{_pid}'},
                                     {'extraction_error': stamp,
                                      'extraction_status': term_status,
                                      'n_questions': n_eff})
                            status = term_status
                            retry_stats[stamp.split(' ')[0]] += 1
                            log(f'    [ledger] TERMINAL {stamp}')
                        except Exception as e:
                            log(f'    [db] ledger stamp failed: {str(e)[:80]}')
                    else:
                        stamp = f'RETRY#{attempts}:{n_eff}q(prev {old_n}q)'
                        try:
                            sb.patch('papers', {'id': f'eq.{_pid}'},
                                     {'extraction_error': stamp})
                            retry_stats['attempt'] += 1
                            log(f'    [ledger] {stamp} (attempt {attempts}/'
                                f'{MAX_REATTEMPTS}, may re-run)')
                        except Exception as e:
                            log(f'    [db] ledger stamp failed: {str(e)[:80]}')
                if not args.no_crop:
                    try:
                        crop_diagrams(path, parsed, os.path.join(args.out, 'diagrams'),
                                      fhash, pages=pages)
                    except Exception:
                        pass

            calls_paper = router.budget_spent - calls_before
            summary.append((fname, status, len(merged)))
            state[fname] = {'status': status, 'n_q': len(merged), 'hash': fhash,
                            'flags': flags, 'calls': calls_paper, 'route': route_used,
                            'at': datetime.now(timezone.utc).isoformat()}
            for q in merged:
                outbox.write(json.dumps({'file': fname, 'file_hash': fhash, 'status': status,
                                         'subject_code': meta.get('code'), 'year': meta.get('year'),
                                         'label': q.get('label'), 'text': q['text'],
                                         'marks': q.get('marks'), 'page': q.get('page'),
                                         'topic': q.get('topic'),
                                         'question_type': q.get('qtype') or classify_qtype(q['text'], q.get('has_diagram', False)),
                                         'has_diagram': q.get('has_diagram', False),
                                         'needs_review': mark_review, 'leaks': q.get('leaks'),
                                         'confidence': parsed['confidence'],
                                         'prompt': PROMPT_VERSION}, ensure_ascii=False) + '\n')
            log(f'  [{i + 1}/{len(pdfs)}] {status:8s} q={len(merged):2d} chunks={n_chunks} '
                f'route={route_used} calls={calls_paper} flags={flags or "-"} '
                f'{time.time() - t_paper:.0f}s {fname[:46]}')
        except Exception as e:
            summary.append((fname, 'failed', None))
            state[fname] = {'status': 'failed', 'error': str(e)[:200]}
            log(f'  [{i + 1}/{len(pdfs)}] FAILED {type(e).__name__}: {str(e)[:90]} | {fname[:48]}')
            if sb:
                try:
                    sb.patch('papers', {'file_hash': f'eq.{fhash}'},
                             {'extraction_status': 'failed', 'extraction_error': str(e)[:200]})
                except Exception:
                    pass
        json.dump(state, open(state_path, 'w'))
    outbox.close()

    log('\n===== V6 EXTRACT SUMMARY =====')
    # NOTE: no local `from collections import Counter` here — a function-level
    # import makes Counter local to the WHOLE function and crashes the
    # retry_stats = Counter() above with UnboundLocalError (run #81, all 6
    # shards dead at t+14s). Module-level import (line ~92) is authoritative.
    cnt = Counter(s for _, s, _ in summary)
    log(f'total={len(summary)} ' + ' '.join(f'{k}={v}' for k, v in cnt.items()))
    nq = [c for _, _, c in summary if isinstance(c, int)]
    if nq:
        nq.sort()
        log(f'questions/paper: min={nq[0]} p50={nq[len(nq) // 2]} max={nq[-1]} '
            f'avg={sum(nq) / len(nq):.1f} | <10q papers={sum(1 for x in nq if x < 10)}')
    log(f'AI calls spent this process: {router.budget_spent}'
        + (f' / {router.max_calls}' if router.max_calls else '')
        + f' | budget stops: {budget_stops}')
    if retry_stats:
        log('retry ledger: ' + ' '.join(f'{k}={v}' for k, v in sorted(retry_stats.items()))
            + f' (hard cap {MAX_REATTEMPTS} attempts/paper)')
    mstat = None
    if mercury_on:
        ms = mrouter.stats()
        mstat = dict(ms)
        log(f'mercury lane: calls={ms["calls"]} ok={ms["calls_ok"]} '
            f'tok_in={ms["tokens_in"]} tok_out={ms["tokens_out"]} '
            f'({ms["model"]}/{ms["effort"]}, {ms["keys"]} keys left)')
    with open(os.path.join(args.out, 'run_v6.json'), 'w') as f:
        json.dump({'prompt_version': PROMPT_VERSION, 'route': args.route,
                   'chunks': args.chunk_pages, 'qc_min': args.qc_min,
                   'calls_spent': router.budget_spent, 'max_calls': router.max_calls,
                   'mercury': mstat,
                   'papers': [{"file": a, "status": b, "n_q": c} for a, b, c in summary]},
                  f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser(description='AKTU PYQ extraction v6 (quality-first, vision, budgeted)')
    ap.add_argument('--dir', default='corpus', help='PDF dirs, comma-separated')
    ap.add_argument('--out', default='extraction_out')
    ap.add_argument('--manifest', default='', help='manifest CSVs, comma-separated')
    ap.add_argument('--pilot', type=int, metavar='N')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--ai', action='store_true', help='enable AI (vision extraction)')
    ap.add_argument('--db', action='store_true', help='write to Supabase')
    ap.add_argument('--no-crop', action='store_true', help='disable diagram PNG crops')
    ap.add_argument('--dry-run', action='store_true', help='tier0+chunk plan only, no AI, no writes')
    ap.add_argument('--reprocess', action='store_true',
                    help='paper-scoped REPLACE of already-extracted papers (backup + delete + re-insert)')
    ap.add_argument('--status-filter', default='complete,review,failed',
                    help='with --reprocess: DB statuses to re-extract')
    ap.add_argument('--min-interval', type=float, default=9.0,
                    help='seconds between AI call STARTS in this process (rate governor)')
    ap.add_argument('--max-calls', type=int, default=0,
                    help='hard per-process AI call budget (0 = unlimited). 2000/5h run spec: use ~total/shards')
    ap.add_argument('--call-budget', type=int, default=150, help='per-call deadline seconds')
    ap.add_argument('--route', choices=['auto', 'text', 'vision', 'ocr'], default='auto')
    ap.add_argument('--mercury', choices=['auto', 'off'], default='auto',
                    help='mercury-3 text lane (Inception Labs) for text-layer chunks '
                         '+ text-only passes; auto = on when MERCURY_API_KEYS present')
    ap.add_argument('--mercury-min-interval', type=float, default=0.7,
                    help='seconds between mercury calls in this process '
                         '(free tier allows 1,000 RPM/key)')
    ap.add_argument('--course', default='', help='substring filter on file path (e.g. BTech, BPharm)')
    ap.add_argument('--year', default='', help='substring filter on file path (e.g. 2022-23)')
    ap.add_argument('--chunk-pages', type=int, default=2)
    ap.add_argument('--qc-min', type=int, default=10, help='papers with fewer questions -> review')
    ap.add_argument('--thin-max', type=int, default=0,
                    help='with --reprocess: only papers with n_questions < N (0 = off). '
                         'Thin-paper completeness campaign (AKTU papers carry ~20 questions)')
    ap.add_argument('--no-ai-clean', dest='ai_clean', action='store_false',
                    help='disable the AI clean pass for leak-flagged questions')
    ap.add_argument('--shard', type=int, default=0)
    ap.add_argument('--shards', type=int, default=1)
    ap.add_argument('--soft-deadline', type=int, default=285, help='minutes')
    ap.add_argument('--list-models', action='store_true')
    args = ap.parse_args()
    load_pipeline_env()
    pool = load_key_pool()
    models = load_models()
    router = AIRouterV6(pool.keys, models, max_calls=args.max_calls)
    mkeys = load_mercury_keys() if args.mercury != 'off' else []
    if mkeys:
        router.mercury = MercuryRouter(mkeys, max_calls=0,
                                       min_interval=args.mercury_min_interval)
    log(f'[v6] models={models} keys={len(pool.keys)} combos={len(router.combos)} '
        f'min_interval={args.min_interval}s max_calls={args.max_calls or "inf"} '
        f'route={args.route} qc_min={args.qc_min} '
        + (f'mercury={MERCURY_MODEL}x{len(mkeys)}({MERCURY_EFFORT})' if mkeys else 'mercury=off'))
    if args.list_models:
        v5.list_models(pool)
        return
    need_db = args.db
    if need_db and not (os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_SERVICE_KEY')):
        sys.exit('v6 extract with --db needs SUPABASE_URL + SUPABASE_SERVICE_KEY')
    sb = Supabase(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY']) if need_db else None
    if args.dry_run:
        sb = None
    stage_extract_v6(args, sb, router)


if __name__ == '__main__':
    main()

