#!/usr/bin/env python3
"""prune_dead_keys - runs ON a GitHub Actions runner (US egress).

Reads $GEMMA_API_KEYS, live-probes every key against the Generative Language
API (GET /v1beta/models), rebuilds the pool with ONLY healthy keys and writes
the updated comma-joined value back to the repo secret GEMMA_API_KEYS using
$PRUNE_PAT (temporary secret, deleted right after by the operator).

Key VALUES are never printed - labels k1..kN plus the last 4 chars only.
Safety: if every key probes dead, the secret is left untouched (exit 2).
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
    return k[:7] + "..." + k[-4:]


def probe(key, tries=2):
    """True if the key can list models (i.e. is alive and authorised)."""
    for i in range(tries):
        req = urllib.request.Request(BASE + "/models?pageSize=1")
        req.add_header("x-goog-api-key", key)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status == 200, "ok"
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:
                body = {}
            st = (body.get("error") or {}).get("status", "")
            msg = ((body.get("error") or {}).get("message") or "")[:90]
            if e.code in (429,) or st == "RESOURCE_EXHAUSTED":
                return True, "quota-today (key alive)"       # exhausted != dead
            if e.code in (400, 401, 403):
                return False, f"HTTP {e.code} {st} {msg}"
            if e.code >= 500 and i < tries - 1:
                time.sleep(3)
                continue
            return False, f"HTTP {e.code} {st} {msg}"
        except Exception as e:
            if i < tries - 1:
                time.sleep(3)
                continue
            return False, f"net {str(e)[:60]}"
    return False, "unreachable"


def main():
    raw = os.environ.get("GEMMA_API_KEYS") or ""
    pat = os.environ.get("PRUNE_PAT") or ""
    keys = [k.strip() for k in re.split(r"[,;\s]+", raw) if k.strip().startswith("AIzaSy")]
    junk = [k.strip() for k in re.split(r"[,;\s]+", raw)
            if k.strip() and not k.strip().startswith("AIzaSy")]
    print(f"[prune] {len(keys)} real keys + {len(junk)} junk/placeholder tokens found")
    healthy, dead = [], []
    for i, k in enumerate(keys, 1):
        ok, why = probe(k)
        (healthy if ok else dead).append(k)
        print(f"[prune] k{i} {mask(k)} -> {'ALIVE' if ok else 'DEAD'} ({why})")
    if not healthy:
        print("[prune] REFUSING to write: zero healthy keys would remain")
        sys.exit(2)
    if not dead and not junk:
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
    print(f"[prune] GEMMA_API_KEYS rewritten: {len(keys)} -> {len(healthy)} keys "
          f"(removed {len(dead)} dead, {len(junk)} junk) HTTP {r.status_code}")


if __name__ == "__main__":
    main()
