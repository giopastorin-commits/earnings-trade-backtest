from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, timedelta
import socket

import pytest

import trinity.validation.simple_quant_baseline as baseline_module
from tests.trinity.validation.test_outcome_expectations import _collected
from tests.trinity.validation.test_weekly_baskets import (
    _append_final,
    _append_run_and_expectations,
    _empty_official,
    _expectation,
    _official_collected,
)
from trinity.validation.baseline_v1 import (
    BASELINE, BASELINE_HASH, BASELINE_ID, BASELINE_VERSION,
)
from trinity.validation.baskets import (
    AVAILABLE, EMPTY_BENCHMARK, PENDING, UNAVAILABLE_DQF,
)
from trinity.validation.protocol_v1 import PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID
from trinity.validation.simple_quant_baseline import (
    FORMAL_INFERENCE_AVAILABLE,
    INSUFFICIENT_USABLE_WEEKS,
    NO_OFFICIAL_WEEKS,
    SimpleQuantBaselineError,
    _bootstrap_ci,
    baseline_bootstrap_seed,
    build_quant_baseline_weeks,
    build_simple_quant_comparator,
)
from trinity.validation.storage import ValidationStore


EXPECTED_PROTOCOL_HASH = "c04e4009962e2d93265b725e0c3213000151202e8ecd3dd45e60cf9f92ed59d3"
SHA = "9" * 64


def _setup_payload(
    run_id: str, ticker: str, *, rr1: object = 2.0, rr2: object = 3.0,
    setup_type: str = "PULLBACK",
):
    operational = setup_type != "NO_SETUP"
    return {
        "run_id": run_id, "ticker": ticker, "setup_id": None,
        "setup_sha256": SHA, "setup_type": setup_type,
        "setup_timestamp": "2026-01-05", "entry_condition": None,
        "entry_level": 100.0 if operational else None,
        "entry_lower": None, "entry_upper": None,
        "stop": 90.0 if operational else None,
        "tp1": 110.0 if operational else None,
        "tp2": 120.0 if operational else None,
        "original_rr1": rr1 if operational else None,
        "original_rr2": rr2 if operational else None,
        "expiry": None, "protocol_version": PROTOCOL_ID,
        "protocol_epoch": PROTOCOL_EPOCH,
    }


def _append_setup(store, run_id, ticker, **kwargs):
    payload = _setup_payload(run_id, ticker, **kwargs)
    key = f"{PROTOCOL_ID}:{PROTOCOL_EPOCH}:{run_id}:{ticker}"
    assert store.append("SETUP", key, payload)


def _append_case(
    store, *, run_id="RUN", target="2026-01-05", specs=(), setup_values=None,
):
    collected = _empty_official(run_id=run_id, target=target) if not specs else \
        _official_collected(specs, run_id=run_id, target=target)
    _run, expectations = _append_run_and_expectations(store, collected)
    values = setup_values or {}
    for ticker in sorted(selection.ticker for selection in collected.selections):
        _append_setup(store, run_id, ticker, **values.get(ticker, {}))
    return collected, expectations


def test_baseline_spec_is_separate_frozen_and_bound_to_unchanged_protocol():
    assert BASELINE_ID == "TRINITY_SIMPLE_QUANT_BASELINE_V1"
    assert BASELINE_VERSION == 1 and len(BASELINE_HASH) == 64
    assert BASELINE["upstream_protocol_id"] == PROTOCOL_ID
    assert BASELINE["upstream_protocol_hash"] == PROTOCOL_HASH == EXPECTED_PROTOCOL_HASH
    assert BASELINE["ranking"] == (
        "ORIGINAL_RR1_DESC", "ORIGINAL_RR2_DESC", "TICKER_ASC",
    )


def test_zero_official_runs_and_smoke_are_excluded(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, _collected())
        result = build_simple_quant_comparator(store)
    assert result.status == NO_OFFICIAL_WEEKS
    assert result.n_total_official_weeks == 0
    assert result.mean_delta_ar20_pct is None and result.confidence_interval_95 is None


def test_zero_scanner_and_zero_sol_are_retained_as_empty_zero_delta(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_case(store, run_id="EMPTY")
        week = build_quant_baseline_weeks(store)[0]
        result = build_simple_quant_comparator(store)
    assert week.selection_count == 0 and week.members == ()
    assert week.status == EMPTY_BENCHMARK and week.gross_active_return_pct == 0.0
    assert result.n_total_official_weeks == result.n_usable == 1
    assert result.mean_delta_ar20_pct == 0.0


@pytest.mark.parametrize("mode", ["missing", "extra", "bad_key", "nonfinite"])
def test_setup_set_and_lineage_are_exact_and_fail_closed(tmp_path, mode):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        collected, _ = _append_run_and_expectations(
            store, _official_collected(("SOL",), run_id="RUN"),
        )
        if mode != "missing":
            payload = _setup_payload(
                "RUN", "AAA", rr1="not-a-number" if mode == "nonfinite" else 2.0,
            )
            key = f"{PROTOCOL_ID}:{PROTOCOL_EPOCH}:RUN:AAA"
            if mode == "bad_key":
                key += ":DUPLICATE"
            store.append("SETUP", key, payload)
        if mode == "extra":
            _append_setup(store, "RUN", "BBB")
        with pytest.raises(SimpleQuantBaselineError):
            build_quant_baseline_weeks(store)
    assert collected.run_id == "RUN"


def test_no_setup_is_excluded_and_capacity_overflow_fails_closed(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_case(
            store, specs=("SOL", "SCANNER"),
            setup_values={"AAA": {"setup_type": "NO_SETUP"}},
        )
        week = build_quant_baseline_weeks(store)[0]
        assert week.members == ("BBB",) and week.selection_count == 1
    with ValidationStore(tmp_path / "overflow.sqlite3") as store:
        _append_case(
            store, specs=("SOL", "SOL"),
            setup_values={"AAA": {"setup_type": "NO_SETUP"}},
        )
        with pytest.raises(SimpleQuantBaselineError, match="exceeds operational"):
            build_quant_baseline_weeks(store)


def test_frozen_ranking_rr1_rr2_and_ticker_tiebreak(tmp_path):
    values = {
        "AAA": {"rr1": 3.0, "rr2": 1.0},
        "BBB": {"rr1": 4.0, "rr2": 0.5},
        "CCC": {"rr1": 3.0, "rr2": 2.0},
        "DDD": {"rr1": 3.0, "rr2": 2.0},
    }
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_case(
            store, specs=("SOL", "SOL", "SOL", "SCANNER"), setup_values=values,
        )
        week = build_quant_baseline_weeks(store)[0]
    assert week.selection_count == 3
    assert week.members == ("BBB", "CCC", "DDD")
    assert all(item.ticker in {"AAA", "BBB", "CCC", "DDD"} for item in week.ranking_evidence)


def test_sol_identity_has_zero_effect_when_capacity_is_equal(tmp_path):
    members = []
    for filename, specs in (("first.sqlite3", ("SOL", "SCANNER")),
                            ("second.sqlite3", ("SCANNER", "SOL"))):
        with ValidationStore(tmp_path / filename) as store:
            _append_case(
                store, specs=specs,
                setup_values={
                    "AAA": {"rr1": 1.0, "rr2": 1.0},
                    "BBB": {"rr1": 5.0, "rr2": 5.0},
                },
            )
            members.append(build_quant_baseline_weeks(store)[0].members)
    assert members == [("BBB",), ("BBB",)]


def test_available_uses_scanner_d20_directly_equal_weight_and_costs(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _collected_run, expectations = _append_case(
            store, specs=("SOL", "SOL"),
            setup_values={
                "AAA": {"rr1": 5.0}, "BBB": {"rr1": 4.0},
            },
        )
        _append_final(store, _expectation(expectations, "AAA", "SCANNER", 20),
                      active_return=2.0, benchmark_return=80.0)
        _append_final(store, _expectation(expectations, "BBB", "SCANNER", 20),
                      active_return=4.0, benchmark_return=-50.0)
        week = build_quant_baseline_weeks(store)[0]
    assert week.status == AVAILABLE
    assert (week.complete_count, week.pending_count, week.dqf_count) == (2, 0, 0)
    assert week.gross_active_return_pct == 3.0
    assert dict(week.cost_adjusted_active_return_pct) == {0: 3.0, 10: 2.8, 25: 2.5}


def test_pending_and_dqf_precedence_forbid_partial_average_and_zero_imputation(tmp_path):
    with ValidationStore(tmp_path / "pending.sqlite3") as store:
        _collected_run, expectations = _append_case(store, specs=("SOL", "SOL"))
        _append_final(store, _expectation(expectations, "AAA", "SCANNER", 20),
                      active_return=8.0)
        pending = build_quant_baseline_weeks(store)[0]
    assert pending.status == PENDING and pending.gross_active_return_pct is None
    assert (pending.complete_count, pending.pending_count, pending.dqf_count) == (1, 1, 0)
    with ValidationStore(tmp_path / "dqf.sqlite3") as store:
        _collected_run, expectations = _append_case(store, specs=("SOL", "SOL"))
        _append_final(store, _expectation(expectations, "AAA", "SCANNER", 20), dqf=True)
        dqf = build_quant_baseline_weeks(store)[0]
    assert dqf.status == UNAVAILABLE_DQF and dqf.gross_active_return_pct is None
    assert (dqf.complete_count, dqf.pending_count, dqf.dqf_count) == (0, 1, 1)
    assert all(value is None for value in dqf.cost_adjusted_active_return_pct.values())


def test_sol_minus_baseline_formula_fixed_denominator_week_weighting_and_costs(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        for index, (scanner_return, sol_return) in enumerate(((1.0, 4.0), (5.0, 4.0))):
            _run, expectations = _append_case(
                store, run_id=f"RUN_{index}",
                target=(date(2026, 1, 5) + timedelta(weeks=index)).isoformat(),
                specs=("SOL",),
            )
            _append_final(store, _expectation(expectations, "AAA", "SCANNER", 20),
                          active_return=scanner_return)
            _append_final(store, _expectation(expectations, "AAA", "SOL", 20),
                          active_return=sol_return)
        result = build_simple_quant_comparator(store)
    # Weekly deltas 3 and -1 have equal weight.
    assert (result.n_total_official_weeks, result.n_usable, result.n_pending, result.n_dqf) == (2, 2, 0, 0)
    assert result.mean_delta_ar20_pct == 1.0
    assert [item.mean_delta_ar20_pct for item in result.cost_sensitivity] == [1.0, 1.0, 1.0]


def test_comparator_pending_and_dqf_fixed_denominators(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_case(store, run_id="EMPTY", target="2026-01-05")
        _append_case(store, run_id="PENDING", target="2026-01-12", specs=("SOL",))
        _run, expectations = _append_case(
            store, run_id="DQF", target="2026-01-19", specs=("SOL",),
        )
        _append_final(store, _expectation(expectations, "AAA", "SCANNER", 20), dqf=True)
        result = build_simple_quant_comparator(store)
    assert (result.n_total_official_weeks, result.n_usable, result.n_pending, result.n_dqf) == (3, 1, 1, 1)
    assert result.pending_rate == pytest.approx(1 / 3)
    assert result.dqf_rate == pytest.approx(1 / 3)


@pytest.mark.parametrize("weeks,formal", [(51, False), (52, True)])
def test_formal_gate_uses_usable_weeks_and_bootstrap_is_deterministic(tmp_path, weeks, formal):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        start = date(2025, 1, 6)
        for index in range(weeks):
            _append_case(
                store, run_id=f"RUN_{index:03d}",
                target=(start + timedelta(weeks=index)).isoformat(),
            )
        result = build_simple_quant_comparator(store)
    assert result.status == (
        FORMAL_INFERENCE_AVAILABLE if formal else INSUFFICIENT_USABLE_WEEKS
    )
    assert result.confidence_interval_95 == ((0.0, 0.0) if formal else None)
    values = tuple(float(index % 5) for index in range(52))
    assert _bootstrap_ci(values) == _bootstrap_ci(values)
    assert isinstance(baseline_bootstrap_seed(), int)
    assert (baseline_module._REPLICATES, baseline_module._BLOCK_LENGTH,
            baseline_module._CONFIDENCE) == (10000, 4, 0.95)


def test_derived_baseline_is_read_only_offline_frozen_and_protocol_unchanged(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted")
    ))
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_case(store, run_id="EMPTY")
        before = store.connection.execute("SELECT COUNT(*) FROM validation_record").fetchone()[0]
        week = build_quant_baseline_weeks(store)[0]
        result = build_simple_quant_comparator(store)
        after = store.connection.execute("SELECT COUNT(*) FROM validation_record").fetchone()[0]
    assert before == after
    with pytest.raises(FrozenInstanceError):
        week.status = "changed"
    with pytest.raises(FrozenInstanceError):
        result.status = "changed"
    assert PROTOCOL_HASH == EXPECTED_PROTOCOL_HASH
