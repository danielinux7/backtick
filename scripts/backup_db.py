#!/usr/bin/env python3
"""Online backup of the SQLite database, with rotation.

Managed Postgres gave us automated backups for free; on SQLite-over-a-disk we
own them. This uses SQLite's online backup API (safe against a live WAL DB — no
need to stop the app) to write a consistent timestamped copy, then prunes to the
most recent N.

Run manually, or via the in-process scheduler the app starts on boot
(`backend.backup_scheduler`). A standalone Render Cron Job can't be used here: a
Render persistent disk attaches to a single service, so a separate cron service
can't read the web service's `/var/data/backtick.db`.

    python scripts/backup_db.py                       # uses DATABASE_URL
    python scripts/backup_db.py --keep 14 --dir /var/data/backups

It is a no-op (exit 0) when DATABASE_URL is not SQLite, so it's safe to run
regardless of backend. `run_backup()` is importable for the in-process path.
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


def run_backup(url: str | None = None, keep: int = 14, out_dir: str | None = None) -> Path | None:
    """Write one timestamped online backup and rotate to the newest `keep`.

    Returns the backup Path, or None when there's nothing to do (URL isn't a
    SQLite file, or the DB doesn't exist yet). Safe to call from the running app:
    the SQLite online-backup API copies a live WAL DB consistently without
    stopping it. The sqlite3 calls are blocking, so async callers should run this
    in a worker thread (see backend.backup_scheduler)."""
    url = url if url is not None else os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./backtick.db")
    db_path = _sqlite_path(url)
    if not db_path:
        print(f"[backup_db] DATABASE_URL is not SQLite ({url!r}) — nothing to do.")
        return None

    src = Path(db_path).resolve()
    if not src.exists():
        print(f"[backup_db] no database at {src} yet — nothing to back up.")
        return None

    dest_dir = Path(out_dir) if out_dir else src.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"{src.stem}-{stamp}.db"

    # Online backup: consistent snapshot of a live DB, WAL included.
    with sqlite3.connect(str(src)) as conn, sqlite3.connect(str(dest)) as bck:
        conn.backup(bck)
    print(f"[backup_db] wrote {dest} ({dest.stat().st_size} bytes)")

    # Rotate: keep the newest `keep`, delete the rest.
    backups = sorted(dest_dir.glob(f"{src.stem}-*.db"))
    stale = backups[:-keep] if keep > 0 else []
    for old in stale:
        old.unlink()
        print(f"[backup_db] pruned {old.name}")

    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", type=int, default=14, help="how many backups to retain (default 14)")
    ap.add_argument("--dir", default=None, help="backup directory (default: <db dir>/backups)")
    args = ap.parse_args()
    run_backup(keep=args.keep, out_dir=args.dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
