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
    assert PROTOCOL["trading_cost_bps_per_side"] == 10
    assert tuple(PROTOCOL["trading_cost_sensitivity_bps_per_side"]) == (0, 25)
    assert len(PROTOCOL_HASH) == 64
    assert PROTOCOL_HASH != "074ced65bbdef589ff94ce31a5214982ffd482f830c508f15143638cc7bf30f5"
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
