#!/usr/bin/env python3
"""GOD v7 BRIDGE - deterministic post-processing, ZERO AI calls.

codex lessons run-9/10 (2026-11-24), pipeline/codex_laws.json:
  - SHAPE POLICY: GOD stores container-level rows with sub-parts INLINE
    (19 rows for ea84defe) while the questions-table shape wants one row
    per sub-part (Gemini Hydrogen's 22 rows). The bridge SPLITS verified
    container rows deterministically - NOT a re-extraction call.
  - DEVANAGARI STRAY GLYPH: born-digital ryzenstudy text layers leak stray
    Devanagari glyphs (ea84defe row 1.c ended with a lone 'प'). The cleaner
    removes boundary-stray glyph runs and logs every removal; garbled
    segments (Latin-Ext/Greek chars inside Devanagari runs, e.g. the
    'मŐकोʿक' mojibake on bcs301-2024-25 4.b) are FLAGGED, never rewritten.
  - SECTION-TOTAL MARKS LEAK: models attribute the printed section-total
    arithmetic ('2 x 10 = 20' style headers) to container rows (marks
    20/30/14 outside the {2,7,10} law). They are FLAGGED +marks-off-law by
    god_extract.marks_qc. The DEMOTION to honest NULL is an AMENDMENT
    PROPOSAL awaiting owner review -> --demote-section-total, default OFF.

Every action returns a change log so the audit diff records what the
bridge did. Zero deletions, keep-never-rewrite honored: the bridge never
drops a row and never rewrites text content except logged stray-glyph
strips.

Standalone CLI (offline, read-only on god6_* or a JSON file):
    python3 pipeline/god_bridge.py --from-db [--stem SUBSTR] [--json OUT]
    python3 pipeline/god_bridge.py --from-file rows.json [--json OUT]
"""
import argparse, hashlib, json, os, re, sys

DEVA = '\u0900-\u097F'
DEVA_RE = re.compile(f'[{DEVA}]')
# Latin Extended-A/B, IPA Extensions, Greek, Latin Ext Additional, modifier
# letters - chars that must NEVER sit inside a Devanagari run (mojibake).
GARBLE_RE = re.compile('[\u00c0-\u024f\u0250-\u02af\u0370-\u03ff\u1e00-\u1eff\u02b0-\u02ff]')
# sub-part markers: 'a. ' / '(a) ' with letter a-l, not preceded by alnum
MARK_RE = re.compile(r'(?<![A-Za-z0-9])([a-l])\.\s+|\(([a-l])\)\s+')
BARE_LABEL_RE = re.compile(r'^\d{1,2}$')
# stray glyph: a Devanagari run of length 1..2 GLUED to ASCII on both
# sides (no space - a space-flanked 1-2 char run is a normal Hindi word
# like का/की/को and must never be touched), or dangling at the very end
# of the text after an ASCII sentence end
STRAY_TAIL_RE = re.compile(f'(?<=[{DEVA}])([{DEVA}]{{1,2}})?\\s*$')
STRAY_EMBED_RE = re.compile(
    "(?<=[A-Za-z0-9,;:()\\\"'?\\-.\\]])[\\u0900-\\u097F]{1,2}"
    "(?=[A-Za-z0-9(,\\.;:?\\\"'\\]])")
INSTRUCTION_RE = re.compile(
    r'attempt\s+(all|any|five|three|four|two|one)|^Q\b|answer\s+(all|any|three|two)',
    re.IGNORECASE)

MIN_SEGMENT = 30          # chars a split segment must carry to be real
MIN_CONTAINER_TEXT = 60   # a bare-number row shorter than this is never split


def log_changes(text, changes, kind, detail):
    changes.append({'kind': kind, 'detail': detail})
    return text


def clean_text(text):
    """Stray Devanagari glyph strips (logged) + garble detection (flag only).

    Rules (deterministic):
      - a Devanagari RUN of 1-2 chars dangling at the very end of the text,
        preceded by an ASCII sentence end, is a leaked glyph -> strip
        (ea84defe 1.c '...significance?\nप' case). A real Hindi ending is a
        run of >=3 chars or ends with Devanagari punctuation (।) and is
        never touched.
      - a Devanagari run of 1-2 chars embedded BETWEEN ASCII word/punct
        characters is a leaked glyph -> strip.
      - Latin-Ext/Greek/modifier chars inside a Devanagari context ->
        '+text-garbled' FLAG, no rewrite (keep-never-rewrite).
    Returns (cleaned_text, changes, garbled).
    """
    changes, t = [], text or ''
    # 1) dangling tail run of 1-2 chars after an ASCII line (the '1.c ends
    #    with प' case): strip only if the tail run is preceded by ASCII on
    #    the line and is <=2 chars
    m = re.search("([A-Za-z0-9,;:.?!)\\]\"']*)\\s*([\\u0900-\\u097F]{1,2})\\s*$", t)
    if m and m.group(2) and m.group(1):
        # only when the last ASCII-bearing line ends the text: the run is
        # a stray glyph, not a Hindi sentence (Hindi sentences are >=3 chars)
        t2 = t[:m.start(2)].rstrip()
        changes.append({'kind': 'strip-stray-tail',
                        'detail': f"removed trailing stray glyph {m.group(2)!r}"})
        t = t2
    # 2) embedded 1-2 char runs GLUED to ASCII (space-flanked runs are
    #    normal Hindi words - the space class is deliberately absent)
    def _sub(mo):
        changes.append({'kind': 'strip-stray-embedded',
                        'detail': f"removed embedded stray glyph {mo.group(0)!r} "
                                  f"at offset {mo.start()}"})
        return ''
    t = STRAY_EMBED_RE.sub(_sub, t)
    # 3) garble detection (FLAG only - never a rewrite, never a change
    #    entry: detection is per-pass and must not break idempotence)
    garbled = False
    if DEVA_RE.search(t) and GARBLE_RE.search(t):
        # garble chars must sit within a Devanagari neighborhood (<=3 chars
        # from a Devanagari char) - a stray Latin char in pure English text
        # is normal transcription, not mojibake
        for gm in GARBLE_RE.finditer(t):
            lo, hi = max(0, gm.start() - 3), min(len(t), gm.end() + 3)
            if DEVA_RE.search(t[lo:hi]):
                garbled = True
                break
    return t, changes, garbled


def marker_chain(text):
    """Longest monotonic a->b->c marker chain in the text.

    Returns [(letter, start_offset)] or []. The chain must start at 'a',
    letters must advance exactly +1, positions strictly increasing, and
    every segment (marker -> next marker) must carry >= MIN_SEGMENT chars -
    this rejects false markers inside letter-sequence data
    ('...a g f b k d h... u p b. Construct...' keeps only the REAL 'b.').
    """
    hits = []
    for m in MARK_RE.finditer(text):
        letter = (m.group(1) or m.group(2)).lower()
        hits.append((letter, m.start(1) if m.group(1) else m.start(2)))
    if not hits:
        return []
    # longest chain starting at the first 'a'
    best = []
    for start_i, (l0, p0) in enumerate(hits):
        if l0 != 'a':
            continue
        chain = [(l0, p0)]
        want = 'b'
        for l, p in hits[start_i + 1:]:
            if l == want and p > chain[-1][1]:
                chain.append((l, p))
                want = chr(ord(want) + 1)
        if len(chain) > len(best):
            best = chain
    if len(best) < 2:
        return []
    # segment-length guard
    for i, (l, p) in enumerate(best):
        seg_end = best[i + 1][1] if i + 1 < len(best) else len(text)
        if seg_end - p < MIN_SEGMENT:
            return []
    return best


def split_containers(qs, printed_letters=None, allowed_marks=frozenset()):
    """One row per printed sub-part for bare-number container rows.

    printed_letters: optional {bare_label: ['a','b',...]} from the printed
    reference (parse_printed parts). When supplied, split letters MUST match
    the printed letters exactly - mismatch keeps the container intact and
    flags '+split-mismatch' (never invent a sub-part the paper does not
    print).

    Marks propagate to every child ONLY when in-law (or NULL): an off-law
    container mark is a section-total attribution ('2 x 10 = 20' header
    arithmetic) and is NEVER spread to children - it is logged; when demote
    is OFF it stays on the container row (keep-never-rewrite).
    Diagram fields propagate to every child (the crop covers the whole
    container region).
    """
    changes, out = [], []
    for q in qs:
        label = str(q.get('label') or '').strip()
        text = q.get('text') or ''
        if not BARE_LABEL_RE.match(label) or len(text) < MIN_CONTAINER_TEXT:
            out.append(q)
            continue
        if INSTRUCTION_RE.search(text[:120]):
            out.append(q)
            continue
        chain = marker_chain(text)
        if not chain:
            out.append(q)
            continue
        letters = [l for l, _ in chain]
        if printed_letters is not None:
            want = [x.lower() for x in printed_letters.get(label) or []]
            if want and want != letters:
                q2 = dict(q)
                q2['verify'] = (q.get('verify') or '') + '+split-mismatch'
                out.append(q2)
                changes.append({'kind': 'split-aborted',
                                'detail': f'label {label}: chain {letters} != '
                                          f'printed {want} - container kept'})
                continue
        cm = q.get('marks')
        propagate = cm is None or (int(cm) in set(allowed_marks))
        for i, (l, p) in enumerate(chain):
            seg_end = chain[i + 1][1] if i + 1 < len(chain) else len(text)
            child_text = text[p:seg_end].strip()
            child = {'label': f'{label}.{l}', 'text': child_text,
                     'marks': cm if propagate else None,
                     'has_diagram': bool(q.get('has_diagram')),
                     'diagram_page': q.get('diagram_page'),
                     'diagram_bbox': q.get('diagram_bbox'),
                     'diagram_url': q.get('diagram_url'),
                     'key': f'{label}.{l}'}
            if q.get('marks_qc'):
                child['marks_qc'] = q['marks_qc']
            out.append(child)
        changes.append({'kind': 'split',
                        'detail': f'label {label} -> {len(chain)} sub-parts '
                                  f'{letters} (container marks {cm} '
                                  + ('propagated)' if propagate else
                                     'off-law: NOT propagated to children)')})
    return out, changes


def demote_section_totals(qs, allowed):
    """OWNER-PENDING amendment (default OFF): container rows (bare label +
    >=2 sub-part markers) carrying marks outside the law are section-total
    attributions ('2 x 10 = 20' headers) -> honest NULL + documented
    transition. Sub-part rows (m=4 case) and stray marks (m=5 case) are
    NEVER demoted - they stay +marks-off-law per keep-never-rewrite."""
    changes = []
    for q in qs:
        label = str(q.get('label') or '').strip()
        m = q.get('marks')
        if m is None or (int(m) in allowed):
            continue
        if not BARE_LABEL_RE.match(label):
            continue
        if not marker_chain(q.get('text') or ''):
            continue
        changes.append({'kind': 'demote-section-total',
                        'detail': f'label {label}: marks {m} -> NULL '
                                  f'(section-total attribution on container)'})
        q['marks'] = None
        q['marks_qc'] = 'demoted-section-total'
    return qs, changes


def label_shape_scan(qs):
    """DETECT (never rewrite): bare-number rows whose text begins with a
    sub-part marker ('7' starting 'b. Write...') are mislabeled sub-parts.
    Flag '+label-shape-suspect' for the codex amendment queue."""
    flagged = []
    for q in qs:
        label = str(q.get('label') or '').strip()
        text = (q.get('text') or '').lstrip()
        if BARE_LABEL_RE.match(label) and re.match(r'^[b-l]\.\s+', text):
            q['verify'] = (q.get('verify') or '') + '+label-shape-suspect'
            flagged.append({'label': label,
                            'detail': f"text begins with sub-part marker "
                                      f"{text[:12]!r} - likely {label + text[0]}"})
    return flagged


def bridge(qs, allowed=(2, 7, 10), demote=False, printed_letters=None):
    """Full bridge pass. Returns (rows_out, report).

    report keys: clean_changes, garbled_labels, split_changes,
    demote_changes, label_suspects, rows_before, rows_after.
    """
    allowed = set(int(a) for a in allowed)
    out, report = [], {'rows_before': len(qs or []),
                       'clean_changes': [], 'garbled_labels': [],
                       'split_changes': [], 'demote_changes': [],
                       'label_suspects': []}
    for q in (qs or []):
        t, ch, garbled = clean_text(q.get('text') or '')
        if ch:
            q = dict(q)
        if t != (q.get('text') or ''):
            q['text'] = t
            q['hash'] = hashlib.sha256(
                re.sub(r'[^a-z0-9]+', ' ', t.lower()).strip().encode()).hexdigest()[:32]
        if garbled:
            q['verify'] = (q.get('verify') or '') + '+text-garbled'
            report['garbled_labels'].append(q.get('label'))
        report['clean_changes'] += [{'label': q.get('label'), **c} for c in ch]
        out.append(q)
    if demote:
        out, dm = demote_section_totals(out, allowed)
        report['demote_changes'] = dm
    out, sp = split_containers(out, printed_letters=printed_letters,
                               allowed_marks=allowed)
    report['split_changes'] = sp
    ls = label_shape_scan(out)
    report['label_suspects'] = ls
    report['rows_after'] = len(out)
    return out, report


# ---------------------------------------------------------------------------
# offline CLI: run the bridge over god6_* rows (read-only) or a JSON file
# ---------------------------------------------------------------------------

DSN = ("postgresql://postgres.qnakqtiokspzoopivfyl:repeatrankpyq"
       "@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres")


def rows_from_db(stem_substr=None):
    import psycopg2
    conn = psycopg2.connect(DSN, connect_timeout=30)
    conn.autocommit = True
    cur = conn.cursor()
    q = ("SELECT p.pdf_stem, p.printed_containers, p.printed_atomic, "
         "q.seq, q.text, q.marks, q.has_diagram, q.diagram_url, q.verify "
         "FROM god6_papers p JOIN god6_questions q ON q.paper_id = p.id ")
    args = []
    if stem_substr:
        q += "WHERE p.pdf_stem LIKE %s "
        args.append(f"%{stem_substr}%")
    q += "ORDER BY p.pdf_stem, q.seq"
    cur.execute(q, args)
    by_stem, meta = {}, {}
    for stem, pc, pa, seq, text, marks, dia, url, verify in cur.fetchall():
        by_stem.setdefault(stem, []).append(
            {'label': seq, 'text': text, 'marks': marks,
             'has_diagram': bool(dia), 'diagram_url': url,
             'verify': verify or ''})
        meta[stem] = {'printed_containers': pc, 'printed_atomic': pa}
    cur.close()
    conn.close()
    return by_stem, meta


def main():
    ap = argparse.ArgumentParser(description='GOD v7 deterministic bridge')
    ap.add_argument('--from-db', action='store_true')
    ap.add_argument('--from-file')
    ap.add_argument('--stem')
    ap.add_argument('--demote', action='store_true',
                    help='OWNER-PENDING: demote section-total marks to NULL')
    ap.add_argument('--json')
    args = ap.parse_args()

    if args.from_db:
        by_stem, meta = rows_from_db(args.stem)
    elif args.from_file:
        data = json.load(open(args.from_file, encoding='utf-8'))
        by_stem, meta = data.get('rows', {}), data.get('meta', {})
    else:
        ap.error('need --from-db or --from-file')

    result = {}
    for stem, rows in sorted(by_stem.items()):
        out, rep = bridge(rows, demote=args.demote)
        result[stem] = {'meta': meta.get(stem, {}), 'report': rep,
                        'rows': [{'label': r.get('label'),
                                  'marks': r.get('marks'),
                                  'verify': r.get('verify'),
                                  'text': r.get('text')} for r in out]}
        line = (f"{stem}: {rep['rows_before']} -> {rep['rows_after']} rows, "
                f"split={len(rep['split_changes'])} "
                f"clean={len(rep['clean_changes'])} "
                f"garbled={len(rep['garbled_labels'])} "
                f"demoted={len(rep['demote_changes'])} "
                f"mislabel={len(rep['label_suspects'])}")
        print(line)
        for c in rep['split_changes']:
            print('   ', c['detail'])
        for c in rep['demote_changes']:
            print('   ', c['detail'])
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print(f"[bridge] JSON report -> {args.json}")


if __name__ == '__main__':
    main()
