from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import socket

import pytest

import trinity.validation.cycle_registry as registry_module
from tests.trinity.validation.test_observation_cycle import TARGET, _one, _rewrite_bound_cycle
from tests.trinity.validation.test_spy_benchmark import _acquire
from tests.trinity.validation.test_stock_observation import _write_snapshot
from trinity.validation.cycle_registry import (
    OBSERVATION_CYCLE_REGISTRATION,
    CycleRegistryError,
    ObservationCycleRegistration,
    load_registered_cycles,
    register_observation_cycle,
    registered_cycles_for_expectation,
)
from trinity.validation.observation_cycle import (
    MANIFEST_NAME,
    create_observation_cycle,
    observation_cycle_identity,
)
from trinity.validation.protocol_v1 import PROTOCOL_HASH, canonical_json
from trinity.validation.stock_observation import expected_ticker_set_sha256
from trinity.validation.storage import (
    ValidationStorageConflict,
    ValidationStorageIntegrityError,
    ValidationStore,
)


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory):
    root = tmp_path_factory.mktemp("cycle-registry-artifacts")
    stock = root / "stock"
    stock_alternate = root / "stock-alternate"
    spy = root / "spy"
    spy_alternate = root / "spy-alternate"
    start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    alternate_start = start + timedelta(hours=1)
    _write_snapshot(stock, ("AAPL",), target=TARGET, started=start,
                    completed=start + timedelta(minutes=10))
    _write_snapshot(stock_alternate, ("AAPL",), target=TARGET,
                    started=alternate_start,
                    completed=alternate_start + timedelta(minutes=10))
    _acquire(spy, target=TARGET, started_at=start)
    _acquire(spy_alternate, target=TARGET, started_at=alternate_start)
    expectation = _one()
    cycle = root / "cycle"
    overlapping_cycle = root / "overlapping-cycle"
    create_observation_cycle(
        expectations=(expectation,), observation_target_session=TARGET.isoformat(),
        stock_root=stock, spy_root=spy, output_root=cycle,
    )
    create_observation_cycle(
        expectations=(expectation,), observation_target_session=TARGET.isoformat(),
        stock_root=stock_alternate, spy_root=spy, output_root=overlapping_cycle,
    )
    return {
        "root": root, "stock": stock, "stock_alternate": stock_alternate,
        "spy": spy, "spy_alternate": spy_alternate, "expectation": expectation,
        "cycle": cycle, "overlapping_cycle": overlapping_cycle,
    }


def _register(store: ValidationStore, artifacts, *, cycle="cycle", stock="stock", spy="spy",
              expectations=None) -> bool:
    return register_observation_cycle(
        store, cycle_root=artifacts[cycle],
        expectations=expectations or (artifacts["expectation"],),
        stock_root=artifacts[stock], spy_root=artifacts[spy],
    )


@pytest.fixture(scope="module")
def registrations(tmp_path_factory, artifacts):
    database = tmp_path_factory.mktemp("cycle-registry-db") / "registry.sqlite3"
    with ValidationStore(database) as store:
        assert _register(store, artifacts)
        assert _register(
            store, artifacts, cycle="overlapping_cycle", stock="stock_alternate",
        )
        values = load_registered_cycles(store)
    return values


def _payload(registrations, index=0) -> dict:
    return registrations[index].to_dict()


def _replace_stored_payload(store: ValidationStore, record_key: str, payload: dict) -> None:
    raw = canonical_json(payload)
    store.connection.execute(
        "UPDATE validation_record SET payload_json=?, payload_sha256=? "
        "WHERE record_kind=? AND record_key=?",
        (raw, hashlib.sha256(raw).hexdigest(), OBSERVATION_CYCLE_REGISTRATION, record_key),
    )


def test_valid_registration_and_identical_append_is_idempotent(tmp_path, artifacts):
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        assert _register(store, artifacts) is True
        assert _register(store, artifacts) is False
        values = load_registered_cycles(store)
    assert len(values) == 1
    assert values[0].key == values[0].observation_cycle_id


def test_divergent_payload_for_same_cycle_id_conflicts(tmp_path, artifacts):
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        _register(store, artifacts)
        registration = load_registered_cycles(store)[0]
        payload = registration.to_dict()
        payload["stock_acquisition_completed_at_utc"] = "2026-10-12T10:11:00+00:00"
        divergent = ObservationCycleRegistration.from_dict(payload)
        with pytest.raises(ValidationStorageConflict):
            store.append(
                OBSERVATION_CYCLE_REGISTRATION, divergent.key, divergent.to_dict(),
            )


def test_registration_preserves_all_verified_lineage(registrations, artifacts):
    manifest_raw = (artifacts["cycle"] / MANIFEST_NAME).read_bytes()
    manifest = json.loads(manifest_raw)
    registration = next(
        item for item in registrations
        if item.observation_cycle_id == manifest["observation_cycle_id"]
    )
    assert registration.observation_cycle_id == manifest["observation_cycle_id"]
    assert registration.observation_cycle_sha256 == manifest["observation_cycle_sha256"]
    assert registration.cycle_manifest_sha256 == hashlib.sha256(manifest_raw).hexdigest()
    assert registration.cycle_manifest_sha256 != registration.observation_cycle_sha256
    assert registration.expectation_set_sha256 == manifest["expectation_set_sha256"]
    assert registration.expectation_keys == tuple(
        item["expectation_key"] for item in manifest["expectation_refs"]
    )
    assert registration.expected_tickers == tuple(manifest["expected_tickers"])
    assert registration.expected_ticker_set_sha256 == manifest["expected_ticker_set_sha256"]
    stock = manifest["stock_snapshot"]
    spy = manifest["spy_snapshot"]
    assert (
        registration.stock_snapshot_id, registration.stock_snapshot_sha256,
        registration.stock_manifest_sha256,
    ) == (stock["snapshot_id"], stock["snapshot_sha256"], stock["manifest_sha256"])
    assert (
        registration.spy_snapshot_id, registration.spy_snapshot_sha256,
        registration.spy_manifest_sha256,
    ) == (spy["snapshot_id"], spy["snapshot_sha256"], spy["manifest_sha256"])


def test_expectation_keys_and_tickers_are_canonical(registrations):
    for item in registrations:
        assert item.expectation_keys == tuple(sorted(item.expectation_keys))
        assert len(item.expectation_keys) == len(set(item.expectation_keys))
        assert item.expected_tickers == tuple(sorted(item.expected_tickers)) == ("AAPL",)
        assert item.expected_ticker_set_sha256 == expected_ticker_set_sha256(("AAPL",))


def test_registration_payload_contains_no_local_paths(registrations, artifacts):
    serialized = json.dumps(registrations[0].to_dict(), sort_keys=True)
    for field in ("cycle_root", "stock_root", "spy_root"):
        assert field not in serialized
    for path in (artifacts["cycle"], artifacts["stock"], artifacts["spy"]):
        assert str(path) not in serialized
    assert "C:\\" not in serialized and "/home/" not in serialized

    tampered = registrations[0].to_dict()
    tampered["stock_snapshot_id"] = "C:\\private\\snapshot"
    with pytest.raises(CycleRegistryError, match="local path"):
        ObservationCycleRegistration.from_dict(tampered)


def test_registry_load_and_expectation_query_are_deterministic(registrations, artifacts):
    assert registrations == tuple(sorted(
        registrations,
        key=lambda item: (item.observation_target_session, item.observation_cycle_id),
    ))
    matches = registered_cycles_for_expectation(
        reversed(registrations), artifacts["expectation"].key,
    )
    assert matches == registrations
    assert len(matches) == 2
    assert matches[0].observation_cycle_id != matches[1].observation_cycle_id


def test_overlap_is_inventory_not_automatic_winner(registrations, artifacts):
    matches = registered_cycles_for_expectation(registrations, artifacts["expectation"].key)
    assert len(matches) == 2
    source = inspect.getsource(registry_module)
    assert "FIRST_ELIGIBLE" not in source
    assert "FIRST_OBSERVATION" not in source
    assert "AUTHORITATIVE_OUTCOME_CYCLE" not in source


@pytest.mark.parametrize(("field", "value", "message"), [
    ("protocol_hash", "0" * 64, "protocol identity"),
    ("observation_cycle_sha256", "bad", "observation_cycle_sha256"),
    ("cycle_manifest_sha256", "bad", "cycle_manifest_sha256"),
    ("observation_target_session", "not-a-date", "target"),
])
def test_malformed_registration_fields_are_rejected(registrations, field, value, message):
    payload = _payload(registrations)
    payload[field] = value
    with pytest.raises(CycleRegistryError, match=message):
        ObservationCycleRegistration.from_dict(payload)


@pytest.mark.parametrize("prefix", ["stock", "spy"])
def test_reversed_acquisition_timestamps_are_rejected(registrations, prefix):
    payload = _payload(registrations)
    payload[f"{prefix}_acquisition_started_at_utc"] = "2026-10-12T11:00:00Z"
    payload[f"{prefix}_acquisition_completed_at_utc"] = "2026-10-12T10:00:00Z"
    with pytest.raises(CycleRegistryError, match="reversed"):
        ObservationCycleRegistration.from_dict(payload)


def test_duplicate_expectation_keys_are_rejected(registrations):
    payload = _payload(registrations)
    payload["expectation_keys"] = payload["expectation_keys"] * 2
    with pytest.raises(CycleRegistryError, match="canonical"):
        ObservationCycleRegistration.from_dict(payload)


def test_storage_payload_corruption_is_rejected_before_deserialization(tmp_path, artifacts):
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        _register(store, artifacts)
        store.connection.execute(
            "UPDATE validation_record SET payload_json=? WHERE record_kind=?",
            (b"{}", OBSERVATION_CYCLE_REGISTRATION),
        )
        with pytest.raises(ValidationStorageIntegrityError):
            load_registered_cycles(store)


def test_rehashed_payload_with_record_key_mismatch_is_rejected(tmp_path, artifacts):
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        _register(store, artifacts)
        registration = load_registered_cycles(store)[0]
        payload = registration.to_dict()
        payload["observation_cycle_id"] = "f" * 64
        _replace_stored_payload(store, registration.key, payload)
        with pytest.raises(CycleRegistryError, match="record key"):
            load_registered_cycles(store)


def test_rehashed_payload_with_protocol_tampering_is_rejected(tmp_path, artifacts):
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        _register(store, artifacts)
        registration = load_registered_cycles(store)[0]
        payload = registration.to_dict()
        payload["protocol_hash"] = "0" * 64
        _replace_stored_payload(store, registration.key, payload)
        with pytest.raises(CycleRegistryError, match="stored cycle registration"):
            load_registered_cycles(store)


def test_corrupted_cycle_artifact_cannot_register(tmp_path, artifacts):
    cycle = tmp_path / "cycle"
    shutil.copytree(artifacts["cycle"], cycle)
    (cycle / MANIFEST_NAME).write_bytes(b"corrupt")
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        with pytest.raises(CycleRegistryError, match="verification failed"):
            register_observation_cycle(
                store, cycle_root=cycle, expectations=(artifacts["expectation"],),
                stock_root=artifacts["stock"], spy_root=artifacts["spy"],
            )


def test_stock_or_spy_source_mismatch_cannot_register(tmp_path, artifacts):
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        with pytest.raises(CycleRegistryError, match="verification failed"):
            _register(store, artifacts, stock="stock_alternate")
        with pytest.raises(CycleRegistryError, match="verification failed"):
            _register(store, artifacts, spy="spy_alternate")


def test_altered_expectation_lineage_cannot_register(tmp_path, artifacts):
    altered = artifacts["expectation"].to_dict()
    altered["selection_data_quality_flags"] = ["ALTERED"]
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        with pytest.raises(CycleRegistryError, match="verification failed"):
            _register(store, artifacts, expectations=(altered,))


def test_rehashed_cycle_lineage_still_cannot_register(tmp_path, artifacts):
    cycle = tmp_path / "cycle"
    shutil.copytree(artifacts["cycle"], cycle)
    manifest = json.loads((cycle / MANIFEST_NAME).read_bytes())
    manifest["stock_snapshot"]["snapshot_id"] = "forged-stock"
    manifest["identity_core"]["stock_snapshot_id"] = "forged-stock"
    manifest["observation_cycle_id"] = observation_cycle_identity(manifest["identity_core"])
    _rewrite_bound_cycle(cycle, manifest)
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        with pytest.raises(CycleRegistryError, match="verification failed"):
            register_observation_cycle(
                store, cycle_root=cycle, expectations=(artifacts["expectation"],),
                stock_root=artifacts["stock"], spy_root=artifacts["spy"],
            )


def test_no_network_maturity_state_or_selection_policy(tmp_path, artifacts, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", denied)
    source = inspect.getsource(registry_module)
    assert "OUTCOME_FINAL" not in source
    assert "DATA_QUALITY_FAILURE" not in source
    assert '"PENDING"' not in source
    assert "resolve_first" not in source
    with ValidationStore(tmp_path / "registry.sqlite3") as store:
        assert _register(store, artifacts)
