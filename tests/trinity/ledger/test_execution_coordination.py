from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from trinity.ledger import (
    ArtifactIntegrityError,
    FinalizationConflict,
    InvalidAttemptTransition,
    LedgerStorage,
    RequestIdempotencyConflict,
    StaleAttemptError,
)

CUTOFF = "2026-09-29T12:00:00.000000Z"


@pytest.fixture
def storage(tmp_path):
    with LedgerStorage.open(tmp_path / "ledger.sqlite3") as opened:
        yield opened


def _request(storage: LedgerStorage, *, caller: str = "test-suite", key: str = "request-1"):
    return storage.create_run_request(
        request_kind="TEST_COORDINATION_V1",
        analysis_cutoff_at=CUTOFF,
        parameters={"symbols": ["AAPL", "MSFT"], "mode": "test"},
        idempotency_key=key,
        requested_by=caller,
        baseline_commit="abc123",
    )


def _attempt(storage: LedgerStorage, request_id: str, worker: str = "worker-1"):
    return storage.allocate_attempt(
        run_request_id=request_id,
        worker_identity=worker,
        code_commit="def456",
        environment_fingerprint="env:v1",
    )


def test_request_create_read_replay_scope_conflict_and_immutability(storage):
    first = _request(storage)
    assert storage.get_run_request(first.run_request_id) == first

    replay = storage.create_run_request(
        request_kind="TEST_COORDINATION_V1",
        analysis_cutoff_at=CUTOFF,
        parameters={"mode": "test", "symbols": ["AAPL", "MSFT"]},
        idempotency_key="request-1",
        requested_by="test-suite",
        baseline_commit="abc123",
    )
    assert replay == first

    with pytest.raises(RequestIdempotencyConflict):
        storage.create_run_request(
            request_kind="TEST_COORDINATION_V1",
            analysis_cutoff_at=CUTOFF,
            parameters={"mode": "different"},
            idempotency_key="request-1",
            requested_by="test-suite",
            baseline_commit="abc123",
        )

    other_scope = _request(storage, caller="other-caller")
    assert other_scope.run_request_id != first.run_request_id
    assert other_scope.idempotency_key == first.idempotency_key

    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "UPDATE run_request SET request_kind = request_kind WHERE run_request_id = ?",
            (first.run_request_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "DELETE FROM run_request WHERE run_request_id = ?", (first.run_request_id,)
        )


def test_control_has_valid_run_fk_and_begins_active_without_commit(storage):
    request = _request(storage)
    foreign_keys = {
        row[3]: row[2]
        for row in storage.connection.execute("PRAGMA foreign_key_list(run_request_control)")
    }
    assert foreign_keys["committed_run_id"] == "run"
    control = storage.connection.execute(
        "SELECT * FROM run_request_control WHERE run_request_id = ?",
        (request.run_request_id,),
    ).fetchone()
    assert control["request_state"] == "ACTIVE"
    assert control["committed_run_id"] is None
    assert control["current_attempt_id"] is None
    assert control["current_fence_token"] == 0


def test_attempt_allocation_retry_fencing_and_history(storage):
    request = _request(storage)
    first = _attempt(storage, request.run_request_id)
    assert first.attempt_ordinal == 1
    assert first.fence_token == 1
    assert first.status == "RUNNING"
    assert storage.is_attempt_authorized(first.attempt_id, first.fence_token)

    second = _attempt(storage, request.run_request_id, worker="worker-2")
    assert second.attempt_ordinal == 2
    assert second.fence_token == 2
    assert second.attempt_id != first.attempt_id
    assert storage.get_attempt(first.attempt_id).status == "ABORTED"
    assert not storage.is_attempt_authorized(first.attempt_id, first.fence_token)
    assert storage.is_attempt_authorized(second.attempt_id, second.fence_token)

    first_events = storage.list_attempt_events(first.attempt_id)
    assert [event.sequence_number for event in first_events] == [1, 2, 3]
    assert [event.event_type for event in first_events] == [
        "ALLOCATED",
        "AUTHORITY_REVOKED",
        "ABORTED",
    ]
    assert first_events[-1].reason_code == "SUPERSEDED_BY_RETRY"
    assert storage.get_attempt(first.attempt_id).attempt_ordinal == 1


def test_concurrent_allocation_serializes_ordinals_and_fences(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    with LedgerStorage.open(path) as initial:
        request_id = _request(initial).run_request_id

    def allocate(worker: str):
        with LedgerStorage.open(path) as opened:
            return opened.allocate_attempt(
                run_request_id=request_id,
                worker_identity=worker,
                code_commit="def456",
                environment_fingerprint="env:v1",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(allocate, ("worker-a", "worker-b")))
    assert sorted(item.attempt_ordinal for item in attempts) == [1, 2]
    assert sorted(item.fence_token for item in attempts) == [1, 2]
    assert len({item.attempt_id for item in attempts}) == 2


def test_stale_or_forged_authority_cannot_transition(storage):
    request = _request(storage)
    first = _attempt(storage, request.run_request_id)
    second = _attempt(storage, request.run_request_id, worker="worker-2")

    with pytest.raises(StaleAttemptError):
        storage.require_attempt_authority(first.attempt_id, first.fence_token)
    with pytest.raises(StaleAttemptError):
        storage.require_attempt_authority(second.attempt_id, second.fence_token + 100)
    with pytest.raises(InvalidAttemptTransition):
        storage.terminalize_attempt(
            attempt_id=first.attempt_id,
            fence_token=first.fence_token,
            status="FAILED",
            actor_identity="worker-1",
            failure_class="STALE",
        )


@pytest.mark.parametrize(
    ("status", "reason_code", "failure_class", "failure_message"),
    [
        ("FAILED", "WORKER_ERROR", "RuntimeError", "sanitized"),
        ("ABORTED", "OPERATOR_CANCELLED", None, None),
    ],
)
def test_independent_terminal_transitions_are_atomic_and_idempotent(
    storage, status, reason_code, failure_class, failure_message
):
    request = _request(storage)
    attempt = _attempt(storage, request.run_request_id)
    kwargs = dict(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        status=status,
        actor_identity="worker-1",
        reason_code=reason_code,
        failure_class=failure_class,
        failure_message=failure_message,
    )
    terminal = storage.terminalize_attempt(**kwargs)
    assert terminal.status == status
    assert terminal.finished_at >= terminal.started_at
    assert terminal.failure_class == failure_class
    event_count = len(storage.list_attempt_events(attempt.attempt_id))
    assert storage.terminalize_attempt(**kwargs) == terminal
    assert len(storage.list_attempt_events(attempt.attempt_id)) == event_count

    conflicting = dict(kwargs)
    conflicting["reason_code"] = "DIFFERENT"
    with pytest.raises(InvalidAttemptTransition):
        storage.terminalize_attempt(**conflicting)


def test_success_transition_cannot_be_invoked_outside_run_commit(storage):
    attempt = _attempt(storage, _request(storage).run_request_id)
    with pytest.raises(InvalidAttemptTransition, match="committed-run"):
        storage.terminalize_attempt(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            status="SUCCEEDED",
            actor_identity="worker-1",
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="checked projection|successful attempt requires committed run",
    ):
        storage.connection.execute(
            "UPDATE attempt SET status = 'SUCCEEDED', finished_at = ? WHERE attempt_id = ?",
            ("2026-09-29T13:00:00.000000Z", attempt.attempt_id),
        )


def test_event_history_is_append_only_and_projection_survives_retry(storage):
    request = _request(storage)
    first = _attempt(storage, request.run_request_id)
    _attempt(storage, request.run_request_id, worker="worker-2")
    events = storage.list_attempt_events(first.attempt_id)
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "UPDATE attempt_event SET event_at = event_at WHERE attempt_event_id = ?",
            (events[0].attempt_event_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "DELETE FROM attempt_event WHERE attempt_event_id = ?",
            (events[0].attempt_event_id,),
        )
    assert [event.event_type for event in storage.list_attempt_events(first.attempt_id)] == [
        "ALLOCATED",
        "AUTHORITY_REVOKED",
        "ABORTED",
    ]


def test_failed_attempt_artifact_link_is_preserved_and_immutable(storage):
    attempt = _attempt(storage, _request(storage).run_request_id)
    diagnostic = storage.insert_opaque_artifact(
        artifact_type="test.failure-diagnostic.v1",
        payload=b"sanitized diagnostic",
        media_type="text/plain",
    )
    link_id = storage.attach_attempt_artifact(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        artifact_id=diagnostic.artifact_id,
        role="FAILED_ATTEMPT_OUTPUT",
    )
    storage.terminalize_attempt(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        status="FAILED",
        actor_identity="worker-1",
        failure_class="RuntimeError",
    )
    assert storage.connection.execute(
        "SELECT artifact_id FROM attempt_artifact WHERE attempt_artifact_id = ?",
        (link_id,),
    ).fetchone()[0] == diagnostic.artifact_id
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "UPDATE attempt_artifact SET role = role WHERE attempt_artifact_id = ?",
            (link_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "DELETE FROM attempt_artifact WHERE attempt_artifact_id = ?", (link_id,)
        )


def test_failed_allocation_and_transition_roll_back_history_and_projection(storage):
    request = _request(storage)
    storage.connection.execute(
        """
        CREATE TRIGGER inject_allocation_failure BEFORE INSERT ON attempt
        BEGIN SELECT RAISE(ABORT, 'injected allocation failure'); END
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        _attempt(storage, request.run_request_id)
    control = storage.connection.execute(
        "SELECT * FROM run_request_control WHERE run_request_id = ?",
        (request.run_request_id,),
    ).fetchone()
    assert control["current_attempt_id"] is None
    assert control["current_fence_token"] == 0
    storage.connection.execute("DROP TRIGGER inject_allocation_failure")

    attempt = _attempt(storage, request.run_request_id)
    storage.connection.execute(
        """
        CREATE TRIGGER inject_transition_failure BEFORE UPDATE ON attempt
        BEGIN SELECT RAISE(ABORT, 'injected transition failure'); END
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        storage.terminalize_attempt(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            status="FAILED",
            actor_identity="worker-1",
            failure_class="RuntimeError",
        )
    assert storage.get_attempt(attempt.attempt_id).status == "RUNNING"
    assert [event.event_type for event in storage.list_attempt_events(attempt.attempt_id)] == [
        "ALLOCATED"
    ]


def test_authorized_attempt_commits_canonical_manifest_and_run_atomically(storage):
    request = _request(storage)
    attempt = _attempt(storage, request.run_request_id)
    output = storage.insert_json_artifact(
        artifact_type="test.output.v1", value={"result": "ok"}
    )
    storage.attach_attempt_artifact(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        artifact_id=output.artifact_id,
        role="OUTPUT",
    )
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        run_id="run-001",
        output_artifact_ids=(output.artifact_id,),
    )
    assert run.status == "COMMITTED"
    assert storage.get_attempt(attempt.attempt_id).status == "SUCCEEDED"
    control = storage.connection.execute(
        "SELECT * FROM run_request_control WHERE run_request_id = ?",
        (request.run_request_id,),
    ).fetchone()
    assert control["request_state"] == "COMMITTED"
    assert control["committed_run_id"] == run.run_id
    manifest = storage.get_artifact(run.result_manifest_artifact_id)
    value = json.loads(manifest.payload)
    assert manifest.artifact_kind == "ledger.run-result-manifest.v1"
    assert value["manifest_version"] == "1"
    assert value["output_artifact_ids"] == [output.artifact_id]
    assert value["input_observation_ids"] == []
    assert storage.connection.execute(
        "SELECT COUNT(*) FROM attempt_artifact WHERE attempt_id = ? AND role = 'RESULT_MANIFEST'",
        (attempt.attempt_id,),
    ).fetchone()[0] == 1
    assert [event.event_type for event in storage.list_attempt_events(attempt.attempt_id)] == [
        "ALLOCATED",
        "SUCCEEDED",
    ]


def test_duplicate_commit_is_idempotent_but_conflicting_finalization_fails(storage):
    attempt = _attempt(storage, _request(storage).run_request_id)
    first = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        run_id="run-stable",
    )
    replay = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        run_id="run-stable",
    )
    assert replay == first
    assert len(storage.list_attempt_events(attempt.attempt_id)) == 2
    with pytest.raises(FinalizationConflict):
        storage.commit_run(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            run_id="run-different",
        )
    with pytest.raises(InvalidAttemptTransition):
        _attempt(storage, attempt.run_request_id, worker="too-late")


def test_stale_and_failed_attempts_cannot_commit(storage):
    request = _request(storage)
    stale = _attempt(storage, request.run_request_id)
    current = _attempt(storage, request.run_request_id, worker="worker-2")
    with pytest.raises((StaleAttemptError, FinalizationConflict)):
        storage.commit_run(
            attempt_id=stale.attempt_id,
            fence_token=stale.fence_token,
            run_id="stale-run",
        )
    storage.terminalize_attempt(
        attempt_id=current.attempt_id,
        fence_token=current.fence_token,
        status="FAILED",
        actor_identity="worker-2",
        failure_class="RuntimeError",
    )
    with pytest.raises(StaleAttemptError):
        storage.commit_run(
            attempt_id=current.attempt_id,
            fence_token=current.fence_token,
            run_id="failed-run",
        )
    assert storage.connection.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0


def test_injected_commit_failure_leaves_no_success_visibility(storage):
    request = _request(storage)
    attempt = _attempt(storage, request.run_request_id)
    storage.connection.execute(
        """
        CREATE TRIGGER inject_commit_failure BEFORE UPDATE ON attempt
        WHEN NEW.status = 'SUCCEEDED'
        BEGIN SELECT RAISE(ABORT, 'injected commit failure'); END
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        storage.commit_run(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            run_id="rolled-back-run",
        )
    assert storage.connection.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0
    assert storage.get_attempt(attempt.attempt_id).status == "RUNNING"
    assert [event.event_type for event in storage.list_attempt_events(attempt.attempt_id)] == [
        "ALLOCATED"
    ]
    control = storage.connection.execute(
        "SELECT * FROM run_request_control WHERE run_request_id = ?",
        (request.run_request_id,),
    ).fetchone()
    assert control["committed_run_id"] is None
    assert storage.connection.execute(
        "SELECT COUNT(*) FROM attempt_artifact WHERE role = 'RESULT_MANIFEST'"
    ).fetchone()[0] == 0
    assert storage.connection.execute(
        "SELECT COUNT(*) FROM artifact WHERE artifact_kind = 'ledger.run-result-manifest.v1'"
    ).fetchone()[0] == 0


def test_manifest_tampering_is_detected(storage):
    attempt = _attempt(storage, _request(storage).run_request_id)
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        run_id="tamper-run",
    )
    storage.connection.execute("DROP TRIGGER artifact_no_update")
    storage.connection.execute(
        "UPDATE artifact SET payload = ?, byte_length = 2 WHERE artifact_id = ?",
        (b"{}", run.result_manifest_artifact_id),
    )
    with pytest.raises(ArtifactIntegrityError):
        storage.get_run(run.run_id)


def test_run_input_schema_is_exactly_guarded_and_immutable(storage):
    columns = {
        row[1]
        for row in storage.connection.execute("PRAGMA table_info(run_input)").fetchall()
    }
    assert columns == {
        "run_input_id",
        "run_id",
        "input_observation_id",
        "input_role",
        "derivation_node_id",
        "created_at",
    }
    triggers = {
        row[0]
        for row in storage.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'run_input'"
        )
    }
    assert {
        "run_input_insert_authorized",
        "run_input_insert_valid",
        "run_input_no_update",
        "run_input_no_delete",
    } <= triggers

    attempt = _attempt(storage, _request(storage).run_request_id)
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        run_id="run-with-test-input",
    )
    # Derivation remains deferred. A test-only node plus a minimally valid row
    # exercise the already-frozen run_input constraints and immutability.
    storage.connection.execute(
        """
        CREATE TABLE derivation_node (
            derivation_node_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_kind TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL
        ) STRICT
        """
    )
    with storage._internal_write():
        storage.connection.execute(
            """
            INSERT INTO input_observation (
                input_observation_id, artifact_id, source_id, source_record_key,
                source_published_at, retrieved_at, availability_basis,
                effective_available_at, observed_by_attempt_id, source_metadata_json
            ) VALUES (
                'observation-1', ?, 'test:run-input', 'record-1', NULL, ?,
                'RETRIEVED_AT_FALLBACK', ?, ?, '{}'
            )
            """,
            (run.result_manifest_artifact_id, CUTOFF, CUTOFF, attempt.attempt_id),
        )
    storage.connection.execute(
        """
        INSERT INTO derivation_node VALUES (
            'node-1', ?, 'INPUT_OBSERVATION', 'input_observation', 'observation-1'
        )
        """,
        (run.run_id,),
    )
    with storage.transaction(), storage._internal_write():
        storage.connection.execute(
            """
            INSERT INTO run_input (
                run_input_id, run_id, input_observation_id, input_role,
                derivation_node_id, created_at
            ) VALUES ('run-input-1', ?, 'observation-1', 'PRIMARY', 'node-1', ?)
            """,
            (run.run_id, CUTOFF),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "UPDATE run_input SET input_role = input_role WHERE run_input_id = 'run-input-1'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable record"):
        storage.connection.execute(
            "DELETE FROM run_input WHERE run_input_id = 'run-input-1'"
        )


def test_schema_rejects_direct_attempt_identity_and_event_forgery(storage):
    attempt = _attempt(storage, _request(storage).run_request_id)
    with pytest.raises(sqlite3.IntegrityError, match="checked projection|invalid attempt"):
        storage.connection.execute(
            "UPDATE attempt SET fence_token = fence_token + 1 WHERE attempt_id = ?",
            (attempt.attempt_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="authoritative write required"):
        storage.connection.execute(
            """
            INSERT INTO attempt_event (
                attempt_event_id, attempt_id, sequence_number, event_type, event_at,
                from_status, to_status, reason_code, failure_class, failure_message,
                actor_identity, details_artifact_id, created_at
            ) VALUES ('forged', ?, 99, 'AUTHORITY_REVOKED', ?, 'RUNNING', 'RUNNING',
                      NULL, NULL, NULL, 'forger', NULL, ?)
            """,
            (attempt.attempt_id, CUTOFF, CUTOFF),
        )
