"""Immutable weekly-run units for official validation analytics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping

from .contracts import CollectedRun, OFFICIAL_ELIGIBLE, SMOKE_ONLY, SelectionObservation
from .protocol_v1 import PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID
from .storage import ValidationStore


RUN_OBSERVATION = "RUN_OBSERVATION"
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.-]*\Z")


class RunObservationError(ValueError):
    """A collected run cannot satisfy the immutable run-unit contract."""


def _sha(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 \
            or any(char not in "0123456789abcdef" for char in value):
        raise RunObservationError(f"{field} must be a lowercase SHA-256")


def _git_oid(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value) not in {40, 64} \
            or any(char not in "0123456789abcdef" for char in value):
        raise RunObservationError(f"{field} must be a full lowercase Git object ID")


def _aware_utc(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RunObservationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RunObservationError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _membership(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise RunObservationError(f"{field} must be a ticker sequence")
    tickers = tuple(value)
    if any(not isinstance(ticker, str) or not _TICKER.fullmatch(ticker) for ticker in tickers):
        raise RunObservationError(f"{field} contains a non-canonical ticker")
    if tickers != tuple(sorted(tickers)) or len(tickers) != len(set(tickers)):
        raise RunObservationError(f"{field} must be ordered and unique")
    return tickers


def _cost(value: object) -> str:
    if not isinstance(value, str):
        raise RunObservationError("total_api_cost_usd must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise RunObservationError("total_api_cost_usd must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise RunObservationError("total_api_cost_usd must be finite and non-negative")
    return value


def run_observation_identity(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise RunObservationError("run_id is required")
    return f"{PROTOCOL_HASH}:{run_id}"


@dataclass(frozen=True)
class RunObservation:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    run_id: str
    eligibility: str
    decision_target_session: str
    run_timestamp_utc: str
    analytical_commit_sha: str
    decision_snapshot_id: str
    decision_snapshot_sha256: str
    source_run_manifest_sha256: str
    universe_version: str
    universe_sha256: str
    scanner_tickers: tuple[str, ...]
    luna_tickers: tuple[str, ...]
    sol_tickers: tuple[str, ...]
    scanner_count: int
    luna_count: int
    sol_count: int
    total_api_cost_usd: str

    def __post_init__(self) -> None:
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise RunObservationError("run observation protocol identity mismatch")
        run_observation_identity(self.run_id)
        if self.eligibility not in {SMOKE_ONLY, OFFICIAL_ELIGIBLE}:
            raise RunObservationError("run observation eligibility is invalid")
        try:
            date.fromisoformat(self.decision_target_session)
        except (TypeError, ValueError) as exc:
            raise RunObservationError("decision_target_session must be an ISO date") from exc
        _aware_utc(self.run_timestamp_utc, "run_timestamp_utc")
        _git_oid(self.analytical_commit_sha, "analytical_commit_sha")
        if not isinstance(self.decision_snapshot_id, str) or not self.decision_snapshot_id:
            raise RunObservationError("decision_snapshot_id is required")
        _sha(self.decision_snapshot_sha256, "decision_snapshot_sha256")
        _sha(self.source_run_manifest_sha256, "source_run_manifest_sha256")
        if not isinstance(self.universe_version, str) or not self.universe_version:
            raise RunObservationError("universe_version is required")
        _sha(self.universe_sha256, "universe_sha256")
        scanner = _membership(self.scanner_tickers, "scanner_tickers")
        luna = _membership(self.luna_tickers, "luna_tickers")
        sol = _membership(self.sol_tickers, "sol_tickers")
        if not set(sol).issubset(luna) or not set(luna).issubset(scanner):
            raise RunObservationError("membership must satisfy SOL subset LUNA subset SCANNER")
        if (self.scanner_count, self.luna_count, self.sol_count) != (
            len(scanner), len(luna), len(sol),
        ):
            raise RunObservationError("run observation membership count mismatch")
        _cost(self.total_api_cost_usd)

    @property
    def key(self) -> str:
        return run_observation_identity(self.run_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunObservation":
        if not isinstance(value, Mapping):
            raise RunObservationError("run observation payload must be an object")
        try:
            payload = dict(value)
            for field in ("scanner_tickers", "luna_tickers", "sol_tickers"):
                if isinstance(payload.get(field), list):
                    payload[field] = tuple(payload[field])
            return cls(**payload)
        except TypeError as exc:
            raise RunObservationError("run observation payload shape is invalid") from exc


def _validate_selection_lineage(selection: SelectionObservation, collected: CollectedRun) -> None:
    expected = (
        collected.run_id, collected.eligibility, collected.decision_target_session,
        collected.run_timestamp_utc, collected.analytical_commit_sha, collected.snapshot_id,
        collected.snapshot_sha256, collected.universe_version, collected.universe_sha256,
    )
    actual = (
        selection.run_id, selection.eligibility, selection.intended_session,
        selection.run_timestamp_utc, selection.analytical_commit_sha, selection.snapshot_id,
        selection.snapshot_sha256, selection.universe_version, selection.universe_sha256,
    )
    if actual != expected:
        raise RunObservationError("selection and run-level provenance mismatch")
    if not selection.scanner_selected or selection.scanner_state != "READY_TECHNICALLY":
        raise RunObservationError("selection is not a frozen Scanner member")
    if selection.sol_status == "PROMOTED" and not (
        selection.sol_evaluated and selection.sol_decision == "INVESTIGATE"
        and selection.luna_evaluated and selection.luna_status == "COMPLETED"
        and selection.luna_decision == "ESCALATE"
    ):
        raise RunObservationError("invalid Sol membership lineage")


def build_run_observation(collected: CollectedRun) -> RunObservation:
    """Build one immutable statistical unit, including an empty Scanner run."""

    if not isinstance(collected, CollectedRun):
        raise RunObservationError("collected run is required")
    tickers = [selection.ticker for selection in collected.selections]
    if len(tickers) != len(set(tickers)):
        raise RunObservationError("collected run contains duplicate selection tickers")
    for selection in collected.selections:
        _validate_selection_lineage(selection, collected)
    scanner = tuple(sorted(tickers))
    luna = tuple(sorted(
        selection.ticker for selection in collected.selections
        if selection.luna_evaluated and selection.luna_status == "COMPLETED"
        and selection.luna_decision == "ESCALATE"
    ))
    sol = tuple(sorted(
        selection.ticker for selection in collected.selections
        if selection.sol_evaluated and selection.sol_status == "PROMOTED"
        and selection.sol_decision == "INVESTIGATE"
    ))
    return RunObservation(
        protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH, protocol_epoch=PROTOCOL_EPOCH,
        run_id=collected.run_id, eligibility=collected.eligibility,
        decision_target_session=collected.decision_target_session,
        run_timestamp_utc=collected.run_timestamp_utc,
        analytical_commit_sha=collected.analytical_commit_sha,
        decision_snapshot_id=collected.snapshot_id,
        decision_snapshot_sha256=collected.snapshot_sha256,
        source_run_manifest_sha256=collected.source_manifest_sha256,
        universe_version=collected.universe_version, universe_sha256=collected.universe_sha256,
        scanner_tickers=scanner, luna_tickers=luna, sol_tickers=sol,
        scanner_count=len(scanner), luna_count=len(luna), sol_count=len(sol),
        total_api_cost_usd=_cost(collected.total_api_cost_usd),
    )


def append_run_observation(store: ValidationStore, collected: CollectedRun) -> bool:
    observation = build_run_observation(collected)
    return store.append(RUN_OBSERVATION, observation.key, observation.to_dict())


def load_run_observations(store: ValidationStore) -> tuple[RunObservation, ...]:
    observations: list[RunObservation] = []
    for key, payload in store.record_items(RUN_OBSERVATION):
        observation = RunObservation.from_dict(payload)
        if key != observation.key:
            raise RunObservationError("RUN_OBSERVATION record key mismatch")
        observations.append(observation)
    return tuple(sorted(observations, key=lambda item: (item.decision_target_session, item.key)))
