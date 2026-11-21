#!/usr/bin/env python3
"""Read-only introspection for the subject_directory build (owner ask 2026-11-21).
Answers: (a) PROD repeats trio schema + pgvector availability;
(b) RAW papers/questions subject_code coverage; (c) embedding-related objects."""
import sys, json
sys.path.insert(0, "/home/z/my-project/scripts")
from v6_cron_cycle import DSN as RAW_DSN
import psycopg2

PROD_DSN = "postgresql://postgres.dlkerzaatqwqwbxeyrho:repeatrankprod@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"

def probe(name, dsn):
    c = psycopg2.connect(dsn, connect_timeout=30, application_name="subject_dir_probe")
    cur = c.cursor()
    out = {}
    cur.execute("select extname from pg_extension order by 1")
    out["extensions"] = [r[0] for r in cur.fetchall()]
    cur.execute("select name || ' ' || default_version from pg_available_extensions where name='vector'")
    out["vector_available"] = [r[0] for r in cur.fetchall()]
    cur.execute("""select table_name from information_schema.tables
                   where table_schema='public' order by 1""")
    out["tables"] = [r[0] for r in cur.fetchall()]
    # columns of repeats-ish tables
    for t in out["tables"]:
        if "repeat" in t or t == "sync_state":
            cur.execute("""select column_name, data_type from information_schema.columns
                           where table_schema='public' and table_name=%s order by ordinal_position""", (t,))
            out[t] = cur.fetchall()
    # functions mentioning embed
    cur.execute("""select proname, pg_get_function_arguments(oid) from pg_proc
                   where pronamespace='public'::regnamespace and (proname ilike '%embed%' or proname ilike '%match%')""")
    out["embed_functions"] = cur.fetchall()
    if "papers" in out["tables"]:
        cur.execute("select count(*) from papers")
        out["papers"] = cur.fetchone()[0]
    print(f"=== {name} ===")
    print(json.dumps(out, indent=1, default=str)[:3500])
    c.close()
    return out

r = probe("RAW", RAW_DSN)
p = probe("PROD", PROD_DSN)

# RAW code coverage for the directory
c = psycopg2.connect(RAW_DSN, connect_timeout=30, application_name="subject_dir_probe")
cur = c.cursor()
cur.execute("select status, count(*) from papers group by 1")
print("RAW papers by status:", cur.fetchall())
cur.execute("select count(*) from questions")
print("RAW questions:", cur.fetchone()[0])
cur.execute("""select column_name from information_schema.columns
               where table_schema='public' and table_name='questions' and column_name ilike '%code%'""")
print("questions code cols:", cur.fetchall())
cur.execute("""select column_name from information_schema.columns
               where table_schema='public' and table_name='papers' order by ordinal_position""")
print("papers cols:", [r[0] for r in cur.fetchall()])
c.close()
