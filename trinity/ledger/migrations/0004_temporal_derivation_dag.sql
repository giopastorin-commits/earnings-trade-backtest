CREATE TABLE derivation_node (
    derivation_node_id TEXT PRIMARY KEY CHECK (length(derivation_node_id) > 0),
    run_id TEXT,
    attempt_id TEXT NOT NULL,
    node_kind TEXT NOT NULL CHECK (
        node_kind IN (
            'INPUT_OBSERVATION', 'NORMALIZED_FACT', 'RESEARCH',
            'SETUP', 'ELIGIBILITY', 'OUTCOME'
        )
    ),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL CHECK (length(entity_id) > 0),
    direct_available_at TEXT CHECK (
        direct_available_at IS NULL OR (
            length(direct_available_at) = 27
            AND direct_available_at GLOB '????-??-??T??:??:??.??????Z'
        )
    ),
    derived_available_at TEXT NOT NULL CHECK (
        length(derived_available_at) = 27
        AND derived_available_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    derivation_policy_version TEXT NOT NULL CHECK (
        derivation_policy_version = 'MAX_REQUIRED_PARENTS_V1'
    ),
    CHECK (
        (node_kind = 'INPUT_OBSERVATION' AND entity_type = 'input_observation'
            AND direct_available_at IS NOT NULL)
        OR (node_kind = 'NORMALIZED_FACT' AND entity_type = 'artifact'
            AND direct_available_at IS NULL)
        OR (node_kind = 'RESEARCH' AND entity_type = 'research_record'
            AND direct_available_at IS NULL)
        OR (node_kind = 'SETUP' AND entity_type = 'setup'
            AND direct_available_at IS NULL)
        OR (node_kind = 'ELIGIBILITY' AND entity_type = 'setup_execution_eligibility'
            AND direct_available_at IS NULL)
        OR (node_kind = 'OUTCOME' AND entity_type = 'outcome'
            AND direct_available_at IS NULL)
    ),
    FOREIGN KEY (run_id) REFERENCES run(run_id),
    FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id)
) STRICT;

CREATE TABLE derivation_edge (
    derivation_edge_id TEXT PRIMARY KEY CHECK (length(derivation_edge_id) > 0),
    parent_node_id TEXT NOT NULL,
    child_node_id TEXT NOT NULL,
    edge_role TEXT NOT NULL CHECK (
        length(edge_role) > 0 AND instr(edge_role, char(0)) = 0
    ),
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    CHECK (parent_node_id <> child_node_id),
    UNIQUE (parent_node_id, child_node_id, edge_role),
    FOREIGN KEY (parent_node_id)
        REFERENCES derivation_node(derivation_node_id),
    FOREIGN KEY (child_node_id)
        REFERENCES derivation_node(derivation_node_id)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE INDEX derivation_node_attempt_idx
ON derivation_node(attempt_id, run_id);

CREATE INDEX derivation_node_entity_idx
ON derivation_node(node_kind, entity_type, entity_id);

CREATE INDEX derivation_edge_child_order_idx
ON derivation_edge(child_node_id, edge_role COLLATE BINARY, parent_node_id COLLATE BINARY);

CREATE INDEX derivation_edge_parent_idx
ON derivation_edge(parent_node_id);

CREATE TRIGGER derivation_edge_insert_authorized
BEFORE INSERT ON derivation_edge
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: derivation_edge');
END;

CREATE TRIGGER derivation_edge_insert_valid
BEFORE INSERT ON derivation_edge
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM derivation_node
        WHERE derivation_node_id = NEW.child_node_id
    ) THEN RAISE(ABORT, 'cannot add edge after child construction') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM derivation_node
        WHERE derivation_node_id = NEW.parent_node_id
    ) THEN RAISE(ABORT, 'derivation parent does not exist') END;
    SELECT CASE WHEN EXISTS (
        WITH RECURSIVE descendants(node_id) AS (
            SELECT child_node_id FROM derivation_edge
            WHERE parent_node_id = NEW.child_node_id
            UNION
            SELECT edge.child_node_id
            FROM derivation_edge AS edge
            JOIN descendants ON edge.parent_node_id = descendants.node_id
        )
        SELECT 1 FROM descendants WHERE node_id = NEW.parent_node_id
    ) THEN RAISE(ABORT, 'derivation cycle') END;
END;

CREATE TRIGGER derivation_edge_no_update
BEFORE UPDATE ON derivation_edge
BEGIN
    SELECT RAISE(ABORT, 'immutable record: derivation_edge');
END;

CREATE TRIGGER derivation_edge_no_delete
BEFORE DELETE ON derivation_edge
BEGIN
    SELECT RAISE(ABORT, 'immutable record: derivation_edge');
END;

CREATE TRIGGER derivation_node_insert_authorized
BEFORE INSERT ON derivation_node
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: derivation_node');
END;

CREATE TRIGGER derivation_node_insert_valid
BEFORE INSERT ON derivation_node
BEGIN
    SELECT CASE WHEN NEW.run_id IS NOT NULL
        THEN RAISE(ABORT, 'new derivation node must be attempt-local') END;
    SELECT CASE WHEN NEW.node_kind = 'INPUT_OBSERVATION' AND NOT EXISTS (
        SELECT 1 FROM input_observation
        WHERE input_observation_id = NEW.entity_id
          AND effective_available_at = NEW.direct_available_at
          AND effective_available_at = NEW.derived_available_at
    ) THEN RAISE(ABORT, 'invalid input-observation derivation node') END;
    SELECT CASE WHEN NEW.node_kind = 'INPUT_OBSERVATION' AND EXISTS (
        SELECT 1 FROM derivation_edge WHERE child_node_id = NEW.derivation_node_id
    ) THEN RAISE(ABORT, 'raw derivation node cannot have parents') END;
    SELECT CASE WHEN NEW.node_kind = 'NORMALIZED_FACT' AND NOT EXISTS (
        SELECT 1 FROM artifact WHERE artifact_id = NEW.entity_id
    ) THEN RAISE(ABORT, 'normalized fact requires artifact target') END;
    SELECT CASE WHEN NEW.node_kind = 'NORMALIZED_FACT' AND NOT EXISTS (
        SELECT 1 FROM derivation_edge
        WHERE child_node_id = NEW.derivation_node_id AND required = 1
    ) THEN RAISE(ABORT, 'derived node requires a required parent') END;
    SELECT CASE WHEN NEW.node_kind = 'NORMALIZED_FACT' AND EXISTS (
        SELECT 1
        FROM derivation_edge AS edge
        LEFT JOIN derivation_node AS parent
          ON parent.derivation_node_id = edge.parent_node_id
        WHERE edge.child_node_id = NEW.derivation_node_id
          AND (
              parent.derivation_node_id IS NULL
              OR parent.attempt_id <> NEW.attempt_id
              OR parent.run_id IS NOT NULL
          )
    ) THEN RAISE(ABORT, 'derived parents must share attempt-local lineage') END;
    SELECT CASE WHEN NEW.node_kind IN ('RESEARCH', 'SETUP', 'ELIGIBILITY', 'OUTCOME')
        THEN RAISE(ABORT, 'reserved derivation node kind is not instantiable') END;
END;

CREATE TRIGGER derivation_node_update_authorized
BEFORE UPDATE ON derivation_node
WHEN ledger_run_binding_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'immutable record: derivation_node');
END;

CREATE TRIGGER derivation_node_update_valid
BEFORE UPDATE ON derivation_node
WHEN ledger_run_binding_authorized() = 1
BEGIN
    SELECT CASE WHEN
        OLD.run_id IS NOT NULL
        OR NEW.run_id IS NULL
        OR NEW.derivation_node_id <> OLD.derivation_node_id
        OR NEW.attempt_id <> OLD.attempt_id
        OR NEW.node_kind <> OLD.node_kind
        OR NEW.entity_type <> OLD.entity_type
        OR NEW.entity_id <> OLD.entity_id
        OR NEW.direct_available_at IS NOT OLD.direct_available_at
        OR NEW.derived_available_at <> OLD.derived_available_at
        OR NEW.derivation_policy_version <> OLD.derivation_policy_version
        OR NOT EXISTS (
            SELECT 1 FROM run
            WHERE run.run_id = NEW.run_id
              AND run.attempt_id = NEW.attempt_id
        )
    THEN RAISE(ABORT, 'invalid derivation run binding') END;
END;

CREATE TRIGGER derivation_node_no_delete
BEFORE DELETE ON derivation_node
BEGIN
    SELECT RAISE(ABORT, 'immutable record: derivation_node');
END;
