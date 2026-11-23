#!/usr/bin/env python3
"""BUILD subject_directory in PROD (owner ask 2026-11-21):
"CSVs subjects with 'ALL code' and subject name in a new table with associated
content availability (has repeat questions and papers or not) and the subject
name embeddings for search."

Source of truth:
  - upload/syllabus_extreme_rich (6).csv  -> 3,080 subject rows (subject_id,
    subject_name, primary_code, all_codes, course_name, branch_names)
  - RAW papers/questions (read-only)      -> paper/question availability per code
  - PROD repeats (gold-only trio)         -> repeat availability per subject_sid
    (in_scope) or per normalized subject_code (343 fallback rows, sid NULL)

Search: subject_name embedding, all-MiniLM-L6-v2, 384 dims, cosine HNSW index.
Code lookup: all_codes_norm (upper, alnum-only) with GIN index.
Idempotent: CREATE TABLE IF NOT EXISTS + TRUNCATE + INSERT, single transaction
per stage; additive table only - touches nothing else in PROD.
"""
import csv, json, re, sys, collections
from datetime import datetime, timezone
import numpy as np
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN as RAW_DSN
import psycopg2
from psycopg2.extras import execute_values

PROD_DSN = "postgresql://postgres.dlkerzaatqwqwbxeyrho:repeatrankprod@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"
CSV_PATH = "/home/z/my-project/upload/syllabus_extreme_rich (6).csv"
OUT_JSON = "/home/z/my-project/scripts/out/subject_directory_20261121.json"
OUT_CSV = "/home/z/my-project/download/subject_directory_20261121.csv"

def norm_code(c):
    return re.sub(r"[^A-Z0-9]", "", str(c).upper()) if c else ""

def y2int(y):
    try:
        return int(str(y).split("-")[0])
    except Exception:
        return None

R = {"run": "SUBJECT-DIRECTORY-20261121", "ts": datetime.now(timezone.utc).isoformat()}

# ---------------- CSV subjects ----------------
with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
    rows = [r for r in csv.DictReader(f) if r["row_kind"] == "subject"]
R["csv_subjects"] = len(rows)
codes_of = {}
subjects = []
for r in rows:
    sid = r["subject_id"].strip()
    allc = [c.strip() for c in r["all_codes"].split(";") if c.strip()]
    alln = sorted({norm_code(c) for c in allc if norm_code(c)})
    subjects.append({
        "subject_id": sid,
        "subject_name": r["subject_name"].strip(),
        "primary_code": r["primary_code"].strip(),
        "all_codes": allc,
        "all_codes_norm": alln,
        "course_name": r.get("course_name", "").strip(),
        "branch_names": r.get("branch_names", "").strip(),
    })
    for n in alln:
        codes_of.setdefault(n, set()).add(sid)
R["distinct_codes"] = len(codes_of)
print("subjects:", len(subjects), "distinct norm codes:", len(codes_of), flush=True)

# ---------------- RAW availability (read-only) ----------------
rc = psycopg2.connect(RAW_DSN, connect_timeout=30, application_name="subject_dir_build")
rcur = rc.cursor()
rcur.execute("SELECT subject_code, extraction_status, year FROM papers")
papers_by, complete_by = collections.defaultdict(int), collections.defaultdict(int)
ymin_by, ymax_by = collections.defaultdict(lambda: None), collections.defaultdict(lambda: None)
for code, status, year in rcur.fetchall():
    n = norm_code(code)
    if not n:
        continue
    papers_by[n] += 1
    if status == "complete":
        complete_by[n] += 1
    y = y2int(year)
    if y:
        ymin_by[n] = y if ymin_by[n] is None else min(ymin_by[n], y)
        ymax_by[n] = y if ymax_by[n] is None else max(ymax_by[n], y)
rcur.execute("SELECT subject_code, count(*) FROM questions GROUP BY 1")
q_by = {norm_code(c): q for c, q in rcur.fetchall() if norm_code(c)}
rc.close()
print("paper codes:", len(papers_by), "question codes:", len(q_by), flush=True)

# ---------------- PROD repeats availability ----------------
pc = psycopg2.connect(PROD_DSN, connect_timeout=30, application_name="subject_dir_build")
pcur = pc.cursor()
pcur.execute("""SELECT subject_sid, subject_code, count(*), max(papers_count), max(years_count)
                FROM repeats GROUP BY 1, 2""")
rep_by_sid, rep_by_code = collections.defaultdict(int), collections.defaultdict(int)
maxp_by_sid, maxy_by_sid, maxp_by_code = {}, {}, {}
fallback_mapped = fallback_unmapped = 0
for sid, code, cnt, maxp, maxy in pcur.fetchall():
    if sid:  # in_scope rows: csv subject_id
        rep_by_sid[sid] += cnt
        maxp_by_sid[sid] = max(maxp_by_sid.get(sid, 0), maxp or 0)
        maxy_by_sid[sid] = max(maxy_by_sid.get(sid, 0), maxy or 0)
    else:    # fallback rows: map via normalized code family
        n = norm_code(code)
        if n in codes_of:
            fallback_mapped += cnt
            rep_by_code[n] += cnt
            maxp_by_code[n] = max(maxp_by_code.get(n, 0), maxp or 0)
        else:
            fallback_unmapped += cnt
R["fallback_repeats_mapped"] = fallback_mapped
R["fallback_repeats_unmapped"] = fallback_unmapped
print("fallback repeats mapped/unmapped:", fallback_mapped, "/", fallback_unmapped, flush=True)

# ---------------- build rows ----------------
out = []
for s in subjects:
    sid = s["subject_id"]
    p = sum(papers_by.get(n, 0) for n in s["all_codes_norm"])
    pcnt = sum(complete_by.get(n, 0) for n in s["all_codes_norm"])
    q = sum(q_by.get(n, 0) for n in s["all_codes_norm"])
    rr = rep_by_sid.get(sid, 0)
    mp, my = maxp_by_sid.get(sid, 0), maxy_by_sid.get(sid, 0)
    if rr == 0:  # only fallback rows used code mapping
        rr = sum(rep_by_code.get(n, 0) for n in s["all_codes_norm"])
        mp = max(maxp_by_code.get(n, 0) for n in s["all_codes_norm"]) if s["all_codes_norm"] else 0
    ys = [ymin_by[n] for n in s["all_codes_norm"] if ymin_by.get(n)]
    ye = [ymax_by[n] for n in s["all_codes_norm"] if ymax_by.get(n)]
    out.append((sid, s["subject_name"], s["subject_name"].lower(), s["primary_code"],
                s["all_codes"], s["all_codes_norm"], s["course_name"], s["branch_names"],
                p, pcnt, q, rr, mp, my,
                min(ys) if ys else None, max(ye) if ye else None,
                p > 0, rr > 0))
R["with_papers"] = sum(1 for o in out if o[16])
R["with_repeats"] = sum(1 for o in out if o[17])
R["with_questions"] = sum(1 for o in out if o[10] > 0)
print("rows with papers:", R["with_papers"], "| with repeats:", R["with_repeats"], flush=True)

# ---------------- embeddings ----------------
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
names = [o[1] for o in out]
emb = model.encode(names, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
R["embedding_dim"] = int(emb.shape[1])
print("embeddings:", emb.shape, flush=True)

# ---------------- PROD write (additive table) ----------------
pcur.execute("CREATE EXTENSION IF NOT EXISTS vector")
pcur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
pcur.execute("""
CREATE TABLE IF NOT EXISTS subject_directory (
  subject_id text PRIMARY KEY,
  subject_name text NOT NULL,
  subject_name_norm text NOT NULL,
  primary_code text,
  all_codes text[] DEFAULT '{}',
  all_codes_norm text[] DEFAULT '{}',
  course_name text,
  branch_names text,
  papers_count integer NOT NULL DEFAULT 0,
  complete_papers_count integer NOT NULL DEFAULT 0,
  questions_count integer NOT NULL DEFAULT 0,
  repeat_rows_count integer NOT NULL DEFAULT 0,
  repeat_max_papers integer NOT NULL DEFAULT 0,
  repeat_max_years integer NOT NULL DEFAULT 0,
  years_min integer,
  years_max integer,
  has_papers boolean NOT NULL DEFAULT false,
  has_repeats boolean NOT NULL DEFAULT false,
  name_embedding vector(384),
  updated_at timestamptz NOT NULL DEFAULT now()
)""")
pcur.execute("TRUNCATE subject_directory")
data = []
for o, e in zip(out, emb):
    data.append(o + ("[" + ",".join(f"{x:.6f}" for x in e.tolist()) + "]",))
execute_values(pcur, """INSERT INTO subject_directory
  (subject_id, subject_name, subject_name_norm, primary_code, all_codes, all_codes_norm,
   course_name, branch_names, papers_count, complete_papers_count, questions_count,
   repeat_rows_count, repeat_max_papers, repeat_max_years, years_min, years_max,
   has_papers, has_repeats, name_embedding)
  VALUES %s""", data, template="""(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)""",
  page_size=200)
pcur.execute("CREATE INDEX IF NOT EXISTS subject_directory_emb_hnsw ON subject_directory USING hnsw (name_embedding vector_cosine_ops)")
pcur.execute("CREATE INDEX IF NOT EXISTS subject_directory_codes_gin ON subject_directory USING gin (all_codes_norm)")
pcur.execute("CREATE INDEX IF NOT EXISTS subject_directory_norm_trgm ON subject_directory USING gin (subject_name_norm gin_trgm_ops)")
pcur.execute("ALTER TABLE subject_directory ENABLE ROW LEVEL SECURITY")
pcur.execute("DROP POLICY IF EXISTS subject_directory_public_read ON subject_directory")
pcur.execute("CREATE POLICY subject_directory_public_read ON subject_directory FOR SELECT TO anon, authenticated USING (true)")
pc.commit()
R["inserted"] = len(data)
print("inserted:", len(data), flush=True)

# ---------------- verify ----------------
pcur.execute("SELECT count(*), count(name_embedding) FROM subject_directory")
R["verify_count"], R["verify_emb"] = pcur.fetchone()
pcur.execute("SELECT has_papers, has_repeats, count(*) FROM subject_directory GROUP BY 1, 2 ORDER BY 1, 2")
R["avail_matrix"] = pcur.fetchall()
pcur.execute("""SELECT subject_name, 1 - (name_embedding <=> (SELECT name_embedding FROM subject_directory WHERE subject_id='data-structure')) AS sim
               FROM subject_directory ORDER BY sim DESC LIMIT 6""")
R["nn_data_structure"] = pcur.fetchall()
pcur.execute("SELECT sum(repeat_rows_count) FROM subject_directory")
R["repeat_rows_mapped"] = pcur.fetchone()[0]
pc.close()

# snapshot CSV for owner
with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["subject_id", "subject_name", "subject_name_norm", "primary_code", "all_codes",
                "all_codes_norm", "course_name", "branch_names", "papers_count",
                "complete_papers_count", "questions_count",
                "repeat_rows_count", "repeat_max_papers", "repeat_max_years",
                "years_min", "years_max", "has_papers", "has_repeats"])
    for o in out:
        w.writerow(o[:18])

json.dump(R, open(OUT_JSON, "w"), indent=1, default=str)
print(json.dumps(R, indent=1, default=str))
