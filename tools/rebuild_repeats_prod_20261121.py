#!/usr/bin/env python3
"""REBUILD PROD repeats trio from GOLD-VERIFIED data only (owner order 2026-11-21:
"recreate the repeats thing in prod for my app to show today's data").

Source of truth:
  - RAW papers/questions/occurrences/clusters (read-only)
  - GOLD set = strict COMPLETE-RULE-OK papers from gold_census_20261121
    (text band: pdftotext re-verify, zero flags; scan band: tesseract count==DB, zero flags)
  - Subject metadata: upload/syllabus_extreme_rich (6).csv (owner: "rely on this CSV,
    not the coin sieve"); RAW subjects table only as annotated fallback for codes the
    CSV does not know.

Repeat definitions (E5 semantics, gold-filtered):
  1. clusters with >= 2 gold occurrences across their member questions
  2. verbatim questions with >= 2 gold papers, not in any cluster
Re-runnable: TRUNCATE + INSERT, idempotent.
"""
import json, os, re, sys, uuid, collections
from datetime import datetime, timezone
import csv
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN as RAW_DSN
import psycopg2
from psycopg2.extras import execute_values

PROD_DSN = "postgresql://postgres.dlkerzaatqwqwbxeyrho:repeatrankprod@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"
OUT = "/home/z/my-project/scripts/out/repeats_rebuild_20261121.json"
GOLD_FILE = "/home/z/my-project/scripts/out/gold_pids_20261121.json"
CSV_MAP = "/home/z/my-project/scripts/out/csv_code_map_20261121.json"
FALLBACK = "/home/z/my-project/scripts/out/raw_subjects_fallback_20261121.json"
UNIVERSITY = "Dr. A.P.J. Abdul Kalam Technical University, Lucknow"

def norm_code(c):
    return re.sub(r"[^A-Z0-9]", "", str(c).upper()) if c else ""

def y2int(y):
    try:
        return int(str(y).split("-")[0])
    except Exception:
        return None

def mode(xs):
    xs = [x for x in xs if x is not None]
    return collections.Counter(xs).most_common(1)[0][0] if xs else None

R = {"run": "REPEATS-GOLD-20261121", "ts": datetime.now(timezone.utc).isoformat()}

# ---------------- inputs ----------------
gold = set(json.load(open(GOLD_FILE))["gold_pids"])
code2sub = json.load(open(CSV_MAP))
fallback = json.load(open(FALLBACK))
R["gold_papers"] = len(gold)

conn = psycopg2.connect(RAW_DSN, connect_timeout=30, application_name="repeats_gold_build")
cur = conn.cursor()

# papers meta
cur.execute("SELECT id, subject_code, year, source FROM papers")
pinfo = {str(r[0]): {"code": r[1], "year": r[2], "src": r[3]} for r in cur.fetchall()}
print("papers:", len(pinfo), flush=True)

# clusters
cur.execute("SELECT id, subject_code, label, member_ids FROM clusters")
clusters = []
for cid, sc, label, members in cur.fetchall():
    if isinstance(members, str):
        ms = [x.strip() for x in members.strip("{}").split(",") if x.strip()]
    else:
        ms = [str(m) for m in (members or [])]
    clusters.append((str(cid), label, ms))
print("clusters:", len(clusters), flush=True)
cluster_members = set()
for _, _, ms in clusters:
    cluster_members.update(ms)

# all questions of interest (cluster members)
qids_needed = list(cluster_members)
qinfo = {}
CH = 3000
for i in range(0, len(qids_needed), CH):
    cur.execute("""SELECT id, text, question_type::text, marks, has_diagram, unit
                   FROM questions WHERE id = ANY(%s::uuid[])""", (qids_needed[i:i + CH],))
    for qid, text, qt, mk, dg, un in cur.fetchall():
        qinfo[str(qid)] = {"text": text or "", "type": qt, "marks": mk, "diag": bool(dg), "unit": un}
print("cluster member questions:", len(qinfo), flush=True)

# occurrences of member questions (all papers), filter gold
occ_of = collections.defaultdict(list)
for i in range(0, len(qids_needed), CH):
    cur.execute("""SELECT o.question_id, o.id, o.paper_id, o.year, o.q_no, o.marks
                   FROM occurrences o WHERE o.question_id = ANY(%s::uuid[])""",
                (qids_needed[i:i + CH],))
    for qid, oid, pid, yr, qno, mk in cur.fetchall():
        occ_of[str(qid)].append((str(qid), str(oid), str(pid), yr, qno, mk))
print("member occurrence links (all papers):", sum(len(v) for v in occ_of.values()), flush=True)

# verbatim repeats: >=2 gold papers, not in any cluster
cur.execute("""SELECT o.question_id FROM occurrences o
               WHERE o.paper_id = ANY(%s::uuid[])
               GROUP BY o.question_id HAVING count(DISTINCT o.paper_id) >= 2""", (list(gold),))
verb_qids = [str(r[0]) for r in cur.fetchall() if str(r[0]) not in cluster_members]
print("verbatim gold questions (outside clusters):", len(verb_qids), flush=True)
need2 = [q for q in verb_qids if q not in qinfo]
for i in range(0, len(need2), CH):
    cur.execute("""SELECT id, text, question_type::text, marks, has_diagram, unit
                   FROM questions WHERE id = ANY(%s::uuid[])""", (need2[i:i + CH],))
    for qid, text, qt, mk, dg, un in cur.fetchall():
        qinfo[str(qid)] = {"text": text or "", "type": qt, "marks": mk, "diag": bool(dg), "unit": un}
occ_of_v = collections.defaultdict(list)
for i in range(0, len(verb_qids), CH):
    cur.execute("""SELECT o.question_id, o.id, o.paper_id, o.year, o.q_no, o.marks
                   FROM occurrences o WHERE o.question_id = ANY(%s::uuid[])
                   AND o.paper_id = ANY(%s::uuid[])""",
                (verb_qids[i:i + CH], list(gold)))
    for qid, oid, pid, yr, qno, mk in cur.fetchall():
        occ_of_v[str(qid)].append((str(qid), str(oid), str(pid), yr, qno, mk))
conn.close()

def gold_occ_for(members):
    out = []
    for q in members:
        for o in occ_of.get(q, []):
            if o[2] in gold:
                out.append(o)
    return out

# ---------------- build repeat definitions ----------------
rep_defs = []  # (cluster_id|None, members, label)
kept_clusters = dropped_clusters = 0
for cid, label, ms in clusters:
    go = gold_occ_for(ms)
    if len(go) >= 2:
        rep_defs.append((cid, ms, label))
        kept_clusters += 1
    else:
        dropped_clusters += 1
verb_kept = 0
for q in verb_qids:
    go = occ_of_v.get(q, [])
    if len(go) >= 2:
        rep_defs.append((None, [q], None))
        verb_kept += 1
R["clusters_kept"] = kept_clusters
R["clusters_dropped_no_gold_evidence"] = dropped_clusters
R["verbatim_kept"] = verb_kept
R["repeats_total"] = len(rep_defs)
print("repeat defs:", len(rep_defs), f"(clusters {kept_clusters} + verbatim {verb_kept})", flush=True)

# ---------------- assemble rows ----------------
repeats_rows, member_rows = [], []
csv_hits = fb_hits = no_meta = 0
for cluster_id, ms, label in rep_defs:
    rid = str(uuid.uuid4())
    occ = []
    for q in ms:
        occ.extend(occ_of.get(q, []) if cluster_id else occ_of_v.get(q, []))
    occ = [o for o in occ if o[2] in gold]
    if len(occ) < 2:
        continue
    codes = [pinfo[o[2]]["code"] for o in occ if pinfo.get(o[2])]
    code = mode(codes) or (qinfo.get(ms[0], {}).get("text") and None) or None
    if not code:
        continue
    nc = norm_code(code)
    meta = code2sub.get(nc)
    if meta:
        csv_hits += 1
    else:
        meta = fallback.get(nc)
        if meta:
            fb_hits += 1
        else:
            no_meta += 1
    texts = list({qinfo[q]["text"] for q in ms if q in qinfo and qinfo[q]["text"]})
    gold_members = [q for q in ms if any(o[0] == q for o in occ)] if cluster_id else ms
    years = sorted({y2int(o[3]) for o in occ if y2int(o[3])})
    papers_set = {o[2] for o in occ}
    # consecutive streak ending at the repeat's latest year
    streak = 0
    if years:
        ys = set(years)
        k = max(years)
        while k in ys:
            streak += 1
            k -= 1
    gq = [qinfo[q] for q in gold_members if q in qinfo]
    branches = (meta or {}).get("branches") or []
    branch1 = branches[0] if branches else None
    semester = (meta or {}).get("semester")
    course = (meta or {}).get("course")
    repeats_rows.append({
        "id": rid, "cluster_id": cluster_id,
        "canonical_text": label or (max(texts, key=len) if texts else ""),
        "variants": json.dumps(texts[:12], ensure_ascii=False),
        "variant_count": len(texts),
        "subject_code": code,
        "subject_name": (meta or {}).get("subject_name"),
        "course": course,
        "branch": branch1,
        "semester": semester if isinstance(semester, int) else None,
        "units": json.dumps((meta or {}).get("units") or [], ensure_ascii=False),
        "question_type": mode([g["type"] for g in gq]),
        "typical_marks": mode([g["marks"] for g in gq]),
        "has_diagram": any(g["diag"] for g in gq),
        "university": UNIVERSITY,
        "occurrences_count": len(occ),
        "papers_count": len(papers_set),
        "years": years,
        "years_count": len(years),
        "year_first": years[0] if years else None,
        "year_last": years[-1] if years else None,
        "exam_sessions": json.dumps([]),
        "branches_seen": json.dumps(branches[:20], ensure_ascii=False),
        "courses_seen": json.dumps([course] if course else [], ensure_ascii=False),
        "semesters_seen": json.dumps([semester] if semester else []),
        "_streak": streak,
        "_scope": "in_scope" if nc in code2sub else None,
        "_sid": (meta or {}).get("subject_sid") if meta else None,
        "_csv": bool(nc in code2sub),
    })
    for q in gold_members:
        for o in occ:
            if o[0] != q:
                continue
            p = pinfo.get(o[2], {})
            member_rows.append((rid, q, o[1], o[2], p.get("code"), o[3], None,
                                qinfo.get(q, {}).get("unit"), o[5], o[4],
                                branch1, course,
                                semester if isinstance(semester, int) else None,
                                None, p.get("src")))
R["csv_meta_hits"] = csv_hits
R["fallback_meta_hits"] = fb_hits
R["no_meta"] = no_meta
R["member_rows"] = len(member_rows)
print("repeat rows:", len(repeats_rows), "member rows:", len(member_rows), flush=True)

# ---------------- scores (within subject; E5 formulas) ----------------
bysubj = collections.defaultdict(list)
for r in repeats_rows:
    bysubj[r["subject_code"]].append(r)
for code, rs in bysubj.items():
    max_occ = max(r["occurrences_count"] for r in rs) or 1
    max_yrs = max(r["years_count"] for r in rs) or 1
    max_stk = max(r["_streak"] for r in rs) or 1
    subj_last = max((r["year_last"] or 0) for r in rs) or 0
    for r in rs:
        r["importance_score"] = round(100 * (0.45 * r["occurrences_count"] / max_occ
                                             + 0.25 * r["years_count"] / max_yrs
                                             + 0.15 * 1.0
                                             + 0.15 * r["_streak"] / max_stk), 2)
        decay = sum(0.65 ** max(subj_last - y, 0) for y in r["years"])
        denom = max(max(sum(0.65 ** max(subj_last - y, 0) for y in rr["years"]) for rr in rs), 1)
        bonus = 1.15 if r["year_last"] and subj_last - r["year_last"] <= 1 else 1.0
        r["upcoming_possibility"] = round(min(100, 100 * min(1.0, decay / denom) * bonus), 2)
for r in repeats_rows:
    r.pop("_streak")

# ---------------- write PROD ----------------
print("connecting PROD...", flush=True)
pc = psycopg2.connect(PROD_DSN, connect_timeout=30, application_name="repeats_gold_build")
pc.autocommit = False
pcu = pc.cursor()
pcu.execute("SET statement_timeout = '20min'")
pcu.execute("TRUNCATE repeat_members, repeats, repeat_facets")
now = datetime.now(timezone.utc)
execute_values(pcu, """INSERT INTO repeats
    (id, cluster_id, canonical_text, variants, variant_count, subject_code, subject_name,
     course, branch, semester, units, question_type, typical_marks, has_diagram, university,
     occurrences_count, papers_count, years, years_count, year_first, year_last,
     exam_sessions, branches_seen, courses_seen, semesters_seen,
     importance_score, upcoming_possibility, built_at, scope, subject_sid) VALUES %s""",
    [(r["id"], r["cluster_id"], r["canonical_text"], r["variants"], r["variant_count"],
      r["subject_code"], r["subject_name"], r["course"], r["branch"], r["semester"],
      r["units"], r["question_type"], r["typical_marks"], r["has_diagram"], r["university"],
      r["occurrences_count"], r["papers_count"], r["years"], r["years_count"],
      r["year_first"], r["year_last"], r["exam_sessions"], r["branches_seen"],
      r["courses_seen"], r["semesters_seen"], r["importance_score"],
      r["upcoming_possibility"], now, r["_scope"], r["_sid"]) for r in repeats_rows],
    page_size=800)
print("repeats inserted", flush=True)
execute_values(pcu, """INSERT INTO repeat_members
    (repeat_id, question_id, occurrence_id, paper_id, paper_code, exam_year, exam_session,
     unit, marks_observed, q_no, branch, course, semester, scheme, source) VALUES %s""",
    member_rows, page_size=3000)
print("members inserted", flush=True)
pcu.execute("""INSERT INTO repeat_facets
    (subject_code, unit, question_type, branch, exam_year, repeats_count, occurrences_count)
    SELECT r.subject_code, coalesce(CAST(m.unit AS text), '?'), coalesce(r.question_type, '?'),
           coalesce(r.branch, '?'), coalesce(m.exam_year, '?'),
           count(DISTINCT m.repeat_id), count(*)
    FROM repeat_members m JOIN repeats r ON r.id = m.repeat_id
    GROUP BY 1, 2, 3, 4, 5""")
# public read policies (idempotent)
pcu.execute("""
alter table repeats        enable row level security;
alter table repeat_members enable row level security;
alter table repeat_facets  enable row level security;
drop policy if exists "public read repeats"        on repeats;
drop policy if exists "public read repeat_members" on repeat_members;
drop policy if exists "public read repeat_facets"  on repeat_facets;
create policy "public read repeats"        on repeats        for select to anon, authenticated using (true);
create policy "public read repeat_members" on repeat_members for select to anon, authenticated using (true);
create policy "public read repeat_facets"  on repeat_facets  for select to anon, authenticated using (true);
""")
pcu.execute("""INSERT INTO sync_state (table_name, rows_synced, synced_at)
               VALUES ('repeats', %s, now())
               ON CONFLICT (table_name) DO UPDATE SET rows_synced = EXCLUDED.rows_synced, synced_at = now()""",
            (len(repeats_rows),))
pc.commit()
print("PROD committed", flush=True)

# verify
pcu.execute("select count(*) from repeats")
R["prod_repeats"] = pcu.fetchone()[0]
pcu.execute("select count(*) from repeat_members")
R["prod_members"] = pcu.fetchone()[0]
pcu.execute("select count(*) from repeat_facets")
R["prod_facets"] = pcu.fetchone()[0]
pcu.execute("select count(distinct subject_code) from repeats")
R["prod_subjects"] = pcu.fetchone()[0]
pcu.execute("select count(*) from repeats where importance_score >= 80")
R["high_importance"] = pcu.fetchone()[0]
pcu.execute("select count(*) from repeats where scope='in_scope'")
R["csv_in_scope"] = pcu.fetchone()[0]
pcu.execute("select pg_size_pretty(pg_database_size(current_database()))")
R["prod_db_size"] = pcu.fetchone()[0]
pc.commit()
pc.close()
json.dump(R, open(OUT, "w"), indent=1, default=str)
print(json.dumps(R, indent=1, default=str))
