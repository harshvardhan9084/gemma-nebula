---
Task ID: CRON-V6-20261027 (setup)
Agent: Super Z (main)
Task: CEO order 2026-10-27 — "setup a scheduled cron job for interval of 6 hours: audit all stats from all platforms → fix or flag good → run the next jobs on GHA. Start from upcoming 9PM IST. Use the RPM/RPD dashboard to manipulate the round robin, higher RPD models first. Do not launch GHA now. Quality over cost."

Work Log:
- ROUND-ROBIN MANIPULATED per the AI Studio dashboard (3.1-flash-lite 180/500 RPD used vs 3.5-flash-lite 335/500): KIND_ORDER now leads gemini-3.1-flash-lite on every call kind; NEW RPD_WEIGHTS = {3.1-lite: 2, 3.5-lite: 1} gives a WEIGHTED ring (2:1 first-tries toward the roomier pool); gemma-4-26b stays EMERGENCY-gated (unlocks only when every lite lane benches; its 14.4k RPD remains the safety net). Bench state keyed by (model,key) so duplicate slots are bench-filtered — adjacency costs nothing. Ladder order also swapped in load_models() default + extraction.yml GEMMA_FALLBACK_MODELS env. Verified live with a 3-key test ring (12 entries, 3.1×2/3.5×1/gemma×1 per key, gemma gated). Commit f65b0db pushed to aktu-runner (rebased over my 3c61451+bd29017 after a soft-reset recovery — local tree had drifted).
- AUDIT-DISPATCH SCRIPT: scripts/v6_cron_cycle.py — self-contained cycle: (1) GHA audit (latest extraction.yml run status/conclusion; cancelled runs treated as neutral, not faults); (2) RAW DB audit (papers/questions deltas vs persisted state, new failed-error signature classes vs known set, unit-400 drain counter, review/complete of new batch); (3) repo audit (local==origin, fix commits present, fix commit time fetched live); (4) gates: run-active → audit only; conclusion bad → FLAG; unit-400 still failed after a POST-fix SUCCESS run → FLAG (pre-fix failures = pending-retry note, not a flag); repo unsynced / new fail class → FLAG; clean → DISPATCH next run (same proven #52 inputs, total_calls=2000, qc_min=10, chunk=2, route=auto) or REPROCESS-MAKE-GREAT when two consecutive clean cycles add zero papers (drain signal; reprocess inputs include corpus/hunt5). DRY=1 env = audit without dispatch. State: scripts/out/v6_cron_state.json + append-only v6_cron_cycles.log.
- CRON REGISTERED: job id 477668, priority 10, kind=cron "0 0 21,3,9,15 * * ?" tz Asia/Calcutta → fires 21:00 / 03:00 / 09:00 / 15:00 IST (6h cadence), FIRST FIRE today 2026-10-27 21:00 IST (= 15:30 UTC). Payload = full standing order (script path, verdict protocol, quality bar, RPD policy, worklog duty, zero-churn + never-touch-PROD rules, PAT secrecy).
- INCIDENT (transparent): my first script test ran WITHOUT DRY mode and live-dispatched run 40519857329 (#53) — violating "do not launch gha for now". Cancelled within ~1 minute (cancel 202 ×2, final status completed/cancelled; run died in setup/deps, DB confirmed 0 papers ingested, zero AI calls spent). Hardened: DRY mode added, cancelled-conclusion made neutral, unit-400 gate now only trusts post-fix SUCCESS runs as proof. State reset to pre-incident baseline (papers 9458 / questions 182891 / checked_at 2026-10-26 23:55Z) so the 21:00 fire is cycle #1.
- NO GHA LAUNCHED otherwise: #52 remains the last real run (365 papers, 7,019 questions). The 69 failed (25 pre-#52 + 44 unit-400) sit in retry queue for the 21:00 dispatch with the unit fix live.

Stage Summary:
- 6h self-driving loop is live: cron 477668 fires the audit→fix/flag→dispatch cycle from 21:00 IST; each fire audits GHA+RAW+repo, holds on any flag, dispatches only on clean audits; backlog-drain mode auto-transitions to the reprocess make-it-great campaign when the backlog empties.
- Round-robin now spends 3.1-flash-lite RPD first (2:1 ring weight per owner dashboard), 3.5-lite second, gemma emergency-only — the tighter 3.5 pool drains slower.
- Zero net GHA launches today beyond the cancelled 1-minute accident; next real run = cron cycle #1 at 21:00 IST.

---
Task ID: V6-RUN52-REPORT (2026-10-27)
Agent: Super Z (main)
Task: CEO request — "Tell me the output and statistics of the Run. To decide next run" — full post-run analysis of V6 dispatch run #52 (40382351405).

Work Log:
- RUN #52 VERDICT: completed/success, 18:57:24Z -> 23:51:18Z (4h54m), all 3 shards + post green, zero budget stops. Shards exited on the internal ~290-min processing deadline (NOT the call budget: 594+596+608 = 1,798/2,000 spent). Config live in run: models=[gemini-3.5-flash-lite, gemini-3.1-flash-lite, gemma-4-26b-a4b-it], keys=7, combos=21, min_interval=27s, per-shard budget 667, queue 494-495/shard.
- THROUGHPUT: 365 backlog papers ingested (papers 9,093 -> 9,458), window 18:59:54Z -> 23:50:12Z. New batch: complete 127 (avg 22.9 q), review 194 (avg 23.3 q, parked by QC gate: leaks=1..8, below_parser_outline), failed 44. Route: 214 text-layer / 151 vision-flagged, ALL processed vision-first per v6 trustable mandate. Questions +7,019 (total 182,891), needs_review 4,353 (62% — QC gate honesty), avg extraction_confidence 0.98, marks null 39%. v6 quality lift visible: complete rows q=10-48 vs old weak class 3-9.
- ROOT-CAUSED THE 44 FAILURES (new bug class, not extraction): PostgREST 400 on questions insert. questions.unit is INTEGER; v6 AI schema asked "unit":"<Unit-1 etc or null>" and apply_ai_questions L561 passed str(q.get('unit'))[:12] raw into int4 -> 22P02 -> whole paper failed (14+13+17 across shards). All 44 subject_codes exist in subjects; marks int-guarded, question_type enum-safe, embedding NULL by design (embed is a later dispatch) — unit was the only unguarded int4. 208 partial-insert occurrence rows stranded (upserts make retry idempotent-safe).
- FIX SHIPPED: commit 3c61451 on aktu-runner — unit_int() coercion (regex first digit 1-9 or None) applied at BOTH normalize (L573) and push (L679), prompt changed to "unit":<int 1-9 or null>. Syntax-checked, pushed. Next run auto-converts: non-reprocess skips complete only, so 44 failed retry with fix + 194 review get a convergence AI pass + fresh backlog continues.
- BACKLOG COVERAGE (sha256 of 1,483 corpus files vs DB file_hash, git-archive method): aktuonline4 549 files -> 365 ingested (66%), 184 remaining; hunt5 934 files -> 0 ingested (sorts after corpus/aktuonline4, vision-heavy scans never started). TOTAL: 25% drained, 1,118 remaining. Earlier 1,505 estimate = manifest rows; dir truth is 1,483 (22 purged strays/dupe rows).
- ECONOMICS: ~25 papers/shard/hour at current mix; 1,118 remaining ≈ 3-4 more dispatches of this shape (hunt5 scans ~4-8 calls/paper vision-heavy). RPD windows real: 3.5-flash k3/k4 benched until 07:05Z during the run; next dispatch starts with fresh daily quota. 2,000-call target is well-matched to the 5h wall (27s x 3 shards x ~5h ≈ 2,300 ceiling) — do not raise.

Stage Summary:
- Run #52 = first v6 production batch: 365 papers, 7,019 trustable questions, zero churn on existing rows, budget+deadline discipline proven; 44-paper 400 bug root-caused (questions.unit int4) and FIXED in 3c61451 before any re-dispatch.
- Backlog 25% drained; hunt5 (934 scans) fully queued for next runs; next dispatch needs no input changes — same dirs, same budget; it self-heals 44 + converges 194 review + drains 184 a4 + starts hunt5.
- Recommendation delivered to CEO: dispatch next run as-is (clean-stop discipline makes repeat dispatches safe), consider a dedicated 2,000-call top-up for the 194 review conversions after backlog drain, embed+cluster dispatch stays separate.

---
Task ID: 19 (SCOPEPURGE-20261025-1 + UNIFIED-MOVE)
Agent: Super Z (main)
Task: CEO order 2026-10-25 — "remove the existence of those 'out of scope courses' from the supabase projects too... delete everything out of scope, not just questions/repeats... delete some very very useless stuff too. move all 2520 PDFs from repo A to repo B at a unified location (not structured for now). and prepare manifests for the next questions extraction."

Work Log:
- SCOPE PREDICATE EVIDENCE-LOCKED: recon2 exposed a 48-paper disagreement (naive subjects.course join 9,093 in vs lost v2 chain 9,141 in from 10-24 session). Rebuilt the v2 chain locally (canon_map walk + RAW/PROD subject indexes) — it reproduced v2's per-course tallies for 4/5 courses EXACTLY (MBA 920 / MCA 870 / BCA 6 / BTech 6,654) and revealed the delta = cross-course canon edges (name-match aliases dragging out-of-scope papers in, e.g. NFD107 BFad→NBC205 MCA). URL-SLUG ADJUDICATION of all 76 flip codes / 120 papers: 76/76 unanimous, ZERO counter-evidence — every URL says subjects.course is right (btech-1-sem..., bfad-1-sem...). Verdict: subjects.course/scope column = truth; the ~44 bad canon edges are flagged for a future METHOD-LOCK healing pass (NOT touched now)
- RAW SURGERY (single tx, preflight-asserted, abort-safe — 3 aborted attempts rolled back clean by tx atomicity, zero partial state): papers 9,870→9,093 (777 out: 9 courses MTech/BArch/BHMCT/MPharm/BFad/MAM/BFA/DPharm/MURP), questions 182,856→171,800 (10,735 out-subject + 321 in-scope questions whose ALL occurrences sat on out papers; 135 xcontam out-subject questions on in-papers killed explicitly — they would have FK-broken the subjects delete), occurrences 190,414→179,270, subjects 4,033→3,523 (510 out), repeats 22,416→20,831 (521 out-course + 971 papers_count<2 "repeats" = definitionally-not-repeats junk + 93 dropped below pc2 after recompute), members 53,913→50,534 (0 dangling), facets 10,399→10,020 (379 out), clusters 17,349→16,873 (446 out + 30 emptied after pruning 137 member arrays vs dead questions), aliases 2,477→2,351 (canonical-side dead; 74 alias-side-out→canonical-in edges KEPT per METHOD LOCK, flagged)
- AGGREGATE REPAIRS: 108 in-scope repeats recomputed (papers_count/years/occurrences from surviving members), 393 papers n_questions re-synced, 7 surviving children re-parented (parent_id→NULL), moat repeats>=4: 1,724→1,706; top_repeats_mv refreshed 16,152→15,766 rows; VACUUM ANALYZE done; healing_log +6 (run SCOPEPURGE-20261025-1)
- ID-LEVEL BACKUP before deletes: scripts/out/scope_backup_20261025/*.csv (2.7 MB: papers/questions/occurrences/repeats/members/clusters/facets/subjects/aliases — restore-capable)
- PROD SURGERY (mirror): papers 9,870→9,093 (559 via PROD subjects + 218 extra via RAW out-code set — PROD subjects is active-only mirror missing 5,274 papers' codes), subjects 1,726→1,381, facets/aliases/clusters same predicates; RECONCILIATION PASS to mirror RAW exactly (93 stale repeats + 207 members deleted, 15 aggregates synced from RAW, 137 cluster arrays pruned, 30 empties dropped); MEMBER-TUPLE SYNC (77 residual out-paper members) → FINGERPRINTS EXACT MATCH raw==prod (repeats 20,831/Σpc 49,900 · members 50,534/20,803 distinct · facets 10,020/Σ38,083 · clusters 16,873/Σfreq 31,403); PROD has no healing_log table → audit kept in out/scope_surgery_prod_20261025.json; lessons: one "safety" conn.rollback() wiped a healthy tx (caught by post-verify showing pre-surgery counts — re-applied); mv rebuilt
- UNIFIED MOVE: repo A clone was STALE — previous session (PDFHOST-2) had already pushed mirror-drop + 2-dupe-drop + 196 aktuonline-fq hunt papers (caa182e). Reconciled: corpus/unified (FLAT, repo B aktu-runner) = 2,520 core (2,522 files − 2 typo-named content dupes = CEO's exact "2520"; 44 aktuonline-origin renames aktl__ to resolve basename collisions; 100% sha256-verified copies) + 194 fq papers (27 hash-match known editions, 167 NEVER-INGESTED new paper candidates, 2 out-of-scope excluded; fq manifest sha256 all verified incl. 1 multi-row re-download path) = 2,714 files
- REPO B PURGE (standing order): aktuonline3/4 −86 out-of-scope PDFs −20 cross-dups vs unified −2 intra-dups = 108 deleted (7,548→7,440); repo B corpus total now 10,154 PDFs
- REPO A: corpus FULLY removed from main tree (09f2e4e + 648112e — sparse-cone skip-worktree silently no-oped git rm --cached, fixed by expanding cone; remote verified: only data/aktu-pyq_launch-strategy.pdf left); data/ manifests kept as historical record
- MANIFEST FOR NEXT EXTRACTION: data/unified_manifest.csv in repo B (aktu-runner) — extractor schema (course,semester,academic_year,paper_code,paper_name,paper_url,pdf_url,structured_path,unstructured_path,status,downloaded_at) + file_hash + source columns; 2,714 rows, 0 missing paths, DB-joined provenance (source_url via file_hash), course casing normalized; NEXT EXTRACTION = dispatch repo B extraction.yml with manifest input data/unified_manifest.csv (workflow default still aktuonline3 — dispatch param, no code change)

Stage Summary:
- Both Supabase projects now carry ZERO out-of-scope existence: RAW 9,093 papers / 171,800 questions / 179,270 occurrences / 3,523 subjects / 20,831 repeats (moat≥4 = 1,706) / 50,534 members / 16,873 clusters / 10,020 facets / 2,351 aliases — PROD mirror EXACT; junk purged: 1,585 non-repeats + 476 empty/out clusters + 3,379 dead members + 11,056 dead questions
- 2026 scope = 5 courses only, everywhere; DB shrank (vacuumed) ahead of the 10-30 quota wall — delete-side work REDUCES quota pressure
- Unified corpus: repo B corpus/unified 2,714 in-scope PDFs (flat) + manifest ready; repo A corpus GONE (zero PDF loss — every unique byte hash-verified across the move); 167 never-ingested fq papers = ready-made candidates for the next paper-row ingest round
- Flagged for future METHOD-LOCK pass: ~44 cross-course canon edges (URL-proven wrong), 74 alias-side-out→canonical-in edges, BT305 pollution + RAS107 (existing flags)
- Artifacts: scripts/scope_survey_20261025.py, scope_recon2_20261025.py, scope_chain_diff_20261025.py, scope_flips_20261025.py, scope_adjudicate_20261025.py, scope_surgery_raw_20261025.py, scope_surgery_prod_20261025.py, scope_reconcile_prod_20261025.py, scope_vacuum_mv_20261025.py, unified_move_plan_20261025.py, unified_move_exec_20261025.py, fq_integrate_20261025.py + out/*.json evidence

---

# AKTU-PYQ — Product Worklog (single source of truth)

> Read ONLY this file to know the product's current status AND its full history.
> Updated every working session. Newest changelog entries first. Every number
> here was measured from live systems (Supabase / GitHub API / local forensics)
> on the date shown - never guess, always re-verify before acting.

---

## 1. Product Status (live)

| Area | State @ 2026-09-24 (v5.7.1 restart) |
|---|---|
| Product | AKTU highest-frequency repeated questions (aktu-pyq.vercel.app) |
| Code repo (A) | `harshvardhan9084/aktu-pyq` (PRIVATE, product home) |
| Runner repo (B) | `harshvardhan9084/gemma-nebula` branch `aktu-runner` (PUBLIC, temporary, CI only) — pipeline **v5.7.1** live (`b27d20e`) |
| DB | Supabase `qnakqtiokspzoopivfyl` |
| Papers in DB | **3,218** (3,157 complete / 61 review) — was frozen here 09-22→24; v5.7.1 unfreezes |
| Questions in DB | **69,856** (occurrences 73,100) |
| Clusters | 15,056 (freq≥4: 12 · ≥3: 81 · ≥2: 941) across 230 subjects |
| AI pool | 7 keys × 2 flash-lites (14 lanes, ~7k RPD) + gemma-4-26b emergency (7/7 probe PASS, RPD 14.4k) |
| Round-3 expansion | 8,156 PDFs on disk / 1,080 complete / **7,070 remaining** — 3-shard rounds on v5.7.1 |
| Launch gate | cluster-level freq≥4: 12 (freq≥2: 941, ≥3: 81) — expansion rounds re-cluster each finalize; composite recompute after ingest |

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
- Repo A `aktu-pyq` (private): product home, corpus round-1 + round-2, workflows. **REACHABLE again since 09-20 evening**: the new dual-repo fine-grained PAT (user-supplied) has read+write on BOTH aktu-pyq and gemma-nebula (contents, actions, secrets scopes verified live).
- Repo B `gemma-nebula` branch `aktu-runner` (public, TEMPORARY — user plans to flip private after 1-2 days of heavy work). Secrets set via API 09-20: SUPABASE_URL, SUPABASE_SERVICE_KEY, GEMMA_API_KEYS, REPO_A_PAT.
  - `tools/scrape_aktuonline3.py` — resume-safe round-3 downloader (chunked, checkpoint branches `scrape-r3-0/1`, collect job merges into `aktu-runner`)
  - `.github/workflows/scrape.yml` — push-triggered, 2 shards, ~1 req/s aggregate
  - `.github/workflows/extraction.yml` — dispatch-only, true 2-way matrix, secrets-guarded (SUPABASE_URL / SUPABASE_SERVICE_KEY / GEMMA_API_KEYS)
  - `pipeline/aktu_pyq_extractor.py` v5.5 + `data/aktuonline_full_index.csv` (9,099 rows)
- Why public: Actions minutes on standard runners are free/unlimited for public repos; private repos cap at 2,000 min/month. Heavy expansion runs happen while B is public.

### Sources
- aktuonline.com = primary (direct PDFs at `/papers/{slug}.pdf`, born-digital, deep archive back to 2009-10). Polite delay mandatory (robots restricts crawlers) — 1.2-2.0s.
- ryzenstudy.com = round-1 corpus source (1,395 PDFs, all extracted).
- aktu.ac.in = official layer, geo-fenced to India (run from user's machine; wayback fallback exists in `tools/fetch_aktu_structure.py`).

---

## 3. Now / Next (priority order)

1. **RUNNING — Round-3 expansion scrape on B** (8,162 new papers, 2 shards, ~3h). Scrape auto-started by the aktu-runner push; collect now merges BOTH shard manifests (union, last-wins) — the old overwrite bug is fixed.
2. ~~BLOCKED ON USER — add 3 secrets to gemma-nebula~~ DONE 09-20 evening: all 4 secrets set via REST API (libsodium sealed box) using the new dual-repo PAT.
3. **ARMED — unattended extraction chain**: collect fires extraction stage=all (iter=0) the moment scrape completes; extraction's post job counts remaining via tools/remaining.py and re-dispatches itself (cap 30 rounds ≈ 175 shard-hours). No human step between scrape-done and expanded DB.
4. THEN — after chain completes: gate re-measure (cluster rebuild runs inside stage=all) + subject alias audit round-2/3 codes (v5.5 auto-alias v1 already maps 301 old-scheme codes).
5. NEXT — subject alias audit: 1,265 codes now, 222 dash-pairs; run moat_rescue-style merge simulation before/after round-3 ingest.
6. NEXT — official structure layer from user's machine (Indian IP) for ref_aktu_subjects PDFs.
7. LATER — when heavy work done: flip gemma-nebula private (user's plan), revoke the runner PAT, and re-point workflows to Repo A (or keep B private with the 2,000-min budget for light runs).

---

## 4. Changelog (newest first)

### 2026-09-26 — 32H STANDSTILL AUTOPSIED + v5.8 SHIPS + CHAIN RELAUNCHED — Super Z
- **PAT rotated again** (old fine-grained PAT 401-dead); new dual-repo PAT verified live; GEMMA_API_KEYS re-set to 7 keys on BOTH repos + REPO_A_PAT refreshed on B.
- **Autopsy (run `36095328616`, 09-25 04:39→10:24Z):** ALL 3 extract shards GREEN; the **post job** died on Supabase REST 503 straight for 60s+ at 10:23Z — remaining.py v2's 4-attempt/50s ladder exhausted → chain dispatch never fired → **32 h total standstill**. The owner's '5:6 failing' pattern = two unshielded blip paths (AI model-level 503 minutes + Supabase platform blips) hitting per-combo retry logic.
- **v5.8 (`61ca986`):** AIRouter 5xx strikes counted per MODEL (server blips hit every key of a model); 2nd strike benches the whole model 180s with 180→360→600s escalation; sibling lite keeps serving; EMERGENCY gemma-4 lane unchanged; _ring INTERLEAVE (key-index sort → consecutive attempts alternate models, owner rule); db._send backoff 5/15/30→2/6/12s; **remaining.py v3** chain spine tolerates ~7.7 min Supabase unavailability (6 attempts, 10/30/60/120/240s).
- **Live DB at relaunch (REST-measured 17:20 UTC):** papers 6,291 (1,393 ryzenstudy + 4,898 aktuonline) · questions **116,627** · occurrences 121,280 · clusters 24,020 (rebuilt 09-24 22:12, stale by 11 h of post-build ingestion) · subjects 2,576 / aliases 560 · extraction 5,808 complete / 483 review / 0 failed · embeddings 116,533/116,627 (94 gaps only → next re-cluster is union-find only).
- **Round-3 exact remaining (sha256 corpus ↔ DB): 4,390 of 8,156** (3,731 complete · 428 review · 3,962 never ingested). At 3 shards × ~45 s/paper ≈ 1,140 papers/round → **~4 rounds ≈ 24 h wall clock**.
- **Chain RELAUNCHED: run `36262156661` (iter=0, stage=all, 3 shards, v5.8 head) — all 3 shards green through preflight.**
- Gate ladder: freq≥4 = 29 clusters (top 6: BP301T phenol acidity, BP302T eutectics) · freq≥3 = 195 · freq≥2 = 1,693 — refresh due after this corpus wave.
### 2026-09-24 — FREEZE ROOT-CAUSED + v5.7/v5.7.1 (7-key lite-only ladder, 3 shards, running) — Super Z
- **Owner report:** manual key tests 16/16 in ms while pipeline "constantly failing ~5:6"; 3 NEW keys (7-key pool, "only the 2 flash-lite models"); random 503 minutes; ~7,000 RPD; order = stop → analyse → upgrade → re-run.
- **Autopsy (run `35960494539`, 09-24 05:33→11:25Z):** 1,496 AI calls ALL enrich/repair, ZERO extract pushes; DB frozen at 3,218 papers / 69,856 questions since 09-22 (~40 h, ~7 wasted rounds at ~110 papers/h of pure churn). Extract hit its 285-min soft deadline at paper ~574/3521, then repair crashed on a deep-offset 500 → embed+cluster never ran (gate refresh blocked 2 days).
- **ROOT CAUSE (reproduced locally with a stub DB):** scraped dash-name files parse NO subject code (`parse_filename` expects `__` format; `tier0` header regex misses old/scan headers) AND `load_manifest` **discarded the manifest's paper_code** → `meta.code=None` → `push_paper` silently returned `(None, [])` → nothing inserted; papers re-chewed every round. The previous session's "mid-rebuild cluster ladder 1,000" was ALSO a measurement artifact (REST 1,000-row cap) — real clusters were 15,056 all along.
- **v5.7 (`6ae51f8`):** manifest carries paper_code/course/semester/year; `fill_meta_identity()` + dash-name regex fallback (`nar-101`→NAR101, `ar1003`→AR1003, `kcs-502`→KCS502 all unit-tested); push skips now LOUD + counted in EXTRACT SUMMARY; AIRouter = **7 keys × 2 flash-lites = 14 lanes (~7k RPD) + gemma-4-26b-a4b-it as EMERGENCY lane** (gated in `_ring`: excluded while ANY lite lane is healthy); pacing 2.2→1.2 s + router lock; **soft-deadline is per-PROCESS now** (was per-stage → overran into the 350-min wall) with +55 min housekeeping reserve; stage order extract→embed→cluster→enrich→repair, every housekeeping stage crash-isolated; repair scan keyset-paginated (`id=gt`) + cap 2,000/round; workflow true 3-way sharding.
- **keyprobe on all 7 keys** (run `36028128521`, 42 combos, strict JSON): 3.5-flash-lite **PASS 5/7** (k3/k4 429 = today's RPD already burned by morning rounds; resets 07:00Z), 3.1-flash-lite 503×7 (global overload minute — transient, exactly the owner's "random 503 minutes"), gemma-4-26b **7/7 PASS ~0.8 s**, 2.5-flash/3.6-flash confirmed dead (429/404) → ladder design validated; GEMMA_API_KEYS secret refreshed to 7 keys.
- **v5.7.1 hotfix (`b27d20e`):** `subjects.course` is the `course_t` ENUM — the v5.7 fill produced 'Btech'/'Barch' → every subjects upsert violated the enum → run `36028594692` spent ~40 min marking per-paper failures with 0 inserts (killed). `COURSE_ENUM` now maps all 15 census labels exactly; `course_t` extended live via pooler (BArch, BFA, BFad, BHMCT, MAM, MPharm, MTech, MURP, DPharm). Verified against the REAL DB: NAR101 2014-15 paper pushed → complete, 4 questions + occurrences + vectors.
- **Fresh round on `b27d20e` dispatched (run `36032254205`); chain continues automatically.** Ops note: a post-job re-fire can beat a just-pushed fix to the branch head — always check `head_sha` before adopting an auto-fired run; the GHA "pending run" quirk makes each new dispatch supersede older pending ones.

### 2026-09-21 — CHAIN STALL FIXED + RESTARTED (run 35560697111)
- Why numbers stalled: run `35532765328` legs hit the 350-min job wall (~5h50m, ~967 papers done, then cancelled); the `post` job crashed — `tools/remaining.py` built one ~26 KB `in.(…)` URL with hundreds of hashes → PostgREST HTTP 400 → no re-dispatch. Chain died at 648/8,157 papers extracted.
- Fix `5a70103`: **remaining.py v2** = full paginated scan of `papers(extraction_status=complete)` + local hash diff (no URL limits; verified live: 34 pdfs → 5 complete → 29 remaining) + **soft-deadline 320→285** (deadline checks only run between papers; 65-min headroom now).
- Fresh chain dispatched (stage=all, iter=0) → run `35560697111`, both shard legs green. Push of the fix auto-fired the scrape workflow (push trigger) — cancelled as redundant (scrape complete: 9,100/9,100 handled).
- Live DB at restart: 3,157 papers / 68,666 questions / cluster ladder 941×2 · 81×3 · 12×4. Remaining ≈ 7,509 papers ≈ 12 rounds × ~6 h ≈ 3 days at 2 shards (4-shard speedup optional after one clean round).
- NOTE for future pushes to this branch: push triggers scrape (paths-ignore only covers corpus/aktuonline3 + manifests). After the chain finishes, consider gating the push trigger (e.g. add `tools/**`, `.github/**` to paths-ignore).



### 2026-09-20 (evening) — SETUP COMPLETED: push + secrets + self-driving expansion chain
- User issued new dual-repo fine-grained PAT (aktu-pyq + gemma-nebula, read&write, incl. Actions secrets) — verified live against both repos; old public-only PAT retired.
- `aktu-runner` branch PUSHED to gemma-nebula (the previous session's push died with the dead PAT): pipeline v5.5 (Repo A main ad67b9b: rpc shield + auto-alias), round-3 index (9,099 rows / 937 in_db), both workflows, tools.
- Secrets set on B via REST API (libsodium sealed box): SUPABASE_URL, SUPABASE_SERVICE_KEY, GEMMA_API_KEYS, REPO_A_PAT — extraction preflight can no longer block.
- scrape.yml collect job FIXED + UPGRADED: (a) manifest merge bug closed — it previously `git checkout`-overwrote shard 0's manifest rows with shard 1's; now tools/merge_manifests.py unions both (last-wins per paper_url); (b) chain dispatch: scrape complete → extraction stage=all iter=0; rows remain → re-dispatch scrape; ZERO progress → stop + warn (no infinite loop). Progress tracked in data/aktuonline3_progress.json (paths-ignored).
- extraction.yml UPGRADED: `iter` input, optional Repo A corpus checkout (`pdf_dir` prefix `repo-a/` via REPO_A_PAT — round-1/2 corpora extractable from B too), and a `post` job running tools/remaining.py (sha256 vs papers.extraction_status=complete) that self-re-dispatches stage=all up to 30 rounds (~175 shard-hours, covers 8,162 papers at ~45s/paper on 2 shards).
- Push to aktu-runner auto-triggers the round-3 scrape (push trigger) — expansion began immediately.
- **FIRST REAL RUN FOUND A LATENT BUG** (run 35518338973, 15:02-15:27 UTC): scraper ran flawlessly (+456 OK in 25 min, 0 errors, ~1,090/h/shard) but the checkpoint commit crashed with `fatal: not a valid object name origin/scrape-r3-0` — `git rev-parse` of a missing ref ECHOES THE LITERAL ARG to stdout, so PARENT became the string "origin/scrape-r3-0" and commit-tree rejected it (bug inherited from the pre-push workflow, never executed until now; also the direct-URL push never creates a remote-tracking ref → round 2 would have failed too). FIXED: `-q --verify` guarded parent lookup + tracking-ref refresh after each checkpoint push. No corpus state was lost (manifest never checkpointed → next run resumes from zero, politely re-downloading).

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
5. ~~Repo A unreachable from automation PAT~~ CLOSED 09-20 evening: new dual-repo PAT restores Repo A read/write (workflows on B can check out Repo A corpus via REPO_A_PAT; heavy compute stays on B's free minutes).
6. ~~Extraction workflow on B blocked until user adds the 3 secrets~~ CLOSED 09-20 evening: all 4 secrets set via API.
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
# Secrets: DONE 09-20 via API (SUPABASE_URL, SUPABASE_SERVICE_KEY, GEMMA_API_KEYS, REPO_A_PAT)
# Chain: scrape completes → collect dispatches extraction stage=all → post job re-dispatches until remaining=0 (cap 30)
# Chain progress: data/aktuonline3_progress.json + Actions step summaries
# Repo A corpus from B: dispatch extraction with pdf_dir=repo-a/corpus/unstructured, manifest=repo-a/data/aktu-pyq_manifest.csv

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
3. Credentials NEVER in git. Secrets live in: GitHub Actions secrets (A and B), user password manager. The automation PAT is rotate-when-compromised (current: dual-repo fine-grained, rotate when B flips private).
4. After each heavy run: verify finalize survived (extraction_status counts, no_vector=0), then update the Launch Gate tracker (§1).
5. Launch gate decision = per major branch/subject: does the subject have ≥4-verified-repetition questions for its top repeats? Expansion first, metric second — the gate follows the corpus.
