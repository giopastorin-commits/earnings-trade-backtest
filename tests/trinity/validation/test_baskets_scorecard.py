from __future__ import annotations

import pytest

from trinity.validation.baskets import equal_weight_return, freeze_memberships
from trinity.validation.contracts import OFFICIAL_ELIGIBLE, SMOKE_ONLY, SelectionObservation, ValidationContractError
from trinity.validation.scorecard import build_scorecard, missed_winner_rate_20d


SHA = "a" * 64
GIT_SHA = "a" * 40


def _selection(ticker, luna="NOT_EVALUATED", sol="NOT_EVALUATED"):
    return SelectionObservation(
        run_id="run", intended_session="2026-10-09", run_timestamp_utc="2026-10-12T10:00:00Z",
        ticker=ticker, universe_version="v1", universe_sha256=SHA, scanner_selected=True,
        scanner_state="READY_TECHNICALLY", scanner_reason="reason",
        luna_evaluated=luna != "NOT_EVALUATED", luna_status="COMPLETED" if luna != "NOT_EVALUATED" else "NOT_EVALUATED",
        luna_decision=luna, luna_model=None, luna_prompt_version=None, luna_input_sha256=None,
        luna_output_sha256=None, luna_cost_usd="0", sol_evaluated=sol != "NOT_EVALUATED",
        sol_status=sol, sol_decision="INVESTIGATE" if sol == "PROMOTED" else sol,
        sol_model=None, sol_prompt_version=None, sol_input_sha256=None, sol_output_sha256=None,
        sol_cost_usd="0", analytical_commit_sha=GIT_SHA, snapshot_id="snap", snapshot_sha256=SHA,
    )


def test_memberships_preserve_watch_failure_rejection_and_not_evaluated():
    rows = (
        _selection("A", "WATCH"), _selection("B", "DROP"),
        _selection("C", "ESCALATE", "PROMOTED"),
        _selection("D", "EVALUATED_FAILED"),
    )
    cohorts = freeze_memberships(rows)
    assert cohorts.scanner == ("A", "B", "C", "D")
    assert cohorts.luna == ("C",)
    assert cohorts.sol == ("C",)
    assert rows[0].luna_decision == "WATCH" and rows[1].luna_decision == "DROP"
    assert rows[3].luna_decision == "EVALUATED_FAILED"
    assert rows[0].sol_status == "NOT_EVALUATED"
    with pytest.raises(AttributeError):
        rows[0].ticker = "MUTATED"


def test_sol_failure_and_rejection_are_distinct():
    rejected = _selection("A", "ESCALATE", "REJECTED")
    failed = _selection("B", "ESCALATE", "EVALUATED_FAILED")
    assert rejected.sol_evaluated is True and rejected.sol_status == "REJECTED"
    assert failed.sol_evaluated is True and failed.sol_status == "EVALUATED_FAILED"
    assert rejected.sol_status != failed.sol_status


def test_equal_weight_and_empty_sol_week_policy():
    assert equal_weight_return(("A", "B"), {"A": 2.0, "B": 4.0}) == 3.0
    assert equal_weight_return((), {}, benchmark_return=1.25, empty_sol=True) == 1.25
    with pytest.raises(ValidationContractError):
        equal_weight_return((), {})


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
