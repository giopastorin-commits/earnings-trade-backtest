"""Pre-live simple-quant comparator derived only from immutable validation records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import math
import random
import re
from statistics import fmean
from types import MappingProxyType
from typing import Any, Mapping

from .baseline_v1 import BASELINE, BASELINE_HASH, BASELINE_ID, BASELINE_VERSION
from .baskets import (
    AVAILABLE, EMPTY_BENCHMARK, PENDING, UNAVAILABLE_DQF,
    StageBasket, build_official_weekly_baskets,
)
from .contracts import OFFICIAL_ELIGIBLE, ValidationContractError
from .outcome_expectations import OutcomeExpectation, outcome_identity
from .outcome_finalization import COMPLETE, DATA_QUALITY_FAILURE, load_outcome_finals
from .protocol_v1 import PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID
from .run_observation import RunObservation, load_run_observations
from .slot_assignment import load_current_expectations
from .storage import ValidationStore


NO_OFFICIAL_WEEKS = "NO_OFFICIAL_WEEKS"
INSUFFICIENT_USABLE_WEEKS = "INSUFFICIENT_USABLE_WEEKS"
FORMAL_INFERENCE_AVAILABLE = "FORMAL_INFERENCE_AVAILABLE"

COMPARATOR_USABLE = "COMPARATOR_USABLE"
COMPARATOR_PENDING = "COMPARATOR_PENDING"
COMPARATOR_DQF_UNAVAILABLE = "COMPARATOR_DQF_UNAVAILABLE"

_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.-]*\Z")
_SETUP_FIELDS = frozenset({
    "run_id", "ticker", "setup_id", "setup_sha256", "setup_type",
    "setup_timestamp", "entry_condition", "entry_level", "entry_lower",
    "entry_upper", "stop", "tp1", "tp2", "original_rr1", "original_rr2",
    "expiry", "protocol_version", "protocol_epoch",
})
_COST_BPS = tuple(int(item) for item in BASELINE["cost_sensitivity_bps_per_side"])
_INFERENCE = BASELINE["formal_inference"]
_MINIMUM_WEEKS = int(_INFERENCE["minimum_usable_weeks"])
_REPLICATES = int(_INFERENCE["replicates"])
_BLOCK_LENGTH = int(_INFERENCE["block_length_weeks"])
_CONFIDENCE = float(_INFERENCE["confidence_interval"])
_SEED_LABEL = str(_INFERENCE["seed_label"])
if (_COST_BPS, _MINIMUM_WEEKS, _REPLICATES, _BLOCK_LENGTH, _CONFIDENCE) != (
    (0, 10, 25), 52, 10000, 4, 0.95,
):
    raise RuntimeError("simple quant baseline inference or cost policy drift")


class SimpleQuantBaselineError(ValidationContractError):
    """Immutable records cannot satisfy the frozen simple-quant baseline."""


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimpleQuantBaselineError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise SimpleQuantBaselineError(f"{field} must be finite")
    return result


def _sha(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 \
            or any(char not in "0123456789abcdef" for char in value):
        raise SimpleQuantBaselineError(f"{field} must be a lowercase SHA-256")


@dataclass(frozen=True)
class SetupRankingEvidence:
    ticker: str
    setup_sha256: str
    original_rr1: float
    original_rr2: float

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not _TICKER.fullmatch(self.ticker):
            raise SimpleQuantBaselineError("ranking evidence ticker is not canonical")
        _sha(self.setup_sha256, "setup_sha256")
        _finite(self.original_rr1, "original_rr1")
        _finite(self.original_rr2, "original_rr2")


@dataclass(frozen=True)
class QuantBaselineWeek:
    baseline_id: str
    baseline_version: int
    baseline_hash: str
    upstream_protocol_id: str
    upstream_protocol_hash: str
    run_id: str
    decision_target_session: str
    selection_count: int
    members: tuple[str, ...]
    ranking_evidence: tuple[SetupRankingEvidence, ...]
    status: str
    complete_count: int
    pending_count: int
    dqf_count: int
    gross_active_return_pct: float | None
    cost_adjusted_active_return_pct: Mapping[int, float | None]

    def __post_init__(self) -> None:
        if (self.baseline_id, self.baseline_version, self.baseline_hash) != (
            BASELINE_ID, BASELINE_VERSION, BASELINE_HASH,
        ) or (self.upstream_protocol_id, self.upstream_protocol_hash) != (
            PROTOCOL_ID, PROTOCOL_HASH,
        ):
            raise SimpleQuantBaselineError("baseline week identity mismatch")
        if not self.run_id:
            raise SimpleQuantBaselineError("baseline week run_id is required")
        try:
            date.fromisoformat(self.decision_target_session)
        except (TypeError, ValueError) as exc:
            raise SimpleQuantBaselineError("baseline week target session is invalid") from exc
        members = tuple(self.members)
        evidence = tuple(self.ranking_evidence)
        if type(self.selection_count) is not int or self.selection_count < 0 \
                or len(members) != self.selection_count \
                or len(set(members)) != len(members) \
                or tuple(item.ticker for item in evidence) != members:
            raise SimpleQuantBaselineError("baseline membership or ranking evidence mismatch")
        counts = (self.complete_count, self.pending_count, self.dqf_count)
        if any(type(item) is not int or item < 0 for item in counts) \
                or sum(counts) != self.selection_count:
            raise SimpleQuantBaselineError("baseline member counts are inconsistent")
        costs = dict(self.cost_adjusted_active_return_pct)
        if tuple(sorted(costs)) != _COST_BPS:
            raise SimpleQuantBaselineError("baseline cost scenarios are invalid")
        if self.status == EMPTY_BENCHMARK:
            if self.selection_count or self.gross_active_return_pct != 0.0 \
                    or any(value != 0.0 for value in costs.values()):
                raise SimpleQuantBaselineError("empty baseline invariants are invalid")
        elif self.status == AVAILABLE:
            gross = _finite(self.gross_active_return_pct, "gross_active_return_pct")
            if not self.selection_count or self.complete_count != self.selection_count \
                    or self.pending_count or self.dqf_count:
                raise SimpleQuantBaselineError("available baseline invariants are invalid")
            for bps, value in costs.items():
                if not math.isclose(
                    _finite(value, "cost-adjusted return"), gross - 2.0 * bps / 100.0,
                    rel_tol=1e-12, abs_tol=1e-12,
                ):
                    raise SimpleQuantBaselineError("baseline cost arithmetic mismatch")
        elif self.status == PENDING:
            if not self.pending_count or self.dqf_count \
                    or self.gross_active_return_pct is not None \
                    or any(value is not None for value in costs.values()):
                raise SimpleQuantBaselineError("pending baseline invariants are invalid")
        elif self.status == UNAVAILABLE_DQF:
            if not self.dqf_count or self.gross_active_return_pct is not None \
                    or any(value is not None for value in costs.values()):
                raise SimpleQuantBaselineError("DQF baseline invariants are invalid")
        else:
            raise SimpleQuantBaselineError("baseline status is invalid")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "ranking_evidence", evidence)
        object.__setattr__(self, "cost_adjusted_active_return_pct", MappingProxyType(costs))


@dataclass(frozen=True)
class BaselineCostSensitivity:
    bps_per_side: int
    n_usable: int
    mean_delta_ar20_pct: float | None

    def __post_init__(self) -> None:
        if self.bps_per_side not in _COST_BPS or type(self.n_usable) is not int \
                or self.n_usable < 0:
            raise SimpleQuantBaselineError("cost sensitivity identity is invalid")
        if (self.mean_delta_ar20_pct is None) != (self.n_usable == 0):
            raise SimpleQuantBaselineError("cost sensitivity mean and count differ")
        if self.mean_delta_ar20_pct is not None:
            _finite(self.mean_delta_ar20_pct, "cost sensitivity mean")


@dataclass(frozen=True)
class SimpleQuantComparator:
    baseline_id: str
    baseline_version: int
    baseline_hash: str
    upstream_protocol_id: str
    upstream_protocol_hash: str
    metric: str
    metric_class: str
    status: str
    n_total_official_weeks: int
    n_usable: int
    n_pending: int
    n_dqf: int
    dqf_rate: float | None
    pending_rate: float | None
    mean_delta_ar20_pct: float | None
    confidence_interval_95: tuple[float, float] | None
    cost_sensitivity: tuple[BaselineCostSensitivity, ...]

    def __post_init__(self) -> None:
        if (self.baseline_id, self.baseline_version, self.baseline_hash) != (
            BASELINE_ID, BASELINE_VERSION, BASELINE_HASH,
        ) or (self.upstream_protocol_id, self.upstream_protocol_hash) != (
            PROTOCOL_ID, PROTOCOL_HASH,
        ) or self.metric != "SOL_MINUS_SIMPLE_QUANT_AR20" \
                or self.metric_class != "AUXILIARY_COMPARATOR":
            raise SimpleQuantBaselineError("comparator identity mismatch")
        counts = (self.n_total_official_weeks, self.n_usable, self.n_pending, self.n_dqf)
        if any(type(item) is not int or item < 0 for item in counts) \
                or self.n_total_official_weeks != sum(counts[1:]):
            raise SimpleQuantBaselineError("comparator denominator invariant is invalid")
        expected_status = NO_OFFICIAL_WEEKS if not self.n_total_official_weeks else (
            FORMAL_INFERENCE_AVAILABLE if self.n_usable >= _MINIMUM_WEEKS
            else INSUFFICIENT_USABLE_WEEKS
        )
        if self.status != expected_status:
            raise SimpleQuantBaselineError("comparator status is inconsistent")
        if not self.n_total_official_weeks:
            if self.dqf_rate is not None or self.pending_rate is not None:
                raise SimpleQuantBaselineError("zero-week comparator rates must be null")
        elif (self.dqf_rate, self.pending_rate) != (
            self.n_dqf / self.n_total_official_weeks,
            self.n_pending / self.n_total_official_weeks,
        ):
            raise SimpleQuantBaselineError("comparator rates differ from fixed denominator")
        if (self.mean_delta_ar20_pct is None) != (self.n_usable == 0):
            raise SimpleQuantBaselineError("comparator mean and usable count differ")
        if self.mean_delta_ar20_pct is not None:
            _finite(self.mean_delta_ar20_pct, "comparator mean")
        if self.status == FORMAL_INFERENCE_AVAILABLE:
            if not isinstance(self.confidence_interval_95, tuple) \
                    or len(self.confidence_interval_95) != 2:
                raise SimpleQuantBaselineError("formal comparator lacks a confidence interval")
            low, high = (
                _finite(item, "confidence interval")
                for item in self.confidence_interval_95
            )
            if low > high:
                raise SimpleQuantBaselineError("comparator confidence interval is reversed")
        elif self.confidence_interval_95 is not None:
            raise SimpleQuantBaselineError("non-formal comparator cannot expose a CI")
        costs = tuple(self.cost_sensitivity)
        if tuple(item.bps_per_side for item in costs) != _COST_BPS \
                or any(item.n_usable != self.n_usable for item in costs):
            raise SimpleQuantBaselineError("comparator cost sensitivity is inconsistent")
        object.__setattr__(self, "cost_sensitivity", costs)
        zero_cost = costs[0].mean_delta_ar20_pct
        if (zero_cost is None) != (self.mean_delta_ar20_pct is None) or (
            zero_cost is not None and not math.isclose(
                zero_cost, self.mean_delta_ar20_pct, rel_tol=0.0, abs_tol=1e-12,
            )
        ):
            raise SimpleQuantBaselineError("zero-cost comparator differs from gross")


def _setup_key(run_id: str, ticker: str) -> str:
    return f"{PROTOCOL_ID}:{PROTOCOL_EPOCH}:{run_id}:{ticker}"


def _load_setups(store: ValidationStore) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for key, value in store.record_items("SETUP"):
        if not isinstance(value, Mapping) or set(value) != _SETUP_FIELDS:
            raise SimpleQuantBaselineError("SETUP payload shape is invalid")
        payload = dict(value)
        run_id, ticker = payload.get("run_id"), payload.get("ticker")
        if not isinstance(run_id, str) or not run_id \
                or not isinstance(ticker, str) or not _TICKER.fullmatch(ticker) \
                or key != _setup_key(run_id, ticker):
            raise SimpleQuantBaselineError("SETUP record key or run/ticker lineage mismatch")
        if (payload.get("protocol_version"), payload.get("protocol_epoch")) != (
            PROTOCOL_ID, PROTOCOL_EPOCH,
        ):
            raise SimpleQuantBaselineError("SETUP protocol identity mismatch")
        _sha(payload.get("setup_sha256"), "setup_sha256")
        setup_type = payload.get("setup_type")
        if not isinstance(setup_type, str) or not setup_type:
            raise SimpleQuantBaselineError("SETUP type is invalid")
        if ticker in result.setdefault(run_id, {}):
            raise SimpleQuantBaselineError("duplicate SETUP ticker")
        if setup_type != "NO_SETUP":
            _finite(payload.get("original_rr1"), "original_rr1")
            _finite(payload.get("original_rr2"), "original_rr2")
        result[run_id][ticker] = payload
    return result


def _ranking(setups: Mapping[str, Mapping[str, Any]]) -> tuple[SetupRankingEvidence, ...]:
    values = tuple(
        SetupRankingEvidence(
            ticker=ticker, setup_sha256=str(payload["setup_sha256"]),
            original_rr1=_finite(payload["original_rr1"], "original_rr1"),
            original_rr2=_finite(payload["original_rr2"], "original_rr2"),
        )
        for ticker, payload in setups.items()
        if payload["setup_type"] != "NO_SETUP"
    )
    return tuple(sorted(values, key=lambda item: (-item.original_rr1, -item.original_rr2, item.ticker)))


def _validate_scanner_expectation(
    run: RunObservation, ticker: str, expectation: OutcomeExpectation,
) -> None:
    if (expectation.run_id, expectation.eligibility, expectation.ticker,
        expectation.cohort, expectation.horizon_sessions) != (
        run.run_id, run.eligibility, ticker, "SCANNER", 20,
    ) or (
        expectation.decision_timestamp_utc,
        expectation.decision_target_session,
        expectation.analytical_commit_sha,
        expectation.decision_snapshot_id,
        expectation.decision_snapshot_sha256,
        expectation.source_run_manifest_sha256,
        expectation.universe_version,
        expectation.universe_sha256,
    ) != (
        run.run_timestamp_utc,
        run.decision_target_session,
        run.analytical_commit_sha,
        run.decision_snapshot_id,
        run.decision_snapshot_sha256,
        run.source_run_manifest_sha256,
        run.universe_version,
        run.universe_sha256,
    ):
        raise SimpleQuantBaselineError("Scanner D20 expectation lineage mismatch")
    if expectation.selection_data_quality_flags:
        raise SimpleQuantBaselineError("official baseline expectation has data quality flags")


def build_quant_baseline_weeks(store: ValidationStore) -> tuple[QuantBaselineWeek, ...]:
    """Derive capacity-matched baseline sleeves without using Sol identities."""

    runs = tuple(
        item for item in load_run_observations(store)
        if item.eligibility == OFFICIAL_ELIGIBLE
    )
    weeks: set[tuple[int, int]] = set()
    for run in runs:
        iso = date.fromisoformat(run.decision_target_session).isocalendar()
        identity = (iso.year, iso.week)
        if identity in weeks:
            raise SimpleQuantBaselineError("multiple official runs share an ISO week")
        weeks.add(identity)
    setups = _load_setups(store)
    expectations = load_current_expectations(store)
    expectation_by_key = {item.key: item for item in expectations}
    finals = {item.expectation_key: item for item in load_outcome_finals(store, expectations)}
    result: list[QuantBaselineWeek] = []
    for run in runs:
        run_setups = setups.get(run.run_id, {})
        if set(run_setups) != set(run.scanner_tickers) or len(run_setups) != run.scanner_count:
            raise SimpleQuantBaselineError("official run requires exactly one SETUP per Scanner ticker")
        ranked = _ranking(run_setups)
        if run.sol_count > len(ranked):
            raise SimpleQuantBaselineError("Sol count exceeds operational Setup count")
        selected = ranked[:run.sol_count]
        completed: list[float] = []
        pending = dqf = 0
        for evidence in selected:
            key = outcome_identity(run.run_id, evidence.ticker, "SCANNER", 20)
            expectation = expectation_by_key.get(key)
            if expectation is None:
                raise SimpleQuantBaselineError("selected ticker lacks Scanner D20 expectation")
            _validate_scanner_expectation(run, evidence.ticker, expectation)
            final = finals.get(key)
            if final is None:
                pending += 1
            elif final.status == COMPLETE:
                completed.append(_finite(final.active_return_pct, "active_return_pct"))
            elif final.status == DATA_QUALITY_FAILURE:
                dqf += 1
            else:
                raise SimpleQuantBaselineError("Scanner D20 final has invalid status")
        if not selected:
            status, gross = EMPTY_BENCHMARK, 0.0
        elif dqf:
            status, gross = UNAVAILABLE_DQF, None
        elif pending:
            status, gross = PENDING, None
        else:
            status, gross = AVAILABLE, fmean(completed)
        costs = {
            bps: (
                0.0 if status == EMPTY_BENCHMARK
                else gross - 2.0 * bps / 100.0 if status == AVAILABLE
                else None
            )
            for bps in _COST_BPS
        }
        result.append(QuantBaselineWeek(
            baseline_id=BASELINE_ID, baseline_version=BASELINE_VERSION,
            baseline_hash=BASELINE_HASH, upstream_protocol_id=PROTOCOL_ID,
            upstream_protocol_hash=PROTOCOL_HASH, run_id=run.run_id,
            decision_target_session=run.decision_target_session,
            selection_count=run.sol_count,
            members=tuple(item.ticker for item in selected), ranking_evidence=selected,
            status=status, complete_count=len(completed), pending_count=pending,
            dqf_count=dqf, gross_active_return_pct=gross,
            cost_adjusted_active_return_pct=costs,
        ))
    return tuple(sorted(result, key=lambda item: (item.decision_target_session, item.run_id)))


def baseline_bootstrap_seed() -> int:
    return int(hashlib.sha256(f"{BASELINE_HASH}:{_SEED_LABEL}".encode()).hexdigest()[:16], 16)


def _bootstrap_ci(values: tuple[float, ...]) -> tuple[float, float]:
    if len(values) < _MINIMUM_WEEKS:
        raise SimpleQuantBaselineError("formal comparator requires 52 usable weeks")
    rng, samples, size = random.Random(baseline_bootstrap_seed()), [], len(values)
    for _ in range(_REPLICATES):
        draw: list[float] = []
        while len(draw) < size:
            start = rng.randrange(size)
            draw.extend(values[(start + offset) % size] for offset in range(_BLOCK_LENGTH))
        samples.append(fmean(draw[:size]))
    samples.sort()
    alpha = (1.0 - _CONFIDENCE) / 2.0
    return (
        samples[int(alpha * (_REPLICATES - 1))],
        samples[int((1.0 - alpha) * (_REPLICATES - 1))],
    )


def _state(sol: StageBasket, baseline: QuantBaselineWeek) -> str:
    if UNAVAILABLE_DQF in {sol.status, baseline.status}:
        return COMPARATOR_DQF_UNAVAILABLE
    if PENDING in {sol.status, baseline.status}:
        return COMPARATOR_PENDING
    if sol.status in {AVAILABLE, EMPTY_BENCHMARK} \
            and baseline.status in {AVAILABLE, EMPTY_BENCHMARK}:
        return COMPARATOR_USABLE
    raise SimpleQuantBaselineError("comparator contains an unknown sleeve state")


def _return(value: float | None, field: str) -> float:
    if value is None:
        raise SimpleQuantBaselineError(f"usable comparator lacks {field}")
    return _finite(value, field)


def build_simple_quant_comparator(store: ValidationStore) -> SimpleQuantComparator:
    weekly = build_official_weekly_baskets(store)
    baseline = build_quant_baseline_weeks(store)
    if tuple((item.run_id, item.decision_target_session) for item in weekly) != tuple(
        (item.run_id, item.decision_target_session) for item in baseline
    ):
        raise SimpleQuantBaselineError("baseline and official weekly units differ")
    deltas: list[float] = []
    pending = dqf = 0
    cost_deltas = {bps: [] for bps in _COST_BPS}
    for official, quant in zip(weekly, baseline, strict=True):
        sol = official.basket(20, "SOL")
        state = _state(sol, quant)
        if state == COMPARATOR_DQF_UNAVAILABLE:
            dqf += 1
        elif state == COMPARATOR_PENDING:
            pending += 1
        else:
            deltas.append(
                _return(sol.gross_active_return_pct, "Sol return")
                - _return(quant.gross_active_return_pct, "baseline return")
            )
            for bps in _COST_BPS:
                cost_deltas[bps].append(
                    _return(sol.cost_adjusted_active_return_pct[bps], "Sol cost return")
                    - _return(quant.cost_adjusted_active_return_pct[bps], "baseline cost return")
                )
    total, usable = len(weekly), len(deltas)
    status = NO_OFFICIAL_WEEKS if not total else (
        FORMAL_INFERENCE_AVAILABLE if usable >= _MINIMUM_WEEKS
        else INSUFFICIENT_USABLE_WEEKS
    )
    return SimpleQuantComparator(
        baseline_id=BASELINE_ID, baseline_version=BASELINE_VERSION,
        baseline_hash=BASELINE_HASH, upstream_protocol_id=PROTOCOL_ID,
        upstream_protocol_hash=PROTOCOL_HASH, metric="SOL_MINUS_SIMPLE_QUANT_AR20",
        metric_class="AUXILIARY_COMPARATOR", status=status,
        n_total_official_weeks=total, n_usable=usable, n_pending=pending, n_dqf=dqf,
        dqf_rate=None if not total else dqf / total,
        pending_rate=None if not total else pending / total,
        mean_delta_ar20_pct=None if not deltas else fmean(deltas),
        confidence_interval_95=_bootstrap_ci(tuple(deltas)) if usable >= _MINIMUM_WEEKS else None,
        cost_sensitivity=tuple(
            BaselineCostSensitivity(
                bps_per_side=bps, n_usable=usable,
                mean_delta_ar20_pct=None if not cost_deltas[bps] else fmean(cost_deltas[bps]),
            )
            for bps in _COST_BPS
        ),
    )
