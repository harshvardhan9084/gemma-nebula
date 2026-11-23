# AKTU-PYQ Complete Pipeline — Owner's Guide (v3)

This document teaches you EVERYTHING: what was built, how it works, how the
"already extracted" detection works, what you still have to touch, and what
every column in your database means. Nothing here requires you to trust it —
every claim is something you can verify with the commands shown.

---

## 1. The big picture

```
ryzenstudy.com ──(scraper, done)──► corpus/unstructured/*.pdf   (1,395 PDFs, in THIS repo)
                                          │
                                          ▼  GitHub Actions (free compute)
┌─────────────────────────────────────────────────────────────────────┐
│ WORKFLOW: .github/workflows/extraction.yml                          │
│                                                                     │
│  health job ──► checks secrets + which Google keys are alive        │
│  extract job ──► parse PDFs → questions → Supabase (2 shards on     │
│                  backlog; AI repair for hard papers; diagram crops) │
│  enrich job ──► Gemma tags EVERY question: unit_topic,              │
│                  question_type, parent_id linking                   │
│  embed_cluster job ──► local 384-dim embeddings (fastembed, ₹0)     │
│                  + near-duplicate clustering + importance scoring   │
│  summary job ──► writes run report into the Actions summary page    │
└─────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                    Supabase (your DB) ──► your website reads it
                    (subjects/papers/questions/occurrences/clusters
                     + repeated_questions ranking view)
```

---

## 2. Repo layout (after the restructure)

```
aktu-pyq/                        (PRIVATE repo)
├── .github/workflows/extraction.yml   ← the pipeline workflow (v3)
├── pipeline/                          ← everything the pipeline needs
│   ├── aktu_pyq_extractor.py          the 4-stage extractor (v3)
│   ├── supabase_schema.sql            full DB schema (v2, idempotent)
│   ├── supabase_check.py              15-check data-plane verifier
│   ├── check_keys.py                  Google-key health checker
│   ├── requirements.txt               python deps
│   ├── aktu_pipeline.env.example      template for local runs (never commit real .env)
│   ├── README.md                      ← THIS guide
│   └── EXTRACTION_GUIDE.md            older runbook (kept for reference)
├── corpus/
│   ├── unstructured/*.pdf             ← ALL 1,395 flat PDFs (pipeline input)
│   └── structured/BTech/Sem-*/...     human-browsable copies (same files)
├── data/                              manifests, inventory, reconcile reports
├── tools/                             scraper / reconciler / inventory updater
├── backend/  frontend/                your web apps (untouched)
├── README.md                          short pointer to pipeline/README.md
└── .gitignore                         blocks *.env, extraction_out/, etc.
```

Why this layout: `pipeline/` is code, `data/` is provenance, `tools/` are
utilities, `corpus/` is the raw material. The workflow's default `pdf_dir`
input is `corpus/unstructured` — no more `update2/aktu-pyq/...` path confusion.

---

## 3. The 4 stages — what happens and why

### Stage `extract` — PDF → structured questions
1. **Tier 0 (deterministic cleanup):** filters the diagonal watermark
   characters, rebuilds lines, fixes mojibake (ftfy), strips Hindi lines
   (they are duplicate alternates of English lines), drops footers.
   Scanned papers (no text layer) automatically fall back to OCR (tesseract).
2. **Tier 1 (fuzzy parser):** segments questions using multiple signals
   (numbering, section headers, marks math like `2 × 7 = 14`, trailing
   marks+CO columns), groups OR-alternatives into `choice_group`s, and
   computes a confidence score per paper.
3. **Gate:** confidence ≥ 0.62 → accepted **with zero AI tokens**.
4. **Tier 2 (AI repair, only for failed papers):** Gemma reconstructs the
   question list as strict JSON (`response_schema` enforced). Output is
   RE-VALIDATED with the same checks — if the AI fails validation it goes
   to `needs_review`, never silently trusted (anti-hallucination rule).
5. **Diagram crops:** questions flagged `has_diagram` get a PNG crop of the
   region below them, uploaded to Storage (`question-diagrams` bucket) →
   public URL written to `questions.diagram_url`.
6. **Push:** upserts into Supabase (subjects → papers → questions →
   occurrences). One paper row per PDF (`file_hash` UNIQUE).

### Stage `enrich` — AI tagging (kills the 'other'/empty labels)
For every paper with `enriched_at IS NULL`, ONE Gemma call receives all its
questions and returns, per question:
- `unit_topic` — 2–6 word syllabus topic (sanitized; fallback `'general'`)
- `question_type` — theory / numerical / short / mcq / diagram
  (validated against the DB enum; invalid → deterministic heuristic)
- `parent` — links sub-questions (a)/(b) to their stem question row
  (`parent_id`) when the stem exists as its own row
Then the paper is stamped `enriched_at = now()` → never re-tagged.

### Stage `embed` — vector search & dedup, zero API cost
Every question with `embedding IS NULL` is embedded **locally** with
fastembed `all-MiniLM-L6-v2` (384-dim — matches the schema) on the runner's
CPU. Vectors are written in bulk via the `set_embeddings` RPC (1 HTTP call
per 400 questions). No Google quota, deterministic, free.

### Stage `cluster` — the "most repeated questions" engine
Per subject: cosine similarity between all embeddings → union-find grouping
at ≥ **0.88** (validated: identical meaning ≈ 1.00, paraphrase ≈ 0.93,
unrelated ≈ 0.06–0.21). Groups of ≥2 questions become rows in `clusters`:
- `freq_count` = number of DISTINCT years the group appeared in
- `importance` (0–100) = `45·(freq/target) + 25·(avg_marks/15) + 30·recency`
- `label` = most common unit_topic in the group (fallback: shortest text)
Clusters are recomputed per subject (delete + insert) → always consistent.

---

## 4. How reruns know what is already extracted (idempotency)

This is the part you asked to "just make sure it does". It works at FIVE
independent layers — you never have to clean anything up manually:

| Layer | Mechanism | Effect on rerun |
|---|---|---|
| 1 | `papers.file_hash` UNIQUE | same PDF can never create a second paper row |
| 2 | `papers.extraction_status = 'complete'` | extract hashes every PDF (sha256), looks them up in the DB, and **skips** papers already done — even if the local state file was deleted, even on a fresh runner |
| 3 | `questions (subject_code, question_hash)` UNIQUE + upsert | re-pushing a paper updates rows instead of duplicating |
| 4 | `occurrences (question_id, paper_id)` UNIQUE + upsert | occurrences can never double |
| 5 | `papers.enriched_at` / `questions.embedding IS NULL` / cluster recompute | enrich and embed only touch what's missing; clusters are rebuilt deterministically |

Practical meaning: **run any mode, any time, as many times as you want.**
A crashed backlog run? Just run it again — finished papers are skipped in
seconds, unfinished ones resume. The local `extract_state.json` is only a
convenience cache; the DATABASE is the source of truth.

---

## 5. Keys & secrets (what is set where)

**GitHub repo Secrets (I set these for you via the API):**
- `SUPABASE_URL` = `https://qnakqtiokspzoopivfyl.supabase.co`
- `SUPABASE_SERVICE_KEY` = your `sb_secret_...` key (pipeline write access)
- `GEMMA_API_KEYS` = your two `AIza...` AI Studio keys, comma-separated

The extractor reads `GEMMA_API_KEYS` as a **fallback chain**: it starts with
key #1 and rotates to the next on 429/quota/401/403/dead-key, so one
exhausted project never stops a run. If all keys die mid-run, the paper goes
to `needs_review` and the run continues.

**About your 3rd credential** (`AQ.Ab8RN6I...`): it is not a valid AI Studio
API key format (real ones start `AIza...`); Google rejected it as
UNAUTHENTICATED. It looks like a one-time authorization code. If you meant
to create a 3rd API key, go to aistudio.google.com → Get API key with your
3rd GCP project, copy the `AIza...` value, and add it to `GEMMA_API_KEYS`
(then re-run — the pipeline picks it up automatically).

**Model:** `gemma-4-31b-it` (your specified name) is the default everywhere.
Verify from your machine any time:
`python3 pipeline/aktu_pyq_extractor.py --list-models` (needs a working key;
it prints all gemma models each key can see). If the exact id differs on the
API, override with the `GEMMA_MODEL` env var — the workflow also surfaces
available model names in the health job log.

**Security notes:**
- Your repo is PRIVATE now (verified). The service key can read/wipe your DB —
  it exists only in GitHub Secrets (encrypted) and your local `aktu_pipeline.env`.
- The fine-grained PAT you pasted in chat: go to GitHub → Settings →
  Fine-grained tokens and **Regenerate/Revoke it** after you're comfortable —
  it was shared in plaintext. Same for the AI keys if you ever suspect leakage.
- `aktu_pipeline.env` is git-ignored; never commit it.

---

## 6. How to run everything

**Primary way (no local setup):** GitHub repo → **Actions** tab →
**PYQ Pipeline** → **Run workflow** → pick mode:

| Mode | What it does | AI cost |
|---|---|---|
| `test` | 20 papers: parse → DB → embeddings (no AI) | 0 |
| `pilot` | 20 papers: parse → AI repair → enrich → embed → cluster | ~20 papers of tokens |
| `backlog` | ALL 1,395 papers, everything, 2 parallel shards | biggest |
| `enrich` | skip parsing; only AI-tag papers not yet enriched | medium |
| `embed` | only embeddings + clusters (re-rank after anything) | 0 |

Reruns are always safe (Section 4). A backlog takes a few runs if quotas
hit — each run resumes exactly where the last stopped.

**Local way** (optional, from repo root):
```bash
pip install -r pipeline/requirements.txt   # + tesseract-ocr for scanned papers
cp pipeline/aktu_pipeline.env.example pipeline/aktu_pipeline.env   # fill in
python pipeline/aktu_pyq_extractor.py --stage extract --dir corpus/unstructured \
    --manifest data/ryzenstudy_download_manifest.csv --db --ai
python pipeline/aktu_pyq_extractor.py --stage enrich
python pipeline/aktu_pyq_extractor.py --stage embed
python pipeline/aktu_pyq_extractor.py --stage cluster
```

**Health checks:**
```bash
python pipeline/supabase_check.py      # 15 data-plane checks, ALL GREEN expected
python pipeline/check_keys.py          # which Google keys are alive
python pipeline/aktu_pyq_extractor.py --list-models
```

---

## 7. Where the data goes (who fills what)

| Table.column | Filled by | Notes |
|---|---|---|
| subjects.code/name/course/semester | extract | from filename + manifest `paper_name` |
| papers.file_hash/source_url/storage_url | extract | storage_url = source URL (ryzenstudy) |
| papers.extraction_status/extracted_at/n_questions | extract | the resume engine |
| papers.enriched_at | enrich | set when AI tagging done |
| questions.text/text_normalized/question_hash | extract | exact-dup identity per subject |
| questions.question_type | extract (heuristic) → enrich (AI, validated) | enum: theory/numerical/short/diagram/mcq |
| questions.marks/unit | extract | only when printed/inferable |
| questions.choice_group | extract | OR-alternative pairs share a uuid |
| questions.unit_topic | enrich | AI topic, sanitized, fallback 'general' |
| questions.parent_id | enrich | sub-question → stem linking |
| questions.has_diagram/diagram_url | extract | crop PNG → Storage public URL |
| questions.embedding | embed | 384-dim MiniLM vector |
| questions.extraction_confidence/needs_review | extract | gate + review flag |
| occurrences.* | extract | which paper, which Q-no, which year |
| clusters.* | cluster | freq_count, importance, member_ids, label |

---

## 8. NULL policy — which NULLs are honest (and why that's correct)

You said you don't like NULLs. Correct: **unfilled** NULLs are bad. But some
NULLs are *information* — they mean "this question genuinely has no such
attribute". The pipeline guarantees every row is fully processed; after a
full run the ONLY remaining NULLs are honest ones:

| Column | NULL means | Why we don't fake it |
|---|---|---|
| marks | not printed / not inferable | inventing marks = hallucination |
| unit | paper has no UNIT headers (post-2021 AKTU format) | no unit exists |
| choice_group | not an OR-alternative | most questions aren't |
| parent_id | standalone question, no stem row | most questions aren't sub-parts |
| diagram_url | no diagram on that question | has_diagram=false |
| extraction_error | no error occurred | good news! |

Everything else MUST be filled after a full run: question_type (never NULL,
fallback chain AI→heuristic), unit_topic (fallback 'general'), embedding
(all questions), extraction_confidence, subject/paper provenance. If you
ever see a non-honest NULL, re-run the relevant stage — it is self-healing.

---

## 9. Using the data on your website

The publishable key can read everything (RLS: anon read-only). Golden queries:

```js
// Top repeated questions of a subject (THE core page):
GET {SUPABASE_URL}/rest/v1/repeated_questions?subject_code=eq.KCS-401
    &select=text,times_asked,distinct_years,first_year,last_year,rank_score,
            question_type,unit_topic,marks,cluster_label,importance
    &order=rank_score.desc&limit=50
// headers: apikey: <publishable>, Authorization: Bearer <publishable>
```

- `rank_score` = cluster importance when clustered, else `distinct_years*12 + avg_marks`
- filter `distinct_years=gte.2` → only genuinely repeated questions
- filter `question_type=eq.numerical` / `unit_topic=eq.<topic>` for topic pages
- `subject_coverage` view powers your COMPLETE/PARTIAL/EARLY/COMING badges

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| health job: `User location is not supported` | Gemini API blocked for that key's region | expected in some regions; runners in US work. Test keys from India, not VPN-less EU/other blocked regions |
| `PGRST205 / PGRST202` in logs | PostgREST schema cache stale after DDL | run `NOTIFY pgrst, 'reload schema';` in SQL Editor |
| enrich logs `no GEMMA key(s)` | GEMMA_API_KEYS secret empty | add the secret, re-run `enrich` |
| many `429` in AI logs | quota exhausted on key #1 | automatic — chain rotates to key #2 |
| paper status `failed` in papers table | crash for that PDF | `extraction_error` column says why; re-run any mode to retry |
| `review` papers | AI output failed validation twice | check `pipeline/EXTRACTION_GUIDE.md` playbook; fix manually if rare |
| OCR slow on scanned papers | tesseract rendering | normal; backlog sharding absorbs it |

---

## 11. What you still have to touch

Almost nothing:
1. **Run the workflow** (Actions → PYQ Pipeline → Run workflow → `pilot` first,
   then `backlog`). That's the main loop.
2. **Optional:** add your 3rd Google key to `GEMMA_API_KEYS` when you have it.
3. **Optional:** adjust `coverage_target` per subject (default 8 years) via
   SQL — it feeds the importance formula and the coverage badges.
4. **Optional:** re-run `embed` mode after big additions to refresh rankings.
```
