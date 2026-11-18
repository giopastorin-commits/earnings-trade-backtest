"""Synthetic-data tests for Pre-Event Covariates V0.1 (H2_PROTOCOL_V2_1)."""

from __future__ import annotations

from datetime import date, timedelta
import math
from statistics import pstdev

import pytest

from trinity.outcomes import _Bar, _realized_volatility
from trinity.pre_event_covariates import (
    PreEventCovariateInputError,
    compute_pre_event_covariates,
)


START = date(2026, 1, 5)


def _series(closes, volumes=None, start=START):
    volumes = volumes if volumes is not None else [1000.0] * len(closes)
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "close": c, "volume": v}
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def _alternating(n):
    return [100.0 * (1.01 if i % 2 else 1.0) for i in range(n)]


def test_constant_growth_has_zero_volatility():
    rows = _series([100.0 * 1.001 ** i for i in range(61)])
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "AFTER_MARKET")
    assert out["sigma_status"] == "COMPLETE"
    assert out["sigma_pre_60"] == pytest.approx(0.0, abs=1e-9)


def test_sigma_matches_outcome_engine_formula():
    closes = _alternating(61)
    rows = _series(closes)
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    bars = [_Bar(START + timedelta(days=i), c, c, c, c, 1000.0) for i, c in enumerate(closes)]
    assert out["sigma_pre_60"] == pytest.approx(_realized_volatility(bars[0], bars[1:]))
    log_returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    assert out["sigma_pre_60"] == pytest.approx(pstdev(log_returns) * math.sqrt(252) * 100)


def test_uses_only_the_61_closes_ending_at_reference():
    closes = [50.0, 500.0] + _alternating(61)  # extreme early values must be ignored
    rows = _series(closes)
    expected = compute_pre_event_covariates(
        rows[-1]["date"], rows[2:], 500, 400, "PRE_MARKET"
    )["sigma_pre_60"]
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    assert out["sigma_pre_60"] == pytest.approx(expected)
    assert out["price_window_start"] == rows[2]["date"]


def test_bars_after_reference_never_change_the_result():
    rows = _series(_alternating(61) + [100.0, 200.0, 50.0])
    reference = rows[60]["date"]
    base = compute_pre_event_covariates(reference, rows[:61], 500, 400, "AFTER_MARKET")
    mutated = [dict(row) for row in rows]
    for row in mutated[61:]:
        row["close"] = 9999.0
        row["volume"] = 0.0
    out = compute_pre_event_covariates(reference, mutated, 500, 400, "AFTER_MARKET")
    assert out == base


def test_insufficient_history_is_missing_not_error():
    rows = _series(_alternating(60))
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    assert out["sigma_pre_60"] is None
    assert out["sigma_status"] == "INSUFFICIENT_HISTORY"
    assert out["dollar_volume_pre_60"] is None
    assert out["price_window_sha256"] is None


def test_reference_session_absent_is_missing():
    rows = _series(_alternating(61))
    out = compute_pre_event_covariates("2030-01-01", rows, 500, 400, "PRE_MARKET")
    assert out["sigma_status"] == "REFERENCE_SESSION_NOT_FOUND"
    assert out["sigma_pre_60"] is None


@pytest.mark.parametrize("bad", [0.0, -1.0, None, float("nan"), "x"])
def test_invalid_close_in_window_makes_sigma_missing(bad):
    closes = _alternating(61)
    closes[10] = bad
    rows = _series(closes)
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    assert out["sigma_pre_60"] is None
    assert out["sigma_status"] == "INVALID_CLOSE"


def test_dollar_volume_is_mean_of_last_60_sessions():
    closes = [10.0] + [20.0] * 60
    volumes = [1e9] + [100.0 + i for i in range(60)]
    rows = _series(closes, volumes)
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    expected = sum(20.0 * (100.0 + i) for i in range(60)) / 60
    assert out["dollar_volume_pre_60"] == pytest.approx(expected)
    assert out["dollar_volume_status"] == "COMPLETE"


def test_zero_volume_is_valid_but_negative_volume_is_missing():
    rows = _series(_alternating(61), [0.0] * 61)
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    assert out["dollar_volume_pre_60"] == 0.0
    rows[-1]["volume"] = -5.0
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    assert out["dollar_volume_pre_60"] is None
    assert out["sigma_status"] == "COMPLETE"  # sigma unaffected by volume


def test_word_covariates():
    rows = _series(_alternating(61))
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 800, 400, "PRE_MARKET")
    assert out["log_word_ratio"] == pytest.approx(math.log(2.0))
    assert out["log_word_count"] == pytest.approx(math.log(800))
    for bad in (0, -3, None, 4.5, True):
        out = compute_pre_event_covariates(rows[-1]["date"], rows, bad, 400, "PRE_MARKET")
        assert out["log_word_ratio"] is None and out["words_status"] == "INVALID_WORD_COUNT"


@pytest.mark.parametrize(
    "timing, expected",
    [("AFTER_MARKET", 1), ("PRE_MARKET", 0), ("INTRADAY", None), ("UNKNOWN", None)],
)
def test_timing_indicator(timing, expected):
    rows = _series(_alternating(61))
    out = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, timing)
    assert out["after_market"] == expected


def test_window_hash_is_deterministic_and_sensitive():
    rows = _series(_alternating(61))
    a = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    b = compute_pre_event_covariates(rows[-1]["date"], [dict(r) for r in rows], 500, 400, "PRE_MARKET")
    assert a["price_window_sha256"] == b["price_window_sha256"]
    rows[30]["close"] = rows[30]["close"] + 0.01
    c = compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
    assert c["price_window_sha256"] != a["price_window_sha256"]


def test_unordered_series_is_rejected():
    rows = _series(_alternating(61))
    rows[5], rows[6] = rows[6], rows[5]
    with pytest.raises(PreEventCovariateInputError):
        compute_pre_event_covariates(rows[-1]["date"], rows, 500, 400, "PRE_MARKET")
