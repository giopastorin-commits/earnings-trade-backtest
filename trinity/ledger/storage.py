"""SQLite storage for Ledger artifacts and V1.1 execution coordination."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .canonical import (
    CANONICAL_JSON_V1,
    IDENTITY,
    canonicalize_json,
    canonicalize_json_document,
    canonicalize_opaque,
)
from .contracts_v14 import (
    RESEARCH_METHOD_DEFINITION,
    SETUP_POLICY_DEFINITION,
    analyst_response_schema,
    critic_response_schema,
    setup_rule_values,
    validate_effective_prompt,
    validate_json_artifact,
)
from .errors import (
    ArtifactIntegrityError,
    ArtifactMetadataConflict,
    CanonicalizationError,
    ClassificationIntegrityError,
    DerivationIntegrityError,
    MigrationHashDrift,
    MigrationHistoryError,
    MigrationIdentityDrift,
    FinalizationConflict,
    InvalidAttemptTransition,
    InvalidObservationProvenance,
    RequestIdempotencyConflict,
    StaleAttemptError,
    UnsupportedAvailabilityBasis,
    UnsupportedClassificationPolicy,
    UnsupportedDerivationPolicy,
    UnsupportedRunInput,
    UnknownAppliedMigration,
    UnsupportedSchemaVersion,
)
from .schema import (
    FREEZE_IDENTIFIER,
    SCHEMA_VERSION,
    Migration,
    MigrationRegistry,
    migration_registry,
)

HASH_DOMAIN = b"TRINITY-LEDGER-V1\0"
_ARTIFACT_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SOURCE_ID_RE = re.compile(
    r"[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)?\Z"
)
_SOURCE_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "provider",
        "dataset_name",
        "source_record_key",
        "acquisition_method",
        "availability_rule_id",
        "availability_rule_version",
        "evidence_artifact_ids",
        "provider_metadata",
        "future_effective_at",
    }
)
MAX_REQUIRED_PARENTS_V1 = "MAX_REQUIRED_PARENTS_V1"
REQUIRED_ANCESTRY_PIT_V1 = "REQUIRED_ANCESTRY_PIT_V1"
REQUIRED_ANCESTRY_PIT_V2 = "REQUIRED_ANCESTRY_PIT_V2"
_SUPPORTED_NODE_MAPPINGS = {
    "INPUT_OBSERVATION": "input_observation",
    "NORMALIZED_FACT": "artifact",
    "RESEARCH": "research_record",
    "SETUP": "setup",
}
_RESERVED_NODE_KINDS = {"ELIGIBILITY", "OUTCOME"}
_POLICY_ARTIFACT_KIND = "ledger.pit-classification-policy-definition.v1"
_EVIDENCE_ARTIFACT_KIND = "ledger.pit-classification-evidence.v1"
_MANIFEST_V2_KIND = "ledger.run-result-manifest.v2"
_MANIFEST_V3_KIND = "ledger.run-result-manifest.v3"
_CLASSIFIED_REQUEST_KINDS = {
    "PIT_SAFE_DECISION", "RECONSTRUCTION", "EXPLORATORY_NON_PIT"
}
_EVIDENCE_KEYS = {
    "schema_name", "schema_version", "derivation_node_id",
    "input_observation_id", "artifact_id", "pit_reference_at",
    "record_origin_evidence_kind", "legacy_namespace", "legacy_record_key",
    "legacy_record_id", "pit_evidence_kind", "verifier_id",
    "archive_captured_at", "reconstruction_completed_at",
    "supporting_artifact_ids", "rationale_code",
}


def _pit_policy_definition_v1() -> dict[str, Any]:
    """Return the exact frozen REQUIRED_ANCESTRY_PIT_V1 definition."""
    return {
        "schema_name": "ledger.pit-classification-policy-definition",
        "schema_version": "1",
        "policy_kind": "PIT_CLASSIFICATION",
        "policy_version": REQUIRED_ANCESTRY_PIT_V1,
        "record_classes": ["LEDGER_NATIVE", "LEGACY_NON_LEDGER_ARTIFACT"],
        "pit_classes": ["ARCHIVED_POINT_IN_TIME", "NOT_APPLICABLE", "RECONSTRUCTED_NOT_ARCHIVED", "UNKNOWN"],
        "classification_bases": ["RAW_ARCHIVE_EVIDENCE", "RAW_NOT_APPLICABLE_EVIDENCE", "RAW_RECONSTRUCTION_EVIDENCE", "RAW_UNKNOWN_EVIDENCE", "REQUIRED_PARENT_PROPAGATION"],
        "record_origin_evidence_kinds": ["LEDGER_AUTHORIZED_CAPTURE", "LEGACY_IMPORT"],
        "pit_evidence_kinds": ["INDEPENDENT_ARCHIVE_PROOF", "INSUFFICIENT_PIT_EVIDENCE", "LEDGER_CONTEMPORANEOUS_CAPTURE", "PIT_IRRELEVANT_CONTENT", "POST_REFERENCE_RECONSTRUCTION"],
        "recognized_verifiers": [
            {"verifier_id": "INDEPENDENT_ARCHIVE_PROOF_V1", "pit_evidence_kind": "INDEPENDENT_ARCHIVE_PROOF"},
            {"verifier_id": "LEDGER_AUTHORIZED_CAPTURE_V1", "pit_evidence_kind": "LEDGER_CONTEMPORANEOUS_CAPTURE"},
            {"verifier_id": "LEDGER_INSUFFICIENT_EVIDENCE_V1", "pit_evidence_kind": "INSUFFICIENT_PIT_EVIDENCE"},
            {"verifier_id": "LEDGER_PIT_IRRELEVANCE_V1", "pit_evidence_kind": "PIT_IRRELEVANT_CONTENT"},
            {"verifier_id": "LEDGER_RECONSTRUCTION_EVIDENCE_V1", "pit_evidence_kind": "POST_REFERENCE_RECONSTRUCTION"},
        ],
        "not_applicable_artifact_kinds": [_POLICY_ARTIFACT_KIND],
        "record_origin_results": [
            {"record_origin_evidence_kind": "LEDGER_AUTHORIZED_CAPTURE", "record_class": "LEDGER_NATIVE"},
            {"record_origin_evidence_kind": "LEGACY_IMPORT", "record_class": "LEGACY_NON_LEDGER_ARTIFACT"},
        ],
        "raw_evidence_results": [
            {"pit_evidence_kind": "INDEPENDENT_ARCHIVE_PROOF", "classification_basis": "RAW_ARCHIVE_EVIDENCE", "pit_class": "ARCHIVED_POINT_IN_TIME", "verifier_id": "INDEPENDENT_ARCHIVE_PROOF_V1"},
            {"pit_evidence_kind": "INSUFFICIENT_PIT_EVIDENCE", "classification_basis": "RAW_UNKNOWN_EVIDENCE", "pit_class": "UNKNOWN", "verifier_id": "LEDGER_INSUFFICIENT_EVIDENCE_V1"},
            {"pit_evidence_kind": "LEDGER_CONTEMPORANEOUS_CAPTURE", "classification_basis": "RAW_ARCHIVE_EVIDENCE", "pit_class": "ARCHIVED_POINT_IN_TIME", "verifier_id": "LEDGER_AUTHORIZED_CAPTURE_V1"},
            {"pit_evidence_kind": "PIT_IRRELEVANT_CONTENT", "classification_basis": "RAW_NOT_APPLICABLE_EVIDENCE", "pit_class": "NOT_APPLICABLE", "verifier_id": "LEDGER_PIT_IRRELEVANCE_V1"},
            {"pit_evidence_kind": "POST_REFERENCE_RECONSTRUCTION", "classification_basis": "RAW_RECONSTRUCTION_EVIDENCE", "pit_class": "RECONSTRUCTED_NOT_ARCHIVED", "verifier_id": "LEDGER_RECONSTRUCTION_EVIDENCE_V1"},
        ],
        "parent_rules": {"include_required_edges": True, "include_optional_edges": False, "require_complete_direct_parent_set": True, "require_same_pit_reference_at": True, "require_same_policy_identity": True},
        "pit_propagation": [
            {"left": "ARCHIVED_POINT_IN_TIME", "right": "ARCHIVED_POINT_IN_TIME", "result": "ARCHIVED_POINT_IN_TIME"},
            {"left": "ARCHIVED_POINT_IN_TIME", "right": "NOT_APPLICABLE", "result": "ARCHIVED_POINT_IN_TIME"},
            {"left": "ARCHIVED_POINT_IN_TIME", "right": "RECONSTRUCTED_NOT_ARCHIVED", "result": "RECONSTRUCTED_NOT_ARCHIVED"},
            {"left": "ARCHIVED_POINT_IN_TIME", "right": "UNKNOWN", "result": "UNKNOWN"},
            {"left": "NOT_APPLICABLE", "right": "NOT_APPLICABLE", "result": "NOT_APPLICABLE"},
            {"left": "NOT_APPLICABLE", "right": "RECONSTRUCTED_NOT_ARCHIVED", "result": "RECONSTRUCTED_NOT_ARCHIVED"},
            {"left": "NOT_APPLICABLE", "right": "UNKNOWN", "result": "UNKNOWN"},
            {"left": "RECONSTRUCTED_NOT_ARCHIVED", "right": "RECONSTRUCTED_NOT_ARCHIVED", "result": "RECONSTRUCTED_NOT_ARCHIVED"},
            {"left": "RECONSTRUCTED_NOT_ARCHIVED", "right": "UNKNOWN", "result": "UNKNOWN"},
            {"left": "UNKNOWN", "right": "UNKNOWN", "result": "UNKNOWN"},
        ],
        "record_class_propagation": [
            {"left": "LEDGER_NATIVE", "right": "LEDGER_NATIVE", "result": "LEDGER_NATIVE"},
            {"left": "LEDGER_NATIVE", "right": "LEGACY_NON_LEDGER_ARTIFACT", "result": "LEGACY_NON_LEDGER_ARTIFACT"},
            {"left": "LEGACY_NON_LEDGER_ARTIFACT", "right": "LEGACY_NON_LEDGER_ARTIFACT", "result": "LEGACY_NON_LEDGER_ARTIFACT"},
        ],
        "supersession_rules": {"chain_scope": ["derivation_node_id", "pit_reference_at"], "first_classification_version": 1, "classification_version_increment": 1, "parallel_policy_heads_allowed": False, "policy_change_supersedes_current_head": True, "implicit_current_policy_allowed_for_commit": False},
        "run_gating": [
            {"request_kind": "EXPLORATORY_NON_PIT", "allowed_resolved_pit_classes": ["ARCHIVED_POINT_IN_TIME", "NOT_APPLICABLE", "RECONSTRUCTED_NOT_ARCHIVED", "UNKNOWN"]},
            {"request_kind": "PIT_SAFE_DECISION", "allowed_resolved_pit_classes": ["ARCHIVED_POINT_IN_TIME", "NOT_APPLICABLE"]},
            {"request_kind": "RECONSTRUCTION", "allowed_resolved_pit_classes": ["ARCHIVED_POINT_IN_TIME", "NOT_APPLICABLE", "RECONSTRUCTED_NOT_ARCHIVED"]},
        ],
    }


def pit_classification_policy_definition_v1() -> dict[str, Any]:
    """Return a fresh value for the frozen V1.3 policy-definition Artifact."""
    return _pit_policy_definition_v1()


def pit_classification_policy_definition_v2() -> dict[str, Any]:
    """Return a fresh value for the frozen V1.4 policy-definition Artifact."""
    value = json.loads(json.dumps(_pit_policy_definition_v1()))
    value["policy_version"] = REQUIRED_ANCESTRY_PIT_V2
    value["not_applicable_artifact_kinds"] = [
        "ledger.llm-raw-response.v1", _POLICY_ARTIFACT_KIND,
    ]
    return value


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    sha256: str
    content_hash: str
    byte_length: int
    media_type: str
    storage_uri: str
    created_at: str
    artifact_kind: str
    canonicalization_version: str
    payload: bytes


@dataclass(frozen=True)
class RunRequest:
    run_request_id: str
    request_kind: str
    requested_at: str
    analysis_cutoff_at: str
    parameters_json: str
    idempotency_key: str
    requested_by: str
    baseline_commit: str


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    run_request_id: str
    attempt_ordinal: int
    fence_token: int
    started_at: str
    finished_at: str | None
    status: str
    worker_identity: str
    code_commit: str
    environment_fingerprint: str
    failure_class: str | None
    failure_message: str | None


@dataclass(frozen=True)
class AttemptEvent:
    attempt_event_id: str
    attempt_id: str
    sequence_number: int
    event_type: str
    event_at: str
    from_status: str | None
    to_status: str
    reason_code: str | None
    failure_class: str | None
    failure_message: str | None
    actor_identity: str
    details_artifact_id: str | None
    created_at: str


@dataclass(frozen=True)
class Run:
    run_id: str
    run_request_id: str
    attempt_id: str
    committed_at: str
    status: str
    result_manifest_artifact_id: str


@dataclass(frozen=True)
class InputObservation:
    input_observation_id: str
    artifact_id: str
    source_id: str
    source_record_key: str
    source_published_at: str | None
    retrieved_at: str
    availability_basis: str
    effective_available_at: str
    observed_by_attempt_id: str
    source_metadata_json: str


@dataclass(frozen=True)
class DerivationNode:
    derivation_node_id: str
    run_id: str | None
    attempt_id: str
    node_kind: str
    entity_type: str
    entity_id: str
    direct_available_at: str | None
    derived_available_at: str
    derivation_policy_version: str


@dataclass(frozen=True)
class DerivationEdge:
    derivation_edge_id: str
    parent_node_id: str
    child_node_id: str
    edge_role: str
    required: int


@dataclass(frozen=True)
class DerivationParent:
    parent_node_id: str
    edge_role: str
    required: bool = True


@dataclass(frozen=True)
class RunInputBinding:
    input_observation_id: str
    input_role: str
    derivation_node_id: str


@dataclass(frozen=True)
class RawArtifactLineage:
    node: DerivationNode
    observation: InputObservation
    artifact: Artifact


@dataclass(frozen=True)
class PitClassificationPolicy:
    classification_policy_id: str
    policy_kind: str
    policy_version: str
    definition_artifact_id: str
    code_commit: str
    created_at: str


@dataclass(frozen=True)
class DerivationNodeClassification:
    derivation_node_classification_id: str
    derivation_node_id: str
    pit_reference_at: str
    classification_version: int
    record_class: str
    resolved_record_class: str
    pit_class: str
    classification_basis: str
    classification_policy_id: str
    evidence_artifact_id: str | None
    classified_by_attempt_id: str
    created_at: str
    supersedes_classification_id: str | None


@dataclass(frozen=True)
class ResearchMethod:
    research_method_id: str
    research_kind: str
    method_version: str
    definition_artifact_id: str
    code_commit: str
    created_at: str


@dataclass(frozen=True)
class LLMInteraction:
    llm_interaction_id: str
    attempt_id: str
    ordinal: int
    provider: str
    model: str
    model_version: str
    request_artifact_id: str
    response_artifact_id: str | None
    error_artifact_id: str | None
    status: str
    started_at: str
    finished_at: str
    input_tokens: int | None
    output_tokens: int | None
    created_at: str


@dataclass(frozen=True)
class ResearchRecord:
    research_id: str
    attempt_id: str
    run_id: str | None
    research_kind: str
    subject_key: str
    content_artifact_id: str
    research_method_id: str
    method_version: str
    as_of_at: str
    derivation_node_id: str
    supersedes_research_id: str | None
    created_at: str


@dataclass(frozen=True)
class SetupPolicy:
    setup_policy_id: str
    policy_kind: str
    policy_version: str
    definition_artifact_id: str
    code_commit: str
    created_at: str


@dataclass(frozen=True)
class Setup:
    setup_id: str
    attempt_id: str
    run_id: str | None
    instrument_id: str
    direction: str
    setup_kind: str
    signal_at: str
    entry_rule_json: str
    stop_rule_json: str
    target_rule_json: str
    valid_from: str | None
    expires_at: str | None
    invalidates_at: str | None
    setup_policy_id: str
    setup_policy_version: str
    result_artifact_id: str
    derivation_node_id: str
    currency: str
    venue_id: str
    calendar_id: str
    price_adjustment: str
    created_at: str


def artifact_preimage(
    artifact_type: str, canonicalization_version: str, payload_bytes: bytes
) -> bytes:
    """Build the frozen, domain-separated artifact hash preimage."""

    for name, value in (
        ("artifact_type", artifact_type),
        ("canonicalization_version", canonicalization_version),
    ):
        if not isinstance(value, str) or not value or "\0" in value:
            raise ArtifactIntegrityError(f"{name} must be non-empty UTF-8 text without NUL")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ArtifactIntegrityError(f"{name} is not valid UTF-8 text") from exc
    if not isinstance(payload_bytes, bytes):
        raise ArtifactIntegrityError("hashed payload must be canonical bytes")
    return b"".join(
        (
            HASH_DOMAIN,
            artifact_type.encode("utf-8"),
            b"\0",
            canonicalization_version.encode("utf-8"),
            b"\0",
            payload_bytes,
        )
    )


def artifact_id_for(
    artifact_type: str, canonicalization_version: str, payload_bytes: bytes
) -> str:
    digest = hashlib.sha256(
        artifact_preimage(artifact_type, canonicalization_version, payload_bytes)
    ).hexdigest()
    return f"sha256:{digest}"


class LedgerStorage:
    """An explicitly opened connection; importing this module has no side effects."""

    def __init__(self, connection: sqlite3.Connection, path: Path):
        self.connection = connection
        self.path = path
        self._internal_write_depth = 0
        self._run_binding_depth = 0
        self._classification_construction_depth = 0
        self.connection.create_function(
            "ledger_internal_write_authorized",
            0,
            lambda: int(self._internal_write_depth > 0),
        )
        self.connection.create_function(
            "ledger_run_binding_authorized",
            0,
            lambda: int(self._run_binding_depth > 0),
        )
        self.connection.create_function(
            "ledger_classification_construction_authorized", 0,
            lambda: int(self._classification_construction_depth > 0),
        )

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        registry: MigrationRegistry | None = None,
    ) -> "LedgerStorage":
        database_path = Path(path)
        connection = sqlite3.connect(database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            _configure_connection(connection)
            storage = cls(connection, database_path)
            selected_registry = registry if registry is not None else migration_registry()
            storage.apply_migrations(selected_registry)
            return storage
        except Exception:
            connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LedgerStorage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            yield self.connection
        else:
            with self.transaction() as connection:
                yield connection

    @contextmanager
    def _internal_write(self) -> Iterator[None]:
        self._internal_write_depth += 1
        try:
            yield
        finally:
            self._internal_write_depth -= 1

    @contextmanager
    def _run_binding(self) -> Iterator[None]:
        self._run_binding_depth += 1
        try:
            yield
        finally:
            self._run_binding_depth -= 1

    @contextmanager
    def _classification_construction(self) -> Iterator[None]:
        self._classification_construction_depth += 1
        try:
            yield
        finally:
            self._classification_construction_depth -= 1

    def apply_migration(self, migration: Migration) -> bool:
        """Compatibility helper for a one-entry bootstrap registry."""

        return self.apply_migrations(MigrationRegistry((migration,))) > 0

    def apply_migrations(self, registry: MigrationRegistry) -> int:
        """Validate complete history, then apply pending migrations in order."""

        applied = self._read_migration_history()
        _validate_migration_history(applied, registry)
        if applied:
            self._validate_schema(registry.bootstrap)

        applied_count = 0
        for migration in registry.migrations[len(applied) :]:
            self._apply_pending_migration(migration, bootstrap=(migration.sequence == 1))
            applied_count += 1

        final_history = self._read_migration_history()
        _validate_migration_history(final_history, registry)
        self._validate_schema(registry.bootstrap)
        return applied_count

    def current_migration_level(self) -> int:
        """Derive the current level from immutable migration history."""

        history = self._read_migration_history()
        return int(history[-1]["schema_version"]) if history else 0

    def _apply_pending_migration(self, migration: Migration, *, bootstrap: bool) -> None:
        applied_at = _utc_now()
        statements = [
            "BEGIN IMMEDIATE;",
            migration.text,
            "INSERT INTO schema_migration "
            "(migration_id, schema_version, sha256, applied_at) VALUES "
            f"({_sql_literal(migration.migration_id)}, {migration.sequence}, "
            f"{_sql_literal(migration.sha256)}, {_sql_literal(applied_at)});",
        ]
        if bootstrap:
            statements.append(
                "INSERT INTO schema_metadata "
                "(singleton_id, schema_version, freeze_identifier, created_at, "
                "applied_migration_id) VALUES "
                f"(1, {SCHEMA_VERSION}, {_sql_literal(FREEZE_IDENTIFIER)}, "
                f"{_sql_literal(applied_at)}, {_sql_literal(migration.migration_id)});"
            )
        statements.append("COMMIT;")

        try:
            self.connection.executescript("\n".join(statements))
        except sqlite3.DatabaseError:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def insert_artifact(
        self,
        *,
        artifact_type: str,
        canonicalization_version: str,
        payload: bytes,
        media_type: str,
        claimed_artifact_id: str | None = None,
    ) -> Artifact:
        canonical_payload = _validate_canonical_payload(canonicalization_version, payload)
        artifact_id = artifact_id_for(
            artifact_type, canonicalization_version, canonical_payload
        )
        if claimed_artifact_id is not None and claimed_artifact_id != artifact_id:
            raise ArtifactIntegrityError(
                f"claimed artifact ID {claimed_artifact_id!r} does not match {artifact_id!r}"
            )
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):  # defensive invariant
            raise ArtifactIntegrityError("computed artifact ID is malformed")

        raw_sha256 = hashlib.sha256(canonical_payload).hexdigest()
        existing = self.connection.execute(
            "SELECT * FROM artifact WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if existing is not None:
            artifact = _artifact_from_row(existing)
            self._assert_artifact_metadata(
                artifact,
                artifact_type=artifact_type,
                canonicalization_version=canonicalization_version,
                media_type=media_type,
            )
            self.verify_artifact(artifact)
            return artifact

        same_bytes = self.connection.execute(
            "SELECT * FROM artifact WHERE sha256 = ? AND byte_length = ?",
            (raw_sha256, len(canonical_payload)),
        ).fetchone()
        if same_bytes is not None:
            raise ArtifactMetadataConflict(
                "identical bytes are already bound to different immutable artifact metadata"
            )

        created_at = _utc_now()
        storage_uri = f"sqlite:artifact:{artifact_id}"
        try:
            with self._write_transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO artifact (
                        artifact_id, sha256, content_hash, byte_length, media_type,
                        storage_uri, created_at, artifact_kind,
                        canonicalization_version, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        raw_sha256,
                        artifact_id.removeprefix("sha256:"),
                        len(canonical_payload),
                        media_type,
                        storage_uri,
                        created_at,
                        artifact_type,
                        canonicalization_version,
                        canonical_payload,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ArtifactMetadataConflict("artifact immutable metadata conflicts") from exc
        return self.get_artifact(artifact_id)

    def insert_json_artifact(
        self, *, artifact_type: str, value: Any, media_type: str = "application/json"
    ) -> Artifact:
        return self.insert_artifact(
            artifact_type=artifact_type,
            canonicalization_version=CANONICAL_JSON_V1,
            payload=canonicalize_json(value),
            media_type=media_type,
        )

    def insert_opaque_artifact(
        self, *, artifact_type: str, payload: bytes, media_type: str
    ) -> Artifact:
        return self.insert_artifact(
            artifact_type=artifact_type,
            canonicalization_version=IDENTITY,
            payload=payload,
            media_type=media_type,
        )

    def get_artifact(self, artifact_id: str, *, verify: bool = True) -> Artifact:
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise ArtifactIntegrityError("artifact ID must be sha256:<64 lowercase hex>")
        row = self.connection.execute(
            "SELECT * FROM artifact WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        artifact = _artifact_from_row(row)
        if verify:
            self.verify_artifact(artifact)
        return artifact

    def create_input_observation(
        self,
        *,
        artifact_id: str,
        source_id: str,
        source_record_key: str,
        source_published_at: str | None,
        retrieved_at: str,
        availability_basis: str,
        effective_available_at: str,
        observed_by_attempt_id: str,
        fence_token: int,
        source_metadata: Any,
    ) -> InputObservation:
        """Insert one immutable, conservatively available observation."""

        if availability_basis != "RETRIEVED_AT_FALLBACK":
            if availability_basis in {"SOURCE_PUBLISHED_AT", "LEGACY_ASSERTED_AT"}:
                raise UnsupportedAvailabilityBasis(
                    f"{availability_basis} is frozen but deferred in Milestone 3A"
                )
            raise InvalidObservationProvenance(
                f"unknown availability basis: {availability_basis!r}"
            )
        _validate_source_id(source_id)
        _require_text("source_record_key", source_record_key)
        _require_timestamp("retrieved_at", retrieved_at)
        _require_timestamp("effective_available_at", effective_available_at)
        if source_published_at is not None:
            raise InvalidObservationProvenance(
                "RETRIEVED_AT_FALLBACK requires source_published_at to be null"
            )
        if effective_available_at != retrieved_at:
            raise InvalidObservationProvenance(
                "RETRIEVED_AT_FALLBACK requires effective_available_at = retrieved_at exactly"
            )
        metadata_json, evidence_ids = _validate_source_metadata(
            source_metadata,
            source_id=source_id,
            source_record_key=source_record_key,
        )
        observation_id = _new_id()
        with self.transaction() as connection, self._internal_write():
            self._require_attempt_authority(observed_by_attempt_id, fence_token)
            self.get_artifact(artifact_id)
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                attached = {
                    row[0]
                    for row in connection.execute(
                        f"""
                        SELECT artifact_id FROM attempt_artifact
                        WHERE attempt_id = ?
                          AND role IN ('DIAGNOSTIC', 'OUTPUT')
                          AND artifact_id IN ({placeholders})
                        """,
                        (observed_by_attempt_id, *evidence_ids),
                    )
                }
                if attached != set(evidence_ids):
                    raise InvalidObservationProvenance(
                        "every evidence artifact must be attached to the observing attempt "
                        "as DIAGNOSTIC or OUTPUT"
                    )
            connection.execute(
                """
                INSERT INTO input_observation (
                    input_observation_id, artifact_id, source_id, source_record_key,
                    source_published_at, retrieved_at, availability_basis,
                    effective_available_at, observed_by_attempt_id, source_metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    artifact_id,
                    source_id,
                    source_record_key,
                    None,
                    retrieved_at,
                    availability_basis,
                    effective_available_at,
                    observed_by_attempt_id,
                    metadata_json,
                ),
            )
        return self.get_input_observation(observation_id)

    def get_input_observation(self, input_observation_id: str) -> InputObservation:
        row = self.connection.execute(
            "SELECT * FROM input_observation WHERE input_observation_id = ?",
            (input_observation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(input_observation_id)
        observation = _input_observation_from_row(row)
        canonical = canonicalize_json_document(observation.source_metadata_json).decode(
            "utf-8"
        )
        if canonical != observation.source_metadata_json:
            raise ArtifactIntegrityError("observation source metadata is not canonical JSON")
        return observation

    def list_input_observations(
        self, *, artifact_id: str | None = None
    ) -> list[InputObservation]:
        if artifact_id is None:
            rows = self.connection.execute(
                "SELECT * FROM input_observation ORDER BY input_observation_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM input_observation
                WHERE artifact_id = ? ORDER BY input_observation_id
                """,
                (artifact_id,),
            ).fetchall()
        return [_input_observation_from_row(row) for row in rows]

    def create_raw_derivation_node(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        input_observation_id: str,
        derivation_node_id: str | None = None,
        derivation_policy_version: str = MAX_REQUIRED_PARENTS_V1,
    ) -> DerivationNode:
        """Create an attempt-local raw node from one authoritative observation."""

        _require_derivation_policy(derivation_policy_version)
        node_id = derivation_node_id or _new_id(excluding={attempt_id})
        _require_text("derivation_node_id", node_id)
        with self.transaction() as connection, self._internal_write():
            self._require_attempt_authority(attempt_id, fence_token)
            observation = self.get_input_observation(input_observation_id)
            connection.execute(
                """
                INSERT INTO derivation_node (
                    derivation_node_id, run_id, attempt_id, node_kind,
                    entity_type, entity_id, direct_available_at,
                    derived_available_at, derivation_policy_version
                ) VALUES (?, NULL, ?, 'INPUT_OBSERVATION', 'input_observation',
                          ?, ?, ?, ?)
                """,
                (
                    node_id,
                    attempt_id,
                    input_observation_id,
                    observation.effective_available_at,
                    observation.effective_available_at,
                    derivation_policy_version,
                ),
            )
        return self.get_derivation_node(node_id)

    def create_normalized_fact_node(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        artifact_id: str,
        parents: tuple[DerivationParent, ...],
        derivation_node_id: str | None = None,
        derivation_policy_version: str = MAX_REQUIRED_PARENTS_V1,
    ) -> DerivationNode:
        """Atomically create one derived Artifact node and its complete edges."""

        _require_derivation_policy(derivation_policy_version)
        node_id = derivation_node_id or _new_id(excluding={attempt_id})
        _require_text("derivation_node_id", node_id)
        normalized_parents = _validate_derivation_parents(parents)
        if not any(parent.required for parent in normalized_parents):
            raise DerivationIntegrityError("derived node requires at least one required parent")

        with self.transaction() as connection, self._internal_write():
            self._require_attempt_authority(attempt_id, fence_token)
            self.get_artifact(artifact_id)
            parent_nodes: dict[str, DerivationNode] = {}
            for parent in normalized_parents:
                if parent.parent_node_id == node_id:
                    raise DerivationIntegrityError("derivation self-edge is forbidden")
                parent_node = self._verify_derivation_node(
                    parent.parent_node_id, states={}, memo={}
                )
                if parent_node.attempt_id != attempt_id or parent_node.run_id is not None:
                    raise DerivationIntegrityError(
                        "derived parents must share the current attempt-local lineage"
                    )
                cycle = connection.execute(
                    """
                    WITH RECURSIVE descendants(node_id) AS (
                        SELECT child_node_id FROM derivation_edge
                        WHERE parent_node_id = ?
                        UNION
                        SELECT edge.child_node_id
                        FROM derivation_edge AS edge
                        JOIN descendants ON edge.parent_node_id = descendants.node_id
                    )
                    SELECT 1 FROM descendants WHERE node_id = ?
                    """,
                    (node_id, parent.parent_node_id),
                ).fetchone()
                if cycle is not None:
                    raise DerivationIntegrityError("proposed derivation edge creates a cycle")
                parent_nodes[parent.parent_node_id] = parent_node

            required_times = [
                parent_nodes[parent.parent_node_id].derived_available_at
                for parent in normalized_parents
                if parent.required
            ]
            derived_available_at = max(required_times)
            for parent in normalized_parents:
                connection.execute(
                    """
                    INSERT INTO derivation_edge (
                        derivation_edge_id, parent_node_id, child_node_id,
                        edge_role, required
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id(excluding={node_id, attempt_id}),
                        parent.parent_node_id,
                        node_id,
                        parent.edge_role,
                        int(parent.required),
                    ),
                )
            connection.execute(
                """
                INSERT INTO derivation_node (
                    derivation_node_id, run_id, attempt_id, node_kind,
                    entity_type, entity_id, direct_available_at,
                    derived_available_at, derivation_policy_version
                ) VALUES (?, NULL, ?, 'NORMALIZED_FACT', 'artifact', ?, NULL, ?, ?)
                """,
                (
                    node_id,
                    attempt_id,
                    artifact_id,
                    derived_available_at,
                    derivation_policy_version,
                ),
            )
        return self.get_derivation_node(node_id)

    def get_derivation_node(
        self, derivation_node_id: str, *, verify: bool = True
    ) -> DerivationNode:
        row = self.connection.execute(
            "SELECT * FROM derivation_node WHERE derivation_node_id = ?",
            (derivation_node_id,),
        ).fetchone()
        if row is None:
            raise KeyError(derivation_node_id)
        node = _derivation_node_from_row(row)
        if verify:
            self.verify_derivation(derivation_node_id)
        return node

    def list_derivation_edges(self, child_node_id: str) -> list[DerivationEdge]:
        rows = self.connection.execute(
            """
            SELECT * FROM derivation_edge
            WHERE child_node_id = ?
            ORDER BY edge_role COLLATE BINARY, parent_node_id COLLATE BINARY
            """,
            (child_node_id,),
        ).fetchall()
        return [_derivation_edge_from_row(row) for row in rows]

    def verify_derivation(self, derivation_node_id: str) -> str:
        """Recompute and verify one complete temporal derivation proof."""

        return self.verify_derivations((derivation_node_id,))[derivation_node_id]

    def verify_derivations(
        self, derivation_node_ids: tuple[str, ...]
    ) -> dict[str, str]:
        """Atomically verify one or more roots with shared deterministic memoization."""

        if not isinstance(derivation_node_ids, tuple) or not derivation_node_ids:
            raise DerivationIntegrityError("verification requires one or more root node IDs")
        if len(derivation_node_ids) != len(set(derivation_node_ids)):
            raise DerivationIntegrityError("verification root node IDs must be unique")
        states: dict[str, str] = {}
        memo: dict[str, DerivationNode] = {}
        verified: dict[str, str] = {}
        for node_id in sorted(derivation_node_ids):
            _require_text("derivation_node_id", node_id)
            node = self._verify_derivation_node(node_id, states=states, memo=memo)
            verified[node_id] = node.derived_available_at
        return verified

    def get_required_ancestry(self, derivation_node_id: str) -> list[DerivationNode]:
        return self._get_derivation_ancestry(derivation_node_id, required_only=True)

    def get_full_provenance_ancestry(
        self, derivation_node_id: str
    ) -> list[DerivationNode]:
        return self._get_derivation_ancestry(derivation_node_id, required_only=False)

    def get_raw_artifact_lineage(
        self, derivation_node_id: str, *, required_only: bool = False
    ) -> list[RawArtifactLineage]:
        root = self.get_derivation_node(derivation_node_id)
        nodes = self._get_derivation_ancestry(
            derivation_node_id, required_only=required_only
        )
        candidates = ([root] if root.node_kind == "INPUT_OBSERVATION" else []) + nodes
        result: list[RawArtifactLineage] = []
        for node in candidates:
            if node.node_kind != "INPUT_OBSERVATION":
                continue
            observation = self.get_input_observation(node.entity_id)
            result.append(
                RawArtifactLineage(
                    node=node,
                    observation=observation,
                    artifact=self.get_artifact(observation.artifact_id),
                )
            )
        return sorted(result, key=lambda item: item.node.derivation_node_id)

    @staticmethod
    def verify_artifact(artifact: Artifact) -> None:
        if artifact.byte_length != len(artifact.payload):
            raise ArtifactIntegrityError("artifact byte_length does not match stored payload")
        raw_sha256 = hashlib.sha256(artifact.payload).hexdigest()
        if artifact.sha256 != raw_sha256:
            raise ArtifactIntegrityError("artifact SHA-256 does not match stored payload")
        expected_id = artifact_id_for(
            artifact.artifact_kind,
            artifact.canonicalization_version,
            artifact.payload,
        )
        if artifact.artifact_id != expected_id:
            raise ArtifactIntegrityError("artifact ID does not match the frozen hash preimage")
        if artifact.content_hash != expected_id.removeprefix("sha256:"):
            raise ArtifactIntegrityError("artifact content_hash does not match artifact ID")
        if artifact.storage_uri != f"sqlite:artifact:{artifact.artifact_id}":
            raise ArtifactIntegrityError("artifact storage URI does not match content address")
        try:
            _validate_canonical_payload(artifact.canonicalization_version, artifact.payload)
        except CanonicalizationError as exc:
            raise ArtifactIntegrityError("stored artifact payload is not canonical") from exc

    def create_run_request(
        self,
        *,
        request_kind: str,
        analysis_cutoff_at: str,
        parameters: Any,
        idempotency_key: str,
        requested_by: str,
        baseline_commit: str,
    ) -> RunRequest:
        """Create or replay one caller-scoped immutable logical request."""

        _require_text("request_kind", request_kind)
        _require_timestamp("analysis_cutoff_at", analysis_cutoff_at)
        _require_text("idempotency_key", idempotency_key)
        _require_text("requested_by", requested_by)
        _require_text("baseline_commit", baseline_commit)
        _validate_classified_request_parameters(
            request_kind, analysis_cutoff_at, parameters
        )
        parameters_json = canonicalize_json(parameters).decode("utf-8")
        fingerprint = _request_fingerprint(
            request_kind=request_kind,
            analysis_cutoff_at=analysis_cutoff_at,
            parameters_json=parameters_json,
            baseline_commit=baseline_commit,
        )

        with self.transaction() as connection, self._internal_write():
            existing = connection.execute(
                "SELECT * FROM run_request WHERE requested_by = ? AND idempotency_key = ?",
                (requested_by, idempotency_key),
            ).fetchone()
            if existing is not None:
                request = _run_request_from_row(existing)
                if _request_fingerprint_from_request(request) != fingerprint:
                    raise RequestIdempotencyConflict(
                        "caller-scoped idempotency key has incompatible request semantics"
                    )
                return request

            request_id = _new_id()
            requested_at = _utc_now()
            connection.execute(
                """
                INSERT INTO run_request (
                    run_request_id, request_kind, requested_at, analysis_cutoff_at,
                    parameters_json, idempotency_key, requested_by, baseline_commit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    request_kind,
                    requested_at,
                    analysis_cutoff_at,
                    parameters_json,
                    idempotency_key,
                    requested_by,
                    baseline_commit,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_request_control (
                    run_request_id, current_attempt_id, current_fence_token,
                    request_state, committed_run_id, updated_at
                ) VALUES (?, NULL, 0, 'ACTIVE', NULL, ?)
                """,
                (request_id, requested_at),
            )
        return self.get_run_request(request_id)

    def get_run_request(self, run_request_id: str) -> RunRequest:
        row = self.connection.execute(
            "SELECT * FROM run_request WHERE run_request_id = ?", (run_request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_request_id)
        return _run_request_from_row(row)

    def allocate_attempt(
        self,
        *,
        run_request_id: str,
        worker_identity: str,
        code_commit: str,
        environment_fingerprint: str,
    ) -> Attempt:
        """Atomically allocate the next ordinal and fencing token for a request."""

        _require_text("worker_identity", worker_identity)
        _require_text("code_commit", code_commit)
        _require_text("environment_fingerprint", environment_fingerprint)
        now = _utc_now()
        with self.transaction() as connection, self._internal_write():
            control = connection.execute(
                "SELECT * FROM run_request_control WHERE run_request_id = ?",
                (run_request_id,),
            ).fetchone()
            if control is None:
                raise KeyError(run_request_id)
            if control["request_state"] != "ACTIVE":
                raise InvalidAttemptTransition("a committed request cannot allocate attempts")

            current_id = control["current_attempt_id"]
            if current_id is not None:
                current = self._get_attempt_row(current_id)
                self._append_attempt_event(
                    attempt_id=current_id,
                    event_type="AUTHORITY_REVOKED",
                    event_at=now,
                    from_status=current["status"],
                    to_status=current["status"],
                    reason_code="SUPERSEDED_BY_RETRY",
                    failure_class=None,
                    failure_message=None,
                    actor_identity=worker_identity,
                    details_artifact_id=None,
                )
                if current["status"] == "RUNNING":
                    self._append_attempt_event(
                        attempt_id=current_id,
                        event_type="ABORTED",
                        event_at=now,
                        from_status="RUNNING",
                        to_status="ABORTED",
                        reason_code="SUPERSEDED_BY_RETRY",
                        failure_class=None,
                        failure_message=None,
                        actor_identity=worker_identity,
                        details_artifact_id=None,
                    )
                    connection.execute(
                        """
                        UPDATE attempt
                        SET status = 'ABORTED', finished_at = ?,
                            failure_class = NULL, failure_message = NULL
                        WHERE attempt_id = ?
                        """,
                        (now, current_id),
                    )

            attempt_ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_ordinal), 0) + 1 FROM attempt WHERE run_request_id = ?",
                    (run_request_id,),
                ).fetchone()[0]
            )
            fence_token = int(control["current_fence_token"]) + 1
            attempt_id = _new_id(excluding={run_request_id})
            connection.execute(
                """
                INSERT INTO attempt (
                    attempt_id, run_request_id, attempt_ordinal, fence_token,
                    started_at, finished_at, status, worker_identity, code_commit,
                    environment_fingerprint, failure_class, failure_message
                ) VALUES (?, ?, ?, ?, ?, NULL, 'RUNNING', ?, ?, ?, NULL, NULL)
                """,
                (
                    attempt_id,
                    run_request_id,
                    attempt_ordinal,
                    fence_token,
                    now,
                    worker_identity,
                    code_commit,
                    environment_fingerprint,
                ),
            )
            self._append_attempt_event(
                attempt_id=attempt_id,
                event_type="ALLOCATED",
                event_at=now,
                from_status=None,
                to_status="RUNNING",
                reason_code=None,
                failure_class=None,
                failure_message=None,
                actor_identity=worker_identity,
                details_artifact_id=None,
            )
            connection.execute(
                """
                UPDATE run_request_control
                SET current_attempt_id = ?, current_fence_token = ?, updated_at = ?
                WHERE run_request_id = ?
                """,
                (attempt_id, fence_token, now, run_request_id),
            )
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> Attempt:
        return _attempt_from_row(self._get_attempt_row(attempt_id))

    def list_attempt_events(self, attempt_id: str) -> list[AttemptEvent]:
        rows = self.connection.execute(
            """
            SELECT * FROM attempt_event
            WHERE attempt_id = ? ORDER BY sequence_number
            """,
            (attempt_id,),
        ).fetchall()
        return [_attempt_event_from_row(row) for row in rows]

    def is_attempt_authorized(self, attempt_id: str, fence_token: int) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM attempt
            JOIN run_request_control USING (run_request_id)
            WHERE attempt.attempt_id = ?
              AND attempt.fence_token = ?
              AND attempt.status = 'RUNNING'
              AND run_request_control.request_state = 'ACTIVE'
              AND run_request_control.current_attempt_id = attempt.attempt_id
              AND run_request_control.current_fence_token = attempt.fence_token
            """,
            (attempt_id, fence_token),
        ).fetchone()
        return row is not None

    def require_attempt_authority(self, attempt_id: str, fence_token: int) -> Attempt:
        row = self._require_attempt_authority(attempt_id, fence_token)
        return _attempt_from_row(row)

    def terminalize_attempt(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        status: str,
        actor_identity: str,
        reason_code: str | None = None,
        failure_class: str | None = None,
        failure_message: str | None = None,
        details_artifact_id: str | None = None,
    ) -> Attempt:
        """Atomically record FAILED or ABORTED; success is commit_run-only."""

        if status not in {"FAILED", "ABORTED"}:
            raise InvalidAttemptTransition(
                "only FAILED or ABORTED may terminalize outside committed-run transaction"
            )
        _require_text("actor_identity", actor_identity)
        if status == "FAILED":
            _require_text("failure_class", failure_class)
        elif failure_class is not None or failure_message is not None:
            raise InvalidAttemptTransition("ABORTED forbids failure fields")
        if status == "ABORTED":
            _require_text("reason_code", reason_code)

        with self.transaction() as connection, self._internal_write():
            row = self._get_attempt_row(attempt_id)
            if row["status"] != "RUNNING":
                if self._terminalization_matches(
                    row,
                    status=status,
                    reason_code=reason_code,
                    failure_class=failure_class,
                    failure_message=failure_message,
                    actor_identity=actor_identity,
                    details_artifact_id=details_artifact_id,
                ):
                    return _attempt_from_row(row)
                raise InvalidAttemptTransition(
                    f"attempt is already terminal with status {row['status']}"
                )
            self._require_attempt_authority(attempt_id, fence_token)
            if details_artifact_id is not None:
                self.get_artifact(details_artifact_id)
            finished_at = _utc_now()
            self._append_attempt_event(
                attempt_id=attempt_id,
                event_type=status,
                event_at=finished_at,
                from_status="RUNNING",
                to_status=status,
                reason_code=reason_code,
                failure_class=failure_class,
                failure_message=failure_message,
                actor_identity=actor_identity,
                details_artifact_id=details_artifact_id,
            )
            connection.execute(
                """
                UPDATE attempt
                SET status = ?, finished_at = ?, failure_class = ?, failure_message = ?
                WHERE attempt_id = ?
                """,
                (status, finished_at, failure_class, failure_message, attempt_id),
            )
        return self.get_attempt(attempt_id)

    def attach_attempt_artifact(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        artifact_id: str,
        role: str,
    ) -> str:
        if role == "RESULT_MANIFEST":
            raise InvalidAttemptTransition("result manifests attach only during run commit")
        if role not in {
            "OUTPUT",
            "FAILED_ATTEMPT_OUTPUT",
            "LOG",
            "DIAGNOSTIC",
            "PARTIAL_OUTPUT",
        }:
            raise ValueError(f"unsupported attempt artifact role: {role!r}")
        with self.transaction(), self._internal_write():
            self._require_attempt_authority(attempt_id, fence_token)
            self.get_artifact(artifact_id)
            return self._attach_attempt_artifact(attempt_id, artifact_id, role, _utc_now())

    def validate_v14_artifact(self, artifact_id: str) -> Any:
        """Validate one frozen V1.4 structured or byte-preserving Artifact."""
        artifact = self.get_artifact(artifact_id)
        if artifact.artifact_kind == "ledger.llm-effective-prompt.v1":
            validate_effective_prompt(artifact)
            return artifact.payload
        return validate_json_artifact(artifact, resolver=self.get_artifact)

    def register_research_method(
        self, *, definition_artifact_id: str, code_commit: str,
        research_method_id: str | None = None,
    ) -> ResearchMethod:
        method_id = research_method_id or _new_id()
        _require_uuid4("research_method_id", method_id)
        if not re.fullmatch(r"[0-9a-f]{40}", code_commit or ""):
            raise ArtifactIntegrityError("code_commit must be 40 lowercase hexadecimal characters")
        artifact = self.get_artifact(definition_artifact_id)
        value = validate_json_artifact(artifact, resolver=self.get_artifact)
        if value != RESEARCH_METHOD_DEFINITION:
            raise ArtifactIntegrityError("Research method definition differs")
        with self.transaction() as connection, self._internal_write():
            connection.execute(
                "INSERT INTO research_method VALUES (?, 'TRINITY_USA_RESEARCH', 'USA_V2', ?, ?, ?)",
                (method_id, definition_artifact_id, code_commit, _utc_now()),
            )
        return self.get_research_method(method_id)

    def get_research_method(self, research_method_id: str) -> ResearchMethod:
        row = self.connection.execute(
            "SELECT * FROM research_method WHERE research_method_id = ?",
            (research_method_id,),
        ).fetchone()
        if row is None:
            raise KeyError(research_method_id)
        result = _research_method_from_row(row)
        if validate_json_artifact(self.get_artifact(result.definition_artifact_id)) != RESEARCH_METHOD_DEFINITION:
            raise ArtifactIntegrityError("stored Research method differs")
        return result

    def register_setup_policy(
        self, *, definition_artifact_id: str, code_commit: str,
        setup_policy_id: str | None = None,
    ) -> SetupPolicy:
        policy_id = setup_policy_id or _new_id()
        _require_uuid4("setup_policy_id", policy_id)
        if not re.fullmatch(r"[0-9a-f]{40}", code_commit or ""):
            raise ArtifactIntegrityError("code_commit must be 40 lowercase hexadecimal characters")
        artifact = self.get_artifact(definition_artifact_id)
        value = validate_json_artifact(artifact, resolver=self.get_artifact)
        if value != SETUP_POLICY_DEFINITION:
            raise ArtifactIntegrityError("Setup policy definition differs")
        with self.transaction() as connection, self._internal_write():
            connection.execute(
                "INSERT INTO setup_policy VALUES (?, 'SETUP', 'USA_SETUP_V1', ?, ?, ?)",
                (policy_id, definition_artifact_id, code_commit, _utc_now()),
            )
        return self.get_setup_policy(policy_id)

    def get_setup_policy(self, setup_policy_id: str) -> SetupPolicy:
        row = self.connection.execute(
            "SELECT * FROM setup_policy WHERE setup_policy_id = ?", (setup_policy_id,)
        ).fetchone()
        if row is None:
            raise KeyError(setup_policy_id)
        result = _setup_policy_from_row(row)
        if validate_json_artifact(self.get_artifact(result.definition_artifact_id)) != SETUP_POLICY_DEFINITION:
            raise ArtifactIntegrityError("stored Setup policy differs")
        return result

    def create_llm_interaction(
        self, *, attempt_id: str, fence_token: int, ordinal: int,
        request_artifact_id: str, status: str, started_at: str, finished_at: str,
        response_artifact_id: str | None = None, error_artifact_id: str | None = None,
        input_tokens: int | None = None, output_tokens: int | None = None,
        llm_interaction_id: str | None = None,
    ) -> LLMInteraction:
        interaction_id = llm_interaction_id or _new_id()
        _require_uuid4("llm_interaction_id", interaction_id)
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
            raise ValueError("ordinal must be positive")
        _require_timestamp("started_at", started_at); _require_timestamp("finished_at", finished_at)
        if finished_at < started_at:
            raise ValueError("finished_at precedes started_at")
        request = self.get_artifact(request_artifact_id)
        request_value = validate_json_artifact(request, resolver=self.get_artifact)
        role = request_value["interaction_role"]
        context_value = validate_json_artifact(
            self.get_artifact(request_value["context_artifact_id"]), resolver=self.get_artifact
        )
        if context_value["interaction_role"] != role:
            raise ArtifactIntegrityError("request/context interaction role differs")
        response_schema = validate_json_artifact(
            self.get_artifact(request_value["response_schema_artifact_id"]), resolver=self.get_artifact
        )
        expected_schema = analyst_response_schema() if role == "ANALYST" else critic_response_schema()
        if response_schema != expected_schema:
            raise ArtifactIntegrityError("request response schema differs from interaction role")
        parameters = validate_json_artifact(
            self.get_artifact(request_value["invocation_parameters_artifact_id"]), resolver=self.get_artifact
        )
        if parameters["response_schema_artifact_id"] != request_value["response_schema_artifact_id"]:
            raise ArtifactIntegrityError("request and invocation parameters name different schemas")
        for item in context_value["items"]:
            context_node = self.get_derivation_node(item["derivation_node_id"])
            if context_node.node_kind != "NORMALIZED_FACT" or context_node.entity_id != item["artifact_id"]:
                raise ArtifactIntegrityError("context Artifact and derivation node differ")
        if role == "CRITIC":
            analyst_stage_id = context_value["items"][1]["artifact_id"]
            analyst_stage = validate_json_artifact(self.get_artifact(analyst_stage_id), resolver=self.get_artifact)
            try:
                analyst_interaction = self.get_llm_interaction(analyst_stage["llm_interaction_id"])
            except KeyError as exc:
                raise ArtifactIntegrityError("Critic context names unknown Analyst interaction") from exc
            analyst_success = validate_json_artifact(
                self.get_artifact(analyst_interaction.response_artifact_id or ""), resolver=self.get_artifact
            )
            if analyst_interaction.status != "SUCCEEDED" or analyst_success["effective_stage_result_artifact_id"] != analyst_stage_id:
                raise ArtifactIntegrityError("Critic context does not name exact successful Analyst stage")
        if status == "SUCCEEDED":
            if response_artifact_id is None or error_artifact_id is not None:
                raise ArtifactIntegrityError("successful interaction requires only success Artifact")
            result = self.get_artifact(response_artifact_id)
            result_value = validate_json_artifact(result, resolver=self.get_artifact)
            if result.artifact_kind != "ledger.llm-invocation-success.v1" or result_value["llm_interaction_id"] != interaction_id:
                raise ArtifactIntegrityError("success bundle interaction identity differs")
            parsed = validate_json_artifact(
                self.get_artifact(result_value["parsed_response_artifact_id"]),
                resolver=self.get_artifact,
            )
            stage = validate_json_artifact(
                self.get_artifact(result_value["effective_stage_result_artifact_id"]),
                resolver=self.get_artifact,
            )
            if (
                parsed["interaction_role"] != role or stage["interaction_role"] != role
                or stage["llm_interaction_id"] != interaction_id
                or parsed["raw_response_artifact_id"] != result_value["raw_response_artifact_id"]
                or parsed["response_schema_artifact_id"] != request_value["response_schema_artifact_id"]
                or stage["parsed_response_artifact_id"] != result_value["parsed_response_artifact_id"]
            ):
                raise ArtifactIntegrityError("LLM success role or stage identity differs")
        elif status == "FAILED":
            if error_artifact_id is None or response_artifact_id is not None:
                raise ArtifactIntegrityError("failed interaction requires only error Artifact")
            error = self.get_artifact(error_artifact_id)
            error_value = validate_json_artifact(error, resolver=self.get_artifact)
            if error.artifact_kind != "ledger.llm-error.v1" or error_value["llm_interaction_id"] != interaction_id:
                raise ArtifactIntegrityError("error bundle interaction identity differs")
        else:
            raise ArtifactIntegrityError("unsupported interaction status")
        for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be nonnegative integer or null")
        with self.transaction() as connection, self._internal_write():
            self._require_attempt_authority(attempt_id, fence_token)
            connection.execute(
                """INSERT INTO llm_interaction VALUES
                (?, ?, ?, 'OPENAI_CODEX_CLI', 'gpt-5.6-sol', 'codex-cli:gpt-5.6-sol',
                 ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (interaction_id, attempt_id, ordinal, request_artifact_id,
                 response_artifact_id, error_artifact_id, status, started_at, finished_at,
                 input_tokens, output_tokens, _utc_now()),
            )
        return self.get_llm_interaction(interaction_id)

    def get_llm_interaction(self, llm_interaction_id: str) -> LLMInteraction:
        row = self.connection.execute(
            "SELECT * FROM llm_interaction WHERE llm_interaction_id = ?",
            (llm_interaction_id,),
        ).fetchone()
        if row is None:
            raise KeyError(llm_interaction_id)
        return _llm_interaction_from_row(row)

    def create_research_record(
        self, *, attempt_id: str, fence_token: int, subject_key: str,
        content_artifact_id: str, research_method_id: str, as_of_at: str,
        parents: tuple[DerivationParent, ...],
        llm_links: tuple[tuple[str, str, int], ...],
        research_id: str | None = None, derivation_node_id: str | None = None,
    ) -> ResearchRecord:
        research_identity = research_id or _new_id()
        node_id = derivation_node_id or _new_id(excluding={research_identity})
        _require_uuid4("research_id", research_identity)
        _require_text("derivation_node_id", node_id); _require_text("subject_key", subject_key)
        _require_timestamp("as_of_at", as_of_at)
        normalized_parents = _validate_derivation_parents(parents)
        if not any(item.required for item in normalized_parents):
            raise DerivationIntegrityError("Research requires a REQUIRED parent")
        method = self.get_research_method(research_method_id)
        content = self.get_artifact(content_artifact_id)
        content_value = validate_json_artifact(content, resolver=self.get_artifact)
        if content.artifact_kind != "ledger.usa-v2-research-result.v1":
            raise ArtifactIntegrityError("Research content has wrong Artifact kind")
        if subject_key != f"ticker:{content_value['ticker']}":
            raise ArtifactIntegrityError("Research subject differs from semantic content")
        if as_of_at[:10] != content_value["as_of"]:
            raise ArtifactIntegrityError("Research reference date differs from semantic content")
        if not isinstance(llm_links, tuple) or len(llm_links) != 2:
            raise ArtifactIntegrityError("USA_V2 Research requires exact Analyst and Critic links")
        expected_links = {("PRIMARY", 1), ("CRITIQUE", 1)}
        if {(item[1], item[2]) for item in llm_links} != expected_links:
            raise ArtifactIntegrityError("Research LLM roles differ from frozen pilot")
        with self.transaction() as connection, self._internal_write():
            self._require_attempt_authority(attempt_id, fence_token)
            parent_nodes = self._validated_attempt_local_parents(
                attempt_id, node_id, normalized_parents
            )
            required_artifacts = {
                node.entity_id for key, node in parent_nodes.items()
                if next(p for p in normalized_parents if p.parent_node_id == key).required
                and node.node_kind == "NORMALIZED_FACT"
            }
            stage_results: dict[str, dict[str, Any]] = {}
            for interaction_id, role, ordinal in llm_links:
                interaction = self.get_llm_interaction(interaction_id)
                if interaction.attempt_id != attempt_id or interaction.status != "SUCCEEDED":
                    raise ArtifactIntegrityError("Research interaction is not successful same-attempt evidence")
                request_value = validate_json_artifact(
                    self.get_artifact(interaction.request_artifact_id),
                    resolver=self.get_artifact,
                )
                expected_interaction_role = "ANALYST" if role == "PRIMARY" else "CRITIC"
                if request_value["interaction_role"] != expected_interaction_role:
                    raise ArtifactIntegrityError("Research LLM role does not match interaction role")
                success = validate_json_artifact(
                    self.get_artifact(interaction.response_artifact_id or ""),
                    resolver=self.get_artifact,
                )
                if success["effective_stage_result_artifact_id"] not in required_artifacts:
                    raise DerivationIntegrityError("Research lacks required effective LLM stage node")
                stage = validate_json_artifact(
                    self.get_artifact(success["effective_stage_result_artifact_id"]),
                    resolver=self.get_artifact,
                )
                stage_results[expected_interaction_role] = stage["result"]
            _validate_research_stage_projection(
                content_value, stage_results["ANALYST"], stage_results["CRITIC"]
            )
            derived_at = max(
                parent_nodes[item.parent_node_id].derived_available_at
                for item in normalized_parents if item.required
            )
            connection.execute(
                """INSERT INTO research_record VALUES
                (?, ?, NULL, 'TRINITY_USA_RESEARCH', ?, ?, ?, 'USA_V2', ?, ?, NULL, ?)""",
                (research_identity, attempt_id, subject_key, content_artifact_id,
                 method.research_method_id, as_of_at, node_id, _utc_now()),
            )
            self._insert_derivation_edges(node_id, normalized_parents, attempt_id)
            connection.execute(
                """INSERT INTO derivation_node VALUES
                (?, NULL, ?, 'RESEARCH', 'research_record', ?, NULL, ?, ?)""",
                (node_id, attempt_id, research_identity, derived_at, MAX_REQUIRED_PARENTS_V1),
            )
            for interaction_id, role, ordinal in llm_links:
                connection.execute(
                    "INSERT INTO research_llm_interaction VALUES (?, ?, ?, ?)",
                    (research_identity, interaction_id, role, ordinal),
                )
        return self.get_research_record(research_identity)

    def get_research_record(self, research_id: str, *, verify: bool = True) -> ResearchRecord:
        row = self.connection.execute(
            "SELECT * FROM research_record WHERE research_id = ?", (research_id,)
        ).fetchone()
        if row is None:
            raise KeyError(research_id)
        result = _research_record_from_row(row)
        if not verify:
            return result
        node = self.get_derivation_node(result.derivation_node_id)
        if (node.node_kind, node.entity_type, node.entity_id, node.attempt_id, node.run_id) != (
            "RESEARCH", "research_record", result.research_id, result.attempt_id, result.run_id
        ):
            raise DerivationIntegrityError("Research entity/node identity differs")
        self.get_research_method(result.research_method_id)
        content = validate_json_artifact(
            self.get_artifact(result.content_artifact_id), resolver=self.get_artifact
        )
        if (
            result.subject_key != f"ticker:{content['ticker']}"
            or result.as_of_at[:10] != content["as_of"]
        ):
            raise ArtifactIntegrityError("Research entity/content identity differs")
        return result

    def create_setup(
        self, *, attempt_id: str, fence_token: int, instrument_id: str,
        setup_kind: str, signal_at: str, setup_policy_id: str,
        result_artifact_id: str, research_id: str,
        research_derivation_node_id: str, ohlcv_derivation_node_id: str,
        setup_id: str | None = None, derivation_node_id: str | None = None,
    ) -> Setup:
        setup_identity = setup_id or _new_id()
        node_id = derivation_node_id or _new_id(excluding={setup_identity})
        _require_uuid4("setup_id", setup_identity); _require_text("derivation_node_id", node_id)
        _require_text("instrument_id", instrument_id); _require_timestamp("signal_at", signal_at)
        policy = self.get_setup_policy(setup_policy_id)
        result_artifact = self.get_artifact(result_artifact_id)
        result = validate_json_artifact(result_artifact, resolver=self.get_artifact)
        if result_artifact.artifact_kind != "ledger.usa-setup-v1-result.v1" or result["setup_type"] != setup_kind:
            raise ArtifactIntegrityError("Setup result identity differs")
        if result["ticker"] != instrument_id:
            raise ArtifactIntegrityError("Setup entity differs from semantic result")
        research = self.get_research_record(research_id)
        if research.derivation_node_id != research_derivation_node_id:
            raise DerivationIntegrityError("Setup Research node differs from exact Research")
        ohlcv = self.get_derivation_node(ohlcv_derivation_node_id)
        if ohlcv.node_kind != "NORMALIZED_FACT" or ohlcv.entity_type != "artifact":
            raise DerivationIntegrityError("Setup OHLCV parent must be NORMALIZED_FACT")
        rules = setup_rule_values(setup_kind)
        parents = (
            DerivationParent(research_derivation_node_id, "RESEARCH", True),
            DerivationParent(ohlcv_derivation_node_id, "OHLCV", True),
        )
        normalized_parents = _validate_derivation_parents(parents)
        with self.transaction() as connection, self._internal_write():
            self._require_attempt_authority(attempt_id, fence_token)
            if research.attempt_id != attempt_id or research.run_id is not None:
                raise DerivationIntegrityError("Setup Research must be same-attempt and unbound")
            parent_nodes = self._validated_attempt_local_parents(
                attempt_id, node_id, normalized_parents
            )
            derived_at = max(item.derived_available_at for item in parent_nodes.values())
            connection.execute(
                """INSERT INTO setup VALUES
                (?, ?, NULL, ?, 'LONG', ?, ?, ?, ?, ?, NULL, NULL, NULL,
                 ?, 'USA_SETUP_V1', ?, ?, 'USD', 'XNYS',
                 'XNYS_PROVIDED_SESSIONS_V1', 'RAW', ?)""",
                (setup_identity, attempt_id, instrument_id, setup_kind, signal_at,
                 *rules, policy.setup_policy_id, result_artifact_id, node_id, _utc_now()),
            )
            connection.execute(
                "INSERT INTO setup_research_lineage VALUES (?, ?, 'PRIMARY', ?)",
                (setup_identity, research_id, research_derivation_node_id),
            )
            self._insert_derivation_edges(node_id, normalized_parents, attempt_id)
            connection.execute(
                """INSERT INTO derivation_node VALUES
                (?, NULL, ?, 'SETUP', 'setup', ?, NULL, ?, ?)""",
                (node_id, attempt_id, setup_identity, derived_at, MAX_REQUIRED_PARENTS_V1),
            )
        return self.get_setup(setup_identity)

    def get_setup(self, setup_id: str, *, verify: bool = True) -> Setup:
        row = self.connection.execute(
            "SELECT * FROM setup WHERE setup_id = ?", (setup_id,)
        ).fetchone()
        if row is None:
            raise KeyError(setup_id)
        result = _setup_from_row(row)
        if not verify:
            return result
        node = self.get_derivation_node(result.derivation_node_id)
        if (node.node_kind, node.entity_type, node.entity_id, node.attempt_id, node.run_id) != (
            "SETUP", "setup", result.setup_id, result.attempt_id, result.run_id
        ):
            raise DerivationIntegrityError("Setup entity/node identity differs")
        expected_rules = setup_rule_values(result.setup_kind)
        if (result.entry_rule_json, result.stop_rule_json, result.target_rule_json) != expected_rules:
            raise ArtifactIntegrityError("Setup rule JSON differs")
        content = validate_json_artifact(
            self.get_artifact(result.result_artifact_id), resolver=self.get_artifact
        )
        if (
            content["ticker"] != result.instrument_id
            or content["setup_type"] != result.setup_kind
        ):
            raise ArtifactIntegrityError("Setup entity/result identity differs")
        return result

    def _validated_attempt_local_parents(
        self, attempt_id: str, node_id: str,
        parents: tuple[DerivationParent, ...],
    ) -> dict[str, DerivationNode]:
        result: dict[str, DerivationNode] = {}
        for parent in parents:
            if parent.parent_node_id == node_id:
                raise DerivationIntegrityError("derivation self-edge is forbidden")
            node = self._verify_derivation_node(parent.parent_node_id, states={}, memo={})
            if node.attempt_id != attempt_id or node.run_id is not None:
                raise DerivationIntegrityError("derived parents must share attempt-local lineage")
            result[parent.parent_node_id] = node
        return result

    def _insert_derivation_edges(
        self, child_node_id: str, parents: tuple[DerivationParent, ...], attempt_id: str,
    ) -> None:
        for parent in parents:
            self.connection.execute(
                "INSERT INTO derivation_edge VALUES (?, ?, ?, ?, ?)",
                (_new_id(excluding={child_node_id, attempt_id}), parent.parent_node_id,
                 child_node_id, parent.edge_role, int(parent.required)),
            )

    def register_pit_classification_policy(
        self, *, definition_artifact_id: str, code_commit: str,
        classification_policy_id: str | None = None,
    ) -> PitClassificationPolicy:
        """Register one exact frozen V1/V2 classification policy immutably."""
        _require_text("code_commit", code_commit)
        policy_id = classification_policy_id or _new_id()
        _require_uuid4("classification_policy_id", policy_id)
        artifact = self.get_artifact(definition_artifact_id)
        _require_artifact_envelope(artifact, _POLICY_ARTIFACT_KIND)
        try:
            value = json.loads(artifact.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClassificationIntegrityError("invalid policy definition JSON") from exc
        if value == _pit_policy_definition_v1():
            policy_version = REQUIRED_ANCESTRY_PIT_V1
        elif value == pit_classification_policy_definition_v2():
            policy_version = REQUIRED_ANCESTRY_PIT_V2
        else:
            raise ClassificationIntegrityError("policy definition differs from frozen V1/V2")
        with self.transaction() as connection, self._internal_write():
            connection.execute(
                "INSERT INTO pit_classification_policy VALUES (?, 'PIT_CLASSIFICATION', ?, ?, ?, ?)",
                (policy_id, policy_version, definition_artifact_id,
                 code_commit, _utc_now()),
            )
        return self.get_pit_classification_policy(policy_id)

    def get_pit_classification_policy(
        self, classification_policy_id: str
    ) -> PitClassificationPolicy:
        row = self.connection.execute(
            "SELECT * FROM pit_classification_policy WHERE classification_policy_id = ?",
            (classification_policy_id,),
        ).fetchone()
        if row is None:
            raise KeyError(classification_policy_id)
        result = _classification_policy_from_row(row)
        self._verify_classification_policy(result)
        return result

    def create_raw_node_classification(
        self, *, attempt_id: str, fence_token: int, derivation_node_id: str,
        pit_reference_at: str, classification_policy_id: str,
        evidence_artifact_id: str,
        supersedes_classification_id: str | None = None,
        derivation_node_classification_id: str | None = None,
    ) -> DerivationNodeClassification:
        _require_timestamp("pit_reference_at", pit_reference_at)
        classification_id = derivation_node_classification_id or _new_id()
        _require_uuid4("derivation_node_classification_id", classification_id)
        with self.transaction() as connection, self._internal_write():
            self._require_attempt_authority(attempt_id, fence_token)
            policy = self.get_pit_classification_policy(classification_policy_id)
            node = self.get_derivation_node(derivation_node_id)
            if node.node_kind != "INPUT_OBSERVATION" or node.entity_type != "input_observation":
                raise ClassificationIntegrityError("raw classification requires INPUT_OBSERVATION node")
            observation = self.get_input_observation(node.entity_id)
            evidence = self.get_artifact(evidence_artifact_id)
            record_class, pit_class, basis = self._validate_raw_evidence(
                evidence=evidence, node=node, observation=observation,
                pit_reference_at=pit_reference_at, attempt_id=attempt_id,
                policy_version=policy.policy_version,
            )
            version, predecessor = self._classification_successor(
                derivation_node_id, pit_reference_at, supersedes_classification_id
            )
            connection.execute(
                """INSERT INTO derivation_node_classification (
                    derivation_node_classification_id, derivation_node_id,
                    pit_reference_at, classification_version, record_class,
                    resolved_record_class, pit_class, classification_basis,
                    classification_policy_id, evidence_artifact_id,
                    classified_by_attempt_id, created_at, supersedes_classification_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (classification_id, derivation_node_id, pit_reference_at, version,
                 record_class, record_class, pit_class, basis,
                 policy.classification_policy_id, evidence_artifact_id, attempt_id,
                 _utc_now(), predecessor),
            )
        return self.get_node_classification(classification_id)

    def create_derived_node_classification(
        self, *, attempt_id: str, fence_token: int, derivation_node_id: str,
        pit_reference_at: str, classification_policy_id: str,
        parent_classification_ids: tuple[str, ...],
        supersedes_classification_id: str | None = None,
        derivation_node_classification_id: str | None = None,
    ) -> DerivationNodeClassification:
        _require_timestamp("pit_reference_at", pit_reference_at)
        classification_id = derivation_node_classification_id or _new_id()
        _require_uuid4("derivation_node_classification_id", classification_id)
        if len(parent_classification_ids) != len(set(parent_classification_ids)):
            raise ClassificationIntegrityError("parent classifications must be unique")
        with self.transaction() as connection, self._internal_write(), self._classification_construction():
            self._require_attempt_authority(attempt_id, fence_token)
            policy = self.get_pit_classification_policy(classification_policy_id)
            node = self.get_derivation_node(derivation_node_id)
            if node.node_kind not in {"NORMALIZED_FACT", "RESEARCH", "SETUP"}:
                raise ClassificationIntegrityError("derived classification requires a supported derived node")
            if node.node_kind in {"RESEARCH", "SETUP"} and policy.policy_version != REQUIRED_ANCESTRY_PIT_V2:
                raise ClassificationIntegrityError("Research/Setup classification requires PIT V2")
            required_nodes = {
                edge.parent_node_id for edge in self.list_derivation_edges(derivation_node_id)
                if edge.required == 1
            }
            parents = [self.get_node_classification(item) for item in parent_classification_ids]
            if {item.derivation_node_id for item in parents} != required_nodes or len(parents) != len(required_nodes):
                raise ClassificationIntegrityError("classification parents must exactly cover direct REQUIRED parents")
            for parent in parents:
                self.verify_node_classification(parent.derivation_node_classification_id)
                parent_policy = self.get_pit_classification_policy(parent.classification_policy_id)
                if parent.pit_reference_at != pit_reference_at or (
                    parent_policy.policy_kind, parent_policy.policy_version
                ) != (policy.policy_kind, policy.policy_version):
                    raise ClassificationIntegrityError("classification parent reference or policy differs")
            resolved_record = (
                "LEGACY_NON_LEDGER_ARTIFACT"
                if any(p.resolved_record_class == "LEGACY_NON_LEDGER_ARTIFACT" for p in parents)
                else "LEDGER_NATIVE"
            )
            pit_class = _fold_pit_classes([p.pit_class for p in parents])
            version, predecessor = self._classification_successor(
                derivation_node_id, pit_reference_at, supersedes_classification_id
            )
            connection.execute(
                """INSERT INTO derivation_node_classification VALUES
                (?, ?, ?, ?, 'LEDGER_NATIVE', ?, ?, 'REQUIRED_PARENT_PROPAGATION',
                 ?, NULL, ?, ?, ?)""",
                (classification_id, derivation_node_id, pit_reference_at, version,
                 resolved_record, pit_class, policy.classification_policy_id,
                 attempt_id, _utc_now(), predecessor),
            )
            for parent in parents:
                connection.execute(
                    "INSERT INTO derivation_node_classification_parent VALUES (?, ?)",
                    (classification_id, parent.derivation_node_classification_id),
                )
        return self.get_node_classification(classification_id)

    def get_node_classification(
        self, derivation_node_classification_id: str, *, verify: bool = True,
    ) -> DerivationNodeClassification:
        row = self.connection.execute(
            "SELECT * FROM derivation_node_classification WHERE derivation_node_classification_id = ?",
            (derivation_node_classification_id,),
        ).fetchone()
        if row is None:
            raise KeyError(derivation_node_classification_id)
        value = _classification_from_row(row)
        if verify:
            self.verify_node_classification(derivation_node_classification_id)
        return value

    def get_current_node_classification(
        self, derivation_node_id: str, pit_reference_at: str,
    ) -> DerivationNodeClassification:
        row = self.connection.execute(
            """SELECT c.* FROM derivation_node_classification c
               WHERE c.derivation_node_id = ? AND c.pit_reference_at = ?
                 AND NOT EXISTS (SELECT 1 FROM derivation_node_classification n
                                 WHERE n.supersedes_classification_id = c.derivation_node_classification_id)""",
            (derivation_node_id, pit_reference_at),
        ).fetchone()
        if row is None:
            raise KeyError((derivation_node_id, pit_reference_at))
        return self.get_node_classification(row["derivation_node_classification_id"])

    def verify_node_classification(self, classification_id: str) -> DerivationNodeClassification:
        return self._verify_node_classification(classification_id, states={}, memo={})

    def _verify_node_classification(self, classification_id: str, *, states: dict[str, int], memo: dict[str, DerivationNodeClassification]) -> DerivationNodeClassification:
        if classification_id in memo:
            return memo[classification_id]
        if states.get(classification_id) == 1:
            raise ClassificationIntegrityError("classification ancestry contains a cycle")
        states[classification_id] = 1
        row = self.connection.execute("SELECT * FROM derivation_node_classification WHERE derivation_node_classification_id = ?", (classification_id,)).fetchone()
        if row is None:
            raise ClassificationIntegrityError("classification ancestry is incomplete")
        item = _classification_from_row(row)
        chain_rows = self.connection.execute(
            """SELECT derivation_node_classification_id, classification_version,
                      supersedes_classification_id
               FROM derivation_node_classification
               WHERE derivation_node_id = ? AND pit_reference_at = ?
               ORDER BY classification_version""",
            (item.derivation_node_id, item.pit_reference_at),
        ).fetchall()
        expected_predecessor = None
        for expected_version, chain_row in enumerate(chain_rows, 1):
            if int(chain_row["classification_version"]) != expected_version or chain_row["supersedes_classification_id"] != expected_predecessor:
                raise ClassificationIntegrityError("classification supersession chain is invalid")
            expected_predecessor = str(chain_row["derivation_node_classification_id"])
        policy = self.get_pit_classification_policy(item.classification_policy_id)
        node = self.get_derivation_node(item.derivation_node_id)
        if node.node_kind == "INPUT_OBSERVATION":
            if self.connection.execute("SELECT 1 FROM derivation_node_classification_parent WHERE child_classification_id = ?", (classification_id,)).fetchone():
                raise ClassificationIntegrityError("raw classification cannot have parents")
            observation = self.get_input_observation(node.entity_id)
            if item.evidence_artifact_id is None:
                raise ClassificationIntegrityError("raw classification lacks evidence")
            expected_record, expected_pit, expected_basis = self._validate_raw_evidence(
                evidence=self.get_artifact(item.evidence_artifact_id), node=node,
                observation=observation, pit_reference_at=item.pit_reference_at,
                attempt_id=item.classified_by_attempt_id,
                policy_version=policy.policy_version,
            )
            if (item.record_class, item.resolved_record_class, item.pit_class, item.classification_basis) != (expected_record, expected_record, expected_pit, expected_basis):
                raise ClassificationIntegrityError("stored raw classification differs from evidence")
        elif node.node_kind in {"NORMALIZED_FACT", "RESEARCH", "SETUP"}:
            if node.node_kind in {"RESEARCH", "SETUP"} and policy.policy_version != REQUIRED_ANCESTRY_PIT_V2:
                raise ClassificationIntegrityError("Research/Setup classification requires PIT V2")
            required = {e.parent_node_id for e in self.list_derivation_edges(node.derivation_node_id) if e.required}
            parent_rows = self.connection.execute("SELECT parent_classification_id FROM derivation_node_classification_parent WHERE child_classification_id = ? ORDER BY parent_classification_id", (classification_id,)).fetchall()
            parents = [self._verify_node_classification(str(r[0]), states=states, memo=memo) for r in parent_rows]
            if {p.derivation_node_id for p in parents} != required or len(parents) != len(required):
                raise ClassificationIntegrityError("classification parent closure is incomplete")
            if any(p.pit_reference_at != item.pit_reference_at for p in parents):
                raise ClassificationIntegrityError("classification references differ")
            for parent in parents:
                pp = self.get_pit_classification_policy(parent.classification_policy_id)
                if (pp.policy_kind, pp.policy_version) != (policy.policy_kind, policy.policy_version):
                    raise ClassificationIntegrityError("classification policies differ")
            expected_record = "LEGACY_NON_LEDGER_ARTIFACT" if any(p.resolved_record_class == "LEGACY_NON_LEDGER_ARTIFACT" for p in parents) else "LEDGER_NATIVE"
            expected_pit = _fold_pit_classes([p.pit_class for p in parents])
            if (item.record_class, item.resolved_record_class, item.pit_class, item.classification_basis, item.evidence_artifact_id) != ("LEDGER_NATIVE", expected_record, expected_pit, "REQUIRED_PARENT_PROPAGATION", None):
                raise ClassificationIntegrityError("stored derived classification differs from required ancestry")
        else:
            raise ClassificationIntegrityError("unsupported classified node kind")
        states[classification_id] = 2
        memo[classification_id] = item
        return item

    def _verify_classification_policy(self, policy: PitClassificationPolicy) -> None:
        if policy.policy_kind != "PIT_CLASSIFICATION" or policy.policy_version not in {
            REQUIRED_ANCESTRY_PIT_V1, REQUIRED_ANCESTRY_PIT_V2,
        }:
            raise UnsupportedClassificationPolicy("unsupported classification policy")
        artifact = self.get_artifact(policy.definition_artifact_id)
        _require_artifact_envelope(artifact, _POLICY_ARTIFACT_KIND)
        expected = (
            _pit_policy_definition_v1() if policy.policy_version == REQUIRED_ANCESTRY_PIT_V1
            else pit_classification_policy_definition_v2()
        )
        if json.loads(artifact.payload) != expected:
            raise ClassificationIntegrityError("policy definition differs from frozen version")

    def _classification_successor(self, node_id: str, reference: str, requested: str | None) -> tuple[int, str | None]:
        rows = self.connection.execute("SELECT c.* FROM derivation_node_classification c WHERE c.derivation_node_id = ? AND c.pit_reference_at = ? ORDER BY c.classification_version", (node_id, reference)).fetchall()
        if not rows:
            if requested is not None:
                raise ClassificationIntegrityError("first classification cannot supersede")
            return 1, None
        head = next((r for r in rows if not self.connection.execute("SELECT 1 FROM derivation_node_classification WHERE supersedes_classification_id = ?", (r["derivation_node_classification_id"],)).fetchone()), None)
        if head is None or requested != head["derivation_node_classification_id"]:
            raise ClassificationIntegrityError("supersession must name the unique current head")
        return int(head["classification_version"]) + 1, str(head["derivation_node_classification_id"])

    def _validate_raw_evidence(self, *, evidence: Artifact, node: DerivationNode, observation: InputObservation, pit_reference_at: str, attempt_id: str, policy_version: str) -> tuple[str, str, str]:
        _require_artifact_envelope(evidence, _EVIDENCE_ARTIFACT_KIND)
        try:
            value = json.loads(evidence.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClassificationIntegrityError("invalid classification evidence JSON") from exc
        if not isinstance(value, dict) or set(value) != _EVIDENCE_KEYS:
            raise ClassificationIntegrityError("classification evidence must use the closed V1 schema")
        if value["schema_name"] != "ledger.pit-classification-evidence" or value["schema_version"] != "1" or value["derivation_node_id"] != node.derivation_node_id or value["input_observation_id"] != observation.input_observation_id or value["artifact_id"] != observation.artifact_id or value["pit_reference_at"] != pit_reference_at:
            raise ClassificationIntegrityError("classification evidence identity does not match raw node")
        _require_timestamp("evidence pit_reference_at", value["pit_reference_at"])
        support = value["supporting_artifact_ids"]
        if not isinstance(support, list) or not all(isinstance(x, str) for x in support) or support != sorted(set(support)) or evidence.artifact_id in support:
            raise ClassificationIntegrityError("supporting Artifact IDs must be sorted and unique")
        attached = {r[0] for r in self.connection.execute("SELECT artifact_id FROM attempt_artifact WHERE attempt_id = ? AND role IN ('DIAGNOSTIC','OUTPUT')", (attempt_id,))}
        if evidence.artifact_id not in attached or not set(support) <= attached:
            raise ClassificationIntegrityError("classification evidence must be attached to classifying attempt")
        for artifact_id in support:
            self.get_artifact(artifact_id)
        origin = value["record_origin_evidence_kind"]
        if origin == "LEDGER_AUTHORIZED_CAPTURE":
            if any(value[k] is not None for k in ("legacy_namespace", "legacy_record_key", "legacy_record_id")):
                raise ClassificationIntegrityError("native evidence must have null legacy identity")
            record = "LEDGER_NATIVE"
        elif origin == "LEGACY_IMPORT":
            namespace, key, legacy_id = value["legacy_namespace"], value["legacy_record_key"], value["legacy_record_id"]
            if not isinstance(namespace, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", namespace) or not isinstance(key, str) or not key or "\0" in key:
                raise ClassificationIntegrityError("invalid legacy identity")
            digest = hashlib.sha256(canonicalize_json({"legacy_namespace": namespace, "legacy_record_key": key})).hexdigest()
            if legacy_id != f"legacy:sha256:{digest}":
                raise ClassificationIntegrityError("legacy_record_id does not match canonical identity")
            record = "LEGACY_NON_LEDGER_ARTIFACT"
        else:
            raise ClassificationIntegrityError("unsupported record-origin evidence")
        kind = value["pit_evidence_kind"]
        matrix = {
            "LEDGER_CONTEMPORANEOUS_CAPTURE": ("LEDGER_AUTHORIZED_CAPTURE_V1", "CONTEMPORANEOUS_CAPTURE_VERIFIED", "ARCHIVED_POINT_IN_TIME", "RAW_ARCHIVE_EVIDENCE"),
            "INDEPENDENT_ARCHIVE_PROOF": ("INDEPENDENT_ARCHIVE_PROOF_V1", "INDEPENDENT_ARCHIVE_VERIFIED", "ARCHIVED_POINT_IN_TIME", "RAW_ARCHIVE_EVIDENCE"),
            "POST_REFERENCE_RECONSTRUCTION": ("LEDGER_RECONSTRUCTION_EVIDENCE_V1", "POST_REFERENCE_RECONSTRUCTION_VERIFIED", "RECONSTRUCTED_NOT_ARCHIVED", "RAW_RECONSTRUCTION_EVIDENCE"),
            "INSUFFICIENT_PIT_EVIDENCE": ("LEDGER_INSUFFICIENT_EVIDENCE_V1", "PIT_EVIDENCE_INSUFFICIENT", "UNKNOWN", "RAW_UNKNOWN_EVIDENCE"),
            "PIT_IRRELEVANT_CONTENT": ("LEDGER_PIT_IRRELEVANCE_V1", "PIT_NOT_APPLICABLE_BY_ARTIFACT_KIND", "NOT_APPLICABLE", "RAW_NOT_APPLICABLE_EVIDENCE"),
        }
        if kind not in matrix or (value["verifier_id"], value["rationale_code"]) != matrix[kind][:2]:
            raise ClassificationIntegrityError("evidence verifier or rationale is invalid")
        archive, reconstruction = value["archive_captured_at"], value["reconstruction_completed_at"]
        for timestamp_name, timestamp_value in (
            ("archive_captured_at", archive),
            ("reconstruction_completed_at", reconstruction),
        ):
            if timestamp_value is not None:
                try:
                    _require_timestamp(timestamp_name, timestamp_value)
                except ValueError as exc:
                    raise ClassificationIntegrityError(
                        f"{timestamp_name} is not a normalized UTC timestamp"
                    ) from exc
        if kind == "LEDGER_CONTEMPORANEOUS_CAPTURE":
            if origin != "LEDGER_AUTHORIZED_CAPTURE" or archive != observation.retrieved_at or archive > pit_reference_at or reconstruction is not None:
                raise ClassificationIntegrityError("invalid contemporaneous-capture evidence")
        elif kind == "INDEPENDENT_ARCHIVE_PROOF":
            if archive is None or reconstruction is not None or not support:
                raise ClassificationIntegrityError("independent archive proof is incomplete")
            if archive > pit_reference_at:
                raise ClassificationIntegrityError("archive proof is after PIT reference")
        elif kind == "POST_REFERENCE_RECONSTRUCTION":
            if archive is not None or reconstruction is None or not support:
                raise ClassificationIntegrityError("reconstruction evidence is incomplete")
            if not (pit_reference_at < reconstruction <= observation.retrieved_at):
                raise ClassificationIntegrityError("reconstruction timestamps are invalid")
        elif kind in {"INSUFFICIENT_PIT_EVIDENCE", "PIT_IRRELEVANT_CONTENT"}:
            if archive is not None or reconstruction is not None:
                raise ClassificationIntegrityError("evidence timestamps must be null")
        if kind == "PIT_IRRELEVANT_CONTENT":
            allowed = {_POLICY_ARTIFACT_KIND}
            if policy_version == REQUIRED_ANCESTRY_PIT_V2:
                allowed.add("ledger.llm-raw-response.v1")
            if self.get_artifact(observation.artifact_id).artifact_kind not in allowed:
                raise ClassificationIntegrityError("Artifact kind is not PIT-irrelevant under policy")
        return record, matrix[kind][2], matrix[kind][3]

    def commit_run(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        run_id: str | None = None,
        output_artifact_ids: tuple[str, ...] = (),
        run_inputs: tuple[RunInputBinding, ...] = (),
        derivation_node_ids: tuple[str, ...] = (),
        derivation_node_classification_ids: tuple[str, ...] = (),
    ) -> Run:
        """Atomically commit a successful run and its exact derivation closure."""

        if len(output_artifact_ids) != len(set(output_artifact_ids)):
            raise FinalizationConflict("output artifact IDs must be unique")
        if len(derivation_node_ids) != len(set(derivation_node_ids)):
            raise FinalizationConflict("selected derivation node IDs must be unique")
        if len(derivation_node_classification_ids) != len(set(derivation_node_classification_ids)):
            raise FinalizationConflict("classification IDs must be unique")
        sorted_outputs = tuple(sorted(output_artifact_ids))
        selected_nodes = tuple(sorted(derivation_node_ids))
        normalized_inputs = _validate_run_input_bindings(run_inputs)
        attempt = self.get_attempt(attempt_id)
        request = self.get_run_request(attempt.run_request_id)
        existing_row = self.connection.execute(
            "SELECT * FROM run WHERE attempt_id = ? OR run_request_id = ?",
            (attempt_id, attempt.run_request_id),
        ).fetchone()
        effective_run_id = run_id or (existing_row["run_id"] if existing_row else _new_id())
        if effective_run_id in {attempt_id, attempt.run_request_id}:
            raise FinalizationConflict("request, attempt, and run IDs must be pairwise unequal")
        closure_ids, input_observation_ids = self._prepare_derivation_commit(
            attempt_id=attempt_id,
            selected_node_ids=selected_nodes,
            run_inputs=normalized_inputs,
            analysis_cutoff_at=request.analysis_cutoff_at,
            expected_run_id=effective_run_id if existing_row is not None else None,
        )
        classified = request.request_kind in _CLASSIFIED_REQUEST_KINDS
        if classified:
            classification_ids, pit_reference_at, resolved_record, resolved_pit, pit_policy_version = self._prepare_classification_commit(
                attempt_id=attempt_id, request=request, selected_node_ids=selected_nodes,
                classification_ids=tuple(sorted(derivation_node_classification_ids)),
            )
        else:
            if derivation_node_classification_ids:
                raise FinalizationConflict("unclassified request kind cannot create manifest V2")
            classification_ids, pit_reference_at, resolved_record, resolved_pit, pit_policy_version = (), None, None, None, None
        manifest_version = (
            "3" if pit_policy_version == REQUIRED_ANCESTRY_PIT_V2
            else "2" if classified else "1"
        )
        research_ids = tuple(sorted(
            self.get_derivation_node(node_id, verify=False).entity_id
            for node_id in closure_ids
            if self.get_derivation_node(node_id, verify=False).node_kind == "RESEARCH"
        ))
        setup_ids = tuple(sorted(
            self.get_derivation_node(node_id, verify=False).entity_id
            for node_id in closure_ids
            if self.get_derivation_node(node_id, verify=False).node_kind == "SETUP"
        ))
        if manifest_version != "3" and (research_ids or setup_ids):
            raise FinalizationConflict("Research/Setup closure requires Manifest V3")
        policy_references = (() if not closure_ids else (
            {"policy_kind": "DERIVATION", "policy_version": MAX_REQUIRED_PARENTS_V1},
            *(({"policy_kind": "PIT_CLASSIFICATION", "policy_version": pit_policy_version},) if classified else ()),
        ))
        manifest_value = _result_manifest_value(
            run_id=effective_run_id,
            request=request,
            attempt=attempt,
            output_artifact_ids=sorted_outputs,
            input_observation_ids=input_observation_ids,
            derivation_node_ids=closure_ids,
            policy_references=policy_references,
            pit_reference_at=pit_reference_at,
            derivation_node_classification_ids=classification_ids,
            resolved_record_class=resolved_record,
            resolved_pit_class=resolved_pit,
            manifest_version=manifest_version,
            research_ids=research_ids,
            setup_ids=setup_ids,
        )
        manifest_payload = canonicalize_json(manifest_value)
        manifest_kind = (
            _MANIFEST_V3_KIND if manifest_version == "3"
            else _MANIFEST_V2_KIND if classified else "ledger.run-result-manifest.v1"
        )
        manifest_id = artifact_id_for(
            manifest_kind, CANONICAL_JSON_V1, manifest_payload
        )
        if existing_row is not None:
            existing = _run_from_row(existing_row)
            if (
                existing.run_id == effective_run_id
                and existing.attempt_id == attempt_id
                and existing.result_manifest_artifact_id == manifest_id
            ):
                stored_inputs = {
                    (row[0], row[1], row[2])
                    for row in self.connection.execute(
                        """
                        SELECT input_observation_id, input_role, derivation_node_id
                        FROM run_input WHERE run_id = ?
                        """,
                        (existing.run_id,),
                    )
                }
                requested_inputs = {
                    (
                        item.input_observation_id,
                        item.input_role,
                        item.derivation_node_id,
                    )
                    for item in normalized_inputs
                }
                if stored_inputs != requested_inputs:
                    raise FinalizationConflict("run input closure differs from committed run")
                return self.get_run(existing.run_id)
            raise FinalizationConflict("request or attempt already has incompatible run closure")

        with (
            self.transaction() as connection,
            self._internal_write(),
            self._run_binding(),
        ):
            attempt = _attempt_from_row(self._require_attempt_authority(attempt_id, fence_token))
            checked_closure, checked_inputs = self._prepare_derivation_commit(
                attempt_id=attempt_id,
                selected_node_ids=selected_nodes,
                run_inputs=normalized_inputs,
                analysis_cutoff_at=request.analysis_cutoff_at,
                expected_run_id=None,
            )
            if checked_closure != closure_ids or checked_inputs != input_observation_ids:
                raise FinalizationConflict("derivation closure changed during finalization")
            if classified:
                checked_classification = self._prepare_classification_commit(
                    attempt_id=attempt_id, request=request, selected_node_ids=selected_nodes,
                    classification_ids=tuple(sorted(derivation_node_classification_ids)),
                )
                if checked_classification != (classification_ids, pit_reference_at, resolved_record, resolved_pit, pit_policy_version):
                    raise FinalizationConflict("classification closure changed during finalization")
            attached_outputs = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT artifact_id FROM attempt_artifact
                    WHERE attempt_id = ? AND role = 'OUTPUT'
                    """,
                    (attempt_id,),
                )
            }
            if attached_outputs != set(sorted_outputs):
                raise FinalizationConflict(
                    "manifest output IDs must equal the attempt's attached OUTPUT closure"
                )
            for artifact_id in sorted_outputs:
                self.get_artifact(artifact_id)
            manifest = self.insert_artifact(
                artifact_type=manifest_kind,
                canonicalization_version=CANONICAL_JSON_V1,
                payload=manifest_payload,
                media_type="application/json",
                claimed_artifact_id=manifest_id,
            )
            committed_at = _utc_now()
            self._attach_attempt_artifact(
                attempt_id, manifest.artifact_id, "RESULT_MANIFEST", committed_at
            )
            connection.execute(
                """
                INSERT INTO run (
                    run_id, run_request_id, attempt_id, committed_at, status,
                    result_manifest_artifact_id
                ) VALUES (?, ?, ?, ?, 'COMMITTED', ?)
                """,
                (
                    effective_run_id,
                    attempt.run_request_id,
                    attempt_id,
                    committed_at,
                    manifest.artifact_id,
                ),
            )
            for node_id in closure_ids:
                connection.execute(
                    "UPDATE derivation_node SET run_id = ? WHERE derivation_node_id = ?",
                    (effective_run_id, node_id),
                )
            for research_id in research_ids:
                connection.execute(
                    "UPDATE research_record SET run_id = ? WHERE research_id = ?",
                    (effective_run_id, research_id),
                )
            for setup_id in setup_ids:
                connection.execute(
                    "UPDATE setup SET run_id = ? WHERE setup_id = ?",
                    (effective_run_id, setup_id),
                )
            for node_id in selected_nodes:
                self.verify_derivation(node_id)
            for item in normalized_inputs:
                connection.execute(
                    """
                    INSERT INTO run_input (
                        run_input_id, run_id, input_observation_id, input_role,
                        derivation_node_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id(excluding={effective_run_id, attempt_id}),
                        effective_run_id,
                        item.input_observation_id,
                        item.input_role,
                        item.derivation_node_id,
                        committed_at,
                    ),
                )
            self._append_attempt_event(
                attempt_id=attempt_id,
                event_type="SUCCEEDED",
                event_at=committed_at,
                from_status="RUNNING",
                to_status="SUCCEEDED",
                reason_code=None,
                failure_class=None,
                failure_message=None,
                actor_identity=attempt.worker_identity,
                details_artifact_id=None,
            )
            connection.execute(
                """
                UPDATE attempt
                SET status = 'SUCCEEDED', finished_at = ?,
                    failure_class = NULL, failure_message = NULL
                WHERE attempt_id = ?
                """,
                (committed_at, attempt_id),
            )
            connection.execute(
                """
                UPDATE run_request_control
                SET request_state = 'COMMITTED', committed_run_id = ?, updated_at = ?
                WHERE run_request_id = ?
                """,
                (effective_run_id, committed_at, attempt.run_request_id),
            )
        return self.get_run(effective_run_id)

    def get_run(self, run_id: str) -> Run:
        row = self.connection.execute(
            "SELECT * FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        run = _run_from_row(row)
        manifest = self.get_artifact(run.result_manifest_artifact_id)
        if manifest.artifact_kind not in {"ledger.run-result-manifest.v1", _MANIFEST_V2_KIND, _MANIFEST_V3_KIND}:
            raise ArtifactIntegrityError("run manifest has incorrect artifact kind")
        value = json.loads(manifest.payload)
        classified = manifest.artifact_kind in {_MANIFEST_V2_KIND, _MANIFEST_V3_KIND}
        manifest_version = {
            "ledger.run-result-manifest.v1": "1", _MANIFEST_V2_KIND: "2", _MANIFEST_V3_KIND: "3",
        }[manifest.artifact_kind]
        if value.get("manifest_version") != manifest_version:
            raise ArtifactIntegrityError("run manifest version does not match Artifact kind")
        list_fields = (
            "output_artifact_ids",
            "input_observation_ids",
            "derivation_node_ids",
            "policy_references",
        )
        if any(not isinstance(value.get(field), list) for field in list_fields):
            raise ArtifactIntegrityError("run manifest closure arrays are malformed")
        output_ids = value["output_artifact_ids"]
        input_ids = value["input_observation_ids"]
        node_ids = value["derivation_node_ids"]
        policy_references = value["policy_references"]
        classification_ids = value.get("derivation_node_classification_ids", [])
        if classified and not isinstance(classification_ids, list):
            raise ArtifactIntegrityError("run manifest classification closure is malformed")
        if not all(isinstance(item, str) for item in output_ids + input_ids + node_ids):
            raise ArtifactIntegrityError("run manifest identity arrays are malformed")
        expected = _result_manifest_value(
            run_id=run.run_id,
            request=self.get_run_request(run.run_request_id),
            attempt=self.get_attempt(run.attempt_id),
            output_artifact_ids=tuple(output_ids),
            input_observation_ids=tuple(input_ids),
            derivation_node_ids=tuple(node_ids),
            policy_references=tuple(policy_references),
            pit_reference_at=value.get("pit_reference_at") if classified else None,
            derivation_node_classification_ids=tuple(classification_ids),
            resolved_record_class=value.get("resolved_record_class") if classified else None,
            resolved_pit_class=value.get("resolved_pit_class") if classified else None,
            manifest_version=manifest_version,
            research_ids=tuple(value.get("research_ids", [])),
            setup_ids=tuple(value.get("setup_ids", [])),
        )
        if (
            value != expected
            or output_ids != sorted(set(output_ids))
            or input_ids != sorted(set(input_ids))
            or node_ids != sorted(set(node_ids))
        ):
            raise ArtifactIntegrityError("run manifest does not match committed closure")
        manifest_link = self.connection.execute(
            """
            SELECT 1 FROM attempt_artifact
            WHERE attempt_id = ? AND artifact_id = ? AND role = 'RESULT_MANIFEST'
            """,
            (run.attempt_id, run.result_manifest_artifact_id),
        ).fetchone()
        if manifest_link is None:
            raise ArtifactIntegrityError("run manifest is not attached to its attempt")
        attached_outputs = {
            row[0]
            for row in self.connection.execute(
                """
                SELECT artifact_id FROM attempt_artifact
                WHERE attempt_id = ? AND role = 'OUTPUT'
                """,
                (run.attempt_id,),
            )
        }
        if set(output_ids) != attached_outputs:
            raise ArtifactIntegrityError("run manifest output closure is incomplete")
        if node_ids:
            expected_policy = [
                {
                    "policy_kind": "DERIVATION",
                    "policy_version": MAX_REQUIRED_PARENTS_V1,
                }
            ]
            if classified:
                expected_policy.append(
                    {"policy_kind": "PIT_CLASSIFICATION", "policy_version": (
                        REQUIRED_ANCESTRY_PIT_V2 if manifest_version == "3" else REQUIRED_ANCESTRY_PIT_V1
                    )}
                )
            if policy_references != expected_policy:
                raise ArtifactIntegrityError("run manifest derivation policy is invalid")
            manifest_nodes = set(node_ids)
            for node_id in node_ids:
                node = self.get_derivation_node(node_id)
                if node.run_id != run.run_id or node.attempt_id != run.attempt_id:
                    raise ArtifactIntegrityError("manifest node has invalid run lineage")
                parent_ids = {
                    edge.parent_node_id for edge in self.list_derivation_edges(node_id)
                }
                if not parent_ids <= manifest_nodes:
                    raise ArtifactIntegrityError("manifest derivation closure is incomplete")
            research_targets = sorted(
                self.get_derivation_node(node_id, verify=False).entity_id
                for node_id in node_ids
                if self.get_derivation_node(node_id, verify=False).node_kind == "RESEARCH"
            )
            setup_targets = sorted(
                self.get_derivation_node(node_id, verify=False).entity_id
                for node_id in node_ids
                if self.get_derivation_node(node_id, verify=False).node_kind == "SETUP"
            )
            if manifest_version == "3":
                if value.get("research_ids") != research_targets or value.get("setup_ids") != setup_targets:
                    raise ArtifactIntegrityError("Manifest V3 entity IDs differ from DAG targets")
                if value.get("eligibility_ids") or value.get("trade_ids") or value.get("outcome_ids"):
                    raise ArtifactIntegrityError("Manifest V3 future entity arrays must be empty")
                for research_id in research_targets:
                    if self.get_research_record(research_id, verify=False).run_id != run.run_id:
                        raise ArtifactIntegrityError("Manifest V3 Research is not run-bound")
                for setup_id in setup_targets:
                    if self.get_setup(setup_id, verify=False).run_id != run.run_id:
                        raise ArtifactIntegrityError("Manifest V3 Setup is not run-bound")
            elif research_targets or setup_targets:
                raise ArtifactIntegrityError("Research/Setup targets require Manifest V3")
        elif policy_references:
            raise ArtifactIntegrityError("manifest has policy without derivation graph")
        stored_input_ids = {
            row[0]
            for row in self.connection.execute(
                "SELECT input_observation_id FROM run_input WHERE run_id = ?",
                (run.run_id,),
            )
        }
        if set(input_ids) != stored_input_ids:
            raise ArtifactIntegrityError("manifest input-observation closure is incomplete")
        if classified:
            manifest_nodes = set(node_ids)
            parent_nodes = {
                edge.parent_node_id
                for node_id in node_ids
                for edge in self.list_derivation_edges(node_id)
                if edge.parent_node_id in manifest_nodes
            }
            roots = tuple(sorted(manifest_nodes - parent_nodes))
            try:
                prepared = self._prepare_classification_commit(
                    attempt_id=run.attempt_id,
                    request=self.get_run_request(run.run_request_id),
                    selected_node_ids=roots,
                    classification_ids=tuple(classification_ids),
                )
            except (ClassificationIntegrityError, FinalizationConflict, KeyError) as exc:
                raise ArtifactIntegrityError("run classification closure is invalid") from exc
            if prepared != (
                tuple(classification_ids), value["pit_reference_at"],
                value["resolved_record_class"], value["resolved_pit_class"],
                REQUIRED_ANCESTRY_PIT_V2 if manifest_version == "3" else REQUIRED_ANCESTRY_PIT_V1,
            ):
                raise ArtifactIntegrityError("run classification result is invalid")
        return run

    def get_run_classification_status(self, run_id: str) -> str:
        run = self.get_run(run_id)
        artifact = self.get_artifact(run.result_manifest_artifact_id)
        return "CLASSIFIED" if artifact.artifact_kind in {_MANIFEST_V2_KIND, _MANIFEST_V3_KIND} else "UNCLASSIFIED"

    def _prepare_classification_commit(
        self, *, attempt_id: str, request: RunRequest,
        selected_node_ids: tuple[str, ...], classification_ids: tuple[str, ...],
    ) -> tuple[tuple[str, ...], str, str, str, str]:
        if not selected_node_ids:
            raise FinalizationConflict("classified run requires selected derivation roots")
        required_nodes: set[str] = set()
        for node_id in selected_node_ids:
            required_nodes.update(self._derivation_closure((node_id,), required_only=True))
        classifications = [self.verify_node_classification(item) for item in classification_ids]
        by_node = {item.derivation_node_id: item for item in classifications}
        if len(by_node) != len(classifications) or set(by_node) != required_nodes:
            raise FinalizationConflict("classification IDs must exactly cover REQUIRED node closure")
        if any(item.classified_by_attempt_id != attempt_id for item in classifications):
            raise FinalizationConflict("classification belongs to a different consuming attempt")
        references = {item.pit_reference_at for item in classifications}
        if len(references) != 1:
            raise FinalizationConflict("classification closure must share one PIT reference")
        reference = next(iter(references))
        policies = {
            (p.policy_kind, p.policy_version)
            for p in (
                self.get_pit_classification_policy(item.classification_policy_id)
                for item in classifications
            )
        }
        if len(policies) != 1 or next(iter(policies))[0] != "PIT_CLASSIFICATION" or next(iter(policies))[1] not in {
            REQUIRED_ANCESTRY_PIT_V1, REQUIRED_ANCESTRY_PIT_V2,
        }:
            raise FinalizationConflict("classification closure uses unsupported policy")
        policy_version = next(iter(policies))[1]
        expected_reference = _request_pit_reference(request)
        if reference != expected_reference:
            raise FinalizationConflict("classification PIT reference differs from request")
        roots = [by_node[node_id] for node_id in selected_node_ids]
        resolved_record = (
            "LEGACY_NON_LEDGER_ARTIFACT"
            if any(item.resolved_record_class == "LEGACY_NON_LEDGER_ARTIFACT" for item in roots)
            else "LEDGER_NATIVE"
        )
        resolved_pit = _fold_pit_classes([item.pit_class for item in roots])
        allowed = {
            "PIT_SAFE_DECISION": {"ARCHIVED_POINT_IN_TIME", "NOT_APPLICABLE"},
            "RECONSTRUCTION": {"ARCHIVED_POINT_IN_TIME", "RECONSTRUCTED_NOT_ARCHIVED", "NOT_APPLICABLE"},
            "EXPLORATORY_NON_PIT": {"ARCHIVED_POINT_IN_TIME", "RECONSTRUCTED_NOT_ARCHIVED", "NOT_APPLICABLE", "UNKNOWN"},
        }[request.request_kind]
        if resolved_pit not in allowed:
            raise FinalizationConflict("resolved PIT class is forbidden for request kind")
        return tuple(sorted(classification_ids)), reference, resolved_record, resolved_pit, policy_version

    def _prepare_derivation_commit(
        self,
        *,
        attempt_id: str,
        selected_node_ids: tuple[str, ...],
        run_inputs: tuple[RunInputBinding, ...],
        analysis_cutoff_at: str,
        expected_run_id: str | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not selected_node_ids:
            if run_inputs:
                raise UnsupportedRunInput(
                    "run inputs require selected derivation result nodes"
                )
            return (), ()

        full_closure: set[str] = set()
        required_closure: set[str] = set()
        self.verify_derivations(selected_node_ids)
        for node_id in selected_node_ids:
            full_closure.update(
                self._derivation_closure((node_id,), required_only=False)
            )
            required_closure.update(
                self._derivation_closure((node_id,), required_only=True)
            )

        for node_id in full_closure:
            node = self.get_derivation_node(node_id, verify=False)
            if node.attempt_id != attempt_id:
                raise FinalizationConflict("cannot adopt a foreign-attempt derivation node")
            if node.run_id != expected_run_id:
                expected = "attempt-local" if expected_run_id is None else expected_run_id
                raise FinalizationConflict(
                    f"derivation node is not bound to expected lineage {expected!r}"
                )

        for node_id in required_closure:
            node = self.get_derivation_node(node_id, verify=False)
            if node.derived_available_at > analysis_cutoff_at:
                raise FinalizationConflict(
                    "required derivation availability exceeds analysis cutoff"
                )

        required_raw: dict[str, DerivationNode] = {
            node_id: node
            for node_id in required_closure
            if (node := self.get_derivation_node(node_id, verify=False)).node_kind
            == "INPUT_OBSERVATION"
        }
        covered_nodes: set[str] = set()
        for item in run_inputs:
            node = required_raw.get(item.derivation_node_id)
            if node is None:
                raise UnsupportedRunInput(
                    "run input must reference a raw node in required ancestry"
                )
            if node.entity_id != item.input_observation_id:
                raise UnsupportedRunInput(
                    "run input observation does not match its raw derivation node"
                )
            covered_nodes.add(item.derivation_node_id)
        if covered_nodes != set(required_raw):
            raise UnsupportedRunInput(
                "every required raw derivation node needs run_input coverage"
            )

        input_ids = tuple(sorted({item.input_observation_id for item in run_inputs}))
        return tuple(sorted(full_closure)), input_ids

    def _verify_derivation_node(
        self,
        derivation_node_id: str,
        *,
        states: dict[str, str],
        memo: dict[str, DerivationNode],
    ) -> DerivationNode:
        state = states.get(derivation_node_id, "UNVISITED")
        if state == "VISITING":
            raise DerivationIntegrityError("derivation graph contains a cycle")
        if state == "VALIDATED":
            return memo[derivation_node_id]
        row = self.connection.execute(
            "SELECT * FROM derivation_node WHERE derivation_node_id = ?",
            (derivation_node_id,),
        ).fetchone()
        if row is None:
            raise DerivationIntegrityError(
                f"derivation node does not exist: {derivation_node_id!r}"
            )
        node = _derivation_node_from_row(row)
        states[derivation_node_id] = "VISITING"
        try:
            _require_derivation_policy(node.derivation_policy_version)
            expected_entity_type = _SUPPORTED_NODE_MAPPINGS.get(node.node_kind)
            if expected_entity_type is None:
                if node.node_kind in _RESERVED_NODE_KINDS:
                    raise DerivationIntegrityError(
                        f"reserved node kind is not instantiable: {node.node_kind}"
                    )
                raise DerivationIntegrityError(f"unknown node kind: {node.node_kind!r}")
            if node.entity_type != expected_entity_type:
                raise DerivationIntegrityError("invalid node-kind/entity-type mapping")
            try:
                _require_timestamp("derived_available_at", node.derived_available_at)
                if node.direct_available_at is not None:
                    _require_timestamp("direct_available_at", node.direct_available_at)
            except ValueError as exc:
                raise DerivationIntegrityError(str(exc)) from exc

            edges = self.list_derivation_edges(derivation_node_id)
            seen_edges: set[tuple[str, str, str]] = set()
            parent_nodes: dict[str, DerivationNode] = {}
            for edge in edges:
                key = (edge.parent_node_id, edge.child_node_id, edge.edge_role)
                if key in seen_edges:
                    raise DerivationIntegrityError("duplicate derivation edge")
                seen_edges.add(key)
                if edge.child_node_id != derivation_node_id:
                    raise DerivationIntegrityError("edge child identity is inconsistent")
                if edge.parent_node_id == derivation_node_id:
                    raise DerivationIntegrityError("derivation self-edge is forbidden")
                if edge.required not in {0, 1}:
                    raise DerivationIntegrityError("edge required flag must be 0 or 1")
                if not edge.edge_role or "\0" in edge.edge_role:
                    raise DerivationIntegrityError("edge role is invalid")
                parent = self._verify_derivation_node(
                    edge.parent_node_id, states=states, memo=memo
                )
                if parent.attempt_id != node.attempt_id:
                    raise DerivationIntegrityError("cross-attempt edge is forbidden")
                if parent.run_id != node.run_id:
                    raise DerivationIntegrityError("edge endpoints have inconsistent run lineage")
                parent_nodes[edge.parent_node_id] = parent

            if node.node_kind == "INPUT_OBSERVATION":
                if edges:
                    raise DerivationIntegrityError("raw input node must have zero parents")
                try:
                    observation = self.get_input_observation(node.entity_id)
                except KeyError as exc:
                    raise DerivationIntegrityError(
                        "raw node observation target does not exist"
                    ) from exc
                expected = observation.effective_available_at
                if node.direct_available_at != expected:
                    raise DerivationIntegrityError(
                        "raw direct availability differs from observation"
                    )
            else:
                try:
                    if node.node_kind == "NORMALIZED_FACT":
                        self.get_artifact(node.entity_id)
                    elif node.node_kind == "RESEARCH":
                        target = self.connection.execute(
                            "SELECT attempt_id, run_id, derivation_node_id FROM research_record WHERE research_id = ?",
                            (node.entity_id,),
                        ).fetchone()
                        if target is None or (target["attempt_id"], target["run_id"], target["derivation_node_id"]) != (node.attempt_id, node.run_id, node.derivation_node_id):
                            raise KeyError(node.entity_id)
                    elif node.node_kind == "SETUP":
                        target = self.connection.execute(
                            "SELECT attempt_id, run_id, derivation_node_id FROM setup WHERE setup_id = ?",
                            (node.entity_id,),
                        ).fetchone()
                        if target is None or (target["attempt_id"], target["run_id"], target["derivation_node_id"]) != (node.attempt_id, node.run_id, node.derivation_node_id):
                            raise KeyError(node.entity_id)
                except (KeyError, ArtifactIntegrityError) as exc:
                    raise DerivationIntegrityError(
                        "derived node target is invalid"
                    ) from exc
                if node.direct_available_at is not None:
                    raise DerivationIntegrityError(
                        "derived node direct_available_at must be null"
                    )
                required_edges = [edge for edge in edges if edge.required == 1]
                if not required_edges:
                    raise DerivationIntegrityError(
                        "derived node requires at least one required parent"
                    )
                expected = max(
                    parent_nodes[edge.parent_node_id].derived_available_at
                    for edge in required_edges
                )

            if node.derived_available_at != expected:
                raise DerivationIntegrityError(
                    "stored derived availability differs from recursive recomputation"
                )
            states[derivation_node_id] = "VALIDATED"
            memo[derivation_node_id] = node
            return node
        except Exception:
            states.pop(derivation_node_id, None)
            raise

    def _get_derivation_ancestry(
        self, derivation_node_id: str, *, required_only: bool
    ) -> list[DerivationNode]:
        self.verify_derivation(derivation_node_id)
        closure = self._derivation_closure(
            (derivation_node_id,), required_only=required_only
        )
        closure.discard(derivation_node_id)
        if not closure:
            return []

        indegree = {node_id: 0 for node_id in closure}
        children: dict[str, set[str]] = {node_id: set() for node_id in closure}
        placeholders = ",".join("?" for _ in closure)
        condition = "AND required = 1" if required_only else ""
        rows = self.connection.execute(
            f"""
            SELECT parent_node_id, child_node_id FROM derivation_edge
            WHERE parent_node_id IN ({placeholders})
              AND child_node_id IN ({placeholders})
              {condition}
            """,
            (*closure, *closure),
        ).fetchall()
        for row in rows:
            parent_id, child_id = str(row[0]), str(row[1])
            if child_id not in children[parent_id]:
                children[parent_id].add(child_id)
                indegree[child_id] += 1

        available = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
        ordered: list[str] = []
        while available:
            current = available.pop(0)
            ordered.append(current)
            for child in sorted(children[current]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    available.append(child)
                    available.sort()
        if len(ordered) != len(closure):
            raise DerivationIntegrityError("derivation ancestry contains a cycle")
        return [self.get_derivation_node(node_id, verify=False) for node_id in ordered]

    def _derivation_closure(
        self, roots: tuple[str, ...], *, required_only: bool
    ) -> set[str]:
        closure: set[str] = set()
        pending = list(roots)
        while pending:
            node_id = pending.pop()
            if node_id in closure:
                continue
            row = self.connection.execute(
                "SELECT 1 FROM derivation_node WHERE derivation_node_id = ?",
                (node_id,),
            ).fetchone()
            if row is None:
                raise DerivationIntegrityError(f"missing derivation node: {node_id!r}")
            closure.add(node_id)
            query = """
                SELECT parent_node_id FROM derivation_edge
                WHERE child_node_id = ?
            """
            parameters: tuple[object, ...] = (node_id,)
            if required_only:
                query += " AND required = 1"
            query += " ORDER BY edge_role COLLATE BINARY, parent_node_id COLLATE BINARY"
            pending.extend(
                str(row[0])
                for row in self.connection.execute(query, parameters).fetchall()
            )
        return closure

    def _get_attempt_row(self, attempt_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM attempt WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return row

    def _require_attempt_authority(
        self, attempt_id: str, fence_token: int
    ) -> sqlite3.Row:
        row = self.connection.execute(
            """
            SELECT attempt.*
            FROM attempt
            JOIN run_request_control USING (run_request_id)
            WHERE attempt.attempt_id = ?
              AND attempt.fence_token = ?
              AND attempt.status = 'RUNNING'
              AND run_request_control.request_state = 'ACTIVE'
              AND run_request_control.current_attempt_id = attempt.attempt_id
              AND run_request_control.current_fence_token = attempt.fence_token
            """,
            (attempt_id, fence_token),
        ).fetchone()
        if row is None:
            raise StaleAttemptError("attempt does not hold current write authority")
        return row

    def _append_attempt_event(
        self,
        *,
        attempt_id: str,
        event_type: str,
        event_at: str,
        from_status: str | None,
        to_status: str,
        reason_code: str | None,
        failure_class: str | None,
        failure_message: str | None,
        actor_identity: str,
        details_artifact_id: str | None,
    ) -> str:
        sequence = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM attempt_event WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
        )
        event_id = _new_id()
        self.connection.execute(
            """
            INSERT INTO attempt_event (
                attempt_event_id, attempt_id, sequence_number, event_type,
                event_at, from_status, to_status, reason_code, failure_class,
                failure_message, actor_identity, details_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                attempt_id,
                sequence,
                event_type,
                event_at,
                from_status,
                to_status,
                reason_code,
                failure_class,
                failure_message,
                actor_identity,
                details_artifact_id,
                _utc_now(),
            ),
        )
        return event_id

    def _attach_attempt_artifact(
        self, attempt_id: str, artifact_id: str, role: str, attached_at: str
    ) -> str:
        existing = self.connection.execute(
            """
            SELECT attempt_artifact_id FROM attempt_artifact
            WHERE attempt_id = ? AND artifact_id = ? AND role = ?
            """,
            (attempt_id, artifact_id, role),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        ordinal = int(
            self.connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1 FROM attempt_artifact
                WHERE attempt_id = ? AND role = ?
                """,
                (attempt_id, role),
            ).fetchone()[0]
        )
        link_id = _new_id()
        self.connection.execute(
            """
            INSERT INTO attempt_artifact (
                attempt_artifact_id, attempt_id, artifact_id, role, attached_at, ordinal
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (link_id, attempt_id, artifact_id, role, attached_at, ordinal),
        )
        return link_id

    def _terminalization_matches(
        self,
        row: sqlite3.Row,
        *,
        status: str,
        reason_code: str | None,
        failure_class: str | None,
        failure_message: str | None,
        actor_identity: str,
        details_artifact_id: str | None,
    ) -> bool:
        if row["status"] != status:
            return False
        event = self.connection.execute(
            """
            SELECT * FROM attempt_event
            WHERE attempt_id = ? AND event_type = ?
            ORDER BY sequence_number DESC LIMIT 1
            """,
            (row["attempt_id"], status),
        ).fetchone()
        return event is not None and (
            event["reason_code"],
            event["failure_class"],
            event["failure_message"],
            event["actor_identity"],
            event["details_artifact_id"],
        ) == (
            reason_code,
            failure_class,
            failure_message,
            actor_identity,
            details_artifact_id,
        )

    def _read_migration_history(self) -> list[sqlite3.Row]:
        table_exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
        ).fetchone()
        if table_exists is None:
            return []
        return self.connection.execute(
            """
            SELECT migration_id, schema_version, sha256
            FROM schema_migration
            ORDER BY schema_version
            """
        ).fetchall()

    def _validate_schema(self, bootstrap: Migration) -> None:
        row = self.connection.execute(
            """
            SELECT schema_version, freeze_identifier, applied_migration_id
            FROM schema_metadata
            WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None or row["schema_version"] != SCHEMA_VERSION:
            raise UnsupportedSchemaVersion("missing or unsupported schema_metadata version")
        if row["freeze_identifier"] != FREEZE_IDENTIFIER:
            raise UnsupportedSchemaVersion("database freeze identifier is unsupported")
        if row["applied_migration_id"] != bootstrap.migration_id:
            raise MigrationIdentityDrift(
                "schema_metadata bootstrap migration identity does not match registry"
            )

    @staticmethod
    def _assert_artifact_metadata(
        artifact: Artifact,
        *,
        artifact_type: str,
        canonicalization_version: str,
        media_type: str,
    ) -> None:
        expected = (artifact_type, canonicalization_version, media_type)
        actual = (
            artifact.artifact_kind,
            artifact.canonicalization_version,
            artifact.media_type,
        )
        if actual != expected:
            raise ArtifactMetadataConflict(
                f"artifact metadata conflict: stored={actual!r}, requested={expected!r}"
            )


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.DatabaseError("SQLite foreign key enforcement could not be enabled")
    journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(journal_mode).lower() != "wal":
        raise sqlite3.DatabaseError("SQLite WAL journal mode could not be enabled")
    connection.execute("PRAGMA synchronous = FULL")


def _validate_canonical_payload(version: str, payload: bytes) -> bytes:
    if version == IDENTITY:
        return canonicalize_opaque(payload)
    if version == CANONICAL_JSON_V1:
        supplied = canonicalize_opaque(payload)
        canonical = canonicalize_json_document(supplied)
        if canonical != supplied:
            raise CanonicalizationError(
                "structured artifact payload must already be canonical UTF-8 JSON"
            )
        return canonical
    raise CanonicalizationError(f"unsupported canonicalization version: {version!r}")


def _validate_migration_history(
    rows: list[sqlite3.Row], registry: MigrationRegistry
) -> None:
    sequences = [int(row["schema_version"]) for row in rows]
    expected_sequences = list(range(1, len(rows) + 1))
    if sequences != expected_sequences:
        raise MigrationHistoryError(
            f"applied migration history must start at 1 without gaps: got {sequences}"
        )
    if len(rows) > len(registry):
        unknown = rows[len(registry)]["migration_id"]
        raise UnknownAppliedMigration(f"unknown applied migration: {unknown!r}")

    registered_positions = {
        migration.migration_id: migration.sequence for migration in registry
    }
    for row, expected in zip(rows, registry.migrations):
        actual_id = row["migration_id"]
        if actual_id != expected.migration_id:
            actual_position = registered_positions.get(actual_id)
            if actual_position is not None:
                raise MigrationHistoryError(
                    f"reordered migration history: {actual_id!r} is at sequence "
                    f"{row['schema_version']}, expected {actual_position}"
                )
            raise MigrationIdentityDrift(
                f"migration sequence {expected.sequence} has identity {actual_id!r}; "
                f"expected {expected.migration_id!r}"
            )
        if row["sha256"] != expected.sha256:
            raise MigrationHashDrift(
                f"migration {expected.migration_id!r} hash differs from registry"
            )


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    return Artifact(**{field: row[field] for field in Artifact.__dataclass_fields__})


def _run_request_from_row(row: sqlite3.Row) -> RunRequest:
    return RunRequest(**{field: row[field] for field in RunRequest.__dataclass_fields__})


def _attempt_from_row(row: sqlite3.Row) -> Attempt:
    return Attempt(**{field: row[field] for field in Attempt.__dataclass_fields__})


def _attempt_event_from_row(row: sqlite3.Row) -> AttemptEvent:
    return AttemptEvent(
        **{field: row[field] for field in AttemptEvent.__dataclass_fields__}
    )


def _run_from_row(row: sqlite3.Row) -> Run:
    return Run(**{field: row[field] for field in Run.__dataclass_fields__})


def _input_observation_from_row(row: sqlite3.Row) -> InputObservation:
    return InputObservation(
        **{field: row[field] for field in InputObservation.__dataclass_fields__}
    )


def _derivation_node_from_row(row: sqlite3.Row) -> DerivationNode:
    return DerivationNode(
        **{field: row[field] for field in DerivationNode.__dataclass_fields__}
    )


def _derivation_edge_from_row(row: sqlite3.Row) -> DerivationEdge:
    return DerivationEdge(
        **{field: row[field] for field in DerivationEdge.__dataclass_fields__}
    )


def _classification_policy_from_row(row: sqlite3.Row) -> PitClassificationPolicy:
    return PitClassificationPolicy(
        **{field: row[field] for field in PitClassificationPolicy.__dataclass_fields__}
    )


def _research_method_from_row(row: sqlite3.Row) -> ResearchMethod:
    return ResearchMethod(**dict(row))


def _llm_interaction_from_row(row: sqlite3.Row) -> LLMInteraction:
    return LLMInteraction(**dict(row))


def _research_record_from_row(row: sqlite3.Row) -> ResearchRecord:
    return ResearchRecord(**dict(row))


def _setup_policy_from_row(row: sqlite3.Row) -> SetupPolicy:
    return SetupPolicy(**dict(row))


def _setup_from_row(row: sqlite3.Row) -> Setup:
    return Setup(**dict(row))


def _classification_from_row(row: sqlite3.Row) -> DerivationNodeClassification:
    return DerivationNodeClassification(
        **{field: row[field] for field in DerivationNodeClassification.__dataclass_fields__}
    )


def _require_uuid4(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a lowercase UUIDv4")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a lowercase UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name} must be a lowercase UUIDv4")


def _require_artifact_envelope(artifact: Artifact, artifact_kind: str) -> None:
    if (artifact.artifact_kind, artifact.canonicalization_version, artifact.media_type) != (
        artifact_kind, CANONICAL_JSON_V1, "application/json"
    ):
        raise ClassificationIntegrityError("Artifact envelope differs from frozen V1.3")


def _validate_classified_request_parameters(
    request_kind: str, analysis_cutoff_at: str, parameters: Any
) -> None:
    if request_kind not in _CLASSIFIED_REQUEST_KINDS:
        return
    if not isinstance(parameters, dict):
        raise ValueError("classification-aware request parameters must be an object")
    historical = parameters.get("historical_as_of_at")
    explicit = parameters.get("pit_reference_at")
    if request_kind == "PIT_SAFE_DECISION":
        if "historical_as_of_at" in parameters or "pit_reference_at" in parameters:
            raise ValueError("PIT_SAFE_DECISION forbids explicit reference parameters")
    elif request_kind == "RECONSTRUCTION":
        if "historical_as_of_at" not in parameters or "pit_reference_at" in parameters:
            raise ValueError("RECONSTRUCTION requires only historical_as_of_at")
        _require_timestamp("historical_as_of_at", historical)
        if historical > analysis_cutoff_at:
            raise ValueError("historical_as_of_at exceeds analysis cutoff")
    else:
        if "pit_reference_at" not in parameters or "historical_as_of_at" in parameters:
            raise ValueError("EXPLORATORY_NON_PIT requires only pit_reference_at")
        _require_timestamp("pit_reference_at", explicit)
        if explicit > analysis_cutoff_at:
            raise ValueError("pit_reference_at exceeds analysis cutoff")


def _request_pit_reference(request: RunRequest) -> str:
    parameters = json.loads(request.parameters_json)
    _validate_classified_request_parameters(
        request.request_kind, request.analysis_cutoff_at, parameters
    )
    if request.request_kind == "PIT_SAFE_DECISION":
        return request.analysis_cutoff_at
    if request.request_kind == "RECONSTRUCTION":
        return str(parameters["historical_as_of_at"])
    if request.request_kind == "EXPLORATORY_NON_PIT":
        return str(parameters["pit_reference_at"])
    raise FinalizationConflict("request kind is not classification-aware")


def _fold_pit_classes(values: list[str]) -> str:
    if not values:
        raise ClassificationIntegrityError("derived classification requires a parent")
    rank = {
        "NOT_APPLICABLE": 0,
        "ARCHIVED_POINT_IN_TIME": 1,
        "RECONSTRUCTED_NOT_ARCHIVED": 2,
        "UNKNOWN": 3,
    }
    try:
        return max(values, key=rank.__getitem__)
    except KeyError as exc:
        raise ClassificationIntegrityError("unsupported PIT class") from exc


def _request_fingerprint(
    *,
    request_kind: str,
    analysis_cutoff_at: str,
    parameters_json: str,
    baseline_commit: str,
) -> str:
    value = {
        "request_kind": request_kind,
        "analysis_cutoff_at": analysis_cutoff_at,
        "parameters_json": json.loads(parameters_json),
        "baseline_commit": baseline_commit,
    }
    return hashlib.sha256(canonicalize_json(value)).hexdigest()


def _validate_research_stage_projection(
    content: dict[str, Any], analyst: dict[str, Any], critic: dict[str, Any]
) -> None:
    analysis_fields = (
        "fundamental_analysis", "earnings_and_news_analysis", "price_context",
        "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation",
    )
    revised = critic["revised_analysis"]
    accepted_claims = [
        {key: value for key, value in item.items() if key != "validation_errors"}
        for item in content["claim_refs"]
    ]
    expected = {
        "status": critic["status"],
        "evidence_confidence": critic["evidence_confidence"],
        "thesis_strength": critic["thesis_strength"],
        "critic_notes": critic["notes"],
        "event_assessments": critic["event_assessments"],
        "analyst_status": analyst["proposed_status"],
        "analyst_thesis_strength": analyst["thesis_strength"],
        "analyst_evidence_confidence": analyst["evidence_confidence"],
        "critic_status": critic["critic_status"],
        "critic_thesis_strength": critic["critic_thesis_strength"],
        "critic_evidence_confidence": critic["critic_evidence_confidence"],
    }
    if any(content[key] != value for key, value in expected.items()):
        raise ArtifactIntegrityError("Research content differs from exact Analyst/Critic stages")
    if any(content[field] != revised[field] for field in analysis_fields):
        raise ArtifactIntegrityError("Research analysis differs from Critic effective stage")
    if accepted_claims != revised["claim_refs"]:
        raise ArtifactIntegrityError("Research claims differ from Critic effective stage")


def _request_fingerprint_from_request(request: RunRequest) -> str:
    return _request_fingerprint(
        request_kind=request.request_kind,
        analysis_cutoff_at=request.analysis_cutoff_at,
        parameters_json=request.parameters_json,
        baseline_commit=request.baseline_commit,
    )


def _result_manifest_value(
    *,
    run_id: str,
    request: RunRequest,
    attempt: Attempt,
    output_artifact_ids: tuple[str, ...],
    input_observation_ids: tuple[str, ...] = (),
    derivation_node_ids: tuple[str, ...] = (),
    policy_references: tuple[dict[str, str], ...] = (),
    pit_reference_at: str | None = None,
    derivation_node_classification_ids: tuple[str, ...] = (),
    resolved_record_class: str | None = None,
    resolved_pit_class: str | None = None,
    manifest_version: str | None = None,
    research_ids: tuple[str, ...] = (),
    setup_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    classified = pit_reference_at is not None
    version = manifest_version or ("2" if classified else "1")
    if version not in {"1", "2", "3"} or ((version == "1") == classified):
        raise FinalizationConflict("manifest version is incompatible with classification")
    value = {
        "manifest_version": version,
        "run_id": run_id,
        "run_request_id": request.run_request_id,
        "attempt_id": attempt.attempt_id,
        "analysis_cutoff_at": request.analysis_cutoff_at,
        "request_kind": request.request_kind,
        "baseline_commit": request.baseline_commit,
        "attempt_code_commit": attempt.code_commit,
        "environment_fingerprint": attempt.environment_fingerprint,
        "policy_references": list(policy_references),
        "input_observation_ids": list(input_observation_ids),
        "derivation_node_ids": list(derivation_node_ids),
        "output_artifact_ids": list(output_artifact_ids),
        "research_ids": list(research_ids),
        "setup_ids": list(setup_ids),
        "eligibility_ids": [],
        "trade_ids": [],
        "outcome_ids": [],
    }
    if classified:
        value.update(
            {
                "pit_reference_at": pit_reference_at,
                "derivation_node_classification_ids": list(
                    derivation_node_classification_ids
                ),
                "resolved_record_class": resolved_record_class,
                "resolved_pit_class": resolved_pit_class,
            }
        )
    return value


def _new_id(*, excluding: set[str] | None = None) -> str:
    excluded = excluding or set()
    while True:
        value = str(uuid.uuid4())
        if value not in excluded:
            return value


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")


def _require_timestamp(name: str, value: object) -> None:
    _require_text(name, value)
    assert isinstance(value, str)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value):
        raise ValueError(f"{name} must be an RFC 3339 UTC timestamp with microseconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid UTC timestamp") from exc


def _validate_source_id(source_id: object) -> None:
    if not isinstance(source_id, str) or not _SOURCE_ID_RE.fullmatch(source_id):
        raise InvalidObservationProvenance(
            "source_id must be provider:dataset[:contract-version] using lowercase "
            "ASCII components"
        )


def _require_derivation_policy(policy_version: object) -> None:
    if policy_version != MAX_REQUIRED_PARENTS_V1:
        raise UnsupportedDerivationPolicy(
            f"unsupported derivation policy: {policy_version!r}"
        )


def _validate_run_input_bindings(
    run_inputs: tuple[RunInputBinding, ...],
) -> tuple[RunInputBinding, ...]:
    if not isinstance(run_inputs, tuple):
        raise UnsupportedRunInput("run_inputs must be an immutable tuple")
    result: list[RunInputBinding] = []
    seen: set[tuple[str, str, str]] = set()
    for item in run_inputs:
        if not isinstance(item, RunInputBinding):
            raise UnsupportedRunInput("every run input must be RunInputBinding")
        try:
            _require_text("input_observation_id", item.input_observation_id)
            _require_text("input_role", item.input_role)
            _require_text("derivation_node_id", item.derivation_node_id)
        except ValueError as exc:
            raise UnsupportedRunInput(str(exc)) from exc
        key = (
            item.input_observation_id,
            item.input_role,
            item.derivation_node_id,
        )
        if key in seen:
            raise UnsupportedRunInput("duplicate run input binding")
        seen.add(key)
        result.append(item)
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.input_observation_id,
                item.input_role,
                item.derivation_node_id,
            ),
        )
    )


def _validate_derivation_parents(
    parents: tuple[DerivationParent, ...],
) -> tuple[DerivationParent, ...]:
    if not isinstance(parents, tuple):
        raise DerivationIntegrityError("parents must be a complete immutable tuple")
    normalized: list[DerivationParent] = []
    seen: set[tuple[str, str]] = set()
    for parent in parents:
        if not isinstance(parent, DerivationParent):
            raise DerivationIntegrityError("every parent must be DerivationParent")
        try:
            _require_text("parent_node_id", parent.parent_node_id)
            _require_text("edge_role", parent.edge_role)
        except ValueError as exc:
            raise DerivationIntegrityError(str(exc)) from exc
        if "\0" in parent.edge_role:
            raise DerivationIntegrityError("edge_role must not contain NUL")
        if not isinstance(parent.required, bool):
            raise DerivationIntegrityError("parent required flag must be bool")
        key = (parent.parent_node_id, parent.edge_role)
        if key in seen:
            raise DerivationIntegrityError("duplicate parent/edge_role")
        seen.add(key)
        normalized.append(parent)
    return tuple(
        sorted(normalized, key=lambda item: (item.edge_role, item.parent_node_id))
    )


def _validate_source_metadata(
    value: Any, *, source_id: str, source_record_key: str
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != _SOURCE_METADATA_KEYS:
        raise InvalidObservationProvenance(
            "source_metadata must contain exactly the frozen V1.1 fields"
        )
    if value["schema_version"] != "1":
        raise InvalidObservationProvenance("source metadata schema_version must be '1'")
    for field in (
        "provider",
        "dataset_name",
        "source_record_key",
        "acquisition_method",
        "availability_rule_id",
        "availability_rule_version",
    ):
        try:
            _require_text(f"source_metadata.{field}", value[field])
        except ValueError as exc:
            raise InvalidObservationProvenance(str(exc)) from exc
    source_parts = source_id.split(":")
    if value["provider"] != source_parts[0] or value["dataset_name"] != source_parts[1]:
        raise InvalidObservationProvenance(
            "source metadata provider and dataset_name must match source_id"
        )
    if value["source_record_key"] != source_record_key:
        raise InvalidObservationProvenance(
            "source metadata source_record_key must match the observation"
        )
    evidence = value["evidence_artifact_ids"]
    if (
        not isinstance(evidence, list)
        or not all(
            isinstance(item, str) and _ARTIFACT_ID_RE.fullmatch(item)
            for item in evidence
        )
        or evidence != sorted(set(evidence))
    ):
        raise InvalidObservationProvenance(
            "evidence_artifact_ids must be a sorted unique artifact-ID array"
        )
    if not isinstance(value["provider_metadata"], dict):
        raise InvalidObservationProvenance("provider_metadata must be a canonical object")
    _reject_operational_source_metadata(value["provider_metadata"])
    if value["future_effective_at"] is not None:
        raise InvalidObservationProvenance(
            "RETRIEVED_AT_FALLBACK requires future_effective_at to be null"
        )
    try:
        metadata_json = canonicalize_json(value).decode("utf-8")
    except CanonicalizationError as exc:
        raise InvalidObservationProvenance(
            "source metadata must use deterministic canonical Ledger JSON"
        ) from exc
    return metadata_json, tuple(evidence)


def _reject_operational_source_metadata(value: Any) -> None:
    prohibited_keys = {
        "api_key",
        "credential",
        "credentials",
        "file_path",
        "filesystem_path",
        "local_path",
        "password",
        "secret",
        "token",
    }
    for key, item in value.items():
        normalized = key.lower().replace("-", "_")
        if normalized in prohibited_keys:
            raise InvalidObservationProvenance(
                "provider_metadata must not contain secrets or mutable local locators"
            )
        if isinstance(item, dict):
            _reject_operational_source_metadata(item)
        elif _contains_local_path(item):
            raise InvalidObservationProvenance(
                "provider_metadata must not contain secrets or mutable local locators"
            )


def _contains_local_path(value: Any) -> bool:
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith(
            ("/", "\\\\", "file:")
        )
    if isinstance(value, list):
        return any(_contains_local_path(item) for item in value)
    if isinstance(value, dict):
        _reject_operational_source_metadata(value)
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
