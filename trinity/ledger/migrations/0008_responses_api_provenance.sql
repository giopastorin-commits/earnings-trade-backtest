DROP TRIGGER research_llm_interaction_insert_valid;

CREATE TABLE llm_interaction_v2 (
    llm_interaction_id TEXT PRIMARY KEY CHECK (llm_interaction_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    attempt_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
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
    CHECK ((provider = 'OPENAI_CODEX_CLI' AND model = 'gpt-5.6-sol' AND model_version = 'codex-cli:gpt-5.6-sol') OR (provider = 'OPENAI_RESPONSES_API' AND model = 'gpt-5.6-sol' AND model_version = 'responses-api:gpt-5.6-sol')),
    CHECK ((status = 'SUCCEEDED' AND response_artifact_id IS NOT NULL AND error_artifact_id IS NULL) OR (status = 'FAILED' AND response_artifact_id IS NULL AND error_artifact_id IS NOT NULL)),
    FOREIGN KEY (attempt_id) REFERENCES attempt(attempt_id),
    FOREIGN KEY (request_artifact_id) REFERENCES artifact(artifact_id),
    FOREIGN KEY (response_artifact_id) REFERENCES artifact(artifact_id),
    FOREIGN KEY (error_artifact_id) REFERENCES artifact(artifact_id)
) STRICT;
INSERT INTO llm_interaction_v2 SELECT * FROM llm_interaction;
DROP TABLE llm_interaction;
ALTER TABLE llm_interaction_v2 RENAME TO llm_interaction;
CREATE INDEX llm_interaction_attempt_idx ON llm_interaction(attempt_id, ordinal);
CREATE TRIGGER llm_interaction_insert_authorized BEFORE INSERT ON llm_interaction WHEN ledger_internal_write_authorized() <> 1 BEGIN SELECT RAISE(ABORT, 'authoritative write required: llm_interaction'); END;
CREATE TRIGGER llm_interaction_no_update BEFORE UPDATE ON llm_interaction BEGIN SELECT RAISE(ABORT, 'immutable record: llm_interaction'); END;
CREATE TRIGGER llm_interaction_no_delete BEFORE DELETE ON llm_interaction BEGIN SELECT RAISE(ABORT, 'immutable record: llm_interaction'); END;
CREATE TRIGGER research_llm_interaction_insert_valid BEFORE INSERT ON research_llm_interaction BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM research_record r JOIN llm_interaction l ON l.attempt_id = r.attempt_id WHERE r.research_id = NEW.research_id AND l.llm_interaction_id = NEW.llm_interaction_id AND l.status = 'SUCCEEDED') THEN RAISE(ABORT, 'invalid Research LLM lineage') END;
END;
