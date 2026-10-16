"""Fail-closed reader for immutable TRINITY Shadow artifacts."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any, Mapping

from .contracts import (
    CollectedRun, ExecutionObservation, SelectionObservation, SetupObservation,
    ValidationContractError,
)
from .protocol_v1 import canonical_json


REQUIRED_FILES = frozenset({
    "artifacts/pre_research_funnel.json", "artifacts/setups.json",
    "artifacts/luna_results.json", "artifacts/sol_results.json",
    "artifacts/final_report.json", "artifacts/api_usage.json",
    "artifacts/weekly_orchestrator.json", "artifacts/price_snapshot_restore.json",
    "ledger/trinity.sqlite3", "run_state.json",
})
_PULLBACK = re.compile(r"BUY ON PULLBACK ([0-9.]+)-([0-9.]+)\Z")


class CollectionError(ValidationContractError):
    pass


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"invalid JSON artifact: {path.name}") from exc


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_source_manifest(root: str | Path) -> tuple[dict[str, Any], str]:
    base = Path(root).resolve()
    manifest_path = base / "manifest.json"
    manifest = _json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_name") != "trinity.shadow-run-manifest":
        raise CollectionError("invalid Shadow manifest identity")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise CollectionError("Shadow manifest lacks file inventory")
    inventory: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise CollectionError("malformed Shadow manifest entry")
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in inventory:
            raise CollectionError("unsafe or duplicate Shadow manifest path")
        inventory.add(relative.as_posix())
        path = (base / Path(*relative.parts)).resolve()
        if base not in path.parents or not path.is_file():
            raise CollectionError(f"missing inventoried artifact: {relative.as_posix()}")
        if path.stat().st_size != row.get("bytes") or _hash(path) != row.get("sha256"):
            raise CollectionError(f"inventoried artifact mismatch: {relative.as_posix()}")
    missing = sorted(REQUIRED_FILES - inventory)
    if missing:
        raise CollectionError(f"required artifacts absent from manifest: {', '.join(missing)}")
    return manifest, _hash(manifest_path)


def _setup_bounds(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    if row.get("setup_type") == "NO_SETUP":
        return None, None
    entry = float(row["entry_level"])
    if row.get("setup_type") != "PULLBACK":
        return entry, entry
    match = _PULLBACK.fullmatch(str(row.get("entry_condition") or ""))
    if match is None or float(match.group(2)) != entry:
        raise CollectionError(f"{row.get('ticker')}: frozen entry range is invalid")
    return float(match.group(1)), float(match.group(2))


def _usage_by_ticker(calls: object) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(calls, list):
        raise CollectionError("API usage artifact lacks calls")
    for call in calls:
        if not isinstance(call, dict) or not call.get("ticker"):
            raise CollectionError("API usage call lacks ticker")
        grouped.setdefault(str(call["ticker"]), []).append(call)
    return grouped


def _sum_cost(calls: list[Mapping[str, Any]], role_prefix: str) -> str:
    value = sum((Decimal(str(item["calculated_cost_usd"])) for item in calls
                 if str(item.get("role") or "").startswith(role_prefix)), Decimal("0"))
    return format(value, "f")


def _sol_output_hash(root: Path, ticker: str) -> str | None:
    paths = sorted((root / "model_responses").glob(f"*_ {ticker}_critic.json".replace("_ ", "_")))
    if len(paths) > 1:
        raise CollectionError(f"{ticker}: ambiguous Sol Critic response")
    return _hash(paths[0]) if paths else None


def _artifact_digest(value: object, field: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    digest = text.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise CollectionError(f"invalid {field} artifact identity")
    return digest


def _critic_lineage(ledger_path: Path) -> dict[str, dict[str, str | None]]:
    """Read final Critic identities from the immutable operational Ledger."""

    uri = f"file:{ledger_path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute("""
            SELECT a.worker_identity, li.model, li.model_version,
                   li.request_artifact_id, li.response_artifact_id, li.status
            FROM research_llm_interaction AS rli
            JOIN llm_interaction AS li
              ON li.llm_interaction_id = rli.llm_interaction_id
            JOIN attempt AS a ON a.attempt_id = li.attempt_id
            WHERE rli.interaction_role = 'CRITIQUE'
        """).fetchall()
    except sqlite3.Error as exc:
        raise CollectionError("operational Ledger lacks readable Critic lineage") from exc
    finally:
        if "connection" in locals():
            connection.close()
    result: dict[str, dict[str, str | None]] = {}
    for worker, model, model_version, request_id, response_id, status in rows:
        prefix = "forward-pilot:"
        if not isinstance(worker, str) or not worker.startswith(prefix):
            raise CollectionError("Critic lineage has unexpected worker identity")
        ticker = worker.removeprefix(prefix).upper()
        if ticker in result:
            raise CollectionError(f"{ticker}: ambiguous Critic lineage")
        result[ticker] = {
            "model": str(model), "prompt_version": str(model_version),
            "input_sha256": _artifact_digest(request_id, "Critic request"),
            "output_sha256": _artifact_digest(response_id, "Critic response"),
            "status": str(status),
        }
    return result


def collect_run(root: str | Path) -> CollectedRun:
    """Read and validate a completed run without writing to it or importing transports."""

    base = Path(root).resolve()
    manifest, manifest_sha = verify_source_manifest(base)
    artifacts = base / "artifacts"
    funnel = _json(artifacts / "pre_research_funnel.json")
    setups_raw = _json(artifacts / "setups.json")
    luna = _json(artifacts / "luna_results.json")
    sol_raw = _json(artifacts / "sol_results.json")
    report = _json(artifacts / "final_report.json")
    usage = _json(artifacts / "api_usage.json")
    orchestrator = _json(artifacts / "weekly_orchestrator.json")
    restore = _json(artifacts / "price_snapshot_restore.json")
    run_state = _json(base / "run_state.json")
    run_id = str(report.get("run_id") or "")
    if not run_id or manifest.get("run_id") != run_id or run_state.get("run_id") != run_id:
        raise CollectionError("run identity differs across authoritative artifacts")
    if run_state.get("state") != "COMPLETE":
        raise CollectionError("only COMPLETE Shadow runs can be collected")
    if orchestrator.get("mode") != "VALIDATION" or orchestrator.get("official_benchmark_observation") is not False:
        raise CollectionError("source run is not a non-official VALIDATION run")
    commit = str(orchestrator.get("analytical_commit") or "")
    if report.get("git_commit") != commit:
        raise CollectionError("analytical commit provenance mismatch")
    snapshot = report.get("price_snapshot")
    funnel_snapshot = funnel.get("price_snapshot")
    if not isinstance(snapshot, dict) or not isinstance(funnel_snapshot, dict):
        raise CollectionError("price snapshot provenance missing")
    snapshot_id = str(snapshot.get("price_snapshot_id") or "")
    snapshot_sha = str(snapshot.get("price_snapshot_sha256") or "")
    session = str(snapshot.get("price_snapshot_session") or "")
    if (
        funnel_snapshot.get("snapshot_id") != snapshot_id
        or funnel_snapshot.get("sha256") != snapshot_sha
        or funnel_snapshot.get("session") != session
        or restore.get("snapshot_id") != snapshot_id
        or restore.get("snapshot_sha256") != snapshot_sha
        or orchestrator.get("target_market_session") != session
    ):
        raise CollectionError("snapshot lineage is incomplete or inconsistent")
    if not isinstance(setups_raw, list) or not isinstance(sol_raw, list):
        raise CollectionError("Setup or Sol artifact has invalid shape")
    luna_rows = luna.get("results")
    scanner_rows = funnel.get("survivors")
    if not isinstance(luna_rows, list) or not isinstance(scanner_rows, list):
        raise CollectionError("Scanner or Luna artifact has invalid shape")
    universe_inputs = funnel.get("inputs")
    universe_count = funnel.get("counts", {}).get("universe")
    universe_source = funnel.get("universe_source")
    if (
        not isinstance(universe_inputs, dict)
        or len(universe_inputs) != universe_count
        or not isinstance(universe_source, str)
        or not universe_source
    ):
        raise CollectionError("universe identity or membership is incomplete")
    scanner = {str(row["ticker"]): row for row in scanner_rows
               if isinstance(row, dict) and row.get("screening_state") == "READY_TECHNICALLY"}
    if len(scanner) != int(funnel.get("counts", {}).get("ready_technically", -1)):
        raise CollectionError("Scanner READY membership count mismatch")
    setup_map = {str(row.get("ticker")): row for row in setups_raw if isinstance(row, dict)}
    if set(setup_map) != set(scanner) or len(setup_map) != len(setups_raw):
        raise CollectionError("Setup membership differs from Scanner membership")
    luna_map = {str(row.get("ticker")): row for row in luna_rows if isinstance(row, dict)}
    if len(luna_map) != len(luna_rows) or not set(luna_map).issubset(scanner):
        raise CollectionError("Luna membership is invalid")
    sol_map = {str(row.get("ticker")): row for row in sol_raw if isinstance(row, dict)}
    if len(sol_map) != len(sol_raw):
        raise CollectionError("duplicate Sol ticker")
    routed = {ticker for ticker, row in luna_map.items()
              if row.get("status") == "SUCCESS" and row.get("decision") == "ESCALATE"}
    if set(sol_map) != routed:
        raise CollectionError("Sol evaluated membership differs from Luna ESCALATE")
    usage_map = _usage_by_ticker(usage.get("calls"))
    critic_lineage = _critic_lineage(base / "ledger" / "trinity.sqlite3")
    universe_hash = hashlib.sha256(canonical_json(sorted(str(key) for key in universe_inputs))).hexdigest()
    universe_version = PurePosixPath(universe_source.replace("\\", "/")).name
    run_timestamp = str(report.get("research_cutoff", {}).get("research_cutoff_utc") or "")
    selections: list[SelectionObservation] = []
    setup_observations: list[SetupObservation] = []
    executions: list[ExecutionObservation] = []
    for ticker in sorted(scanner):
        luna_row = luna_map.get(ticker)
        sol_row = sol_map.get(ticker)
        calls = usage_map.get(ticker, [])
        flags: list[str] = []
        if luna_row is None:
            luna_evaluated, luna_status, luna_decision = False, "NOT_EVALUATED", "NOT_EVALUATED"
        elif luna_row.get("status") != "SUCCESS":
            luna_evaluated, luna_status, luna_decision = True, "EVALUATED_FAILED", "EVALUATED_FAILED"
        else:
            luna_evaluated, luna_status = True, "COMPLETED"
            luna_decision = str(luna_row.get("decision") or "")
        if sol_row is None:
            sol_evaluated, sol_status, sol_decision = False, "NOT_EVALUATED", "NOT_EVALUATED"
        elif sol_row.get("run_status") == "FAILED":
            sol_evaluated, sol_status, sol_decision = True, "EVALUATED_FAILED", "EVALUATED_FAILED"
        else:
            sol_evaluated = True
            sol_decision = str(sol_row.get("research_status") or "")
            sol_status = "PROMOTED" if sol_decision == "INVESTIGATE" else "REJECTED"
        sol_response_file = _sol_output_hash(base, ticker) if sol_evaluated else None
        lineage = critic_lineage.get(ticker)
        if lineage is None:
            sol_input = sol_output = None
            if sol_evaluated:
                flags.append("SOL_CRITIC_LINEAGE_NOT_COMMITTED")
        else:
            sol_input = lineage["input_sha256"]
            sol_output = lineage["output_sha256"]
            if sol_response_file is None:
                raise CollectionError(f"{ticker}: committed Critic lacks response artifact")
        selections.append(SelectionObservation(
            run_id=run_id, intended_session=session, run_timestamp_utc=run_timestamp,
            ticker=ticker, universe_version=universe_version,
            universe_sha256=universe_hash, scanner_selected=True,
            scanner_state="READY_TECHNICALLY", scanner_reason=str(scanner[ticker].get("primary_reason") or ""),
            luna_evaluated=luna_evaluated, luna_status=luna_status, luna_decision=luna_decision,
            luna_model=str(luna_row.get("model")) if luna_row else None,
            luna_prompt_version=str(luna_row.get("prompt_version")) if luna_row else None,
            luna_input_sha256=luna_row.get("input_sha256") if luna_row else None,
            luna_output_sha256=luna_row.get("raw_response_sha256") if luna_row else None,
            luna_cost_usd=_sum_cost(calls, "LUNA"), sol_evaluated=sol_evaluated,
            sol_status=sol_status, sol_decision=sol_decision,
            sol_model=str(lineage["model"]) if lineage else
                (str(next((x.get("model") for x in calls if str(x.get("role", "")).startswith("SOL")), "")) or None),
            sol_prompt_version=str(lineage["prompt_version"]) if lineage else None,
            sol_input_sha256=sol_input, sol_output_sha256=sol_output,
            sol_cost_usd=str(sol_row.get("cost_usd") or "0") if sol_row else "0",
            analytical_commit_sha=commit, snapshot_id=snapshot_id, snapshot_sha256=snapshot_sha,
            data_quality_flags=tuple(flags),
        ))
        setup = setup_map[ticker]
        lower, upper = _setup_bounds(setup)
        setup_bytes = canonical_json(setup)
        setup_observations.append(SetupObservation(
            # setups.json is the pre-research Setup cohort artifact.  A later
            # committed Sol Setup has a different identity and is referenced
            # independently by ExecutionObservation; never conflate them.
            run_id=run_id, ticker=ticker, setup_id=None,
            setup_sha256=hashlib.sha256(setup_bytes).hexdigest(), setup_type=str(setup["setup_type"]),
            setup_timestamp=str(setup.get("as_of") or ""), entry_condition=setup.get("entry_condition"),
            entry_level=_float(setup.get("entry_level")), entry_lower=lower, entry_upper=upper,
            stop=_float(setup.get("stop_level")), tp1=_float(setup.get("tp1")),
            tp2=_float(setup.get("tp2")), original_rr1=_float(setup.get("rr_tp1")),
            original_rr2=_float(setup.get("rr_tp2")), expiry=None,
        ))
        execution = sol_row.get("execution_status") if sol_row else None
        if isinstance(execution, dict):
            executions.append(ExecutionObservation(
                run_id=run_id, ticker=ticker, setup_id=str(execution["setup_id"]),
                setup_sha256=str(execution["setup_sha256"]),
                freshness_timestamp=str(execution["price_retrieved_at"]),
                current_price=_float(execution.get("current_price")),
                price_session=str(execution["price_as_of"]), price_provider=str(execution["price_provider"]),
                execution_status=str(execution["execution_status"]),
                current_rr1=_float(execution.get("current_rr1")), current_rr2=_float(execution.get("current_rr2")),
                reason_codes=tuple(str(x) for x in execution.get("reason_codes", [])),
                policy_version=str(execution["policy_version"]),
            ))
    return CollectedRun(
        run_id=run_id, source_root=str(base), source_manifest_sha256=manifest_sha,
        selections=tuple(selections), setups=tuple(setup_observations),
        executions=tuple(executions), total_api_cost_usd=str(usage.get("total_cost_usd") or "0"),
    )


def _float(value: object) -> float | None:
    return None if value is None else float(value)
