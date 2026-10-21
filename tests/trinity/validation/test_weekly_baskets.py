from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import socket

import pytest

import trinity.validation.baskets as basket_module
from tests.trinity.validation.test_outcome_expectations import _collected, _selection
from trinity.validation.baskets import (
    AVAILABLE,
    EMPTY_BENCHMARK,
    PENDING,
    UNAVAILABLE_DQF,
    WeeklyBasketError,
    build_official_weekly_baskets,
)
from trinity.validation.contracts import OFFICIAL_ELIGIBLE
from trinity.validation.outcome_expectations import (
    OUTCOME_EXPECTATION, OutcomeExpectation, build_outcome_expectations,
)
from trinity.validation.outcome_finalization import (
    COMPLETE,
    DATA_QUALITY_FAILURE,
    OUTCOME_FINAL,
    TERMINAL_STOCK_D1_UNAVAILABLE,
    OutcomeFinal,
)
from trinity.validation.protocol_v1 import PROTOCOL_HASH, canonical_json
from trinity.validation.run_observation import (
    RUN_OBSERVATION, build_run_observation,
)
from trinity.validation.storage import ValidationStore


EXPECTED_PROTOCOL_HASH = "c04e4009962e2d93265b725e0c3213000151202e8ecd3dd45e60cf9f92ed59d3"
SHA = "d" * 64


def _official_collected(
    specs=("SCANNER",), *, run_id="RUN_A", target="2026-10-09",
):
    selections = tuple(
        replace(
            _selection(stage, ticker=chr(ord("A") + index) * 3),
            run_id=run_id, intended_session=target, eligibility=OFFICIAL_ELIGIBLE,
            data_quality_flags=(),
        )
        for index, stage in enumerate(specs)
    )
    collected = _collected(*selections)
    return replace(
        collected, run_id=run_id, eligibility=OFFICIAL_ELIGIBLE,
        decision_target_session=target, selections=selections,
    )


def _empty_official(*, run_id="EMPTY", target="2026-10-09"):
    return replace(
        _collected(), run_id=run_id, eligibility=OFFICIAL_ELIGIBLE,
        decision_target_session=target, selections=(), setups=(), executions=(),
        total_api_cost_usd="0",
    )


def _append_run_and_expectations(store, collected, *, expectations=None):
    run = build_run_observation(collected)
    assert store.append(RUN_OBSERVATION, run.key, run.to_dict())
    values = tuple(expectations) if expectations is not None else build_outcome_expectations(collected)
    for item in values:
        assert store.append(OUTCOME_EXPECTATION, item.key, item.to_dict())
    return run, values


def _payload_sha(expectation: OutcomeExpectation) -> str:
    return hashlib.sha256(canonical_json(expectation.to_dict())).hexdigest()


def _final(
    expectation: OutcomeExpectation, *, active_return: float = 2.0,
    benchmark_return: float = 0.0, dqf: bool = False,
) -> OutcomeFinal:
    if dqf:
        numeric = {
            "first_tradable_price": None, "future_price": None,
            "benchmark_first_tradable_price": None, "benchmark_future_price": None,
            "stock_return_pct": None, "benchmark_return_pct": None,
            "active_return_pct": None,
        }
        status = DATA_QUALITY_FAILURE
        classification = "CORPORATE_ACTION_NO_LONGER_TRADING"
        flags = (TERMINAL_STOCK_D1_UNAVAILABLE,)
    else:
        stock_return = active_return + benchmark_return
        numeric = {
            "first_tradable_price": 100.0,
            "future_price": 100.0 * (1.0 + stock_return / 100.0),
            "benchmark_first_tradable_price": 100.0,
            "benchmark_future_price": 100.0 * (1.0 + benchmark_return / 100.0),
            "stock_return_pct": stock_return,
            "benchmark_return_pct": benchmark_return,
            "active_return_pct": active_return,
        }
        status = COMPLETE
        classification = "ACTIVE_COMPLETE"
        flags = ()
    return OutcomeFinal(
        protocol_id=expectation.protocol_id, protocol_hash=expectation.protocol_hash,
        protocol_epoch=expectation.protocol_epoch, expectation_key=expectation.key,
        expectation_payload_sha256=_payload_sha(expectation), run_id=expectation.run_id,
        eligibility=expectation.eligibility, ticker=expectation.ticker,
        cohort=expectation.cohort, horizon_sessions=expectation.horizon_sessions,
        decision_timestamp_utc=expectation.decision_timestamp_utc,
        decision_target_session=expectation.decision_target_session,
        expected_first_tradable_session=expectation.expected_first_tradable_session,
        maturity_session=expectation.maturity_session, status=status,
        stock_classification=classification,
        observation_slot_id=SHA, slot_target_session="2030-01-02",
        observation_cycle_id=SHA, observation_cycle_sha256=SHA,
        cycle_manifest_sha256=SHA, stock_snapshot_id="STOCK_SNAPSHOT",
        stock_snapshot_sha256=SHA, stock_manifest_sha256=SHA,
        spy_snapshot_id="SPY_SNAPSHOT", spy_snapshot_sha256=SHA,
        spy_manifest_sha256=SHA, quality_flags=flags, **numeric,
    )


def _append_final(store, expectation, **kwargs):
    final = _final(expectation, **kwargs)
    assert store.append(OUTCOME_FINAL, final.key, final.to_dict())
    return final


def _expectation(expectations, ticker, cohort, horizon):
    return next(
        item for item in expectations
        if (item.ticker, item.cohort, item.horizon_sessions) == (ticker, cohort, horizon)
    )


def test_zero_scanner_run_is_retained_with_nine_empty_benchmark_baskets(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, _empty_official())
        weekly = build_official_weekly_baskets(store)
    assert len(weekly) == 1
    assert len(weekly[0].baskets) == 9
    assert {(item.horizon_sessions, item.cohort) for item in weekly[0].baskets} == {
        (horizon, cohort) for horizon in (5, 20, 60) for cohort in ("SCANNER", "LUNA", "SOL")
    }
    for item in weekly[0].baskets:
        assert item.status == EMPTY_BENCHMARK
        assert item.members == () and item.member_count == 0
        assert item.complete_count == item.pending_count == item.dqf_count == 0
        assert item.gross_active_return_pct == 0.0
        assert dict(item.cost_adjusted_active_return_pct) == {0: 0.0, 10: 0.0, 25: 0.0}


def test_empty_luna_and_sol_are_zero_while_nonempty_scanner_is_pending(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, _official_collected())
        week = build_official_weekly_baskets(store)[0]
    assert week.basket(20, "SCANNER").status == PENDING
    for cohort in ("LUNA", "SOL"):
        item = week.basket(20, cohort)
        assert item.status == EMPTY_BENCHMARK and item.gross_active_return_pct == 0.0


def test_available_scanner_luna_sol_use_equal_weight_authoritative_active_return(tmp_path):
    collected = _official_collected(("SOL", "SOL"))
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _run, expectations = _append_run_and_expectations(store, collected)
        for cohort in ("SCANNER", "LUNA", "SOL"):
            _append_final(store, _expectation(expectations, "AAA", cohort, 20),
                          active_return=2.0, benchmark_return=1.0)
            _append_final(store, _expectation(expectations, "BBB", cohort, 20),
                          active_return=4.0, benchmark_return=-2.0)
        week = build_official_weekly_baskets(store)[0]
    for cohort in ("SCANNER", "LUNA", "SOL"):
        item = week.basket(20, cohort)
        assert item.status == AVAILABLE
        assert item.gross_active_return_pct == 3.0
        assert (item.member_count, item.complete_count, item.pending_count, item.dqf_count) == (2, 2, 0, 0)
        assert dict(item.cost_adjusted_active_return_pct) == {0: 3.0, 10: 2.8, 25: 2.5}


def test_d5_d20_d60_are_independent_and_use_active_return_directly(tmp_path):
    collected = _official_collected()
    values = {5: 1.5, 20: 3.5, 60: -2.5}
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _run, expectations = _append_run_and_expectations(store, collected)
        for horizon, active in values.items():
            _append_final(
                store, _expectation(expectations, "AAA", "SCANNER", horizon),
                active_return=active, benchmark_return=7.0,
            )
        week = build_official_weekly_baskets(store)[0]
    for horizon, active in values.items():
        assert week.basket(horizon, "SCANNER").gross_active_return_pct == active


def test_missing_and_partial_finals_are_pending_with_exact_counts_and_no_cost(tmp_path):
    collected = _official_collected(("SCANNER", "SCANNER"))
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _run, expectations = _append_run_and_expectations(store, collected)
        _append_final(store, _expectation(expectations, "AAA", "SCANNER", 20), active_return=8.0)
        item = build_official_weekly_baskets(store)[0].basket(20, "SCANNER")
    assert item.status == PENDING and item.gross_active_return_pct is None
    assert (item.member_count, item.complete_count, item.pending_count, item.dqf_count) == (2, 1, 1, 0)
    assert dict(item.cost_adjusted_active_return_pct) == {0: None, 10: None, 25: None}


def test_dqf_precedes_pending_and_never_partial_averages_or_imputes_zero(tmp_path):
    collected = _official_collected(("SCANNER", "SCANNER", "SCANNER"))
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _run, expectations = _append_run_and_expectations(store, collected)
        _append_final(store, _expectation(expectations, "AAA", "SCANNER", 20), active_return=9.0)
        _append_final(store, _expectation(expectations, "BBB", "SCANNER", 20), dqf=True)
        item = build_official_weekly_baskets(store)[0].basket(20, "SCANNER")
    assert item.status == UNAVAILABLE_DQF and item.gross_active_return_pct is None
    assert (item.member_count, item.complete_count, item.pending_count, item.dqf_count) == (3, 1, 1, 1)
    assert dict(item.cost_adjusted_active_return_pct) == {0: None, 10: None, 25: None}


@pytest.mark.parametrize("mode", ["missing", "extra", "wrong_cohort"])
def test_expectation_set_must_exactly_match_frozen_membership(tmp_path, mode):
    collected = _official_collected()
    expectations = list(build_outcome_expectations(collected))
    if mode == "missing":
        expectations.pop()
    else:
        routed = _official_collected(("LUNA",))
        extra = next(
            item for item in build_outcome_expectations(routed) if item.cohort == "LUNA"
        )
        expectations.append(extra)
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, collected, expectations=expectations)
        with pytest.raises(WeeklyBasketError, match="exact frozen membership set"):
            build_official_weekly_baskets(store)


@pytest.mark.parametrize("field", ["decision_snapshot_sha256", "source_run_manifest_sha256"])
def test_run_expectation_lineage_mismatch_fails_closed(tmp_path, field):
    collected = _official_collected()
    expectations = list(build_outcome_expectations(collected))
    expectations[0] = replace(expectations[0], **{field: "e" * 64})
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, collected, expectations=expectations)
        with pytest.raises(WeeklyBasketError, match="lineage mismatch"):
            build_official_weekly_baskets(store)


def test_any_selection_quality_flag_fails_closed_for_official_analytics(tmp_path):
    collected = _official_collected()
    expectations = list(build_outcome_expectations(collected))
    expectations[0] = replace(
        expectations[0], selection_data_quality_flags=("SOL_CRITIC_LINEAGE_NOT_COMMITTED",),
    )
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, collected, expectations=expectations)
        with pytest.raises(WeeklyBasketError, match="data quality flags"):
            build_official_weekly_baskets(store)


def test_smoke_run_and_expectations_are_excluded_from_official_units(tmp_path):
    collected = _collected()
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, collected)
        assert build_official_weekly_baskets(store) == ()


@pytest.mark.parametrize("targets", [
    ("2026-10-05", "2026-10-09"),
    ("2026-09-08", "2026-09-09"),
])
def test_two_official_runs_in_same_iso_week_fail_closed_including_holiday_tuesday(tmp_path, targets):
    first = _empty_official(run_id="RUN_A", target=targets[0])
    second = _empty_official(run_id="RUN_B", target=targets[1])
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, first)
        _append_run_and_expectations(store, second)
        with pytest.raises(WeeklyBasketError, match="multiple official"):
            build_official_weekly_baskets(store)


def test_cost_is_one_round_trip_basket_rate_not_multiplied_by_member_count(tmp_path):
    collected = _official_collected(("SCANNER", "SCANNER"))
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _run, expectations = _append_run_and_expectations(store, collected)
        for ticker, active in (("AAA", 2.0), ("BBB", 4.0)):
            _append_final(store, _expectation(expectations, ticker, "SCANNER", 20),
                          active_return=active)
        item = build_official_weekly_baskets(store)[0].basket(20, "SCANNER")
    assert item.gross_active_return_pct == 3.0
    assert item.cost_adjusted_active_return_pct[10] == 2.8
    assert item.cost_adjusted_active_return_pct[25] == 2.5


def test_weekly_units_and_members_have_deterministic_chronological_order(tmp_path):
    later = _official_collected(("SCANNER", "SCANNER"), run_id="RUN_Z", target="2026-10-09")
    earlier = _official_collected(("SCANNER", "SCANNER"), run_id="RUN_A", target="2026-10-02")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, later)
        _append_run_and_expectations(store, earlier)
        weekly = build_official_weekly_baskets(store)
    assert [item.run_id for item in weekly] == ["RUN_A", "RUN_Z"]
    assert weekly[0].basket(5, "SCANNER").members == ("AAA", "BBB")
    assert [(item.horizon_sessions, item.cohort) for item in weekly[0].baskets] == [
        (horizon, cohort) for horizon in (5, 20, 60)
        for cohort in ("SCANNER", "LUNA", "SOL")
    ]


def test_engine_is_read_only_offline_and_protocol_hash_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted")
    ))
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_run_and_expectations(store, _empty_official())
        before = store.connection.execute("SELECT COUNT(*) FROM validation_record").fetchone()[0]
        assert build_official_weekly_baskets(store)
        after = store.connection.execute("SELECT COUNT(*) FROM validation_record").fetchone()[0]
    assert before == after
    assert PROTOCOL_HASH == EXPECTED_PROTOCOL_HASH
    source = inspect.getsource(basket_module)
    assert "SelectionObservation" not in source
    assert "freeze_memberships" not in source
    assert "equal_weight_return" not in source
    assert "store.append" not in source
