#!/usr/bin/env python3
"""Regenerate the PWA / home-screen icons from the boot-splash glyph so the
installed app's icon + Android launch splash match the in-app boot splash
(the gradient-tile candle in frontend/index.html). One-off generator — re-run
after tweaking the glyph. Needs cairosvg (in the venv, not a runtime dep).

    /tmp/backtick_venv/bin/python scripts/gen_icons.py
"""
from __future__ import annotations

from pathlib import Path

import cairosvg

ICONS = Path(__file__).resolve().parent.parent / "frontend" / "icons"

# The defs + candle group are shared; the tile differs between the rounded
# "any" icon and the full-bleed maskable variant. Kept byte-for-byte in sync
# with the #boot-splash <svg> in frontend/index.html.
_DEFS = """
  <defs>
    <linearGradient id="bt-body" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#6fe9da"/><stop offset="1" stop-color="#1f9488"/>
    </linearGradient>
    <linearGradient id="bt-tile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#1c222d"/><stop offset="1" stop-color="#0f141b"/>
    </linearGradient>
    <linearGradient id="bt-border" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#46d8c6"/>
      <stop offset="0.6" stop-color="#1c8d80" stop-opacity="0.35"/>
      <stop offset="1" stop-color="#1c8d80" stop-opacity="0.12"/>
    </linearGradient>
  </defs>"""

_CANDLE = """
  <g transform="rotate(38 32 32)">
    <line x1="32" y1="12" x2="32" y2="52" stroke="#4dd0c1" stroke-width="2.6" stroke-linecap="round"/>
    <rect x="25" y="21.5" width="14" height="21" rx="4" fill="url(#bt-body)"/>
    <rect x="27.6" y="24.5" width="3.2" height="15" rx="1.6" fill="#ffffff" opacity="0.20"/>
  </g>"""

# Rounded tile — the splash glyph as-is (transparent corners read as a rounded
# app icon). Used for icon-192/512, apple-touch-icon, favicon.
ROUNDED = f"""<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">{_DEFS}
  <rect x="3" y="3" width="58" height="58" rx="16" fill="url(#bt-tile)" stroke="url(#bt-border)" stroke-width="1.5"/>{_CANDLE}
</svg>"""

# Maskable — full-bleed dark square (the OS applies its own mask), candle shrunk
# into the ~80% safe zone so nothing important gets clipped.
MASKABLE = f"""<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">{_DEFS}
  <rect x="0" y="0" width="64" height="64" fill="#0f1218"/>
  <g transform="translate(32 32) scale(0.8) translate(-32 -32)">{_CANDLE}</g>
</svg>"""


def render(svg: str, name: str, size: int) -> None:
    out = ICONS / name
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(out),
                     output_width=size, output_height=size)
    print(f"  {name}  {size}x{size}")


def main() -> None:
    print(f"Writing icons to {ICONS}")
    render(ROUNDED, "icon-192.png", 192)
    render(ROUNDED, "icon-512.png", 512)
    render(ROUNDED, "apple-touch-icon.png", 180)
    render(ROUNDED, "favicon-32.png", 32)
    render(MASKABLE, "icon-maskable-512.png", 512)
    print("done.")


if __name__ == "__main__":
    main()
