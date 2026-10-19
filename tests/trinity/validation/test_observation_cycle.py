from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import inspect
import json
from pathlib import Path
import socket

import pytest

import trinity.validation.observation_cycle as cycle_module
from tests.trinity.validation.test_outcome_expectations import _collected, _selection
from tests.trinity.validation.test_spy_benchmark import _acquire
from tests.trinity.validation.test_stock_observation import _write_snapshot
from trinity.validation.observation_cycle import (
    CYCLE_STATE,
    MANIFEST_NAME,
    READY_NAME,
    ObservationCycleError,
    create_observation_cycle,
    expectation_references,
    expectation_set_sha256,
    observation_cycle_envelope_sha256,
    observation_cycle_identity,
    verify_observation_cycle,
)
from trinity.validation.outcome_expectations import (
    OutcomeExpectation,
    build_outcome_expectations,
)
from trinity.validation.protocol_v1 import canonical_json
from trinity.validation.stock_observation import expected_ticker_set_sha256


TARGET = date(2026, 10, 9)


def _expectations(
    *tickers: str, decision: str = "2026-07-01T20:01:00Z",
) -> tuple[OutcomeExpectation, ...]:
    selections = tuple(replace(
        _selection(ticker=ticker), run_timestamp_utc=decision,
        intended_session=decision[:10],
    ) for ticker in (tickers or ("AAPL",)))
    return build_outcome_expectations(_collected(*selections))


def _one(
    ticker: str = "AAPL", *, decision: str = "2026-07-01T20:01:00Z",
    horizon: int = 5,
) -> OutcomeExpectation:
    return next(item for item in _expectations(ticker, decision=decision)
                if item.horizon_sessions == horizon)


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("observation-cycle-sources")
    stock_one = root / "stock-one"
    stock_two = root / "stock-two"
    stock_other_target = root / "stock-other-target"
    spy = root / "spy"
    _write_snapshot(stock_one, ("AAPL",), target=TARGET)
    _write_snapshot(stock_two, ("AAPL", "HOOD"), target=TARGET)
    _write_snapshot(stock_other_target, ("AAPL",), target=date(2026, 10, 8))
    _acquire(spy, target=TARGET)
    return {
        "stock_one": stock_one,
        "stock_two": stock_two,
        "stock_other_target": stock_other_target,
        "spy": spy,
    }


def _create(root: Path, sources, expectations=None, *, stock="stock_one", target="2026-10-09"):
    return create_observation_cycle(
        expectations=expectations or (_one(),), observation_target_session=target,
        stock_root=sources[stock], spy_root=sources["spy"], output_root=root,
    )


def _rewrite_bound_cycle(root: Path, manifest: dict) -> None:
    manifest["observation_cycle_sha256"] = observation_cycle_envelope_sha256(manifest)
    raw = canonical_json(manifest)
    (root / MANIFEST_NAME).write_bytes(raw)
    ready = {
        "schema_name": "trinity.validation.observation-cycle-ready",
        "schema_version": "1", "state": CYCLE_STATE,
        "observation_cycle_id": manifest["observation_cycle_id"],
        "observation_cycle_sha256": manifest["observation_cycle_sha256"],
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }
    (root / READY_NAME).write_bytes(canonical_json(ready))


def test_valid_single_expectation_closed_cycle(tmp_path, sources):
    manifest = _create(tmp_path / "cycle", sources)
    assert manifest["state"] == "CLOSED_VERIFIED"
    assert len(manifest["expectation_refs"]) == 1


def test_one_shot_expectation_iterable_is_frozen_before_self_verification(tmp_path, sources):
    manifest = _create(tmp_path / "cycle", sources, (item for item in (_one(),)))
    assert manifest["state"] == CYCLE_STATE


def test_multiple_expectations_for_same_ticker(tmp_path, sources):
    expectations = _expectations("AAPL")
    manifest = _create(tmp_path / "cycle", sources, expectations)
    assert len(manifest["expectation_refs"]) == 3
    assert manifest["expected_tickers"] == ["AAPL"]


def test_multiple_tickers_derive_exact_stock_membership(tmp_path, sources):
    expectations = (_one("HOOD"), _one("AAPL"))
    manifest = _create(tmp_path / "cycle", sources, expectations, stock="stock_two")
    assert manifest["expected_tickers"] == ["AAPL", "HOOD"]
    assert manifest["stock_snapshot"]["expected_tickers"] == ["AAPL", "HOOD"]


def test_expectation_order_does_not_change_refs_hash_or_cycle_id(tmp_path, sources):
    expectations = (_one("AAPL"), _one("HOOD"))
    left = _create(tmp_path / "left", sources, expectations, stock="stock_two")
    right = _create(tmp_path / "right", sources, tuple(reversed(expectations)), stock="stock_two")
    assert left["expectation_refs"] == right["expectation_refs"]
    assert left["expectation_set_sha256"] == right["expectation_set_sha256"]
    assert left["observation_cycle_id"] == right["observation_cycle_id"]
    assert left["observation_cycle_sha256"] == right["observation_cycle_sha256"]


def test_duplicate_expectation_key_is_rejected(tmp_path, sources):
    item = _one()
    with pytest.raises(ObservationCycleError, match="duplicate"):
        _create(tmp_path / "cycle", sources, (item, item))
    assert not (tmp_path / "cycle" / READY_NAME).exists()


def test_malformed_or_mixed_protocol_expectation_is_rejected(tmp_path, sources):
    malformed = _one().to_dict()
    malformed["protocol_hash"] = "0" * 64
    with pytest.raises(ObservationCycleError, match="malformed"):
        _create(tmp_path / "cycle", sources, (malformed,))


def test_maturity_equal_target_passes(tmp_path, sources):
    item = _one(decision="2026-10-02T20:01:00Z")
    assert item.maturity_session == TARGET.isoformat()
    _create(tmp_path / "cycle", sources, (item,))


def test_maturity_before_target_passes(tmp_path, sources):
    item = _one()
    assert item.maturity_session < TARGET.isoformat()
    _create(tmp_path / "cycle", sources, (item,))


def test_unmatured_expectation_is_rejected_without_ready(tmp_path, sources):
    item = _one(decision="2026-10-09T20:01:00Z")
    assert item.maturity_session > TARGET.isoformat()
    root = tmp_path / "cycle"
    with pytest.raises(ObservationCycleError, match="not mature"):
        _create(root, sources, (item,))
    assert not (root / READY_NAME).exists()


def test_missing_or_unexpected_stock_ticker_is_rejected(tmp_path, sources):
    with pytest.raises(ObservationCycleError, match="membership"):
        _create(
            tmp_path / "missing", sources, (_one("AAPL"), _one("HOOD")),
            stock="stock_one",
        )
    with pytest.raises(ObservationCycleError, match="membership"):
        _create(tmp_path / "unexpected", sources, (_one("AAPL"),), stock="stock_two")


def test_stock_target_mismatch_is_rejected(tmp_path, sources):
    with pytest.raises(ObservationCycleError, match="target session mismatch"):
        _create(tmp_path / "cycle", sources, stock="stock_other_target")


def test_spy_target_mismatch_is_rejected(tmp_path, sources):
    item = _one(decision="2026-06-01T20:01:00Z")
    with pytest.raises(ObservationCycleError, match="SPY observation"):
        _create(
            tmp_path / "cycle", sources, (item,), stock="stock_other_target",
            target="2026-10-08",
        )


def test_snapshot_identities_manifest_hashes_and_common_target_are_frozen(tmp_path, sources):
    manifest = _create(tmp_path / "cycle", sources)
    stock_manifest = (sources["stock_one"] / "snapshot_manifest.json").read_bytes()
    spy_manifest = (sources["spy"] / "benchmark_manifest.json").read_bytes()
    assert manifest["stock_snapshot"]["snapshot_id"]
    assert manifest["stock_snapshot"]["snapshot_sha256"]
    assert manifest["spy_snapshot"]["snapshot_id"]
    assert manifest["spy_snapshot"]["snapshot_sha256"]
    assert manifest["stock_snapshot"]["manifest_sha256"] == hashlib.sha256(
        stock_manifest,
    ).hexdigest()
    assert manifest["spy_snapshot"]["manifest_sha256"] == hashlib.sha256(
        spy_manifest,
    ).hexdigest()
    assert {
        manifest["observation_target_session"],
        manifest["stock_snapshot"]["observation_target_session"],
        manifest["spy_snapshot"]["observation_target_session"],
    } == {TARGET.isoformat()}


def test_expectation_and_ticker_set_hashes_are_deterministic():
    expectations = (_one("HOOD"), _one("AAPL"))
    left = expectation_references(expectations)
    right = expectation_references(reversed(expectations))
    assert left == right
    assert expectation_set_sha256(left) == expectation_set_sha256(right)
    assert expected_ticker_set_sha256(["HOOD", "AAPL"]) == \
        expected_ticker_set_sha256(["AAPL", "HOOD"])


def test_cycle_identity_is_content_bound_and_envelope_is_separate(tmp_path, sources):
    manifest = _create(tmp_path / "cycle", sources)
    assert manifest["observation_cycle_id"] == observation_cycle_identity(
        manifest["identity_core"],
    )
    assert manifest["observation_cycle_sha256"] == observation_cycle_envelope_sha256(
        manifest,
    )
    assert manifest["observation_cycle_id"] != manifest["observation_cycle_sha256"]


def test_output_root_must_be_empty(tmp_path, sources):
    root = tmp_path / "cycle"
    root.mkdir()
    (root / "existing").write_text("occupied", encoding="utf-8")
    with pytest.raises(ObservationCycleError, match="new or empty"):
        _create(root, sources)


def test_missing_ready_corrupt_manifest_corrupt_ready_and_extra_file_fail(tmp_path, sources):
    missing = tmp_path / "missing"
    _create(missing, sources)
    (missing / READY_NAME).unlink()
    with pytest.raises(ObservationCycleError):
        verify_observation_cycle(
            missing, expectations=(_one(),), stock_root=sources["stock_one"],
            spy_root=sources["spy"],
        )

    corrupt_manifest = tmp_path / "manifest"
    _create(corrupt_manifest, sources)
    (corrupt_manifest / MANIFEST_NAME).write_bytes(b"not-json")
    with pytest.raises(ObservationCycleError):
        verify_observation_cycle(
            corrupt_manifest, expectations=(_one(),), stock_root=sources["stock_one"],
            spy_root=sources["spy"],
        )

    corrupt_ready = tmp_path / "ready"
    _create(corrupt_ready, sources)
    (corrupt_ready / READY_NAME).write_bytes(b"not-json")
    with pytest.raises(ObservationCycleError):
        verify_observation_cycle(
            corrupt_ready, expectations=(_one(),), stock_root=sources["stock_one"],
            spy_root=sources["spy"],
        )

    extra = tmp_path / "extra"
    _create(extra, sources)
    (extra / "unexpected").write_text("x", encoding="utf-8")
    with pytest.raises(ObservationCycleError, match="closure"):
        verify_observation_cycle(
            extra, expectations=(_one(),), stock_root=sources["stock_one"],
            spy_root=sources["spy"],
        )


@pytest.mark.parametrize(("section", "field", "value"), [
    ("ref", "expectation_key", "altered"),
    ("root", "expectation_set_sha256", "0" * 64),
    ("root", "expected_tickers", ["MSFT"]),
    ("root", "expected_ticker_set_sha256", "0" * 64),
    ("stock_snapshot", "snapshot_id", "altered-stock"),
    ("stock_snapshot", "snapshot_sha256", "0" * 64),
    ("stock_snapshot", "manifest_sha256", "0" * 64),
    ("spy_snapshot", "snapshot_id", "altered-spy"),
    ("spy_snapshot", "snapshot_sha256", "0" * 64),
    ("spy_snapshot", "manifest_sha256", "0" * 64),
    ("root", "observation_target_session", "2026-10-08"),
    ("stock_snapshot", "acquisition_started_at_utc", "2026-01-01T00:00:00Z"),
    ("spy_snapshot", "acquisition_completed_at_utc", "2026-01-01T00:00:00Z"),
    ("root", "observation_cycle_id", "0" * 64),
])
def test_manifest_tampering_is_rejected_even_after_envelope_and_ready_rehash(
    tmp_path, sources, section, field, value,
):
    root = tmp_path / f"cycle-{section}-{field}"
    _create(root, sources)
    manifest = json.loads((root / MANIFEST_NAME).read_bytes())
    if section == "root":
        manifest[field] = value
    elif section == "ref":
        manifest["expectation_refs"][0][field] = value
    else:
        manifest[section][field] = value
    _rewrite_bound_cycle(root, manifest)
    with pytest.raises(ObservationCycleError):
        verify_observation_cycle(
            root, expectations=(_one(),), stock_root=sources["stock_one"],
            spy_root=sources["spy"],
        )


def test_altered_expectation_payload_is_rejected_against_frozen_refs(tmp_path, sources):
    root = tmp_path / "cycle"
    item = _one()
    _create(root, sources, (item,))
    altered = item.to_dict()
    altered["selection_data_quality_flags"] = ["ALTERED"]
    with pytest.raises(ObservationCycleError, match="expectation_refs"):
        verify_observation_cycle(
            root, expectations=(altered,), stock_root=sources["stock_one"],
            spy_root=sources["spy"],
        )


def test_rehash_cannot_bypass_expectation_stock_or_spy_authority(tmp_path, sources):
    mutations = (
        ("expectation_refs", "expectation_payload_sha256", "0" * 64),
        ("stock_snapshot", "snapshot_id", "forged-stock"),
        ("spy_snapshot", "snapshot_id", "forged-spy"),
    )
    for index, (section, field, value) in enumerate(mutations):
        root = tmp_path / f"cycle-{index}"
        _create(root, sources)
        manifest = json.loads((root / MANIFEST_NAME).read_bytes())
        if section == "expectation_refs":
            manifest[section][0][field] = value
            manifest["expectation_set_sha256"] = expectation_set_sha256(
                manifest[section],
            )
            manifest["identity_core"]["expectation_set_sha256"] = \
                manifest["expectation_set_sha256"]
        else:
            manifest[section][field] = value
            prefix = "stock" if section == "stock_snapshot" else "spy"
            manifest["identity_core"][f"{prefix}_snapshot_id"] = value
        manifest["observation_cycle_id"] = observation_cycle_identity(
            manifest["identity_core"],
        )
        _rewrite_bound_cycle(root, manifest)
        with pytest.raises(ObservationCycleError, match="authoritative lineage"):
            verify_observation_cycle(
                root, expectations=(_one(),), stock_root=sources["stock_one"],
                spy_root=sources["spy"],
            )


def test_bad_cycle_sha_and_ready_binding_are_rejected(tmp_path, sources):
    bad_sha = tmp_path / "bad-sha"
    _create(bad_sha, sources)
    manifest = json.loads((bad_sha / MANIFEST_NAME).read_bytes())
    manifest["observation_cycle_sha256"] = "0" * 64
    raw = canonical_json(manifest)
    (bad_sha / MANIFEST_NAME).write_bytes(raw)
    ready = json.loads((bad_sha / READY_NAME).read_bytes())
    ready["observation_cycle_sha256"] = "0" * 64
    ready["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    (bad_sha / READY_NAME).write_bytes(canonical_json(ready))
    with pytest.raises(ObservationCycleError, match="envelope"):
        verify_observation_cycle(
            bad_sha, expectations=(_one(),), stock_root=sources["stock_one"],
            spy_root=sources["spy"],
        )

    bad_ready = tmp_path / "bad-ready"
    _create(bad_ready, sources)
    ready = json.loads((bad_ready / READY_NAME).read_bytes())
    ready["manifest_sha256"] = "0" * 64
    (bad_ready / READY_NAME).write_bytes(canonical_json(ready))
    with pytest.raises(ObservationCycleError, match="bind"):
        verify_observation_cycle(
            bad_ready, expectations=(_one(),), stock_root=sources["stock_one"],
            spy_root=sources["spy"],
        )


def test_manifest_is_portable_and_has_no_local_paths(tmp_path, sources):
    root = tmp_path / "cycle"
    manifest = _create(root, sources)
    serialized = json.dumps(manifest, sort_keys=True)
    assert str(root) not in serialized
    assert str(sources["stock_one"]) not in serialized
    assert str(sources["spy"]) not in serialized
    assert "C:\\" not in serialized and "/home/" not in serialized


def test_no_registry_network_or_maturity_state_dependency(tmp_path, sources, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", denied)
    source = inspect.getsource(cycle_module)
    assert "usa_issuer_registry" not in source
    assert "OUTCOME_FINAL" not in source
    assert "DATA_QUALITY_FAILURE" not in source
    assert '"PENDING"' not in source
    _create(tmp_path / "cycle", sources)
