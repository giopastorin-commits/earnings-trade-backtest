from __future__ import annotations

from dataclasses import replace
import inspect
import json
import socket

import pytest

import trinity.validation.outcome_expectations as expectation_module
from trinity.validation.contracts import (
    CollectedRun, SMOKE_ONLY, SelectionObservation, ValidationContractError,
)
from trinity.validation.market_calendar import CALENDAR_VERSION, schedule_evidence_sha256
from trinity.validation.outcome_expectations import (
    OUTCOME_EXPECTATION,
    OutcomeExpectation,
    OutcomeExpectationError,
    append_outcome_expectations,
    build_outcome_expectations,
    outcome_identity,
)
from trinity.validation.protocol_v1 import HORIZONS, PROTOCOL_HASH
from trinity.validation.storage import (
    ValidationStorageConflict,
    ValidationStorageIntegrityError,
    ValidationStore,
)


SHA = "a" * 64
MANIFEST_SHA = "b" * 64
GIT_SHA = "c" * 40
DECISION = "2026-10-09T20:01:00Z"


def _selection(stage: str = "SCANNER", *, ticker: str = "AAPL") -> SelectionObservation:
    if stage == "SCANNER":
        luna = (True, "COMPLETED", "WATCH")
        sol = (False, "NOT_EVALUATED", "NOT_EVALUATED")
    elif stage == "LUNA":
        luna = (True, "COMPLETED", "ESCALATE")
        sol = (True, "REJECTED", "WATCH")
    elif stage == "SOL":
        luna = (True, "COMPLETED", "ESCALATE")
        sol = (True, "PROMOTED", "INVESTIGATE")
    else:
        raise AssertionError(stage)
    return SelectionObservation(
        run_id="RUN_A", intended_session="2026-10-09",
        run_timestamp_utc=DECISION, ticker=ticker,
        universe_version="usa_issuer_registry_v1.fixture", universe_sha256=SHA,
        scanner_selected=True, scanner_state="READY_TECHNICALLY",
        scanner_reason="ready", luna_evaluated=luna[0], luna_status=luna[1],
        luna_decision=luna[2], luna_model="luna", luna_prompt_version="v1",
        luna_input_sha256=SHA, luna_output_sha256=SHA, luna_cost_usd="0.01",
        sol_evaluated=sol[0], sol_status=sol[1], sol_decision=sol[2],
        sol_model="sol" if sol[0] else None, sol_prompt_version="v1" if sol[0] else None,
        sol_input_sha256=SHA if sol[0] else None,
        sol_output_sha256=SHA if sol[0] else None, sol_cost_usd="0.02" if sol[0] else "0",
        analytical_commit_sha=GIT_SHA, snapshot_id="TWELVEDATA_DECISION_RUN_A",
        snapshot_sha256=SHA, data_quality_flags=("SOURCE_FLAG",), eligibility=SMOKE_ONLY,
    )


def _collected(*selections: SelectionObservation) -> CollectedRun:
    values = selections or (_selection(),)
    return CollectedRun(
        run_id="RUN_A", source_root="C:/immutable/run-a",
        source_manifest_sha256=MANIFEST_SHA,
        decision_target_session="2026-10-09", run_timestamp_utc=DECISION,
        analytical_commit_sha=GIT_SHA, snapshot_id="TWELVEDATA_DECISION_RUN_A",
        snapshot_sha256=SHA, universe_version="usa_issuer_registry_v1.fixture",
        universe_sha256=SHA, selections=tuple(values),
        setups=(), executions=(), total_api_cost_usd="0.03", eligibility=SMOKE_ONLY,
    )


@pytest.mark.parametrize(("stage", "count", "cohorts"), [
    ("SCANNER", 3, {"SCANNER"}),
    ("LUNA", 6, {"SCANNER", "LUNA"}),
    ("SOL", 9, {"SCANNER", "LUNA", "SOL"}),
])
def test_frozen_stage_membership_builds_expected_cohorts(stage, count, cohorts):
    expectations = build_outcome_expectations(_collected(_selection(stage)))
    assert len(expectations) == count
    assert {item.cohort for item in expectations} == cohorts
    assert {item.horizon_sessions for item in expectations} == set(HORIZONS) == {5, 20, 60}


@pytest.mark.parametrize(("horizon", "d1", "dn"), [
    (5, "2026-10-12", "2026-10-16"),
    (20, "2026-10-12", "2026-11-06"),
    (60, "2026-10-12", "2027-01-06"),
])
def test_d1_and_inclusive_dn_are_frozen_for_each_official_horizon(horizon, d1, dn):
    item = next(
        row for row in build_outcome_expectations(_collected())
        if row.horizon_sessions == horizon
    )
    assert item.expected_first_tradable_session == d1
    assert item.maturity_session == dn
    assert len(item.schedule_evidence["sessions"]) == horizon


def test_d10_is_rejected_and_never_generated():
    with pytest.raises(OutcomeExpectationError, match="identity"):
        outcome_identity("RUN_A", "AAPL", "SCANNER", 10)
    assert 10 not in {item.horizon_sessions for item in build_outcome_expectations(_collected())}


def test_identity_and_payload_are_deterministic_and_contain_no_creation_time():
    first = build_outcome_expectations(_collected())
    second = build_outcome_expectations(_collected())
    assert [item.key for item in first] == [item.key for item in second]
    assert [item.to_dict() for item in first] == [item.to_dict() for item in second]
    assert first[0].key == f"{PROTOCOL_HASH}:RUN_A:AAPL:SCANNER:5"
    assert "expectation_created_at" not in first[0].to_dict()


def test_schedule_evidence_sha_count_and_endpoints_are_verified():
    item = build_outcome_expectations(_collected())[0]
    evidence = item.schedule_evidence
    assert item.schedule_sha256 == evidence["schedule_sha256"]
    assert item.schedule_sha256 == schedule_evidence_sha256(evidence)
    assert evidence["sessions"][0]["session"] == item.expected_first_tradable_session
    assert evidence["sessions"][-1]["session"] == item.maturity_session


@pytest.mark.parametrize(("field", "value", "message"), [
    ("protocol_hash", "d" * 64, "protocol identity"),
    ("horizon_sessions", 10, "identity"),
    ("cohort", "OTHER", "identity"),
    ("decision_snapshot_sha256", "bad", "decision_snapshot_sha256"),
])
def test_deserialization_rejects_identity_and_lineage_tampering(field, value, message):
    payload = build_outcome_expectations(_collected())[0].to_dict()
    payload[field] = value
    with pytest.raises(OutcomeExpectationError, match=message):
        OutcomeExpectation.from_dict(payload)


def test_deserialization_rejects_ambiguous_types_and_extra_schedule_fields():
    payload = build_outcome_expectations(_collected())[0].to_dict()
    wrong_bool = dict(payload)
    wrong_bool["scanner_selected"] = 1
    with pytest.raises(OutcomeExpectationError):
        OutcomeExpectation.from_dict(wrong_bool)

    wrong_flags = dict(payload)
    wrong_flags["selection_data_quality_flags"] = "FLAG"
    with pytest.raises(OutcomeExpectationError):
        OutcomeExpectation.from_dict(wrong_flags)

    extra_schedule = build_outcome_expectations(_collected())[0].to_dict()
    evidence = extra_schedule["schedule_evidence"]
    evidence["unexpected"] = "field"
    evidence["schedule_sha256"] = schedule_evidence_sha256(evidence)
    extra_schedule["schedule_sha256"] = evidence["schedule_sha256"]
    with pytest.raises(OutcomeExpectationError, match="shape"):
        OutcomeExpectation.from_dict(extra_schedule)


def test_tampered_schedule_sha_is_rejected():
    payload = build_outcome_expectations(_collected())[0].to_dict()
    payload["schedule_evidence"]["sessions"][0]["open_utc"] = "2026-10-12T14:00:00Z"
    with pytest.raises(OutcomeExpectationError, match="SHA-256"):
        OutcomeExpectation.from_dict(payload)


@pytest.mark.parametrize(("field", "value"), [
    ("expected_first_tradable_session", "2026-10-13"),
    ("maturity_session", "2026-10-19"),
])
def test_tampered_d1_or_dn_is_rejected(field, value):
    payload = build_outcome_expectations(_collected())[0].to_dict()
    payload[field] = value
    with pytest.raises(OutcomeExpectationError, match="D1 or DN"):
        OutcomeExpectation.from_dict(payload)


def test_historical_calendar_version_is_provenance_not_runtime_equality():
    payload = build_outcome_expectations(_collected())[0].to_dict()
    evidence = payload["schedule_evidence"]
    evidence["calendar_version"] = "historical-4.12.0"
    evidence["schedule_sha256"] = schedule_evidence_sha256(evidence)
    payload["calendar_version"] = "historical-4.12.0"
    payload["schedule_sha256"] = evidence["schedule_sha256"]
    restored = OutcomeExpectation.from_dict(payload)
    assert restored.calendar_version == "historical-4.12.0"


def test_current_calendar_version_recomputes_and_rejects_inconsistent_schedule():
    payload = build_outcome_expectations(_collected())[0].to_dict()
    evidence = payload["schedule_evidence"]
    assert evidence["calendar_version"] == CALENDAR_VERSION
    evidence["sessions"][0]["open_utc"] = "2026-10-12T14:00:00.000000Z"
    evidence["schedule_sha256"] = schedule_evidence_sha256(evidence)
    payload["schedule_sha256"] = evidence["schedule_sha256"]
    with pytest.raises(OutcomeExpectationError, match="runtime calendar"):
        OutcomeExpectation.from_dict(payload)


def test_luna_cohort_lineage_is_fail_closed_on_deserialization():
    item = next(row for row in build_outcome_expectations(
        _collected(_selection("LUNA"))) if row.cohort == "LUNA"
    )
    payload = item.to_dict()
    payload["luna_decision"] = "WATCH"
    with pytest.raises(OutcomeExpectationError, match="lineage"):
        OutcomeExpectation.from_dict(payload)


def test_invalid_sol_promoted_without_investigate_is_rejected():
    selection = replace(_selection("SOL"), sol_decision="WATCH")
    with pytest.raises(OutcomeExpectationError, match="Sol routing"):
        build_outcome_expectations(_collected(selection))


def test_sol_without_luna_escalate_is_rejected():
    selection = replace(
        _selection("SOL"), luna_decision="WATCH", luna_status="COMPLETED",
    )
    with pytest.raises(OutcomeExpectationError, match="Sol routing"):
        build_outcome_expectations(_collected(selection))


def test_full_decision_run_and_stage_provenance_is_preserved():
    selection = _selection("SOL")
    item = next(row for row in build_outcome_expectations(
        _collected(selection)) if row.cohort == "SOL" and row.horizon_sessions == 20
    )
    assert item.source_run_manifest_sha256 == MANIFEST_SHA
    assert (item.decision_snapshot_id, item.decision_snapshot_sha256) == (
        selection.snapshot_id, selection.snapshot_sha256,
    )
    assert (item.universe_version, item.universe_sha256) == (
        selection.universe_version, selection.universe_sha256,
    )
    assert item.analytical_commit_sha == GIT_SHA
    assert item.selection_data_quality_flags == ("SOURCE_FLAG",)
    assert (item.luna_status, item.luna_decision) == ("COMPLETED", "ESCALATE")
    assert (item.sol_status, item.sol_decision) == ("PROMOTED", "INVESTIGATE")


def test_collected_run_consistency_and_duplicate_ticker_are_rejected():
    selection = _selection()
    with pytest.raises(OutcomeExpectationError, match="duplicate selection"):
        build_outcome_expectations(_collected(selection, selection))
    with pytest.raises(OutcomeExpectationError, match="identity mismatch"):
        build_outcome_expectations(replace(_collected(selection), run_id="OTHER"))
    with pytest.raises(ValidationContractError, match="source_manifest_sha256"):
        replace(_collected(selection), source_manifest_sha256="bad")


def test_append_is_idempotent_and_divergent_same_identity_conflicts(tmp_path):
    database = tmp_path / "validation.sqlite3"
    collected = _collected()
    with ValidationStore(database) as store:
        assert append_outcome_expectations(store, collected) == 3
        assert append_outcome_expectations(store, collected) == 0
        assert len(store.record_items(OUTCOME_EXPECTATION)) == 3
        divergent = replace(
            collected,
            selections=(replace(collected.selections[0], data_quality_flags=("CHANGED",)),),
        )
        with pytest.raises(ValidationStorageConflict, match="divergent"):
            append_outcome_expectations(store, divergent)


def test_storage_digest_is_verified_before_records_are_returned(tmp_path):
    with ValidationStore(tmp_path / "validation.sqlite3") as store:
        append_outcome_expectations(store, _collected())
        store.connection.execute(
            "UPDATE validation_record SET payload_json=? WHERE record_kind=?",
            (b'{"tampered":true}', OUTCOME_EXPECTATION),
        )
        with pytest.raises(ValidationStorageIntegrityError, match="hash mismatch"):
            store.records(OUTCOME_EXPECTATION)
        with pytest.raises(ValidationStorageIntegrityError, match="hash mismatch"):
            store.record_items(OUTCOME_EXPECTATION)


def test_expectation_build_has_no_network_or_future_snapshot_dependency(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted")
    ))
    assert len(build_outcome_expectations(_collected())) == 3
    source = inspect.getsource(expectation_module)
    assert "spy_benchmark" not in source
    assert "stock_observation" not in source
    assert "load_registry" not in source
    assert "OUTCOME_FINAL" not in source
    assert "DATA_QUALITY_FAILURE" not in source
