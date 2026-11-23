#!/usr/bin/env python3
"""
AKTU-PYQ  ::  Supabase end-to-end checker
==========================================
Run this ANY time to verify the whole data plane is healthy:

  python3 supabase_check.py

It needs SUPABASE_URL + SUPABASE_SERVICE_KEY (+ optionally
SUPABASE_PUBLISHABLE_KEY) - either as environment variables or from
aktu_pipeline.env sitting next to this script. Read-only checks plus one
insert/delete test row (code TEST-000), fully self-cleaning.

WHAT IT CHECKS
  1. Credentials present and service reachable
  2. All 5 tables exist and are queryable (schema.sql was applied)
     + v2 columns (papers.extraction_status etc.)
  3. subject_coverage view exists (badge engine)
  3b. repeated_questions view (ranking engine) + set_embeddings RPC
  4. Row counts per table (your live data status)
  5. RLS: publishable key can READ, must NOT be able to WRITE
  6. Storage bucket 'question-diagrams' exists + upload/delete roundtrip
  7. Coverage summary by status (complete/partial/early/coming)
EXIT 0 = all green; 1 = at least one check failed (the report says which).
"""
import json
import os
import struct
import sys
import zlib

import requests

BUCKET = "question-diagrams"
TABLES = ["subjects", "papers", "questions", "occurrences", "clusters"]
results = []


def log(m):
    print(m, flush=True)


def envs():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "aktu_pipeline.env"), os.path.join(here, ".env")):
        if os.path.exists(cand):
            for line in open(cand, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sk = os.environ.get("SUPABASE_SERVICE_KEY", "")
    pk = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "") or sk
    return url, sk, pk


def check(name, ok, detail=""):
    results.append(ok)
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))
    return ok


def tiny_png():
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00" + b"\xff\x00\x00" * 4 + b"\x00" + b"\x00\xff\x00" * 4 + b"\x00" * 2))
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + chunk(b"IEND", b"")


def main():
    url, sk, pk = envs()
    log("=" * 62)
    log("SUPABASE END-TO-END CHECK")
    log("=" * 62)
    if not check("credentials present", bool(url and sk), url or "(no url)"):
        log("  -> put SUPABASE_URL and SUPABASE_SERVICE_KEY in env or aktu_pipeline.env")
        return 1
    h_sk = {"apikey": sk, "Authorization": f"Bearer {sk}"}
    h_pk = {"apikey": pk, "Authorization": f"Bearer {pk}"}

    # 1) reachability
    r = requests.get(f"{url}/rest/v1/", headers=h_sk, timeout=20)
    check("PostgREST reachable", r.status_code == 200, f"HTTP {r.status_code}")

    # 2) tables exist (report; do not abort - storage checks below still run)
    missing = []
    counts = {}
    for t in TABLES:
        rr = requests.get(f"{url}/rest/v1/{t}", headers={**h_sk, "Range": "0-0"}, timeout=20)
        if rr.status_code == 200:
            cr = rr.headers.get("Content-Range", "")
            counts[t] = cr.split("/")[-1] if cr else "?"
            log(f"        {t:<12} rows: {counts[t]}")
        else:
            missing.append(t)
    schema_ok = not missing
    check("all 5 tables exist (schema.sql applied)", schema_ok,
          f"missing: {missing}" if missing else "")
    if not schema_ok:
        log("  FIX: Dashboard -> SQL Editor -> New query -> paste supabase_schema.sql -> Run")
        log("       (or send me the Session-pooler connection string and I apply it)")

    # 3) coverage view
    if schema_ok:
        rr = requests.get(f"{url}/rest/v1/subject_coverage", headers=h_sk, timeout=20)
        view_ok = check("subject_coverage view exists", rr.status_code == 200)
        if view_ok:
            rows = rr.json()
            from collections import Counter
            st = Counter(x.get("status") for x in rows)
            log(f"        coverage: {dict(st) or 'no subjects yet'}")

    # 3b) v2 objects: ranking view + bulk-embedding RPC + resume columns
    if schema_ok:
        rr = requests.get(f"{url}/rest/v1/repeated_questions", headers=h_sk, timeout=20)
        check("repeated_questions ranking view exists", rr.status_code == 200)
        rr = requests.get(f"{url}/rest/v1/papers", headers={**h_sk, "Range": "0-0"},
                          params={"extraction_status": "not.eq."}, timeout=20)
        v2_cols_ok = rr.status_code == 200
        check("papers.extraction_status resume column live", v2_cols_ok)
        rr = requests.post(f"{url}/rest/v1/rpc/set_embeddings", headers=h_sk,
                           json={"pairs": []}, timeout=30)
        rpc_ok = rr.status_code == 200
        check("set_embeddings RPC callable", rpc_ok,
              "" if rpc_ok else rr.text[:100] + (" (fix: NOTIFY pgrst, 'reload schema')" if rr.status_code == 404 else ""))

    # 5) RLS: publishable READ ok / WRITE must fail
    if schema_ok:
        rr = requests.get(f"{url}/rest/v1/subjects", headers=h_pk, timeout=20)
        check("anon (publishable key) can READ", rr.status_code == 200)
        rr = requests.post(f"{url}/rest/v1/subjects", headers=h_pk,
                           json={"code": "SPAM-000", "name": "spam", "course": "Other"}, timeout=20)
        check("anon WRITE correctly rejected (RLS)", rr.status_code in (401, 403))

    # 6) write path with secret key: insert -> read -> delete TEST-000
    if schema_ok:
        rr = requests.post(f"{url}/rest/v1/subjects",
                           headers={**h_sk, "Prefer": "resolution=merge-duplicates,return=representation"},
                           json=[{"code": "TEST-000", "name": "pipeline self-test", "course": "Other",
                                  "semester": 1, "is_active": False}], timeout=20)
        wrote = rr.status_code in (200, 201)
        check("service key can WRITE (insert test row)", wrote, "" if wrote else rr.text[:120])
        if wrote:
            rr = requests.get(f"{url}/rest/v1/subjects?code=eq.TEST-000", headers=h_pk, timeout=20)
            check("test row visible via public read", rr.status_code == 200 and len(rr.json()) == 1)
            rr = requests.delete(f"{url}/rest/v1/subjects?code=eq.TEST-000", headers=h_sk, timeout=20)
            check("test row deleted (self-cleaning)", rr.status_code in (200, 204))

    # 7) storage bucket + roundtrip
    rr = requests.get(f"{url}/storage/v1/bucket", headers=h_sk, timeout=20)
    names = [b.get("name") for b in rr.json()] if rr.status_code == 200 else []
    check(f"storage bucket '{BUCKET}' exists", BUCKET in names, f"buckets: {names or 'none'}")
    if BUCKET in names:
        rr = requests.post(f"{url}/storage/v1/object/{BUCKET}/__selftest.png",
                           headers={**h_sk, "x-upsert": "true", "Content-Type": "image/png"},
                           data=tiny_png(), timeout=30)
        up = rr.status_code in (200, 201)
        check("diagram upload works", up, "" if up else rr.text[:120])
        if up:
            rr = requests.get(f"{url}/storage/v1/object/public/{BUCKET}/__selftest.png", timeout=20)
            check("public diagram URL serves", rr.status_code == 200)
            requests.delete(f"{url}/storage/v1/object/{BUCKET}/__selftest.png", headers=h_sk, timeout=20)

    log("-" * 62)
    ok = all(results)
    log(f"RESULT: {'ALL GREEN - data plane ready' if ok else 'FAILURES ABOVE - fix flagged items before running the pipeline'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
