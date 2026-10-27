"""P3 orchestration over the frozen TRINITY Live Validation V1 primitives.

This module intentionally owns no strategy, calendar, acquisition, benchmark,
or outcome mathematics.  It sequences the immutable implementation checked out
at TRINITY_ANALYTICAL_REF and exposes dependency-injected entry points so its
production transports can be tested without network access.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
from statistics import fmean
import sys
from typing import Any, Callable, Iterable, Sequence

# The workflow checks out the frozen analytical tree as the working directory
# and this registration repository under .workflow-registration.
sys.path.insert(0, str(Path.cwd()))

from trinity.twelvedata_prices import DAILY_CREDIT_RESERVE, TwelveDataClient
from trinity.validation.baskets import build_official_weekly_baskets
from trinity.validation.collector import collect_run
from trinity.validation.contracts import OFFICIAL_ELIGIBLE
from trinity.validation.cycle_registry import register_observation_cycle
from trinity.validation.observation_cycle import (
    create_observation_cycle,
    expectation_references,
    expectation_set_sha256,
)
from trinity.validation.observation_slots import ObservationSlot, weekly_observation_slot
from trinity.validation.outcome_expectations import (
    append_outcome_expectations,
    outcome_identity,
)
from trinity.validation.outcome_finalization import (
    COMPLETE,
    CycleSourceLocator,
    finalize_outcomes,
    load_outcome_finals,
)
from trinity.validation.protocol_v1 import (
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
    canonical_json,
    protocol_dict,
)
from trinity.validation.run_observation import append_run_observation
from trinity.validation.run_observation import load_run_observations
from trinity.validation.scorecard import build_final_scorecard
from trinity.validation.simple_quant_baseline import build_simple_quant_comparator
from trinity.validation.slot_assignment import (
    ASSIGNED_EXACT_CYCLE,
    close_observation_slot,
    load_current_expectations,
    load_expectation_assignments,
    load_slot_closures,
)
from trinity.validation.spy_benchmark import acquire_spy_benchmark
from trinity.validation.stock_observation import expected_ticker_set_sha256
from trinity.validation.stock_observation_acquisition import acquire_stock_observation
from trinity.validation.storage import ValidationStore


ANALYTICS_SNAPSHOT = "PROSPECTIVE_ANALYTICS_SNAPSHOT"
INGESTION_RECEIPT = "OFFICIAL_INGESTION_RECEIPT"
NO_MATURE_EXPECTATIONS = "NO_MATURE_EXPECTATIONS"
ALREADY_COMPLETED = "ALREADY_COMPLETED"
READY_TO_ACQUIRE = "READY_TO_ACQUIRE"
COMPLETED = "COMPLETED"
NAMESPACE_ROOT = f"prospective-validation/protocol={PROTOCOL_ID}/epoch={PROTOCOL_EPOCH}"
SHADOW_RUN_ROOT = "runs/shadow/"
_OFFICIAL_RUN_ID = re.compile(r"SHADOW_USA_[0-9]{4}-[0-9]{2}-[0-9]{2}_[A-Za-z0-9-]+_A[0-9]+\Z")


class AutomaticOutcomesError(RuntimeError):
    """A fail-closed P3 orchestration error."""


@dataclass(frozen=True)
class SlotPlan:
    status: str
    slot: ObservationSlot
    expectations: tuple[Any, ...]
    expectation_set_sha256: str | None
    expected_tickers: tuple[str, ...]
    expected_ticker_set_sha256: str | None


@dataclass(frozen=True)
class QuotaPlan:
    stock_ticker_count: int
    stock_credits: int
    spy_credits: int
    safety_reserve: int
    required_available_credits: int
    available_credits: int


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AutomaticOutcomesError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ingest_official_run(store: ValidationStore, run_root: str | Path) -> dict[str, Any]:
    """Atomically append one OFFICIAL cohort and all D5/D20/D60 expectations."""

    collected = collect_run(run_root)
    if collected.eligibility != OFFICIAL_ELIGIBLE:
        raise AutomaticOutcomesError(
            "only OFFICIAL_ELIGIBLE runs may enter the prospective store"
        )
    prefix = f"{PROTOCOL_ID}:{PROTOCOL_EPOCH}:{collected.run_id}"
    before = store.connection.total_changes
    store.connection.execute("SAVEPOINT official_ingestion")
    try:
        store.append("PROTOCOL", f"{PROTOCOL_ID}:{PROTOCOL_EPOCH}", {
            **protocol_dict(), "protocol_hash": PROTOCOL_HASH,
        })
        append_run_observation(store, collected)
        for item in collected.selections:
            store.append("SELECTION", f"{prefix}:{item.ticker}", item.to_dict())
        for item in collected.setups:
            store.append("SETUP", f"{prefix}:{item.ticker}", item.to_dict())
        for item in collected.executions:
            store.append(
                "EXECUTION",
                f"{prefix}:{item.ticker}:{item.freshness_timestamp}",
                item.to_dict(),
            )
        appended_expectations = append_outcome_expectations(store, collected)
        receipt = {
            "schema_name": "trinity.official-ingestion-receipt",
            "schema_version": "1",
            "protocol_id": PROTOCOL_ID,
            "protocol_hash": PROTOCOL_HASH,
            "protocol_epoch": PROTOCOL_EPOCH,
            "run_id": collected.run_id,
            "source_manifest_sha256": collected.source_manifest_sha256,
            "analytical_commit_sha": collected.analytical_commit_sha,
            "registration_commit_sha": os.environ.get("GITHUB_SHA"),
            "eligibility": collected.eligibility,
            "expectation_keys": tuple(
                item.key for item in load_current_expectations(store)
                if item.run_id == collected.run_id
            ),
        }
        store.append(INGESTION_RECEIPT, collected.run_id, receipt)
        store.connection.execute("RELEASE SAVEPOINT official_ingestion")
    except Exception:
        store.connection.execute("ROLLBACK TO SAVEPOINT official_ingestion")
        store.connection.execute("RELEASE SAVEPOINT official_ingestion")
        raise
    integrity, foreign_keys = store.integrity()
    return {
        "run_id": collected.run_id,
        "eligibility": collected.eligibility,
        "records_appended": store.connection.total_changes - before,
        "outcome_expectations_appended": appended_expectations,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
    }


def plan_observation_slot(
    store: ValidationStore, *, iso_year: int, iso_week: int, now_utc: datetime,
) -> SlotPlan:
    """Derive exactly ALL_MATURE_UNASSIGNED_V1_EXPECTATIONS without mutation."""

    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise AutomaticOutcomesError("now_utc must be timezone-aware")
    slot = weekly_observation_slot(iso_year, iso_week)
    finality = datetime.fromisoformat(slot.finality_eligible_at_utc.replace("Z", "+00:00"))
    if now_utc.astimezone(timezone.utc) < finality:
        raise AutomaticOutcomesError("observation slot is not final")
    if any(
        item.observation_slot_id == slot.observation_slot_id
        for item in load_slot_closures(store)
    ):
        return SlotPlan(ALREADY_COMPLETED, slot, (), None, (), None)
    expectations = load_current_expectations(store)
    if any(item.eligibility != OFFICIAL_ELIGIBLE for item in expectations):
        raise AutomaticOutcomesError(
            "prospective store contains a non-OFFICIAL expectation"
        )
    assigned = {
        item.expectation_key
        for item in load_expectation_assignments(store, expectations)
    }
    mature = tuple(sorted((
        item for item in expectations
        if item.maturity_session <= slot.slot_target_session and item.key not in assigned
    ), key=lambda item: item.key))
    if not mature:
        return SlotPlan(NO_MATURE_EXPECTATIONS, slot, (), None, (), None)
    refs = expectation_references(mature)
    tickers = tuple(sorted({item.ticker for item in mature}))
    return SlotPlan(
        READY_TO_ACQUIRE,
        slot,
        mature,
        expectation_set_sha256(refs),
        tickers,
        expected_ticker_set_sha256(tickers),
    )


def preflight_combined_quota(provider: Any, ticker_count: int) -> QuotaPlan:
    """Guard the whole exact stock set plus SPY before the first acquisition."""

    if type(ticker_count) is not int or ticker_count <= 0:
        raise AutomaticOutcomesError("ticker_count must be positive")
    try:
        usage = provider.fetch_api_usage()
    except Exception as exc:
        raise AutomaticOutcomesError("provider quota telemetry is unavailable") from exc
    available = getattr(usage, "daily_remaining", None)
    if type(available) is not int or available < 0:
        raise AutomaticOutcomesError("provider quota telemetry is unavailable")
    required = ticker_count + 1 + DAILY_CREDIT_RESERVE
    plan = QuotaPlan(
        ticker_count, ticker_count, 1, DAILY_CREDIT_RESERVE, required, available,
    )
    if available < required:
        raise AutomaticOutcomesError(
            f"insufficient combined stock/SPY quota: required={required} "
            f"available={available}"
        )
    return plan


def execute_observation_slot(
    store: ValidationStore,
    *,
    plan: SlotPlan,
    provider: Any,
    work_root: str | Path,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    stock_acquirer: Callable[..., Any] = acquire_stock_observation,
    spy_acquirer: Callable[..., Any] = acquire_spy_benchmark,
) -> dict[str, Any]:
    """Sequence frozen acquisition, cycle, assignment, and analytics primitives."""

    if plan.status in {NO_MATURE_EXPECTATIONS, ALREADY_COMPLETED}:
        return {"status": plan.status, "slot_target_session": plan.slot.slot_target_session}
    if (
        plan.status != READY_TO_ACQUIRE
        or not plan.expectations
        or plan.expectation_set_sha256 is None
    ):
        raise AutomaticOutcomesError("slot plan is not acquisition-ready")
    quota = preflight_combined_quota(provider, len(plan.expected_tickers))
    root = Path(work_root)
    stock_root, spy_root, cycle_root = root / "stock", root / "spy", root / "cycle"
    target = date.fromisoformat(plan.slot.slot_target_session)
    stock_acquirer(
        target_session=target,
        expected_tickers=plan.expected_tickers,
        expectation_set_sha256=plan.expectation_set_sha256,
        output_root=stock_root,
        client=provider,
        now=now,
    )
    spy_acquirer(
        target_session=target,
        run_id=plan.slot.observation_slot_id,
        output_root=spy_root,
        client=provider,
        now=now,
    )
    manifest = create_observation_cycle(
        expectations=plan.expectations,
        observation_target_session=plan.slot.slot_target_session,
        stock_root=stock_root,
        spy_root=spy_root,
        output_root=cycle_root,
    )
    register_observation_cycle(
        store,
        cycle_root=cycle_root,
        expectations=plan.expectations,
        stock_root=stock_root,
        spy_root=spy_root,
    )
    closure = close_observation_slot(
        store,
        iso_year=plan.slot.iso_year,
        iso_week=plan.slot.iso_week,
        closed_at_utc=_utc_text(now()),
    )
    if closure.closure_state != ASSIGNED_EXACT_CYCLE:
        raise AutomaticOutcomesError(
            f"slot failed to bind exact cycle: {closure.closure_state}"
        )
    summary = finalize_outcomes(store, cycle_sources=(CycleSourceLocator(
        observation_cycle_id=manifest["observation_cycle_id"],
        cycle_root=cycle_root,
        stock_root=stock_root,
        spy_root=spy_root,
    ),))
    analytics = {
        "protocol_id": PROTOCOL_ID,
        "protocol_hash": PROTOCOL_HASH,
        "protocol_epoch": PROTOCOL_EPOCH,
        "observation_slot_id": plan.slot.observation_slot_id,
        "weekly_baskets": [asdict(item) for item in build_official_weekly_baskets(store)],
        "scorecard": asdict(build_final_scorecard(store)),
        "simple_quant": asdict(build_simple_quant_comparator(store)),
    }
    store.append(ANALYTICS_SNAPSHOT, plan.slot.observation_slot_id, analytics)
    return {
        "status": COMPLETED,
        "slot_target_session": plan.slot.slot_target_session,
        "observation_cycle_id": manifest["observation_cycle_id"],
        "closure_state": closure.closure_state,
        "quota": asdict(quota),
        "new_final_records": summary.new_final_records,
        "pending_unassigned": summary.pending_unassigned,
        "pending_source_unavailable": summary.pending_source_unavailable,
    }


def record_object_key(record_kind: str, record_key: str) -> str:
    identity = hashlib.sha256(record_key.encode("utf-8")).hexdigest()
    return f"{NAMESPACE_ROOT}/records/kind={record_kind}/identity={identity}/record.json"


def artifact_prefix(kind: str, identity: str) -> str:
    if not kind or "/" in kind or not identity or "/" in identity:
        raise AutomaticOutcomesError("invalid immutable artifact identity")
    return f"{NAMESPACE_ROOT}/{kind}/identity={identity}/"


def export_record_envelopes(store: ValidationStore) -> tuple[tuple[str, bytes], ...]:
    """Export deterministic per-record envelopes suitable for immutable R2 keys."""

    rows = store.connection.execute(
        "SELECT record_kind, record_key, payload_json, payload_sha256 "
        "FROM validation_record ORDER BY record_kind, record_key"
    ).fetchall()
    exported = []
    for kind, key, raw, digest in rows:
        payload = json.loads(bytes(raw))
        envelope = canonical_json({
            "protocol_id": PROTOCOL_ID,
            "protocol_hash": PROTOCOL_HASH,
            "protocol_epoch": PROTOCOL_EPOCH,
            "record_kind": kind,
            "record_key": key,
            "payload_sha256": digest,
            "payload": payload,
        })
        exported.append((record_object_key(str(kind), str(key)), envelope))
    return tuple(exported)


def import_record_envelopes(
    store: ValidationStore, envelopes: Iterable[bytes],
) -> int:
    """Hydrate the frozen SQLite view from immutable authoritative records."""

    appended = 0
    for raw in envelopes:
        try:
            item = json.loads(raw)
            if (
                item["protocol_id"] != PROTOCOL_ID
                or item["protocol_hash"] != PROTOCOL_HASH
                or item["protocol_epoch"] != PROTOCOL_EPOCH
                or hashlib.sha256(canonical_json(item["payload"])).hexdigest()
                != item["payload_sha256"]
            ):
                raise AutomaticOutcomesError("authoritative record envelope is invalid")
            appended += int(store.append(
                str(item["record_kind"]), str(item["record_key"]), item["payload"],
            ))
        except AutomaticOutcomesError:
            raise
        except Exception as exc:
            raise AutomaticOutcomesError("authoritative record envelope is malformed") from exc
    return appended


def _record_map(store: ValidationStore) -> dict[tuple[str, str], dict[str, Any]]:
    rows = store.connection.execute(
        "SELECT record_kind, record_key, payload_json, payload_sha256 "
        "FROM validation_record ORDER BY record_kind, record_key"
    ).fetchall()
    return {
        (str(kind), str(key)): {
            "record_kind": str(kind),
            "record_key": str(key),
            "payload_sha256": str(digest),
            "payload": json.loads(bytes(raw)),
        }
        for kind, key, raw, digest in rows
    }


def build_commit_bundle(
    store: ValidationStore,
    *,
    before: set[tuple[str, str]],
    operation: str,
    identity: str,
) -> bytes:
    """Build one atomic authoritative commit containing only newly added records."""

    records = _record_map(store)
    additions = [records[key] for key in sorted(set(records) - before)]
    if not additions:
        raise AutomaticOutcomesError("authoritative commit contains no new records")
    return canonical_json({
        "schema_name": "trinity.prospective-validation-commit",
        "schema_version": "1",
        "protocol_id": PROTOCOL_ID,
        "protocol_hash": PROTOCOL_HASH,
        "protocol_epoch": PROTOCOL_EPOCH,
        "operation": operation,
        "identity": identity,
        "records": additions,
    })


def import_commit_bundle(store: ValidationStore, raw: bytes) -> int:
    """Validate and atomically materialize one authoritative commit bundle."""

    try:
        item = json.loads(raw)
        if (
            item["schema_name"] != "trinity.prospective-validation-commit"
            or item["schema_version"] != "1"
            or item["protocol_id"] != PROTOCOL_ID
            or item["protocol_hash"] != PROTOCOL_HASH
            or item["protocol_epoch"] != PROTOCOL_EPOCH
            or not isinstance(item["records"], list)
        ):
            raise AutomaticOutcomesError("authoritative commit bundle is invalid")
    except AutomaticOutcomesError:
        raise
    except Exception as exc:
        raise AutomaticOutcomesError("authoritative commit bundle is malformed") from exc
    before = store.connection.total_changes
    store.connection.execute("SAVEPOINT import_authoritative_commit")
    try:
        for record in item["records"]:
            payload = record["payload"]
            if hashlib.sha256(canonical_json(payload)).hexdigest() \
                    != record["payload_sha256"]:
                raise AutomaticOutcomesError("commit record payload hash mismatch")
            store.append(
                str(record["record_kind"]), str(record["record_key"]), payload,
            )
        store.connection.execute("RELEASE SAVEPOINT import_authoritative_commit")
    except Exception:
        store.connection.execute("ROLLBACK TO SAVEPOINT import_authoritative_commit")
        store.connection.execute("RELEASE SAVEPOINT import_authoritative_commit")
        raise
    return store.connection.total_changes - before


def _list_keys(client: Any, bucket: str, prefix: str) -> tuple[str, ...]:
    keys: list[str] = []
    token = None
    while True:
        args: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        keys.extend(str(item["Key"]) for item in page.get("Contents", ()))
        if not page.get("IsTruncated"):
            return tuple(sorted(keys))
        token = page["NextContinuationToken"]


def hydrate_authoritative_store(
    client: Any, bucket: str, store: ValidationStore,
) -> int:
    """Restore all immutable commit bundles; no mutable pointer is consulted."""

    prefix = f"{NAMESPACE_ROOT}/commits/"
    appended = 0
    for key in _list_keys(client, bucket, prefix):
        if not key.endswith(".json"):
            raise AutomaticOutcomesError("unexpected object in commit namespace")
        raw = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        appended += import_commit_bundle(store, raw)
    return appended


def commit_key(operation: str, identity: str) -> str:
    if operation not in {"official-ingestion", "observation-slot"}:
        raise AutomaticOutcomesError("invalid prospective commit operation")
    safe_identity = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{NAMESPACE_ROOT}/commits/{operation}/identity={safe_identity}.json"


def archive_immutable_tree(
    client: Any, bucket: str, *, kind: str, identity: str, root: str | Path,
) -> dict[str, Any]:
    """Archive and verify one immutable snapshot/cycle directory."""

    source = Path(root)
    files = tuple(sorted(path for path in source.rglob("*") if path.is_file()))
    if not files:
        raise AutomaticOutcomesError("immutable artifact tree is empty")
    prefix = artifact_prefix(kind, identity)
    manifest_files = []
    for path in files:
        relative = path.relative_to(source).as_posix()
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        put_immutable(client, bucket, prefix + relative, body)
        manifest_files.append({
            "path": relative, "bytes": len(body), "sha256": digest,
        })
    manifest = canonical_json({
        "schema_name": "trinity.prospective-artifact-manifest",
        "schema_version": "1",
        "protocol_hash": PROTOCOL_HASH,
        "kind": kind,
        "identity": identity,
        "files": manifest_files,
    })
    manifest_sha256 = put_immutable(
        client, bucket, prefix + "ARCHIVE_MANIFEST.json", manifest,
    )
    return {
        "prefix": prefix,
        "object_count": len(files) + 1,
        "manifest_sha256": manifest_sha256,
    }


def _jsonable(value: Any) -> Any:
    """Convert frozen dataclasses and read-only mappings to canonical JSON data."""

    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _authoritative_record_digest(store: ValidationStore) -> str:
    """Return the deterministic identity of the current authoritative view."""

    rows = store.connection.execute(
        "SELECT record_kind, record_key, payload_sha256 "
        "FROM validation_record ORDER BY record_kind, record_key"
    ).fetchall()
    return hashlib.sha256(canonical_json([
        {
            "record_kind": str(kind),
            "record_key": str(key),
            "payload_sha256": str(payload_sha256),
        }
        for kind, key, payload_sha256 in rows
    ])).hexdigest()


def _stage_scorecard_row(stage: Any) -> dict[str, Any]:
    return {
        "status": stage.status,
        "members": list(stage.members),
        "member_count": stage.member_count,
        "complete_count": stage.complete_count,
        "pending_count": stage.pending_count,
        "dqf_count": stage.dqf_count,
        "gross_active_return_pct": stage.gross_active_return_pct,
        "cost_adjusted_active_return_pct": {
            str(bps): value
            for bps, value in stage.cost_adjusted_active_return_pct.items()
        },
    }


def _horizon_scorecard(
    weekly: tuple[Any, ...], horizon: int,
) -> dict[str, Any]:
    """Build descriptive D5/D20/D60 summaries without changing V1 endpoints."""

    cohorts = ("SCANNER", "LUNA", "SOL")
    cohort_summary: dict[str, Any] = {}
    for cohort in cohorts:
        stages = [week.basket(horizon, cohort) for week in weekly]
        values = [
            float(stage.gross_active_return_pct)
            for stage in stages if stage.gross_active_return_pct is not None
        ]
        cohort_summary[cohort] = {
            "n_official_weeks": len(stages),
            "n_usable": len(values),
            "n_pending": sum(stage.status == "PENDING" for stage in stages),
            "n_dqf": sum(stage.status == "UNAVAILABLE_DQF" for stage in stages),
            "mean_active_return_pct": None if not values else fmean(values),
        }

    def endpoint(first: str, second: str) -> dict[str, Any]:
        values: list[float] = []
        pending = dqf = 0
        for week in weekly:
            left = week.basket(horizon, first)
            right = week.basket(horizon, second)
            if left.status == "UNAVAILABLE_DQF" or right.status == "UNAVAILABLE_DQF":
                dqf += 1
            elif left.status == "PENDING" or right.status == "PENDING":
                pending += 1
            elif left.gross_active_return_pct is not None \
                    and right.gross_active_return_pct is not None:
                values.append(
                    float(left.gross_active_return_pct)
                    - float(right.gross_active_return_pct)
                )
            else:
                raise AutomaticOutcomesError("stage basket has an impossible scorecard state")
        return {
            "n_total_official_weeks": len(weekly),
            "n_usable": len(values),
            "n_pending": pending,
            "n_dqf": dqf,
            "mean_delta_active_return_pct": None if not values else fmean(values),
        }

    weekly_rows = []
    for week in weekly:
        stages = {
            cohort: _stage_scorecard_row(week.basket(horizon, cohort))
            for cohort in cohorts
        }
        deltas: dict[str, float | None] = {}
        for name, first, second in (
            ("LUNA_MINUS_SCANNER", "LUNA", "SCANNER"),
            ("SOL_MINUS_LUNA", "SOL", "LUNA"),
            ("SOL_MINUS_SCANNER", "SOL", "SCANNER"),
        ):
            left = stages[first]["gross_active_return_pct"]
            right = stages[second]["gross_active_return_pct"]
            deltas[name] = None if left is None or right is None else left - right
        weekly_rows.append({
            "run_id": week.run_id,
            "decision_target_session": week.decision_target_session,
            "horizon_sessions": horizon,
            "stages": stages,
            "deltas": deltas,
        })
    return {
        "horizon_sessions": horizon,
        "cohorts": cohort_summary,
        "endpoints": {
            "LUNA_MINUS_SCANNER": endpoint("LUNA", "SCANNER"),
            "SOL_MINUS_LUNA": endpoint("SOL", "LUNA"),
            "SOL_MINUS_SCANNER": endpoint("SOL", "SCANNER"),
        },
        "weekly_distribution": weekly_rows,
    }


def _selection_error_diagnostics(store: ValidationStore) -> dict[str, Any]:
    """Report missed Scanner winners and negative-return promoted selections."""

    runs = tuple(
        item for item in load_run_observations(store)
        if item.eligibility == OFFICIAL_ELIGIBLE
    )
    expectations = load_current_expectations(store)
    finals = {
        item.expectation_key: item
        for item in load_outcome_finals(store, expectations)
    }
    threshold = float(protocol_dict()["missed_winner_threshold_active_return_pct_20d"])
    missed_luna: list[dict[str, Any]] = []
    missed_sol: list[dict[str, Any]] = []
    promoted_underperformers: list[dict[str, Any]] = []
    for run in runs:
        for ticker in run.scanner_tickers:
            key = outcome_identity(run.run_id, ticker, "SCANNER", 20)
            final = finals.get(key)
            if final is None or final.status != COMPLETE \
                    or final.active_return_pct is None:
                continue
            active_return = float(final.active_return_pct)
            identity = {
                "run_id": run.run_id,
                "decision_target_session": run.decision_target_session,
                "ticker": ticker,
                "active_return_pct_20d": active_return,
            }
            if active_return >= threshold:
                if ticker not in run.luna_tickers:
                    missed_luna.append(identity)
                if ticker not in run.sol_tickers:
                    missed_sol.append(identity)
            promoted_by = [
                stage for stage, members in (
                    ("LUNA", run.luna_tickers), ("SOL", run.sol_tickers),
                ) if ticker in members
            ]
            if promoted_by and active_return < 0:
                promoted_underperformers.append({
                    **identity, "promoted_by": promoted_by,
                })
    return {
        "winner_threshold_active_return_pct_20d": threshold,
        "missed_winners_by_luna": missed_luna,
        "missed_winners_by_sol": missed_sol,
        "promoted_underperformers_20d": promoted_underperformers,
    }


def build_scorecard_artifact(store: ValidationStore) -> dict[str, Any]:
    """Build a deterministic descriptive artifact plus the frozen V1 scorecard."""

    weekly = build_official_weekly_baskets(store)
    runs = tuple(
        item for item in load_run_observations(store)
        if item.eligibility == OFFICIAL_ELIGIBLE
    )
    api_costs = [str(item.total_api_cost_usd) for item in runs]
    total_cost = sum((Decimal(value) for value in api_costs), Decimal("0"))
    stage_costs = {"LUNA": Decimal("0"), "SOL": Decimal("0")}
    official_run_ids = {item.run_id for item in runs}
    for _key, payload in store.record_items("SELECTION"):
        if payload.get("run_id") not in official_run_ids:
            continue
        stage_costs["LUNA"] += Decimal(str(payload.get("luna_cost_usd") or "0"))
        stage_costs["SOL"] += Decimal(str(payload.get("sol_cost_usd") or "0"))
    return {
        "schema_name": "trinity.prospective-scorecard",
        "schema_version": "1",
        "protocol_id": PROTOCOL_ID,
        "protocol_hash": PROTOCOL_HASH,
        "protocol_epoch": PROTOCOL_EPOCH,
        "source_records_sha256": _authoritative_record_digest(store),
        "official_run_count": len(runs),
        "api_costs": {
            "official_run_cost_usd": api_costs,
            "total_usd": format(total_cost, "f"),
            "by_stage_usd": {
                stage: format(value, "f") for stage, value in stage_costs.items()
            },
            "mean_per_official_run_usd": (
                None if not api_costs else format(total_cost / len(api_costs), "f")
            ),
        },
        "frozen_v1_scorecard": _jsonable(build_final_scorecard(store)),
        "horizons": {
            str(horizon): _horizon_scorecard(weekly, horizon)
            for horizon in (5, 20, 60)
        },
        "selection_error_diagnostics": _selection_error_diagnostics(store),
    }


def archive_scorecard(
    client: Any, bucket: str, *, store: ValidationStore, root: str | Path,
) -> dict[str, Any]:
    """Publish a deterministic scorecard tree keyed by authoritative records."""

    payload = build_scorecard_artifact(store)
    output = Path(root)
    output.mkdir(parents=True, exist_ok=True)
    (output / "scorecard.json").write_bytes(canonical_json(payload))
    return archive_immutable_tree(
        client,
        bucket,
        kind="scorecards",
        identity=str(payload["source_records_sha256"]),
        root=output,
    )


def _validate_official_run_id(run_id: str) -> str:
    if not _OFFICIAL_RUN_ID.fullmatch(run_id):
        raise AutomaticOutcomesError("invalid archived OFFICIAL run identity")
    return run_id


def discover_archived_official_run(
    client: Any,
    bucket: str,
    *,
    github_run_id: str,
    github_run_attempt: str,
) -> str:
    """Find exactly one archived complete-run identity for a workflow retry."""

    marker = f"_{github_run_id}_A{github_run_attempt}"
    candidates: set[str] = set()
    for key in _list_keys(client, bucket, SHADOW_RUN_ROOT):
        if not key.endswith("/artifacts/run_state.json") or "run_id=" not in key:
            continue
        run_id = key.split("run_id=", 1)[1].split("/", 1)[0]
        if run_id.endswith(marker):
            candidates.add(_validate_official_run_id(run_id))
    if len(candidates) != 1:
        raise AutomaticOutcomesError(
            "expected exactly one archived OFFICIAL run for workflow retry, "
            f"found={len(candidates)}"
        )
    return next(iter(candidates))


def restore_archived_run(
    client: Any,
    bucket: str,
    *,
    run_id: str,
    destination: str | Path,
) -> Path:
    """Restore a verified immutable Shadow archive without rerunning analysis."""

    run_id = _validate_official_run_id(run_id)
    prefix = f"{SHADOW_RUN_ROOT}run_id={run_id}/"
    output = Path(destination)
    if output.exists() and any(output.iterdir()):
        raise AutomaticOutcomesError("restore destination must be new and empty")
    output.mkdir(parents=True, exist_ok=True)
    keys = _list_keys(client, bucket, prefix)
    if not keys:
        raise AutomaticOutcomesError("archived OFFICIAL run is not available in R2")
    for key in keys:
        relative_key = key.removeprefix(prefix)
        parts = relative_key.split("/", 1)
        if len(parts) != 2 or parts[0] not in {"artifacts", "ledger", "manifests", "reports"}:
            raise AutomaticOutcomesError("archived run contains an unexpected object")
        relative = PurePosixPath(parts[1])
        if relative.is_absolute() or ".." in relative.parts:
            raise AutomaticOutcomesError("archived run contains an unsafe path")
        target = output.joinpath(*relative.parts)
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read()
        metadata = response.get("Metadata", {})
        expected_sha = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        if expected_sha and hashlib.sha256(body).hexdigest() != expected_sha:
            raise AutomaticOutcomesError(f"archived run checksum mismatch: {key}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return output


def reconcile_official_run_r2(
    client: Any,
    bucket: str,
    *,
    local_store: str | Path,
    restore_root: str | Path,
    run_id: str | None = None,
    github_run_id: str | None = None,
    github_run_attempt: str = "1",
) -> dict[str, Any]:
    """Retry only the feedback ingestion from an already archived OFFICIAL run."""

    if run_id is None:
        if not github_run_id:
            raise AutomaticOutcomesError("run_id or github_run_id is required")
        run_id = discover_archived_official_run(
            client,
            bucket,
            github_run_id=str(github_run_id),
            github_run_attempt=str(github_run_attempt),
        )
    restored = restore_archived_run(
        client, bucket, run_id=run_id, destination=restore_root,
    )
    result = ingest_official_run_r2(
        client, bucket, run_root=restored, local_store=local_store,
    )
    return {**result, "restored_run_id": run_id, "restored_root": str(restored)}


def ingest_official_run_r2(
    client: Any,
    bucket: str,
    *,
    run_root: str | Path,
    local_store: str | Path,
) -> dict[str, Any]:
    """Hydrate, ingest, and atomically publish an OFFICIAL cohort commit."""

    with ValidationStore(local_store) as store:
        hydrate_authoritative_store(client, bucket, store)
        before = set(_record_map(store))
        result = ingest_official_run(store, run_root)
        key = commit_key("official-ingestion", result["run_id"])
        if set(_record_map(store)) == before:
            # An identical replay must already have an identical authoritative commit.
            raw = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            import_commit_bundle(store, raw)
            return {**result, "authoritative_commit_key": key, "idempotent": True}
        bundle = build_commit_bundle(
            store,
            before=before,
            operation="official-ingestion",
            identity=result["run_id"],
        )
        digest = put_immutable(client, bucket, key, bundle)
    return {
        **result,
        "authoritative_commit_key": key,
        "authoritative_commit_sha256": digest,
        "idempotent": False,
    }


def run_outcomes_r2(
    client: Any,
    bucket: str,
    *,
    iso_year: int,
    iso_week: int,
    local_store: str | Path,
    work_root: str | Path,
    provider: Any,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Execute one authoritative slot and atomically publish its validation records."""

    with ValidationStore(local_store) as store:
        hydrate_authoritative_store(client, bucket, store)
        plan = plan_observation_slot(
            store, iso_year=iso_year, iso_week=iso_week, now_utc=now(),
        )
        if plan.status in {NO_MATURE_EXPECTATIONS, ALREADY_COMPLETED}:
            scorecard = archive_scorecard(
                client,
                bucket,
                store=store,
                root=Path(work_root) / "scorecard",
            )
            return {
                "status": plan.status,
                "slot_target_session": plan.slot.slot_target_session,
                "provider_calls": 0,
                "scorecard": scorecard,
            }
        before = set(_record_map(store))
        result = execute_observation_slot(
            store, plan=plan, provider=provider, work_root=work_root, now=now,
        )
        root = Path(work_root)
        archives = {
            "stock": archive_immutable_tree(
                client, bucket, kind="stock-observations",
                identity=result["observation_cycle_id"], root=root / "stock",
            ),
            "spy": archive_immutable_tree(
                client, bucket, kind="spy-benchmarks",
                identity=result["observation_cycle_id"], root=root / "spy",
            ),
            "cycle": archive_immutable_tree(
                client, bucket, kind="observation-cycles",
                identity=result["observation_cycle_id"], root=root / "cycle",
            ),
        }
        bundle = build_commit_bundle(
            store,
            before=before,
            operation="observation-slot",
            identity=plan.slot.observation_slot_id,
        )
        key = commit_key("observation-slot", plan.slot.observation_slot_id)
        digest = put_immutable(client, bucket, key, bundle)
        scorecard = archive_scorecard(
            client,
            bucket,
            store=store,
            root=root / "scorecard",
        )
    return {
        **result,
        "archives": archives,
        "authoritative_commit_key": key,
        "authoritative_commit_sha256": digest,
        "scorecard": scorecard,
    }


def put_immutable(client: Any, bucket: str, key: str, body: bytes) -> str:
    """Create-or-verify one immutable object; divergent replay fails closed."""

    digest = hashlib.sha256(body).hexdigest()
    try:
        existing = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if str(code) not in {"404", "NoSuchKey", "NotFound"}:
            raise AutomaticOutcomesError("immutable object lookup failed") from exc
    else:
        if existing != body:
            raise AutomaticOutcomesError(f"immutable object collision: {key}")
        return digest
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        Metadata={"sha256": digest},
    )
    verified = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    if verified != body:
        raise AutomaticOutcomesError(f"immutable object verification failed: {key}")
    return digest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest-official")
    ingest.add_argument("--run-root", type=Path, required=True)
    ingest.add_argument("--store", type=Path, required=True)
    ingest_r2 = sub.add_parser("ingest-official-r2")
    ingest_r2.add_argument("--run-root", type=Path, required=True)
    ingest_r2.add_argument("--store", type=Path, required=True)
    reconcile = sub.add_parser("reconcile-official-r2")
    reconcile.add_argument("--run-id")
    reconcile.add_argument("--github-run-id")
    reconcile.add_argument("--github-run-attempt", default="1")
    reconcile.add_argument("--restore-root", type=Path, required=True)
    reconcile.add_argument("--store", type=Path, required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--store", type=Path, required=True)
    plan.add_argument("--iso-year", type=int, required=True)
    plan.add_argument("--iso-week", type=int, required=True)
    plan.add_argument("--now-utc", type=datetime.fromisoformat, required=True)
    outcomes_r2 = sub.add_parser("run-outcomes-r2")
    outcomes_r2.add_argument("--store", type=Path, required=True)
    outcomes_r2.add_argument("--work-root", type=Path, required=True)
    outcomes_r2.add_argument("--iso-year", type=int, required=True)
    outcomes_r2.add_argument("--iso-week", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command in {
        "ingest-official-r2", "reconcile-official-r2", "run-outcomes-r2",
    }:
        from trinity.shadow.r2 import client_from_environment
        client, bucket = client_from_environment()
        if args.command == "run-outcomes-r2":
            result = run_outcomes_r2(
                client,
                bucket,
                iso_year=args.iso_year,
                iso_week=args.iso_week,
                local_store=args.store,
                work_root=args.work_root,
                provider=TwelveDataClient(),
            )
        elif args.command == "reconcile-official-r2":
            result = reconcile_official_run_r2(
                client,
                bucket,
                local_store=args.store,
                restore_root=args.restore_root,
                run_id=args.run_id,
                github_run_id=args.github_run_id,
                github_run_attempt=args.github_run_attempt,
            )
        else:
            result = ingest_official_run_r2(
                client, bucket, run_root=args.run_root, local_store=args.store,
            )
    else:
        with ValidationStore(args.store) as store:
            if args.command == "ingest-official":
                result = ingest_official_run(store, args.run_root)
            else:
                result = asdict(plan_observation_slot(
                    store,
                    iso_year=args.iso_year,
                    iso_week=args.iso_week,
                    now_utc=args.now_utc,
                ))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
