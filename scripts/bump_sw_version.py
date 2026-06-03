#!/usr/bin/env python3
"""Bump frontend/sw.js's CACHE_VERSION to a content hash of the shell files.

Run automatically by Render's buildCommand on every deploy. After this
script writes the new version, Uvicorn serves sw.js with the new value,
the visitor's previously-installed service worker sees a different file,
the install handler precaches under the new cache key, and the activate
handler evicts the old keys — so cached app.js/style.css/index.html all
get refreshed on the next page load.

Safe to run locally (`python scripts/bump_sw_version.py`) too; it's
idempotent — if nothing changed, the file is left alone.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SW = ROOT / "frontend" / "sw.js"

# Files whose content invalidates the SW cache. Order is fixed for
# reproducibility — same input → same hash.
SHELL = [
    "frontend/index.html",
    "frontend/login.html",
    "frontend/landing.html",
    "frontend/docs.html",
    "frontend/about.html",
    "frontend/contact.html",
    "frontend/style.css",
    "frontend/site.css",
    "frontend/avatars.js",
    "frontend/app.js",
    "frontend/symbol-picker.js",
    "frontend/pwa.js",
    "frontend/auth-modal.js",
    "frontend/manifest.webmanifest",
    "frontend/sw.js",  # included with CACHE_VERSION line masked (see below)
]

VERSION_RE = re.compile(r'const CACHE_VERSION = "([^"]*)";')
MASKED_LINE = 'const CACHE_VERSION = "<masked>";'


def _content_for_hash(path: Path) -> bytes:
    """Return the bytes of `path` used for hashing. For sw.js itself we mask
    the CACHE_VERSION line — otherwise bumping the version would change the
    hash, which would prompt another bump, ad infinitum."""
    text = path.read_text(encoding="utf-8")
    if path.name == "sw.js":
        text = VERSION_RE.sub(MASKED_LINE, text, count=1)
    return text.encode("utf-8")


def compute_version() -> str:
    h = hashlib.sha256()
    for rel in SHELL:
        p = ROOT / rel
        if not p.exists():
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(_content_for_hash(p))
        h.update(b"\0")
    return h.hexdigest()[:10]


def main() -> int:
    if not SW.exists():
        print(f"[bump_sw_version] no sw.js at {SW}, skipping", file=sys.stderr)
        return 0
    text = SW.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        print("[bump_sw_version] no CACHE_VERSION line found, skipping", file=sys.stderr)
        return 0
    new_version = compute_version()
    if m.group(1) == new_version:
        print(f"[bump_sw_version] CACHE_VERSION unchanged ({new_version})", file=sys.stderr)
        return 0
    new_text = VERSION_RE.sub(f'const CACHE_VERSION = "{new_version}";', text, count=1)
    SW.write_text(new_text, encoding="utf-8")
    print(f"[bump_sw_version] CACHE_VERSION {m.group(1)} → {new_version}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
