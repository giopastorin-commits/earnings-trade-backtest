"""Authoritative, immutable pairing of mature expectations and market snapshots.

Cycle scope is the explicit frozen expectation set supplied by the caller.  A
closed cycle does not claim to contain every globally outstanding expectation,
and absence of a CLOSED_VERIFIED cycle is not a data-quality conclusion.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import ValidationContractError
from .outcome_expectations import OutcomeExpectation
from .protocol_v1 import PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID, canonical_json
from .spy_benchmark import (
    BenchmarkSnapshotError,
    verify_spy_benchmark_snapshot,
)
from .stock_observation import (
    StockObservationError,
    expected_ticker_set_sha256,
    load_stock_observation_snapshot,
)


SCHEMA_NAME = "trinity.validation.observation-cycle"
SCHEMA_VERSION = "1"
CYCLE_STATE = "CLOSED_VERIFIED"
MANIFEST_NAME = "observation_cycle_manifest.json"
READY_NAME = "READY"

_MANIFEST_FIELDS = {
    "schema_name", "schema_version", "state", "protocol_id", "protocol_hash",
    "protocol_epoch", "observation_target_session", "expectation_refs",
    "expectation_set_sha256", "expected_tickers", "expected_ticker_set_sha256",
    "stock_snapshot", "spy_snapshot", "identity_core", "observation_cycle_id",
    "observation_cycle_sha256",
}
_READY_FIELDS = {
    "schema_name", "schema_version", "state", "observation_cycle_id",
    "observation_cycle_sha256", "manifest_sha256",
}


class ObservationCycleError(ValidationContractError):
    """An observation cycle cannot prove its authoritative frozen pairing."""


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_value(value: object) -> str:
    return _sha_bytes(canonical_json(value))


def _require_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ObservationCycleError(f"{field} must be a lowercase SHA-256")
    return value


def _validated_expectations(
    expectations: Iterable[OutcomeExpectation | Mapping[str, Any]],
) -> tuple[OutcomeExpectation, ...]:
    if isinstance(expectations, (str, bytes, Mapping)):
        raise ObservationCycleError("cycle expectations must be an iterable")
    try:
        supplied = tuple(expectations)
    except TypeError as exc:
        raise ObservationCycleError("cycle expectations are invalid") from exc
    if not supplied:
        raise ObservationCycleError("cycle expectation set must not be empty")
    validated: list[OutcomeExpectation] = []
    for item in supplied:
        try:
            payload = item.to_dict() if isinstance(item, OutcomeExpectation) else dict(item)
            expectation = OutcomeExpectation.from_dict(payload)
        except (TypeError, ValueError, ValidationContractError) as exc:
            raise ObservationCycleError("cycle contains a malformed expectation") from exc
        if (
            expectation.protocol_id != PROTOCOL_ID
            or expectation.protocol_hash != PROTOCOL_HASH
            or expectation.protocol_epoch != PROTOCOL_EPOCH
        ):
            raise ObservationCycleError("cycle contains mixed protocol identity")
        validated.append(expectation)
    keys = [item.key for item in validated]
    if len(keys) != len(set(keys)):
        raise ObservationCycleError("cycle contains duplicate expectation keys")
    return tuple(sorted(validated, key=lambda item: item.key))


def expectation_references(
    expectations: Iterable[OutcomeExpectation | Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return canonical, content-bound references for an explicit expectation set."""

    validated = _validated_expectations(expectations)
    return tuple({
        "expectation_key": item.key,
        "expectation_payload_sha256": _sha_value(item.to_dict()),
        "run_id": item.run_id,
        "ticker": item.ticker,
        "cohort": item.cohort,
        "horizon_sessions": item.horizon_sessions,
        "maturity_session": item.maturity_session,
    } for item in validated)


def expectation_set_sha256(refs: Iterable[Mapping[str, Any]]) -> str:
    detached = [dict(item) for item in refs]
    if not detached or any(not isinstance(item, dict) for item in detached):
        raise ObservationCycleError("expectation references are invalid")
    keys = [item.get("expectation_key") for item in detached]
    if any(not isinstance(key, str) or not key for key in keys) \
            or keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ObservationCycleError("expectation references are not unique and ordered")
    return _sha_value(detached)


def observation_cycle_identity(identity_core: Mapping[str, Any]) -> str:
    return _sha_value(dict(identity_core))


def observation_cycle_envelope_sha256(manifest: Mapping[str, Any]) -> str:
    detached = dict(manifest)
    detached.pop("observation_cycle_sha256", None)
    return _sha_value(detached)


def _manifest_file_sha(root: Path, name: str) -> str:
    try:
        return _sha_bytes((root / name).read_bytes())
    except OSError as exc:
        raise ObservationCycleError(f"missing source snapshot manifest: {name}") from exc


def _source_provenance(
    *, stock_root: Path, spy_root: Path, expected_tickers: tuple[str, ...],
    target: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        stock = load_stock_observation_snapshot(
            stock_root, expected_tickers=expected_tickers,
            expected_observation_target_session=target,
        )
    except StockObservationError as exc:
        raise ObservationCycleError(f"stock observation verification failed: {exc}") from exc
    try:
        spy = verify_spy_benchmark_snapshot(
            spy_root, expected_target_session=target,
        )
    except BenchmarkSnapshotError as exc:
        raise ObservationCycleError(f"SPY observation verification failed: {exc}") from exc
    if stock.observation_target_session != target \
            or str(spy.get("target_market_session") or "") != target:
        raise ObservationCycleError("stock, SPY, and cycle target sessions differ")
    ticker_sha = expected_ticker_set_sha256(expected_tickers)
    if stock.expected_tickers != expected_tickers \
            or stock.expected_ticker_set_sha256 != ticker_sha:
        raise ObservationCycleError("stock observation ticker-set lineage mismatch")
    stock_provenance = {
        "snapshot_id": stock.source_snapshot_id,
        "snapshot_sha256": stock.source_snapshot_sha256,
        "manifest_sha256": _manifest_file_sha(stock_root, "snapshot_manifest.json"),
        "observation_target_session": stock.observation_target_session,
        "acquisition_started_at_utc": stock.acquisition_started_at_utc,
        "acquisition_completed_at_utc": stock.acquisition_completed_at_utc,
        "expected_tickers": list(stock.expected_tickers),
        "expected_ticker_set_sha256": stock.expected_ticker_set_sha256,
        "provider": stock.provider,
        "interval": stock.interval,
        "adjustment": stock.adjustment,
        "calendar": stock.calendar,
        "calendar_version": stock.calendar_version,
    }
    spy_provenance = {
        "snapshot_id": str(spy.get("benchmark_snapshot_id") or ""),
        "snapshot_sha256": str(spy.get("benchmark_snapshot_sha256") or ""),
        "manifest_sha256": _manifest_file_sha(spy_root, "benchmark_manifest.json"),
        "observation_target_session": str(spy.get("target_market_session") or ""),
        "acquisition_started_at_utc": str(spy.get("acquisition_started_at_utc") or ""),
        "acquisition_completed_at_utc": str(spy.get("acquisition_completed_at_utc") or ""),
        "symbol": str(spy.get("symbol") or ""),
        "provider": str(spy.get("provider") or ""),
        "interval": str(spy.get("interval") or ""),
        "adjustment": str(spy.get("adjustment") or ""),
        "return_kind": str(spy.get("return_kind") or ""),
        "universe_role": str(spy.get("universe_role") or ""),
        "calendar": str(spy.get("session_calendar") or ""),
        "calendar_version": str(spy.get("session_calendar_version") or ""),
    }
    for field in ("snapshot_sha256", "manifest_sha256"):
        _require_sha(stock_provenance[field], f"stock {field}")
        _require_sha(spy_provenance[field], f"SPY {field}")
    return stock_provenance, spy_provenance


def _cycle_components(
    expectations: Iterable[OutcomeExpectation | Mapping[str, Any]],
    observation_target_session: str, stock_root: str | Path, spy_root: str | Path,
) -> dict[str, Any]:
    validated = _validated_expectations(expectations)
    try:
        target_date = date.fromisoformat(observation_target_session)
    except (TypeError, ValueError) as exc:
        raise ObservationCycleError("invalid observation target session") from exc
    for expectation in validated:
        try:
            maturity = date.fromisoformat(expectation.maturity_session)
        except ValueError as exc:
            raise ObservationCycleError("expectation maturity session is invalid") from exc
        if maturity > target_date:
            raise ObservationCycleError(
                f"expectation is not mature at observation target: {expectation.key}"
            )
    refs = expectation_references(validated)
    refs_sha = expectation_set_sha256(refs)
    tickers = tuple(sorted({item.ticker for item in validated}))
    ticker_sha = expected_ticker_set_sha256(tickers)
    stock, spy = _source_provenance(
        stock_root=Path(stock_root), spy_root=Path(spy_root),
        expected_tickers=tickers, target=observation_target_session,
    )
    identity_core = {
        "protocol_hash": PROTOCOL_HASH,
        "observation_target_session": observation_target_session,
        "expectation_set_sha256": refs_sha,
        "expected_ticker_set_sha256": ticker_sha,
        "stock_snapshot_id": stock["snapshot_id"],
        "stock_snapshot_sha256": stock["snapshot_sha256"],
        "spy_snapshot_id": spy["snapshot_id"],
        "spy_snapshot_sha256": spy["snapshot_sha256"],
    }
    return {
        "observation_target_session": observation_target_session,
        "expectation_refs": [dict(item) for item in refs],
        "expectation_set_sha256": refs_sha,
        "expected_tickers": list(tickers),
        "expected_ticker_set_sha256": ticker_sha,
        "stock_snapshot": stock,
        "spy_snapshot": spy,
        "identity_core": identity_core,
        "observation_cycle_id": observation_cycle_identity(identity_core),
    }


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationCycleError(f"invalid or missing {label}") from exc
    if not isinstance(value, dict):
        raise ObservationCycleError(f"{label} must be an object")
    return value, raw


def _write_new(path: Path, raw: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(raw)
    except OSError as exc:
        raise ObservationCycleError(f"cannot create immutable cycle file: {path.name}") from exc


def create_observation_cycle(
    *, expectations: Iterable[OutcomeExpectation | Mapping[str, Any]],
    observation_target_session: str, stock_root: str | Path, spy_root: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    """Create a CLOSED_VERIFIED artifact for one explicit expectation set."""

    root = Path(output_root)
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise ObservationCycleError("observation cycle output root must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    try:
        frozen_expectations = tuple(expectations)
    except TypeError as exc:
        raise ObservationCycleError("cycle expectations are invalid") from exc
    components = _cycle_components(
        frozen_expectations, observation_target_session, stock_root, spy_root,
    )
    manifest: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "state": CYCLE_STATE,
        "protocol_id": PROTOCOL_ID,
        "protocol_hash": PROTOCOL_HASH,
        "protocol_epoch": PROTOCOL_EPOCH,
        **components,
    }
    manifest["observation_cycle_sha256"] = observation_cycle_envelope_sha256(manifest)
    manifest_raw = canonical_json(manifest)
    _write_new(root / MANIFEST_NAME, manifest_raw)
    ready = {
        "schema_name": f"{SCHEMA_NAME}-ready",
        "schema_version": SCHEMA_VERSION,
        "state": CYCLE_STATE,
        "observation_cycle_id": manifest["observation_cycle_id"],
        "observation_cycle_sha256": manifest["observation_cycle_sha256"],
        "manifest_sha256": _sha_bytes(manifest_raw),
    }
    _write_new(root / READY_NAME, canonical_json(ready))
    return verify_observation_cycle(
        root, expectations=frozen_expectations, stock_root=stock_root, spy_root=spy_root,
    )


def verify_observation_cycle(
    cycle_root: str | Path, *,
    expectations: Iterable[OutcomeExpectation | Mapping[str, Any]],
    stock_root: str | Path, spy_root: str | Path,
) -> dict[str, Any]:
    """Rebuild all authoritative lineage and verify an immutable cycle artifact."""

    root = Path(cycle_root)
    try:
        children = tuple(root.iterdir())
    except OSError as exc:
        raise ObservationCycleError("observation cycle root is missing") from exc
    if {item.name for item in children} != {MANIFEST_NAME, READY_NAME} \
            or any(not item.is_file() for item in children):
        raise ObservationCycleError("observation cycle file closure mismatch")
    manifest, manifest_raw = _load_json(root / MANIFEST_NAME, "cycle manifest")
    ready, _ready_raw = _load_json(root / READY_NAME, "cycle READY marker")
    if set(manifest) != _MANIFEST_FIELDS or (
        manifest.get("schema_name"), manifest.get("schema_version"), manifest.get("state")
    ) != (SCHEMA_NAME, SCHEMA_VERSION, CYCLE_STATE):
        raise ObservationCycleError("unsupported or malformed cycle manifest")
    if set(ready) != _READY_FIELDS or (
        ready.get("schema_name"), ready.get("schema_version"), ready.get("state")
    ) != (f"{SCHEMA_NAME}-ready", SCHEMA_VERSION, CYCLE_STATE):
        raise ObservationCycleError("unsupported or malformed cycle READY marker")
    if (
        ready.get("manifest_sha256") != _sha_bytes(manifest_raw)
        or ready.get("observation_cycle_id") != manifest.get("observation_cycle_id")
        or ready.get("observation_cycle_sha256") != manifest.get("observation_cycle_sha256")
    ):
        raise ObservationCycleError("cycle READY marker does not bind the manifest")
    if (
        manifest.get("protocol_id"), manifest.get("protocol_hash"),
        manifest.get("protocol_epoch"),
    ) != (PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH):
        raise ObservationCycleError("cycle protocol identity mismatch")
    expected_sha = observation_cycle_envelope_sha256(manifest)
    if manifest.get("observation_cycle_sha256") != expected_sha:
        raise ObservationCycleError("cycle envelope SHA-256 mismatch")
    target = manifest.get("observation_target_session")
    if not isinstance(target, str):
        raise ObservationCycleError("cycle observation target is invalid")
    authoritative = _cycle_components(expectations, target, stock_root, spy_root)
    for field, value in authoritative.items():
        if manifest.get(field) != value:
            raise ObservationCycleError(f"cycle authoritative lineage mismatch: {field}")
    if observation_cycle_identity(authoritative["identity_core"]) != manifest.get(
        "observation_cycle_id"
    ):
        raise ObservationCycleError("cycle identity mismatch")
    return manifest
