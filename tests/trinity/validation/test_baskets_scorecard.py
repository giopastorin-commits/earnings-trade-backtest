from __future__ import annotations

import pytest

from trinity.validation.contracts import OFFICIAL_ELIGIBLE, SMOKE_ONLY, ValidationContractError
from trinity.validation.scorecard import build_scorecard, missed_winner_rate_20d


def _week(**changes):
    row = {"run_id": "official", "eligibility": OFFICIAL_ELIGIBLE,
           "scanner_ar20": 1, "luna_ar20": 2, "sol_ar20": 3}
    row.update(changes)
    return row


def test_small_sample_scorecard_withholds_confidence_interval():
    score = build_scorecard([_week()])
    assert score.status == "INSUFFICIENT_SAMPLE"
    assert score.primary_delta_ar20 == 2.0
    assert score.confidence_interval_95 is None


def test_formal_review_uses_frozen_deterministic_bootstrap():
    rows = [
        _week(run_id=f"official-{index}", scanner_ar20=float(index % 4),
              luna_ar20=float(index % 4) + 0.5, sol_ar20=float(index % 4) + 1.0)
        for index in range(52)
    ]
    first = build_scorecard(rows)
    second = build_scorecard(rows)
    assert first.status == "FORMAL_REVIEW_DUE"
    assert first.confidence_interval_95 == second.confidence_interval_95 == (1.0, 1.0)


def test_missed_winner_metric_is_diagnostic():
    result = missed_winner_rate_20d([
        {"run_id": "official", "eligibility": OFFICIAL_ELIGIBLE,
         "maturity_status": "COMPLETE", "active_return_20d": 12,
         "luna_promoted": True, "sol_evaluated": True, "sol_promoted": False},
        {"run_id": "official", "eligibility": OFFICIAL_ELIGIBLE,
         "maturity_status": "COMPLETE", "active_return_20d": 8,
         "luna_promoted": False, "sol_evaluated": False, "sol_promoted": False},
    ])
    assert result["metric_class"] == "DIAGNOSTIC"
    assert result["scanner_winners"] == 1
    assert result["luna_promoted"] == 1 and result["sol_promoted"] == 0


def test_smoke_or_mixed_rows_fail_closed_for_official_analytics():
    smoke = _week(run_id="smoke", eligibility=SMOKE_ONLY)
    with pytest.raises(ValidationContractError, match="non-OFFICIAL_ELIGIBLE"):
        build_scorecard([smoke])
    with pytest.raises(ValidationContractError, match="non-OFFICIAL_ELIGIBLE"):
        build_scorecard([_week(), smoke])
    with pytest.raises(ValidationContractError, match="non-OFFICIAL_ELIGIBLE"):
        missed_winner_rate_20d([smoke])
