from __future__ import annotations

import json

import pytest

from trinity.validation.protocol_v1 import PROTOCOL, PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID
from trinity.validation.storage import ValidationStorageConflict, ValidationStore


def test_protocol_identity_and_frozen_parameters():
    assert PROTOCOL_ID == "TRINITY_LIVE_VALIDATION_V1"
    assert PROTOCOL_EPOCH == 1
    assert PROTOCOL["primary_horizon_sessions"] == 20
    assert tuple(PROTOCOL["secondary_horizons_sessions"]) == (5, 60)
    assert PROTOCOL["benchmark"] == "SPY_PRICE_RETURN"
    assert PROTOCOL["benchmark_contract"] == {
        "symbol": "SPY", "return_kind": "PRICE_RETURN", "provider": "TWELVE_DATA",
        "interval": "1day", "adjustment": "none", "universe_role": "BENCHMARK_ONLY",
    }
    assert PROTOCOL["horizon_definition"] == \
        "FIRST_TRADABLE_OPEN_TO_NTH_TRADABLE_SESSION_CLOSE_INCLUSIVE"
    assert dict(PROTOCOL["observation_policy"]) == {
        "cadence": "WEEKLY",
        "week_definition": "ISO_WEEK",
        "slot_target": "FIRST_XNYS_SESSION_OF_ISO_WEEK",
        "finality_grace_minutes": 20,
        "first_eligible_slot": "FIRST_SLOT_TARGET_ON_OR_AFTER_MATURITY_SESSION",
        "slot_expected_set": "ALL_MATURE_UNASSIGNED_V1_EXPECTATIONS",
        "missing_slot": "REMAIN_PENDING",
        "partial_cycle_official": False,
        "missing_slot_is_dqf": False,
        "multiple_candidate_cycles": "AMBIGUOUS_OPERATIONAL_ERROR",
    }
    assert dict(PROTOCOL["outcome_finalization_policy"]) == {
        "pending_is_terminal": False,
        "pending_persisted_as_outcome_final": False,
        "unassigned_expectation": "PENDING",
        "assigned_source_not_available": "PENDING",
        "supplied_source_verification_failure": "OPERATIONAL_ERROR_NO_FINAL",
        "dqf_requires_authoritative_assignment": True,
        "dqf_requires_positive_verified_evidence": True,
        "terminal_stock_missing_required_session": "DATA_QUALITY_FAILURE",
        "active_stock_missing_required_session": "CONTRACT_ERROR_NO_FINAL",
        "benchmark_missing_required_session": "CONTRACT_ERROR_NO_FINAL",
        "forward_fill_missing_session": False,
        "complete_return": "D1_OPEN_TO_DN_CLOSE",
        "benchmark_return": "D1_OPEN_TO_DN_CLOSE",
        "active_return": "STOCK_RETURN_MINUS_BENCHMARK_RETURN",
        "outcome_final_is_terminal": True,
        "outcome_restatement": False,
    }
    assert PROTOCOL["trading_cost_bps_per_side"] == 10
    assert tuple(PROTOCOL["trading_cost_sensitivity_bps_per_side"]) == (0, 25)
    assert len(PROTOCOL_HASH) == 64
    assert PROTOCOL_HASH != "074ced65bbdef589ff94ce31a5214982ffd482f830c508f15143638cc7bf30f5"
    assert PROTOCOL_HASH != "38980fc7bdc0ab8912f23cbc8ce67325f0d0ffe896a8d29838fe81ed1d094a1a"
    assert PROTOCOL_HASH != "134d9d6a3751c27952a3848baeff4d12642bfcccebada68b6984e95c2c8449a8"
    with pytest.raises(TypeError):
        PROTOCOL["protocol_epoch"] = 2


def test_append_only_store_is_idempotent_and_fails_on_divergence(tmp_path):
    database = tmp_path / "validation.sqlite3"
    with ValidationStore(database) as store:
        assert store.append("SELECTION", "run:A", {"value": 1}) is True
        assert store.append("SELECTION", "run:A", {"value": 1}) is False
        with pytest.raises(ValidationStorageConflict, match="divergent"):
            store.append("SELECTION", "run:A", {"value": 2})
        assert store.records("SELECTION") == [{"value": 1}]
        assert store.integrity() == ("ok", 0)


def test_store_payload_is_canonical(tmp_path):
    with ValidationStore(tmp_path / "v.sqlite3") as store:
        store.append("X", "1", {"b": 2, "a": 1})
        raw = store.connection.execute("select payload_json from validation_record").fetchone()[0]
        assert json.loads(bytes(raw)) == {"a": 1, "b": 2}
