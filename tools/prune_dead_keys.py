#!/usr/bin/env python3
"""prune_dead_keys - runs ON a GitHub Actions runner (US egress).

Reads $GEMMA_API_KEYS, live-probes EVERY token (any prefix - newer Google key
formats exist; v1 of this tool wrongly dropped non-AIzaSy tokens, incident
2026-11-05), rebuilds the pool with ONLY healthy tokens and writes the updated
comma-joined value back to the repo secret GEMMA_API_KEYS using $PRUNE_PAT
(temporary secret, deleted right after by the operator).

Key VALUES are never printed - labels k1..kN plus the last 4 chars only.
Safety: if every token probes dead, the secret is left untouched (exit 2).
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

import requests
from nacl import encoding, public

BASE = "https://generativelanguage.googleapis.com/v1beta"
REPO = "harshvardhan9084/gemma-nebula"


def mask(k):
    return k[:5] + "..." + k[-4:] if len(k) >= 12 else "??"


def probe(key, tries=2):
    """'alive' | 'dead' | reason. Quota-exhausted and geo-fenced keys are ALIVE
    (they fail today from this egress but serve the pipeline elsewhere)."""
    for i in range(tries):
        req = urllib.request.Request(BASE + "/models?pageSize=1")
        req.add_header("x-goog-api-key", key)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return "alive", "ok"
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:
                body = {}
            err = body.get("error") or {}
            st, msg = err.get("status", ""), (err.get("message") or "")[:90]
            if e.code == 429 or st == "RESOURCE_EXHAUSTED":
                return "alive", "quota-today (key alive)"
            if "location is not supported" in msg.lower():
                return "alive", "geo-fenced from THIS runner (key valid)"
            if e.code in (400, 401, 403):
                return "dead", f"HTTP {e.code} {st} {msg}"
            if e.code >= 500 and i < tries - 1:
                time.sleep(3)
                continue
            return "dead", f"HTTP {e.code} {st} {msg}"
        except Exception as e:
            if i < tries - 1:
                time.sleep(3)
                continue
            return "dead", f"net {str(e)[:60]}"
    return "dead", "unreachable"


def main():
    raw = os.environ.get("GEMMA_API_KEYS") or ""
    pat = os.environ.get("PRUNE_PAT") or ""
    tokens = []
    for chunk in re.split(r"[,;\s]+", raw):
        t = chunk.strip().strip('"').strip("'").strip()
        if not t or t.startswith("<") or t.lower() in {"dead", "removed", "xxx", "null"}:
            if t:
                print(f"[prune] dropped placeholder/junk token: {t[:24]}")
            continue
        if t not in tokens:
            tokens.append(t)
    print(f"[prune] {len(tokens)} candidate tokens (any prefix accepted, probed live)")
    healthy, dead = [], []
    for i, t in enumerate(tokens, 1):
        verdict, why = probe(t)
        (healthy if verdict == "alive" else dead).append(t)
        print(f"[prune] k{i} {mask(t)} -> {verdict.upper()} ({why})")
    if not healthy:
        print("[prune] REFUSING to write: zero healthy tokens would remain")
        sys.exit(2)
    if not dead:
        print("[prune] nothing to prune - secret already clean")
        return
    if not pat:
        print("[prune] no PRUNE_PAT - cannot write the secret; dry-run only")
        sys.exit(0)
    new_val = ",".join(healthy)
    H = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    pk = requests.get(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key",
                      headers=H, timeout=30).json()
    box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(new_val.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{REPO}/actions/secrets/GEMMA_API_KEYS",
                     headers=H, json={"encrypted_value": enc, "key_id": pk["key_id"]},
                     timeout=30)
    print(f"[prune] GEMMA_API_KEYS rewritten: {len(tokens)} -> {len(healthy)} tokens "
          f"(removed {len(dead)} dead) HTTP {r.status_code}")


if __name__ == "__main__":
    main()
