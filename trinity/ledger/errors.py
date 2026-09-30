"""Explicit failures raised by the Ledger V1 core."""


class LedgerError(Exception):
    """Base class for Ledger failures."""


class CanonicalizationError(LedgerError):
    """Structured input cannot be canonicalized safely."""


class UnsupportedCanonicalValue(CanonicalizationError):
    """A value is outside the Ledger-supported canonical JSON subset."""


class ArtifactIntegrityError(LedgerError):
    """Stored or claimed artifact content does not match its identity."""


class ArtifactMetadataConflict(LedgerError):
    """A content address is already bound to incompatible immutable metadata."""


class MigrationIntegrityError(LedgerError):
    """Applied migration history is inconsistent with the ordered registry."""


class MigrationHashDrift(MigrationIntegrityError):
    """An applied migration no longer has its recorded SHA-256."""


class MigrationIdentityDrift(MigrationIntegrityError):
    """A migration sequence is bound to an unexpected stable identifier."""


class MigrationHistoryError(MigrationIntegrityError):
    """Migration history or registry ordering is invalid."""


class UnknownAppliedMigration(MigrationIntegrityError):
    """The database contains a migration unknown to this writer."""


class ImmutableRecordViolation(LedgerError):
    """An immutable Ledger record was targeted by UPDATE or DELETE."""


class UnsupportedSchemaVersion(LedgerError):
    """The database schema is not supported by this Ledger writer."""


class RequestIdempotencyConflict(LedgerError):
    """A caller-scoped key was reused for different logical work."""


class StaleAttemptError(LedgerError):
    """An attempt no longer owns current write authority."""


class InvalidAttemptTransition(LedgerError):
    """An attempt lifecycle transition is not permitted."""


class FinalizationConflict(LedgerError):
    """A successful run was repeated with incompatible closure identity."""


class UnsupportedRunInput(LedgerError):
    """A run input requires a deferred observation or derivation entity."""


class UnsupportedAvailabilityBasis(LedgerError):
    """An availability basis is frozen but operationally deferred."""


class InvalidObservationProvenance(LedgerError):
    """Input-observation provenance violates the frozen contract."""
