"""Append-only inventory of known CLOSED_VERIFIED observation cycles.

The registry records verified cycle reality; it does not prove that every
possible earlier cycle was registered and does not select a winning cycle for
an expectation.  Assignment and observation-slot closure remain separate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .contracts import ValidationContractError
from .market_calendar import parse_utc
from .observation_cycle import (
    CYCLE_STATE,
    MANIFEST_NAME,
    ObservationCycleError,
    verify_observation_cycle,
)
from .outcome_expectations import OutcomeExpectation
from .protocol_v1 import PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID
from .stock_observation import expected_ticker_set_sha256
from .storage import ValidationStore


OBSERVATION_CYCLE_REGISTRATION = "OBSERVATION_CYCLE_REGISTRATION"
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.-]*\Z")


class CycleRegistryError(ValidationContractError):
    """A cycle registration violates the append-only registry contract."""


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CycleRegistryError(f"{field} must be a lowercase SHA-256")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CycleRegistryError(f"{field} must be a non-empty string")
    return value


def _portable_identifier(value: object, field: str) -> str:
    identifier = _nonempty(value, field)
    if "/" in identifier or "\\" in identifier or any(
        ord(character) < 32 for character in identifier
    ):
        raise CycleRegistryError(f"{field} must not contain a local path")
    return identifier


def _timestamp_pair(started: object, completed: object, label: str) -> None:
    try:
        start = parse_utc(_nonempty(started, f"{label}_acquisition_started_at_utc"), "start")
        end = parse_utc(
            _nonempty(completed, f"{label}_acquisition_completed_at_utc"), "completed",
        )
    except ValidationContractError as exc:
        raise CycleRegistryError(f"invalid {label} acquisition timestamps") from exc
    if end < start:
        raise CycleRegistryError(f"{label} acquisition timestamps are reversed")


@dataclass(frozen=True)
class ObservationCycleRegistration:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    observation_cycle_id: str
    observation_cycle_sha256: str
    cycle_manifest_sha256: str
    observation_target_session: str
    expectation_set_sha256: str
    expectation_keys: tuple[str, ...]
    expected_ticker_set_sha256: str
    expected_tickers: tuple[str, ...]
    stock_snapshot_id: str
    stock_snapshot_sha256: str
    stock_manifest_sha256: str
    stock_acquisition_started_at_utc: str
    stock_acquisition_completed_at_utc: str
    spy_snapshot_id: str
    spy_snapshot_sha256: str
    spy_manifest_sha256: str
    spy_acquisition_started_at_utc: str
    spy_acquisition_completed_at_utc: str
    cycle_state: str

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_id, str) or not isinstance(self.protocol_hash, str) \
                or type(self.protocol_epoch) is not int:
            raise CycleRegistryError("registration protocol identity types are invalid")
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise CycleRegistryError("registration protocol identity mismatch")
        if self.cycle_state != CYCLE_STATE:
            raise CycleRegistryError("registration cycle is not CLOSED_VERIFIED")
        for field in (
            "observation_cycle_id", "observation_cycle_sha256", "cycle_manifest_sha256",
            "expectation_set_sha256", "expected_ticker_set_sha256",
            "stock_snapshot_sha256", "stock_manifest_sha256",
            "spy_snapshot_sha256", "spy_manifest_sha256",
        ):
            _sha(getattr(self, field), field)
        _portable_identifier(self.stock_snapshot_id, "stock_snapshot_id")
        _portable_identifier(self.spy_snapshot_id, "spy_snapshot_id")
        try:
            date.fromisoformat(self.observation_target_session)
        except (TypeError, ValueError) as exc:
            raise CycleRegistryError("invalid observation_target_session") from exc
        if not isinstance(self.expectation_keys, (list, tuple)) or not self.expectation_keys \
                or any(not isinstance(key, str) or not key for key in self.expectation_keys):
            raise CycleRegistryError("expectation keys must be non-empty strings")
        expectation_keys = tuple(self.expectation_keys)
        if expectation_keys != tuple(sorted(expectation_keys)) \
                or len(expectation_keys) != len(set(expectation_keys)) \
                or any(not key.startswith(f"{PROTOCOL_HASH}:")
                       or "/" in key or "\\" in key for key in expectation_keys):
            raise CycleRegistryError("expectation keys must be canonical, unique, and Protocol V1")
        if not isinstance(self.expected_tickers, (list, tuple)) or not self.expected_tickers \
                or any(not isinstance(ticker, str) or not _TICKER.fullmatch(ticker)
                       for ticker in self.expected_tickers):
            raise CycleRegistryError("expected tickers are invalid")
        expected_tickers = tuple(self.expected_tickers)
        if expected_tickers != tuple(sorted(expected_tickers)) \
                or len(expected_tickers) != len(set(expected_tickers)):
            raise CycleRegistryError("expected tickers must be canonical and unique")
        if expected_ticker_set_sha256(expected_tickers) != self.expected_ticker_set_sha256:
            raise CycleRegistryError("expected ticker-set SHA-256 mismatch")
        _timestamp_pair(
            self.stock_acquisition_started_at_utc,
            self.stock_acquisition_completed_at_utc, "stock",
        )
        _timestamp_pair(
            self.spy_acquisition_started_at_utc,
            self.spy_acquisition_completed_at_utc, "SPY",
        )
        object.__setattr__(self, "expectation_keys", expectation_keys)
        object.__setattr__(self, "expected_tickers", expected_tickers)

    @property
    def key(self) -> str:
        return self.observation_cycle_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ObservationCycleRegistration":
        if not isinstance(value, Mapping):
            raise CycleRegistryError("registration payload must be an object")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise CycleRegistryError("registration payload shape is invalid") from exc


def _registration_from_verified_manifest(
    manifest: Mapping[str, Any], cycle_manifest_sha256: str,
) -> ObservationCycleRegistration:
    try:
        refs = manifest["expectation_refs"]
        stock = manifest["stock_snapshot"]
        spy = manifest["spy_snapshot"]
        if not isinstance(refs, list) or not isinstance(stock, Mapping) \
                or not isinstance(spy, Mapping):
            raise TypeError
        keys = tuple(str(item["expectation_key"]) for item in refs)
        tickers = tuple(str(item) for item in manifest["expected_tickers"])
        return ObservationCycleRegistration(
            protocol_id=manifest["protocol_id"],
            protocol_hash=manifest["protocol_hash"],
            protocol_epoch=manifest["protocol_epoch"],
            observation_cycle_id=manifest["observation_cycle_id"],
            observation_cycle_sha256=manifest["observation_cycle_sha256"],
            cycle_manifest_sha256=cycle_manifest_sha256,
            observation_target_session=manifest["observation_target_session"],
            expectation_set_sha256=manifest["expectation_set_sha256"],
            expectation_keys=keys,
            expected_ticker_set_sha256=manifest["expected_ticker_set_sha256"],
            expected_tickers=tickers,
            stock_snapshot_id=stock["snapshot_id"],
            stock_snapshot_sha256=stock["snapshot_sha256"],
            stock_manifest_sha256=stock["manifest_sha256"],
            stock_acquisition_started_at_utc=stock["acquisition_started_at_utc"],
            stock_acquisition_completed_at_utc=stock["acquisition_completed_at_utc"],
            spy_snapshot_id=spy["snapshot_id"],
            spy_snapshot_sha256=spy["snapshot_sha256"],
            spy_manifest_sha256=spy["manifest_sha256"],
            spy_acquisition_started_at_utc=spy["acquisition_started_at_utc"],
            spy_acquisition_completed_at_utc=spy["acquisition_completed_at_utc"],
            cycle_state=manifest["state"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CycleRegistryError("verified cycle manifest cannot form a registration") from exc


def register_observation_cycle(
    store: ValidationStore, *, cycle_root: str | Path,
    expectations: Iterable[OutcomeExpectation | Mapping[str, Any]],
    stock_root: str | Path, spy_root: str | Path,
) -> bool:
    """Verify an authoritative cycle and append its portable registration."""

    try:
        frozen_expectations = tuple(expectations)
    except TypeError as exc:
        raise CycleRegistryError("cycle expectations are invalid") from exc
    try:
        manifest = verify_observation_cycle(
            cycle_root, expectations=frozen_expectations,
            stock_root=stock_root, spy_root=spy_root,
        )
    except ObservationCycleError as exc:
        raise CycleRegistryError(f"observation cycle verification failed: {exc}") from exc
    manifest_path = Path(cycle_root) / MANIFEST_NAME
    try:
        manifest_raw = manifest_path.read_bytes()
        persisted = json.loads(manifest_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CycleRegistryError("verified cycle manifest is no longer readable") from exc
    if persisted != manifest:
        raise CycleRegistryError("cycle manifest changed after authoritative verification")
    registration = _registration_from_verified_manifest(
        manifest, hashlib.sha256(manifest_raw).hexdigest(),
    )
    return store.append(
        OBSERVATION_CYCLE_REGISTRATION, registration.key, registration.to_dict(),
    )


def load_registered_cycles(store: ValidationStore) -> tuple[ObservationCycleRegistration, ...]:
    result: list[ObservationCycleRegistration] = []
    for record_key, payload in store.record_items(OBSERVATION_CYCLE_REGISTRATION):
        try:
            registration = ObservationCycleRegistration.from_dict(payload)
        except (TypeError, ValueError, ValidationContractError) as exc:
            raise CycleRegistryError("stored cycle registration is invalid") from exc
        if record_key != registration.observation_cycle_id:
            raise CycleRegistryError("registration record key and cycle identity differ")
        result.append(registration)
    identities = [item.observation_cycle_id for item in result]
    if len(identities) != len(set(identities)):
        raise CycleRegistryError("registry contains duplicate cycle identities")
    return tuple(sorted(
        result, key=lambda item: (item.observation_target_session, item.observation_cycle_id),
    ))


def registered_cycles_for_expectation(
    registrations: Iterable[ObservationCycleRegistration], expectation_key: str,
) -> tuple[ObservationCycleRegistration, ...]:
    """Return every matching cycle; this inventory query never selects a winner."""

    _nonempty(expectation_key, "expectation_key")
    verified: list[ObservationCycleRegistration] = []
    for item in registrations:
        if not isinstance(item, ObservationCycleRegistration):
            raise CycleRegistryError("expectation query received an invalid registration")
        registration = ObservationCycleRegistration.from_dict(item.to_dict())
        if expectation_key in registration.expectation_keys:
            verified.append(registration)
    return tuple(sorted(
        verified,
        key=lambda item: (item.observation_target_session, item.observation_cycle_id),
    ))
