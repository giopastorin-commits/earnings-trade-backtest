PRAGMA defer_foreign_keys = ON;

CREATE TABLE pit_classification_policy_v141 (
    classification_policy_id TEXT PRIMARY KEY CHECK (
        classification_policy_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    ),
    policy_kind TEXT NOT NULL CHECK (policy_kind = 'PIT_CLASSIFICATION'),
    policy_version TEXT NOT NULL CHECK (policy_version IN (
        'REQUIRED_ANCESTRY_PIT_V1', 'REQUIRED_ANCESTRY_PIT_V2'
    )),
    definition_artifact_id TEXT NOT NULL,
    code_commit TEXT NOT NULL CHECK (length(code_commit) > 0),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    UNIQUE (policy_kind, policy_version),
    FOREIGN KEY (definition_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

INSERT INTO pit_classification_policy_v141 (
    classification_policy_id, policy_kind, policy_version,
    definition_artifact_id, code_commit, created_at
)
SELECT classification_policy_id, policy_kind, policy_version,
       definition_artifact_id, code_commit, created_at
FROM pit_classification_policy;

CREATE TEMP TABLE ledger_v141_policy_rebuild_guard (
    valid INTEGER NOT NULL CHECK (valid = 1)
) STRICT;
INSERT INTO ledger_v141_policy_rebuild_guard VALUES (
    (SELECT count(*) FROM pit_classification_policy) =
    (SELECT count(*) FROM pit_classification_policy_v141)
    AND NOT EXISTS (
        SELECT classification_policy_id, policy_kind, policy_version,
               definition_artifact_id, code_commit, created_at
        FROM pit_classification_policy
        EXCEPT
        SELECT classification_policy_id, policy_kind, policy_version,
               definition_artifact_id, code_commit, created_at
        FROM pit_classification_policy_v141
    )
    AND NOT EXISTS (
        SELECT classification_policy_id, policy_kind, policy_version,
               definition_artifact_id, code_commit, created_at
        FROM pit_classification_policy_v141
        EXCEPT
        SELECT classification_policy_id, policy_kind, policy_version,
               definition_artifact_id, code_commit, created_at
        FROM pit_classification_policy
    )
);
DROP TABLE ledger_v141_policy_rebuild_guard;

-- SQLite treats DROP TABLE as an implicit DELETE for FK accounting. Preserve
-- populated classification rows transactionally while the referenced policy
-- table is replaced, then restore them byte-for-byte before commit.
CREATE TEMP TABLE ledger_v141_classification_backup AS
SELECT derivation_node_classification_id, derivation_node_id, pit_reference_at,
       classification_version, record_class, resolved_record_class, pit_class,
       classification_basis, classification_policy_id, evidence_artifact_id,
       classified_by_attempt_id, created_at, supersedes_classification_id
FROM derivation_node_classification;
CREATE TEMP TABLE ledger_v141_classification_parent_backup AS
SELECT child_classification_id, parent_classification_id
FROM derivation_node_classification_parent;

DROP TRIGGER pit_classification_policy_insert_authorized;
DROP TRIGGER pit_classification_policy_no_update;
DROP TRIGGER pit_classification_policy_no_delete;
DROP TRIGGER derivation_node_classification_insert_authorized;
DROP TRIGGER derivation_node_classification_insert_valid;
DROP TRIGGER derivation_node_classification_no_delete;
DROP TRIGGER derivation_node_classification_parent_insert_authorized;
DROP TRIGGER derivation_node_classification_parent_insert_valid;
DROP TRIGGER derivation_node_classification_parent_no_delete;
DELETE FROM derivation_node_classification_parent;
DELETE FROM derivation_node_classification;
DROP TABLE pit_classification_policy;
ALTER TABLE pit_classification_policy_v141 RENAME TO pit_classification_policy;

INSERT INTO derivation_node_classification (
    derivation_node_classification_id, derivation_node_id, pit_reference_at,
    classification_version, record_class, resolved_record_class, pit_class,
    classification_basis, classification_policy_id, evidence_artifact_id,
    classified_by_attempt_id, created_at, supersedes_classification_id
)
SELECT derivation_node_classification_id, derivation_node_id, pit_reference_at,
       classification_version, record_class, resolved_record_class, pit_class,
       classification_basis, classification_policy_id, evidence_artifact_id,
       classified_by_attempt_id, created_at, supersedes_classification_id
FROM ledger_v141_classification_backup
ORDER BY derivation_node_id, pit_reference_at, classification_version;
INSERT INTO derivation_node_classification_parent (
    child_classification_id, parent_classification_id
)
SELECT child_classification_id, parent_classification_id
FROM ledger_v141_classification_parent_backup;

CREATE TEMP TABLE ledger_v141_classification_restore_guard (
    valid INTEGER NOT NULL CHECK (valid = 1)
) STRICT;
INSERT INTO ledger_v141_classification_restore_guard VALUES (
    (SELECT count(*) FROM ledger_v141_classification_backup) =
    (SELECT count(*) FROM derivation_node_classification)
    AND (SELECT count(*) FROM ledger_v141_classification_parent_backup) =
        (SELECT count(*) FROM derivation_node_classification_parent)
    AND NOT EXISTS (
        SELECT * FROM ledger_v141_classification_backup
        EXCEPT SELECT * FROM derivation_node_classification
    )
    AND NOT EXISTS (
        SELECT * FROM derivation_node_classification
        EXCEPT SELECT * FROM ledger_v141_classification_backup
    )
    AND NOT EXISTS (
        SELECT * FROM ledger_v141_classification_parent_backup
        EXCEPT SELECT * FROM derivation_node_classification_parent
    )
    AND NOT EXISTS (
        SELECT * FROM derivation_node_classification_parent
        EXCEPT SELECT * FROM ledger_v141_classification_parent_backup
    )
);
DROP TABLE ledger_v141_classification_restore_guard;
DROP TABLE ledger_v141_classification_parent_backup;
DROP TABLE ledger_v141_classification_backup;

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
CREATE TRIGGER derivation_node_classification_parent_no_delete BEFORE DELETE ON derivation_node_classification_parent BEGIN SELECT RAISE(ABORT, 'immutable record: derivation_node_classification_parent'); END;

CREATE TABLE research_method (
    research_method_id TEXT PRIMARY KEY CHECK (research_method_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    research_kind TEXT NOT NULL CHECK (research_kind = 'TRINITY_USA_RESEARCH'),
    method_version TEXT NOT NULL CHECK (method_version = 'USA_V2'),
    definition_artifact_id TEXT NOT NULL,
    code_commit TEXT NOT NULL CHECK (length(code_commit) = 40 AND code_commit NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    UNIQUE (research_kind, method_version),
    FOREIGN KEY (definition_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

CREATE TABLE llm_interaction (
    llm_interaction_id TEXT PRIMARY KEY CHECK (llm_interaction_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    attempt_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    provider TEXT NOT NULL CHECK (provider = 'OPENAI_CODEX_CLI'),
    model TEXT NOT NULL CHECK (model = 'gpt-5.6-sol'),
    model_version TEXT NOT NULL CHECK (model_version = 'codex-cli:gpt-5.6-sol'),
    request_artifact_id TEXT NOT NULL,
    response_artifact_id TEXT,
    error_artifact_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('SUCCEEDED','FAILED')),
    started_at TEXT NOT NULL CHECK (length(started_at) = 27 AND started_at GLOB '????-??-??T??:??:??.??????Z'),
    finished_at TEXT NOT NULL CHECK (length(finished_at) = 27 AND finished_at GLOB '????-??-??T??:??:??.??????Z' AND finished_at >= started_at),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    UNIQUE (attempt_id, ordinal),
    CHECK ((status = 'SUCCEEDED' AND response_artifact_id IS NOT NULL AND error_artifact_id IS NULL) OR (status = 'FAILED' AND response_artifact_id IS NULL AND error_artifact_id IS NOT NULL)),
    FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (request_artifact_id) REFERENCES artifact(artifact_id),
    FOREIGN KEY (response_artifact_id) REFERENCES artifact(artifact_id),
    FOREIGN KEY (error_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

CREATE TABLE research_record (
    research_id TEXT PRIMARY KEY CHECK (research_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    attempt_id TEXT NOT NULL,
    run_id TEXT,
    research_kind TEXT NOT NULL CHECK (research_kind = 'TRINITY_USA_RESEARCH'),
    subject_key TEXT NOT NULL CHECK (length(subject_key) > 0),
    content_artifact_id TEXT NOT NULL,
    research_method_id TEXT NOT NULL,
    method_version TEXT NOT NULL CHECK (method_version = 'USA_V2'),
    as_of_at TEXT NOT NULL CHECK (length(as_of_at) = 27 AND as_of_at GLOB '????-??-??T??:??:??.??????Z'),
    derivation_node_id TEXT NOT NULL UNIQUE,
    supersedes_research_id TEXT CHECK (supersedes_research_id IS NULL),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (run_id) REFERENCES run(run_id),
    FOREIGN KEY (content_artifact_id) REFERENCES artifact(artifact_id),
    FOREIGN KEY (research_method_id) REFERENCES research_method(research_method_id),
    FOREIGN KEY (derivation_node_id) REFERENCES derivation_node(derivation_node_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (supersedes_research_id) REFERENCES research_record(research_id)
) STRICT;

CREATE TABLE research_llm_interaction (
    research_id TEXT NOT NULL,
    llm_interaction_id TEXT NOT NULL,
    interaction_role TEXT NOT NULL CHECK (interaction_role IN ('PRIMARY','SUPPORTING','CRITIQUE')),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY (research_id, llm_interaction_id, interaction_role),
    UNIQUE (research_id, interaction_role, ordinal),
    UNIQUE (research_id, llm_interaction_id),
    FOREIGN KEY (research_id) REFERENCES research_record(research_id),
    FOREIGN KEY (llm_interaction_id) REFERENCES llm_interaction(llm_interaction_id)
) STRICT;

CREATE TABLE setup_policy (
    setup_policy_id TEXT PRIMARY KEY CHECK (setup_policy_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    policy_kind TEXT NOT NULL CHECK (policy_kind = 'SETUP'),
    policy_version TEXT NOT NULL CHECK (policy_version = 'USA_SETUP_V1'),
    definition_artifact_id TEXT NOT NULL,
    code_commit TEXT NOT NULL CHECK (length(code_commit) = 40 AND code_commit NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    UNIQUE (policy_kind, policy_version),
    FOREIGN KEY (definition_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

CREATE TABLE setup (
    setup_id TEXT PRIMARY KEY CHECK (setup_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    attempt_id TEXT NOT NULL,
    run_id TEXT,
    instrument_id TEXT NOT NULL CHECK (length(instrument_id) > 0),
    direction TEXT NOT NULL CHECK (direction = 'LONG'),
    setup_kind TEXT NOT NULL CHECK (setup_kind IN ('BREAKOUT','PULLBACK','NO_SETUP')),
    signal_at TEXT NOT NULL CHECK (length(signal_at) = 27 AND signal_at GLOB '????-??-??T??:??:??.??????Z'),
    entry_rule_json TEXT NOT NULL CHECK (json_valid(entry_rule_json)),
    stop_rule_json TEXT NOT NULL CHECK (json_valid(stop_rule_json)),
    target_rule_json TEXT NOT NULL CHECK (json_valid(target_rule_json)),
    valid_from TEXT CHECK (valid_from IS NULL),
    expires_at TEXT CHECK (expires_at IS NULL),
    invalidates_at TEXT CHECK (invalidates_at IS NULL),
    setup_policy_id TEXT NOT NULL,
    setup_policy_version TEXT NOT NULL CHECK (setup_policy_version = 'USA_SETUP_V1'),
    result_artifact_id TEXT NOT NULL,
    derivation_node_id TEXT NOT NULL UNIQUE,
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    venue_id TEXT NOT NULL CHECK (venue_id = 'XNYS'),
    calendar_id TEXT NOT NULL CHECK (calendar_id = 'XNYS_PROVIDED_SESSIONS_V1'),
    price_adjustment TEXT NOT NULL CHECK (price_adjustment = 'RAW'),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (run_id) REFERENCES run(run_id),
    FOREIGN KEY (setup_policy_id) REFERENCES setup_policy(setup_policy_id),
    FOREIGN KEY (result_artifact_id) REFERENCES artifact(artifact_id),
    FOREIGN KEY (derivation_node_id) REFERENCES derivation_node(derivation_node_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE setup_research_lineage (
    setup_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    lineage_role TEXT NOT NULL CHECK (lineage_role = 'PRIMARY'),
    research_derivation_node_id TEXT NOT NULL,
    PRIMARY KEY (setup_id, research_id, lineage_role),
    UNIQUE (setup_id),
    FOREIGN KEY (setup_id) REFERENCES setup(setup_id),
    FOREIGN KEY (research_id) REFERENCES research_record(research_id),
    FOREIGN KEY (research_derivation_node_id) REFERENCES derivation_node(derivation_node_id)
) STRICT;

CREATE INDEX research_record_attempt_idx ON research_record(attempt_id, run_id);
CREATE INDEX llm_interaction_attempt_idx ON llm_interaction(attempt_id, ordinal);
CREATE INDEX setup_attempt_idx ON setup(attempt_id, run_id);

CREATE TRIGGER research_method_insert_authorized BEFORE INSERT ON research_method WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: research_method'); END;
CREATE TRIGGER research_method_no_update BEFORE UPDATE ON research_method BEGIN SELECT RAISE(ABORT, 'immutable record: research_method'); END;
CREATE TRIGGER research_method_no_delete BEFORE DELETE ON research_method BEGIN SELECT RAISE(ABORT, 'immutable record: research_method'); END;
CREATE TRIGGER setup_policy_insert_authorized BEFORE INSERT ON setup_policy WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: setup_policy'); END;
CREATE TRIGGER setup_policy_no_update BEFORE UPDATE ON setup_policy BEGIN SELECT RAISE(ABORT, 'immutable record: setup_policy'); END;
CREATE TRIGGER setup_policy_no_delete BEFORE DELETE ON setup_policy BEGIN SELECT RAISE(ABORT, 'immutable record: setup_policy'); END;
CREATE TRIGGER llm_interaction_insert_authorized BEFORE INSERT ON llm_interaction WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: llm_interaction'); END;
CREATE TRIGGER llm_interaction_no_update BEFORE UPDATE ON llm_interaction BEGIN SELECT RAISE(ABORT, 'immutable record: llm_interaction'); END;
CREATE TRIGGER llm_interaction_no_delete BEFORE DELETE ON llm_interaction BEGIN SELECT RAISE(ABORT, 'immutable record: llm_interaction'); END;
CREATE TRIGGER research_llm_interaction_insert_authorized BEFORE INSERT ON research_llm_interaction WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: research_llm_interaction'); END;
CREATE TRIGGER research_llm_interaction_insert_valid BEFORE INSERT ON research_llm_interaction BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM research_record r JOIN llm_interaction l ON l.attempt_id = r.attempt_id WHERE r.research_id = NEW.research_id AND l.llm_interaction_id = NEW.llm_interaction_id AND l.status = 'SUCCEEDED') THEN RAISE(ABORT, 'invalid Research LLM lineage') END;
END;
CREATE TRIGGER research_llm_interaction_no_update BEFORE UPDATE ON research_llm_interaction BEGIN SELECT RAISE(ABORT, 'immutable record: research_llm_interaction'); END;
CREATE TRIGGER research_llm_interaction_no_delete BEFORE DELETE ON research_llm_interaction BEGIN SELECT RAISE(ABORT, 'immutable record: research_llm_interaction'); END;

CREATE TRIGGER research_record_insert_authorized BEFORE INSERT ON research_record WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: research_record'); END;
CREATE TRIGGER research_record_update_authorized BEFORE UPDATE ON research_record WHEN ledger_run_binding_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'immutable record: research_record'); END;
CREATE TRIGGER research_record_update_valid BEFORE UPDATE ON research_record WHEN ledger_run_binding_authorized() = 1 BEGIN
    SELECT CASE WHEN OLD.run_id IS NOT NULL OR NEW.run_id IS NULL OR NEW.research_id <> OLD.research_id OR NEW.attempt_id <> OLD.attempt_id OR NEW.research_kind <> OLD.research_kind OR NEW.subject_key <> OLD.subject_key OR NEW.content_artifact_id <> OLD.content_artifact_id OR NEW.research_method_id <> OLD.research_method_id OR NEW.method_version <> OLD.method_version OR NEW.as_of_at <> OLD.as_of_at OR NEW.derivation_node_id <> OLD.derivation_node_id OR NEW.supersedes_research_id IS NOT OLD.supersedes_research_id OR NEW.created_at <> OLD.created_at OR NOT EXISTS (SELECT 1 FROM run WHERE run_id = NEW.run_id AND attempt_id = NEW.attempt_id) THEN RAISE(ABORT, 'invalid Research run binding') END;
END;
CREATE TRIGGER research_record_no_delete BEFORE DELETE ON research_record BEGIN SELECT RAISE(ABORT, 'immutable record: research_record'); END;

CREATE TRIGGER setup_insert_authorized BEFORE INSERT ON setup WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: setup'); END;
CREATE TRIGGER setup_update_authorized BEFORE UPDATE ON setup WHEN ledger_run_binding_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'immutable record: setup'); END;
CREATE TRIGGER setup_update_valid BEFORE UPDATE ON setup WHEN ledger_run_binding_authorized() = 1 BEGIN
    SELECT CASE WHEN OLD.run_id IS NOT NULL OR NEW.run_id IS NULL OR NEW.setup_id <> OLD.setup_id OR NEW.attempt_id <> OLD.attempt_id OR NEW.instrument_id <> OLD.instrument_id OR NEW.direction <> OLD.direction OR NEW.setup_kind <> OLD.setup_kind OR NEW.signal_at <> OLD.signal_at OR NEW.entry_rule_json <> OLD.entry_rule_json OR NEW.stop_rule_json <> OLD.stop_rule_json OR NEW.target_rule_json <> OLD.target_rule_json OR NEW.valid_from IS NOT OLD.valid_from OR NEW.expires_at IS NOT OLD.expires_at OR NEW.invalidates_at IS NOT OLD.invalidates_at OR NEW.setup_policy_id <> OLD.setup_policy_id OR NEW.setup_policy_version <> OLD.setup_policy_version OR NEW.result_artifact_id <> OLD.result_artifact_id OR NEW.derivation_node_id <> OLD.derivation_node_id OR NEW.currency <> OLD.currency OR NEW.venue_id <> OLD.venue_id OR NEW.calendar_id <> OLD.calendar_id OR NEW.price_adjustment <> OLD.price_adjustment OR NEW.created_at <> OLD.created_at OR NOT EXISTS (SELECT 1 FROM run WHERE run_id = NEW.run_id AND attempt_id = NEW.attempt_id) THEN RAISE(ABORT, 'invalid Setup run binding') END;
END;
CREATE TRIGGER setup_no_delete BEFORE DELETE ON setup BEGIN SELECT RAISE(ABORT, 'immutable record: setup'); END;

CREATE TRIGGER setup_research_lineage_insert_authorized BEFORE INSERT ON setup_research_lineage WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: setup_research_lineage'); END;
CREATE TRIGGER setup_research_lineage_insert_valid BEFORE INSERT ON setup_research_lineage BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM setup s JOIN research_record r ON r.attempt_id = s.attempt_id JOIN derivation_node n ON n.derivation_node_id = NEW.research_derivation_node_id AND n.node_kind = 'RESEARCH' AND n.entity_type = 'research_record' AND n.entity_id = r.research_id AND n.attempt_id = s.attempt_id WHERE s.setup_id = NEW.setup_id AND r.research_id = NEW.research_id) THEN RAISE(ABORT, 'invalid Setup Research lineage') END;
END;
CREATE TRIGGER setup_research_lineage_no_update BEFORE UPDATE ON setup_research_lineage BEGIN SELECT RAISE(ABORT, 'immutable record: setup_research_lineage'); END;
CREATE TRIGGER setup_research_lineage_no_delete BEFORE DELETE ON setup_research_lineage BEGIN SELECT RAISE(ABORT, 'immutable record: setup_research_lineage'); END;

DROP TRIGGER derivation_node_insert_valid;
CREATE TRIGGER derivation_node_insert_valid BEFORE INSERT ON derivation_node BEGIN
    SELECT CASE WHEN NEW.run_id IS NOT NULL THEN RAISE(ABORT, 'new derivation node must be attempt-local') END;
    SELECT CASE WHEN NEW.node_kind = 'INPUT_OBSERVATION' AND NOT EXISTS (SELECT 1 FROM input_observation WHERE input_observation_id = NEW.entity_id AND effective_available_at = NEW.direct_available_at AND effective_available_at = NEW.derived_available_at) THEN RAISE(ABORT, 'invalid input-observation derivation node') END;
    SELECT CASE WHEN NEW.node_kind = 'INPUT_OBSERVATION' AND EXISTS (SELECT 1 FROM derivation_edge WHERE child_node_id = NEW.derivation_node_id) THEN RAISE(ABORT, 'raw derivation node cannot have parents') END;
    SELECT CASE WHEN NEW.node_kind = 'NORMALIZED_FACT' AND NOT EXISTS (SELECT 1 FROM artifact WHERE artifact_id = NEW.entity_id) THEN RAISE(ABORT, 'normalized fact requires artifact target') END;
    SELECT CASE WHEN NEW.node_kind = 'RESEARCH' AND NOT EXISTS (SELECT 1 FROM research_record WHERE research_id = NEW.entity_id AND attempt_id = NEW.attempt_id AND derivation_node_id = NEW.derivation_node_id AND run_id IS NULL) THEN RAISE(ABORT, 'Research node requires same-attempt target') END;
    SELECT CASE WHEN NEW.node_kind = 'SETUP' AND NOT EXISTS (SELECT 1 FROM setup WHERE setup_id = NEW.entity_id AND attempt_id = NEW.attempt_id AND derivation_node_id = NEW.derivation_node_id AND run_id IS NULL) THEN RAISE(ABORT, 'Setup node requires same-attempt target') END;
    SELECT CASE WHEN NEW.node_kind IN ('NORMALIZED_FACT','RESEARCH','SETUP') AND NOT EXISTS (SELECT 1 FROM derivation_edge WHERE child_node_id = NEW.derivation_node_id AND required = 1) THEN RAISE(ABORT, 'derived node requires a required parent') END;
    SELECT CASE WHEN NEW.node_kind IN ('NORMALIZED_FACT','RESEARCH','SETUP') AND EXISTS (SELECT 1 FROM derivation_edge e LEFT JOIN derivation_node p ON p.derivation_node_id = e.parent_node_id WHERE e.child_node_id = NEW.derivation_node_id AND (p.derivation_node_id IS NULL OR p.attempt_id <> NEW.attempt_id OR p.run_id IS NOT NULL)) THEN RAISE(ABORT, 'derived parents must share attempt-local lineage') END;
    SELECT CASE WHEN NEW.node_kind IN ('RESEARCH','SETUP') AND EXISTS (SELECT 1 FROM derivation_node n WHERE n.node_kind = NEW.node_kind AND n.entity_type = NEW.entity_type AND n.entity_id = NEW.entity_id) THEN RAISE(ABORT, 'derivation target already represented') END;
    SELECT CASE WHEN NEW.node_kind IN ('ELIGIBILITY','OUTCOME') THEN RAISE(ABORT, 'reserved derivation node kind is not instantiable') END;
END;

DROP TRIGGER run_insert_valid;
CREATE TRIGGER run_insert_valid BEFORE INSERT ON run BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM attempt WHERE attempt_id = NEW.attempt_id AND run_request_id = NEW.run_request_id AND status = 'RUNNING') THEN RAISE(ABORT, 'committed run requires matching running attempt') END;
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM artifact WHERE artifact_id = NEW.result_manifest_artifact_id AND artifact_kind IN ('ledger.run-result-manifest.v1','ledger.run-result-manifest.v2','ledger.run-result-manifest.v3') AND canonicalization_version = 'JCS-LEDGER-SUBSET-V1') THEN RAISE(ABORT, 'committed run requires canonical result manifest') END;
END;
