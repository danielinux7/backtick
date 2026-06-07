#!/usr/bin/env python3
"""Online backup of the SQLite database, with rotation.

Managed Postgres gave us automated backups for free; on SQLite-over-a-disk we
own them. This uses SQLite's online backup API (safe against a live WAL DB — no
need to stop the app) to write a consistent timestamped copy, then prunes to the
most recent N.

Run manually, or wire it to a scheduler (a Render Cron Job, host crontab, etc.):

    python scripts/backup_db.py                       # uses DATABASE_URL
    python scripts/backup_db.py --keep 14 --dir /var/data/backups

It is a no-op (exit 0) when DATABASE_URL is not SQLite, so it's safe to schedule
regardless of backend.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _sqlite_path(url: str) -> str | None:
    """Extract the on-disk file path from a sqlite SQLAlchemy URL, or None if the
    URL isn't a SQLite *file* (Postgres, :memory:, etc.).

    SQLAlchemy's slash convention: `sqlite:///foo.db` is relative `foo.db`, while
    `sqlite:////abs/foo.db` is absolute `/abs/foo.db`. So we strip through the
    third slash and keep the remainder verbatim."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            rest = url[len(prefix):]            # 'foo.db' (rel) or '/var/data/foo.db' (abs)
            if not rest or rest.startswith(":memory:"):
                return None
            return rest
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", type=int, default=14, help="how many backups to retain (default 14)")
    ap.add_argument("--dir", default=None, help="backup directory (default: <db dir>/backups)")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./backtick.db")
    db_path = _sqlite_path(url)
    if not db_path:
        print(f"[backup_db] DATABASE_URL is not SQLite ({url!r}) — nothing to do.")
        return 0

    src = Path(db_path).resolve()
    if not src.exists():
        print(f"[backup_db] no database at {src} yet — nothing to back up.")
        return 0

    out_dir = Path(args.dir) if args.dir else src.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"{src.stem}-{stamp}.db"

    # Online backup: consistent snapshot of a live DB, WAL included.
    with sqlite3.connect(str(src)) as conn, sqlite3.connect(str(dest)) as bck:
        conn.backup(bck)
    print(f"[backup_db] wrote {dest} ({dest.stat().st_size} bytes)")

    # Rotate: keep the newest --keep, delete the rest.
    backups = sorted(out_dir.glob(f"{src.stem}-*.db"))
    stale = backups[:-args.keep] if args.keep > 0 else []
    for old in stale:
        old.unlink()
        print(f"[backup_db] pruned {old.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
