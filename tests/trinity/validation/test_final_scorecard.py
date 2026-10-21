from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
import inspect
import socket

import pytest

import trinity.validation.scorecard as scorecard_module
from tests.trinity.validation.test_outcome_expectations import _collected
from tests.trinity.validation.test_weekly_baskets import (
    _append_final,
    _append_run_and_expectations,
    _empty_official,
    _expectation,
    _official_collected,
)
from trinity.validation.baskets import WeeklyBasketError
from trinity.validation.protocol_v1 import PROTOCOL_HASH
from trinity.validation.scorecard import (
    FORMAL_INFERENCE_AVAILABLE,
    INSUFFICIENT_PRIMARY_USABLE_WEEKS,
    NO_OFFICIAL_WEEKS,
    FinalScorecardError,
    _circular_block_bootstrap_ci,
    bootstrap_seed,
    build_final_scorecard,
)
from trinity.validation.storage import ValidationStore


EXPECTED_PROTOCOL_HASH = "c04e4009962e2d93265b725e0c3213000151202e8ecd3dd45e60cf9f92ed59d3"


def _append_week(
    store, *, run_id: str, target: str, specs=(),
    scanner: float | str | None = None,
    luna: float | str | None = None,
    sol: float | str | None = None,
):
    collected = _empty_official(run_id=run_id, target=target) if not specs else \
        _official_collected(specs, run_id=run_id, target=target)
    _run, expectations = _append_run_and_expectations(store, collected)
    ticker = "AAA"
    for cohort, value in (("SCANNER", scanner), ("LUNA", luna), ("SOL", sol)):
        if value is None:
            continue
        expectation = _expectation(expectations, ticker, cohort, 20)
        _append_final(
            store, expectation,
            **({"dqf": True} if value == "DQF" else {"active_return": float(value)}),
        )
    return collected, expectations


def test_zero_official_weeks_and_smoke_are_excluded(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, _collected())
        score = build_final_scorecard(store)
    assert score.status == NO_OFFICIAL_WEEKS
    assert score.n_total_official_weeks == 0
    assert score.primary_mean_delta_ar20_pct is None
    assert score.primary_confidence_interval_95 is None
    assert score.primary_dqf_rate is None and score.primary_pending_rate is None
    assert score.diagnostics.missed_winner_20d.scanner_winners == 0


def test_zero_scanner_week_is_usable_zero_and_empty_sleeves_cost_zero(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_week(store, run_id="ZERO", target="2026-01-05")
        score = build_final_scorecard(store)
    assert score.n_total_official_weeks == score.n_primary_usable_weeks == 1
    assert score.primary_mean_delta_ar20_pct == 0.0
    assert [item.mean_delta_ar20_pct for item in score.cost_sensitivity] == [0.0, 0.0, 0.0]
    assert score.luna_minus_scanner.mean_delta_ar20_pct == 0.0
    assert score.sol_minus_luna.mean_delta_ar20_pct == 0.0


def test_primary_secondary_equal_week_weight_and_empty_sol_are_included(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_week(
            store, run_id="FULL", target="2026-01-05", specs=("SOL",),
            scanner=1.0, luna=2.0, sol=4.0,
        )
        _append_week(
            store, run_id="SCAN", target="2026-01-12", specs=("SCANNER",),
            scanner=3.0,
        )
        score = build_final_scorecard(store)
    # Weekly primary deltas are 3 and -3: weeks, not tickers, receive equal weight.
    assert score.primary_mean_delta_ar20_pct == 0.0
    assert score.luna_minus_scanner.mean_delta_ar20_pct == -1.0
    assert score.sol_minus_luna.mean_delta_ar20_pct == 1.0
    assert score.n_primary_usable_weeks == 2


def test_primary_fixed_denominators_rates_and_dqf_precedence(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_week(store, run_id="OK", target="2026-01-05")
        _append_week(
            store, run_id="PENDING", target="2026-01-12", specs=("SCANNER",),
        )
        _append_week(
            store, run_id="DQF", target="2026-01-19", specs=("SOL",),
            sol="DQF",  # Scanner remains pending; DQF must win.
        )
        score = build_final_scorecard(store)
    assert (score.n_total_official_weeks, score.n_primary_usable_weeks,
            score.n_primary_pending_weeks,
            score.n_primary_dqf_unavailable_weeks) == (3, 1, 1, 1)
    assert score.primary_pending_rate == pytest.approx(1 / 3)
    assert score.primary_dqf_rate == pytest.approx(1 / 3)
    assert score.n_total_official_weeks == (
        score.n_primary_usable_weeks + score.n_primary_pending_weeks
        + score.n_primary_dqf_unavailable_weeks
    )
    assert score.sol_minus_luna.n_dqf == 1


def test_all_pending_and_all_dqf_have_no_performance(tmp_path):
    with ValidationStore(tmp_path / "pending.sqlite3") as store:
        _append_week(store, run_id="P", target="2026-01-05", specs=("SCANNER",))
        pending = build_final_scorecard(store)
    assert pending.status == INSUFFICIENT_PRIMARY_USABLE_WEEKS
    assert pending.primary_mean_delta_ar20_pct is None
    assert pending.n_primary_pending_weeks == 1
    with ValidationStore(tmp_path / "dqf.sqlite3") as store:
        _append_week(
            store, run_id="D", target="2026-01-05", specs=("SCANNER",),
            scanner="DQF",
        )
        dqf = build_final_scorecard(store)
    assert dqf.primary_mean_delta_ar20_pct is None
    assert dqf.n_primary_dqf_unavailable_weeks == 1
    assert all(item.mean_delta_ar20_pct is None for item in dqf.cost_sensitivity)


def test_cost_sensitivity_uses_stage_values_and_empty_sol_pays_no_cost(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_week(
            store, run_id="COST", target="2026-01-05", specs=("SCANNER",),
            scanner=1.2,
        )
        score = build_final_scorecard(store)
    assert score.primary_mean_delta_ar20_pct == pytest.approx(-1.2)
    assert score.sensitivity(0).mean_delta_ar20_pct == pytest.approx(-1.2)
    assert score.sensitivity(10).mean_delta_ar20_pct == pytest.approx(-1.0)
    assert score.sensitivity(25).mean_delta_ar20_pct == pytest.approx(-0.7)


@pytest.mark.parametrize("weeks,formal", [(51, False), (52, True)])
def test_formal_gate_uses_primary_usable_weeks(tmp_path, weeks, formal):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        start = date(2025, 1, 6)
        for index in range(weeks):
            _append_week(
                store, run_id=f"RUN_{index:03d}",
                target=(start + timedelta(weeks=index)).isoformat(),
            )
        score = build_final_scorecard(store)
    assert score.status == (
        FORMAL_INFERENCE_AVAILABLE if formal
        else INSUFFICIENT_PRIMARY_USABLE_WEEKS
    )
    assert score.primary_confidence_interval_95 == ((0.0, 0.0) if formal else None)


def test_52_gate_does_not_use_total_weeks(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        start = date(2025, 1, 6)
        for index in range(60):
            kwargs = {} if index < 51 else {"specs": ("SCANNER",)}
            _append_week(
                store, run_id=f"RUN_{index:03d}",
                target=(start + timedelta(weeks=index)).isoformat(), **kwargs,
            )
        score = build_final_scorecard(store)
    assert score.n_total_official_weeks == 60
    assert score.n_primary_usable_weeks == 51
    assert score.status == INSUFFICIENT_PRIMARY_USABLE_WEEKS
    assert score.primary_confidence_interval_95 is None


def test_bootstrap_is_frozen_circular_block_deterministic():
    values = tuple(float(index % 7) for index in range(52))
    first = _circular_block_bootstrap_ci(values)
    second = _circular_block_bootstrap_ci(values)
    assert first == second
    assert isinstance(bootstrap_seed(), int)
    assert scorecard_module._BOOTSTRAP_REPLICATES == 10000
    assert scorecard_module._BLOCK_LENGTH == 4
    assert scorecard_module._CONFIDENCE == 0.95
    with pytest.raises(FinalScorecardError, match="requires 52"):
        _circular_block_bootstrap_ci(values[:-1])


def test_missed_winner_uses_complete_scanner_d20_and_frozen_membership(tmp_path):
    specs = ("SOL", "LUNA", "SCANNER", "SCANNER", "SCANNER")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _run, expectations = _append_run_and_expectations(
            store, _official_collected(specs, run_id="WIN", target="2026-01-05"),
        )
        for ticker, value in (("AAA", 10.0), ("BBB", 12.0), ("CCC", 15.0)):
            _append_final(store, _expectation(expectations, ticker, "SCANNER", 20),
                          active_return=value)
        _append_final(store, _expectation(expectations, "EEE", "SCANNER", 20), dqf=True)
        # DDD remains pending and both DDD/EEE must stay outside the winner denominator.
        result = build_final_scorecard(store).diagnostics.missed_winner_20d
    assert result.metric_class == "DIAGNOSTIC"
    assert result.threshold_active_return_pct_20d == 10.0
    assert (result.scanner_winners, result.luna_promoted, result.sol_promoted) == (3, 2, 1)
    assert (result.missed_by_luna_count, result.missed_by_sol_count) == (1, 2)
    assert result.missed_by_luna_rate == pytest.approx(1 / 3)
    assert result.missed_by_sol_rate == pytest.approx(2 / 3)


def test_scorecard_is_read_only_offline_frozen_and_has_no_legacy_mapping_api(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted")
    ))
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_week(store, run_id="ZERO", target="2026-01-05")
        before = store.connection.execute("SELECT COUNT(*) FROM validation_record").fetchone()[0]
        score = build_final_scorecard(store)
        after = store.connection.execute("SELECT COUNT(*) FROM validation_record").fetchone()[0]
    assert before == after
    with pytest.raises(FrozenInstanceError):
        score.status = "changed"
    with pytest.raises(FrozenInstanceError):
        score.luna_minus_scanner.n_usable = 99
    assert isinstance(score.cost_sensitivity, tuple)
    assert not hasattr(scorecard_module, "build_scorecard")
    assert not hasattr(scorecard_module, "missed_winner_rate_20d")
    assert "Mapping" not in inspect.getsource(scorecard_module)
    assert PROTOCOL_HASH == EXPECTED_PROTOCOL_HASH


def test_basket_contract_failures_propagate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        scorecard_module, "build_official_weekly_baskets",
        lambda _store: (_ for _ in ()).throw(WeeklyBasketError("broken lineage")),
    )
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        with pytest.raises(WeeklyBasketError, match="broken lineage"):
            build_final_scorecard(store)
