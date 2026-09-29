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
    ImmutableRecordViolation,
    LedgerError,
    MigrationHashDrift,
    MigrationHistoryError,
    MigrationIdentityDrift,
    MigrationIntegrityError,
    UnknownAppliedMigration,
    UnsupportedCanonicalValue,
    UnsupportedSchemaVersion,
)
from .storage import Artifact, LedgerStorage, artifact_id_for, artifact_preimage

__all__ = [
    "Artifact",
    "ArtifactIntegrityError",
    "ArtifactMetadataConflict",
    "CANONICAL_JSON_V1",
    "CanonicalizationError",
    "IDENTITY",
    "ImmutableRecordViolation",
    "LedgerError",
    "LedgerStorage",
    "MigrationHashDrift",
    "MigrationHistoryError",
    "MigrationIdentityDrift",
    "MigrationIntegrityError",
    "UnsupportedCanonicalValue",
    "UnsupportedSchemaVersion",
    "UnknownAppliedMigration",
    "artifact_id_for",
    "artifact_preimage",
    "canonicalize_json",
    "canonicalize_json_document",
    "canonicalize_opaque",
]
