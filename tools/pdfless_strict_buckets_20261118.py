#!/usr/bin/env python3
"""Strict identity quantification of the 2,585 PDF-ABSENT papers.
Identity = (course code, session window, subject-title slug).
Buckets: exact-slug unique / exact-slug ambiguous / near-slug / no-match.
READ-ONLY. Writes scripts/out/pdfless_strict_buckets_20261118.json"""
import json, re, sys, collections, subprocess
import difflib

rows = [json.loads(l) for l in open("/home/z/my-project/scripts/out/completeness_full.jsonl")]
pdfless = [r for r in rows if r["verdict"] == "PDF-ABSENT"]

# ---- corpus inventory: full path, per dir, parsed identity ----
files = subprocess.run(["git", "ls-files"], cwd="/home/z/my-project/repo_b",
                       capture_output=True, text=True).stdout.splitlines()
pdfs = [f for f in files if f.lower().endswith(".pdf")]

CODE = re.compile(r"([a-z]{2,4})-?(\d{3})([a-z]?)(?![a-z0-9])")
def code_of(s):
    m = CODE.search(s)
    return (m.group(1) + m.group(2) + (m.group(3) or "")) if m else None

def slug_of(stem):
    """subject-title slug: strip program/sem tokens, code token, year tokens.
    IMPORTANT: unify '__' and '-' separators FIRST so year stripping works on
    both naming eras (old: btech-1-sem-x-nas101-2019, new: btech__sem01__2018-19__nas101__x)."""
    s = stem.lower().replace("__", "-")
    s = re.sub(r"\b(btech|bpharm|bpharma|mba|mca|barch|bfa|bdes|aktu)\b", " ", s)
    s = re.sub(r"\b(sem|yr|semester)\s*-?\s*\d+\b", " ", s)
    s = re.sub(r"\b\d+\s*-?\s*sem\b", " ", s)                     # old style '1-sem'
    s = re.sub(r"\b(sem|yr)\b", " ", s)                            # leftover bare token
    s = re.sub(r"-(19|20)\d{2}-(19|20)\d{2}(?=(\D|$))", " ", s)   # session pair 2018-19
    s = re.sub(r"\b(19|20)\d{2}\b", " ", s)                       # any remaining 4-digit year
    s = re.sub(r"\b\d{1,2}(st|nd|rd|th)?\b", " ", s)              # stray sem numbers
    s = re.sub(r"\b(may|june|nov|dec|feb|march|april|july|aug|sept|september|october)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # order-insensitive identity key: sorted tokens (word order differs between eras)
    return " ".join(sorted(s.split()))

YEAR = re.compile(r"(20\d{2})(?:-(\d{2}))?")
def session_windows(stem):
    """plausible session (start,end) civil-year windows implied by the stem's year token(s)"""
    wins = set()
    pairs = re.findall(r"(20\d{2})-(\d{2})(?!\d)", stem)
    for a, b in pairs:
        s, e = int(a), 2000 + int(b)
        if s < e: wins.add((s, e))
    singles = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", stem)]
    for y in singles:
        wins.add((y - 1, y))   # exam held in Y of session (Y-1)-Y
        wins.add((y, y + 1))   # page-year = session start year
        wins.add((y, y))       # single-year token
    return wins

corpus = []   # (dir, stem, code, slug, window)
for f in pdfs:
    stem = f.rsplit("/", 1)[-1][:-4].lower()
    d = f.split("/")[1]
    corpus.append((d, stem, code_of(stem), slug_of(stem), None))
for i, c in enumerate(corpus):
    c = list(c); c[4] = session_windows(c[1]); corpus[i] = tuple(c)

by_code = collections.defaultdict(list)
by_slug = collections.defaultdict(list)
for c in corpus:
    if c[2]: by_code[c[2]].append(c)
    if c[3]: by_slug[c[3]].append(c)

def program_of(s):
    return re.split(r"[_-]", s)[0]

buckets = collections.Counter()
out_rows = []
for r in pdfless:
    st = r["stem"].lower()
    c = code_of(st); slug = slug_of(st)
    wins = session_windows(st)
    # candidate pool: same code if code present; else same slug exact index, else same program
    if c:
        pool = by_code.get(c, [])
    else:
        pool = by_slug.get(slug, []) or [x for x in corpus if program_of(x[1]) == program_of(st)]
    cand = [x for x in pool if x[4] & wins]
    exact = [x for x in cand if x[3] == slug and slug]
    near = [x for x in cand if x[3] != slug and slug and
            difflib.SequenceMatcher(None, slug, x[3]).ratio() >= 0.85]
    # token-order-insensitive near-match rescue (same tokens, different order/split)
    if not exact and not near:
        near = [x for x in cand if x[3] and slug and
                difflib.SequenceMatcher(None, "".join(slug.split()), "".join(x[3].split())).ratio() >= 0.88]
    rec = {"pid": r["pid"], "stem": st, "code": c, "slug": slug}
    if len(exact) == 1:
        buckets["EXACT-SLUG-UNIQUE"] += 1
        rec.update(bucket="EXACT-SLUG-UNIQUE", hit=exact[0][1], dir=exact[0][0])
    elif len(exact) > 1:
        buckets["EXACT-SLUG-AMBIGUOUS"] += 1
        rec.update(bucket="EXACT-SLUG-AMBIGUOUS",
                   hits=[x[1] for x in exact], dirs=sorted({x[0] for x in exact}))
    elif len(near) == 1:
        buckets["NEAR-SLUG-UNIQUE"] += 1
        rec.update(bucket="NEAR-SLUG-UNIQUE", hit=near[0][1], dir=near[0][0])
    elif len(near) > 1:
        buckets["NEAR-SLUG-AMBIGUOUS"] += 1
        rec.update(bucket="NEAR-SLUG-AMBIGUOUS",
                   hits=[x[1] for x in near[:4]], dirs=sorted({x[0] for x in near}))
    else:
        buckets["NO-MATCH"] += 1
        rec.update(bucket="NO-MATCH")
    out_rows.append(rec)

json.dump({"buckets": dict(buckets), "rows": out_rows},
          open("/home/z/my-project/scripts/out/pdfless_strict_buckets_20261118.json", "w"), indent=1)
print("PDF-ABSENT:", len(pdfless))
for k, v in buckets.most_common():
    print(f"  {k}: {v}")
recoverable = sum(v for k, v in buckets.items() if k != "NO-MATCH")
print("recoverable-by-name (all buckets):", recoverable, "| truly no-match:", buckets["NO-MATCH"])
# sample rows per bucket
seen = set()
for rec in out_rows:
    if rec["bucket"] in seen: continue
    seen.add(rec["bucket"])
    print("\nSAMPLE", rec["bucket"], ":", rec["stem"])
    for h in rec.get("hits", rec.get("hit", []))[:2] if isinstance(rec.get("hits"), list) else [rec.get("hit")]:
        if h: print("   ->", h)
