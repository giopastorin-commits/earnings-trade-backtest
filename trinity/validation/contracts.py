"""Immutable records emitted by the offline validation layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .protocol_v1 import HORIZONS, PROTOCOL_EPOCH, PROTOCOL_ID


class ValidationContractError(ValueError):
    """An input cannot satisfy the frozen validation contract."""


def _sha(value: str | None, field: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValidationContractError(f"{field} must be a lowercase SHA-256")


def _git_oid(value: str, field: str) -> None:
    if len(value) not in {40, 64} or any(c not in "0123456789abcdef" for c in value):
        raise ValidationContractError(f"{field} must be a full lowercase Git object ID")


@dataclass(frozen=True)
class SelectionObservation:
    run_id: str
    intended_session: str
    run_timestamp_utc: str
    ticker: str
    universe_version: str
    universe_sha256: str
    scanner_selected: bool
    scanner_state: str
    scanner_reason: str
    luna_evaluated: bool
    luna_status: str
    luna_decision: str
    luna_model: str | None
    luna_prompt_version: str | None
    luna_input_sha256: str | None
    luna_output_sha256: str | None
    luna_cost_usd: str
    sol_evaluated: bool
    sol_status: str
    sol_decision: str
    sol_model: str | None
    sol_prompt_version: str | None
    sol_input_sha256: str | None
    sol_output_sha256: str | None
    sol_cost_usd: str
    analytical_commit_sha: str
    snapshot_id: str
    snapshot_sha256: str
    data_quality_flags: tuple[str, ...] = ()
    protocol_version: str = PROTOCOL_ID
    protocol_epoch: int = PROTOCOL_EPOCH

    def __post_init__(self) -> None:
        if not self.run_id or not self.ticker or self.scanner_state != "READY_TECHNICALLY":
            raise ValidationContractError("selection identity or Scanner membership is invalid")
        if self.luna_decision not in {"ESCALATE", "WATCH", "DROP", "NOT_EVALUATED", "EVALUATED_FAILED"}:
            raise ValidationContractError("invalid Luna decision")
        if self.sol_status not in {"PROMOTED", "REJECTED", "NOT_EVALUATED", "EVALUATED_FAILED"}:
            raise ValidationContractError("invalid Sol status")
        if not self.sol_evaluated and self.sol_status != "NOT_EVALUATED":
            raise ValidationContractError("non-evaluated Sol ticker cannot be rejected")
        _sha(self.universe_sha256, "universe_sha256")
        _git_oid(self.analytical_commit_sha, "analytical_commit_sha")
        _sha(self.snapshot_sha256, "snapshot_sha256")
        _sha(self.luna_input_sha256, "luna_input_sha256", optional=True)
        _sha(self.luna_output_sha256, "luna_output_sha256", optional=True)
        _sha(self.sol_input_sha256, "sol_input_sha256", optional=True)
        _sha(self.sol_output_sha256, "sol_output_sha256", optional=True)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SetupObservation:
    run_id: str
    ticker: str
    setup_id: str | None
    setup_sha256: str
    setup_type: str
    setup_timestamp: str
    entry_condition: str | None
    entry_level: float | None
    entry_lower: float | None
    entry_upper: float | None
    stop: float | None
    tp1: float | None
    tp2: float | None
    original_rr1: float | None
    original_rr2: float | None
    expiry: str | None
    protocol_version: str = PROTOCOL_ID
    protocol_epoch: int = PROTOCOL_EPOCH

    def __post_init__(self) -> None:
        _sha(self.setup_sha256, "setup_sha256")
        if self.setup_type != "NO_SETUP" and any(value is None for value in (
            self.entry_level, self.stop, self.tp1, self.tp2, self.original_rr1, self.original_rr2
        )):
            raise ValidationContractError("operational Setup is incomplete")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionObservation:
    run_id: str
    ticker: str
    setup_id: str
    setup_sha256: str
    freshness_timestamp: str
    current_price: float | None
    price_session: str
    price_provider: str
    execution_status: str
    current_rr1: float | None
    current_rr2: float | None
    reason_codes: tuple[str, ...]
    policy_version: str
    protocol_version: str = PROTOCOL_ID
    protocol_epoch: int = PROTOCOL_EPOCH

    def __post_init__(self) -> None:
        _sha(self.setup_sha256, "setup_sha256")
        if self.execution_status not in {"VALID", "WAIT", "EXTENDED", "INVALIDATED", "RR_TOO_LOW"}:
            raise ValidationContractError("invalid Entry Freshness status")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OutcomeObservation:
    run_id: str
    ticker: str
    cohort: str
    horizon_sessions: int
    first_tradable_session: str | None
    first_tradable_price: float | None
    future_session: str | None
    stock_return_pct: float | None
    benchmark_return_pct: float | None
    active_return_pct: float | None
    maturity_status: str
    source_snapshot_id: str
    source_snapshot_sha256: str
    benchmark_snapshot_id: str | None = None
    benchmark_snapshot_sha256: str | None = None
    quality_flags: tuple[str, ...] = ()
    protocol_version: str = PROTOCOL_ID
    protocol_epoch: int = PROTOCOL_EPOCH

    def __post_init__(self) -> None:
        if self.horizon_sessions not in HORIZONS:
            raise ValidationContractError("outcome horizon is outside protocol")
        if self.cohort not in {"SCANNER", "LUNA", "SOL"}:
            raise ValidationContractError("invalid cohort")
        if self.maturity_status not in {"PENDING", "COMPLETE", "DATA_QUALITY_FAILURE"}:
            raise ValidationContractError("invalid maturity status")
        _sha(self.source_snapshot_sha256, "source_snapshot_sha256")
        _sha(self.benchmark_snapshot_sha256, "benchmark_snapshot_sha256", optional=True)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CollectedRun:
    run_id: str
    source_root: str
    source_manifest_sha256: str
    selections: tuple[SelectionObservation, ...]
    setups: tuple[SetupObservation, ...]
    executions: tuple[ExecutionObservation, ...]
    total_api_cost_usd: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_root": self.source_root,
            "source_manifest_sha256": self.source_manifest_sha256,
            "total_api_cost_usd": self.total_api_cost_usd,
            "selections": [item.to_dict() for item in self.selections],
            "setups": [item.to_dict() for item in self.setups],
            "executions": [item.to_dict() for item in self.executions],
        }
