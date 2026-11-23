# AKTU-PYQ

Most-repeated AKTU university questions, mined from **1,395 past papers** (BTech / BPharm / MCA).

| Folder | What's inside |
|---|---|
| `corpus/` | the raw paper PDFs (private) — `unstructured/` feeds the pipeline |
| `pipeline/` | extraction pipeline + **the owner guide (`pipeline/README.md`)** |
| `data/` | manifests, inventory, reconcile reports |
| `tools/` | scraper, reconciler, inventory updater |
| `backend/` `frontend/` | web apps |

**Run the pipeline:** Actions tab -> "PYQ Pipeline" -> Run workflow
(`test` = 20 papers no AI, `pilot` = 20 full, `backlog` = everything).
Reruns are always safe — finished papers are skipped automatically.

**Docs:** start with `pipeline/README.md` (what each stage does, idempotency,
NULL policy, website query recipes, troubleshooting).
