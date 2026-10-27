#!/usr/bin/env python3
"""V6 cron cycle (owner order 2026-10-27): AUDIT all platforms -> fix/flag ->
if last run GOOD, dispatch the next GHA extraction run. Never stacks runs.

Platforms audited:
  1. GHA   harshvardhan9084/gemma-nebula extraction.yml (repo B, ref aktu-runner)
  2. RAW   Supabase qnakqtiokspzoopivfyl (papers/questions/failed classes)
  3. QUOTA RPD inference (07:05Z daily reset; run logs pulled only on failure)
  4. REPO  repo_b checkout vs origin (fix commits present: 3c61451 unit, f65b0db rpd-bias)

Verdicts:
  DISPATCHED <run_id>  last run good -> next run queued
  RUNNING              last run still active -> audit only, no dispatch
  FLAG:<reasons>       problems found -> dispatch held, diagnosis printed
  DRAINED              backlog exhausted -> next job = reprocess campaign (dispatched)

State: /home/z/my-project/scripts/out/v6_cron_state.json
Cycle log: /home/z/my-project/scripts/out/v6_cron_cycles.log (append)
"""
import json, os, subprocess, sys, time, urllib.request

PAT = open("/home/z/my-project/.secrets/gh_pat.txt").read().strip()
REPO = "harshvardhan9084/gemma-nebula"
REF = "aktu-runner"
HDR = {"Authorization": f"Bearer {PAT}", "Accept": "application/vnd.github+json"}
OUT_DIR = "/home/z/my-project/scripts/out"
STATE_F = f"{OUT_DIR}/v6_cron_state.json"
LOG_F = f"{OUT_DIR}/v6_cron_cycles.log"
REPO_B = "/home/z/my-project/repo_b"
KNOWN_FAIL_SIGS = ("400 Client Error: Bad Request for url: https://qnakqtiokspzoopivfyl.supabase.co/rest/v1/qu",)

DISPATCH_INPUTS = {
    "stage": "extract", "extractor": "v6",
    "pdf_dir": "corpus/hunt5,corpus/aktuonline4",
    "manifest": "data/hunt5_extractor_manifest.csv,data/aktuonline4_manifest.csv",
    "pilot_n": "0", "limit": "0", "course": "", "year": "",
    "reprocess": "false", "status_filter": "complete,review,failed",
    "total_calls": "2000", "min_interval_s": "", "route": "auto",
    "qc_min_questions": "10", "chunk_pages": "2", "dry_run": "false", "not_before": "",
}
REPROCESS_INPUTS = dict(DISPATCH_INPUTS, **{
    "pdf_dir": "corpus/unified,corpus/aktuonline3,corpus/aktuonline4,corpus/hunt5",
    "manifest": "data/unified_manifest.csv,data/aktuonline3_manifest.csv,data/aktuonline4_manifest.csv,data/hunt5_extractor_manifest.csv",
    "reprocess": "true", "status_filter": "complete,review,failed",
})

def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"https://api.github.com{path}", headers=HDR,
                                 data=data, method=method)
    return json.load(urllib.request.urlopen(req, timeout=30))

def load_state():
    if os.path.exists(STATE_F):
        return json.load(open(STATE_F))
    return {"cycles": 0, "last_run_id": 40382351405, "papers_total": 9458,
            "questions_total": 182891, "zero_delta_streak": 0}

def save_state(s):
    json.dump(s, open(STATE_F, "w"), indent=1, default=str)

def cycle_log(msg):
    with open(LOG_F, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}\n")

# ---------------- DB ----------------
import psycopg2
DSN = "postgresql://postgres.qnakqtiokspzoopivfyl:repeatrankpyq@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"

def db_audit(prev):
    conn = psycopg2.connect(DSN, connect_timeout=30); conn.autocommit = True
    cur = conn.cursor()
    a = {}
    cur.execute("SELECT count(*) FROM papers"); a["papers"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM questions"); a["questions"] = cur.fetchone()[0]
    cur.execute("""SELECT extraction_status, count(*) FROM papers GROUP BY 1""")
    a["status"] = dict(cur.fetchall())
    # new failed since previous cycle -> error signature classes
    since = prev.get("checked_at", "2026-10-27 00:00:00+00")
    cur.execute("""SELECT left(extraction_error,60), count(*) FROM papers
                   WHERE ingested_at > %s AND extraction_status='failed'
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 5""", (since,))
    a["new_fail_classes"] = cur.fetchall()
    # unit-400 class remaining (should drain to 0 after the fixed run)
    cur.execute("""SELECT count(*) FROM papers WHERE extraction_status='failed'
                   AND extraction_error LIKE '%%400 Client Error%%'""")
    a["unit400_failed_remaining"] = cur.fetchone()[0]
    # of those, how many were actually RETRIED in this window and failed AGAIN?
    # (ingested_at is upserted on every attempt, so ingested_at > since == touched)
    # fix-disproof signal = refailed > 0. Untouched leftovers are pending-retry
    # (in-scope) or legacy out-of-scope -> healed by the reprocess campaign.
    cur.execute("""SELECT count(*) FROM papers WHERE extraction_status='failed'
                   AND extraction_error LIKE '%%400 Client Error%%' AND ingested_at > %s""", (since,))
    a["unit400_refailed_since"] = cur.fetchone()[0]
    # review ratio of new batch
    cur.execute("""SELECT count(*) FILTER (WHERE extraction_status='review'),
                          count(*) FILTER (WHERE extraction_status='complete')
                   FROM papers WHERE ingested_at > %s""", (since,))
    a["new_review_complete"] = cur.fetchone()
    conn.close()
    a["d_papers"] = a["papers"] - prev.get("papers_total", a["papers"])
    a["d_questions"] = a["questions"] - prev.get("questions_total", a["questions"])
    return a

# ---------------- GHA ----------------
def gha_audit():
    g = {}
    runs = api(f"/repos/{REPO}/actions/workflows/extraction.yml/runs?per_page=3")["workflow_runs"]
    g["runs"] = [{k: r[k] for k in ("id", "run_number", "status", "conclusion",
                   "run_started_at", "updated_at")} for r in runs]
    g["latest"] = g["runs"][0] if g["runs"] else None
    return g

def repo_audit():
    r = {}
    subprocess.run(["git", "-C", REPO_B, "fetch", "-q", "origin", REF], capture_output=True)
    local = subprocess.run(["git", "-C", REPO_B, "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    remote = subprocess.run(["git", "-C", REPO_B, "rev-parse", f"origin/{REF}"],
                            capture_output=True, text=True).stdout.strip()
    r["local_head"], r["remote_head"] = local[:7], remote[:7]
    r["synced"] = local == remote
    for name, needle in (("unit_fix_3c61451", "unit_int"), ("rpd_bias_f65b0db", "RPD_WEIGHTS")):
        src = open(f"{REPO_B}/pipeline/aktu_pyq_extractor_v6.py").read() \
              if name == "unit_fix_3c61451" else \
              open(f"{REPO_B}/pipeline/aktu_pyq_extractor.py").read()
        r[name] = needle in src
    # commit time of the unit fix — a run that STARTED after this has had its
    # chance to prove the fix; older runs' failures are pending-retry, not flags
    try:
        c = api(f"/repos/{REPO}/commits/3c61451")
        r["fix_time"] = c["commit"]["committer"]["date"]
    except Exception:
        r["fix_time"] = "2026-10-27T14:00:00Z"
    return r

def main():
    dry = os.environ.get("DRY") == "1"   # DRY=1: audit + verdict only, never dispatch
    state = load_state()
    state["cycles"] = state.get("cycles", 0) + 1
    cyc = state["cycles"]
    print(f"===== V6 CRON CYCLE #{cyc} — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} =====")
    flags = []

    g = gha_audit()
    latest = g["latest"]
    print(f"[GHA] latest run #{latest['run_number']} id={latest['id']} "
          f"{latest['status']}/{latest['conclusion']} (started {latest['run_started_at']})")
    for r in g["runs"][1:]:
        print(f"      prior  #{r['run_number']} {r['status']}/{r['conclusion']}")

    a = db_audit(state)
    print(f"[RAW] papers {a['papers']} ({a['d_papers']:+d} since last cycle), "
          f"questions {a['questions']} ({a['d_questions']:+d})")
    print(f"[RAW] status: {a['status']}")
    print(f"[RAW] unit-400 failed remaining: {a['unit400_failed_remaining']} "
          f"(target 0 after fixed run)")
    print(f"[RAW] new-batch review/complete: {a['new_review_complete']}")
    unknown = [x for x in a["new_fail_classes"]
               if not any(s in (x[0] or "") for s in KNOWN_FAIL_SIGS)]
    if unknown:
        flags.append(f"new-fail-class {unknown}")

    r = repo_audit()
    print(f"[REPO] local {r['local_head']} vs remote {r['remote_head']} "
          f"synced={r['synced']} unit_fix={r['unit_fix_3c61451']} rpd_bias={r['rpd_bias_f65b0db']} "
          f"(fix committed {r['fix_time']})")
    if not r["synced"]:
        flags.append("repo_b not synced with origin (parallel session?)")
    if not (r["unit_fix_3c61451"] and r["rpd_bias_f65b0db"]):
        flags.append("fix commits missing from tree")

    verdict, dispatched = "", None
    eff = latest["conclusion"]
    if eff == "cancelled":
        print("[DECIDE] last run was cancelled (orchestrator stop) — not a fault; "
              "proceeding to normal gates.")
        eff = "success"
    if latest["status"] != "completed":
        verdict = "RUNNING"
        print("[DECIDE] last run still active — audit only, no dispatch.")
    elif eff not in ("success", "skipped"):
        flags.append(f"last run conclusion={latest['conclusion']}")
        verdict = "FLAG:" + ";".join(flags)
    else:
        # run finished — sanity gates
        if a["d_papers"] == 0 and a["unit400_failed_remaining"] == 0:
            state["zero_delta_streak"] = state.get("zero_delta_streak", 0) + 1
        else:
            state["zero_delta_streak"] = 0
        if a["unit400_failed_remaining"] > 0:
            # fix is disproven ONLY if a post-fix success run actually RETRIED
            # some of them and they failed again (unit400_refailed_since > 0).
            # Untouched leftovers = queue never reached them (budget/deadline)
            # or legacy files outside the current pdf_dir scope (reprocess heals).
            if (a["unit400_refailed_since"] > 0
                    and latest["conclusion"] == "success"
                    and str(latest["run_started_at"]) > str(r["fix_time"])):
                flags.append(f"unit-400 RETRIED-and-refailed after post-fix run: "
                             f"{a['unit400_refailed_since']} of "
                             f"{a['unit400_failed_remaining']} remaining — investigate 3c61451")
            else:
                print(f"[note] {a['unit400_failed_remaining']} unit-400 failures remain "
                      f"but 0 were retried-and-refailed in this window (fix 3c61451 not "
                      f"disproven); in-scope ones auto-retry next dispatch, "
                      f"legacy ones are reprocess-campaign scope")
        if unknown:
            pass  # already flagged above
        if flags:
            verdict = "FLAG:" + ";".join(flags)
        else:
            # drain check: backlog = hunt5+aktuonline4 hashes not in DB (cheap proxy:
            # zero papers delta over 2 consecutive clean cycles => drained)
            if state.get("zero_delta_streak", 0) >= 2:
                inputs = REPROCESS_INPUTS
                mode = "REPROCESS-MAKE-GREAT"
            else:
                inputs = DISPATCH_INPUTS
                mode = "BACKLOG-DRAIN"
            body = {"ref": REF, "inputs": inputs}
            if dry:
                verdict = f"GOOD (DRY) — would dispatch {mode} with inputs={inputs['pdf_dir']}"
                print(f"[DECIDE] DRY: audit clean — {verdict}")
            else:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{REPO}/actions/workflows/extraction.yml/dispatches",
                    data=json.dumps(body).encode(), headers=HDR, method="POST")
                try:
                    urllib.request.urlopen(req, timeout=30)
                    time.sleep(8)
                    nr = api(f"/repos/{REPO}/actions/workflows/extraction.yml/runs?per_page=1")["workflow_runs"][0]
                    dispatched = nr["id"]
                    verdict = f"DISPATCHED {dispatched} ({mode})"
                    print(f"[DECIDE] audit GOOD -> dispatched run id={dispatched} mode={mode}")
                except Exception as e:
                    verdict = f"FLAG:dispatch-failed {e}"
                    flags.append(f"dispatch failed: {e}")

    state.update({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime()),
                  "last_run_id": (dispatched or latest["id"]),
                  "papers_total": a["papers"], "questions_total": a["questions"]})
    save_state(state)
    line = f"cycle#{cyc} verdict={verdict} papers={a['papers']} q={a['questions']} flags={flags or '-'}"
    cycle_log(line)
    print(f"\nVERDICT: {verdict}")
    print(f"STATE: {STATE_F}")

if __name__ == "__main__":
    main()
