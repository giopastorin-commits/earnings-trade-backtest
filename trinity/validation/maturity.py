"""Append-only, offline maturation of frozen prospective outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trinity.twelvedata_prices import (
    ADJUSTMENT, INTERVAL, PRICE_PROVIDER, PriceSnapshotError, verify_snapshot,
)

from .contracts import (
    CollectedRun, OFFICIAL_ELIGIBLE, SMOKE_ONLY, ValidationContractError,
)
from .market_calendar import (
    CALENDAR_NAME, CALENDAR_VERSION, derive_outcome_sessions, parse_utc, session_times,
)
from .outcomes import calculate_selection_outcome
from .protocol_v1 import HORIZONS, PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID
from .spy_benchmark import (
    BenchmarkSnapshotError, load_spy_benchmark_snapshot, verify_spy_benchmark_snapshot,
)
from .stock_observation import StockObservationError, load_stock_observation_snapshot
from .storage import ValidationStore


SELECTION_POLICY_VERSION = "FIRST_IMMUTABLE_WEEKLY_OBSERVATION_AFTER_MATURITY_V1"
OUTCOME_EXPECTATION = "OUTCOME_EXPECTATION"
OUTCOME_FINAL = "OUTCOME_FINAL"


class MaturityError(ValidationContractError):
    pass


def _sha(value: str | None, field: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise MaturityError(f"{field} must be a lowercase SHA-256")


def outcome_identity(run_id: str, ticker: str, cohort: str, horizon_sessions: int) -> str:
    return f"{PROTOCOL_HASH}:{run_id}:{ticker}:{cohort}:{horizon_sessions}"


@dataclass(frozen=True)
class OutcomeExpectation:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    run_id: str
    eligibility: str
    ticker: str
    cohort: str
    horizon_sessions: int
    decision_timestamp_utc: str
    decision_target_session: str
    expected_first_tradable_session: str
    maturity_session: str
    analytical_commit_sha: str
    decision_snapshot_id: str
    decision_snapshot_sha256: str
    universe_version: str
    universe_sha256: str
    calendar: str
    calendar_version: str
    expectation_created_at: str
    scanner_selected: bool
    luna_decision: str
    sol_status: str

    def __post_init__(self) -> None:
        if (self.protocol_id, self.protocol_hash, self.protocol_epoch) != (
            PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
        ):
            raise MaturityError("outcome expectation protocol identity mismatch")
        if self.eligibility not in {SMOKE_ONLY, OFFICIAL_ELIGIBLE}:
            raise MaturityError("outcome expectation eligibility is invalid")
        if self.cohort not in {"SCANNER", "LUNA", "SOL"} or self.horizon_sessions not in HORIZONS:
            raise MaturityError("outcome expectation identity is invalid")
        if self.calendar != CALENDAR_NAME or self.calendar_version != CALENDAR_VERSION:
            raise MaturityError("outcome expectation calendar mismatch")
        if not self.scanner_selected or (self.cohort == "LUNA" and self.luna_decision != "ESCALATE") \
                or (self.cohort == "SOL" and self.sol_status != "PROMOTED"):
            raise MaturityError("outcome expectation cohort lineage mismatch")
        if len(self.analytical_commit_sha) not in {40, 64} or any(
            char not in "0123456789abcdef" for char in self.analytical_commit_sha
        ):
            raise MaturityError("analytical_commit_sha must be a full Git object ID")
        _sha(self.decision_snapshot_sha256, "decision_snapshot_sha256")
        _sha(self.universe_sha256, "universe_sha256")
        try:
            date.fromisoformat(self.decision_target_session)
        except ValueError as exc:
            raise MaturityError("invalid decision_target_session") from exc
        parse_utc(self.expectation_created_at, "expectation_created_at")
        first, maturity = derive_outcome_sessions(
            self.decision_timestamp_utc, self.horizon_sessions,
        )
        if (first, maturity) != (
            self.expected_first_tradable_session, self.maturity_session,
        ):
            raise MaturityError("outcome expectation session derivation mismatch")

    @property
    def key(self) -> str:
        return outcome_identity(self.run_id, self.ticker, self.cohort, self.horizon_sessions)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutcomeExpectation":
        return cls(**dict(value))


@dataclass(frozen=True)
class SnapshotCatalogEntry:
    kind: str
    root: Path
    snapshot_id: str
    snapshot_sha256: str
    manifest_sha256: str
    observation_snapshot_target_session: str
    acquisition_completed_at_utc: str
    observation_cycle_id: str
    provider: str
    interval: str
    adjustment: str
    archive_identity: str
    ready: bool

    def __post_init__(self) -> None:
        if self.kind not in {"STOCK", "SPY"} or not self.snapshot_id or not self.archive_identity:
            raise MaturityError("invalid snapshot catalog identity")
        _sha(self.snapshot_sha256, "snapshot_sha256")
        _sha(self.manifest_sha256, "manifest_sha256")
        parse_utc(self.acquisition_completed_at_utc, "acquisition_completed_at_utc")
        try:
            date.fromisoformat(self.observation_snapshot_target_session)
        except ValueError as exc:
            raise MaturityError("invalid observation_snapshot_target_session") from exc
        if self.provider != PRICE_PROVIDER or self.interval != INTERVAL or self.adjustment != ADJUSTMENT:
            raise MaturityError("snapshot catalog provider contract mismatch")
        if self.ready is not True:
            raise MaturityError("snapshot catalog entry is not READY")


@dataclass(frozen=True)
class ObservationCatalog:
    stock: tuple[SnapshotCatalogEntry, ...]
    spy: tuple[SnapshotCatalogEntry, ...]

    def __post_init__(self) -> None:
        entries = self.stock + self.spy
        identities = [(item.kind, item.snapshot_id) for item in entries]
        if len(identities) != len(set(identities)):
            raise MaturityError("duplicate snapshot catalog entry")
        if any(item.kind != "STOCK" for item in self.stock) or any(
            item.kind != "SPY" for item in self.spy
        ):
            raise MaturityError("snapshot catalog kind mismatch")


@dataclass(frozen=True)
class ObservationBundle:
    stock: SnapshotCatalogEntry
    spy: SnapshotCatalogEntry

    def __post_init__(self) -> None:
        if self.stock.observation_snapshot_target_session != self.spy.observation_snapshot_target_session:
            raise MaturityError("stock and SPY observation target sessions differ")
        if self.stock.observation_cycle_id != self.spy.observation_cycle_id:
            raise MaturityError("stock and SPY snapshots are not from the same observation cycle")

    @property
    def observation_snapshot_target_session(self) -> str:
        return self.stock.observation_snapshot_target_session

    @property
    def acquisition_completed_at(self) -> datetime:
        return max(
            parse_utc(self.stock.acquisition_completed_at_utc, "stock acquisition timestamp"),
            parse_utc(self.spy.acquisition_completed_at_utc, "SPY acquisition timestamp"),
        )


@dataclass(frozen=True)
class Resolution:
    state: str
    bundle: ObservationBundle | None = None
    stock: SnapshotCatalogEntry | None = None
    spy: SnapshotCatalogEntry | None = None
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutcomeFinal:
    protocol_id: str
    protocol_hash: str
    protocol_epoch: int
    expectation_key: str
    run_id: str
    eligibility: str
    ticker: str
    cohort: str
    horizon_sessions: int
    status: str
    decision_timestamp_utc: str
    decision_target_session: str
    decision_snapshot_id: str
    decision_snapshot_sha256: str
    analytical_commit_sha: str
    expected_first_tradable_session: str
    first_tradable_session: str | None
    first_tradable_price: float | None
    maturity_session: str
    future_price: float | None
    stock_return_pct: float | None
    benchmark_return_pct: float | None
    active_return_pct: float | None
    stock_observation_snapshot_id: str | None
    stock_observation_snapshot_sha256: str | None
    stock_manifest_sha256: str | None
    stock_observation_target_session: str | None
    stock_acquisition_completed_at_utc: str | None
    stock_provider: str | None
    stock_adjustment: str | None
    stock_archive_identity: str | None
    spy_snapshot_id: str | None
    spy_snapshot_sha256: str | None
    spy_manifest_sha256: str | None
    spy_observation_target_session: str | None
    spy_acquisition_completed_at_utc: str | None
    spy_provider: str | None
    spy_adjustment: str | None
    spy_archive_identity: str | None
    quality_flags: tuple[str, ...]
    snapshot_selection_policy_version: str
    calendar: str
    calendar_version: str

    def __post_init__(self) -> None:
        if self.status not in {"COMPLETE", "DATA_QUALITY_FAILURE"}:
            raise MaturityError("OUTCOME_FINAL cannot be PENDING")
        if self.eligibility not in {SMOKE_ONLY, OFFICIAL_ELIGIBLE}:
            raise MaturityError("OUTCOME_FINAL eligibility is invalid")
        if self.expectation_key != outcome_identity(
            self.run_id, self.ticker, self.cohort, self.horizon_sessions,
        ):
            raise MaturityError("OUTCOME_FINAL expectation identity mismatch")
        if self.protocol_hash != PROTOCOL_HASH or self.protocol_id != PROTOCOL_ID \
                or self.protocol_epoch != PROTOCOL_EPOCH:
            raise MaturityError("OUTCOME_FINAL protocol identity mismatch")
        if self.status == "COMPLETE" and any(value is None for value in (
            self.first_tradable_price, self.future_price, self.stock_return_pct,
            self.benchmark_return_pct, self.active_return_pct,
            self.stock_observation_snapshot_id, self.spy_snapshot_id,
        )):
            raise MaturityError("COMPLETE OUTCOME_FINAL lacks required values")
        _sha(self.decision_snapshot_sha256, "decision_snapshot_sha256")
        _sha(self.stock_observation_snapshot_sha256, "stock_observation_snapshot_sha256", optional=True)
        _sha(self.stock_manifest_sha256, "stock_manifest_sha256", optional=True)
        _sha(self.spy_snapshot_sha256, "spy_snapshot_sha256", optional=True)
        _sha(self.spy_manifest_sha256, "spy_manifest_sha256", optional=True)
        if self.stock_observation_target_session is not None \
                and self.spy_observation_target_session is not None \
                and self.stock_observation_target_session != self.spy_observation_target_session:
            raise MaturityError("OUTCOME_FINAL observation target sessions differ")

    @property
    def key(self) -> str:
        return self.expectation_key

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MaturitySummary:
    expectations_total: int
    already_final: int
    pending: int
    completed: int
    data_quality_failure: int
    new_records: int


def build_outcome_expectations(collected: CollectedRun) -> tuple[OutcomeExpectation, ...]:
    """Freeze all Scanner memberships and promoted stage memberships at Week 0."""

    result: list[OutcomeExpectation] = []
    for selection in collected.selections:
        cohorts = ["SCANNER"]
        if selection.luna_decision == "ESCALATE":
            cohorts.append("LUNA")
        if selection.sol_status == "PROMOTED":
            cohorts.append("SOL")
        for cohort in cohorts:
            for horizon in HORIZONS:
                first, maturity = derive_outcome_sessions(selection.run_timestamp_utc, horizon)
                result.append(OutcomeExpectation(
                    protocol_id=PROTOCOL_ID, protocol_hash=PROTOCOL_HASH,
                    protocol_epoch=PROTOCOL_EPOCH, run_id=selection.run_id,
                    eligibility=selection.eligibility, ticker=selection.ticker,
                    cohort=cohort, horizon_sessions=horizon,
                    decision_timestamp_utc=selection.run_timestamp_utc,
                    decision_target_session=selection.intended_session,
                    expected_first_tradable_session=first, maturity_session=maturity,
                    analytical_commit_sha=selection.analytical_commit_sha,
                    decision_snapshot_id=selection.snapshot_id,
                    decision_snapshot_sha256=selection.snapshot_sha256,
                    universe_version=selection.universe_version,
                    universe_sha256=selection.universe_sha256,
                    calendar=CALENDAR_NAME, calendar_version=CALENDAR_VERSION,
                    # The immutable source decision timestamp is the deterministic
                    # logical creation time; storage separately records append time.
                    expectation_created_at=selection.run_timestamp_utc,
                    scanner_selected=selection.scanner_selected,
                    luna_decision=selection.luna_decision, sol_status=selection.sol_status,
                ))
    keys = [item.key for item in result]
    if len(keys) != len(set(keys)):
        raise MaturityError("duplicate OUTCOME_EXPECTATION identity")
    return tuple(result)


def append_outcome_expectations(store: ValidationStore, collected: CollectedRun) -> int:
    return sum(
        store.append(OUTCOME_EXPECTATION, item.key, item.to_dict())
        for item in build_outcome_expectations(collected)
    )


def _manifest_sha(root: Path, name: str) -> str:
    try:
        return hashlib.sha256((root / name).read_bytes()).hexdigest()
    except OSError as exc:
        raise MaturityError(f"missing snapshot manifest: {name}") from exc


def _stock_cycle(snapshot_id: str, target: str) -> str:
    prefix = f"TWELVEDATA_{target}_RUN_"
    if not snapshot_id.startswith(prefix) or not snapshot_id.removeprefix(prefix):
        raise MaturityError("stock snapshot ID lacks observation cycle identity")
    return snapshot_id.removeprefix(prefix)


def catalog_stock_snapshot(root: str | Path, *, archive_identity: str) -> SnapshotCatalogEntry:
    base = Path(root).resolve()
    try:
        manifest = verify_snapshot(base, require_ready=True, require_raw=False)
    except PriceSnapshotError as exc:
        raise MaturityError(f"stock snapshot is not catalogable: {exc}") from exc
    target = str(manifest.get("target_market_session") or "")
    snapshot_id = str(manifest.get("snapshot_id") or "")
    return SnapshotCatalogEntry(
        kind="STOCK", root=base, snapshot_id=snapshot_id,
        snapshot_sha256=str(manifest.get("snapshot_sha256") or ""),
        manifest_sha256=_manifest_sha(base, "snapshot_manifest.json"),
        observation_snapshot_target_session=target,
        acquisition_completed_at_utc=str(manifest.get("acquisition_completed_at_utc") or ""),
        observation_cycle_id=_stock_cycle(snapshot_id, target), provider=str(manifest.get("provider") or ""),
        interval=str(manifest.get("interval") or ""), adjustment=str(manifest.get("adjust") or ""),
        archive_identity=archive_identity, ready=True,
    )


def catalog_spy_snapshot(root: str | Path, *, archive_identity: str) -> SnapshotCatalogEntry:
    base = Path(root).resolve()
    try:
        manifest = verify_spy_benchmark_snapshot(base)
    except BenchmarkSnapshotError as exc:
        raise MaturityError(f"SPY snapshot is not catalogable: {exc}") from exc
    return SnapshotCatalogEntry(
        kind="SPY", root=base, snapshot_id=str(manifest["benchmark_snapshot_id"]),
        snapshot_sha256=str(manifest["benchmark_snapshot_sha256"]),
        manifest_sha256=_manifest_sha(base, "benchmark_manifest.json"),
        observation_snapshot_target_session=str(manifest["target_market_session"]),
        acquisition_completed_at_utc=str(manifest["acquisition_completed_at_utc"]),
        observation_cycle_id=str(manifest["source_run_id"]), provider=str(manifest["provider"]),
        interval=str(manifest["interval"]), adjustment=str(manifest["adjustment"]),
        archive_identity=archive_identity, ready=True,
    )


def build_observation_catalog(
    *, stock: Iterable[tuple[str | Path, str]], spy: Iterable[tuple[str | Path, str]],
) -> ObservationCatalog:
    return ObservationCatalog(
        stock=tuple(catalog_stock_snapshot(root, archive_identity=archive) for root, archive in stock),
        spy=tuple(catalog_spy_snapshot(root, archive_identity=archive) for root, archive in spy),
    )


def resolve_first_eligible_observation(
    expectation: OutcomeExpectation, catalog: ObservationCatalog,
) -> Resolution:
    """Resolve the first complete immutable pair after the maturity close."""

    _open, maturity_close = session_times(expectation.maturity_session)
    stock_entries = [item for item in catalog.stock if (
        item.observation_snapshot_target_session >= expectation.maturity_session
        and parse_utc(item.acquisition_completed_at_utc, "stock acquisition timestamp") > maturity_close
    )]
    if not stock_entries:
        return Resolution("NOT_MATURE_YET")
    bundles: list[ObservationBundle] = []
    for stock in stock_entries:
        for spy in catalog.spy:
            if (
                spy.observation_snapshot_target_session == stock.observation_snapshot_target_session
                and spy.observation_cycle_id == stock.observation_cycle_id
                and parse_utc(spy.acquisition_completed_at_utc, "SPY acquisition timestamp") > maturity_close
            ):
                bundles.append(ObservationBundle(stock, spy))
    if not bundles:
        stock_entries.sort(key=lambda item: (
            parse_utc(item.acquisition_completed_at_utc, "stock acquisition timestamp"),
            item.observation_snapshot_target_session, item.snapshot_id,
        ))
        return Resolution(
            "DATA_QUALITY_FAILURE", stock=stock_entries[0],
            quality_flags=("SPY_OBSERVATION_BUNDLE_MISSING",),
        )
    bundles.sort(key=lambda item: (
        item.acquisition_completed_at,
        item.observation_snapshot_target_session,
        item.stock.snapshot_id,
        item.spy.snapshot_id,
    ))
    return Resolution("BUNDLE", bundle=bundles[0])


def _base_final(expectation: OutcomeExpectation) -> dict[str, Any]:
    return {
        "protocol_id": PROTOCOL_ID, "protocol_hash": PROTOCOL_HASH,
        "protocol_epoch": PROTOCOL_EPOCH, "expectation_key": expectation.key,
        "run_id": expectation.run_id, "eligibility": expectation.eligibility,
        "ticker": expectation.ticker, "cohort": expectation.cohort,
        "horizon_sessions": expectation.horizon_sessions,
        "decision_timestamp_utc": expectation.decision_timestamp_utc,
        "decision_target_session": expectation.decision_target_session,
        "decision_snapshot_id": expectation.decision_snapshot_id,
        "decision_snapshot_sha256": expectation.decision_snapshot_sha256,
        "analytical_commit_sha": expectation.analytical_commit_sha,
        "expected_first_tradable_session": expectation.expected_first_tradable_session,
        "maturity_session": expectation.maturity_session,
        "snapshot_selection_policy_version": SELECTION_POLICY_VERSION,
        "calendar": expectation.calendar, "calendar_version": expectation.calendar_version,
    }


def _lineage(entry: SnapshotCatalogEntry | None, prefix: str) -> dict[str, Any]:
    identity_prefix = "stock_observation" if prefix == "stock" else "spy"
    if entry is None:
        return {
            f"{identity_prefix}_snapshot_id": None,
            f"{identity_prefix}_snapshot_sha256": None,
            f"{prefix}_manifest_sha256": None,
            f"{prefix}_observation_target_session": None,
            f"{prefix}_acquisition_completed_at_utc": None,
            f"{prefix}_provider": None, f"{prefix}_adjustment": None,
            f"{prefix}_archive_identity": None,
        }
    return {
        f"{identity_prefix}_snapshot_id": entry.snapshot_id,
        f"{identity_prefix}_snapshot_sha256": entry.snapshot_sha256,
        f"{prefix}_manifest_sha256": entry.manifest_sha256,
        f"{prefix}_observation_target_session": entry.observation_snapshot_target_session,
        f"{prefix}_acquisition_completed_at_utc": entry.acquisition_completed_at_utc,
        f"{prefix}_provider": entry.provider, f"{prefix}_adjustment": entry.adjustment,
        f"{prefix}_archive_identity": entry.archive_identity,
    }


def _dq_final(
    expectation: OutcomeExpectation, flags: Sequence[str],
    stock: SnapshotCatalogEntry | None = None, spy: SnapshotCatalogEntry | None = None,
) -> OutcomeFinal:
    return OutcomeFinal(
        **_base_final(expectation), status="DATA_QUALITY_FAILURE",
        first_tradable_session=None, first_tradable_price=None, future_price=None,
        stock_return_pct=None, benchmark_return_pct=None, active_return_pct=None,
        **_lineage(stock, "stock"), **_lineage(spy, "spy"),
        quality_flags=tuple(flags),
    )


def finalize_expectation(
    expectation: OutcomeExpectation, resolution: Resolution,
) -> OutcomeFinal | None:
    if resolution.state == "NOT_MATURE_YET":
        return None
    if resolution.state == "DATA_QUALITY_FAILURE":
        return _dq_final(
            expectation, resolution.quality_flags, resolution.stock, resolution.spy,
        )
    bundle = resolution.bundle
    if bundle is None:
        raise MaturityError("BUNDLE resolution lacks observation bundle")
    try:
        stock = load_stock_observation_snapshot(
            bundle.stock.root, ticker=expectation.ticker,
            expected_observation_target_session=bundle.observation_snapshot_target_session,
        )
    except StockObservationError as exc:
        flag = "STOCK_TICKER_MISSING_FROM_OBSERVATION_SNAPSHOT" \
            if "ticker missing" in str(exc) else "STOCK_OBSERVATION_DATA_QUALITY_FAILURE"
        return _dq_final(expectation, (flag,), bundle.stock, bundle.spy)
    by_date = {str(row["date"]): row for row in stock.bars}
    if expectation.expected_first_tradable_session not in by_date:
        return _dq_final(expectation, ("FIRST_TRADABLE_SESSION_MISSING",), bundle.stock, bundle.spy)
    if expectation.maturity_session not in by_date:
        return _dq_final(expectation, ("MATURITY_SESSION_MISSING",), bundle.stock, bundle.spy)
    try:
        spy = load_spy_benchmark_snapshot(
            bundle.spy.root,
            expected_observation_target_session=bundle.observation_snapshot_target_session,
        )
    except BenchmarkSnapshotError:
        return _dq_final(expectation, ("SPY_OBSERVATION_DATA_QUALITY_FAILURE",), bundle.stock, bundle.spy)
    spy_dates = {str(row["date"]) for row in spy.bars}
    if expectation.expected_first_tradable_session not in spy_dates:
        return _dq_final(expectation, ("SPY_FIRST_TRADABLE_SESSION_MISSING",), bundle.stock, bundle.spy)
    if expectation.maturity_session not in spy_dates:
        return _dq_final(expectation, ("SPY_MATURITY_SESSION_MISSING",), bundle.stock, bundle.spy)
    outcome = calculate_selection_outcome(
        run_id=expectation.run_id, ticker=expectation.ticker, cohort=expectation.cohort,
        decision_timestamp_utc=expectation.decision_timestamp_utc,
        horizon_sessions=expectation.horizon_sessions, stock_bars=stock.bars,
        benchmark=spy, source_snapshot_id=stock.snapshot_id,
        source_snapshot_sha256=stock.snapshot_sha256, eligibility=expectation.eligibility,
    )
    if outcome.maturity_status != "COMPLETE" \
            or outcome.first_tradable_session != expectation.expected_first_tradable_session \
            or outcome.future_session != expectation.maturity_session:
        return _dq_final(expectation, ("OUTCOME_SESSION_ALIGNMENT_FAILURE",), bundle.stock, bundle.spy)
    future = by_date[expectation.maturity_session]
    return OutcomeFinal(
        **_base_final(expectation), status="COMPLETE",
        first_tradable_session=outcome.first_tradable_session,
        first_tradable_price=outcome.first_tradable_price,
        future_price=float(future["close"]), stock_return_pct=outcome.stock_return_pct,
        benchmark_return_pct=outcome.benchmark_return_pct,
        active_return_pct=outcome.active_return_pct,
        **_lineage(bundle.stock, "stock"), **_lineage(bundle.spy, "spy"),
        quality_flags=(),
    )


def mature_outcomes(store: ValidationStore, catalog: ObservationCatalog) -> MaturitySummary:
    expectations = [
        OutcomeExpectation.from_dict(payload)
        for _key, payload in store.record_items(OUTCOME_EXPECTATION)
    ]
    final_keys = {key for key, _payload in store.record_items(OUTCOME_FINAL)}
    already = pending = completed = failures = created = 0
    for expectation in expectations:
        if expectation.key in final_keys:
            already += 1
            continue
        resolution = resolve_first_eligible_observation(expectation, catalog)
        final = finalize_expectation(expectation, resolution)
        if final is None:
            pending += 1
            continue
        if store.append(OUTCOME_FINAL, final.key, final.to_dict()):
            created += 1
        if final.status == "COMPLETE":
            completed += 1
        else:
            failures += 1
    return MaturitySummary(len(expectations), already, pending, completed, failures, created)


def catalog_from_json(path: str | Path) -> ObservationCatalog:
    """Load explicit local roots; never discover network or R2 state."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaturityError("invalid observation catalog JSON") from exc
    if not isinstance(value, dict) or set(value) != {"stock", "spy"}:
        raise MaturityError("observation catalog must contain only stock and spy")

    def sources(kind: str) -> list[tuple[str, str]]:
        rows = value.get(kind)
        if not isinstance(rows, list):
            raise MaturityError(f"observation catalog {kind} entries are invalid")
        result: list[tuple[str, str]] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"root", "archive_identity"}:
                raise MaturityError(f"observation catalog {kind} entry is invalid")
            result.append((str(row["root"]), str(row["archive_identity"])))
        return result

    return build_observation_catalog(stock=sources("stock"), spy=sources("spy"))
