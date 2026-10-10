"""Manual cloud Shadow Production entry point."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import time

from trinity.ledger import LedgerStorage
from trinity.paths import baseline_root, price_snapshot_root, run_root
from trinity.pilots.forward import render_forward_summary, run_forward_pilot
from trinity.pilots.luna_triage import FreshTriageSources, run_triage
from trinity.pilots.pre_research import SnapshotPrices, run_funnel
from trinity.pilots.production_funnel import run_production_funnel
from trinity.twelvedata_prices import ValidatedPriceSnapshot
from trinity.usa_forward import EODHDNewsSECForwardProvider

from .atomic import atomic_json
from .cutoff import capture_live_research_cutoff, cutoff_for_verified_session
from .golden_replay import GoldenReplayError, run_golden_replay
from .manifest import generate_manifest, verify_manifest
from .model_adapters import ResponsesLuna, ResponsesSolProvider
from .r2 import (
    archive_run, client_from_environment, restore_baseline, restore_price_snapshot,
)
from .responses import CostGuardStop, PersistentCostLedger, ResponsesAPI
from .sqlite_lifecycle import close_and_verify, initialize_fresh_ledger
from .state import RunState


def restore_command() -> int:
    try:
        client, bucket = client_from_environment()
        prefix = os.environ.get("TRINITY_BASELINE_PREFIX", "")
        manifest = restore_baseline(client, bucket, prefix, baseline_root())
        atomic_json(run_root() / "baseline_restore.json", {
            "prefix": prefix, "file_count": manifest.get("file_count", len(manifest.get("files", []))),
            "total_bytes": manifest.get("total_bytes"),
        })
        return 0
    except Exception as exc:
        github_run_id = os.environ.get("GITHUB_RUN_ID", "local")
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        run_id = f"SHADOW_USA_PENDING_{github_run_id}_A{attempt}"
        failure_root = run_root() / run_id
        failure_root.mkdir(parents=True, exist_ok=True)
        state = RunState(failure_root / "run_state.json", run_id)
        state.write("FAILED_DATA", f"baseline restore: {type(exc).__name__}: {exc}")
        atomic_json(run_root() / "current_run.json", {"run_id": run_id, "path": str(failure_root)})
        raise


def restore_prices_command() -> int:
    client, bucket = client_from_environment()
    prefix = os.environ.get("TRINITY_PRICE_SNAPSHOT_PREFIX", "")
    expected = os.environ.get("TRINITY_PRICE_SNAPSHOT_SHA256", "")
    manifest = restore_price_snapshot(
        client, bucket, prefix, price_snapshot_root(),
        expected_snapshot_sha256=expected,
    )
    atomic_json(run_root() / "price_snapshot_restore.json", {
        "prefix": prefix,
        "snapshot_id": manifest["snapshot_id"],
        "target_market_session": manifest["target_market_session"],
        "snapshot_sha256": manifest["snapshot_sha256"],
    })
    return 0


def run_command() -> int:
    # This verification is the hard coverage gate. No analytical artifact or
    # model call is created until the explicit snapshot is READY and hash-valid.
    snapshot = ValidatedPriceSnapshot(price_snapshot_root(), require_ready=True)
    started = time.perf_counter()
    github_run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    preliminary_id = f"SHADOW_USA_PENDING_{github_run_id}_A{attempt}"
    staging = run_root() / preliminary_id
    staging.mkdir(parents=True, exist_ok=False)
    artifacts = staging / "artifacts"
    artifacts.mkdir()
    restore_record = run_root() / "baseline_restore.json"
    if restore_record.is_file():
        atomic_json(
            artifacts / "baseline_restore.json",
            json.loads(restore_record.read_text(encoding="utf-8")),
        )
    price_restore_record = run_root() / "price_snapshot_restore.json"
    if price_restore_record.is_file():
        atomic_json(
            artifacts / "price_snapshot_restore.json",
            json.loads(price_restore_record.read_text(encoding="utf-8")),
        )
    state = RunState(staging / "run_state.json", preliminary_id)
    atomic_json(run_root() / "current_run.json", {"run_id": preliminary_id, "path": str(staging)})
    stage = "DATA"
    try:
        funnel_path = artifacts / "pre_research_funnel.json"
        funnel = run_funnel(
            output_path=funnel_path,
            provider=SnapshotPrices(snapshot),
        )
        latest_dates = [
            str(item["last_valid_date"]) for item in snapshot.manifest["tickers"]
            if item["classification"] == "ACTIVE_COMPLETE"
        ]
        cutoff = cutoff_for_verified_session(funnel.latest_completed_session, latest_dates)
        run_id = f"SHADOW_USA_{cutoff.session_date}_{github_run_id}_A{attempt}"
        final_root = run_root() / run_id
        staging.rename(final_root)
        staging = final_root
        artifacts = staging / "artifacts"
        funnel_path = artifacts / "pre_research_funnel.json"
        cache = snapshot.normalized_root
        state.path = staging / "run_state.json"
        state.run_id = run_id
        state.write("RUNNING")
        atomic_json(run_root() / "current_run.json", {"run_id": run_id, "path": str(staging)})
        atomic_json(artifacts / "decision_cutoff.json", asdict(cutoff))

        usage = PersistentCostLedger(artifacts / "api_usage.json", Decimal(os.getenv("TRINITY_COST_GUARD_USD", "5.00")))
        transport = ResponsesAPI(run_root=staging, cost_ledger=usage)
        stage = "SETUP"
        research_cutoff = None

        def begin_fresh_research() -> None:
            nonlocal research_cutoff
            if research_cutoff is not None:
                raise RuntimeError("fresh research boundary was entered more than once")
            research_cutoff = capture_live_research_cutoff(
                verified_price_session=cutoff.session_date,
                technical_cutoff_utc=cutoff.utc,
            )
            atomic_json(artifacts / "research_cutoff.json", asdict(research_cutoff))

        def triage_runner(**kwargs):
            nonlocal stage
            stage = "LUNA"
            rows = kwargs["ready_entries"]
            tickers = [str(row["ticker"]) for row in rows]
            if research_cutoff is None:
                raise RuntimeError("live research cutoff was not captured")
            source = FreshTriageSources(
                cache_dir=staging / "luna_sources",
                sec_root=baseline_root() / "sec_compact",
                research_cutoff_utc=research_cutoff_dt(),
            )
            return run_triage(
                **kwargs, luna=ResponsesLuna(transport, tickers), sources=source,
            )

        cutoff_dt = datetime.fromisoformat(cutoff.utc.replace("Z", "+00:00"))

        def research_cutoff_dt() -> datetime:
            if research_cutoff is None:
                raise RuntimeError("live research cutoff was not captured")
            return datetime.fromisoformat(
                research_cutoff.research_cutoff_utc.replace("Z", "+00:00")
            )

        ledger_path = staging / "ledger" / "trinity.sqlite3"
        sol_ordinal = 10_000

        def sol_runner(database, tickers, *, dry_run):
            nonlocal sol_ordinal, stage
            stage = "SOL"
            result = run_forward_pilot(
                database, tickers, dry_run=dry_run,
                source_root=staging / "fresh_sources",
                source_provider_factory=lambda root: EODHDNewsSECForwardProvider(
                    root, price_snapshot=snapshot,
                    continuity_path=artifacts / "luna_results.json",
                    decision_cutoff_utc=cutoff_dt,
                    research_cutoff_utc=research_cutoff_dt(),
                ),
                llm_provider_factory=lambda ticker: ResponsesSolProvider(transport, ticker, sol_ordinal),
            )
            sol_ordinal += 2 * len(tickers)
            return result

        result = run_production_funnel(
            funnel_path=funnel_path, price_dir=cache,
            luna_output_path=artifacts / "luna_results.json",
            watchlist_path=artifacts / "active_watch.json",
            luna_source_root=staging / "luna_sources",
            ledger_db=ledger_path, triage_runner=triage_runner, sol_runner=sol_runner,
            price_snapshot=snapshot,
            before_fresh_research=begin_fresh_research,
        )
        if research_cutoff is None:
            # A custom/no-candidate runner may have no fresh research phase, but
            # the completed live run still carries an explicit boundary.
            begin_fresh_research()
        atomic_json(artifacts / "setups.json", [item.to_dict() for item in result.setups])
        if result.failed_tickers:
            raise RuntimeError(f"Luna failures: {', '.join(result.failed_tickers)}")
        stage = "SOL"
        if result.sol_result is not None and any(item.run_status == "FAILED" for item in result.sol_result.results):
            raise RuntimeError("one or more Sol ticker pipelines failed")
        if not ledger_path.exists():
            initialize_fresh_ledger(ledger_path)
        with LedgerStorage.open(ledger_path) as storage:
            storage.insert_json_artifact(
                artifact_type="ledger.price-snapshot-provenance.v1",
                value=snapshot.provenance(),
                media_type="application/json",
            )
            storage.insert_json_artifact(
                artifact_type="ledger.live-research-cutoff-provenance.v1",
                value=asdict(research_cutoff),
                media_type="application/json",
            )
        stage = "LEDGER"
        close_and_verify(ledger_path)
        sol_rows = [] if result.sol_result is None else [asdict(item) for item in result.sol_result.results]
        atomic_json(artifacts / "sol_results.json", sol_rows)
        telegram = (
            render_forward_summary(result.sol_result.results) if result.sol_result is not None
            else "TRINITY Shadow Production\nNo ESCALATE tickers; no Sol research results.\nDRY RUN - NOT SENT"
        )
        (artifacts / "telegram_dry_run.txt").write_text(telegram + "\n", encoding="utf-8")
        atomic_json(artifacts / "final_report.json", {
            "run_id": run_id, "git_commit": _git_head(), "github_run_id": github_run_id,
            "github_run_attempt": attempt, "decision_cutoff": asdict(cutoff),
            "research_cutoff": asdict(research_cutoff),
            "counts": {**funnel.counts, "operational_setup": result.operational_setup_count,
                       "no_setup": result.no_setup_count, "luna_watch": len(result.watch_tickers),
                       "luna_escalate": len(result.escalate_tickers), "luna_drop": len(result.drop_tickers)},
            "watch": list(result.watch_tickers), "escalate": list(result.escalate_tickers),
            "drop": list(result.drop_tickers), "sol_results": sol_rows,
            "method_versions": {"setup": "USA_SETUP_V1", "facts": "USA_V2_FACTS_V3",
                                "luna": "luna-triage-v1",
                                "live_research_cutoff": research_cutoff.method_version},
            "price_snapshot": snapshot.provenance(),
            "runtime_seconds": time.perf_counter() - started,
            "telegram_sent": False, "broker_execution": False,
        })
        return 0
    except CostGuardStop as exc:
        state.write("COST_GUARD_STOP", str(exc)); return 2
    except Exception as exc:
        mapping = {"DATA": "FAILED_DATA", "SETUP": "FAILED_SETUP", "LUNA": "FAILED_LUNA",
                   "SOL": "FAILED_SOL", "LEDGER": "FAILED_LEDGER"}
        state.write(mapping.get(stage, "FAILED_DATA"), f"{type(exc).__name__}: {exc}")
        return 1


def archive_command() -> int:
    pointer = json.loads((run_root() / "current_run.json").read_text(encoding="utf-8"))
    root = Path(pointer["path"])
    state_value = json.loads((root / "run_state.json").read_text(encoding="utf-8"))
    if state_value["state"] == "RUNNING":
        state_value.update({"state": "COMPLETE", "recorded_at": datetime.now(timezone.utc).isoformat(), "error": None})
        atomic_json(root / "run_state.json", state_value)
    manifest = generate_manifest(root, run_id=pointer["run_id"])
    verify_manifest(root, manifest)
    client, bucket = client_from_environment()
    try:
        remote = archive_run(client, bucket, pointer["run_id"], root)
    except Exception as exc:
        if state_value["state"] == "COMPLETE":
            state_value.update({"state": "FAILED_ARCHIVE", "error": f"{type(exc).__name__}: {exc}"})
            atomic_json(root / "run_state.json", state_value)
        raise
    atomic_json(root / "r2_archive_result.json", remote)
    return 0


def _git_head() -> str:
    expected = os.environ.get("GITHUB_SHA")
    if expected:
        return expected
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, text=True, capture_output=True,
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("restore", "restore-prices", "run", "archive", "golden-replay"),
    )
    parser.add_argument("--decision-cutoff")
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--universe-path", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--freeze-reference-root", type=Path)
    args = parser.parse_args(argv)
    if args.command == "golden-replay":
        missing = [name for name in (
            "decision_cutoff", "baseline_root", "universe_path", "output_root",
        ) if getattr(args, name) is None]
        if missing:
            parser.error("golden-replay requires " + ", ".join("--" + name.replace("_", "-") for name in missing))
        try:
            comparison = run_golden_replay(
                decision_cutoff=args.decision_cutoff,
                baseline_root=args.baseline_root,
                universe_path=args.universe_path,
                output_root=args.output_root,
                freeze_reference_root=args.freeze_reference_root,
            )
        except GoldenReplayError as exc:
            parser.error(str(exc))
        print(json.dumps(comparison, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    return {
        "restore": restore_command, "restore-prices": restore_prices_command,
        "run": run_command, "archive": archive_command,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
