#!/usr/bin/env python3
"""
fetch_aktu_structure.py — official AKTU academic structure harvester
====================================================================
PURPOSE
  Builds the permanent, separately-stored reference of AKTU's official
  course/branch/subject structure directly from the university website
  (aktu.ac.in). Runs inside GitHub Actions (sandbox machines often cannot
  reach the site), pushes into Supabase `ref_*` tables that are COMPLETELY
  SEPARATE from the pipeline tables (subjects/papers/questions/...).

WHAT IT CAPTURES (v1 — index layer)
  1. ref_aktu_snapshots   : raw HTML of the syllabus index page (re-parseable
                            forever, even if the site changes)
  2. ref_aktu_documents   : every official syllabus/document link found
                            (program, branch, scheme year, title, pdf_url)
                            parsed OUT OF THE ANCHOR TEXT with defensive regex
  3. ref_aktu_subjects    : subject codes parsed out of fetched syllabus PDFs
                            (v2, best-effort: code + nearest name per page;
                            every row carries doc_url provenance)

DESIGN RULES
  - Never invent rows: everything comes from fetched official content.
  - Idempotent: ON CONFLICT DO UPDATE (re-runs refresh, never duplicate).
  - Provenance columns on every row (source_url, fetched_at).

USAGE
  python3 fetch_aktu_structure.py --index-only
  python3 fetch_aktu_structure.py --pdfs N      # also parse first N docs
  python3 fetch_aktu_structure.py --db          # push to Supabase (env: SUPABASE_URL, SUPABASE_SERVICE_KEY)
"""
import argparse
import io
import json
import os
import re
import sys
import html as htmllib
from datetime import datetime, timezone

BASE = "https://aktu.ac.in"
INDEX_CANDIDATES = [
    f"{BASE}/syllabus.html",
    f"{BASE}/Syllabus.html",
    f"{BASE}/syllabus",
    f"{BASE}/page/syllabus",
    f"{BASE}/",
]
#aktu.ac.in geo-fences non-IN IPs (times out from GitHub US runners and most
#cloud sandboxes) -> fall back to the Internet Archive's latest snapshot of
#the SAME official page. Provenance keeps both origin and actual source.
WAYBACK_CANDIDATES = [
    "https://web.archive.org/web/2026/" + f"{BASE}/syllabus.html",
    "https://web.archive.org/web/2026/" + f"{BASE}/",
    "https://web.archive.org/web/" + f"{BASE}/syllabus.html",
]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# --- classification regexes (defensive: AKTU titles vary wildly) -------------
COURSE_PAT = [
    (re.compile(r"\bb\.?\s*tech\b", re.I), "BTech"),
    (re.compile(r"\bb\.?\s*pharm\b", re.I), "BPharm"),
    (re.compile(r"\bm\.?\s*ca\b", re.I), "MCA"),
    (re.compile(r"\bm\.?\s*tech\b", re.I), "MTech"),
    (re.compile(r"\bb\.?\s*ca\b", re.I), "BCA"),
    (re.compile(r"\bm\.?\s*ba\b", re.I), "MBA"),
    (re.compile(r"\bb\.?\s*fa\b", re.I), "BFA"),
    (re.compile(r"\bm\.?\s*pharm\b", re.I), "MPharm"),
]
YEAR_PAT = re.compile(r"\b(20\d{2})[-\s–to]{1,4}(20\d{2})\b")
SEM_PAT = re.compile(r"\b(?:sem(?:ester)?)\s*[-:]?\s*([1-8])\b", re.I)
YEAR_ONLY = re.compile(r"\b(20\d{2})[-\s–]{0,2}(2[0-9])\b")
CODE_PAT = re.compile(r"\b([A-Z]{2,4})[-\s]?(\d{3})([A-Z]?)\b")

BRANCH_HINTS = [
    "computer science", "information technology", "electronics", "electrical",
    "mechanical", "civil", "chemical", "aerospace", "automobile", "agriculture",
    "biotech", "instrumentation", "textile", "food tech", "leather", "plastic",
    "paint", "oil", "silk", "apparel", "mining", "metallurgy", "marine", "dairy",
    "engineering physics", "mathematics", "artificial intelligence", "data science",
    "cyber", "robotics", "internet of things", "iot", "mechatronics", "enviro",
    "pharmacy", "business", "architecture", "pharmaceutical",
]


def now(): return datetime.now(timezone.utc).isoformat()


def fetch(url, timeout=40):
    from urllib.request import Request, urlopen
    req = Request(url, headers=UA)
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def pick_index():
    for label, cands in (("direct", INDEX_CANDIDATES),
                         ("wayback", WAYBACK_CANDIDATES)):
        for url in cands:
            try:
                raw = fetch(url, timeout=35)
                if raw and len(raw) > 500:
                    print(f"[i] index via {label}: {url}")
                    return url, raw
            except Exception as e:
                print(f"[i] ({label}) {url} -> {e}", file=sys.stderr)
    return None, None


def extract_links(raw_html):
    try:
        text = raw_html.decode("utf-8", "ignore")
    except Exception:
        text = str(raw_html)
    links = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text,
                         re.I | re.S):
        href, inner = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        inner = htmllib.unescape(re.sub(r"\s+", " ", inner)).strip()
        links.append((href, inner))
    return links, text


def classify(title):
    course = None
    for pat, name in COURSE_PAT:
        if pat.search(title):
            course = name
            break
    ym = YEAR_PAT.search(title)
    scheme = f"{ym.group(1)}-{ym.group(2)[2:]}" if ym else None
    sm = SEM_PAT.search(title)
    branch = next((b for b in BRANCH_HINTS if b in title.lower()), None)
    return course, branch, scheme, (int(sm.group(1)) if sm else None)


def push_db(rows_docs, rows_subjects, snapshot):
    supa = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    if not (supa and key):
        print("[!] SUPABASE_URL / SUPABASE_SERVICE_KEY missing -> DB push skipped")
        return
    import urllib.request

    def rest(table, payload, method="POST", qs=""):
        url = f"{supa}/rest/v1/{table}{qs}"
        data = json.dumps(payload).encode() if not isinstance(payload, str) else payload.encode()
        req = urllib.request.Request(url, data=data if method in ("POST", "PATCH") else None,
                                     method=method, headers={
            "apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status

    ts = now()
    try:
        rest("ref_aktu_snapshots", {"page": "syllabus_index", "url": snapshot["url"],
                                    "html": snapshot["html"][:1000000],
                                    "fetched_at": ts})
    except Exception as e:
        print(f"[!] snapshot push: {e}", file=sys.stderr)
    if rows_docs:
        for k in range(0, len(rows_docs), 400):
            rest("ref_aktu_documents", rows_docs[k:k + 400])
        print(f"[i] ref_aktu_documents upserted: {len(rows_docs)}")
    if rows_subjects:
        for k in range(0, len(rows_subjects), 400):
            rest("ref_aktu_subjects", rows_subjects[k:k + 400])
        print(f"[i] ref_aktu_subjects upserted: {len(rows_subjects)}")


def absolutize(href, base_url):
    if href.startswith("http"): return href
    # wayback-wrapped relative? keep as-is if it starts with /web/
    if href.startswith("/web/"):
        return f"https://web.archive.org{href}"
    from urllib.parse import quote, urljoin
    base_dir = base_url.rsplit("/", 1)[0]
    return urljoin(base_dir + "/", quote(href))


def is_scheme_page(href, title):
    h = href.lower()
    return ("syllabus" in h and h.endswith(".html")
            and not h.startswith("javascript") and "#" not in h)


def parse_pdf_subjects(pdf_bytes, doc_url, course, branch, scheme):
    """Best-effort subject-code harvest from a syllabus PDF."""
    rows = []
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
        last_seen_names = []
        for pno in range(len(doc)):
            page = doc[pno]
            tp = page.get_textpage()
            txt = tp.get_text_bounded() or ""
            tp.close(); page.close()
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            for i, line in enumerate(lines):
                for cm in CODE_PAT.finditer(line):
                    code = f"{cm.group(1)}{cm.group(2)}{cm.group(3) or ''}"
                    ctx = " ".join(lines[max(0, i - 1):i + 2])
                    name = re.sub(r"\s+", " ", ctx).strip()[:160]
                    rows.append({"code": code, "subject_name": name,
                                 "course": course, "branch": branch,
                                 "semester": None, "scheme_year": scheme,
                                 "doc_url": doc_url, "source": "official-syllabus-pdf",
                                 "fetched_at": now()})
        doc.close()
    except Exception as e:
        print(f"[!] pdf parse {doc_url}: {e}", file=sys.stderr)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", type=int, default=0,
                    help="also parse first N syllabus PDFs for subject codes")
    ap.add_argument("--db", action="store_true", help="push to Supabase ref_* tables")
    ap.add_argument("--out", default="aktu_structure_out.json")
    a = ap.parse_args()

    url, raw = pick_index()
    if not raw:
        print("[!] FAILED: no syllabus index reachable")
        sys.exit(2)
    print(f"[i] index fetched: {url} ({len(raw)} bytes)")

    links, full_text = extract_links(raw)
    docs = {}

    # depth 1: any PDFs directly on the index (rare)
    for href, title in links:
        if not title or len(title) < 4:
            continue
        full = absolutize(href, url)
        if re.search(r"\.pdf(\?|$)", full, re.I):
            course, branch, scheme, sem = classify(title)
            docs[full] = {"pdf_url": full, "title": title, "course": course,
                          "branch": branch, "scheme_year": scheme,
                          "source_url": url, "fetched_at": now()}

    # depth 2: scheme pages ("syllabus 2022-2023.html" etc.) hold the branch PDFs
    scheme_pages = [(href, title) for href, title in links if is_scheme_page(href, title)]
    print(f"[i] scheme sub-pages discovered: {len(scheme_pages)}")
    seen_pages = 0
    for sp_href, sp_title in scheme_pages:
        if seen_pages >= 15:
            break
        sp_url = absolutize(sp_href, url)
        try:
            sp_raw = fetch(sp_url, timeout=45)
        except Exception as e:
            print(f"[!] scheme page {sp_url}: {e}", file=sys.stderr)
            continue
        seen_pages += 1
        sp_links, sp_text = extract_links(sp_raw)
        if a.db:
            try:
                push_db([], [], {"url": sp_url, "html": sp_text})
            except Exception:
                pass
        added = 0
        for href, title in sp_links:
            if not title or len(title) < 4:
                continue
            full = absolutize(href, sp_url)
            if not re.search(r"\.pdf(\?|$)", full, re.I):
                continue
            course, branch, scheme, sem = classify(title)
            sm = YEAR_PAT.search(sp_title) or YEAR_PAT.search(sp_href)
            scheme = scheme or (f"{sm.group(1)}-{sm.group(2)[2:]}" if sm else None)
            if full not in docs:
                docs[full] = {"pdf_url": full, "title": title, "course": course,
                              "branch": branch, "scheme_year": scheme,
                              "source_url": sp_url, "fetched_at": now()}
                added += 1
        print(f"[i] {sp_title[:40]!r}: +{added} pdfs (page {len(sp_text)}B)")

    rows_docs = list(docs.values())
    print(f"[i] official syllabus documents found: {len(rows_docs)}")
    for d in rows_docs[:10]:
        print("   ", d["course"], "|", d["scheme_year"], "|", d["title"][:60])

    rows_subjects = []
    if a.pdfs > 0:
        for d in rows_docs[:a.pdfs]:
            try:
                pdf = fetch(d["pdf_url"], timeout=90)
                got = parse_pdf_subjects(pdf, d["pdf_url"], d["course"],
                                         d["branch"], d["scheme_year"])
                rows_subjects.extend(got)
                print(f"[i] {d['title'][:50]}: {len(got)} code hits")
            except Exception as e:
                print(f"[!] {d['pdf_url']}: {e}", file=sys.stderr)

    with open(a.out, "w") as f:
        json.dump({"index_url": url, "documents": rows_docs,
                   "subjects_sample": rows_subjects[:2000]}, f, indent=1)
    if a.db:
        push_db(rows_docs, rows_subjects,
                {"url": url, "html": full_text})


if __name__ == "__main__":
    main()
