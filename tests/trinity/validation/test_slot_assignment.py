from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import inspect
import socket

import pytest

import trinity.validation.slot_assignment as assignment_module
from tests.trinity.validation.test_observation_cycle import _one
from tests.trinity.validation.test_spy_benchmark import _acquire
from tests.trinity.validation.test_stock_observation import _write_snapshot
from trinity.validation.cycle_registry import register_observation_cycle
from trinity.validation.observation_cycle import create_observation_cycle
from trinity.validation.observation_slots import weekly_observation_slot
from trinity.validation.outcome_expectations import OUTCOME_EXPECTATION
from trinity.validation.protocol_v1 import (
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
    canonical_json,
)
from trinity.validation.slot_assignment import (
    AMBIGUOUS_OPERATIONAL_ERROR,
    ASSIGNED_EXACT_CYCLE,
    EMPTY_NO_EXPECTATIONS,
    EXPECTATION_CYCLE_ASSIGNMENT,
    OBSERVATION_SLOT_CLOSURE,
    OPERATIONAL_OBSERVATION_MISSING,
    ExpectationCycleAssignment,
    ObservationSlotClosure,
    SlotAssignmentError,
    close_observation_slot,
    load_current_expectations,
    load_expectation_assignments,
    load_slot_closures,
)
from trinity.validation.storage import ValidationStore


OLD_HASH = "38980fc7bdc0ab8912f23cbc8ce67325f0d0ffe896a8d29838fe81ed1d094a1a"
TARGET = date(2026, 10, 12)
ISO_YEAR, ISO_WEEK, _ = TARGET.isocalendar()
CLOSED_AT = "2026-10-12T20:20:00Z"


def _expectation(ticker: str):
    return _one(ticker, decision="2026-07-01T20:01:00Z", horizon=5)


EXPECTATIONS = tuple(_expectation(ticker) for ticker in ("AAPL", "HOOD", "VEEV"))
EXTRA = _expectation("MSFT")


def _append_expectations(store: ValidationStore, expectations=EXPECTATIONS) -> None:
    for item in expectations:
        assert store.append(OUTCOME_EXPECTATION, item.key, item.to_dict())


@pytest.fixture(scope="module")
def cycle_artifacts(tmp_path_factory):
    root = tmp_path_factory.mktemp("slot-assignment-artifacts")
    spy = root / "spy"
    _acquire(spy, target=TARGET, started_at=datetime(2026, 10, 13, 10, tzinfo=timezone.utc))

    def build(name, expectations, *, minute=20):
        tickers = tuple(sorted({item.ticker for item in expectations}))
        stock = root / f"stock-{name}"
        started = datetime(2026, 10, 12, 20, minute, tzinfo=timezone.utc)
        _write_snapshot(
            stock, tickers, target=TARGET, started=started,
            completed=started + timedelta(minutes=10),
        )
        cycle = root / f"cycle-{name}"
        create_observation_cycle(
            expectations=expectations, observation_target_session=TARGET.isoformat(),
            stock_root=stock, spy_root=spy, output_root=cycle,
        )
        return {"cycle": cycle, "stock": stock, "expectations": expectations}

    return {
        "spy": spy,
        "exact": build("exact", EXPECTATIONS),
        "exact_two": build("exact-two", EXPECTATIONS, minute=21),
        "partial": build("partial", EXPECTATIONS[:2], minute=22),
        "superset": build("superset", EXPECTATIONS + (EXTRA,), minute=23),
    }


def _register(store: ValidationStore, cycle_artifacts, name: str) -> None:
    item = cycle_artifacts[name]
    assert register_observation_cycle(
        store, cycle_root=item["cycle"], expectations=item["expectations"],
        stock_root=item["stock"], spy_root=cycle_artifacts["spy"],
    )


def _rewrite(store: ValidationStore, kind: str, key: str, payload: dict) -> None:
    raw = canonical_json(payload)
    store.connection.execute(
        "UPDATE validation_record SET payload_json=?, payload_sha256=? "
        "WHERE record_kind=? AND record_key=?",
        (raw, hashlib.sha256(raw).hexdigest(), kind, key),
    )


def test_closure_before_finality_is_rejected(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        with pytest.raises(SlotAssignmentError, match="before finality"):
            close_observation_slot(
                store, iso_year=ISO_YEAR, iso_week=ISO_WEEK,
                closed_at_utc="2026-10-12T20:19:59Z",
            )
        assert store.records(OBSERVATION_SLOT_CLOSURE) == []


def test_closure_timestamp_must_be_timezone_aware(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        with pytest.raises(SlotAssignmentError, match="input is invalid"):
            close_observation_slot(
                store, iso_year=ISO_YEAR, iso_week=ISO_WEEK,
                closed_at_utc="2026-10-12T20:20:00",
            )


def test_empty_slot_is_persisted_as_processed_evidence(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        closure = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        assert closure.closure_state == EMPTY_NO_EXPECTATIONS
        assert closure.expected_expectation_keys == ()
        assert closure.assignment_count == 0
        assert len(load_slot_closures(store)) == 1


def test_exact_expected_set_derivation_and_assignment(tmp_path, cycle_artifacts):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        _register(store, cycle_artifacts, "exact")
        closure = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        assignments = load_expectation_assignments(store, EXPECTATIONS)
    assert closure.closure_state == ASSIGNED_EXACT_CYCLE
    assert closure.expected_expectation_keys == tuple(sorted(item.key for item in EXPECTATIONS))
    assert closure.expected_tickers == ("AAPL", "HOOD", "VEEV")
    assert closure.assignment_count == len(EXPECTATIONS) == len(assignments)
    assert {item.observation_cycle_id for item in assignments} == {
        closure.authoritative_cycle_id,
    }
    assert all(item.expectation_payload_sha256 == hashlib.sha256(
        canonical_json(next(row.to_dict() for row in EXPECTATIONS if row.key == item.key))
    ).hexdigest() for item in assignments)


@pytest.mark.parametrize("candidate", ["partial", "superset"])
def test_partial_or_superset_cycle_is_not_authoritative(
    tmp_path, cycle_artifacts, candidate,
):
    with ValidationStore(tmp_path / f"{candidate}.sqlite3") as store:
        _append_expectations(store)
        _register(store, cycle_artifacts, candidate)
        closure = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        assignments = load_expectation_assignments(store, EXPECTATIONS)
    assert closure.closure_state == OPERATIONAL_OBSERVATION_MISSING
    assert closure.exact_candidate_cycle_ids == ()
    assert len(closure.nonexact_candidate_cycle_ids) == 1
    assert assignments == ()


def test_no_cycle_is_missing_and_all_expectations_flow_to_next_slot(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        first = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        next_slot = weekly_observation_slot(2026, 43)
        second = close_observation_slot(
            store, iso_year=2026, iso_week=43,
            closed_at_utc=next_slot.finality_eligible_at_utc,
        )
    assert first.closure_state == OPERATIONAL_OBSERVATION_MISSING
    assert first.assignment_count == 0
    assert second.expected_expectation_keys == first.expected_expectation_keys
    assert second.closure_state == OPERATIONAL_OBSERVATION_MISSING


def test_two_exact_cycles_are_ambiguous_without_winner_and_flow_forward(
    tmp_path, cycle_artifacts,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        _register(store, cycle_artifacts, "exact")
        _register(store, cycle_artifacts, "exact_two")
        first = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        assert load_expectation_assignments(store, EXPECTATIONS) == ()
        next_slot = weekly_observation_slot(2026, 43)
        second = close_observation_slot(
            store, iso_year=2026, iso_week=43,
            closed_at_utc=next_slot.finality_eligible_at_utc,
        )
    assert first.closure_state == AMBIGUOUS_OPERATIONAL_ERROR
    assert len(first.exact_candidate_cycle_ids) == 2
    assert first.authoritative_cycle_id is None
    assert second.expected_expectation_keys == first.expected_expectation_keys


def test_assigned_expectations_are_excluded_from_next_slot(tmp_path, cycle_artifacts):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        _register(store, cycle_artifacts, "exact")
        close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        next_slot = weekly_observation_slot(2026, 43)
        following = close_observation_slot(
            store, iso_year=2026, iso_week=43,
            closed_at_utc=next_slot.finality_eligible_at_utc,
        )
    assert following.closure_state == EMPTY_NO_EXPECTATIONS
    assert following.expected_expectation_keys == ()


def test_late_cycle_cannot_rewrite_missing_or_assign_retroactively(
    tmp_path, cycle_artifacts,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        original = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        _register(store, cycle_artifacts, "exact")
        repeated = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK,
            closed_at_utc="2026-10-13T00:00:00Z",
        )
        assignments = load_expectation_assignments(store, EXPECTATIONS)
        next_slot = weekly_observation_slot(2026, 43)
        following = close_observation_slot(
            store, iso_year=2026, iso_week=43,
            closed_at_utc=next_slot.finality_eligible_at_utc,
        )
    assert original == repeated
    assert repeated.closure_state == OPERATIONAL_OBSERVATION_MISSING
    assert assignments == ()
    assert following.expected_expectation_keys == original.expected_expectation_keys


def test_existing_closure_is_idempotent(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        first = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        second = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK,
            closed_at_utc="2026-10-20T00:00:00Z",
        )
    assert first == second


def test_assignment_append_is_atomic_on_mid_batch_failure(
    tmp_path, cycle_artifacts, monkeypatch,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        _register(store, cycle_artifacts, "exact")
        original = store.append
        seen = 0

        def failing_append(kind, key, payload):
            nonlocal seen
            if kind == EXPECTATION_CYCLE_ASSIGNMENT:
                seen += 1
                if seen == 2:
                    raise RuntimeError("injected failure")
            return original(kind, key, payload)

        monkeypatch.setattr(store, "append", failing_append)
        with pytest.raises(RuntimeError, match="injected"):
            close_observation_slot(
                store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
            )
        assert store.records(OBSERVATION_SLOT_CLOSURE) == []
        assert store.records(EXPECTATION_CYCLE_ASSIGNMENT) == []


@pytest.mark.parametrize("field", [
    "observation_slot_id", "slot_target_session", "finality_eligible_at_utc",
])
def test_slot_identity_target_and_finality_tampering_are_rejected(tmp_path, field):
    with ValidationStore(tmp_path / f"{field}.sqlite3") as store:
        closure = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        payload = closure.to_dict()
        payload[field] = "0" * 64 if field == "observation_slot_id" else (
            "2026-10-13" if field == "slot_target_session" else "2026-10-12T20:21:00Z"
        )
        _rewrite(store, OBSERVATION_SLOT_CLOSURE, closure.key, payload)
        with pytest.raises(SlotAssignmentError, match="stored slot closure"):
            load_slot_closures(store)


def test_closure_record_key_mismatch_is_rejected(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        closure = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        store.connection.execute(
            "UPDATE validation_record SET record_key=? WHERE record_kind=?",
            ("0" * 64, OBSERVATION_SLOT_CLOSURE),
        )
        with pytest.raises(SlotAssignmentError, match="record key"):
            load_slot_closures(store)


def test_assignment_record_key_and_expectation_payload_tampering_rejected(
    tmp_path, cycle_artifacts,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        _register(store, cycle_artifacts, "exact")
        close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        assignment = load_expectation_assignments(store, EXPECTATIONS)[0]
        payload = assignment.to_dict()
        payload["expectation_payload_sha256"] = "0" * 64
        _rewrite(store, EXPECTATION_CYCLE_ASSIGNMENT, assignment.key, payload)
        with pytest.raises(SlotAssignmentError, match="payload binding"):
            load_expectation_assignments(store, EXPECTATIONS)

        original = assignment.to_dict()
        _rewrite(store, EXPECTATION_CYCLE_ASSIGNMENT, assignment.key, original)
        store.connection.execute(
            "UPDATE validation_record SET record_key=? WHERE record_kind=? AND record_key=?",
            ("wrong-key", EXPECTATION_CYCLE_ASSIGNMENT, assignment.key),
        )
        with pytest.raises(SlotAssignmentError, match="record key"):
            load_expectation_assignments(store, EXPECTATIONS)


@pytest.mark.parametrize("state, mutate", [
    (EMPTY_NO_EXPECTATIONS, {"assignment_count": 1}),
    (ASSIGNED_EXACT_CYCLE, {"authoritative_cycle_id": None}),
    (OPERATIONAL_OBSERVATION_MISSING, {"assignment_count": 1}),
    (AMBIGUOUS_OPERATIONAL_ERROR, {"exact_candidate_cycle_ids": []}),
])
def test_state_invariant_tampering_is_rejected(tmp_path, cycle_artifacts, state, mutate):
    with ValidationStore(tmp_path / f"{state}.sqlite3") as store:
        if state != EMPTY_NO_EXPECTATIONS:
            _append_expectations(store)
        if state == ASSIGNED_EXACT_CYCLE:
            _register(store, cycle_artifacts, "exact")
        elif state == AMBIGUOUS_OPERATIONAL_ERROR:
            _register(store, cycle_artifacts, "exact")
            _register(store, cycle_artifacts, "exact_two")
        closure = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        assert closure.closure_state == state
        payload = closure.to_dict()
        payload.update(mutate)
        with pytest.raises(SlotAssignmentError, match="invariants|diagnostics"):
            ObservationSlotClosure.from_dict(payload)


def test_retired_expectation_is_preserved_but_ignored(tmp_path):
    current = EXPECTATIONS[0]
    retired = current.to_dict()
    retired["protocol_hash"] = OLD_HASH
    retired_key = f"{OLD_HASH}:retired"
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        store.append(OUTCOME_EXPECTATION, retired_key, retired)
        store.append(OUTCOME_EXPECTATION, current.key, current.to_dict())
        loaded = load_current_expectations(store)
    assert loaded == (current,)


def test_current_protocol_expectation_wrong_record_key_is_rejected(tmp_path):
    current = EXPECTATIONS[0]
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        store.append(OUTCOME_EXPECTATION, "wrong-key", current.to_dict())
        with pytest.raises(SlotAssignmentError, match="record key"):
            load_current_expectations(store)


def test_assignment_model_is_content_bound_and_slot_bound(tmp_path, cycle_artifacts):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        _register(store, cycle_artifacts, "exact")
        closure = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        assignment = load_expectation_assignments(store, EXPECTATIONS)[0]
    assert assignment.observation_slot_id == closure.observation_slot_id
    assert assignment.expected_expectation_set_sha256 \
        == closure.expected_expectation_set_sha256
    assert assignment.expected_ticker_set_sha256 == closure.expected_ticker_set_sha256
    assert assignment.assignment_policy == "EXACT_SLOT_SET_V1"
    assert ExpectationCycleAssignment.from_dict(assignment.to_dict()) == assignment


def test_terminal_assigned_closure_detects_rehashed_set_binding_tampering(
    tmp_path, cycle_artifacts,
):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        _append_expectations(store)
        _register(store, cycle_artifacts, "exact")
        closure = close_observation_slot(
            store, iso_year=ISO_YEAR, iso_week=ISO_WEEK, closed_at_utc=CLOSED_AT,
        )
        payload = closure.to_dict()
        payload["expected_expectation_set_sha256"] = "0" * 64
        _rewrite(store, OBSERVATION_SLOT_CLOSURE, closure.key, payload)
        with pytest.raises(SlotAssignmentError, match="assignments are inconsistent"):
            close_observation_slot(
                store, iso_year=ISO_YEAR, iso_week=ISO_WEEK,
                closed_at_utc="2026-10-13T00:00:00Z",
            )


def test_no_registry_universe_network_future_prices_or_outcome_state_logic():
    source = inspect.getsource(assignment_module)
    assert "verify_canonical_universe" not in source
    assert "usa_issuer_registry" not in source
    assert "TwelveData" not in source
    assert "requests." not in source
    assert "future_price" not in source
    assert "OUTCOME_FINAL" not in source
    assert "DATA_QUALITY_FAILURE" not in source
    original = socket.socket
    socket.socket = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("network forbidden")
    )
    try:
        weekly_observation_slot(ISO_YEAR, ISO_WEEK)
    finally:
        socket.socket = original


def test_current_protocol_identity_is_unchanged():
    assert PROTOCOL_ID == "TRINITY_LIVE_VALIDATION_V1"
    assert PROTOCOL_EPOCH == 1
    assert PROTOCOL_HASH == "134d9d6a3751c27952a3848baeff4d12642bfcccebada68b6984e95c2c8449a8"
