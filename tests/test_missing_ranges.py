"""Unit tests for binance._missing_ranges — the parquet-cache gap-fill logic.

Pure function, no network. tf_ms = 60_000 (1m) throughout; timestamps are ms.
"""
import numpy as np

from backend.binance import _missing_ranges

TF = 60_000


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
