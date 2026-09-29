CREATE TABLE schema_migration (
    migration_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL UNIQUE CHECK (schema_version > 0),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    applied_at TEXT NOT NULL CHECK (
        length(applied_at) = 27
        AND applied_at GLOB '????-??-??T??:??:??.??????Z'
    )
) STRICT;

CREATE TABLE schema_metadata (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    freeze_identifier TEXT NOT NULL CHECK (
        freeze_identifier = 'LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.0.0'
    ),
    created_at TEXT NOT NULL CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    applied_migration_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (applied_migration_id)
        REFERENCES schema_migration(migration_id)
) STRICT;

CREATE TABLE artifact (
    artifact_id TEXT PRIMARY KEY CHECK (
        length(artifact_id) = 71
        AND substr(artifact_id, 1, 7) = 'sha256:'
        AND substr(artifact_id, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
        AND content_hash = substr(artifact_id, 8)
    ),
    byte_length INTEGER NOT NULL CHECK (
        byte_length >= 0 AND byte_length = length(payload)
    ),
    media_type TEXT NOT NULL CHECK (length(media_type) > 0),
    storage_uri TEXT NOT NULL UNIQUE CHECK (
        storage_uri = 'sqlite:artifact:' || artifact_id
    ),
    created_at TEXT NOT NULL CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
    ),
    artifact_kind TEXT NOT NULL CHECK (
        length(artifact_kind) > 0 AND instr(artifact_kind, char(0)) = 0
    ),
    canonicalization_version TEXT NOT NULL CHECK (
        length(canonicalization_version) > 0
        AND instr(canonicalization_version, char(0)) = 0
    ),
    payload BLOB NOT NULL,
    UNIQUE (sha256, byte_length)
) STRICT;

CREATE TRIGGER schema_migration_no_update
BEFORE UPDATE ON schema_migration
BEGIN
    SELECT RAISE(ABORT, 'immutable record: schema_migration');
END;

CREATE TRIGGER schema_migration_no_delete
BEFORE DELETE ON schema_migration
BEGIN
    SELECT RAISE(ABORT, 'immutable record: schema_migration');
END;

CREATE TRIGGER schema_metadata_no_update
BEFORE UPDATE ON schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'immutable record: schema_metadata');
END;

CREATE TRIGGER schema_metadata_no_delete
BEFORE DELETE ON schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'immutable record: schema_metadata');
END;

CREATE TRIGGER artifact_no_update
BEFORE UPDATE ON artifact
BEGIN
    SELECT RAISE(ABORT, 'immutable record: artifact');
END;

CREATE TRIGGER artifact_no_delete
BEFORE DELETE ON artifact
BEGIN
    SELECT RAISE(ABORT, 'immutable record: artifact');
END;
