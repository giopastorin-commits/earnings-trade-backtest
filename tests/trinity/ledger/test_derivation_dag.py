from __future__ import annotations

import json
import sqlite3

import pytest

from trinity.ledger import (
    DerivationIntegrityError,
    DerivationParent,
    FinalizationConflict,
    LedgerStorage,
    RunInputBinding,
    StaleAttemptError,
    UnsupportedDerivationPolicy,
    UnsupportedRunInput,
)

T1 = "2026-09-29T09:00:00.000000Z"
T2 = "2026-09-29T10:00:00.000000Z"
T3 = "2026-09-29T11:00:00.000000Z"
AFTER_T3 = "2026-09-29T12:00:00.000000Z"


@pytest.fixture
def storage(tmp_path):
    with LedgerStorage.open(tmp_path / "derivation.sqlite3") as opened:
        yield opened


def _attempt(storage: LedgerStorage, *, cutoff: str = AFTER_T3, suffix: str = "a"):
    request = storage.create_run_request(
        request_kind="DERIVATION_TEST_V1",
        analysis_cutoff_at=cutoff,
        parameters={"suffix": suffix},
        idempotency_key=f"derivation-{suffix}",
        requested_by="derivation-tests",
        baseline_commit="abc123",
    )
    return storage.allocate_attempt(
        run_request_id=request.run_request_id,
        worker_identity=f"worker-{suffix}",
        code_commit="def456",
        environment_fingerprint="env:v1",
    )


def _observation(storage: LedgerStorage, attempt, *, record: str, available_at: str):
    artifact = storage.insert_opaque_artifact(
        artifact_type=f"source.{record}.v1",
        payload=f"source-payload-{record}".encode(),
        media_type="application/octet-stream",
    )
    observation = storage.create_input_observation(
        artifact_id=artifact.artifact_id,
        source_id="test:derivation:v1",
        source_record_key=record,
        source_published_at=None,
        retrieved_at=available_at,
        availability_basis="RETRIEVED_AT_FALLBACK",
        effective_available_at=available_at,
        observed_by_attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        source_metadata={
            "schema_version": "1",
            "provider": "test",
            "dataset_name": "derivation",
            "source_record_key": record,
            "acquisition_method": "fixture",
            "availability_rule_id": "retrieval-fallback",
            "availability_rule_version": "1",
            "evidence_artifact_ids": [],
            "provider_metadata": {"record": record},
            "future_effective_at": None,
        },
    )
    return artifact, observation


def _raw(storage: LedgerStorage, attempt, *, record: str, available_at: str):
    artifact, observation = _observation(
        storage, attempt, record=record, available_at=available_at
    )
    node = storage.create_raw_derivation_node(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        input_observation_id=observation.input_observation_id,
    )
    return artifact, observation, node


def _derived(storage: LedgerStorage, attempt, *, name: str, parents):
    artifact = storage.insert_json_artifact(
        artifact_type=f"normalized.{name}.v1", value={"name": name}
    )
    node = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        artifact_id=artifact.artifact_id,
        parents=tuple(parents),
    )
    return artifact, node


def _binding(observation, raw, role="PRIMARY"):
    return RunInputBinding(
        input_observation_id=observation.input_observation_id,
        input_role=role,
        derivation_node_id=raw.derivation_node_id,
    )


def test_raw_node_mapping_availability_artifact_traversal_and_shape(storage):
    attempt = _attempt(storage)
    artifact, observation, raw = _raw(
        storage, attempt, record="raw", available_at=T1
    )
    assert raw.node_kind == "INPUT_OBSERVATION"
    assert raw.entity_type == "input_observation"
    assert raw.entity_id == observation.input_observation_id
    assert raw.run_id is None
    assert raw.direct_available_at == T1
    assert raw.derived_available_at == T1
    assert storage.list_derivation_edges(raw.derivation_node_id) == []
    assert "artifact_id" not in raw.__dataclass_fields__
    lineage = storage.get_raw_artifact_lineage(raw.derivation_node_id)
    assert len(lineage) == 1
    assert lineage[0].observation == observation
    assert lineage[0].artifact == artifact
    assert storage.verify_derivation(raw.derivation_node_id) == T1


def test_raw_rejects_missing_observation_unsupported_policy_and_timestamp_override(storage):
    attempt = _attempt(storage)
    with pytest.raises(KeyError):
        storage.create_raw_derivation_node(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            input_observation_id="missing-observation",
        )
    with pytest.raises(UnsupportedDerivationPolicy):
        storage.create_raw_derivation_node(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            input_observation_id="missing-observation",
            derivation_policy_version="FAVORABLE_BACKDATE_V1",
        )
    _, observation = _observation(storage, attempt, record="override", available_at=T1)
    with pytest.raises(TypeError):
        storage.create_raw_derivation_node(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            input_observation_id=observation.input_observation_id,
            derived_available_at="2026-09-28T00:00:00.000000Z",
        )
    assert storage.connection.execute("SELECT count(*) FROM derivation_node").fetchone()[0] == 0


def test_reserved_and_invalid_node_mappings_are_not_instantiable(storage):
    attempt = _attempt(storage, suffix="reserved")
    with storage._internal_write(), pytest.raises(
        sqlite3.IntegrityError, match="reserved derivation node kind"
    ):
        storage.connection.execute(
            """
            INSERT INTO derivation_node VALUES (
                'reserved-node', NULL, ?, 'RESEARCH', 'research_record',
                'future-research', NULL, ?, 'MAX_REQUIRED_PARENTS_V1'
            )
            """,
            (attempt.attempt_id, T1),
        )
    with storage._internal_write(), pytest.raises(sqlite3.IntegrityError):
        storage.connection.execute(
            """
            INSERT INTO derivation_node VALUES (
                'invalid-mapping', NULL, ?, 'INPUT_OBSERVATION', 'artifact',
                'not-an-observation', ?, ?, 'MAX_REQUIRED_PARENTS_V1'
            )
            """,
            (attempt.attempt_id, T1, T1),
        )


def test_required_parent_maximum_and_deterministic_edge_order(storage):
    attempt = _attempt(storage)
    _, _, raw1 = _raw(storage, attempt, record="t1", available_at=T1)
    _, _, raw2 = _raw(storage, attempt, record="t2", available_at=T2)
    _, _, raw3 = _raw(storage, attempt, record="t3", available_at=T3)
    _, child = _derived(
        storage,
        attempt,
        name="max",
        parents=(
            DerivationParent(raw3.derivation_node_id, "z-role"),
            DerivationParent(raw1.derivation_node_id, "a-role"),
            DerivationParent(raw2.derivation_node_id, "a-role"),
        ),
    )
    assert child.derived_available_at == T3
    edges = storage.list_derivation_edges(child.derivation_node_id)
    assert [(edge.edge_role, edge.parent_node_id) for edge in edges] == sorted(
        (edge.edge_role, edge.parent_node_id) for edge in edges
    )
    assert storage.verify_derivation(child.derivation_node_id) == T3
    assert storage.verify_derivations(
        (child.derivation_node_id, raw2.derivation_node_id)
    ) == {
        child.derivation_node_id: T3,
        raw2.derivation_node_id: T2,
    }


def test_optional_parent_is_provenance_only(storage):
    attempt = _attempt(storage)
    _, required_observation, required_raw = _raw(
        storage, attempt, record="required", available_at=T1
    )
    _, optional_observation, optional_raw = _raw(
        storage, attempt, record="optional", available_at=T3
    )
    _, child = _derived(
        storage,
        attempt,
        name="optional",
        parents=(
            DerivationParent(required_raw.derivation_node_id, "content", True),
            DerivationParent(optional_raw.derivation_node_id, "context", False),
        ),
    )
    assert child.derived_available_at == T1
    assert [node.derivation_node_id for node in storage.get_required_ancestry(child.derivation_node_id)] == [
        required_raw.derivation_node_id
    ]
    assert {node.derivation_node_id for node in storage.get_full_provenance_ancestry(child.derivation_node_id)} == {
        required_raw.derivation_node_id,
        optional_raw.derivation_node_id,
    }
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        derivation_node_ids=(child.derivation_node_id,),
        run_inputs=(_binding(required_observation, required_raw),),
    )
    rows = storage.connection.execute(
        "SELECT input_observation_id FROM run_input WHERE run_id = ?", (run.run_id,)
    ).fetchall()
    assert [row[0] for row in rows] == [required_observation.input_observation_id]
    assert optional_observation.input_observation_id not in {row[0] for row in rows}
    assert storage.get_derivation_node(optional_raw.derivation_node_id).run_id == run.run_id


def test_ten_level_chain_and_late_ancestor_propagation(storage):
    attempt = _attempt(storage)
    _, _, raw = _raw(storage, attempt, record="chain", available_at=T1)
    current = raw
    for level in range(10):
        _, current = _derived(
            storage,
            attempt,
            name=f"chain-{level}",
            parents=(DerivationParent(current.derivation_node_id, "prior"),),
        )
    _, _, late = _raw(storage, attempt, record="late", available_at=T3)
    _, terminal = _derived(
        storage,
        attempt,
        name="chain-terminal",
        parents=(
            DerivationParent(current.derivation_node_id, "chain"),
            DerivationParent(late.derivation_node_id, "late"),
        ),
    )
    assert current.derived_available_at == T1
    assert terminal.derived_available_at == T3
    assert storage.verify_derivation(terminal.derivation_node_id) == T3
    assert len(storage.get_required_ancestry(terminal.derivation_node_id)) == 12


def test_derived_requires_parent_target_and_atomic_success(storage):
    attempt = _attempt(storage)
    artifact = storage.insert_json_artifact(artifact_type="normalized.empty.v1", value={})
    with pytest.raises(DerivationIntegrityError, match="required parent"):
        storage.create_normalized_fact_node(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            artifact_id=artifact.artifact_id,
            parents=(),
        )
    with pytest.raises(KeyError):
        storage.create_normalized_fact_node(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            artifact_id="sha256:" + "0" * 64,
            parents=(DerivationParent("missing", "content"),),
        )
    assert storage.connection.execute("SELECT count(*) FROM derivation_edge").fetchone()[0] == 0


def test_invalid_parent_and_self_edge_leave_no_partial_graph(storage):
    attempt = _attempt(storage)
    _, _, raw = _raw(storage, attempt, record="atomic", available_at=T1)
    artifact = storage.insert_json_artifact(artifact_type="normalized.atomic.v1", value={})
    before = storage.connection.execute("SELECT count(*) FROM derivation_node").fetchone()[0]
    with pytest.raises(DerivationIntegrityError):
        storage.create_normalized_fact_node(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            artifact_id=artifact.artifact_id,
            parents=(DerivationParent("missing-parent", "content"),),
        )
    with pytest.raises(DerivationIntegrityError, match="self-edge"):
        storage.create_normalized_fact_node(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            artifact_id=artifact.artifact_id,
            parents=(DerivationParent(raw.derivation_node_id, "content"),),
            derivation_node_id=raw.derivation_node_id,
        )
    assert storage.connection.execute("SELECT count(*) FROM derivation_node").fetchone()[0] == before
    assert storage.connection.execute("SELECT count(*) FROM derivation_edge").fetchone()[0] == 0


def test_post_construction_edge_and_cross_attempt_parent_are_rejected(storage):
    first = _attempt(storage, suffix="first")
    _, _, first_raw = _raw(storage, first, record="first", available_at=T1)
    second = _attempt(storage, suffix="second")
    artifact = storage.insert_json_artifact(artifact_type="normalized.cross.v1", value={})
    with pytest.raises(DerivationIntegrityError, match="attempt-local lineage"):
        storage.create_normalized_fact_node(
            attempt_id=second.attempt_id,
            fence_token=second.fence_token,
            artifact_id=artifact.artifact_id,
            parents=(DerivationParent(first_raw.derivation_node_id, "foreign"),),
        )
    _, _, second_raw = _raw(storage, second, record="second", available_at=T1)
    _, child = _derived(
        storage,
        second,
        name="constructed",
        parents=(DerivationParent(second_raw.derivation_node_id, "content"),),
    )
    with storage._internal_write(), pytest.raises(
        sqlite3.IntegrityError, match="after child construction"
    ):
        storage.connection.execute(
            """
            INSERT INTO derivation_edge VALUES ('late-edge', ?, ?, 'late', 0)
            """,
            (second_raw.derivation_node_id, child.derivation_node_id),
        )


def test_verifier_detects_timestamp_policy_incomplete_and_cycle_corruption(storage):
    attempt = _attempt(storage)
    _, _, raw = _raw(storage, attempt, record="corrupt", available_at=T1)
    _, child = _derived(
        storage,
        attempt,
        name="corrupt",
        parents=(DerivationParent(raw.derivation_node_id, "content"),),
    )
    storage.connection.execute("DROP TRIGGER derivation_node_update_authorized")
    storage.connection.execute("DROP TRIGGER derivation_node_update_valid")
    storage.connection.execute("PRAGMA ignore_check_constraints = ON")
    storage.connection.execute(
        "UPDATE derivation_node SET derived_available_at = ? WHERE derivation_node_id = ?",
        (T2, child.derivation_node_id),
    )
    with pytest.raises(DerivationIntegrityError, match="differs"):
        storage.verify_derivation(child.derivation_node_id)
    storage.connection.execute(
        "UPDATE derivation_node SET derived_available_at = ?, derivation_policy_version = ? "
        "WHERE derivation_node_id = ?",
        (T1, "UNSUPPORTED", child.derivation_node_id),
    )
    with pytest.raises(UnsupportedDerivationPolicy):
        storage.verify_derivation(child.derivation_node_id)
    storage.connection.execute(
        "UPDATE derivation_node SET derivation_policy_version = ? WHERE derivation_node_id = ?",
        ("MAX_REQUIRED_PARENTS_V1", child.derivation_node_id),
    )
    storage.connection.execute("DROP TRIGGER derivation_edge_no_delete")
    with storage._internal_write():
        storage.connection.execute("DROP TRIGGER derivation_edge_insert_valid")
        storage.connection.execute(
            "INSERT INTO derivation_edge VALUES ('cycle-edge', ?, ?, 'cycle', 1)",
            (child.derivation_node_id, raw.derivation_node_id),
        )
    with pytest.raises(DerivationIntegrityError, match="cycle"):
        storage.verify_derivation(child.derivation_node_id)
    storage.connection.execute(
        "DELETE FROM derivation_edge WHERE derivation_edge_id = 'cycle-edge'"
    )
    storage.connection.execute(
        "DELETE FROM derivation_edge WHERE child_node_id = ?", (child.derivation_node_id,)
    )
    with pytest.raises(DerivationIntegrityError, match="required parent"):
        storage.verify_derivation(child.derivation_node_id)


def test_nodes_edges_immutable_and_run_binding_once(storage):
    attempt = _attempt(storage)
    _, observation, raw = _raw(storage, attempt, record="immutable", available_at=T1)
    _, child = _derived(
        storage,
        attempt,
        name="immutable",
        parents=(DerivationParent(raw.derivation_node_id, "content"),),
    )
    edge = storage.list_derivation_edges(child.derivation_node_id)[0]
    for statement, parameters in (
        ("UPDATE derivation_node SET entity_id = entity_id WHERE derivation_node_id = ?", (raw.derivation_node_id,)),
        ("DELETE FROM derivation_node WHERE derivation_node_id = ?", (raw.derivation_node_id,)),
        ("UPDATE derivation_edge SET edge_role = edge_role WHERE derivation_edge_id = ?", (edge.derivation_edge_id,)),
        ("DELETE FROM derivation_edge WHERE derivation_edge_id = ?", (edge.derivation_edge_id,)),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
            storage.connection.execute(statement, parameters)
    foreign_attempt = _attempt(storage, suffix="foreign-run")
    foreign_run = storage.commit_run(
        attempt_id=foreign_attempt.attempt_id,
        fence_token=foreign_attempt.fence_token,
    )
    with storage._run_binding(), pytest.raises(
        sqlite3.IntegrityError, match="invalid derivation run binding"
    ):
        storage.connection.execute(
            "UPDATE derivation_node SET run_id = ? WHERE derivation_node_id = ?",
            (foreign_run.run_id, raw.derivation_node_id),
        )
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        derivation_node_ids=(child.derivation_node_id,),
        run_inputs=(_binding(observation, raw),),
    )
    assert storage.get_derivation_node(raw.derivation_node_id).run_id == run.run_id
    assert storage.get_derivation_node(child.derivation_node_id).run_id == run.run_id
    with storage._run_binding(), pytest.raises(sqlite3.IntegrityError, match="invalid derivation"):
        storage.connection.execute(
            "UPDATE derivation_node SET run_id = run_id WHERE derivation_node_id = ?",
            (child.derivation_node_id,),
        )


def test_authority_stale_forged_and_historical_graph(storage):
    request_attempt = _attempt(storage, suffix="authority")
    _, observation = _observation(
        storage, request_attempt, record="authority", available_at=T1
    )
    with pytest.raises(StaleAttemptError):
        storage.create_raw_derivation_node(
            attempt_id=request_attempt.attempt_id,
            fence_token=request_attempt.fence_token + 1,
            input_observation_id=observation.input_observation_id,
        )
    raw = storage.create_raw_derivation_node(
        attempt_id=request_attempt.attempt_id,
        fence_token=request_attempt.fence_token,
        input_observation_id=observation.input_observation_id,
    )
    replacement = storage.allocate_attempt(
        run_request_id=request_attempt.run_request_id,
        worker_identity="replacement",
        code_commit="def456",
        environment_fingerprint="env:v1",
    )
    with pytest.raises(StaleAttemptError):
        storage.create_raw_derivation_node(
            attempt_id=request_attempt.attempt_id,
            fence_token=request_attempt.fence_token,
            input_observation_id=observation.input_observation_id,
        )
    assert storage.get_derivation_node(raw.derivation_node_id) == raw
    reused = storage.create_raw_derivation_node(
        attempt_id=replacement.attempt_id,
        fence_token=replacement.fence_token,
        input_observation_id=observation.input_observation_id,
    )
    assert reused.attempt_id == replacement.attempt_id
    assert reused.entity_id == observation.input_observation_id


@pytest.mark.parametrize("cutoff", [T3, AFTER_T3])
def test_cutoff_at_or_after_required_availability_succeeds(storage, cutoff):
    attempt = _attempt(storage, cutoff=cutoff, suffix=f"cutoff-{cutoff[11:13]}")
    _, observation, raw = _raw(storage, attempt, record=f"cutoff-{cutoff}", available_at=T3)
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        derivation_node_ids=(raw.derivation_node_id,),
        run_inputs=(_binding(observation, raw),),
    )
    assert run.status == "COMMITTED"


def test_late_required_ancestor_and_missing_run_input_block_commit(storage):
    late_attempt = _attempt(storage, cutoff=T2, suffix="late-cutoff")
    _, late_observation, late_raw = _raw(
        storage, late_attempt, record="late-cutoff", available_at=T3
    )
    _, child = _derived(
        storage,
        late_attempt,
        name="late-cutoff",
        parents=(DerivationParent(late_raw.derivation_node_id, "content"),),
    )
    with pytest.raises(FinalizationConflict, match="cutoff"):
        storage.commit_run(
            attempt_id=late_attempt.attempt_id,
            fence_token=late_attempt.fence_token,
            derivation_node_ids=(child.derivation_node_id,),
            run_inputs=(_binding(late_observation, late_raw),),
        )
    assert storage.get_derivation_node(child.derivation_node_id).run_id is None

    normal = _attempt(storage, suffix="missing-input")
    _, _, raw = _raw(storage, normal, record="missing-input", available_at=T1)
    with pytest.raises(UnsupportedRunInput, match="coverage"):
        storage.commit_run(
            attempt_id=normal.attempt_id,
            fence_token=normal.fence_token,
            derivation_node_ids=(raw.derivation_node_id,),
        )


def test_unrelated_or_optional_run_input_is_rejected_and_node_not_adopted(storage):
    attempt = _attempt(storage, suffix="unrelated")
    _, used_observation, used_raw = _raw(storage, attempt, record="used", available_at=T1)
    _, other_observation, other_raw = _raw(storage, attempt, record="other", available_at=T1)
    with pytest.raises(UnsupportedRunInput, match="required ancestry"):
        storage.commit_run(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            derivation_node_ids=(used_raw.derivation_node_id,),
            run_inputs=(
                _binding(used_observation, used_raw),
                _binding(other_observation, other_raw),
            ),
        )
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        derivation_node_ids=(used_raw.derivation_node_id,),
        run_inputs=(_binding(used_observation, used_raw),),
    )
    assert storage.get_derivation_node(used_raw.derivation_node_id).run_id == run.run_id
    assert storage.get_derivation_node(other_raw.derivation_node_id).run_id is None


def test_realistic_chain_manifest_run_input_and_idempotency(storage):
    attempt = _attempt(storage, cutoff=T2, suffix="realistic")
    artifact_a, observation_a, raw_a = _raw(
        storage, attempt, record="real-a", available_at=T1
    )
    _, fact_a = _derived(
        storage,
        attempt,
        name="real-fact-a",
        parents=(DerivationParent(raw_a.derivation_node_id, "source-a"),),
    )
    artifact_b, observation_b, raw_b = _raw(
        storage, attempt, record="real-b", available_at=T2
    )
    terminal_artifact, terminal = _derived(
        storage,
        attempt,
        name="real-terminal",
        parents=(
            DerivationParent(fact_a.derivation_node_id, "branch-a"),
            DerivationParent(raw_b.derivation_node_id, "branch-b"),
        ),
    )
    assert terminal.derived_available_at == T2
    assert storage.verify_derivation(terminal.derivation_node_id) == T2
    assert {item.artifact.artifact_id for item in storage.get_raw_artifact_lineage(terminal.derivation_node_id, required_only=True)} == {
        artifact_a.artifact_id,
        artifact_b.artifact_id,
    }
    required_ids = {
        node.derivation_node_id
        for node in storage.get_required_ancestry(terminal.derivation_node_id)
    }
    assert required_ids == {
        raw_a.derivation_node_id,
        fact_a.derivation_node_id,
        raw_b.derivation_node_id,
    }
    bindings = (_binding(observation_a, raw_a, "SOURCE_A"), _binding(observation_b, raw_b, "SOURCE_B"))
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        run_id="realistic-run",
        derivation_node_ids=(terminal.derivation_node_id,),
        run_inputs=bindings,
    )
    manifest = json.loads(storage.get_artifact(run.result_manifest_artifact_id).payload)
    assert manifest["derivation_node_ids"] == sorted(
        {
            raw_a.derivation_node_id,
            fact_a.derivation_node_id,
            raw_b.derivation_node_id,
            terminal.derivation_node_id,
        }
    )
    assert manifest["input_observation_ids"] == sorted(
        [observation_a.input_observation_id, observation_b.input_observation_id]
    )
    assert manifest["policy_references"] == [
        {
            "policy_kind": "DERIVATION",
            "policy_version": "MAX_REQUIRED_PARENTS_V1",
        }
    ]
    replay = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        run_id="realistic-run",
        derivation_node_ids=(terminal.derivation_node_id,),
        run_inputs=bindings,
    )
    assert replay == run
    incompatible = tuple(reversed(bindings[:-1]))
    with pytest.raises((FinalizationConflict, UnsupportedRunInput)):
        storage.commit_run(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            run_id="realistic-run",
            derivation_node_ids=(terminal.derivation_node_id,),
            run_inputs=incompatible,
        )
    assert terminal_artifact.artifact_id == storage.get_derivation_node(terminal.derivation_node_id).entity_id


def test_realistic_chain_cannot_commit_before_late_ancestor(storage):
    attempt = _attempt(storage, cutoff=T1, suffix="realistic-late")
    _, observation_a, raw_a = _raw(storage, attempt, record="late-a", available_at=T1)
    _, observation_b, raw_b = _raw(storage, attempt, record="late-b", available_at=T2)
    _, terminal = _derived(
        storage,
        attempt,
        name="late-terminal",
        parents=(
            DerivationParent(raw_a.derivation_node_id, "a"),
            DerivationParent(raw_b.derivation_node_id, "b"),
        ),
    )
    with pytest.raises(FinalizationConflict, match="cutoff"):
        storage.commit_run(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            derivation_node_ids=(terminal.derivation_node_id,),
            run_inputs=(_binding(observation_a, raw_a), _binding(observation_b, raw_b)),
        )
    assert storage.connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0
