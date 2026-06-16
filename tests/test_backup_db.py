"""Unit tests for the SQLite online-backup helper (scripts/backup_db.py)."""
import sqlite3

import pytest

from scripts.backup_db import _sqlite_path, run_backup


@pytest.mark.parametrize("url, expected", [
    ("sqlite+aiosqlite:///./backtick.db", "./backtick.db"),
    ("sqlite+aiosqlite:////var/data/backtick.db", "/var/data/backtick.db"),
    ("sqlite:///rel.db", "rel.db"),
    ("sqlite:////abs/rel.db", "/abs/rel.db"),
    # not a sqlite *file* → None (no backup)
    ("postgresql+asyncpg://u:p@host/db", None),
    ("sqlite+aiosqlite:///:memory:", None),
    ("sqlite:///", None),
])
def test_sqlite_path(url, expected):
    assert _sqlite_path(url) == expected


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('hello')")
    con.commit()
    con.close()


def test_run_backup_writes_consistent_copy(tmp_path):
    db = tmp_path / "app.db"
    _make_db(db)
    url = f"sqlite:///{db}"

    dest = run_backup(url=url, keep=14)

    assert dest is not None and dest.exists()
    assert dest.parent == tmp_path / "backups"
    # the backup is a real, openable copy with the same row
    con = sqlite3.connect(str(dest))
    assert con.execute("SELECT v FROM t").fetchone()[0] == "hello"
    con.close()


def test_run_backup_rotates_to_keep(tmp_path):
    db = tmp_path / "app.db"
    _make_db(db)
    url = f"sqlite:///{db}"
    out = tmp_path / "bk"

    # Pre-seed more backups than we'll keep, with sortable older timestamps.
    out.mkdir()
    for stamp in ("20200101T000000Z", "20200102T000000Z", "20200103T000000Z"):
        (out / f"app-{stamp}.db").write_bytes(b"old")

    dest = run_backup(url=url, keep=2, out_dir=str(out))

    remaining = sorted(p.name for p in out.glob("app-*.db"))
    assert len(remaining) == 2          # rotated down to --keep
    assert dest.name in remaining       # the fresh one survives


def test_run_backup_noop_on_postgres(tmp_path):
    assert run_backup(url="postgresql+asyncpg://u:p@h/db") is None


def test_run_backup_noop_when_missing(tmp_path):
    # valid sqlite URL but the file doesn't exist yet
    assert run_backup(url=f"sqlite:///{tmp_path / 'nope.db'}") is None
