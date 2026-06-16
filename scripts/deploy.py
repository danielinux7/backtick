#!/usr/bin/env python3
"""One command to deploy: sync secrets/env to the host, then push code.

    python scripts/deploy.py

Render builds from git, so the env sync has to run HERE (locally) — secrets.local.json
is gitignored and never reaches Render's build. This wraps the two steps so the
live env can't drift from a deploy and you never run the sync by hand:

    1. scripts/sync_render_env.py   — push only the env/secret values that changed
                                       (no-op, no extra deploy, when nothing moved)
    2. git push <remote> <branch>   — Render auto-deploys the new commit

Env sync runs first so a bad API key / network fails BEFORE the remote is touched.
On a normal code-only deploy step 1 is a no-op, so this triggers a single deploy;
when a secret actually changed, expect the env-change deploy plus the push deploy.

Options:
    --skip-env       just push (don't sync env)
    --env-only       just sync env (don't push)
    --remote NAME    git remote (default: origin)
    --branch NAME    branch to push (default: current branch)
    --force-env      push every env value, even unchanged ones
    --dry-run        print the steps; run nothing
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets.local.json"
SYNC = ROOT / "scripts" / "sync_render_env.py"


def _run(cmd: list[str], dry: bool) -> int:
    print(f"[deploy] $ {' '.join(cmd)}")
    return 0 if dry else subprocess.call(cmd, cwd=str(ROOT))


def _current_branch() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)
    ).decode().strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-env", action="store_true")
    ap.add_argument("--env-only", action="store_true")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default=None)
    ap.add_argument("--force-env", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 1. env sync (fail fast, before we touch the remote)
    if not args.skip_env:
        if SECRETS.exists():
            cmd = [sys.executable, str(SYNC)]
            if args.force_env:
                cmd.append("--force")
            rc = _run(cmd, args.dry_run)
            if rc != 0:
                print("[deploy] env sync failed — aborting before push.")
                return rc
        else:
            print(f"[deploy] {SECRETS.name} not found — skipping env sync.")

    if args.env_only:
        print("[deploy] --env-only: done (not pushing).")
        return 0

    # 2. push → Render auto-deploys
    branch = args.branch or _current_branch()
    if branch != "main":
        print(f"[deploy] note: pushing '{branch}', but Render auto-deploys 'main' only.")
    rc = _run(["git", "push", args.remote, branch], args.dry_run)
    if rc != 0:
        return rc
    print("[deploy] pushed — Render will build & deploy (stop-then-start; brief downtime).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
