CREATE TABLE pit_classification_policy (
    classification_policy_id TEXT PRIMARY KEY CHECK (
        classification_policy_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    ),
    policy_kind TEXT NOT NULL CHECK (policy_kind = 'PIT_CLASSIFICATION'),
    policy_version TEXT NOT NULL CHECK (policy_version = 'REQUIRED_ANCESTRY_PIT_V1'),
    definition_artifact_id TEXT NOT NULL,
    code_commit TEXT NOT NULL CHECK (length(code_commit) > 0),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    UNIQUE (policy_kind, policy_version),
    FOREIGN KEY (definition_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

CREATE TABLE derivation_node_classification (
    derivation_node_classification_id TEXT PRIMARY KEY CHECK (
        derivation_node_classification_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    ),
    derivation_node_id TEXT NOT NULL,
    pit_reference_at TEXT NOT NULL CHECK (length(pit_reference_at) = 27 AND pit_reference_at GLOB '????-??-??T??:??:??.??????Z'),
    classification_version INTEGER NOT NULL CHECK (classification_version > 0),
    record_class TEXT NOT NULL CHECK (record_class IN ('LEDGER_NATIVE','LEGACY_NON_LEDGER_ARTIFACT')),
    resolved_record_class TEXT NOT NULL CHECK (resolved_record_class IN ('LEDGER_NATIVE','LEGACY_NON_LEDGER_ARTIFACT')),
    pit_class TEXT NOT NULL CHECK (pit_class IN ('ARCHIVED_POINT_IN_TIME','RECONSTRUCTED_NOT_ARCHIVED','NOT_APPLICABLE','UNKNOWN')),
    classification_basis TEXT NOT NULL CHECK (classification_basis IN ('RAW_ARCHIVE_EVIDENCE','RAW_RECONSTRUCTION_EVIDENCE','RAW_UNKNOWN_EVIDENCE','RAW_NOT_APPLICABLE_EVIDENCE','REQUIRED_PARENT_PROPAGATION')),
    classification_policy_id TEXT NOT NULL,
    evidence_artifact_id TEXT,
    classified_by_attempt_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    supersedes_classification_id TEXT UNIQUE,
    UNIQUE (derivation_node_id, pit_reference_at, classification_version),
    CHECK (derivation_node_classification_id IS NOT supersedes_classification_id),
    CHECK ((classification_basis = 'REQUIRED_PARENT_PROPAGATION' AND evidence_artifact_id IS NULL) OR (classification_basis <> 'REQUIRED_PARENT_PROPAGATION' AND evidence_artifact_id IS NOT NULL)),
    FOREIGN KEY (derivation_node_id) REFERENCES derivation_node(derivation_node_id),
    FOREIGN KEY (classification_policy_id) REFERENCES pit_classification_policy(classification_policy_id),
    FOREIGN KEY (evidence_artifact_id) REFERENCES artifact(artifact_id),
    FOREIGN KEY (classified_by_attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (supersedes_classification_id) REFERENCES derivation_node_classification(derivation_node_classification_id)
) STRICT;

CREATE TABLE derivation_node_classification_parent (
    child_classification_id TEXT NOT NULL,
    parent_classification_id TEXT NOT NULL,
    PRIMARY KEY (child_classification_id, parent_classification_id),
    CHECK (child_classification_id <> parent_classification_id),
    FOREIGN KEY (child_classification_id) REFERENCES derivation_node_classification(derivation_node_classification_id),
    FOREIGN KEY (parent_classification_id) REFERENCES derivation_node_classification(derivation_node_classification_id)
) STRICT;

CREATE INDEX derivation_node_classification_lookup_idx ON derivation_node_classification(derivation_node_id, pit_reference_at, classification_version);
CREATE INDEX derivation_node_classification_policy_idx ON derivation_node_classification(classification_policy_id);
CREATE INDEX derivation_node_classification_parent_parent_idx ON derivation_node_classification_parent(parent_classification_id);

CREATE TRIGGER pit_classification_policy_insert_authorized BEFORE INSERT ON pit_classification_policy
WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: pit_classification_policy'); END;
CREATE TRIGGER pit_classification_policy_no_update BEFORE UPDATE ON pit_classification_policy BEGIN SELECT RAISE(ABORT, 'immutable record: pit_classification_policy'); END;
CREATE TRIGGER pit_classification_policy_no_delete BEFORE DELETE ON pit_classification_policy BEGIN SELECT RAISE(ABORT, 'immutable record: pit_classification_policy'); END;

CREATE TRIGGER derivation_node_classification_insert_authorized BEFORE INSERT ON derivation_node_classification
WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: derivation_node_classification'); END;
CREATE TRIGGER derivation_node_classification_insert_valid BEFORE INSERT ON derivation_node_classification BEGIN
    SELECT CASE WHEN NEW.classification_version = 1 AND NEW.supersedes_classification_id IS NOT NULL THEN RAISE(ABORT, 'first classification cannot supersede') END;
    SELECT CASE WHEN NEW.classification_version > 1 AND NOT EXISTS (
        SELECT 1 FROM derivation_node_classification p
        WHERE p.derivation_node_classification_id = NEW.supersedes_classification_id
          AND p.derivation_node_id = NEW.derivation_node_id
          AND p.pit_reference_at = NEW.pit_reference_at
          AND p.classification_version = NEW.classification_version - 1
          AND NOT EXISTS (SELECT 1 FROM derivation_node_classification n WHERE n.supersedes_classification_id = p.derivation_node_classification_id)
    ) THEN RAISE(ABORT, 'invalid classification supersession') END;
    SELECT CASE WHEN NEW.classification_version = 1 AND EXISTS (
        SELECT 1 FROM derivation_node_classification c WHERE c.derivation_node_id = NEW.derivation_node_id AND c.pit_reference_at = NEW.pit_reference_at
    ) THEN RAISE(ABORT, 'classification chain already exists') END;
END;
CREATE TRIGGER derivation_node_classification_no_update BEFORE UPDATE ON derivation_node_classification BEGIN SELECT RAISE(ABORT, 'immutable record: derivation_node_classification'); END;
CREATE TRIGGER derivation_node_classification_no_delete BEFORE DELETE ON derivation_node_classification BEGIN SELECT RAISE(ABORT, 'immutable record: derivation_node_classification'); END;

CREATE TRIGGER derivation_node_classification_parent_insert_authorized BEFORE INSERT ON derivation_node_classification_parent
WHEN ledger_internal_write_authorized() <> 1 OR ledger_classification_construction_authorized() <> 1
BEGIN SELECT RAISE(ABORT, 'authoritative construction required: derivation_node_classification_parent'); END;
CREATE TRIGGER derivation_node_classification_parent_insert_valid BEFORE INSERT ON derivation_node_classification_parent BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM derivation_node_classification child
        JOIN derivation_node_classification parent
        JOIN derivation_edge edge ON edge.child_node_id = child.derivation_node_id AND edge.parent_node_id = parent.derivation_node_id AND edge.required = 1
        JOIN pit_classification_policy cp ON cp.classification_policy_id = child.classification_policy_id
        JOIN pit_classification_policy pp ON pp.classification_policy_id = parent.classification_policy_id
        WHERE child.derivation_node_classification_id = NEW.child_classification_id
          AND parent.derivation_node_classification_id = NEW.parent_classification_id
          AND child.pit_reference_at = parent.pit_reference_at
          AND cp.policy_kind = pp.policy_kind AND cp.policy_version = pp.policy_version
    ) THEN RAISE(ABORT, 'invalid classification parent') END;
END;
CREATE TRIGGER derivation_node_classification_parent_no_update BEFORE UPDATE ON derivation_node_classification_parent BEGIN SELECT RAISE(ABORT, 'immutable record: derivation_node_classification_parent'); END;
CREATE TRIGGER derivation_node_classification_parent_no_delete BEFORE DELETE ON derivation_node_classification_parent BEGIN SELECT RAISE(ABORT, 'immutable record: derivation_node_classification_parent'); END;

DROP TRIGGER run_insert_valid;
CREATE TRIGGER run_insert_valid BEFORE INSERT ON run BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM attempt WHERE attempt.attempt_id = NEW.attempt_id AND attempt.run_request_id = NEW.run_request_id AND attempt.status = 'RUNNING'
    ) THEN RAISE(ABORT, 'committed run requires matching running attempt') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM artifact WHERE artifact.artifact_id = NEW.result_manifest_artifact_id
          AND artifact.artifact_kind IN ('ledger.run-result-manifest.v1','ledger.run-result-manifest.v2')
          AND artifact.canonicalization_version = 'JCS-LEDGER-SUBSET-V1'
    ) THEN RAISE(ABORT, 'committed run requires canonical result manifest') END;
END;
