PRAGMA defer_foreign_keys = ON;
PRAGMA legacy_alter_table = ON;

DROP TRIGGER research_method_insert_authorized;
DROP TRIGGER research_method_no_update;
DROP TRIGGER research_method_no_delete;
DROP TRIGGER research_record_insert_authorized;
DROP TRIGGER research_record_update_authorized;
DROP TRIGGER research_record_update_valid;
DROP TRIGGER research_record_no_delete;
DROP TRIGGER research_llm_interaction_insert_authorized;
DROP TRIGGER research_llm_interaction_insert_valid;
DROP TRIGGER research_llm_interaction_no_update;
DROP TRIGGER research_llm_interaction_no_delete;
DROP TRIGGER setup_research_lineage_insert_authorized;
DROP TRIGGER setup_research_lineage_insert_valid;
DROP TRIGGER setup_research_lineage_no_update;
DROP TRIGGER setup_research_lineage_no_delete;
DROP TRIGGER derivation_node_insert_valid;

ALTER TABLE research_method RENAME TO research_method_v6;

CREATE TABLE research_method (
    research_method_id TEXT PRIMARY KEY CHECK (research_method_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    research_kind TEXT NOT NULL CHECK (research_kind = 'TRINITY_USA_RESEARCH'),
    method_version TEXT NOT NULL CHECK (method_version IN ('USA_V2','USA_V2_FACTS_V3')),
    definition_artifact_id TEXT NOT NULL,
    code_commit TEXT NOT NULL CHECK (length(code_commit) = 40 AND code_commit NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND created_at GLOB '????-??-??T??:??:??.??????Z'),
    UNIQUE (research_kind, method_version),
    FOREIGN KEY (definition_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;

INSERT INTO research_method
SELECT research_method_id, research_kind, method_version, definition_artifact_id,
       code_commit, created_at
FROM research_method_v6;

ALTER TABLE research_record RENAME TO research_record_v6;

CREATE TABLE research_record (
    research_id TEXT PRIMARY KEY CHECK (research_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    attempt_id TEXT NOT NULL,
    run_id TEXT,
    research_kind TEXT NOT NULL CHECK (research_kind = 'TRINITY_USA_RESEARCH'),
    subject_key TEXT NOT NULL CHECK (length(subject_key) > 0),
    content_artifact_id TEXT NOT NULL,
    research_method_id TEXT NOT NULL,
    method_version TEXT NOT NULL CHECK (method_version IN ('USA_V2','USA_V2_FACTS_V3')),
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

INSERT INTO research_record
SELECT research_id, attempt_id, run_id, research_kind, subject_key,
       content_artifact_id, research_method_id, method_version, as_of_at,
       derivation_node_id, supersedes_research_id, created_at
FROM research_record_v6;

ALTER TABLE research_llm_interaction RENAME TO research_llm_interaction_v6;

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

INSERT INTO research_llm_interaction
SELECT research_id, llm_interaction_id, interaction_role, ordinal
FROM research_llm_interaction_v6;

ALTER TABLE setup_research_lineage RENAME TO setup_research_lineage_v6;

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

INSERT INTO setup_research_lineage
SELECT setup_id, research_id, lineage_role, research_derivation_node_id
FROM setup_research_lineage_v6;

DROP TABLE setup_research_lineage_v6;
DROP TABLE research_llm_interaction_v6;
DROP TABLE research_record_v6;
DROP TABLE research_method_v6;

CREATE INDEX research_record_attempt_idx ON research_record(attempt_id, run_id);

CREATE TRIGGER research_method_insert_authorized BEFORE INSERT ON research_method WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: research_method'); END;
CREATE TRIGGER research_method_no_update BEFORE UPDATE ON research_method BEGIN SELECT RAISE(ABORT, 'immutable record: research_method'); END;
CREATE TRIGGER research_method_no_delete BEFORE DELETE ON research_method BEGIN SELECT RAISE(ABORT, 'immutable record: research_method'); END;

CREATE TRIGGER research_record_insert_authorized BEFORE INSERT ON research_record WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: research_record'); END;
CREATE TRIGGER research_record_update_authorized BEFORE UPDATE ON research_record WHEN ledger_run_binding_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'immutable record: research_record'); END;
CREATE TRIGGER research_record_update_valid BEFORE UPDATE ON research_record WHEN ledger_run_binding_authorized() = 1 BEGIN
    SELECT CASE WHEN OLD.run_id IS NOT NULL OR NEW.run_id IS NULL OR NEW.research_id <> OLD.research_id OR NEW.attempt_id <> OLD.attempt_id OR NEW.research_kind <> OLD.research_kind OR NEW.subject_key <> OLD.subject_key OR NEW.content_artifact_id <> OLD.content_artifact_id OR NEW.research_method_id <> OLD.research_method_id OR NEW.method_version <> OLD.method_version OR NEW.as_of_at <> OLD.as_of_at OR NEW.derivation_node_id <> OLD.derivation_node_id OR NEW.supersedes_research_id IS NOT OLD.supersedes_research_id OR NEW.created_at <> OLD.created_at OR NOT EXISTS (SELECT 1 FROM run WHERE run_id = NEW.run_id AND attempt_id = NEW.attempt_id) THEN RAISE(ABORT, 'invalid Research run binding') END;
END;
CREATE TRIGGER research_record_no_delete BEFORE DELETE ON research_record BEGIN SELECT RAISE(ABORT, 'immutable record: research_record'); END;

CREATE TRIGGER research_llm_interaction_insert_authorized BEFORE INSERT ON research_llm_interaction WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: research_llm_interaction'); END;
CREATE TRIGGER research_llm_interaction_insert_valid BEFORE INSERT ON research_llm_interaction BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM research_record r JOIN llm_interaction l ON l.attempt_id = r.attempt_id WHERE r.research_id = NEW.research_id AND l.llm_interaction_id = NEW.llm_interaction_id AND l.status = 'SUCCEEDED') THEN RAISE(ABORT, 'invalid Research LLM lineage') END;
END;
CREATE TRIGGER research_llm_interaction_no_update BEFORE UPDATE ON research_llm_interaction BEGIN SELECT RAISE(ABORT, 'immutable record: research_llm_interaction'); END;
CREATE TRIGGER research_llm_interaction_no_delete BEFORE DELETE ON research_llm_interaction BEGIN SELECT RAISE(ABORT, 'immutable record: research_llm_interaction'); END;

CREATE TRIGGER setup_research_lineage_insert_authorized BEFORE INSERT ON setup_research_lineage WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: setup_research_lineage'); END;
CREATE TRIGGER setup_research_lineage_insert_valid BEFORE INSERT ON setup_research_lineage BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM setup s JOIN research_record r ON r.attempt_id = s.attempt_id JOIN derivation_node n ON n.derivation_node_id = NEW.research_derivation_node_id AND n.node_kind = 'RESEARCH' AND n.entity_type = 'research_record' AND n.entity_id = r.research_id AND n.attempt_id = s.attempt_id WHERE s.setup_id = NEW.setup_id AND r.research_id = NEW.research_id) THEN RAISE(ABORT, 'invalid Setup Research lineage') END;
END;
CREATE TRIGGER setup_research_lineage_no_update BEFORE UPDATE ON setup_research_lineage BEGIN SELECT RAISE(ABORT, 'immutable record: setup_research_lineage'); END;
CREATE TRIGGER setup_research_lineage_no_delete BEFORE DELETE ON setup_research_lineage BEGIN SELECT RAISE(ABORT, 'immutable record: setup_research_lineage'); END;

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

CREATE TEMP TABLE research_method_versions_fk_check (
    violation_count INTEGER NOT NULL CHECK (violation_count = 0)
);
INSERT INTO research_method_versions_fk_check
SELECT count(*) FROM pragma_foreign_key_check;
DROP TABLE research_method_versions_fk_check;

PRAGMA legacy_alter_table = OFF;
