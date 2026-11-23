#!/usr/bin/env python3
"""
AKTU PYQ Download Reconciler  -  audit manifest vs actual PDFs on disk
=======================================================================
WHY THIS EXISTS
  The scraper's manifest CSV is an ATTEMPT log: every paper it touches gets
  one row with a status of OK / NO_PDF_LINK / NOT_A_PDF / ERROR: <type>.
  So "manifest row count" != "files on disk" whenever anything failed.
  This script tells you EXACTLY where every paper stands, then tells you
  the exact repair command.

WHAT IT CHECKS
  1. Manifest: dedupes rows by paper_url (LAST row wins - safe even if you
     already re-ran the scraper once), tallies final statuses.
  2. Disk: counts PDFs in structured/ and unstructured/, verifies every OK
     row's file actually exists, starts with %PDF magic, and is not tiny.
  3. Orphans: files on disk that no OK row references.
  4. Inventory cross-check (optional): papers in the sitemap inventory that
     were NEVER attempted (missing from manifest entirely).
  5. Writes: reconcile_report.csv  (one row per paper + action to take)
             reconcile_retry.txt   (URLs needing attention, grouped)

IMPORTANT EDGE CASE IT CATCHES
  If an OK row's file is MISSING on disk, a plain scraper re-run will NOT
  fix it (resume trusts the manifest, skips OK rows). The report flags
  these so you can delete that manifest row first, then re-run.

USAGE (run from the same folder where you ran the scraper)
  python3 ryzenstudy_reconcile.py
    python3 ryzenstudy_reconcile.py --manifest ./ryzenstudy_download_manifest.csv --out ./aktu-pyq
  python3 ryzenstudy_reconcile.py --inventory ryzenstudy_paper_inventory.csv

EXIT CODE: 0 = fully consistent, 1 = discrepancies found (useful for scripting)
"""
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from itertools import chain

COURSES = {"btech": "BTech", "bpharm": "BPharm", "mca": "MCA", "bba": "BBA", "bca": "BCA"}
PERMANENT = ("NO_PDF_LINK", "NOT_A_PDF")
MIN_PDF_BYTES = 5 * 1024  # a real question paper is never smaller than this


def log(msg):
    print(msg, flush=True)


def norm(p):
    """Normalize separators so Windows-written manifests work anywhere."""
    return os.path.normpath(p).replace("\\", "/")


def load_manifest_final(path):
    """Return (final_by_url, total_rows, malformed_count). Last row per URL wins.

    Accept both normal manifests with a header and legacy files whose first
    data row was written before the header was created.
    """
    final = {}
    total = 0
    malformed = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        first = next(reader, None)
        if first is None:
            return final, total, malformed
        has_header = "paper_url" in first and "status" in first
        fieldnames = first if has_header else [
            "course", "semester", "academic_year", "paper_code", "paper_name",
            "paper_url", "pdf_url", "structured_path", "unstructured_path",
            "status", "downloaded_at",
        ]
        rows = reader if has_header else chain([first], reader)
        for values in rows:
            total += 1
            if len(values) > len(fieldnames):  # extra fields -> columns shifted
                malformed += 1
            row = dict(zip(fieldnames, values))
            final[row.get("paper_url", "")] = row
    return final, total, malformed


def count_disk(root, recursive):
    """Return {normalized_relpath_or_name: full_path} of PDFs on disk."""
    found = {}
    if recursive:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.lower().endswith(".pdf"):
                    full = os.path.join(dirpath, fn)
                    found[norm(os.path.relpath(full, root))] = full
    else:
        for fn in sorted(os.listdir(root)):
            full = os.path.join(root, fn)
            if os.path.isfile(full) and fn.lower().endswith(".pdf"):
                found[fn] = full
    return found


def pdf_magic_ok(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(5).startswith(b"%PDF")
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="Audit PYQ download manifest vs disk")
    ap.add_argument("--manifest", default="./update2/ryzenstudy_download_manifest.csv")
    ap.add_argument("--out", default="./update2/aktu-pyq", help="scraper output root (has structured/ and unstructured/)")
    ap.add_argument("--inventory", default="", help="optional: ryzenstudy_paper_inventory.csv for coverage check")
    ap.add_argument("--courses", default="btech,bpharm,mca")
    ap.add_argument("--report", default="reconcile_report.csv")
    ap.add_argument("--retry", default="reconcile_retry.txt")
    args = ap.parse_args()

    struct_root = os.path.join(args.out, "structured")
    flat_root = os.path.join(args.out, "unstructured")

    if not os.path.exists(args.manifest):
        log(f"[!] manifest not found: {os.path.abspath(args.manifest)}")
        log("[i] pass the right path with --manifest")
        return 1

    final, total_rows, malformed = load_manifest_final(args.manifest)
    if malformed:
        log(f"[!] WARNING: {malformed} manifest row(s) have more fields than the header "
            f"(file was hand-edited?). Their status column is unreliable.")
    struct_disk = count_disk(struct_root, recursive=True) if os.path.isdir(struct_root) else {}
    flat_disk = count_disk(flat_root, recursive=False) if os.path.isdir(flat_root) else {}

    log("=" * 62)
    log("RYZENSTUDY DOWNLOAD RECONCILE")
    log("=" * 62)
    log(f"[i] manifest      : {os.path.abspath(args.manifest)}  ({total_rows} rows, {len(final)} unique papers)")
    if total_rows != len(final):
        log("[i] note: duplicate paper rows found - using the LAST attempt for each (correct).")
    log(f"[i] output root   : {os.path.abspath(args.out)}")
    log(f"[i] structured/   : {len(struct_disk)} pdf files on disk")
    log(f"[i] unstructured/ : {len(flat_disk)} pdf files on disk")

    # ---- 1) final status tally -------------------------------------------
    status_counter = Counter()
    for row in final.values():
        s = row["status"]
        if s.startswith("ERROR:"):
            etype = s.split(":", 1)[1].strip().split(":", 1)[0].strip() or "Unknown"
            status_counter[f"ERROR: {etype}"] += 1
        else:
            status_counter[s] += 1
    n_ok = status_counter.get("OK", 0)

    log("-" * 62)
    log("FINAL STATUS PER PAPER (last attempt wins)")
    for s, n in sorted(status_counter.items(), key=lambda kv: -kv[1]):
        log(f"    {s:<28} {n:>5}")
    log(f"    {'TOTAL':<28} {len(final):>5}")

    # ---- 2) disk audit of OK rows ----------------------------------------
    ok_missing_struct, ok_missing_flat, ok_corrupt, ok_tiny = [], [], [], []
    struct_refs, flat_refs = set(), set()
    for url, row in final.items():
        if row["status"] != "OK":
            continue
        sp = row.get("structured_path", "")
        fp = row.get("unstructured_path", "")
        if sp:
            rel = norm(sp)
            struct_refs.add(rel)
            full = os.path.join(struct_root, *rel.split("/"))
            if not os.path.exists(full):
                ok_missing_struct.append(url)
            else:
                if not pdf_magic_ok(full):
                    ok_corrupt.append(url)
                size = os.path.getsize(full)
                if size < MIN_PDF_BYTES:
                    ok_tiny.append((url, size))
        if fp:
            flat_refs.add(fp)
            if fp not in flat_disk:
                ok_missing_flat.append(url)

    log("-" * 62)
    log("DISK AUDIT (OK rows vs files actually present)")
    log(f"    OK rows with structured file present : {n_ok - len(ok_missing_struct)}/{n_ok}")
    log(f"    OK rows with structured file MISSING : {len(ok_missing_struct)}")
    log(f"    OK rows with unstructured file MISSING: {len(ok_missing_flat)}")
    log(f"    corrupt (no %PDF magic)               : {len(ok_corrupt)}")
    log(f"    suspiciously small (<5 KB)            : {len(ok_tiny)}")

    orphans_s = sorted(set(struct_disk) - struct_refs)
    orphans_f = sorted(set(flat_disk) - flat_refs)
    log(f"    orphan files not in any OK row        : {len(orphans_s)} structured, {len(orphans_f)} unstructured")

    # ---- 3) inventory coverage -------------------------------------------
    never_attempted = []
    expected = 0
    if args.inventory and os.path.exists(args.inventory):
        want = {COURSES[c] for c in (x.strip().lower() for x in args.courses.split(",")) if c in COURSES}
        inv_urls = []
        with open(args.inventory, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("course", "").strip() in want:
                    expected += 1
                    u = row.get("url", "").strip()
                    if u and u not in final:
                        never_attempted.append(u)
        log("-" * 62)
        log(f"INVENTORY CROSS-CHECK (expected courses: {', '.join(sorted(want))})")
        log(f"    expected papers in sitemap  : {expected}")
        log(f"    attempted (any manifest row): {expected - len(never_attempted)}")
        log(f"    NEVER attempted (no row)    : {len(never_attempted)}")
        log(f"    coverage now                : {n_ok}/{expected} ({100.0 * n_ok / max(expected, 1):.1f}%)")

    # ---- 4) classification + repair plan ---------------------------------
    transient, permanent, repair_disk = [], [], []
    for url, row in final.items():
        s = row["status"]
        if s == "OK":
            if url in ok_missing_struct or url in ok_missing_flat or url in ok_corrupt:
                repair_disk.append(url)
        elif s.startswith("ERROR:"):
            transient.append(url)
        else:
            permanent.append(url)

    log("-" * 62)
    log("REPAIR PLAN")
    log(f"    [A] transient failures (ERROR:*)        : {len(transient)}")
    log("        -> just re-run the SAME scraper command;")
    log("           resume retries every non-OK row automatically.")
    log(f"    [B] permanent failures (NO_PDF/NOT_PDF) : {len(permanent)}")
    log("        -> open 2-3 of these in a browser to confirm;")
    log("           if the site truly has no PDF, accept as source gap.")
    log(f"    [C] OK but file lost/corrupt on disk    : {len(repair_disk)}")
    log("        -> a plain re-run will SKIP these (resume trusts manifest).")
    log("           delete their rows from the manifest first, then re-run.")
    if never_attempted:
        log(f"    [D] never attempted                     : {len(never_attempted)}")
        log("        -> auto-downloaded by the same re-run.")
    if not (transient or permanent or repair_disk or never_attempted):
        log("    nothing to repair - downloads are fully consistent.")

    # ---- 5) write report + retry list ------------------------------------
    with open(args.report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["paper_url", "course", "semester", "academic_year", "paper_code",
                    "paper_name", "final_status", "struct_on_disk", "flat_on_disk", "action"])
        for url, row in final.items():
            s = row["status"]
            sp_rel = norm(row.get("structured_path", "")) if row.get("structured_path") else ""
            fp = row.get("unstructured_path", "")
            w.writerow([url, row.get("course", ""), row.get("semester", ""), row.get("academic_year", ""),
                        row.get("paper_code", ""), row.get("paper_name", ""), s,
                        "yes" if sp_rel and sp_rel in struct_disk else ("no" if s == "OK" else "n/a"),
                        "yes" if fp and fp in flat_disk else ("no" if s == "OK" else "n/a"),
                        ("delete-manifest-row-then-rerun" if url in repair_disk else
                         ("auto-retry-on-rerun" if s.startswith("ERROR:") else
                          ("browser-check-likely-source-gap" if s in PERMANENT else "none")))])
        for u in never_attempted:
            w.writerow([u, "", "", "", "", "", "NEVER_ATTEMPTED", "n/a", "n/a", "auto-retry-on-rerun"])

    with open(args.retry, "w", encoding="utf-8") as f:
        f.write("# Papers needing attention - generated by ryzenstudy_reconcile.py\n")
        f.write("# [A] TRANSIENT: fixed by re-running the same scraper command.\n")
        f.write("# [C] OK-BUT-FILE-MISSING: DELETE this paper's row from the manifest\n")
        f.write("#     first, then re-run (resume skips OK rows otherwise).\n")
        f.write("# [B] PERMANENT: verify in a browser; if the site has no PDF, it is a source gap.\n\n")
        f.write(f"# [C] OK but file missing/corrupt on disk ({len(repair_disk)})\n")
        f.writelines(u + "\n" for u in repair_disk)
        f.write(f"\n# [A] Transient failures - auto-retried on re-run ({len(transient)})\n")
        f.writelines(u + "\n" for u in transient)
        f.write(f"\n# [B] Permanent failures - browser-check these ({len(permanent)})\n")
        f.writelines(u + "\n" for u in permanent)
        if never_attempted:
            f.write(f"\n# [D] Never attempted - auto-downloaded on re-run ({len(never_attempted)})\n")
            f.writelines(u + "\n" for u in never_attempted)

    log("-" * 62)
    log(f"[i] wrote {args.report}  (per-paper status + action)")
    log(f"[i] wrote {args.retry}   (grouped URL list)")
    problems = len(transient) + len(permanent) + len(repair_disk) + len(never_attempted)
    log(f"[i] result: {'DISCREPANCIES FOUND - follow the repair plan above' if problems else 'ALL CONSISTENT'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
