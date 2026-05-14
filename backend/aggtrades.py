"""Fetch and cache Binance aggTrades — used for time-and-sales, CVD,
volume profile at exact prints, and liquidation-cluster heuristics.

Two sources:
  * data.binance.vision daily ZIPs (complete days, bulk download, cached as parquet)
  * REST API /api/v3/aggTrades or /fapi/v1/aggTrades (today's incomplete day, or
    if the vision archive 404s for a recent day not yet published)
"""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
from zipfile import ZipFile

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data_cache" / "aggtrades"
CACHE.mkdir(parents=True, exist_ok=True)

# binance.vision CSV columns (no header row in current dumps)
# spot has one extra "is_best_match" column at the end
_COLS_FUT = ["agg_id", "price", "qty", "first_id", "last_id", "time_ms", "is_buyer_maker"]
_COLS_SPOT = _COLS_FUT + ["is_best_match"]

OUT_COLS = ["time_ms", "price", "qty", "is_buyer_maker"]


def _empty_df() -> pd.DataFrame:
    """Empty frame with the right dtypes — important so concats with non-empty
    frames don't degrade `is_buyer_maker` from bool to object/int64."""
    return pd.DataFrame({
        "time_ms": pd.Series(dtype="int64"),
        "price": pd.Series(dtype="float64"),
        "qty": pd.Series(dtype="float64"),
        "is_buyer_maker": pd.Series(dtype="bool"),
    })


def _normalize_time_ms(s: pd.Series) -> pd.Series:
    """Coerce a timestamp column to int64 ms. Binance Vision newer dumps use µs."""
    if s.empty:
        return s.astype("int64")
    sample = int(s.iloc[0])
    if sample > 10 ** 17:        # nanoseconds
        return (s // 1_000_000).astype("int64")
    if sample > 10 ** 14:        # microseconds
        return (s // 1_000).astype("int64")
    return s.astype("int64")


def _cache_path(symbol: str, market: str, date: str) -> Path:
    return CACHE / f"{symbol.upper()}_{market}_{date}.parquet"


def _vision_url(symbol: str, market: str, date: str) -> str:
    base = "https://data.binance.vision/data"
    if market == "futures":
        return f"{base}/futures/um/daily/aggTrades/{symbol.upper()}/{symbol.upper()}-aggTrades-{date}.zip"
    return f"{base}/spot/daily/aggTrades/{symbol.upper()}/{symbol.upper()}-aggTrades-{date}.zip"


def _fetch_vision_day(symbol: str, market: str, date: str) -> pd.DataFrame:
    url = _vision_url(symbol, market, date)
    with httpx.Client(timeout=60) as client:
        r = client.get(url)
    if r.status_code == 404:
        raise FileNotFoundError(f"vision archive not found for {symbol} {market} {date}")
    r.raise_for_status()
    with ZipFile(io.BytesIO(r.content)) as zf:
        csv_name = zf.namelist()[0]
        first_line = zf.open(csv_name).readline().decode(errors="ignore")
        has_header = not first_line.split(",")[0].strip().split(".")[0].lstrip("-").isdigit()
        cols = _COLS_SPOT if market != "futures" else _COLS_FUT
        with zf.open(csv_name) as f:
            df = pd.read_csv(f, names=cols, header=0 if has_header else None,
                             usecols=["time_ms", "price", "qty", "is_buyer_maker"])
    df["time_ms"] = _normalize_time_ms(df["time_ms"])
    df["price"] = df["price"].astype("float64")
    df["qty"] = df["qty"].astype("float64")
    if df["is_buyer_maker"].dtype == object:
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(str).str.lower().isin(["true", "1"])
    df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
    return df[OUT_COLS]


def _fetch_rest_range(symbol: str, market: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Pull aggTrades via REST for a (typically short) range, paginated by trade id.
    Binance caps each response at 1000 rows; we step forward by the last trade's time + 1.
    On 429 (rate limit) we abort cleanly and return whatever's collected so far rather
    than propagating the error — calling code degrades to 'no data for this window'."""
    base = "https://fapi.binance.com" if market == "futures" else "https://api.binance.com"
    path = "/fapi/v1/aggTrades" if market == "futures" else "/api/v3/aggTrades"
    rows: list[dict] = []
    cur = start_ms
    with httpx.Client(timeout=30) as client:
        while cur < end_ms:
            try:
                r = client.get(f"{base}{path}", params={
                    "symbol": symbol.upper(),
                    "startTime": cur,
                    "endTime": min(end_ms, cur + 3_600_000),
                    "limit": 1000,
                })
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    break       # rate limited — stop fetching, return partial
                raise
            batch = r.json()
            if not batch:
                cur = min(end_ms, cur + 3_600_000)
                continue
            rows.extend(batch)
            last_t = int(batch[-1]["T"])
            cur = last_t + 1
            if len(batch) < 1000:
                cur = max(cur, min(end_ms, cur + 3_600_000))
    if not rows:
        return _empty_df()
    df = pd.DataFrame({
        "time_ms": [int(r["T"]) for r in rows],
        "price": [float(r["p"]) for r in rows],
        "qty": [float(r["q"]) for r in rows],
        "is_buyer_maker": [bool(r["m"]) for r in rows],
    }).astype({"time_ms": "int64", "price": "float64",
               "qty": "float64", "is_buyer_maker": "bool"})
    return df.sort_values("time_ms").reset_index(drop=True)


def _ensure_day(symbol: str, market: str, date: str) -> pd.DataFrame:
    cache = _cache_path(symbol, market, date)
    if cache.exists():
        try:
            df = pd.read_parquet(cache)
            df["time_ms"] = _normalize_time_ms(df["time_ms"])
            return df
        except Exception:
            cache.unlink(missing_ok=True)
    today_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        df = _fetch_vision_day(symbol, market, date)
    except FileNotFoundError:
        if date >= today_iso:
            # binance.vision publishes complete past days only — today isn't there
            # yet. Skip REST to avoid burning the rate limit; live mode is expected
            # to feed current-day data through the frontend WebSocket buffer.
            return _empty_df()
        start_ms = int(dt.datetime.fromisoformat(date)
                       .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        end_ms = start_ms + 86_400_000
        df = _fetch_rest_range(symbol, market, start_ms, end_ms)
    if date < today_iso and not df.empty:
        df.to_parquet(cache, index=False)
    return df


def fetch_agg_trades(symbol: str, start_ms: int, end_ms: int,
                     market: str = "spot") -> pd.DataFrame:
    """Return aggTrades inside [start_ms, end_ms]. Pulls full days from cache /
    Vision and slices to the requested range."""
    if end_ms < start_ms:
        return pd.DataFrame(columns=OUT_COLS)
    d0 = dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc).date()
    pieces: list[pd.DataFrame] = []
    cur = d0
    while cur <= d1:
        day = _ensure_day(symbol, market, cur.isoformat())
        if not day.empty:                # skip empty pieces so concat doesn't downcast dtypes
            pieces.append(day)
        cur += dt.timedelta(days=1)
    if not pieces:
        return _empty_df()
    df = pd.concat(pieces, ignore_index=True)
    mask = (df["time_ms"] >= start_ms) & (df["time_ms"] <= end_ms)
    return df.loc[mask].sort_values("time_ms").reset_index(drop=True)
