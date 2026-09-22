#!/usr/bin/env python3
"""keyprobe — runs ON a GitHub Actions runner (US egress; sandbox is geo-blocked).

For every key in $GEMMA_API_KEYS (labeled k1..kN, values never printed):
  1. GET /v1beta/models            -> key valid? which of our ladder models exist?
  2. POST generateContent with STRICT JSON mode (responseMimeType + responseSchema)
     -> does this model actually honor structured output for OUR pipeline shape?
Prints machine-parsable lines:  RESULT|k<i>|<model>|<VERDICT>|<latency>|<note>
and a markdown table into $GITHUB_STEP_SUMMARY.

Verdicts: PASS (strict JSON, correct shape) / WEAK (200 but JSON had to be
salvaged) / 429-DAY (RPD exhausted today, see quotaId) / FAIL-<http> / ABSENT
(model not visible to this key) / KEY-DEAD.
"""
import json, os, re, time, urllib.request, urllib.error

BASE = "https://generativelanguage.googleapis.com/v1beta"

CANDS = [
    "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite",
    "gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.6-flash",
    "gemini-3-flash-preview", "gemini-3.8-flash", "gemini-2.5-flash",
    "gemma-4-26b-a4b-it", "gemma-4-31b-it",
]

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "questions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"qn": {"type": "INTEGER"}, "text": {"type": "STRING"}},
                "required": ["qn", "text"],
            },
        }
    },
    "required": ["questions"],
}
PROMPT = ('You are a JSON API. Return exactly one object like '
          '{"questions":[{"qn":1,"text":"What is 2+2?"}]} with exactly 1 item.')

def call(url, key, body=None, timeout=45):
    req = urllib.request.Request(url, method="POST" if body else "GET")
    req.add_header("x-goog-api-key", key)
    data = None
    if body:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": {"message": str(e)}}

def quota_ids(err):
    out = []
    for d in (err.get("details") or []) if isinstance(err, dict) else []:
        if isinstance(d, dict) and d.get("quotaId"):
            out.append(d["quotaId"])
    rd = None
    for d in (err.get("details") or []) if isinstance(err, dict) else []:
        if isinstance(d, dict) and d.get("retryDelay"):
            rd = d["retryDelay"]
    return ",".join(out) + (f" retry={rd}" if rd else "")

def extract_json(body):
    try:
        cands = body.get("candidates") or []
        parts = ((cands[0].get("content") or {}).get("parts")) or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        for t in texts:
            t = t.strip()
            if t.startswith("```"):
                t = re.sub(r"^```[a-z]*\n?|\n?```$", "", t).strip()
            try:
                return json.loads(t), "strict"
            except Exception:
                m = re.search(r"\{.*\}", t, re.S)
                if m:
                    try:
                        return json.loads(m.group(0)), "salvaged"
                    except Exception:
                        pass
        return None, "no-json-part"
    except Exception as e:
        return None, f"parse-err:{e}"

def main():
    keys = [k.strip() for k in os.environ.get("GEMMA_API_KEYS", "").split(",") if k.strip()]
    print(f"[probe] {len(keys)} keys loaded from secret (labels k1..k{len(keys)})")
    rows = []
    for i, key in enumerate(keys, 1):
        kid = f"k{i}"
        st, body = call(f"{BASE}/models?pageSize=200", key)
        if st != 200:
            err = body.get("error", {})
            note = f"{err.get('status','?')}: {str(err.get('message',''))[:80]}"
            print(f"RESULT|{kid}|LIST|KEY-DEAD|0|{note}")
            rows.append((kid, "(list)", "KEY-DEAD", "0", note))
            continue
        have = {m.get("name", "").split("/")[-1] for m in body.get("models", [])}
        print(f"[probe] {kid} valid, {len(have)} models visible")
        for m in CANDS:
            if m not in have:
                print(f"RESULT|{kid}|{m}|ABSENT|0|not visible to this key")
                rows.append((kid, m, "ABSENT", "0", "not visible"))
                continue
            t0 = time.time()
            st, body = call(f"{BASE}/models/{m}:generateContent", key, {
                "contents": [{"parts": [{"text": PROMPT}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": SCHEMA,
                    "temperature": 0, "maxOutputTokens": 300,
                },
            }, timeout=60)
            dt = f"{time.time() - t0:.1f}s"
            if st == 200:
                obj, how = extract_json(body)
                q = (obj or {}).get("questions") if isinstance(obj, dict) else None
                ok = isinstance(q, list) and len(q) >= 1 and \
                     isinstance(q[0], dict) and q[0].get("qn") == 1 and q[0].get("text")
                verdict = "PASS" if (ok and how == "strict") else ("WEAK" if ok else f"FAIL-{how}")
                note = f"shape_ok={ok} how={how}"
            else:
                err = body.get("error", {})
                code = err.get("status", "?")
                note = f"{code}: {str(err.get('message',''))[:70]}"
                if st == 429:
                    qid = quota_ids(err)
                    verdict = "429-DAY" if ("perday" in qid.lower() or "day" in qid.lower()) else "429-WIN"
                    note += f" [{qid}]"
                else:
                    verdict = f"FAIL-{st}"
                if st == -1:
                    verdict, note = "FAIL-NET", note[:70]
            print(f"RESULT|{kid}|{m}|{verdict}|{dt}|{note}")
            rows.append((kid, m, verdict, dt, note))
            time.sleep(0.35)

    # step summary
    lines = ["| key | model | verdict | latency | note |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append("| " + " | ".join(str(x)[:90].replace("|", "/") for x in r) + " |")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Key x model JSON probe\n\n" + "\n".join(lines) + "\n")
    npass = sum(1 for r in rows if r[2] == "PASS")
    print(f"[probe] DONE: {npass} PASS of {len(rows)} combos")

if __name__ == "__main__":
    main()
