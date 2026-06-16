"""Periodic in-process SQLite backup.

Managed Postgres gave us automated backups for free; on SQLite-over-a-disk we own
them. A standalone Render Cron Job can't do it — a Render persistent disk attaches
to a single service, so a separate cron service can't read the web service's
`/var/data/backtick.db`. So we run the backup *inside* the app on a background
asyncio task.

Config (all optional, env-driven):
  BACKUP_INTERVAL_HOURS   how often to back up (default 24; <=0 disables)
  BACKUP_KEEP             how many backups to retain (default 14)
  BACKUP_STARTUP_DELAY_SEC  delay before the first backup (default 60)

No-op on a non-SQLite DATABASE_URL (run_backup itself also guards this).
"""
from __future__ import annotations

import asyncio
import os

from .db import DATABASE_URL
from scripts.backup_db import run_backup


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


async def _loop(interval_sec: float, keep: int, startup_delay: float) -> None:
    # Let cold-start work settle before the first backup.
    await asyncio.sleep(startup_delay)
    while True:
        try:
            # sqlite3's online backup is blocking — keep it off the event loop.
            await asyncio.to_thread(run_backup, DATABASE_URL, keep, None)
        except Exception as exc:  # a backup failure must never kill the loop
            print(f"[backup_db] scheduled backup failed: {exc!r}")
        await asyncio.sleep(interval_sec)


def start() -> "asyncio.Task | None":
    """Launch the periodic backup task. Returns the Task, or None when disabled
    (BACKUP_INTERVAL_HOURS <= 0, or DATABASE_URL isn't SQLite)."""
    interval_hours = _env_float("BACKUP_INTERVAL_HOURS", 24.0)
    if interval_hours <= 0 or not DATABASE_URL.startswith("sqlite"):
        return None
    keep = _env_int("BACKUP_KEEP", 14)
    delay = _env_float("BACKUP_STARTUP_DELAY_SEC", 60.0)
    return asyncio.create_task(_loop(interval_hours * 3600, keep, delay))
