"""Fetch historical klines from Binance public REST with a local parquet cache."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data_cache"
CACHE_DIR.mkdir(exist_ok=True)

VALID_TFS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000,
}


def _to_ms(dt_str: str) -> int:
    dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _cache_path(market: str, symbol: str, tf: str) -> Path:
    return CACHE_DIR / f"{market}_{symbol}_{tf}.parquet"


def _endpoint(market: str) -> str:
    base = FUTURES_BASE if market == "futures" else SPOT_BASE
    path = "/fapi/v1/klines" if market == "futures" else "/api/v3/klines"
    return base + path


def _fetch_chunk(client: httpx.Client, market: str, symbol: str, tf: str,
                 start_ms: int, end_ms: int) -> list[list]:
    params = {"symbol": symbol, "interval": tf, "startTime": start_ms,
              "endTime": end_ms, "limit": 1000}
    r = client.get(_endpoint(market), params=params, timeout=30.0)
    r.raise_for_status()
    return r.json()


def _fetch_range(client: httpx.Client, market: str, symbol: str, tf: str,
                 start_ms: int, end_ms: int) -> list[list]:
    """Walk Binance in 1000-candle chunks to cover [start_ms, end_ms]."""
    rows: list[list] = []
    tf_ms = TF_MS[tf]
    cursor = start_ms
    while cursor < end_ms:
        chunk = _fetch_chunk(client, market, symbol, tf, cursor, end_ms)
        if not chunk:
            break
        rows.extend(chunk)
        last_open = chunk[-1][0]
        next_cursor = last_open + tf_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(chunk) < 1000:
            break
        time.sleep(0.05)
    return rows


def _missing_ranges(cached_ms: np.ndarray | None, start_ms: int, end_ms: int,
                    tf_ms: int) -> list[tuple[int, int]]:
    """Find sub-ranges of [start_ms, end_ms] not contiguously covered by cached_ms."""
    if cached_ms is None or len(cached_ms) == 0:
        return [(start_ms, end_ms)]

    in_range = cached_ms[(cached_ms >= start_ms) & (cached_ms <= end_ms)]
    if len(in_range) == 0:
        return [(start_ms, end_ms)]

    missing: list[tuple[int, int]] = []
    # gap at the front?
    if in_range[0] - start_ms > tf_ms:
        missing.append((start_ms, int(in_range[0]) - 1))
    # internal gaps?
    if len(in_range) > 1:
        diffs = np.diff(in_range)
        for i, d in enumerate(diffs):
            if d > tf_ms * 1.5:
                missing.append((int(in_range[i]) + tf_ms, int(in_range[i + 1]) - 1))
    # gap at the tail?
    if end_ms - in_range[-1] > tf_ms:
        missing.append((int(in_range[-1]) + tf_ms, end_ms))
    return missing


def _rows_to_df(rows: list[list]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbav", "tqav", "ignore",
    ])
    df["time"] = (df["open_time"] // 1000).astype("int64")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df[["time", "open", "high", "low", "close", "volume"]] \
        .drop_duplicates("time").sort_values("time").reset_index(drop=True)


def fetch_klines(symbol: str, tf: str, start: str, end: str,
                 market: str = "spot", use_cache: bool = True) -> pd.DataFrame:
    """Return DataFrame with columns: time (sec), open, high, low, close, volume."""
    if tf not in VALID_TFS:
        raise ValueError(f"timeframe {tf} not supported")
    if market not in ("spot", "futures"):
        raise ValueError("market must be 'spot' or 'futures'")
    symbol = symbol.upper()

    start_ms = _to_ms(start)
    end_ms = _to_ms(end)
    if end_ms <= start_ms:
        raise ValueError("end must be after start")

    cache_file = _cache_path(market, symbol, tf)
    cached: pd.DataFrame | None = None
    if use_cache and cache_file.exists():
        cached = pd.read_parquet(cache_file)

    cached_ms = (cached["time"].to_numpy() * 1000).astype(np.int64) if cached is not None else None
    gaps = _missing_ranges(cached_ms, start_ms, end_ms, TF_MS[tf])

    if gaps:
        new_rows: list[list] = []
        with httpx.Client() as client:
            for g_start, g_end in gaps:
                new_rows.extend(_fetch_range(client, market, symbol, tf, g_start, g_end))
        if new_rows:
            new_df = _rows_to_df(new_rows)
            if cached is not None:
                cached = pd.concat([cached, new_df]).drop_duplicates("time") \
                    .sort_values("time").reset_index(drop=True)
            else:
                cached = new_df
            cached.to_parquet(cache_file, index=False)

    if cached is None or cached.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    mask = (cached["time"] * 1000 >= start_ms) & (cached["time"] * 1000 <= end_ms)
    return cached.loc[mask].reset_index(drop=True)
