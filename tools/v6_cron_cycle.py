#!/usr/bin/env python3
"""V6 cron cycle v2 (owner orders 2026-10-27 + 2026-11-05 + 2026-11-06):
AUDIT all platforms -> fix/flag -> if last run GOOD, dispatch the next GHA
extraction run sized by the DAILY RPD BUDGET. Never stacks runs.

Platforms audited:
  1. GHA   harshvardhan9084/gemma-nebula extraction.yml (repo B, ref aktu-runner)
  2. RAW   Supabase qnakqtiokspzoopivfyl (papers/questions deltas, failed classes,
           papers-clear pools: review / failed / thin <20q)
  3. QUOTA RPD LAW (owner 2026-11-06, CORRECTED same day — "use your own
           calculation, not verbatim"): 6 keys x 500 RPD PER MODEL x 2 vision
           models (gemini-3.1-flash-lite + gemini-3.5-flash-lite) = 6,000
           vision calls/day ceiling; this dispatcher budgets STRICTLY UNDER it
           at DAILY_VISION_CAP=5,700 via a cross-run ledger (scripts/out/
           v6_rpd_ledger.json, fed by the "AI calls spent" lines in run logs).
           LEDGER WINDOW (owner fix 2026-11-07 v3): the quota day is anchored
           at the 07:05Z reset (Google midnight-Pacific per-key RPD) — spend
           = calls of runs FINISHED since 07:05Z today, NOT a rolling 24h
           window. A rolling window let one pre-cadence-law run (4,587 calls)
           pin the ledger ~24h and starve GHA of dispatches; the quota-day
           window frees exactly at 07:05Z so the 4 slots (07:30Z/11:30Z/
           15:30Z/19:30Z fires, 1,425 calls each) fit inside every quota day.
           The old fixed 2,000-calls-per-run spec is RETIRED (owner: "forgot
           that i ever said '2000' calls"); the earlier 3,000 mis-read is also
           retired — models multiply, they don't divide.
  4. REPO  repo_b checkout vs origin (fix commits present: 3c61451 unit, f65b0db RPD-bias)

Modes (papers clear-zero first, owner 2026-11-06):
  PAPERS-CLEAR         review + failed + thin(<20q) re-extract queue non-empty
                       -> THIN-MAKE-GREAT campaign inputs (thin_max=20,
                       status_filter=complete,review,failed). The extractor's
                       RETRY ledger bounds every paper at MAX_REATTEMPTS=2
                       attempts (hard) and requires progress between attempts
                       (soft); terminal papers are never re-picked -> no loop.
  REPROCESS-MAKE-GREAT zero-delta fallback for legacy stragglers
  BACKLOG-DRAIN        fresh hunt5/aktuonline4 ingestion

Verdicts:
  DISPATCHED <id> (MODE, calls=N, rpd_left=M)   audit good -> next run queued
  RUNNING                                        last run active -> audit only
  FLAG:<reasons>                                 problems -> dispatch held
  HOLD-RPD (spent X/CAP)                         < MIN_RUN_CALLS left today
  DRAINED                                        nothing left to do

State: /home/z/my-project/scripts/out/v6_cron_state.json
RPD ledger: /home/z/my-project/scripts/out/v6_rpd_ledger.json
Cycle log: /home/z/my-project/scripts/out/v6_cron_cycles.log (append)
"""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

PAT = open("/home/z/my-project/.secrets/gh_pat.txt").read().strip()
REPO = "harshvardhan9084/gemma-nebula"
REF = "aktu-runner"
HDR = {"Authorization": f"Bearer {PAT}", "Accept": "application/vnd.github+json"}
OUT_DIR = "/home/z/my-project/scripts/out"
STATE_F = f"{OUT_DIR}/v6_cron_state.json"
LOG_F = f"{OUT_DIR}/v6_cron_cycles.log"
RPDL_F = f"{OUT_DIR}/v6_rpd_ledger.json"
OBS_F = f"{OUT_DIR}/v6_observations.jsonl"
REPO_B = "/home/z/my-project/repo_b"
KNOWN_FAIL_SIGS = ("400 Client Error: Bad Request for url: https://qnakqtiokspzoopivfyl.supabase.co/rest/v1/qu",)

# ---- RPD LAW (owner 2026-11-06, corrected: keys x models, not keys only) ----
# 6 keys x 500 RPD/model x 2 flash-lite vision models = 6,000 vision RPD/day.
# Strictly under -> dispatch cap:
DAILY_VISION_CAP = 5700
MIN_RUN_CALLS = 180        # below this a run cannot even clear ~20 papers: hold
MAX_REATTEMPTS = 2         # mirror of extractor MAX_REATTEMPTS (anti-loop law)

# ---- CADENCE LAW (owner 2026-11-07) -----------------------------------------
# The cron agent fires every 4 hours (6 fires/day) and REPORTS every fire.
# GHA extraction runs at most MAX_RUNS_PER_DAY = 4 FULL runs/day (owner: "4
# full runs, not test or failed" -> failed/test runs don't consume the daily
# quota; the RPD ledger still gates their spend). Each run is budgeted
# CALLS_PER_RUN = 5,700/4 = 1,425 vision calls so the daily cap spreads
# evenly across the day's 4 runs. Mercury pool = 6 keys with consecutive-error
# benching (n=2 -> 120s rest) per owner 2026-11-07.
MAX_RUNS_PER_DAY = 4
CALLS_PER_RUN = DAILY_VISION_CAP // MAX_RUNS_PER_DAY   # 1,425

BASE_INPUTS = {
    "stage": "extract", "extractor": "v6",
    "pdf_dir": "corpus/hunt5,corpus/aktuonline4",
    "manifest": "data/hunt5_extractor_manifest.csv,data/aktuonline4_manifest.csv",
    "pilot_n": "0", "limit": "0", "course": "", "year": "",
    "reprocess": "false", "status_filter": "complete,review,failed",
    "min_interval_s": "", "route": "auto",
    "qc_min_questions": "10", "chunk_pages": "2", "dry_run": "false", "not_before": "",
}
REPROCESS_INPUTS = dict(BASE_INPUTS, **{
    "pdf_dir": "corpus/unified,corpus/aktuonline3,corpus/aktuonline4,corpus/hunt5",
    "manifest": "data/unified_manifest.csv,data/aktuonline3_manifest.csv,data/aktuonline4_manifest.csv,data/hunt5_extractor_manifest.csv",
    "reprocess": "true", "status_filter": "complete,review,failed",
})
THIN_INPUTS = dict(REPROCESS_INPUTS, **{
    # owner papers-clear rule 2026-11-05/06: a real AKTU paper carries ~20
    # questions (incl. choice variants; measured corpus median 21.0 / avg 20.3)
    # -> sub-bar papers are re-extracted, BOUNDED by the extractor RETRY ledger
    # (hard 2 attempts, soft progress-gate, terminal ACCEPT/FROZEN stamps, and
    # the zero-loss upsert-only guard). Quality-first ordering inside the
    # extractor: review -> failed -> complete-thin; text layer flies on mercury,
    # scans are bound by the RPD law. total_calls is injected at dispatch time.
    "thin_max": "20",
})

def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"https://api.github.com{path}", headers=HDR,
                                 data=data, method=method)
    return json.load(_OPENER.open(req, timeout=30))

class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Fixes the HTTP 401 on job-log downloads (2026-11-02..05 cycles): urllib
    re-sends the Authorization header when following the 302 to
    logs.githubusercontent.com, whose CDN rejects the PAT. Strip auth on any
    cross-host redirect (curl -L does the same by default)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            req.headers.pop("Authorization", None)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_OPENER = urllib.request.build_opener(_SafeRedirect)

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

# ---------------- RPD ledger ----------------
def load_rpdl():
    if os.path.exists(RPDL_F):
        try:
            return json.load(open(RPDL_F))
        except Exception:
            pass
    return {"runs": {}}

def save_rpdl(d):
    json.dump(d, open(RPDL_F, "w"), indent=1)

def prune_rpdl(d):
    """Keep only entries finished within the last 48h."""
    cut = time.time() - 48 * 3600
    out = {}
    for rid, e in d.get("runs", {}).items():
        try:
            fin = datetime.fromisoformat(e["finished_at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if fin >= cut:
            out[rid] = e
    return {"runs": out}

def quota_day_start() -> float:
    """07:05Z reset anchor for the current vision quota day (owner law)."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=7, minute=5, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    return start.timestamp()

def spent_quota_day(d):
    """Spend THIS quota day: every call by runs finished since 07:05Z today
    (a run straddling the reset is fully attributed to the new day -> never
    over; spend frees exactly at the 07:05Z reset, matching per-key RPD)."""
    cut = quota_day_start()
    tot = 0
    for rid, e in d.get("runs", {}).items():
        try:
            fin = datetime.fromisoformat(e["finished_at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if fin >= cut:
            tot += int(e.get("calls") or 0)
    return tot

def record_run_calls(d, run_id, calls):
    d.setdefault("runs", {})[str(run_id)] = {
        "calls": int(calls),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }

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
    # failed-error signature classes (FULL failed set, not time-windowed — the
    # failure path does not update row timestamps, verified 2026-10-28 TME101/#56)
    since = prev.get("checked_at", "2026-10-27 00:00:00+00")
    cur.execute("""SELECT left(extraction_error,120), count(*) FROM papers
                   WHERE extraction_status='failed'
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")
    a["fail_classes_now"] = cur.fetchall()
    # unit-400 class remaining (should drain to 0 after the fixed run)
    cur.execute("""SELECT count(*) FROM papers WHERE extraction_status='failed'
                   AND extraction_error LIKE '%%400 Client Error%%'""")
    a["unit400_failed_remaining"] = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM papers WHERE extraction_status='failed'
                   AND extraction_error LIKE '%%400 Client Error%%' AND ingested_at > %s""", (since,))
    a["unit400_refailed_since"] = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FILTER (WHERE extraction_status='review'),
                          count(*) FILTER (WHERE extraction_status='complete')
                   FROM papers WHERE ingested_at > %s""", (since,))
    a["new_review_complete"] = cur.fetchone()
    # papers-clear pools (owner rule: real AKTU paper ~20 questions).
    # thin_left EXCLUDES terminal RETRY ledger stamps so accepted-short papers
    # never keep the campaign alive (no endless mode).
    cur.execute("""SELECT count(*) FROM papers
                   WHERE extraction_status='complete' AND coalesce(n_questions,0) < 20
                   AND (extraction_error IS NULL OR extraction_error NOT LIKE 'RETRY-%%')""")
    a["thin_complete"] = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM papers WHERE extraction_error LIKE 'RETRY-ACCEPT%%'""")
    a["retry_accepted"] = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM papers WHERE extraction_error LIKE 'RETRY-FROZEN%%'""")
    a["retry_frozen"] = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM papers WHERE extraction_error LIKE 'RETRY#%%'
                   AND extraction_error NOT LIKE 'RETRY-%%'""")
    a["retry_active"] = cur.fetchone()[0]
    conn.close()
    a["d_papers"] = a["papers"] - prev.get("papers_total", a["papers"])
    a["d_questions"] = a["questions"] - prev.get("questions_total", a["questions"])
    return a

# ---------------- GHA ----------------
def gha_audit():
    g = {}
    runs = api(f"/repos/{REPO}/actions/workflows/extraction.yml/runs?per_page=25")["workflow_runs"]
    g["runs"] = [{k: r[k] for k in ("id", "run_number", "status", "conclusion",
                   "run_started_at", "created_at", "event")} for r in runs]
    g["latest"] = g["runs"][0] if g["runs"] else None
    # CADENCE LAW: the daily 4-run quota counts workflow_dispatch runs started
    # in the last 24h that are in_progress or succeeded (failed runs do not
    # consume the quota; the RPD ledger still constrains their spend).
    cut = time.time() - 86400
    def _ts(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    g["runs_24h"] = [r for r in g["runs"] if r["event"] == "workflow_dispatch"
                     and _ts(r.get("created_at")) >= cut
                     and (r["status"] == "in_progress" or r["conclusion"] == "success")]
    return g

def fetch_run_logs(run_id):
    """One pass over the run's extract-job logs:
    returns (failed_lines, db_invisible_count, sample_names, calls_spent_total).
    calls_spent = sum of 'AI calls spent this process: N' across shards (the
    RPD-law ledger feed). Returns (None, None, [], None) on fetch error."""
    try:
        jobs = api(f"/repos/{REPO}/actions/runs/{run_id}/jobs?per_page=20")["jobs"]
        names, invisible, calls_total = [], 0, 0
        conn = psycopg2.connect(DSN, connect_timeout=30); conn.autocommit = True
        cur = conn.cursor()
        for j in jobs:
            if not j["name"].startswith("extract"):
                continue
            req = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/actions/jobs/{j['id']}/logs", headers=HDR)
            txt = _OPENER.open(req, timeout=60).read().decode("utf-8", "ignore")
            for line in txt.splitlines():
                m = re.search(r"FAILED \w+.*?\| (.+)$", line.strip())
                if m:
                    fn = m.group(1).strip()
                    if fn and fn not in names:
                        names.append(fn)
                        # Join by paper CODE (e.g. KHU801), not by filename slug:
                        # papers.source_url stores HTML/Drive URLs while run logs
                        # print corpus filenames — the old slug-LIKE join false-
                        # flagged 4 DB-visible papers as INVISIBLE (cycle #31).
                        hit = 0
                        for cd in set(re.findall(r"[A-Z]{2,4}\d{3}[A-Z]?", fn)):
                            cur.execute("SELECT count(*) FROM papers WHERE source_url ILIKE %s",
                                        (f"%{cd}%",))
                            hit += cur.fetchone()[0]
                        if not hit:
                            slug = fn.replace(".pdf", "")[:44]
                            cur.execute("SELECT count(*) FROM papers WHERE source_url LIKE %s",
                                        (f"%{slug}%",))
                            hit = cur.fetchone()[0]
                        if hit == 0:
                            invisible += 1
                mc = re.search(r"AI calls spent this process: (\d+)", line)
                if mc:
                    calls_total += int(mc.group(1))
        conn.close()
        return len(names), invisible, names[:5], calls_total
    except Exception as e:
        cycle_log(f"run-log fetch error: {e}")
        return None, None, [], None

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
    print(f"[RAW] papers-clear pools: review={a['status'].get('review', 0)} "
          f"failed={a['status'].get('failed', 0)} thin_left={a['thin_complete']} "
          f"| retry ledger: active={a['retry_active']} "
          f"accepted={a['retry_accepted']} frozen={a['retry_frozen']} "
          f"(hard cap {MAX_REATTEMPTS} attempts/paper)")
    print(f"[RAW] unit-400 failed remaining: {a['unit400_failed_remaining']}")

    # RPD law accounting: record the finished run's real spend, then gate.
    rpdl = prune_rpdl(load_rpdl())
    if latest["status"] == "completed" and str(latest["id"]) not in rpdl["runs"] \
            and latest["conclusion"] != "cancelled":
        _lf, _inv, _smp, calls = fetch_run_logs(latest["id"])
        if calls is not None:
            record_run_calls(rpdl, latest["id"], calls)
            save_rpdl(rpdl)
            print(f"[RPD] run {latest['id']} recorded: {calls} Google calls")
    spent = spent_quota_day(rpdl)
    remaining = max(0, DAILY_VISION_CAP - spent)
    state["rpd_spent_day"] = spent
    print(f"[RPD] spent(quota-day since 07:05Z)={spent} cap={DAILY_VISION_CAP} (owner law corrected: "
          f"strictly under 6 keys x 500 RPD x 2 models = 6,000) "
          f"-> dispatchable now: {remaining}")
    print(f"[CADENCE] full runs last 24h: {len(g['runs_24h'])}/{MAX_RUNS_PER_DAY} "
          f"(per-run budget {CALLS_PER_RUN} = cap/4; cron fires 6x/day)")

    # new fail-class detection vs persisted baseline (fires once per new class)
    known = set(state.get("known_fail_classes", []))
    now_classes = [c for c, n in a["fail_classes_now"] if c]
    unknown = [c for c in now_classes
               if c not in known
               and not any(c.startswith(s[:60]) for s in KNOWN_FAIL_SIGS)]
    state["known_fail_classes"] = sorted(set(known) | set(now_classes))
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
        # invisible-failure detector: run reports FAILED but no DB row exists.
        lf, inv, samples = (None, None, [])
        if latest["conclusion"] != "cancelled" and latest["id"] not in set(state.get("invisible_ack", [])):
            lf, inv, samples, _c2 = fetch_run_logs(latest["id"])
        if lf is not None and lf > 0:
            print(f"[LOGS] run reported {lf} failed papers; DB-invisible: {inv} "
                  f"(samples: {samples})")
            if inv:
                flags.append(f"{inv} INVISIBLE failures (run logs vs DB, e.g. {samples[:2]})")
        if flags:
            verdict = "FLAG:" + ";".join(flags)
        elif len(g["runs_24h"]) >= MAX_RUNS_PER_DAY:
            verdict = (f"HOLD-CADENCE ({len(g['runs_24h'])}/{MAX_RUNS_PER_DAY} full runs "
                       f"in last 24h; rpd_left={remaining}; next reset 07:05Z)")
            print(f"[DECIDE] {verdict}")
        elif remaining < MIN_RUN_CALLS:
            verdict = f"HOLD-RPD (spent {spent}/{DAILY_VISION_CAP}, {remaining} left < {MIN_RUN_CALLS}; next reset 07:05Z)"
            print(f"[DECIDE] {verdict}")
        else:
            # papers clear-zero first (owner 2026-11-06): review+failed+thin queue
            review_left = a["status"].get("review", 0)
            failed_left = a["status"].get("failed", 0)
            thin_left = a["thin_complete"]
            work_left = review_left + failed_left + thin_left
            print(f"[DECIDE] work_left={work_left} (review={review_left} "
                  f"failed={failed_left} thin={thin_left}) rpd_left={remaining}")
            if work_left > 0:
                calls_this = min(remaining, CALLS_PER_RUN)
                inputs = dict(THIN_INPUTS, total_calls=str(calls_this))
                mode = "PAPERS-CLEAR"
            elif state.get("zero_delta_streak", 0) >= 2:
                calls_this = min(remaining, CALLS_PER_RUN)
                inputs = dict(REPROCESS_INPUTS, total_calls=str(calls_this))
                mode = "REPROCESS-MAKE-GREAT"
            else:
                calls_this = min(remaining, CALLS_PER_RUN)
                inputs = dict(BASE_INPUTS, total_calls=str(calls_this))
                mode = "BACKLOG-DRAIN"
            body = {"ref": REF, "inputs": inputs}
            if dry:
                verdict = (f"GOOD (DRY) — would dispatch {mode} with "
                           f"total_calls={calls_this} rpd_left={remaining}")
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
                    verdict = (f"DISPATCHED {dispatched} ({mode}, calls={calls_this}, "
                               f"rpd_left={remaining})")
                    print(f"[DECIDE] audit GOOD -> dispatched run id={dispatched} mode={mode} "
                          f"total_calls={calls_this} (cadence {len(g['runs_24h']) + 1}/{MAX_RUNS_PER_DAY})")
                except Exception as e:
                    verdict = f"FLAG:dispatch-failed {e}"
                    flags.append(f"dispatch failed: {e}")

    state.update({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime()),
                  "last_run_id": (dispatched or latest["id"]),
                  "papers_total": a["papers"], "questions_total": a["questions"]})
    save_state(state)
    # OBSERVATION DUTY (owner 2026-11-07): one JSONL line per fire = the delta
    # series for plotting (papers/questions/status pools over time per run).
    try:
        obs = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "cycle": cyc, "run_id": (dispatched or latest["id"]),
               "run_status": latest["status"], "run_conclusion": latest["conclusion"],
               "papers": a["papers"], "questions": a["questions"],
               "complete": a["status"].get("complete", 0),
               "review": a["status"].get("review", 0),
               "failed": a["status"].get("failed", 0),
               "thin_left": a["thin_complete"],
               "retry_active": a["retry_active"],
               "retry_accepted": a["retry_accepted"],
               "retry_frozen": a["retry_frozen"],
               "rpd_spent_day": spent, "rpd_cap": DAILY_VISION_CAP,
               "runs_24h": len(g["runs_24h"]),
               "verdict": verdict.split(" ")[0]}
        with open(OBS_F, "a", encoding="utf-8") as f:
            f.write(json.dumps(obs) + "\n")
    except Exception as e:
        print(f"[warn] observation append failed: {e}")
    line = (f"cycle#{cyc} verdict={verdict} papers={a['papers']} q={a['questions']} "
            f"rpd_spentday={spent}/{DAILY_VISION_CAP} flags={flags or '-'}")
    cycle_log(line)
    print(f"\nVERDICT: {verdict}")
    print(f"STATE: {STATE_F}")

if __name__ == "__main__":
    main()
