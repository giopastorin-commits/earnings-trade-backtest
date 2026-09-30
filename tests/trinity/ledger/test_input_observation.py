from __future__ import annotations

import json
import sqlite3

import pytest

from trinity.ledger import (
    InvalidObservationProvenance,
    LedgerStorage,
    StaleAttemptError,
    UnsupportedAvailabilityBasis,
)

RETRIEVED = "2026-09-29T10:15:30.123456Z"
LATER = "2026-09-29T10:15:31.123456Z"


@pytest.fixture
def storage(tmp_path):
    with LedgerStorage.open(tmp_path / "observation.sqlite3") as opened:
        yield opened


def _attempt(storage: LedgerStorage, *, worker: str = "observer"):
    request = storage.create_run_request(
        request_kind="OBSERVATION_TEST_V1",
        analysis_cutoff_at="2026-09-29T12:00:00.000000Z",
        parameters={},
        idempotency_key=f"request-{worker}",
        requested_by="observation-tests",
        baseline_commit="abc123",
    )
    return storage.allocate_attempt(
        run_request_id=request.run_request_id,
        worker_identity=worker,
        code_commit="def456",
        environment_fingerprint="env:v1",
    )


def _artifact(storage: LedgerStorage):
    return storage.insert_opaque_artifact(
        artifact_type="source.fixture.v1",
        payload=b"immutable source payload",
        media_type="application/octet-stream",
    )


def _metadata(record_key: str = "record-1", **overrides):
    value = {
        "schema_version": "1",
        "provider": "test-provider",
        "dataset_name": "earnings",
        "source_record_key": record_key,
        "acquisition_method": "fixture-import-v1",
        "availability_rule_id": "retrieval-time-fallback",
        "availability_rule_version": "1",
        "evidence_artifact_ids": [],
        "provider_metadata": {},
        "future_effective_at": None,
    }
    value.update(overrides)
    return value


def _observe(storage: LedgerStorage, attempt, artifact, **overrides):
    values = {
        "artifact_id": artifact.artifact_id,
        "source_id": "test-provider:earnings:v1",
        "source_record_key": "record-1",
        "source_published_at": None,
        "retrieved_at": RETRIEVED,
        "availability_basis": "RETRIEVED_AT_FALLBACK",
        "effective_available_at": RETRIEVED,
        "observed_by_attempt_id": attempt.attempt_id,
        "fence_token": attempt.fence_token,
        "source_metadata": _metadata(),
    }
    values.update(overrides)
    return storage.create_input_observation(**values)


def test_valid_fallback_observation_is_canonical_and_readable(storage):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    observation = _observe(storage, attempt, artifact)

    assert observation.artifact_id == artifact.artifact_id
    assert observation.observed_by_attempt_id == attempt.attempt_id
    assert observation.source_id == "test-provider:earnings:v1"
    assert observation.source_record_key == "record-1"
    assert observation.source_published_at is None
    assert observation.availability_basis == "RETRIEVED_AT_FALLBACK"
    assert observation.effective_available_at == observation.retrieved_at == RETRIEVED
    assert json.loads(observation.source_metadata_json) == _metadata()
    assert observation.source_metadata_json.startswith('{"acquisition_method"')
    assert storage.get_input_observation(observation.input_observation_id) == observation


@pytest.mark.parametrize("effective", ["2026-09-29T10:15:29.123456Z", LATER])
def test_fallback_rejects_any_effective_timestamp_other_than_retrieval(storage, effective):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    with pytest.raises(InvalidObservationProvenance, match="exactly"):
        _observe(storage, attempt, artifact, effective_available_at=effective)
    assert storage.list_input_observations() == []


@pytest.mark.parametrize("basis", ["SOURCE_PUBLISHED_AT", "LEGACY_ASSERTED_AT"])
def test_non_fallback_bases_are_explicitly_deferred_without_downgrade(storage, basis):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    with pytest.raises(UnsupportedAvailabilityBasis, match="deferred"):
        _observe(storage, attempt, artifact, availability_basis=basis)
    assert storage.list_input_observations() == []


def test_fallback_rejects_publication_and_future_effective_claims(storage):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    with pytest.raises(InvalidObservationProvenance, match="source_published_at"):
        _observe(storage, attempt, artifact, source_published_at=RETRIEVED)
    with pytest.raises(InvalidObservationProvenance, match="future_effective_at"):
        _observe(
            storage,
            attempt,
            artifact,
            source_metadata=_metadata(future_effective_at=LATER),
        )


def test_timestamps_require_normalized_utc_microseconds(storage):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    with pytest.raises(ValueError, match="UTC timestamp with microseconds"):
        _observe(
            storage,
            attempt,
            artifact,
            retrieved_at="2026-09-29T12:15:30+02:00",
            effective_available_at="2026-09-29T12:15:30+02:00",
        )


@pytest.mark.parametrize(
    "source_id",
    [
        "Test-Provider:earnings",
        "test-provider",
        "test-provider::earnings",
        r"C:\\mutable\\source",
        "https://provider.example/dataset",
    ],
)
def test_source_id_requires_stable_logical_canonical_identity(storage, source_id):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    with pytest.raises(InvalidObservationProvenance, match="source_id"):
        _observe(storage, attempt, artifact, source_id=source_id)


def test_source_metadata_exact_contract_and_record_consistency(storage):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    missing = _metadata()
    missing.pop("acquisition_method")
    with pytest.raises(InvalidObservationProvenance, match="exactly"):
        _observe(storage, attempt, artifact, source_metadata=missing)
    with pytest.raises(InvalidObservationProvenance, match="source_record_key"):
        _observe(storage, attempt, artifact, source_metadata=_metadata("different"))
    with pytest.raises(InvalidObservationProvenance, match="provider and dataset_name"):
        _observe(
            storage,
            attempt,
            artifact,
            source_metadata=_metadata(provider="another-provider"),
        )
    with pytest.raises(InvalidObservationProvenance, match="secrets or mutable"):
        _observe(
            storage,
            attempt,
            artifact,
            source_metadata=_metadata(provider_metadata={"local_path": "/tmp/current"}),
        )


def test_evidence_must_be_sorted_unique_and_attached_to_attempt(storage):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    evidence = storage.insert_json_artifact(
        artifact_type="source.evidence.v1", value={"proof": "fixture"}
    )
    with pytest.raises(InvalidObservationProvenance, match="attached"):
        _observe(
            storage,
            attempt,
            artifact,
            source_metadata=_metadata(evidence_artifact_ids=[evidence.artifact_id]),
        )
    storage.attach_attempt_artifact(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        artifact_id=evidence.artifact_id,
        role="DIAGNOSTIC",
    )
    observation = _observe(
        storage,
        attempt,
        artifact,
        source_metadata=_metadata(evidence_artifact_ids=[evidence.artifact_id]),
    )
    assert json.loads(observation.source_metadata_json)["evidence_artifact_ids"] == [
        evidence.artifact_id
    ]


def test_same_artifact_can_have_independent_observations_without_duplication(storage):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    first = _observe(storage, attempt, artifact)
    second = _observe(
        storage,
        attempt,
        artifact,
        source_record_key="record-2",
        retrieved_at=LATER,
        effective_available_at=LATER,
        source_metadata=_metadata("record-2"),
    )
    assert first.input_observation_id != second.input_observation_id
    assert first.retrieved_at != second.retrieved_at
    assert first.artifact_id == second.artifact_id
    assert len(storage.list_input_observations(artifact_id=artifact.artifact_id)) == 2
    assert storage.connection.execute("SELECT count(*) FROM artifact").fetchone()[0] == 1


def test_stale_and_forged_attempt_authority_are_rejected_transactionally(storage):
    first = _attempt(storage)
    artifact = _artifact(storage)
    second = storage.allocate_attempt(
        run_request_id=first.run_request_id,
        worker_identity="replacement",
        code_commit="def456",
        environment_fingerprint="env:v1",
    )
    with pytest.raises(StaleAttemptError):
        _observe(storage, first, artifact)
    with pytest.raises(StaleAttemptError):
        _observe(storage, second, artifact, fence_token=second.fence_token + 1)
    assert storage.list_input_observations() == []


def test_historical_observation_survives_supersession_and_is_immutable(storage):
    first = _attempt(storage)
    observation = _observe(storage, first, _artifact(storage))
    storage.allocate_attempt(
        run_request_id=first.run_request_id,
        worker_identity="replacement",
        code_commit="def456",
        environment_fingerprint="env:v1",
    )
    assert storage.get_input_observation(observation.input_observation_id) == observation
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "UPDATE input_observation SET source_id = source_id WHERE input_observation_id = ?",
            (observation.input_observation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "DELETE FROM input_observation WHERE input_observation_id = ?",
            (observation.input_observation_id,),
        )


def test_schema_rejects_direct_observation_forgery(storage):
    attempt = _attempt(storage)
    artifact = _artifact(storage)
    with pytest.raises(sqlite3.IntegrityError, match="authoritative write required"):
        storage.connection.execute(
            """
            INSERT INTO input_observation (
                input_observation_id, artifact_id, source_id, source_record_key,
                source_published_at, retrieved_at, availability_basis,
                effective_available_at, observed_by_attempt_id, source_metadata_json
            ) VALUES ('forged', ?, 'test-provider:earnings', 'record-1', NULL, ?,
                      'RETRIEVED_AT_FALLBACK', ?, ?, '{}')
            """,
            (artifact.artifact_id, RETRIEVED, RETRIEVED, attempt.attempt_id),
        )
