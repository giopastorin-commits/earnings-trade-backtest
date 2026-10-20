from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import socket

import pytest

import trinity.validation.outcome_finalization as finalization_module
from tests.trinity.validation.test_observation_cycle import _one
from tests.trinity.validation.test_spy_benchmark import _acquire
from tests.trinity.validation.test_stock_observation import _write_snapshot
from trinity.validation.cycle_registry import (
    OBSERVATION_CYCLE_REGISTRATION,
    load_registered_cycles,
    register_observation_cycle,
)
from trinity.validation.contracts import ValidationContractError
from trinity.validation.observation_cycle import create_observation_cycle
from trinity.validation.observation_slots import weekly_observation_slot
from trinity.validation.outcome_expectations import OUTCOME_EXPECTATION
from trinity.validation.outcome_finalization import (
    ALREADY_FINAL,
    COMPLETE,
    DATA_QUALITY_FAILURE,
    FINALIZED_COMPLETE,
    FINALIZED_DATA_QUALITY_FAILURE,
    OUTCOME_FINAL,
    PENDING_SOURCE_NOT_AVAILABLE,
    PENDING_UNASSIGNED,
    TERMINAL_STOCK_D1_UNAVAILABLE,
    TERMINAL_STOCK_DN_UNAVAILABLE,
    CycleSourceLocator,
    OutcomeFinal,
    OutcomeFinalizationError,
    _build_final,
    finalize_outcomes,
    load_outcome_finals,
)
from trinity.validation.protocol_v1 import (
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
    canonical_json,
)
from trinity.validation.slot_assignment import (
    AMBIGUOUS_OPERATIONAL_ERROR,
    OBSERVATION_SLOT_CLOSURE,
    OPERATIONAL_OBSERVATION_MISSING,
    ObservationSlotClosure,
    close_observation_slot,
    load_current_expectations,
    load_expectation_assignments,
)
from trinity.validation.spy_benchmark import load_spy_benchmark_snapshot
from trinity.validation.stock_observation import load_stock_observation_snapshot
from trinity.validation.storage import ValidationStorageConflict, ValidationStore
from trinity.twelvedata_prices import canonical_bytes, normalize_response, snapshot_hash


OLD_HASH = "134d9d6a3751c27952a3848baeff4d12642bfcccebada68b6984e95c2c8449a8"
TARGET = date(2027, 4, 5)
ISO_YEAR, ISO_WEEK, _ = TARGET.isocalendar()


def _expectations(ticker="AAPL", decision="2026-10-09T20:01:00Z"):
    return tuple(_one(ticker, decision=decision, horizon=horizon) for horizon in (5, 20, 60))


@pytest.fixture(scope="module")
def complete_artifacts(tmp_path_factory):
    root = tmp_path_factory.mktemp("outcome-finalization-complete")
    expectations = _expectations()
    stock = root / "stock"
    spy = root / "spy"
    cycle = root / "cycle"
    _write_snapshot(stock, ("AAPL",), target=TARGET)
    _acquire(
        spy, target=TARGET,
        started_at=datetime(2027, 4, 6, 10, tzinfo=timezone.utc),
    )
    create_observation_cycle(
        expectations=expectations, observation_target_session=TARGET.isoformat(),
        stock_root=stock, spy_root=spy, output_root=cycle,
    )
    return {
        "root": root, "expectations": expectations,
        "stock": stock, "spy": spy, "cycle": cycle,
    }


def _append_expectations(store, expectations):
    for item in expectations:
        assert store.append(OUTCOME_EXPECTATION, item.key, item.to_dict())


def _bind(store, artifacts):
    expectations = artifacts["expectations"]
    _append_expectations(store, expectations)
    assert register_observation_cycle(
        store, cycle_root=artifacts["cycle"], expectations=expectations,
        stock_root=artifacts["stock"], spy_root=artifacts["spy"],
    )
    slot = weekly_observation_slot(ISO_YEAR, ISO_WEEK)
    closure = close_observation_slot(
        store, iso_year=ISO_YEAR, iso_week=ISO_WEEK,
        closed_at_utc=slot.finality_eligible_at_utc,
    )
    registration = load_registered_cycles(store)[0]
    locator = CycleSourceLocator(
        registration.observation_cycle_id, artifacts["cycle"],
        artifacts["stock"], artifacts["spy"],
    )
    return closure, registration, locator


def _finalize(tmp_path, artifacts, *, with_locator=True):
    store = ValidationStore(tmp_path / "store.sqlite3")
    store.__enter__()
    closure, registration, locator = _bind(store, artifacts)
    summary = finalize_outcomes(store, cycle_sources=(locator,) if with_locator else ())
    return store, closure, registration, locator, summary


def test_unassigned_expectation_is_pending_and_never_persisted(tmp_path):
    expectations = _expectations()[:1]
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store, expectations)
        summary = finalize_outcomes(store)
        assert store.records(OUTCOME_FINAL) == []
    assert summary.pending_unassigned == 1
    assert summary.resolutions[0].state == PENDING_UNASSIGNED


@pytest.mark.parametrize("state", [
    OPERATIONAL_OBSERVATION_MISSING, AMBIGUOUS_OPERATIONAL_ERROR,
])
def test_missed_or_ambiguous_slot_remains_pending(tmp_path, state):
    expectation = _expectations()[0]
    slot = weekly_observation_slot(ISO_YEAR, ISO_WEEK)
    with ValidationStore(tmp_path / f"{state}.sqlite3") as store:
        _append_expectations(store, (expectation,))
        missing = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK,
            closed_at_utc=slot.finality_eligible_at_utc,
        )
        if state == AMBIGUOUS_OPERATIONAL_ERROR:
            payload = missing.to_dict()
            candidates = ["1" * 64, "2" * 64]
            payload.update({
                "closure_state": state,
                "same_target_cycle_ids": candidates,
                "exact_candidate_cycle_ids": candidates,
                "nonexact_candidate_cycle_ids": [],
            })
            closure = ObservationSlotClosure.from_dict(payload)
            raw = canonical_json(closure.to_dict())
            store.connection.execute(
                "UPDATE validation_record SET payload_json=?, payload_sha256=? "
                "WHERE record_kind=? AND record_key=?",
                (raw, hashlib.sha256(raw).hexdigest(),
                 OBSERVATION_SLOT_CLOSURE, closure.key),
            )
        summary = finalize_outcomes(store)
        assert store.records(OUTCOME_FINAL) == []
    assert summary.pending_unassigned == 1


def test_assigned_without_locator_is_pending_source_not_available(
    tmp_path, complete_artifacts,
):
    store, _closure, _registration, _locator, summary = _finalize(
        tmp_path, complete_artifacts, with_locator=False,
    )
    try:
        assert summary.pending_source_unavailable == 3
        assert {item.state for item in summary.resolutions} == {
            PENDING_SOURCE_NOT_AVAILABLE,
        }
        assert store.records(OUTCOME_FINAL) == []
    finally:
        store.__exit__(None, None, None)


def test_duplicate_cycle_source_locator_is_rejected(tmp_path, complete_artifacts):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, _registration, locator = _bind(store, complete_artifacts)
        with pytest.raises(OutcomeFinalizationError, match="duplicate"):
            finalize_outcomes(store, cycle_sources=(locator, locator))


def test_corrupt_supplied_source_fails_closed_without_any_final(
    tmp_path, complete_artifacts,
):
    copied = tmp_path / "corrupt-cycle"
    shutil.copytree(complete_artifacts["cycle"], copied)
    (copied / "READY").write_bytes(b"{}")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, registration, _locator = _bind(store, complete_artifacts)
        bad = CycleSourceLocator(
            registration.observation_cycle_id, copied,
            complete_artifacts["stock"], complete_artifacts["spy"],
        )
        with pytest.raises(OutcomeFinalizationError, match="source verification failed"):
            finalize_outcomes(store, cycle_sources=(bad,))
        assert store.records(OUTCOME_FINAL) == []


def test_exact_lineage_finalizes_d5_d20_d60_from_frozen_d1_dn(
    tmp_path, complete_artifacts,
):
    store, closure, registration, locator, summary = _finalize(
        tmp_path, complete_artifacts,
    )
    try:
        finals = load_outcome_finals(store, complete_artifacts["expectations"])
        stock = load_stock_observation_snapshot(
            complete_artifacts["stock"], expected_tickers=("AAPL",),
            expected_observation_target_session=TARGET.isoformat(),
        ).series("AAPL")
        spy = load_spy_benchmark_snapshot(
            complete_artifacts["spy"], trinity_target_session=TARGET,
        )
        stock_by_date = {str(row["date"]): row for row in stock.bars}
        spy_by_date = {str(row["date"]): row for row in spy.bars}
        assert summary.completed == 3 and summary.new_final_records == 3
        assert {item.horizon_sessions for item in finals} == {5, 20, 60}
        for final in finals:
            d1, dn = final.expected_first_tradable_session, final.maturity_session
            assert final.status == COMPLETE
            assert final.first_tradable_price == float(stock_by_date[d1]["open"])
            assert final.future_price == float(stock_by_date[dn]["close"])
            assert final.benchmark_first_tradable_price == float(spy_by_date[d1]["open"])
            assert final.benchmark_future_price == float(spy_by_date[dn]["close"])
            assert final.stock_return_pct == pytest.approx(
                (final.future_price / final.first_tradable_price - 1) * 100,
            )
            assert final.benchmark_return_pct == pytest.approx(
                (final.benchmark_future_price / final.benchmark_first_tradable_price - 1) * 100,
            )
            assert final.active_return_pct == pytest.approx(
                final.stock_return_pct - final.benchmark_return_pct,
            )
            assert final.slot_target_session == TARGET.isoformat()
            assert final.maturity_session < final.slot_target_session
            assert final.observation_cycle_id == registration.observation_cycle_id
            assert final.observation_slot_id == closure.observation_slot_id
            assert "/" not in json.dumps(final.to_dict())
        assert {item.state for item in summary.resolutions} == {FINALIZED_COMPLETE}
    finally:
        store.__exit__(None, None, None)


def _terminal_artifacts(root: Path, *, mode: str):
    target = date(2026, 10, 12)
    decision = "2026-09-25T20:01:00Z"
    expectation = _one("WBD", decision=decision, horizon=5)
    assert expectation.expected_first_tradable_session == "2026-09-28"
    assert expectation.maturity_session == "2026-10-02"
    stock, spy, cycle = root / "stock", root / "spy", root / "cycle"
    manifest = _write_snapshot(stock, ("WBD",), target=target)
    if mode != "present":
        raw_path = stock / "raw" / "WBD.json"
        raw_payload = json.loads(raw_path.read_bytes())
        omitted = {
            "missing_d1": {expectation.expected_first_tradable_session},
            "missing_dn": {expectation.maturity_session},
            "missing_both": {
                expectation.expected_first_tradable_session, expectation.maturity_session,
            },
        }[mode]
        raw_payload["values"] = [
            row for row in raw_payload["values"] if row["datetime"] not in omitted
        ]
        raw = canonical_bytes(raw_payload)
        capture = normalize_response("WBD", raw, target_session=target)
        assert capture.classification == "CORPORATE_ACTION_NO_LONGER_TRADING"
        normalized = canonical_bytes(list(capture.bars))
        raw_path.write_bytes(raw)
        (stock / "normalized" / "WBD.json").write_bytes(normalized)
        entry = manifest["tickers"][0]
        entry.update({
            "classification": capture.classification,
            "first_date": capture.bars[0]["date"],
            "last_valid_date": capture.bars[-1]["date"],
            "bar_count": len(capture.bars),
            "content_sha256": hashlib.sha256(normalized).hexdigest(),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "excluded_rows": list(capture.excluded_rows),
        })
        manifest["snapshot_sha256"] = snapshot_hash(manifest)
        (stock / "snapshot_manifest.json").write_bytes(canonical_bytes(manifest))
    _acquire(
        spy, target=target,
        started_at=datetime(2026, 10, 13, 10, tzinfo=timezone.utc),
    )
    create_observation_cycle(
        expectations=(expectation,), observation_target_session=target.isoformat(),
        stock_root=stock, spy_root=spy, output_root=cycle,
    )
    return {
        "expectations": (expectation,), "stock": stock, "spy": spy, "cycle": cycle,
        "target": target,
    }


def _bind_for_target(store, artifacts):
    _append_expectations(store, artifacts["expectations"])
    register_observation_cycle(
        store, cycle_root=artifacts["cycle"], expectations=artifacts["expectations"],
        stock_root=artifacts["stock"], spy_root=artifacts["spy"],
    )
    year, week, _ = artifacts["target"].isocalendar()
    slot = weekly_observation_slot(year, week)
    close_observation_slot(
        store, iso_year=year, iso_week=week,
        closed_at_utc=slot.finality_eligible_at_utc,
    )
    registration = load_registered_cycles(store)[0]
    return CycleSourceLocator(
        registration.observation_cycle_id, artifacts["cycle"],
        artifacts["stock"], artifacts["spy"],
    )


def test_terminal_stock_with_d1_and_dn_present_is_complete(tmp_path):
    artifacts = _terminal_artifacts(tmp_path / "artifacts", mode="present")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        locator = _bind_for_target(store, artifacts)
        summary = finalize_outcomes(store, cycle_sources=(locator,))
        final = load_outcome_finals(store, artifacts["expectations"])[0]
    assert summary.completed == 1
    assert final.status == COMPLETE
    assert final.stock_classification == "CORPORATE_ACTION_NO_LONGER_TRADING"


@pytest.mark.parametrize(("mode", "flags"), [
    ("missing_d1", (TERMINAL_STOCK_D1_UNAVAILABLE,)),
    ("missing_dn", (TERMINAL_STOCK_DN_UNAVAILABLE,)),
    ("missing_both", (
        TERMINAL_STOCK_D1_UNAVAILABLE, TERMINAL_STOCK_DN_UNAVAILABLE,
    )),
])
def test_terminal_missing_required_sessions_is_positive_evidence_dqf(
    tmp_path, mode, flags,
):
    artifacts = _terminal_artifacts(tmp_path / mode, mode=mode)
    with ValidationStore(tmp_path / f"{mode}.sqlite3") as store:
        locator = _bind_for_target(store, artifacts)
        summary = finalize_outcomes(store, cycle_sources=(locator,))
        final = load_outcome_finals(store, artifacts["expectations"])[0]
    assert summary.data_quality_failure == 1
    assert summary.resolutions[0].state == FINALIZED_DATA_QUALITY_FAILURE
    assert final.status == DATA_QUALITY_FAILURE
    assert final.quality_flags == tuple(sorted(flags))
    assert all(final.to_dict()[field] is None for field in (
        "first_tradable_price", "future_price", "benchmark_first_tradable_price",
        "benchmark_future_price", "stock_return_pct", "benchmark_return_pct",
        "active_return_pct",
    ))
    assert final.observation_cycle_id and final.stock_snapshot_sha256 \
        and final.spy_snapshot_sha256


@pytest.mark.parametrize("missing", ["d1", "dn"])
def test_active_complete_missing_required_session_is_contract_error(
    tmp_path, complete_artifacts, missing,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, registration, _locator = _bind(store, complete_artifacts)
        expectation = complete_artifacts["expectations"][0]
        assignment = load_expectation_assignments(
            store, complete_artifacts["expectations"],
        )[0]
        stock = load_stock_observation_snapshot(
            complete_artifacts["stock"], expected_tickers=("AAPL",),
            expected_observation_target_session=TARGET.isoformat(),
        )
        series = stock.series("AAPL")
        omitted = expectation.expected_first_tradable_session if missing == "d1" \
            else expectation.maturity_session
        altered = replace(series, bars=tuple(
            row for row in series.bars if row["date"] != omitted
        ))
        stock = replace(stock, tickers=(altered,))
        spy = load_spy_benchmark_snapshot(
            complete_artifacts["spy"], trinity_target_session=TARGET,
        )
        with pytest.raises(OutcomeFinalizationError, match="ACTIVE_COMPLETE"):
            _build_final(expectation, assignment, registration, stock, spy)
        assert store.records(OUTCOME_FINAL) == []


@pytest.mark.parametrize("missing", ["d1", "dn"])
def test_verified_spy_missing_required_session_is_contract_error(
    tmp_path, complete_artifacts, missing,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, registration, _locator = _bind(store, complete_artifacts)
        expectation = complete_artifacts["expectations"][0]
        assignment = load_expectation_assignments(
            store, complete_artifacts["expectations"],
        )[0]
        stock = load_stock_observation_snapshot(
            complete_artifacts["stock"], expected_tickers=("AAPL",),
            expected_observation_target_session=TARGET.isoformat(),
        )
        spy = load_spy_benchmark_snapshot(
            complete_artifacts["spy"], trinity_target_session=TARGET,
        )
        omitted = expectation.expected_first_tradable_session if missing == "d1" \
            else expectation.maturity_session
        spy = replace(spy, bars=tuple(row for row in spy.bars if row["date"] != omitted))
        with pytest.raises(OutcomeFinalizationError, match="SPY source"):
            _build_final(expectation, assignment, registration, stock, spy)


def test_existing_final_is_terminal_and_idempotent_without_source(
    tmp_path, complete_artifacts,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, _registration, locator = _bind(store, complete_artifacts)
        first = finalize_outcomes(store, cycle_sources=(locator,))
        before = store.record_items(OUTCOME_FINAL)
        second = finalize_outcomes(store)
        after = store.record_items(OUTCOME_FINAL)
    assert first.new_final_records == 3
    assert second.already_final == 3 and second.new_final_records == 0
    assert {item.state for item in second.resolutions} == {ALREADY_FINAL}
    assert before == after


def test_batch_final_append_is_atomic(tmp_path, complete_artifacts, monkeypatch):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, _registration, locator = _bind(store, complete_artifacts)
        original = store.append
        count = 0

        def fail_second_final(kind, key, payload):
            nonlocal count
            if kind == OUTCOME_FINAL:
                count += 1
                if count == 2:
                    raise RuntimeError("injected final append failure")
            return original(kind, key, payload)

        monkeypatch.setattr(store, "append", fail_second_final)
        with pytest.raises(RuntimeError, match="injected"):
            finalize_outcomes(store, cycle_sources=(locator,))
        assert store.records(OUTCOME_FINAL) == []


def test_divergent_existing_final_conflicts(tmp_path, complete_artifacts):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, _registration, locator = _bind(store, complete_artifacts)
        finalize_outcomes(store, cycle_sources=(locator,))
        final = load_outcome_finals(store, complete_artifacts["expectations"])[0]
        payload = final.to_dict()
        payload["active_return_pct"] += 1
        with pytest.raises(ValidationStorageConflict):
            store.append(OUTCOME_FINAL, final.key, payload)


def test_outcome_final_cannot_be_pending_or_arithmetically_inconsistent(
    tmp_path, complete_artifacts,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, _registration, locator = _bind(store, complete_artifacts)
        finalize_outcomes(store, cycle_sources=(locator,))
        final = load_outcome_finals(store, complete_artifacts["expectations"])[0]
    payload = final.to_dict()
    payload["status"] = "PENDING"
    with pytest.raises(OutcomeFinalizationError, match="terminal"):
        OutcomeFinal.from_dict(payload)
    payload = final.to_dict()
    payload["active_return_pct"] += 0.01
    with pytest.raises(OutcomeFinalizationError, match="arithmetic"):
        OutcomeFinal.from_dict(payload)


@pytest.mark.parametrize(("kind", "field", "value", "message"), [
    ("expectation", "selection_data_quality_flags", ["TAMPER"], "assignment expectation"),
    ("assignment", "expectation_payload_sha256", "0" * 64, "payload binding"),
    ("closure", "authoritative_cycle_id", "0" * 64, "slot closure|stored slot closure"),
    ("registration", "observation_cycle_sha256", "0" * 64, "cycle lineage"),
])
def test_persisted_authoritative_chain_tampering_fails_closed(
    tmp_path, complete_artifacts, kind, field, value, message,
):
    with ValidationStore(tmp_path / f"{kind}.sqlite3") as store:
        _closure, _registration, locator = _bind(store, complete_artifacts)
        kinds = {
            "expectation": OUTCOME_EXPECTATION,
            "assignment": "EXPECTATION_CYCLE_ASSIGNMENT",
            "closure": OBSERVATION_SLOT_CLOSURE,
            "registration": OBSERVATION_CYCLE_REGISTRATION,
        }
        record_kind = kinds[kind]
        key, payload = store.record_items(record_kind)[0]
        payload[field] = value
        raw = canonical_json(payload)
        store.connection.execute(
            "UPDATE validation_record SET payload_json=?, payload_sha256=? "
            "WHERE record_kind=? AND record_key=?",
            (raw, hashlib.sha256(raw).hexdigest(), record_kind, key),
        )
        with pytest.raises((OutcomeFinalizationError, ValidationContractError), match=message):
            finalize_outcomes(store, cycle_sources=(locator,))
        assert store.records(OUTCOME_FINAL) == []


@pytest.mark.parametrize("source", ["cycle", "stock", "spy"])
def test_source_identity_or_integrity_tampering_fails_closed(
    tmp_path, complete_artifacts, source,
):
    copied = tmp_path / source
    shutil.copytree(complete_artifacts[source], copied)
    if source == "cycle":
        (copied / "READY").write_bytes(b"{}")
    elif source == "stock":
        (copied / "raw" / "AAPL.json").write_bytes(b"{}")
    else:
        (copied / "raw" / "SPY.json").write_bytes(b"{}")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, registration, _locator = _bind(store, complete_artifacts)
        locator = CycleSourceLocator(
            registration.observation_cycle_id,
            copied if source == "cycle" else complete_artifacts["cycle"],
            copied if source == "stock" else complete_artifacts["stock"],
            copied if source == "spy" else complete_artifacts["spy"],
        )
        with pytest.raises(OutcomeFinalizationError, match="source verification"):
            finalize_outcomes(store, cycle_sources=(locator,))
        assert store.records(OUTCOME_FINAL) == []


def test_retired_final_is_ignored_and_current_malformed_final_rejected(
    tmp_path, complete_artifacts,
):
    with ValidationStore(tmp_path / "source.sqlite3") as store:
        _closure, _registration, locator = _bind(store, complete_artifacts)
        finalize_outcomes(store, cycle_sources=(locator,))
        final = load_outcome_finals(store, complete_artifacts["expectations"])[0]
    retired = final.to_dict()
    retired["protocol_hash"] = OLD_HASH
    with ValidationStore(tmp_path / "retired.sqlite3") as store:
        store.append(OUTCOME_FINAL, f"{OLD_HASH}:retired", retired)
        assert load_outcome_finals(store, complete_artifacts["expectations"]) == ()
    malformed = final.to_dict()
    malformed["status"] = "PENDING"
    with ValidationStore(tmp_path / "malformed.sqlite3") as store:
        store.append(OUTCOME_FINAL, final.key, malformed)
        with pytest.raises(OutcomeFinalizationError, match="stored current-protocol"):
            load_outcome_finals(store, complete_artifacts["expectations"])


def test_source_paths_are_runtime_only_and_no_external_dependencies(
    tmp_path, complete_artifacts,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _closure, _registration, locator = _bind(store, complete_artifacts)
        finalize_outcomes(store, cycle_sources=(locator,))
        serialized = json.dumps(store.records(OUTCOME_FINAL), sort_keys=True)
    assert str(complete_artifacts["root"]) not in serialized
    for field in ("cycle_root", "stock_root", "spy_root"):
        assert field not in serialized
    source = inspect.getsource(finalization_module)
    assert "verify_canonical_universe" not in source
    assert "usa_issuer_registry" not in source
    assert "trading_cost" not in source
    assert "scorecard" not in source
    original = socket.socket
    socket.socket = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("network forbidden")
    )
    try:
        CycleSourceLocator("a" * 64, "cycle", "stock", "spy")
    finally:
        socket.socket = original


def test_protocol_identity_and_finalization_hash_are_frozen():
    assert PROTOCOL_ID == "TRINITY_LIVE_VALIDATION_V1"
    assert PROTOCOL_EPOCH == 1
    assert PROTOCOL_HASH == "0a2e4add27ba15507235f04d4a307c21a70c93920f5a7c02cd9e97803a469035"
