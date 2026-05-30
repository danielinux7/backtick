"""Unit tests for binance._missing_ranges — the parquet-cache gap-fill logic.

Pure function, no network. tf_ms = 60_000 (1m) throughout; timestamps are ms.
"""
import numpy as np
import pytest

import backend.binance as binance
from backend.binance import (
    RateLimitedError, UnknownSymbolError, _fetch_chunk, _missing_ranges)

TF = 60_000


class _FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")  # mimics httpx on non-400

    def json(self):
        return []


class _FakeClient:
    def __init__(self, status_code, headers=None):
        self._status = status_code
        self._headers = headers

    def get(self, *_a, **_k):
        return _FakeResp(self._status, self._headers)


@pytest.fixture(autouse=True)
def _reset_cooldown():
    binance._blocked_until = 0.0
    yield
    binance._blocked_until = 0.0


def test_fetch_chunk_400_is_unknown_symbol():
    # Binance answers 400 for an unknown/invalid symbol — surface a clean,
    # caller-mappable UnknownSymbolError (→ 400 "unknown symbol"), not a generic
    # fetch failure (→ 502) whose URL would get mislabeled downstream.
    with pytest.raises(UnknownSymbolError):
        _fetch_chunk(_FakeClient(400), "spot", "ZZZZUSDT", "4h", 0, TF)


def test_fetch_chunk_429_is_rate_limited():
    # 429/418 are rate limiting, not a bad symbol — raise RateLimitedError and
    # honor Retry-After.
    with pytest.raises(RateLimitedError) as ei:
        _fetch_chunk(_FakeClient(429, {"Retry-After": "120"}),
                     "spot", "BTCUSDT", "4h", 0, TF)
    assert not isinstance(ei.value, UnknownSymbolError)
    assert ei.value.retry_after == 120


def test_rate_limit_guard_short_circuits_during_cooldown():
    # After a 418, the process-wide cooldown makes the next fetch fail fast
    # (no network) so we don't deepen the ban.
    with pytest.raises(RateLimitedError):
        _fetch_chunk(_FakeClient(418, {"Retry-After": "60"}),
                     "spot", "BTCUSDT", "4h", 0, TF)
    # a "live" client that would 200 must still be blocked by the guard
    with pytest.raises(RateLimitedError):
        _fetch_chunk(_FakeClient(200), "spot", "BTCUSDT", "4h", 0, TF)


def _ms(*minutes):
    """Array of ms timestamps at the given minute offsets."""
    return np.array([m * TF for m in minutes], dtype=np.int64)


def test_no_cache_requests_full_range():
    assert _missing_ranges(None, 0, 5 * TF, TF) == [(0, 5 * TF)]


def test_empty_cache_requests_full_range():
    assert _missing_ranges(np.array([], dtype=np.int64), 0, 5 * TF, TF) == [(0, 5 * TF)]


def test_no_overlap_requests_full_range():
    # cache sits entirely after the requested window
    cached = _ms(100, 101, 102)
    assert _missing_ranges(cached, 0, 5 * TF, TF) == [(0, 5 * TF)]


def test_fully_covered_returns_nothing():
    cached = _ms(0, 1, 2, 3, 4, 5)
    assert _missing_ranges(cached, 0, 5 * TF, TF) == []


def test_front_gap():
    cached = _ms(3, 4, 5)          # starts late
    gaps = _missing_ranges(cached, 0, 5 * TF, TF)
    assert gaps == [(0, 3 * TF - 1)]


def test_tail_gap():
    cached = _ms(0, 1, 2)          # ends early
    gaps = _missing_ranges(cached, 0, 5 * TF, TF)
    assert gaps == [(2 * TF + TF, 5 * TF)]


def test_internal_gap():
    cached = _ms(0, 1, 4, 5)       # missing minutes 2-3
    gaps = _missing_ranges(cached, 0, 5 * TF, TF)
    assert gaps == [(1 * TF + TF, 4 * TF - 1)]


def test_multiple_gaps_front_internal_tail():
    cached = _ms(2, 3, 6)          # window 0..9: front (0-1), internal (4-5), tail (7-9)
    gaps = _missing_ranges(cached, 0, 9 * TF, TF)
    assert gaps == [
        (0, 2 * TF - 1),
        (3 * TF + TF, 6 * TF - 1),
        (6 * TF + TF, 9 * TF),
    ]
