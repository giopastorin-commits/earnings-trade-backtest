from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from trinity.ledger import (
    ClassificationIntegrityError,
    DerivationParent,
    FinalizationConflict,
    LedgerStorage,
    RunInputBinding,
    StaleAttemptError,
    canonicalize_json,
    pit_classification_policy_definition_v1,
)

T1 = "2026-09-29T09:00:00.000000Z"
T1_HALF = "2026-09-29T09:30:00.000000Z"
T2 = "2026-09-29T10:00:00.000000Z"
T3 = "2026-09-29T11:00:00.000000Z"


@pytest.fixture
def storage(tmp_path):
    with LedgerStorage.open(tmp_path / "classification.sqlite3") as opened:
        yield opened


def _attempt(storage, kind="PIT_SAFE_DECISION", suffix="a"):
    params = {}
    if kind == "RECONSTRUCTION":
        params = {"historical_as_of_at": T1}
    elif kind == "EXPLORATORY_NON_PIT":
        params = {"pit_reference_at": T1}
    request = storage.create_run_request(
        request_kind=kind, analysis_cutoff_at=T3, parameters=params,
        idempotency_key=f"pit-{kind}-{suffix}", requested_by="pit-tests",
        baseline_commit="abc123",
    )
    return storage.allocate_attempt(
        run_request_id=request.run_request_id, worker_identity=f"worker-{suffix}",
        code_commit="def456", environment_fingerprint="env:v1",
    )


def _policy(storage):
    artifact = storage.insert_json_artifact(
        artifact_type="ledger.pit-classification-policy-definition.v1",
        value=pit_classification_policy_definition_v1(),
    )
    return storage.register_pit_classification_policy(
        definition_artifact_id=artifact.artifact_id, code_commit="def456"
    )


def _raw(storage, attempt, name, retrieved=T1):
    artifact = storage.insert_opaque_artifact(
        artifact_type=f"source.{name}.v1", payload=name.encode(),
        media_type="application/octet-stream",
    )
    observation = storage.create_input_observation(
        artifact_id=artifact.artifact_id, source_id="test:pit:v1",
        source_record_key=name, source_published_at=None, retrieved_at=retrieved,
        availability_basis="RETRIEVED_AT_FALLBACK", effective_available_at=retrieved,
        observed_by_attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        source_metadata={
            "schema_version":"1", "provider":"test", "dataset_name":"pit",
            "source_record_key":name, "acquisition_method":"fixture",
            "availability_rule_id":"retrieval-fallback",
            "availability_rule_version":"1", "evidence_artifact_ids":[],
            "provider_metadata":{}, "future_effective_at":None,
        },
    )
    node = storage.create_raw_derivation_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        input_observation_id=observation.input_observation_id,
    )
    return artifact, observation, node


def _evidence(storage, attempt, artifact, observation, node, *, reference,
              kind="LEDGER_CONTEMPORANEOUS_CAPTURE", origin="LEDGER_AUTHORIZED_CAPTURE",
              support=()):
    matrix = {
        "LEDGER_CONTEMPORANEOUS_CAPTURE": ("LEDGER_AUTHORIZED_CAPTURE_V1", "CONTEMPORANEOUS_CAPTURE_VERIFIED", observation.retrieved_at, None),
        "INDEPENDENT_ARCHIVE_PROOF": ("INDEPENDENT_ARCHIVE_PROOF_V1", "INDEPENDENT_ARCHIVE_VERIFIED", T1, None),
        "POST_REFERENCE_RECONSTRUCTION": ("LEDGER_RECONSTRUCTION_EVIDENCE_V1", "POST_REFERENCE_RECONSTRUCTION_VERIFIED", None, T1_HALF),
        "INSUFFICIENT_PIT_EVIDENCE": ("LEDGER_INSUFFICIENT_EVIDENCE_V1", "PIT_EVIDENCE_INSUFFICIENT", None, None),
        "PIT_IRRELEVANT_CONTENT": ("LEDGER_PIT_IRRELEVANCE_V1", "PIT_NOT_APPLICABLE_BY_ARTIFACT_KIND", None, None),
    }
    verifier, rationale, archive, reconstruction = matrix[kind]
    legacy_namespace = legacy_key = legacy_id = None
    if origin == "LEGACY_IMPORT":
        legacy_namespace, legacy_key = "legacy.test", f"record-{node.derivation_node_id}"
        digest = hashlib.sha256(canonicalize_json({"legacy_namespace":legacy_namespace,"legacy_record_key":legacy_key})).hexdigest()
        legacy_id = f"legacy:sha256:{digest}"
    value = {
        "schema_name":"ledger.pit-classification-evidence", "schema_version":"1",
        "derivation_node_id":node.derivation_node_id,
        "input_observation_id":observation.input_observation_id,
        "artifact_id":artifact.artifact_id, "pit_reference_at":reference,
        "record_origin_evidence_kind":origin, "legacy_namespace":legacy_namespace,
        "legacy_record_key":legacy_key, "legacy_record_id":legacy_id,
        "pit_evidence_kind":kind, "verifier_id":verifier,
        "archive_captured_at":archive, "reconstruction_completed_at":reconstruction,
        "supporting_artifact_ids":sorted(support), "rationale_code":rationale,
    }
    evidence = storage.insert_json_artifact(
        artifact_type="ledger.pit-classification-evidence.v1", value=value
    )
    for item in (*support, evidence.artifact_id):
        storage.attach_attempt_artifact(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            artifact_id=item, role="DIAGNOSTIC",
        )
    return evidence


def _classify_raw(storage, attempt, policy, artifact, observation, node, *,
                  reference=T3, kind="LEDGER_CONTEMPORANEOUS_CAPTURE",
                  origin="LEDGER_AUTHORIZED_CAPTURE", support=(), supersedes=None):
    evidence = _evidence(storage, attempt, artifact, observation, node,
                         reference=reference, kind=kind, origin=origin, support=support)
    return storage.create_raw_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=node.derivation_node_id, pit_reference_at=reference,
        classification_policy_id=policy.classification_policy_id,
        evidence_artifact_id=evidence.artifact_id,
        supersedes_classification_id=supersedes,
    )


def test_policy_registration_is_exact_and_immutable(storage):
    policy = _policy(storage)
    assert policy.policy_version == "REQUIRED_ANCESTRY_PIT_V1"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        storage.connection.execute("UPDATE pit_classification_policy SET code_commit=code_commit")
    malformed = pit_classification_policy_definition_v1()
    malformed["extra"] = True
    artifact = storage.insert_json_artifact(
        artifact_type="ledger.pit-classification-policy-definition.v1", value=malformed
    )
    with pytest.raises(ClassificationIntegrityError):
        storage.register_pit_classification_policy(
            definition_artifact_id=artifact.artifact_id, code_commit="x"
        )


@pytest.mark.parametrize(
    "kind,origin,expected_pit,expected_record",
    [
        ("LEDGER_CONTEMPORANEOUS_CAPTURE", "LEDGER_AUTHORIZED_CAPTURE", "ARCHIVED_POINT_IN_TIME", "LEDGER_NATIVE"),
        ("INDEPENDENT_ARCHIVE_PROOF", "LEGACY_IMPORT", "ARCHIVED_POINT_IN_TIME", "LEGACY_NON_LEDGER_ARTIFACT"),
        ("POST_REFERENCE_RECONSTRUCTION", "LEDGER_AUTHORIZED_CAPTURE", "RECONSTRUCTED_NOT_ARCHIVED", "LEDGER_NATIVE"),
        ("INSUFFICIENT_PIT_EVIDENCE", "LEDGER_AUTHORIZED_CAPTURE", "UNKNOWN", "LEDGER_NATIVE"),
    ],
)
def test_raw_evidence_matrix(storage, kind, origin, expected_pit, expected_record):
    attempt = _attempt(storage, "RECONSTRUCTION", kind)
    policy = _policy(storage)
    retrieved = T1 if kind == "LEDGER_CONTEMPORANEOUS_CAPTURE" else T2
    artifact, observation, node = _raw(storage, attempt, kind, retrieved)
    support = ()
    if kind in {"INDEPENDENT_ARCHIVE_PROOF", "POST_REFERENCE_RECONSTRUCTION"}:
        proof = storage.insert_json_artifact(artifact_type="proof.v1", value={"kind":kind})
        support = (proof.artifact_id,)
    result = _classify_raw(storage, attempt, policy, artifact, observation, node,
                           reference=T1, kind=kind, origin=origin, support=support)
    assert (result.pit_class, result.record_class, result.resolved_record_class) == (
        expected_pit, expected_record, expected_record
    )
    assert storage.verify_node_classification(result.derivation_node_classification_id) == result


@pytest.mark.parametrize(
    "left,right,result",
    [
        ("ARCHIVED_POINT_IN_TIME","ARCHIVED_POINT_IN_TIME","ARCHIVED_POINT_IN_TIME"),
        ("ARCHIVED_POINT_IN_TIME","RECONSTRUCTED_NOT_ARCHIVED","RECONSTRUCTED_NOT_ARCHIVED"),
        ("ARCHIVED_POINT_IN_TIME","UNKNOWN","UNKNOWN"),
        ("ARCHIVED_POINT_IN_TIME","NOT_APPLICABLE","ARCHIVED_POINT_IN_TIME"),
        ("RECONSTRUCTED_NOT_ARCHIVED","UNKNOWN","UNKNOWN"),
        ("RECONSTRUCTED_NOT_ARCHIVED","NOT_APPLICABLE","RECONSTRUCTED_NOT_ARCHIVED"),
        ("UNKNOWN","NOT_APPLICABLE","UNKNOWN"),
        ("NOT_APPLICABLE","NOT_APPLICABLE","NOT_APPLICABLE"),
    ],
)
def test_pit_algebra(left, right, result):
    from trinity.ledger.storage import _fold_pit_classes
    assert _fold_pit_classes([left, right]) == result
    assert _fold_pit_classes([right, left, left]) == result


def test_realistic_reconstruction_chain_and_v2_manifest(storage):
    attempt = _attempt(storage, "RECONSTRUCTION", "chain")
    policy = _policy(storage)
    a_art, a_obs, raw_a = _raw(storage, attempt, "a", T1)
    a_class = _classify_raw(storage, attempt, policy, a_art, a_obs, raw_a, reference=T1)
    fact_art = storage.insert_json_artifact(artifact_type="normalized.a.v1", value={"a":1})
    fact = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=fact_art.artifact_id,
        parents=(DerivationParent(raw_a.derivation_node_id, "a"),),
    )
    fact_class = storage.create_derived_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=fact.derivation_node_id, pit_reference_at=T1,
        classification_policy_id=policy.classification_policy_id,
        parent_classification_ids=(a_class.derivation_node_classification_id,),
    )
    b_art, b_obs, raw_b = _raw(storage, attempt, "b", T2)
    proof = storage.insert_json_artifact(artifact_type="reconstruction.proof.v1", value={"b":1})
    b_class = _classify_raw(storage, attempt, policy, b_art, b_obs, raw_b,
                            reference=T1, kind="POST_REFERENCE_RECONSTRUCTION",
                            support=(proof.artifact_id,))
    terminal_art = storage.insert_json_artifact(artifact_type="normalized.terminal.v1", value={"done":True})
    terminal = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=terminal_art.artifact_id,
        parents=(DerivationParent(fact.derivation_node_id,"a"), DerivationParent(raw_b.derivation_node_id,"b")),
    )
    terminal_class = storage.create_derived_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=terminal.derivation_node_id, pit_reference_at=T1,
        classification_policy_id=policy.classification_policy_id,
        parent_classification_ids=(fact_class.derivation_node_classification_id,b_class.derivation_node_classification_id),
    )
    assert terminal_class.pit_class == "RECONSTRUCTED_NOT_ARCHIVED"
    bindings = (
        RunInputBinding(a_obs.input_observation_id,"A",raw_a.derivation_node_id),
        RunInputBinding(b_obs.input_observation_id,"B",raw_b.derivation_node_id),
    )
    ids = tuple(c.derivation_node_classification_id for c in (a_class,fact_class,b_class,terminal_class))
    run = storage.commit_run(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_ids=(terminal.derivation_node_id,), run_inputs=bindings,
        derivation_node_classification_ids=ids,
    )
    manifest = json.loads(storage.get_artifact(run.result_manifest_artifact_id).payload)
    assert manifest["manifest_version"] == "2"
    assert manifest["resolved_pit_class"] == "RECONSTRUCTED_NOT_ARCHIVED"
    assert manifest["derivation_node_classification_ids"] == sorted(ids)
    assert storage.get_run_classification_status(run.run_id) == "CLASSIFIED"


def test_all_archived_native_chain_commits_pit_safe_and_replays(storage):
    attempt = _attempt(storage, "PIT_SAFE_DECISION", "archived")
    policy = _policy(storage)
    artifact, observation, raw = _raw(storage, attempt, "archived", T1)
    raw_class = _classify_raw(storage, attempt, policy, artifact, observation, raw)
    fact_artifact = storage.insert_json_artifact(
        artifact_type="normalized.archived.v1", value={"archived": True}
    )
    fact = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=fact_artifact.artifact_id,
        parents=(DerivationParent(raw.derivation_node_id, "content"),),
    )
    fact_class = storage.create_derived_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=fact.derivation_node_id, pit_reference_at=T3,
        classification_policy_id=policy.classification_policy_id,
        parent_classification_ids=(raw_class.derivation_node_classification_id,),
    )
    kwargs = dict(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        run_id="classified-archived-run",
        derivation_node_ids=(fact.derivation_node_id,),
        run_inputs=(RunInputBinding(observation.input_observation_id,"PRIMARY",raw.derivation_node_id),),
        derivation_node_classification_ids=(fact_class.derivation_node_classification_id,raw_class.derivation_node_classification_id),
    )
    run = storage.commit_run(**kwargs)
    assert storage.commit_run(**kwargs) == run
    manifest = json.loads(storage.get_artifact(run.result_manifest_artifact_id).payload)
    assert (manifest["resolved_record_class"], manifest["resolved_pit_class"]) == (
        "LEDGER_NATIVE", "ARCHIVED_POINT_IN_TIME"
    )


def test_optional_legacy_unknown_parent_is_excluded(storage):
    attempt = _attempt(storage, "PIT_SAFE_DECISION", "optional")
    policy = _policy(storage)
    a_art, a_obs, raw_a = _raw(storage, attempt, "required", T1)
    a_class = _classify_raw(storage, attempt, policy, a_art, a_obs, raw_a)
    b_art, b_obs, raw_b = _raw(storage, attempt, "optional", T1)
    b_class = _classify_raw(
        storage, attempt, policy, b_art, b_obs, raw_b,
        kind="INSUFFICIENT_PIT_EVIDENCE", origin="LEGACY_IMPORT"
    )
    fact_artifact = storage.insert_json_artifact(
        artifact_type="normalized.optional.v1", value={"optional": True}
    )
    fact = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=fact_artifact.artifact_id,
        parents=(DerivationParent(raw_a.derivation_node_id,"required",True), DerivationParent(raw_b.derivation_node_id,"context",False)),
    )
    result = storage.create_derived_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=fact.derivation_node_id, pit_reference_at=T3,
        classification_policy_id=policy.classification_policy_id,
        parent_classification_ids=(a_class.derivation_node_classification_id,),
    )
    assert (result.pit_class, result.resolved_record_class) == (
        "ARCHIVED_POINT_IN_TIME", "LEDGER_NATIVE"
    )
    with pytest.raises(ClassificationIntegrityError, match="exactly cover"):
        storage.create_derived_node_classification(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            derivation_node_id=fact.derivation_node_id, pit_reference_at=T3,
            classification_policy_id=policy.classification_policy_id,
            parent_classification_ids=(a_class.derivation_node_classification_id,b_class.derivation_node_classification_id),
        )


def test_invalid_evidence_is_rejected_without_partial_classification(storage):
    attempt = _attempt(storage, "PIT_SAFE_DECISION", "invalid-evidence")
    policy = _policy(storage)
    artifact, observation, raw = _raw(storage, attempt, "invalid", T1)
    evidence = _evidence(storage, attempt, artifact, observation, raw, reference=T3)
    value = json.loads(evidence.payload)
    value["extra"] = "forbidden"
    malformed = storage.insert_json_artifact(
        artifact_type="ledger.pit-classification-evidence.v1", value=value
    )
    storage.attach_attempt_artifact(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=malformed.artifact_id, role="DIAGNOSTIC",
    )
    with pytest.raises(ClassificationIntegrityError, match="closed"):
        storage.create_raw_node_classification(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            derivation_node_id=raw.derivation_node_id, pit_reference_at=T3,
            classification_policy_id=policy.classification_policy_id,
            evidence_artifact_id=malformed.artifact_id,
        )
    assert storage.connection.execute(
        "SELECT count(*) FROM derivation_node_classification"
    ).fetchone()[0] == 0


def test_pit_safe_rejects_unknown_and_v1_stays_unclassified(storage):
    attempt = _attempt(storage, "PIT_SAFE_DECISION", "unknown")
    policy = _policy(storage)
    artifact, observation, raw = _raw(storage, attempt, "unknown", T1)
    classification = _classify_raw(storage, attempt, policy, artifact, observation, raw,
                                   kind="INSUFFICIENT_PIT_EVIDENCE")
    with pytest.raises(FinalizationConflict, match="forbidden"):
        storage.commit_run(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            derivation_node_ids=(raw.derivation_node_id,),
            run_inputs=(RunInputBinding(observation.input_observation_id,"PRIMARY",raw.derivation_node_id),),
            derivation_node_classification_ids=(classification.derivation_node_classification_id,),
        )
    old_attempt = _attempt(storage, "LEGACY_TEMPORAL_KIND", "v1")
    old_run = storage.commit_run(attempt_id=old_attempt.attempt_id, fence_token=old_attempt.fence_token)
    assert storage.get_run_classification_status(old_run.run_id) == "UNCLASSIFIED"


def test_supersession_authority_and_immutability(storage):
    attempt = _attempt(storage, "PIT_SAFE_DECISION", "supersede")
    policy = _policy(storage)
    artifact, observation, raw = _raw(storage, attempt, "supersede", T1)
    first = _classify_raw(storage, attempt, policy, artifact, observation, raw)
    second = _classify_raw(storage, attempt, policy, artifact, observation, raw,
                           supersedes=first.derivation_node_classification_id)
    assert second.classification_version == 2
    assert storage.get_current_node_classification(raw.derivation_node_id,T3) == second
    with pytest.raises(ClassificationIntegrityError):
        _classify_raw(storage, attempt, policy, artifact, observation, raw,
                      supersedes=first.derivation_node_classification_id)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        storage.connection.execute("DELETE FROM derivation_node_classification WHERE derivation_node_classification_id=?",(first.derivation_node_classification_id,))
    with pytest.raises(StaleAttemptError):
        storage.create_raw_node_classification(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token+1,
            derivation_node_id=raw.derivation_node_id, pit_reference_at=T3,
            classification_policy_id=policy.classification_policy_id,
            evidence_artifact_id=storage.get_node_classification(second.derivation_node_classification_id).evidence_artifact_id,
            supersedes_classification_id=second.derivation_node_classification_id,
        )
