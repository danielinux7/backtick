#!/usr/bin/env python3
"""Push local secrets to Render's env vars from one gitignored JSON file.

Keep every secret (DB URL, Google/Apple OAuth, SESSION_SECRET, R2 backup keys, …)
in `secrets.local.json` on your machine and sync them to Render with one command.

This script is the ONLY host-specific piece of the secret story: each provider
has its own env-var API. The JSON file itself is portable — moving to a VPS means
writing a sibling `sync_<host>_env.py`, not re-collecting secrets, and nothing
secret ever touches git (the file is gitignored; only *.example is tracked).

Usage:
    cp secrets.local.json.example secrets.local.json    # then fill it in
    python scripts/sync_render_env.py --dry-run         # show the plan, no calls
    python scripts/sync_render_env.py                   # push to Render

Auth: needs a Render API key and the target service. Put them in the JSON as
`_render_api_key` + (`_render_service_id` or `_render_service_name`), or pass
RENDER_API_KEY / RENDER_SERVICE_ID via the environment.

Updating env vars on Render triggers a redeploy of the service.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.render.com/v1"


def _plan(secrets: dict) -> tuple[dict, dict, list[str]]:
    """Split a loaded secrets dict into (meta, env_to_push, skipped_empty).

    Pure so it's unit-testable. `_`-prefixed keys are meta (script config, never
    pushed). Remaining keys with a non-empty string value are pushed; blank ones
    are skipped so you can leave a field empty to defer to a host default (e.g.
    SESSION_SECRET via Render's generateValue)."""
    meta, env, skipped = {}, {}, []
    for key, val in secrets.items():
        if key.startswith("_"):
            meta[key] = val
            continue
        if isinstance(val, str) and val.strip() == "":
            skipped.append(key)
            continue
        env[key] = val
    return meta, env, skipped


def _mask(val: str) -> str:
    s = str(val)
    return s if len(s) <= 6 else f"{s[:3]}…{s[-2:]} ({len(s)} chars)"


def _api(method: str, path: str, api_key: str, body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_ROOT}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def _resolve_service_id(meta: dict, api_key: str) -> str:
    sid = meta.get("_render_service_id") or os.environ.get("RENDER_SERVICE_ID")
    if sid:
        return sid
    name = meta.get("_render_service_name") or "backtick"
    q = urllib.parse.urlencode({"name": name, "limit": 1})
    res = _api("GET", f"/services?{q}", api_key)
    if not res:
        sys.exit(f"[sync] no Render service named {name!r} — set _render_service_id instead.")
    return res[0]["service"]["id"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default="secrets.local.json", help="secrets file (default: secrets.local.json)")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; make no API calls")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"[sync] {args.file} not found — copy secrets.local.json.example and fill it in.")
    with open(args.file) as f:
        secrets = json.load(f)

    meta, env, skipped = _plan(secrets)
    if not env:
        sys.exit("[sync] nothing to push — every value is blank.")

    print(f"[sync] {len(env)} var(s) to push:")
    for k, v in env.items():
        print(f"    {k} = {_mask(v)}")
    if skipped:
        print(f"[sync] skipped (blank): {', '.join(skipped)}")

    if args.dry_run:
        print("[sync] dry run — no changes made.")
        return 0

    api_key = meta.get("_render_api_key") or os.environ.get("RENDER_API_KEY")
    if not api_key:
        sys.exit("[sync] no API key — set _render_api_key in the JSON or RENDER_API_KEY in the env.")

    service_id = _resolve_service_id(meta, api_key)
    print(f"[sync] target service: {service_id}")

    for key, val in env.items():
        try:
            _api("PUT", f"/services/{service_id}/env-vars/{key}", api_key, {"value": str(val)})
            print(f"    ✓ {key}")
        except urllib.error.HTTPError as e:
            print(f"    ✗ {key}: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
    print("[sync] done. Render will redeploy the service to apply the changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
