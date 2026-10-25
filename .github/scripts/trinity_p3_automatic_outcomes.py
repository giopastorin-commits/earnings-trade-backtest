"""P3 orchestration over the frozen TRINITY Live Validation V1 primitives.

This module intentionally owns no strategy, calendar, acquisition, benchmark,
or outcome mathematics.  It sequences the immutable implementation checked out
at TRINITY_ANALYTICAL_REF and exposes dependency-injected entry points so its
production transports can be tested without network access.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
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
from trinity.validation.outcome_expectations import append_outcome_expectations
from trinity.validation.outcome_finalization import CycleSourceLocator, finalize_outcomes
from trinity.validation.protocol_v1 import (
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
    canonical_json,
    protocol_dict,
)
from trinity.validation.run_observation import append_run_observation
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
            "protocol_id": PROTOCOL_ID,
            "protocol_hash": PROTOCOL_HASH,
            "protocol_epoch": PROTOCOL_EPOCH,
            "run_id": collected.run_id,
            "source_manifest_sha256": collected.source_manifest_sha256,
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
            return {
                "status": plan.status,
                "slot_target_session": plan.slot.slot_target_session,
                "provider_calls": 0,
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
    return {
        **result,
        "archives": archives,
        "authoritative_commit_key": key,
        "authoritative_commit_sha256": digest,
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
    if args.command in {"ingest-official-r2", "run-outcomes-r2"}:
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
