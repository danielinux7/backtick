"""Cached Binance exchangeInfo with 24h TTL on disk.

Mirrors the parquet caching pattern from binance.py: read-through cache, fall
back to the live API on miss/stale, persist back to disk. Used by the symbol
typeahead so we don't hammer Binance per keystroke (exchangeInfo is ~3 MB raw)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .binance import CACHE_DIR, FUTURES_BASE, SPOT_BASE

TTL_SECONDS = 24 * 3600


def _cache_path(market: str) -> Path:
    return CACHE_DIR / f"exchange_info_{market}.json"


def _endpoint(market: str) -> str:
    base = FUTURES_BASE if market == "futures" else SPOT_BASE
    path = "/fapi/v1/exchangeInfo" if market == "futures" else "/api/v3/exchangeInfo"
    return base + path


def _strip(payload: dict) -> list[dict]:
    out = []
    for s in payload.get("symbols", []) or []:
        status = s.get("status") or s.get("contractStatus")
        if status != "TRADING":
            continue
        out.append({
            "symbol": s.get("symbol", "").upper(),
            "baseAsset": s.get("baseAsset", "").upper(),
            "quoteAsset": s.get("quoteAsset", "").upper(),
        })
    return out


def _load_cached(path: Path) -> tuple[list[dict], float] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data["symbols"], float(data["ts"])
    except Exception:
        return None


def _fetch_fresh(market: str) -> list[dict]:
    with httpx.Client(timeout=20) as client:
        r = client.get(_endpoint(market))
        r.raise_for_status()
        return _strip(r.json())


def list_symbols(market: str) -> list[dict]:
    """Return cached TRADING symbols for the market. Refreshes from Binance on
    cache miss or when the cache is older than TTL_SECONDS."""
    if market not in {"spot", "futures"}:
        return []
    path = _cache_path(market)
    cached = _load_cached(path)
    now = time.time()
    if cached is not None and (now - cached[1]) < TTL_SECONDS:
        return cached[0]
    try:
        symbols = _fetch_fresh(market)
    except Exception:
        # Falling back to stale cache is fine — a 25-hour-old symbol list is
        # still vastly better than a hard error blocking the symbol picker.
        if cached is not None:
            return cached[0]
        raise
    path.write_text(json.dumps({"symbols": symbols, "ts": now}))
    return symbols


def search_symbols(market: str, q: str, limit: int = 20) -> list[dict]:
    """Rank: exact match > prefix match > substring match. Symbol field only."""
    q = (q or "").upper().strip()
    if not q:
        return []
    all_symbols = list_symbols(market)
    exact: list[dict] = []
    prefix: list[dict] = []
    contains: list[dict] = []
    for s in all_symbols:
        sym = s["symbol"]
        if sym == q:
            exact.append(s)
        elif sym.startswith(q):
            prefix.append(s)
        elif q in sym:
            contains.append(s)
    return (exact + prefix + contains)[: max(1, min(limit, 100))]
