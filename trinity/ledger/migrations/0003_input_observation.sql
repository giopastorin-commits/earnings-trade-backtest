CREATE TABLE input_observation (
    input_observation_id TEXT PRIMARY KEY CHECK (length(input_observation_id) > 0),
    artifact_id TEXT NOT NULL,
    source_id TEXT NOT NULL CHECK (
        length(source_id) > 0
        AND source_id = lower(source_id)
        AND source_id NOT GLOB '*[^a-z0-9._:-]*'
        AND source_id NOT LIKE ':%'
        AND source_id NOT LIKE '%:'
        AND source_id NOT LIKE '%::%'
        AND (length(source_id) - length(replace(source_id, ':', ''))) IN (1, 2)
    ),
    source_record_key TEXT NOT NULL CHECK (
        length(source_record_key) > 0 AND instr(source_record_key, char(0)) = 0
    ),
    source_published_at TEXT CHECK (
        source_published_at IS NULL OR (
            length(source_published_at) = 27
            AND source_published_at GLOB '????-??-??T??:??:??.??????Z'
        )
    ),
    retrieved_at TEXT NOT NULL CHECK (
        length(retrieved_at) = 27
        AND retrieved_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    availability_basis TEXT NOT NULL CHECK (
        availability_basis IN (
            'SOURCE_PUBLISHED_AT',
            'RETRIEVED_AT_FALLBACK',
            'LEGACY_ASSERTED_AT'
        )
    ),
    effective_available_at TEXT NOT NULL CHECK (
        length(effective_available_at) = 27
        AND effective_available_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    observed_by_attempt_id TEXT NOT NULL,
    source_metadata_json TEXT NOT NULL CHECK (json_valid(source_metadata_json)),
    CHECK (
        availability_basis <> 'RETRIEVED_AT_FALLBACK'
        OR (source_published_at IS NULL AND effective_available_at = retrieved_at)
    ),
    CHECK (
        availability_basis <> 'SOURCE_PUBLISHED_AT'
        OR source_published_at IS NOT NULL
    ),
    FOREIGN KEY (artifact_id) REFERENCES artifact(artifact_id),
    FOREIGN KEY (observed_by_attempt_id) REFERENCES attempt(attempt_id)
) STRICT;

CREATE INDEX input_observation_artifact_idx ON input_observation(artifact_id);
CREATE INDEX input_observation_source_record_idx
ON input_observation(source_id, source_record_key, retrieved_at);
CREATE INDEX input_observation_attempt_idx
ON input_observation(observed_by_attempt_id);

CREATE TRIGGER input_observation_insert_authorized
BEFORE INSERT ON input_observation
WHEN ledger_internal_write_authorized() <> 1
BEGIN
    SELECT RAISE(ABORT, 'authoritative write required: input_observation');
END;

CREATE TRIGGER input_observation_no_update
BEFORE UPDATE ON input_observation
BEGIN
    SELECT RAISE(ABORT, 'immutable record: input_observation');
END;

CREATE TRIGGER input_observation_no_delete
BEFORE DELETE ON input_observation
BEGIN
    SELECT RAISE(ABORT, 'immutable record: input_observation');
END;
