from __future__ import annotations

from dataclasses import replace
import json
import socket

import pytest

import trinity.validation.cli as validation_cli
from tests.trinity.validation.test_outcome_expectations import (
    DECISION, GIT_SHA, MANIFEST_SHA, SHA, _collected, _selection,
)
from trinity.validation.protocol_v1 import PROTOCOL, PROTOCOL_HASH
from trinity.validation.run_observation import (
    RUN_OBSERVATION,
    RunObservation,
    RunObservationError,
    append_run_observation,
    build_run_observation,
    load_run_observations,
    run_observation_identity,
)
from trinity.validation.storage import (
    ValidationStorageConflict, ValidationStorageIntegrityError, ValidationStore,
)


OLD_PROTOCOL_HASH = "0a2e4add27ba15507235f04d4a307c21a70c93920f5a7c02cd9e97803a469035"


def _empty_collected():
    return replace(_collected(), selections=(), setups=(), executions=(), total_api_cost_usd="0")


def test_normal_run_observation_preserves_run_level_provenance_and_memberships():
    collected = _collected(_selection("SOL"), _selection("LUNA", ticker="MSFT"))
    item = build_run_observation(collected)
    assert item.scanner_tickers == ("AAPL", "MSFT")
    assert item.luna_tickers == ("AAPL", "MSFT")
    assert item.sol_tickers == ("AAPL",)
    assert (item.scanner_count, item.luna_count, item.sol_count) == (2, 2, 1)
    assert item.decision_target_session == "2026-10-09"
    assert item.run_timestamp_utc == DECISION
    assert item.analytical_commit_sha == GIT_SHA
    assert (item.decision_snapshot_id, item.decision_snapshot_sha256) == (
        "TWELVEDATA_DECISION_RUN_A", SHA,
    )
    assert item.source_run_manifest_sha256 == MANIFEST_SHA
    assert (item.universe_version, item.universe_sha256) == (
        "usa_issuer_registry_v1.fixture", SHA,
    )


def test_zero_scanner_run_is_a_valid_immutable_statistical_unit():
    item = build_run_observation(_empty_collected())
    assert item.scanner_tickers == item.luna_tickers == item.sol_tickers == ()
    assert item.scanner_count == item.luna_count == item.sol_count == 0
    assert item.key == f"{PROTOCOL_HASH}:RUN_A"


def test_membership_subset_duplicate_order_and_count_invariants_fail_closed():
    item = build_run_observation(_collected(_selection("SOL")))
    for field, value, message in (
        ("luna_tickers", (), "subset"),
        ("scanner_tickers", ("AAPL", "AAPL"), "ordered and unique"),
        ("scanner_tickers", ("aapl",), "non-canonical"),
        ("scanner_count", 2, "count mismatch"),
    ):
        with pytest.raises(RunObservationError, match=message):
            replace(item, **{field: value})


@pytest.mark.parametrize("field", [
    "run_id", "eligibility", "decision_target_session", "run_timestamp_utc",
    "analytical_commit_sha", "snapshot_id", "snapshot_sha256", "universe_version",
    "universe_sha256",
])
def test_selection_and_run_provenance_mismatch_is_rejected(field):
    collected = _collected(_selection())
    replacement = "OTHER"
    if field.endswith("sha256"):
        replacement = "d" * 64
    elif field == "analytical_commit_sha":
        replacement = "d" * 40
    elif field == "decision_target_session":
        replacement = "2026-10-08"
    elif field == "run_timestamp_utc":
        replacement = "2026-10-09T19:00:00Z"
    elif field == "eligibility":
        replacement = "OFFICIAL_ELIGIBLE"
    changed = replace(collected, **{field: replacement})
    with pytest.raises(RunObservationError, match="provenance mismatch"):
        build_run_observation(changed)


def test_invalid_sol_lineage_is_rejected():
    invalid = replace(_selection("SOL"), luna_decision="WATCH")
    with pytest.raises(RunObservationError, match="Sol membership lineage"):
        build_run_observation(_collected(invalid))


def test_serialized_run_observation_has_no_local_path_and_round_trips():
    item = build_run_observation(_collected())
    payload = item.to_dict()
    assert "source_root" not in payload
    assert "C:/immutable/run-a" not in json.dumps(payload)
    assert RunObservation.from_dict(payload) == item


def test_deserialization_rejects_protocol_drift_retired_hash_and_malformed_fields():
    payload = build_run_observation(_collected()).to_dict()
    payload["protocol_hash"] = OLD_PROTOCOL_HASH
    with pytest.raises(RunObservationError, match="protocol identity"):
        RunObservation.from_dict(payload)
    payload = build_run_observation(_collected()).to_dict()
    payload["run_timestamp_utc"] = "2026-10-09T20:00:00"
    with pytest.raises(RunObservationError, match="timezone-aware"):
        RunObservation.from_dict(payload)


def test_append_is_idempotent_and_divergent_payload_conflicts(tmp_path):
    collected = _collected()
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        assert append_run_observation(store, collected) is True
        assert append_run_observation(store, collected) is False
        changed = replace(collected, total_api_cost_usd="1.00")
        with pytest.raises(ValidationStorageConflict, match="divergent"):
            append_run_observation(store, changed)
        assert load_run_observations(store) == (build_run_observation(collected),)


def test_storage_digest_and_record_key_tampering_are_rejected(tmp_path):
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        append_run_observation(store, _collected())
        store.connection.execute(
            "UPDATE validation_record SET payload_json=? WHERE record_kind=?",
            (b'{"tampered":true}', RUN_OBSERVATION),
        )
        with pytest.raises(ValidationStorageIntegrityError, match="hash mismatch"):
            load_run_observations(store)
    with ValidationStore(tmp_path / "key.sqlite3") as store:
        item = build_run_observation(_collected())
        store.append(RUN_OBSERVATION, "wrong", item.to_dict())
        with pytest.raises(RunObservationError, match="record key mismatch"):
            load_run_observations(store)


def test_cli_collect_persists_run_observation_for_normal_and_zero_scanner(monkeypatch, tmp_path, capsys):
    for name, collected in (("normal", _collected()), ("empty", _empty_collected())):
        monkeypatch.setattr(validation_cli, "collect_run", lambda _root, value=collected: value)
        database = tmp_path / f"{name}.sqlite3"
        assert validation_cli.main([
            "collect", "--run-root", str(tmp_path / name), "--store", str(database),
        ]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["run_observation_appended"] is True
        assert output["scanner"] == len(collected.selections)
        with ValidationStore(database) as store:
            rows = load_run_observations(store)
        assert len(rows) == 1
        assert rows[0].scanner_count == len(collected.selections)


def test_run_observation_build_has_no_network_or_current_registry_dependency(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted")
    ))
    assert build_run_observation(_collected()).scanner_tickers == ("AAPL",)


def test_weekly_analytics_and_freeze_policy_are_exact_and_compute_no_scorecard():
    policy = PROTOCOL["weekly_analytics_policy"]
    assert PROTOCOL["freeze_status"] == "FROZEN_FOR_FIRST_OFFICIAL_OBSERVATION"
    assert policy["statistical_unit"] == "WEEKLY_RUN"
    assert policy["run_denominator"] == "ALL_IMMUTABLE_OFFICIAL_WEEKLY_RUNS"
    assert policy["empty_scanner_sleeve"] == "SPY_ACTIVE_RETURN_ZERO"
    assert policy["empty_luna_sleeve"] == "SPY_ACTIVE_RETURN_ZERO"
    assert policy["empty_sol_sleeve"] == "SPY_ACTIVE_RETURN_ZERO"
    assert policy["pending_member"] == "STAGE_PENDING"
    assert policy["dqf_member"] == "STAGE_UNAVAILABLE_NO_IMPUTATION"
    assert policy["dqf_partial_average"] is False
    assert policy["dqf_zero_imputation"] is False
    assert policy["primary_week_usable"] == "SCANNER_AND_SOL_STAGE_AVAILABLE"
    assert policy["formal_inference_denominator"] == "PRIMARY_USABLE_WEEKS"
    assert policy["formal_inference_minimum_weeks"] == 52
    assert policy["formal_inference_method"] == "CIRCULAR_BLOCK_BOOTSTRAP"
    assert policy["formal_inference_replicates"] == 10000
    assert policy["primary_block_length_weeks"] == 4
    assert policy["confidence_interval"] == 0.95
    assert policy["primary_metric"] == "GROSS_ACTIVE_RETURN"
    assert policy["trading_cost_role"] == "SENSITIVITY_ONLY"
    assert policy["trading_cost_scenarios_bps_per_side"] == (0, 10, 25)
    assert policy["cost_application"] == "ROUND_TRIP_COST_TO_NONEMPTY_STOCK_SLEEVE"
    assert policy["empty_benchmark_sleeve_cost"] == 0
    assert policy["selection_quality_flags"] == "FAIL_CLOSED_FOR_OFFICIAL_ANALYTICS"
    assert "stage_active_return" not in build_run_observation(_collected()).to_dict()


def test_run_observation_identity_is_deterministic_and_content_bound_to_protocol():
    assert run_observation_identity("RUN_A") == f"{PROTOCOL_HASH}:RUN_A"
