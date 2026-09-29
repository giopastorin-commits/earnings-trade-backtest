"""TRINITY Ledger Foundation V1 core.

Importing this package never opens or creates a database. Call
``LedgerStorage.open(path)`` explicitly.
"""

from .canonical import (
    CANONICAL_JSON_V1,
    IDENTITY,
    canonicalize_json,
    canonicalize_json_document,
    canonicalize_opaque,
)
from .errors import (
    ArtifactIntegrityError,
    ArtifactMetadataConflict,
    CanonicalizationError,
    FinalizationConflict,
    InvalidAttemptTransition,
    ImmutableRecordViolation,
    LedgerError,
    MigrationHashDrift,
    MigrationHistoryError,
    MigrationIdentityDrift,
    MigrationIntegrityError,
    RequestIdempotencyConflict,
    StaleAttemptError,
    UnknownAppliedMigration,
    UnsupportedCanonicalValue,
    UnsupportedRunInput,
    UnsupportedSchemaVersion,
)
from .storage import (
    Artifact,
    Attempt,
    AttemptEvent,
    LedgerStorage,
    Run,
    RunRequest,
    artifact_id_for,
    artifact_preimage,
)

__all__ = [
    "Artifact",
    "ArtifactIntegrityError",
    "ArtifactMetadataConflict",
    "Attempt",
    "AttemptEvent",
    "CANONICAL_JSON_V1",
    "CanonicalizationError",
    "FinalizationConflict",
    "IDENTITY",
    "ImmutableRecordViolation",
    "InvalidAttemptTransition",
    "LedgerError",
    "LedgerStorage",
    "MigrationHashDrift",
    "MigrationHistoryError",
    "MigrationIdentityDrift",
    "MigrationIntegrityError",
    "RequestIdempotencyConflict",
    "Run",
    "RunRequest",
    "StaleAttemptError",
    "UnsupportedCanonicalValue",
    "UnsupportedRunInput",
    "UnsupportedSchemaVersion",
    "UnknownAppliedMigration",
    "artifact_id_for",
    "artifact_preimage",
    "canonicalize_json",
    "canonicalize_json_document",
    "canonicalize_opaque",
]
