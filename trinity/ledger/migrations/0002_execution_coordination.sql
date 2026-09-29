CREATE TABLE run_request (
    run_request_id TEXT PRIMARY KEY CHECK (length(run_request_id) > 0),
    request_kind TEXT NOT NULL CHECK (length(request_kind) > 0),
    requested_at TEXT NOT NULL CHECK (
        length(requested_at) = 27
        AND requested_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    analysis_cutoff_at TEXT NOT NULL CHECK (
        length(analysis_cutoff_at) = 27
        AND analysis_cutoff_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json)),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) > 0),
    requested_by TEXT NOT NULL CHECK (length(requested_by) > 0),
    baseline_commit TEXT NOT NULL CHECK (length(baseline_commit) > 0),
    UNIQUE (requested_by, idempotency_key)
) STRICT;

CREATE TABLE attempt (
    attempt_id TEXT PRIMARY KEY CHECK (length(attempt_id) > 0),
    run_request_id TEXT NOT NULL,
    attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal > 0),
    fence_token INTEGER NOT NULL CHECK (fence_token > 0),
    started_at TEXT NOT NULL CHECK (
        length(started_at) = 27
        AND started_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    finished_at TEXT CHECK (
        finished_at IS NULL OR (
            length(finished_at) = 27
            AND finished_at GLOB '????-??-??T??:??:??.??????Z'
            AND finished_at >= started_at
        )
    ),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'ABORTED')),
    worker_identity TEXT NOT NULL CHECK (length(worker_identity) > 0),
    code_commit TEXT NOT NULL CHECK (length(code_commit) > 0),
    environment_fingerprint TEXT NOT NULL CHECK (length(environment_fingerprint) > 0),
    failure_class TEXT,
    failure_message TEXT,
    CHECK (attempt_id <> run_request_id),
    CHECK (
        (status = 'RUNNING' AND finished_at IS NULL AND failure_class IS NULL AND failure_message IS NULL)
        OR (status = 'SUCCEEDED' AND finished_at IS NOT NULL AND failure_class IS NULL AND failure_message IS NULL)
        OR (status = 'FAILED' AND finished_at IS NOT NULL AND length(failure_class) > 0)
        OR (status = 'ABORTED' AND finished_at IS NOT NULL AND failure_class IS NULL AND failure_message IS NULL)
    ),
    UNIQUE (run_request_id, attempt_ordinal),
    UNIQUE (run_request_id, fence_token),
    FOREIGN KEY (run_request_id) REFERENCES run_request(run_request_id)
) STRICT;

CREATE TABLE run (
    run_id TEXT PRIMARY KEY CHECK (length(run_id) > 0),
    run_request_id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL UNIQUE,
    committed_at TEXT NOT NULL CHECK (
        length(committed_at) = 27
        AND committed_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    status TEXT NOT NULL CHECK (status = 'COMMITTED'),
    result_manifest_artifact_id TEXT NOT NULL UNIQUE,
    CHECK (run_id <> run_request_id AND run_id <> attempt_id AND run_request_id <> attempt_id),
    FOREIGN KEY (run_request_id) REFERENCES run_request(run_request_id),
    FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (result_manifest_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

CREATE TABLE run_request_control (
    run_request_id TEXT PRIMARY KEY,
    current_attempt_id TEXT UNIQUE,
    current_fence_token INTEGER NOT NULL DEFAULT 0 CHECK (current_fence_token >= 0),
    request_state TEXT NOT NULL CHECK (request_state IN ('ACTIVE', 'COMMITTED')),
    committed_run_id TEXT UNIQUE,
    updated_at TEXT NOT NULL CHECK (
        length(updated_at) = 27
        AND updated_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    CHECK (
        (request_state = 'ACTIVE' AND committed_run_id IS NULL)
        OR (request_state = 'COMMITTED' AND committed_run_id IS NOT NULL)
    ),
    FOREIGN KEY (run_request_id) REFERENCES run_request(run_request_id),
    FOREIGN KEY (current_attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (committed_run_id) REFERENCES run(run_id)
) STRICT;

CREATE TABLE attempt_event (
    attempt_event_id TEXT PRIMARY KEY CHECK (length(attempt_event_id) > 0),
    attempt_id TEXT NOT NULL,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN ('ALLOCATED', 'AUTHORITY_REVOKED', 'SUCCEEDED', 'FAILED', 'ABORTED')
    ),
    event_at TEXT NOT NULL CHECK (
        length(event_at) = 27
        AND event_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    from_status TEXT CHECK (
        from_status IS NULL OR from_status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'ABORTED')
    ),
    to_status TEXT NOT NULL CHECK (to_status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'ABORTED')),
    reason_code TEXT,
    failure_class TEXT,
    failure_message TEXT,
    actor_identity TEXT NOT NULL CHECK (length(actor_identity) > 0),
    details_artifact_id TEXT,
    created_at TEXT NOT NULL CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    CHECK (
        (event_type = 'ALLOCATED' AND from_status IS NULL AND to_status = 'RUNNING'
            AND reason_code IS NULL AND failure_class IS NULL AND failure_message IS NULL)
        OR (event_type = 'AUTHORITY_REVOKED' AND from_status IS NOT NULL
            AND to_status = from_status AND failure_class IS NULL AND failure_message IS NULL)
        OR (event_type = 'SUCCEEDED' AND from_status = 'RUNNING' AND to_status = 'SUCCEEDED'
            AND reason_code IS NULL AND failure_class IS NULL AND failure_message IS NULL)
        OR (event_type = 'FAILED' AND from_status = 'RUNNING' AND to_status = 'FAILED'
            AND length(failure_class) > 0)
        OR (event_type = 'ABORTED' AND from_status = 'RUNNING' AND to_status = 'ABORTED'
            AND length(reason_code) > 0 AND failure_class IS NULL AND failure_message IS NULL)
    ),
    UNIQUE (attempt_id, sequence_number),
    FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (details_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

CREATE TABLE attempt_artifact (
    attempt_artifact_id TEXT PRIMARY KEY CHECK (length(attempt_artifact_id) > 0),
    attempt_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('OUTPUT', 'RESULT_MANIFEST', 'FAILED_ATTEMPT_OUTPUT', 'LOG', 'DIAGNOSTIC', 'PARTIAL_OUTPUT')
    ),
    attached_at TEXT NOT NULL CHECK (
        length(attached_at) = 27
        AND attached_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    UNIQUE (attempt_id, role, ordinal),
    UNIQUE (attempt_id, artifact_id, role),
    FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

CREATE TABLE run_input (
    run_input_id TEXT PRIMARY KEY CHECK (length(run_input_id) > 0),
    run_id TEXT NOT NULL,
    input_observation_id TEXT NOT NULL,
    input_role TEXT NOT NULL CHECK (length(input_role) > 0),
    derivation_node_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    UNIQUE (run_id, input_observation_id, input_role, derivation_node_id),
    FOREIGN KEY (run_id) REFERENCES run(run_id),
    FOREIGN KEY (input_observation_id) REFERENCES input_observation(input_observation_id),
    FOREIGN KEY (derivation_node_id) REFERENCES derivation_node(derivation_node_id)
) STRICT;

CREATE INDEX attempt_request_status_idx ON attempt(run_request_id, status);
CREATE INDEX attempt_event_attempt_idx ON attempt_event(attempt_id, sequence_number);
CREATE INDEX attempt_artifact_attempt_idx ON attempt_artifact(attempt_id, role, ordinal);
CREATE INDEX run_input_run_idx ON run_input(run_id);

CREATE TRIGGER run_request_insert_authorized
BEFORE INSERT ON run_request
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: run_request');
END;

CREATE TRIGGER run_request_no_update
BEFORE UPDATE ON run_request
BEGIN
    SELECT RAISE(ABORT, 'immutable record: run_request');
END;

CREATE TRIGGER run_request_no_delete
BEFORE DELETE ON run_request
BEGIN
    SELECT RAISE(ABORT, 'immutable record: run_request');
END;

CREATE TRIGGER attempt_no_delete
BEFORE DELETE ON attempt
BEGIN
    SELECT RAISE(ABORT, 'immutable record: attempt');
END;

CREATE TRIGGER attempt_insert_authorized
BEFORE INSERT ON attempt
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: attempt');
END;

CREATE TRIGGER attempt_update_authorized
BEFORE UPDATE ON attempt
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'checked projection: attempt');
END;

CREATE TRIGGER attempt_update_valid
BEFORE UPDATE ON attempt
BEGIN
    SELECT CASE WHEN
        NEW.attempt_id <> OLD.attempt_id
        OR NEW.run_request_id <> OLD.run_request_id
        OR NEW.attempt_ordinal <> OLD.attempt_ordinal
        OR NEW.fence_token <> OLD.fence_token
        OR NEW.started_at <> OLD.started_at
        OR NEW.worker_identity <> OLD.worker_identity
        OR NEW.code_commit <> OLD.code_commit
        OR NEW.environment_fingerprint <> OLD.environment_fingerprint
        OR OLD.status <> 'RUNNING'
        OR NEW.status NOT IN ('SUCCEEDED', 'FAILED', 'ABORTED')
    THEN RAISE(ABORT, 'invalid attempt transition') END;
    SELECT CASE WHEN NEW.status = 'SUCCEEDED' AND NOT EXISTS (
        SELECT 1 FROM run
        WHERE run.attempt_id = NEW.attempt_id
          AND run.run_request_id = NEW.run_request_id
          AND run.status = 'COMMITTED'
    ) THEN RAISE(ABORT, 'successful attempt requires committed run') END;
END;

CREATE TRIGGER run_insert_authorized
BEFORE INSERT ON run
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: run');
END;

CREATE TRIGGER run_insert_valid
BEFORE INSERT ON run
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM attempt
        WHERE attempt.attempt_id = NEW.attempt_id
          AND attempt.run_request_id = NEW.run_request_id
          AND attempt.status = 'RUNNING'
    ) THEN RAISE(ABORT, 'committed run requires matching running attempt') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM artifact
        WHERE artifact.artifact_id = NEW.result_manifest_artifact_id
          AND artifact.artifact_kind = 'ledger.run-result-manifest.v1'
          AND artifact.canonicalization_version = 'JCS-LEDGER-SUBSET-V1'
    ) THEN RAISE(ABORT, 'committed run requires canonical result manifest') END;
END;

CREATE TRIGGER run_no_update
BEFORE UPDATE ON run
BEGIN
    SELECT RAISE(ABORT, 'immutable record: run');
END;

CREATE TRIGGER run_no_delete
BEFORE DELETE ON run
BEGIN
    SELECT RAISE(ABORT, 'immutable record: run');
END;

CREATE TRIGGER run_request_control_update_authorized
BEFORE UPDATE ON run_request_control
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'checked projection: run_request_control');
END;

CREATE TRIGGER run_request_control_insert_authorized
BEFORE INSERT ON run_request_control
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: run_request_control');
END;

CREATE TRIGGER run_request_control_insert_valid
BEFORE INSERT ON run_request_control
WHEN NEW.current_attempt_id IS NOT NULL
  OR NEW.current_fence_token <> 0
  OR NEW.request_state <> 'ACTIVE'
  OR NEW.committed_run_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'invalid initial request control');
END;

CREATE TRIGGER run_request_control_update_valid
BEFORE UPDATE ON run_request_control
BEGIN
    SELECT CASE WHEN NEW.run_request_id <> OLD.run_request_id
        OR NEW.current_fence_token < OLD.current_fence_token
        OR OLD.request_state = 'COMMITTED'
    THEN RAISE(ABORT, 'invalid request control transition') END;
    SELECT CASE WHEN NEW.request_state = 'ACTIVE' AND (
        NEW.committed_run_id IS NOT NULL
        OR NEW.current_fence_token <= OLD.current_fence_token
        OR NEW.current_attempt_id IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM attempt
            WHERE attempt.attempt_id = NEW.current_attempt_id
              AND attempt.run_request_id = NEW.run_request_id
              AND attempt.fence_token = NEW.current_fence_token
              AND attempt.status = 'RUNNING'
        )
    ) THEN RAISE(ABORT, 'invalid active request control') END;
    SELECT CASE WHEN NEW.request_state = 'COMMITTED' AND (
        NEW.committed_run_id IS NULL
        OR NEW.current_attempt_id <> OLD.current_attempt_id
        OR NEW.current_fence_token <> OLD.current_fence_token
        OR NOT EXISTS (
            SELECT 1 FROM run JOIN attempt ON attempt.attempt_id = run.attempt_id
            WHERE run.run_id = NEW.committed_run_id
              AND run.run_request_id = NEW.run_request_id
              AND run.attempt_id = NEW.current_attempt_id
              AND attempt.status = 'SUCCEEDED'
        )
    ) THEN RAISE(ABORT, 'invalid committed request control') END;
END;

CREATE TRIGGER run_request_control_no_delete
BEFORE DELETE ON run_request_control
BEGIN
    SELECT RAISE(ABORT, 'checked projection: run_request_control');
END;

CREATE TRIGGER attempt_event_insert_authorized
BEFORE INSERT ON attempt_event
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: attempt_event');
END;

CREATE TRIGGER attempt_event_no_update
BEFORE UPDATE ON attempt_event
BEGIN
    SELECT RAISE(ABORT, 'immutable record: attempt_event');
END;

CREATE TRIGGER attempt_event_no_delete
BEFORE DELETE ON attempt_event
BEGIN
    SELECT RAISE(ABORT, 'immutable record: attempt_event');
END;

CREATE TRIGGER attempt_artifact_insert_authorized
BEFORE INSERT ON attempt_artifact
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: attempt_artifact');
END;

CREATE TRIGGER attempt_artifact_no_update
BEFORE UPDATE ON attempt_artifact
BEGIN
    SELECT RAISE(ABORT, 'immutable record: attempt_artifact');
END;

CREATE TRIGGER attempt_artifact_no_delete
BEFORE DELETE ON attempt_artifact
BEGIN
    SELECT RAISE(ABORT, 'immutable record: attempt_artifact');
END;

CREATE TRIGGER run_input_insert_authorized
BEFORE INSERT ON run_input
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: run_input');
END;

CREATE TRIGGER run_input_insert_valid
BEFORE INSERT ON run_input
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM derivation_node
        WHERE derivation_node.derivation_node_id = NEW.derivation_node_id
          AND derivation_node.run_id = NEW.run_id
          AND derivation_node.node_kind = 'INPUT_OBSERVATION'
          AND derivation_node.entity_type = 'input_observation'
          AND derivation_node.entity_id = NEW.input_observation_id
    ) THEN RAISE(ABORT, 'run input requires matching committed input-observation node') END;
END;

CREATE TRIGGER run_input_no_update
BEFORE UPDATE ON run_input
BEGIN
    SELECT RAISE(ABORT, 'immutable record: run_input');
END;

CREATE TRIGGER run_input_no_delete
BEFORE DELETE ON run_input
BEGIN
    SELECT RAISE(ABORT, 'immutable record: run_input');
END;
