# AKTU-PYQ — Product Worklog (single source of truth)

> Read ONLY this file to know the product's current status AND its full history.
> Updated every working session. Newest changelog entries first. Every number
> here was measured from live systems (Supabase / GitHub API / local forensics)
> on the date shown - never guess, always re-verify before acting.

---

## 1. Product Status (live)

| Area | State @ 2026-09-20 |
|---|---|
| Product | AKTU highest-frequency repeated questions (aktu-pyq.vercel.app) |
| Code repo (A) | `harshvardhan9084/aktu-pyq` (PRIVATE, product home) |
| Runner repo (B) | `harshvardhan9084/gemma-nebula` branch `aktu-runner` (PUBLIC, temporary, CI only) |
| Pipeline | v5.4 one-pass paper-wise (extract+AI repair+enrich+push+vectors), true 2-way sharding |
| DB | Supabase `qnakqtiokspzoopivfyl` |
| Papers in DB | **2,509** (1,393 ryzenstudy + 1,116 aktuonline) — 2,454 complete / 55 review |
| Questions in DB | **56,080** (embeddings 100%, marks NULL 4,985) |
| Subjects | 1,265 codes (222 dash-codes pending alias audit) |
| Clusters | 15,056 (freq4: 12 · freq3: 81 · freq2: 941 · freq1: 14,022) — member_ids corruption FIXED (0 bad rows) |
| Verbatim repeats | ge2: 1,717 · ge3: 93 · ge4: 2 |
| Corpus on disk | round-1 1,395 PDFs (Repo A) · round-3 scrape **IN PROGRESS on B** |
| Round-3 expansion | **9,099-paper full archive index built; 8,162 NEW to download** (937 already in DB) |
| Launch gate | "min 4 repetitions for major branches/subjects" — NOT yet met at scale (12 clusters + 2 verbatim at freq≥4); expansion is the lever, see §3 |

### Launch Gate tracker
| Metric | 2026-09-15 | 2026-09-20 (post-backlog) |
|---|---|---|
| Questions with repeat signal (ge2 verbatim ∪ cluster) | 2,167 | ~2,300+ (re-measure after round-3 ingest) |
| Clusters freq≥4 | 5 | 12 |
| Verbatim ge4 | 0 | 2 |
| Papers | 1,729 | 2,509 |

---

## 2. Live Systems Map

### Supabase (qnakqtiokspzoopivfyl)
- REST: `https://qnakqtiokspzoopivfyl.supabase.co` + service key (secret, NEVER in git)
- DB: session pooler `aws-0-ap-southeast-2` (works from sandbox + local)
- Tables: papers (file_hash key, idempotent) / questions / occurrences / subjects / subject_aliases (canonical code map) / clusters / extraction + ref_aktu_* (official structure layer)
- Views: top_repeats, subject_coverage, repeated_questions

### GitHub
- Repo A `aktu-pyq` (private): product home, corpus round-1 + round-2, workflows. **Inaccessible to the automation PAT as of 2026-09-20** (old token dead, new token scoped to public repos only).
- Repo B `gemma-nebula` branch `aktu-runner` (public, TEMPORARY — user plans to flip private after 1-2 days of heavy work):
  - `tools/scrape_aktuonline3.py` — resume-safe round-3 downloader (chunked, checkpoint branches `scrape-r3-0/1`, collect job merges into `aktu-runner`)
  - `.github/workflows/scrape.yml` — push-triggered, 2 shards, ~1 req/s aggregate
  - `.github/workflows/extraction.yml` — dispatch-only, true 2-way matrix, secrets-guarded (SUPABASE_URL / SUPABASE_SERVICE_KEY / GEMMA_API_KEYS)
  - `pipeline/aktu_pyq_extractor.py` v5.4 + `data/aktuonline_full_index.csv` (9,099 rows)
- Why public: Actions minutes on standard runners are free/unlimited for public repos; private repos cap at 2,000 min/month. Heavy expansion runs happen while B is public.

### Sources
- aktuonline.com = primary (direct PDFs at `/papers/{slug}.pdf`, born-digital, deep archive back to 2009-10). Polite delay mandatory (robots restricts crawlers) — 1.2-2.0s.
- ryzenstudy.com = round-1 corpus source (1,395 PDFs, all extracted).
- aktu.ac.in = official layer, geo-fenced to India (run from user's machine; wayback fallback exists in `tools/fetch_aktu_structure.py`).

---

## 3. Now / Next (priority order)

1. **NOW — Round-3 expansion scrape on B** (8,162 new papers, 2 shards, ~3h). Monitor run, then consolidate to `aktu-runner`.
2. **BLOCKED ON USER (2 min) — add 3 secrets to gemma-nebula** (Settings → Secrets → Actions): `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GEMMA_API_KEYS`. Without them the extraction workflow refuses to start (preflight error names it).
3. THEN — dispatch `extraction` (stage=extract) on B for corpus/aktuonline3. Free minutes; resume-safe by file_hash.
4. THEN — `repair` stage (NULL marks backfill, v5.4 order=id safe) + `embed`/`cluster` re-run over the full corpus → fresh clusters + gate re-measure.
5. NEXT — subject alias audit: 1,265 codes now, 222 dash-pairs; run moat_rescue-style merge simulation before/after round-3 ingest.
6. NEXT — official structure layer from user's machine (Indian IP) for ref_aktu_subjects PDFs.
7. LATER — when heavy work done: flip gemma-nebula private (user's plan), revoke the runner PAT, and re-point workflows to Repo A (or keep B private with the 2,000-min budget for light runs).

---

## 4. Changelog (newest first)

### 2026-09-20 — public runner migration + EXPANSION kickoff
- Backlog run (Repo A, 791 gap-year papers) **COMPLETED**: papers 1,729→2,509, questions 39,615→56,080, finalize survived (v5.4 shield), vectors 100%, cluster rebuild replaced ALL corrupted member_ids rows (0 bad rows now) — known-issue closed.
- New facts: clusters now 15,056 (12 at freq≥4); verbatim ge2 1,717 / ge3 93 / ge4 2; 55 papers in review status.
- Old PAT dead; user supplied new fine-grained PAT (public repos only): verified scopes — Contents:write + Actions:read on 8 public repos; NO secrets/variables/gists/repo-creation/Repo-A access.
- Public runner architecture (user decision, temporary): Repo B = `gemma-nebula` branch `aktu-runner`, self-contained (code + index + workflows); Repo A untouched. PDFs exposure explicitly accepted by user for 1-2 days.
- Built FULL aktuonline archive index: 38 listing pages → **9,099 papers** (bigger than the 5,365-paper round-2 crawl; covers all courses: barch/bba/bca/bfa/bfad/bhmct/bpharm/dpharm/mam/march/mba/mca/mpharmacy/mtech/murp + 21 btech branches). Direct PDF pattern proven: `/papers/{slug}.html` → `/papers/{slug}.pdf`.
- v5.4 re-applied on runner copy (500-shield 4 attempts 5/15/30s; get_paginated `order=` stable pagination; repair backfill `order=id` page=500) — the finalize-crash fix preserved.
- Smoke test: 3/3 PDFs OK across barch/btech-ee/murp (incl. a 2009-10 paper) → scraper + manifest format validated.
- Scrape workflow live with checkpoint commits to `scrape-r3-0/1` (plumbing-only, linear chain) + collect job into `aktu-runner`.

### 2026-09-19 — v5.4 + dispatch + gate baseline
- Finalize crash root cause (run 35370682506): transient PostgREST 500 during NULL-marks backfill (offset pagination + no retry on 500) → v5.4 fixes pushed; matrix true 2-way fix (`252baa7`); backlog run 35452463201 dispatched on Repo A.
- member_ids corruption discovered (Sep-15 moat_cluster.py pooler write had character-split uuid[] elements) → fix path = full re-cluster; executed by the backlog run's finalize (see 09-20).
- gate4 baseline measured pre-backlog: 0 verbatim≥4, 5 clusters freq=4.
- worklog.md established in Repo A (9b43631) as the single source of truth.

### 2026-09-18 — round-2 corpus + benchmark
- Parallel session: round-2 gap-year corpus (1,127 aktuonline PDFs 2018-19..2021-22) scraped & committed; multi-source benchmark: aktuonline primary / pooripadhai secondary (title-verify) / lastmomenttuitions dropped.
- User attachments received & decoded (bulk-scrape 34/34 report, benchmark_attempts.json, forensics.json).

### 2026-09-15 — moat rescue + corruption + structure layer
- moat_rescue/moat_fix/moat_cluster: subject_aliases (151 rows, 646→495 canonical groups), cross-code verbatim merge (88 dup rows), clusters rebuilt (1,007), repeat-signal questions 330→2,167 (6.6x). top_repeats view created (1,230 entries; top: BP703T community-pharmacy ethics 5x/4yrs).
- member_ids corruption introduced this day (root cause of the later 12-char element bug) — closed 09-20.
- Official structure harvester: ref_aktu_snapshots/documents/subjects (219 docs), depth-2 wayback crawl works from GHA.

### 2026-09-14 / 09-12 — v5 AI layer + full-corpus extraction
- v4/v5 paper-wise one-pass pipeline; quota-aware AIRouter (screenshot-verified model ladder, gemini-2.5-flash primary); v5.2 text cleaners; v5.3 cross-code clustering via subject_aliases; full 1,393-paper extraction completed (32,576 questions), DB health report delivered (Task 11 verdict: foundation launch-grade, repetition depth critical gap).

---

## 5. Metrics history

| Date | Papers | Questions | Clusters | Clusters ge4 | Verbatim ge2/ge3/ge4 |
|---|---|---|---|---|---|
| 2026-09-12 | 1,393 | 32,576 | 642→871 | — | 178 repeats (max 3x) |
| 2026-09-15 | 1,729* | 32,375* | 1,007 | 5 (freq=4) | 414 / — / 0 |
| 2026-09-20 | 2,509 | 56,080 | 15,056 | 12 | 1,717 / 93 / 2 |
(*pre-backlog interim measures)

Repeat-signal questions (verbatim ge2 ∪ clusters ge2): 2,167 (09-15) → re-measure after round-3.

---

## 6. Known Issues

1. ~~clusters.member_ids corruption~~ CLOSED 09-20 (finalize rebuild wrote clean rows; 0 length-1 elements).
2. Subject-code fragmentation GROWING with round-2/3 old-scheme codes (1,265 codes, 222 dash pairs) — alias audit queued (§3.5). canonical_code() guard in push path prevents re-splitting merged subjects.
3. marks NULL on 4,985 questions — `repair` stage (v5.4 order=id) self-heals a batch per run.
4. 55 papers in review status — mostly Google-overload at AI stage; re-run enrich/extract self-heals.
5. Repo A + its remaining 2,000-min/month budget unreachable from the automation PAT — public runner B is the workaround; keep heavy jobs while B is public.
6. Extraction workflow on B blocked until user adds the 3 secrets (§3.2).
7. Pooripadhai secondary source needs title-verify (occasionally wrong paper) — only for subject-gap backfill, not bulk.
8. aktu.ac.in official PDFs geo-fenced — user-local run only (wayback path exists but PDFs not archived).

---

## 7. Operator Cheat-Sheet

```bash
# Scrape status (B): Actions tab → scrape-aktuonline3 → check shard summaries
# Re-scrape/resume:  re-run workflow (resume-safe) or push any commit to aktu-runner
# Extraction run (B): Actions → extraction → Run workflow → stage=extract, pilot_n=0
# Pilot first:        stage=extract, pilot_n=5
# Marks backfill:     stage=repair
# Re-cluster:         stage=cluster (embed first if new questions)
# Secrets (one-time): Settings → Secrets and variables → Actions → 3 keys

# DB quick probe (pooler):
#   papers 2509 · questions 56080 · clusters 15056 · subjects 1265
#   SELECT freq_count, count(*) FROM clusters GROUP BY 1 ORDER BY 1 DESC;

# aktuonline pattern: paper page /papers/{slug}.html → PDF /papers/{slug}.pdf
# Index rebuild (sandbox): scripts/aktuonline_index_builder.py (38 pages, ~1 min)
```

---

## 8. Maintenance protocol

1. Every session: prepend a dated changelog entry (§4) + refresh §1 table + append metrics row (§5). Never delete history.
2. Every number from a live probe (db_state.py / GHA API / filesystem) on the day it was measured. No memory-based numbers.
3. Credentials NEVER in git. Secrets live in: GitHub Actions secrets (B), user password manager. The automation PAT is rotate-when-compromised, scoped public-only.
4. After each heavy run: verify finalize survived (extraction_status counts, no_vector=0), then update the Launch Gate tracker (§1).
5. Launch gate decision = per major branch/subject: does the subject have ≥4-verified-repetition questions for its top repeats? Expansion first, metric second — the gate follows the corpus.
