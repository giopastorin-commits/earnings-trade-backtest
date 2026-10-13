"""Fail-closed verification of a separate historical Shadow attestation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence


SCHEMA_NAME = "trinity.shadow-historical-reconciliation"
SCHEMA_VERSION = "1"
METHOD_VERSION = "SHADOW_MANIFEST_PATH_NAME_RECONCILIATION_V1"
HISTORICAL_RUN_ID = "SHADOW_USA_2026-10-09_38230389276_A1"
HISTORICAL_ANALYTICAL_COMMIT = "c0558e7c95a90d37baa9cf3fbcc09376ad59e5dd"
HISTORICAL_WORKFLOW_COMMIT = "b550a921d4f7bb02fe3cd196422e95f3d583bac6"
HISTORICAL_GITHUB_ARTIFACT_ID = 11742264978
HISTORICAL_OMISSION_PATHS = frozenset({
    "fresh_sources/trinity/MRNA/sec/manifest.json",
    "fresh_sources/trinity/SWKS/sec/manifest.json",
})
CONTROL_PATHS = frozenset({"checksums.sha256", "r2_archive_result.json"})
UNKNOWN_R2_FIELDS = (
    "checksum_sha256", "version_id", "object_lock_mode",
    "object_lock_retain_until", "object_lock_legal_hold",
)


@dataclass(frozen=True)
class HistoricalTrustRecord:
    """Immutable production trust anchor for one authorized historical run."""

    source_run_id: str
    attestation_id: str
    attestation_sha256: str
    original_manifest_sha256: str
    original_ledger_sha256: str
    snapshot_id: str
    snapshot_sha256: str
    analytical_commit: str
    workflow_commit: str


_HISTORICAL_TRUST_REGISTRY: Mapping[str, HistoricalTrustRecord] = MappingProxyType({
    HISTORICAL_RUN_ID: HistoricalTrustRecord(
        source_run_id=HISTORICAL_RUN_ID,
        attestation_id="sha256:321d1907882de2667e3e74ea7bf82140484556619dfae10dc7d103f28c111cb8",
        attestation_sha256="d58658403e12f4c6d1df8e8a7615a982f0938e8ac6a95aa335f33e2ef8a1f5b1",
        original_manifest_sha256="a322cfd900658dee5fc787a79e877262f4aac93fb9cae80f5357502ace464878",
        original_ledger_sha256="c18969c615df6ee465530df6014813b7f55187d7ffa32c3db5fd528d2ef53ae8",
        snapshot_id="TWELVEDATA_2026-10-09_RUN_38230389276-A1",
        snapshot_sha256="7b9ed6ced475810365f7d7e791f528916333261c278e9cddfd27b08c210b36a7",
        analytical_commit=HISTORICAL_ANALYTICAL_COMMIT,
        workflow_commit=HISTORICAL_WORKFLOW_COMMIT,
    ),
})


class HistoricalReconciliationError(RuntimeError):
    """The historical omission was not proven exactly."""


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")


def attestation_id(statement: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(statement)).hexdigest()


def build_attestation(statement: Mapping[str, Any]) -> dict[str, Any]:
    """Build the same envelope for the same statement, without a wall clock."""
    value = json.loads(canonical_json_bytes(statement))
    return {
        "attestation_id": attestation_id(value),
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "statement": value,
    }


def attestation_bytes(statement: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        build_attestation(statement), ensure_ascii=False, allow_nan=False,
        sort_keys=True, indent=2,
    ) + "\n").encode("utf-8")


def verify_historical_reconciliation(
    source_root: str | Path,
    original_manifest: Mapping[str, Any],
    attestation_raw: bytes,
    *,
    expected_attestation_sha256: str,
) -> dict[str, Any]:
    """Verify an exact reconciliation overlay without changing the source run."""
    root = Path(source_root).resolve()
    trusted = _trusted_record(root.name)
    if not _is_sha256(expected_attestation_sha256):
        raise HistoricalReconciliationError("invalid expected attestation SHA-256")
    if expected_attestation_sha256 != trusted.attestation_sha256:
        raise HistoricalReconciliationError("CLI attestation SHA-256 is not authorized")
    actual_attestation_sha = hashlib.sha256(attestation_raw).hexdigest()
    if actual_attestation_sha != trusted.attestation_sha256:
        raise HistoricalReconciliationError("attestation SHA-256 is not authorized")
    value = _strict_json(attestation_raw)
    if not isinstance(value, dict) or set(value) != {
        "attestation_id", "schema_name", "schema_version", "statement",
    }:
        raise HistoricalReconciliationError("invalid attestation envelope")
    if value["schema_name"] != SCHEMA_NAME or value["schema_version"] != SCHEMA_VERSION:
        raise HistoricalReconciliationError("unsupported attestation schema")
    statement = value.get("statement")
    if not isinstance(statement, dict) or value["attestation_id"] != attestation_id(statement):
        raise HistoricalReconciliationError("attestation identity mismatch")
    if attestation_raw != attestation_bytes(statement):
        raise HistoricalReconciliationError("attestation is not canonical JSON")

    manifest_raw = _required_file_bytes(root / "manifest.json", "original manifest")
    if hashlib.sha256(manifest_raw).hexdigest() != trusted.original_manifest_sha256:
        raise HistoricalReconciliationError("original manifest SHA-256 is not authorized")
    if _strict_json(manifest_raw) != original_manifest:
        raise HistoricalReconciliationError("provided manifest differs from the authorized file")
    _verify_statement_identity(statement, original_manifest, trusted)
    manifest_entries = _verified_manifest_entries(root, original_manifest)

    original = _required_mapping(statement, "original_shadow")
    if set(original) != {"manifest", "ledger"}:
        raise HistoricalReconciliationError("invalid original Shadow evidence")
    if any(_required_mapping(original, name).get("status") != "VERIFIED"
           for name in ("manifest", "ledger")):
        raise HistoricalReconciliationError("original Shadow evidence is not verified")
    _verify_bound_file(root, "manifest.json", original.get("manifest"))
    _verify_bound_file(root, "ledger/trinity.sqlite3", original.get("ledger"))
    if _file_identity(original.get("manifest"))["sha256"] != trusted.original_manifest_sha256:
        raise HistoricalReconciliationError("attested manifest SHA-256 is not authorized")
    if _file_identity(original.get("ledger"))["sha256"] != trusted.original_ledger_sha256:
        raise HistoricalReconciliationError("attested Ledger SHA-256 is not authorized")
    ledger_entry = manifest_entries.get("ledger/trinity.sqlite3")
    if ledger_entry != _file_identity(original.get("ledger")):
        raise HistoricalReconciliationError("attested Ledger differs from original inventory")

    controls = _exact_file_records(statement.get("control_files"), CONTROL_PATHS, "control")
    omissions = _exact_file_records(
        statement.get("omissions"), HISTORICAL_OMISSION_PATHS, "omission",
    )
    for path, record in {**controls, **omissions}.items():
        if path in manifest_entries:
            raise HistoricalReconciliationError(f"attested extra is already inventoried: {path}")
        _verify_bound_file(root, path, record)

    _verify_remote_evidence(statement, omissions, original)
    expected_paths = set(manifest_entries) | {"manifest.json"} | set(controls) | set(omissions)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*") if path.is_file()
    }
    missing = sorted(expected_paths - actual_paths)
    extras = sorted(actual_paths - expected_paths)
    if missing:
        raise HistoricalReconciliationError("reconciled source files are missing: " + ", ".join(missing))
    if extras:
        raise HistoricalReconciliationError("unexpected source files: " + ", ".join(extras))
    return {
        "attestation_id": value["attestation_id"],
        "attestation_sha256": actual_attestation_sha,
        "omission_paths": sorted(omissions),
        "verified_file_count": len(actual_paths),
        "mode": "VALIDATION",
        "official_benchmark_observation": False,
        "trusted_snapshot_id": trusted.snapshot_id,
        "trusted_snapshot_sha256": trusted.snapshot_sha256,
        "trusted_analytical_commit": trusted.analytical_commit,
        "trusted_workflow_commit": trusted.workflow_commit,
    }


def _verify_statement_identity(
    statement: Mapping[str, Any], original_manifest: Mapping[str, Any],
    trusted: HistoricalTrustRecord,
) -> None:
    required = {
        "method_version", "source_run_id", "mode", "official_benchmark_observation",
        "analytical_commit", "workflow_commit", "defect", "original_shadow",
        "control_files", "omissions", "github_artifact", "unknown_metadata",
    }
    if set(statement) != required:
        raise HistoricalReconciliationError("invalid attestation statement fields")
    if (
        statement.get("method_version") != METHOD_VERSION
        or statement.get("source_run_id") != HISTORICAL_RUN_ID
        or original_manifest.get("run_id") != HISTORICAL_RUN_ID
        or statement.get("analytical_commit") != trusted.analytical_commit
        or statement.get("workflow_commit") != trusted.workflow_commit
    ):
        raise HistoricalReconciliationError("historical run identity mismatch")
    if statement.get("mode") != "VALIDATION" or statement.get("official_benchmark_observation") is not False:
        raise HistoricalReconciliationError("historical reconciliation is VALIDATION-only")
    defect = _required_mapping(statement, "defect")
    if defect != {
        "component": "trinity/shadow/manifest.py:eligible_files",
        "historical_predicate": "path.name not in {'manifest.json', 'checksums.sha256'}",
        "impact": "all nested files with either basename were omitted from the Shadow inventory",
        "classification": "GENERATOR_PATH_NAME_FILTER_DEFECT",
    }:
        raise HistoricalReconciliationError("historical defect statement mismatch")


def _verified_manifest_entries(
    root: Path, manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise HistoricalReconciliationError("original manifest files are invalid")
    entries: dict[str, dict[str, Any]] = {}
    for item in files:
        identity = _file_identity(item)
        path = str(item.get("path")) if isinstance(item, Mapping) else ""
        _safe_relative(path)
        if path in entries:
            raise HistoricalReconciliationError(f"duplicate original manifest path: {path}")
        entries[path] = identity
        _verify_bound_file(root, path, item)
    if manifest.get("file_count") != len(entries):
        raise HistoricalReconciliationError("original manifest file count mismatch")
    if manifest.get("total_bytes") != sum(item["bytes"] for item in entries.values()):
        raise HistoricalReconciliationError("original manifest total bytes mismatch")
    return entries


def _exact_file_records(
    value: Any, expected_paths: frozenset[str], label: str,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        raise HistoricalReconciliationError(f"{label} records are invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "bytes", "sha256", "status", "r2"}:
            if label == "control" and isinstance(item, Mapping) and set(item) == {
                "path", "bytes", "sha256", "status",
            }:
                pass
            else:
                raise HistoricalReconciliationError(f"invalid {label} record")
        path = str(item.get("path") or "")
        _safe_relative(path)
        if path in result:
            raise HistoricalReconciliationError(f"duplicate attested path: {path}")
        if item.get("status") != "VERIFIED":
            raise HistoricalReconciliationError(f"{label} is not verified: {path}")
        _file_identity(item)
        result[path] = item
    if set(result) != set(expected_paths):
        raise HistoricalReconciliationError(f"unexpected {label} path set")
    return result


def _verify_remote_evidence(
    statement: Mapping[str, Any], omissions: Mapping[str, Mapping[str, Any]],
    original: Mapping[str, Any],
) -> None:
    github = _required_mapping(statement, "github_artifact")
    if github != {
        "status": "VERIFIED",
        "artifact_id": HISTORICAL_GITHUB_ARTIFACT_ID,
        "name": "trinity-weekly-shadow-38230389276-attempt-1",
        "workflow_run_id": 38230389276,
        "head_sha": HISTORICAL_WORKFLOW_COMMIT,
        "created_at": "2026-10-12T11:15:10Z",
        "size_in_bytes": 7327288,
        "digest": "sha256:834bf4de257eb1064bd3c335307a2c113760efa248fe271cbe300f426a69101f",
    }:
        raise HistoricalReconciliationError("GitHub artifact evidence mismatch")
    unknown = statement.get("unknown_metadata")
    expected_unknown = [f"r2.{name}" for name in UNKNOWN_R2_FIELDS]
    if unknown != expected_unknown:
        raise HistoricalReconciliationError("UNKNOWN metadata declaration mismatch")
    prefix = f"runs/shadow/run_id={HISTORICAL_RUN_ID}/"
    remote_records = [
        (item.get("r2"), prefix + "manifests/" + path)
        for path, item in omissions.items()
    ]
    remote_records.extend([
        (_required_mapping(original, "ledger").get("r2"), prefix + "ledger/ledger/trinity.sqlite3"),
        (_required_mapping(original, "manifest").get("r2"), prefix + "manifests/manifest.json"),
    ])
    for remote, expected_key in remote_records:
        if not isinstance(remote, Mapping) or remote.get("status") != "VERIFIED":
            raise HistoricalReconciliationError("R2 evidence is not verified")
        if set(remote) != {
            "status", "key", "content_length", "metadata_sha256", "last_modified",
            *UNKNOWN_R2_FIELDS,
        }:
            raise HistoricalReconciliationError("invalid R2 evidence fields")
        if any(remote.get(name) != "UNKNOWN" for name in UNKNOWN_R2_FIELDS):
            raise HistoricalReconciliationError("unverified R2 immutability claim")
        if remote.get("key") != expected_key:
            raise HistoricalReconciliationError("R2 evidence key mismatch")


def _verify_bound_file(root: Path, relative: str, record: Any) -> None:
    identity = _file_identity(record)
    _safe_relative(relative)
    if not isinstance(record, Mapping) or record.get("path") != relative:
        raise HistoricalReconciliationError(f"attested path mismatch: {relative}")
    path = root.joinpath(*PurePosixPath(relative).parts).resolve()
    if root != path and root not in path.parents:
        raise HistoricalReconciliationError(f"attested path escapes source root: {relative}")
    if not path.is_file():
        raise HistoricalReconciliationError(f"attested file is missing: {relative}")
    raw = path.read_bytes()
    if len(raw) != identity["bytes"]:
        raise HistoricalReconciliationError(f"attested size mismatch: {relative}")
    if hashlib.sha256(raw).hexdigest() != identity["sha256"]:
        raise HistoricalReconciliationError(f"attested SHA-256 mismatch: {relative}")
    remote = record.get("r2")
    if remote is not None and (
        remote.get("content_length") != identity["bytes"]
        or remote.get("metadata_sha256") != identity["sha256"]
    ):
        raise HistoricalReconciliationError(f"R2 evidence differs from file: {relative}")


def _file_identity(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise HistoricalReconciliationError("invalid file identity")
    size = record.get("bytes")
    digest = record.get("sha256")
    if not isinstance(size, int) or size < 0 or not _is_sha256(digest):
        raise HistoricalReconciliationError("invalid file size or SHA-256")
    return {"bytes": size, "sha256": digest}


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise HistoricalReconciliationError(f"missing attestation mapping: {key}")
    return result


def _safe_relative(value: str) -> None:
    pure = PurePosixPath(value)
    if (
        not value or "\\" in value or pure.is_absolute() or ".." in pure.parts
        or value != pure.as_posix() or pure.as_posix() in {".", ""}
    ):
        raise HistoricalReconciliationError(f"unsafe attested path: {value}")


def _strict_json(raw: bytes) -> Any:
    def pairs(values: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise HistoricalReconciliationError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalReconciliationError("attestation is not valid JSON") from exc


def _trusted_record(run_id: str) -> HistoricalTrustRecord:
    record = _HISTORICAL_TRUST_REGISTRY.get(run_id)
    if record is None or record.source_run_id != run_id:
        raise HistoricalReconciliationError("historical run is not in the production trust registry")
    return record


def historical_trust_record(run_id: str) -> HistoricalTrustRecord:
    """Return the immutable trust anchor or fail closed for every other run."""
    return _trusted_record(run_id)


def _required_file_bytes(path: Path, label: str) -> bytes:
    if not path.is_file():
        raise HistoricalReconciliationError(f"{label} is missing")
    return path.read_bytes()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
