"""Manual, fail-closed recovery of one interrupted Sol Critic stage.

This module is deliberately not called by the weekly production workflow.  It
never acquires prices or research, never runs Luna, never sends Telegram, and
only permits VALIDATION source runs.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
from types import MappingProxyType
from typing import Any, Callable, Mapping

from trinity.italia_v1 import analyze_company, build_facts_v3
from trinity.ledger import LedgerStorage
from trinity.ledger import research_result_value, setup_result_value
from trinity.pilots.multiticker import (
    InvocationTrace,
    RecordingUSAProvider,
    _canonical_fact_numbers,
    _persist_completed_pipeline,
    _persist_inputs,
    _record_failure,
)
from trinity.shadow.atomic import atomic_json
from trinity.shadow.historical_reconciliation import (
    HISTORICAL_RUN_ID,
    HistoricalReconciliationError,
    historical_trust_record,
    verify_historical_reconciliation,
)
from trinity.shadow.manifest import generate_manifest, verify_manifest
from trinity.shadow.responses import (
    PersistentCostLedger,
    ResponsesAPI,
    _extract_output_json,
)
from trinity.usa_setup_v1 import SetupRecord
from trinity.usa_v2 import load_company, with_expectation_comparisons


RECOVERY_SCHEMA = "trinity.sol-recovery-plan"
RECOVERY_METHOD = "SOL_CRITIC_RECOVERY_V1"
CONFIRMATION = "VALIDATION:ONE_SOL_CRITIC:NO_TELEGRAM"
HISTORICAL_SETUP_PATH = "artifacts/setups.json"
FINALIZE_EXISTING_CONFIRMATION = "VALIDATION:OFFLINE_EXISTING_CRITIC:NO_NETWORK"
FINALIZE_EXISTING_METHOD = "SOL_EXISTING_CRITIC_FINALIZATION_V1"
FINALIZE_EXISTING_REQUESTED_BY = "trinity-sol-existing-critic-finalizer-v1"


class SolRecoveryError(RuntimeError):
    """A recovery invariant was not proven."""


@dataclass(frozen=True)
class ExistingCriticSpec:
    source_run_id: str
    recovery_run_id: str
    ticker: str
    failed_attempt_id: str
    response_id: str
    model: str
    api_usage_sha256: str
    reconciliation_sha256: str
    recovery_source_sha256: str
    ledger_sha256: str
    run_state_sha256: str
    analyst_file_sha256: str
    critic_file_sha256: str
    request_artifact_id: str
    success_artifact_id: str
    raw_output_artifact_id: str
    parsed_output_artifact_id: str
    effective_stage_artifact_id: str
    facts_artifact_id: str
    setup_file_sha256: str
    setup_record_sha256: str
    setup_code_blob: str
    research_cutoff_at: str
    original_analysis_reference_at: str
    critic_generated_at: str
    critic_cost_usd: str


@dataclass(frozen=True)
class ExistingCriticPlan:
    source_root: Path
    r2_root: Path
    spec: ExistingCriticSpec
    analyst_interaction_id: str
    critic_interaction_id: str
    analyst_started_at: str
    analyst_finished_at: str
    critic_started_at: str
    analyst_prompt_artifact_id: str
    critic_prompt_artifact_id: str
    source_failed_attempt_id: str
    old_research_id: str
    old_setup_id: str
    idempotency_key: str
    total_cost_usd: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": "trinity.sol-existing-critic-finalization-plan",
            "schema_version": "1",
            "method_version": FINALIZE_EXISTING_METHOD,
            "source_root": str(self.source_root),
            "r2_root": str(self.r2_root),
            **asdict(self.spec),
            "analyst_interaction_id": self.analyst_interaction_id,
            "critic_interaction_id": self.critic_interaction_id,
            "analyst_started_at": self.analyst_started_at,
            "analyst_finished_at": self.analyst_finished_at,
            "critic_started_at": self.critic_started_at,
            "analyst_prompt_artifact_id": self.analyst_prompt_artifact_id,
            "critic_prompt_artifact_id": self.critic_prompt_artifact_id,
            "source_failed_attempt_id": self.source_failed_attempt_id,
            "old_research_id": self.old_research_id,
            "old_setup_id": self.old_setup_id,
            "idempotency_key": self.idempotency_key,
            "total_cost_usd": self.total_cost_usd,
            "request_kind": "RECONSTRUCTION",
            "pit_class": "RECONSTRUCTED_NOT_ARCHIVED",
            "reconstruction": True,
            "archived_historical_decision": False,
            "mode": "VALIDATION",
            "official_benchmark_observation": False,
            "telegram_delivery": False,
            "broker_execution": False,
            "cloud_write": False,
            "network_enabled": False,
        }


_EXISTING_CRITIC_TRUST_REGISTRY = MappingProxyType({
    ("SHADOW_USA_2026-10-09_38230389276_A1", "SWKS"):
        ExistingCriticSpec(
            source_run_id="SHADOW_USA_2026-10-09_38230389276_A1",
            recovery_run_id="RECOVERY_SHADOW_USA_2026-10-09_38230389276_A1_SWKS_R2",
            ticker="SWKS",
            failed_attempt_id="474817be-ecfa-4bea-beab-9fe2df01b817",
            response_id="resp_049a7d460a02ea8c016ace3db1d28c87d2b31136f5b772754f",
            model="gpt-5.6-sol",
            api_usage_sha256="6e7c0f63360d6b6519264f0f45e91d8b5ac406533e4f947acdcb9e496d1ded2a",
            reconciliation_sha256="d58658403e12f4c6d1df8e8a7615a982f0938e8ac6a95aa335f33e2ef8a1f5b1",
            recovery_source_sha256="c62797fe8398e5b65abc7058e7d9a5250e572067ec32bb00f7e8c24a20b204b0",
            ledger_sha256="38ce76cec992f430af3548fc889e36fde7cd3b63c11251d414161a904e7d987d",
            run_state_sha256="06364b98054a825a6e164f70797f8a06dd8a091f256e47e2e43abd5fd86292ca",
            analyst_file_sha256="4b9d8ea6a870e9e04e1e669aedbd07531c95ab411bd50bd0adef4cd0bc42a0b9",
            critic_file_sha256="080d515a83459d22d7869d8915bba811a3cf15742ca88ed0455e6056824b5b1c",
            request_artifact_id="sha256:1f6bd52ddaef3f6a9215a3fdd40db1d7e4d7570531738cb379bc1ceebb1dcf19",
            success_artifact_id="sha256:f5c2670f2486a53c6ce808b8edeb38f681c453d68d1f604430c0de699f7abc8e",
            raw_output_artifact_id="sha256:6588916d011b0561a243f6e677cd9075fc36d159bc52d7f71bf3d14defc98370",
            parsed_output_artifact_id="sha256:0723800154131d6b77c3c0fd914291e19feaa47ad3faac9bc0ff42b395c21a8f",
            effective_stage_artifact_id="sha256:7a3e7ccd95f1bdacc8486d2c4a150dad1d90ffa64eff41cca4744117f96890a1",
            facts_artifact_id="sha256:35fd74ea4f3dcf1bfe963fffe54a89b168420c9a73297e11519779bcf911738f",
            setup_file_sha256="6e99dfce1f283372111a83ac1063c8a0246955d3781045e2589dd89b61c07314",
            setup_record_sha256="aff10333e904a54bb54eb9f5d69f630e3c9c2613c89b6b10e990233ce71680c4",
            setup_code_blob="4a9237076657106102e1a1361aba49c9e447e018",
            research_cutoff_at="2026-10-12T11:09:36.364663Z",
            original_analysis_reference_at="2026-10-12T11:43:00.974465Z",
            critic_generated_at="2026-10-13T14:19:31.462433Z",
            critic_cost_usd="0.218737",
        ),
})


@dataclass(frozen=True)
class SolRecoverySpec:
    source_run_id: str
    ticker: str
    committed_tickers: tuple[str, ...]
    analytical_commit: str
    workflow_commit: str
    snapshot_sha256: str
    research_cutoff_utc: str


@dataclass(frozen=True)
class SolRecoveryPlan:
    source_root: Path
    source_run_id: str
    ticker: str
    failed_attempt_id: str
    run_request_id: str
    analyst_raw_path: str
    analyst_raw_sha256: str
    analyst_response_id: str
    source_manifest_sha256: str
    ledger_sha256: str
    source_archive_artifact_id: str
    facts_artifact_id: str
    original_cost_usd: str
    snapshot_id: str
    snapshot_prefix: str
    snapshot_sha256: str
    baseline_prefix: str
    research_cutoff_utc: str
    technical_cutoff_utc: str
    analytical_commit: str
    workflow_commit: str
    historical_setup_path: str
    historical_setup_sha256: str
    historical_setup_record_sha256: str
    historical_setup_as_of: str
    historical_reconciliation_id: str | None = None
    historical_reconciliation_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_root"] = str(self.source_root)
        value.update({
            "schema_name": RECOVERY_SCHEMA,
            "schema_version": "1",
            "method_version": RECOVERY_METHOD,
            "mode": "VALIDATION",
            "official_benchmark_observation": False,
            "telegram_delivery": False,
            "broker_execution": False,
        })
        return value


def preflight_sol_recovery(
    source_root: str | Path, spec: SolRecoverySpec, *,
    allow_historical_reconciliation: bool = False,
    reconciliation_attestation: str | Path | None = None,
    reconciliation_sha256: str | None = None,
) -> SolRecoveryPlan:
    """Prove that exactly one archived Analyst may be followed by one Critic."""
    root = Path(source_root).resolve()
    if root.name != spec.source_run_id:
        raise SolRecoveryError("source directory does not match source_run_id")
    manifest_path = root / "manifest.json"
    manifest_raw = _required_bytes(manifest_path)
    manifest = _json_bytes(manifest_raw, "source manifest")
    if manifest.get("run_id") != spec.source_run_id:
        raise SolRecoveryError("source manifest run_id mismatch")
    try:
        verify_manifest(root, manifest)
    except (KeyError, TypeError, ValueError) as exc:
        raise SolRecoveryError(f"source manifest verification failed: {exc}") from exc
    manifest_entries = {
        str(item["path"]): str(item["sha256"]) for item in manifest.get("files", [])
    }
    _verify_checksums_file(root, manifest)
    reconciliation: dict[str, Any] | None = None
    reconciliation_arguments = reconciliation_attestation is not None or reconciliation_sha256 is not None
    if not allow_historical_reconciliation and reconciliation_arguments:
        raise SolRecoveryError("historical reconciliation is disabled")
    if allow_historical_reconciliation:
        if reconciliation_attestation is None or reconciliation_sha256 is None:
            raise SolRecoveryError("historical reconciliation requires an attestation and SHA-256")
        try:
            reconciliation = verify_historical_reconciliation(
                root, manifest, _required_bytes(Path(reconciliation_attestation)),
                expected_attestation_sha256=reconciliation_sha256,
            )
        except HistoricalReconciliationError as exc:
            raise SolRecoveryError(f"historical reconciliation failed: {exc}") from exc
        for relative in reconciliation["omission_paths"]:
            manifest_entries[relative] = hashlib.sha256(
                _required_bytes(_inside(root, relative))
            ).hexdigest()

    state = _required_json(root / "run_state.json")
    if state.get("run_id") != spec.source_run_id or state.get("state") != "FAILED_SOL":
        raise SolRecoveryError("source run is not the requested immutable FAILED_SOL run")
    weekly = _required_json(root / "artifacts" / "weekly_orchestrator.json")
    if weekly.get("mode") != "VALIDATION" or weekly.get("official_benchmark_observation") is not False:
        raise SolRecoveryError("Sol recovery is restricted to non-official VALIDATION runs")
    if weekly.get("analytical_commit") != spec.analytical_commit:
        raise SolRecoveryError("analytical commit mismatch")
    if weekly.get("workflow_registration_commit") != spec.workflow_commit:
        raise SolRecoveryError("workflow registration commit mismatch")
    if reconciliation is not None and (
        spec.analytical_commit != reconciliation["trusted_analytical_commit"]
        or spec.workflow_commit != reconciliation["trusted_workflow_commit"]
        or weekly.get("analytical_commit") != reconciliation["trusted_analytical_commit"]
        or weekly.get("workflow_registration_commit") != reconciliation["trusted_workflow_commit"]
    ):
        raise SolRecoveryError("historical commits are not authorized by the trust registry")

    cutoff = _required_json(root / "artifacts" / "research_cutoff.json")
    decision = _required_json(root / "artifacts" / "decision_cutoff.json")
    if cutoff.get("research_cutoff_utc") != spec.research_cutoff_utc:
        raise SolRecoveryError("research cutoff mismatch")
    if cutoff.get("captured_at_utc") != cutoff.get("research_cutoff_utc"):
        raise SolRecoveryError("research cutoff was not captured exactly once")
    if cutoff.get("technical_cutoff_utc") != decision.get("utc"):
        raise SolRecoveryError("technical and research provenance disagree")
    if cutoff.get("verified_price_session") != decision.get("session_date"):
        raise SolRecoveryError("verified price session mismatch")

    price = _required_json(root / "artifacts" / "price_snapshot_restore.json")
    if price.get("snapshot_sha256") != spec.snapshot_sha256:
        raise SolRecoveryError("price snapshot SHA-256 mismatch")
    if reconciliation is not None and (
        price.get("snapshot_id") != reconciliation["trusted_snapshot_id"]
        or price.get("snapshot_sha256") != reconciliation["trusted_snapshot_sha256"]
        or spec.snapshot_sha256 != reconciliation["trusted_snapshot_sha256"]
    ):
        raise SolRecoveryError("historical price snapshot is not authorized by the trust registry")
    if price.get("target_market_session") != cutoff.get("verified_price_session"):
        raise SolRecoveryError("price snapshot session mismatch")
    prefix = str(price.get("prefix") or "")
    if not prefix or "latest" in prefix.casefold():
        raise SolRecoveryError("price snapshot prefix must be explicit and immutable")
    baseline = _required_json(root / "artifacts" / "baseline_restore.json")
    baseline_prefix = str(baseline.get("prefix") or "")
    if not baseline_prefix or "version=" not in baseline_prefix:
        raise SolRecoveryError("baseline prefix is not immutable")

    luna = _required_json(root / "artifacts" / "luna_results.json")
    escalates = tuple(str(item) for item in luna.get("escalate_tickers", []))
    if spec.ticker not in escalates:
        raise SolRecoveryError("recovery ticker was not an original Luna ESCALATE")
    if int(luna.get("counts", {}).get("failed", -1)) != 0:
        raise SolRecoveryError("source Luna stage was not complete")
    if set(escalates) != set((*spec.committed_tickers, spec.ticker)):
        raise SolRecoveryError("original ESCALATE closure differs from recovery specification")

    frozen_setup = load_frozen_historical_setup(
        root, manifest, ticker=spec.ticker,
        as_of=str(cutoff.get("verified_price_session") or ""),
    )
    frozen_setup_record_sha = _setup_record_sha256(frozen_setup)

    usage = _required_json(root / "artifacts" / "api_usage.json")
    if usage.get("schema_name") != "trinity.shadow-api-usage":
        raise SolRecoveryError("invalid source API cost ledger")
    calls = list(usage.get("calls") or [])
    calculated = sum(
        (Decimal(str(item["calculated_cost_usd"])) for item in calls), Decimal("0")
    )
    if calculated != Decimal(str(usage.get("total_cost_usd"))):
        raise SolRecoveryError("source API cost ledger total is inconsistent")
    analyst_calls = [
        item for item in calls if item.get("ticker") == spec.ticker
        and item.get("role") == "ANALYST" and item.get("model") == "gpt-5.6-sol"
    ]
    critic_calls = [
        item for item in calls if item.get("ticker") == spec.ticker
        and item.get("role") == "CRITIC"
    ]
    if len(analyst_calls) != 1 or critic_calls:
        raise SolRecoveryError("expected exactly one completed Analyst and no Critic")
    analyst = analyst_calls[0]
    raw_relative = str(analyst.get("raw_path") or "")
    if raw_relative not in manifest_entries:
        raise SolRecoveryError("Analyst checkpoint is not bound to the source manifest")
    raw = _required_bytes(_inside(root, raw_relative))
    raw_sha = hashlib.sha256(raw).hexdigest()
    if raw_sha != analyst.get("raw_sha256") or raw_sha != manifest_entries[raw_relative]:
        raise SolRecoveryError("Analyst checkpoint SHA-256 mismatch")
    envelope = _json_bytes(raw, "Analyst checkpoint")
    response_id = str(envelope.get("id") or "")
    if not response_id or response_id != analyst.get("response_id"):
        raise SolRecoveryError("Analyst checkpoint response identity mismatch")
    try:
        parsed = _extract_output_json(envelope)
    except Exception as exc:
        raise SolRecoveryError(f"Analyst checkpoint is not parseable: {exc}") from exc
    _validate_analyst_shape(parsed)

    ledger_relative = "ledger/trinity.sqlite3"
    if ledger_relative not in manifest_entries:
        raise SolRecoveryError("Ledger is not bound to the source manifest")
    ledger_path = root / ledger_relative
    ledger_sha = hashlib.sha256(_required_bytes(ledger_path)).hexdigest()
    if ledger_sha != manifest_entries[ledger_relative]:
        raise SolRecoveryError("Ledger SHA-256 mismatch")
    with _open_source_ledger_read_only(ledger_path) as storage:
        if storage.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SolRecoveryError("source Ledger integrity_check failed")
        if storage.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SolRecoveryError("source Ledger foreign_key_check failed")
        request_id, failed_attempt = _verify_ledger_closure(storage, spec)
        facts_id = _single_artifact_for_attempt(
            storage, failed_attempt, "ledger.usa-v2-facts.v3", via_node=True,
        )
        source_archive_id = _single_artifact_for_attempt(
            storage, failed_attempt, "ledger.forward-source-archive.v1", via_node=False,
        )
        storage.get_artifact(facts_id)
        storage.get_artifact(source_archive_id)

    source_dir = root / "fresh_sources" / "trinity" / spec.ticker
    for relative in ("prices", "news", "sec"):
        if not (source_dir / relative).is_dir():
            raise SolRecoveryError(f"archived {relative} source directory is missing")
    source_files = [path for path in source_dir.rglob("*") if path.is_file()]
    if not source_files or any(path.relative_to(root).as_posix() not in manifest_entries
                               for path in source_files):
        raise SolRecoveryError("one or more archived ticker sources are not manifested")

    return SolRecoveryPlan(
        source_root=root, source_run_id=spec.source_run_id, ticker=spec.ticker,
        failed_attempt_id=failed_attempt, run_request_id=request_id,
        analyst_raw_path=raw_relative, analyst_raw_sha256=raw_sha,
        analyst_response_id=response_id,
        source_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        ledger_sha256=ledger_sha, source_archive_artifact_id=source_archive_id,
        facts_artifact_id=facts_id, original_cost_usd=str(calculated),
        snapshot_id=str(price["snapshot_id"]), snapshot_prefix=prefix,
        snapshot_sha256=str(price["snapshot_sha256"]), baseline_prefix=baseline_prefix,
        research_cutoff_utc=str(cutoff["research_cutoff_utc"]),
        technical_cutoff_utc=str(cutoff["technical_cutoff_utc"]),
        analytical_commit=spec.analytical_commit, workflow_commit=spec.workflow_commit,
        historical_setup_path=HISTORICAL_SETUP_PATH,
        historical_setup_sha256=manifest_entries[HISTORICAL_SETUP_PATH],
        historical_setup_record_sha256=frozen_setup_record_sha,
        historical_setup_as_of=frozen_setup.as_of,
        historical_reconciliation_id=(
            str(reconciliation["attestation_id"]) if reconciliation else None
        ),
        historical_reconciliation_sha256=(
            str(reconciliation["attestation_sha256"]) if reconciliation else None
        ),
    )


def preflight_finalize_existing(
    source_root: str | Path,
    r2_root: str | Path,
    spec: ExistingCriticSpec,
    *,
    repository_root: str | Path | None = None,
) -> ExistingCriticPlan:
    """Verify one frozen R2 Critic checkpoint without opening a write handle."""

    trusted = _EXISTING_CRITIC_TRUST_REGISTRY.get((spec.source_run_id, spec.ticker))
    if trusted is None or spec != trusted:
        raise SolRecoveryError("existing Critic specification is not authorized")
    source = Path(source_root).resolve()
    root = Path(r2_root).resolve()
    if source.name != spec.source_run_id or root.name != spec.recovery_run_id:
        raise SolRecoveryError("existing Critic source or recovery directory identity mismatch")

    required_hashes = {
        "artifacts/api_usage.json": spec.api_usage_sha256,
        "artifacts/historical_reconciliation.json": spec.reconciliation_sha256,
        "artifacts/recovery_source.json": spec.recovery_source_sha256,
        "ledger/trinity.sqlite3": spec.ledger_sha256,
        "model_responses/10001_SWKS_analyst.json": spec.analyst_file_sha256,
        "model_responses/10002_SWKS_critic.json": spec.critic_file_sha256,
        "run_state.json": spec.run_state_sha256,
    }
    allowed = set(required_hashes) | {
        "ledger/trinity.sqlite3-shm", "ledger/trinity.sqlite3-wal",
    }
    present = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*") if path.is_file()
    }
    extras = sorted(present - allowed)
    if extras:
        raise SolRecoveryError("unexpected existing Critic recovery files: " + ", ".join(extras))
    for relative, expected in required_hashes.items():
        actual = hashlib.sha256(_required_bytes(_inside(root, relative))).hexdigest()
        if actual != expected:
            raise SolRecoveryError(f"existing Critic file SHA-256 mismatch: {relative}")
    wal = root / "ledger" / "trinity.sqlite3-wal"
    if wal.exists() and wal.stat().st_size != 0:
        raise SolRecoveryError("existing Critic Ledger has an unapplied WAL")

    state = _required_json(root / "run_state.json")
    if (
        state.get("run_id") != spec.recovery_run_id
        or state.get("state") != "FAILED_SOL_RECOVERY"
        or state.get("error") != (
            "FinalizationConflict: required derivation availability exceeds analysis cutoff"
        )
    ):
        raise SolRecoveryError("existing Critic recovery state is not the authorized failure")
    recovery_source = _required_json(root / "artifacts" / "recovery_source.json")
    if (
        recovery_source.get("source_run_id") != spec.source_run_id
        or recovery_source.get("ticker") != spec.ticker
        or recovery_source.get("mode") != "VALIDATION"
        or recovery_source.get("official_benchmark_observation") is not False
        or recovery_source.get("telegram_delivery") is not False
        or recovery_source.get("broker_execution") is not False
        or recovery_source.get("facts_artifact_id") != spec.facts_artifact_id
        or recovery_source.get("research_cutoff_utc") != spec.research_cutoff_at
        or recovery_source.get("historical_setup_sha256") != spec.setup_file_sha256
        or recovery_source.get("historical_setup_record_sha256") != spec.setup_record_sha256
    ):
        raise SolRecoveryError("existing Critic recovery provenance mismatch")

    source_manifest = _required_json(source / "manifest.json")
    frozen_setup = load_frozen_historical_setup(
        source, source_manifest, ticker=spec.ticker, as_of="2026-10-09",
        expected_file_sha256=spec.setup_file_sha256,
        expected_record_sha256=spec.setup_record_sha256,
    )
    repo = (
        Path(repository_root).resolve() if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    if _git_blob_id(repo, "c0558e7c95a90d37baa9cf3fbcc09376ad59e5dd", "trinity/usa_setup_v1.py") != spec.setup_code_blob:
        raise SolRecoveryError("historical Setup code blob mismatch")

    usage = _required_json(root / "artifacts" / "api_usage.json")
    calls = list(usage.get("calls") or [])
    critics = [
        item for item in calls
        if item.get("ticker") == spec.ticker and item.get("role") == "CRITIC"
    ]
    if len(critics) != 1:
        raise SolRecoveryError("existing Critic cost ledger must contain exactly one Critic")
    critic_usage = critics[0]
    expected_usage = {
        "response_id": spec.response_id,
        "model": spec.model,
        "raw_path": "model_responses/10002_SWKS_critic.json",
        "raw_sha256": spec.critic_file_sha256,
        "input_tokens": 22048,
        "output_tokens": 5425,
        "reasoning_tokens": 1705,
        "total_tokens": 27473,
        "calculated_cost_usd": spec.critic_cost_usd,
    }
    if any(critic_usage.get(key) != value for key, value in expected_usage.items()):
        raise SolRecoveryError("existing Critic usage or cost mismatch")
    calculated = sum(
        (Decimal(str(item["calculated_cost_usd"])) for item in calls), Decimal("0")
    )
    if calculated != Decimal(str(usage.get("total_cost_usd"))):
        raise SolRecoveryError("existing Critic cumulative cost is inconsistent")

    analyst_envelope = _required_json(root / "model_responses" / "10001_SWKS_analyst.json")
    critic_envelope = _required_json(root / "model_responses" / "10002_SWKS_critic.json")
    if (
        critic_envelope.get("id") != spec.response_id
        or critic_envelope.get("model") != spec.model
        or critic_envelope.get("status") != "completed"
        or critic_envelope.get("error") is not None
    ):
        raise SolRecoveryError("existing Critic API envelope identity mismatch")
    analyst_value = _extract_output_json(analyst_envelope)
    critic_value = _extract_output_json(critic_envelope)
    if (
        critic_value.get("status") != "INVESTIGATE"
        or critic_value.get("evidence_confidence") != "HIGH"
        or critic_value.get("thesis_strength") != "MEDIUM"
    ):
        raise SolRecoveryError("existing Critic analytical result mismatch")

    ledger_path = root / "ledger" / "trinity.sqlite3"
    with _open_source_ledger_read_only(ledger_path) as storage:
        if storage.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SolRecoveryError("existing Critic Ledger integrity_check failed")
        if storage.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SolRecoveryError("existing Critic Ledger foreign_key_check failed")
        attempt = storage.connection.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (spec.failed_attempt_id,),
        ).fetchone()
        if (
            attempt is None or attempt["status"] != "FAILED"
            or attempt["failure_class"] != "FinalizationConflict"
            or attempt["failure_message"] != (
                "required derivation availability exceeds analysis cutoff"
            )
        ):
            raise SolRecoveryError("existing Critic failed attempt identity mismatch")
        request = storage.get_run_request(str(attempt["run_request_id"]))
        if (
            request.request_kind != "PIT_SAFE_DECISION"
            or request.analysis_cutoff_at != spec.original_analysis_reference_at
        ):
            raise SolRecoveryError("original analysis/PIT reference mismatch")

        critic_interaction = storage.connection.execute(
            "SELECT * FROM llm_interaction WHERE attempt_id=? AND ordinal=2",
            (spec.failed_attempt_id,),
        ).fetchall()
        analyst_interaction = storage.connection.execute(
            "SELECT * FROM llm_interaction WHERE attempt_id=? AND ordinal=1",
            (spec.failed_attempt_id,),
        ).fetchall()
        if len(critic_interaction) != 1 or len(analyst_interaction) != 1:
            raise SolRecoveryError("existing Analyst/Critic interaction closure is ambiguous")
        critic_row, analyst_row = critic_interaction[0], analyst_interaction[0]
        if (
            critic_row["llm_interaction_id"] != "eda7c566-70e7-46d8-a33e-93f22e8656a1"
            or critic_row["provider"] != "OPENAI_RESPONSES_API"
            or critic_row["model"] != spec.model
            or critic_row["status"] != "SUCCEEDED"
            or critic_row["request_artifact_id"] != spec.request_artifact_id
            or critic_row["response_artifact_id"] != spec.success_artifact_id
            or critic_row["finished_at"] != spec.critic_generated_at
        ):
            raise SolRecoveryError("existing Critic Ledger interaction mismatch")

        critic_request = json.loads(storage.get_artifact(spec.request_artifact_id).payload)
        critic_success = json.loads(storage.get_artifact(spec.success_artifact_id).payload)
        raw_value = json.loads(storage.get_artifact(spec.raw_output_artifact_id).payload)
        parsed_value = json.loads(storage.get_artifact(spec.parsed_output_artifact_id).payload)
        stage_value = json.loads(storage.get_artifact(spec.effective_stage_artifact_id).payload)
        if (
            critic_request.get("interaction_role") != "CRITIC"
            or critic_request.get("model") != spec.model
            or critic_success.get("llm_interaction_id") != critic_row["llm_interaction_id"]
            or critic_success.get("raw_response_artifact_id") != spec.raw_output_artifact_id
            or critic_success.get("parsed_response_artifact_id") != spec.parsed_output_artifact_id
            or critic_success.get("effective_stage_result_artifact_id") != spec.effective_stage_artifact_id
            or raw_value != critic_value
            or parsed_value.get("parsed_value") != critic_value
            or stage_value.get("result", {}).get("critic_status") != "INVESTIGATE"
            or stage_value.get("result", {}).get("evidence_confidence") != "HIGH"
            or stage_value.get("result", {}).get("thesis_strength") != "MEDIUM"
        ):
            raise SolRecoveryError("existing Critic artifact closure mismatch")

        old_research_id = "5a891cd4-49a5-4513-afda-e2d961f36590"
        old_setup_id = "a7594497-4ce5-4a01-8d5a-12a8a447c5c7"
        old_research = storage.get_research_record(old_research_id)
        old_setup = storage.get_setup(old_setup_id)
        if (
            old_research.attempt_id != spec.failed_attempt_id
            or old_research.run_id is not None
            or old_setup.attempt_id != spec.failed_attempt_id
            or old_setup.run_id is not None
        ):
            raise SolRecoveryError("failed existing Critic records were mutated")
        setup_payload = json.loads(storage.get_artifact(old_setup.result_artifact_id).payload)
        if setup_payload != setup_result_value(frozen_setup):
            raise SolRecoveryError("existing Critic frozen Setup differs")
        if storage.connection.execute(
            "SELECT count(*) FROM research_record rr JOIN run r ON rr.run_id=r.run_id "
            "WHERE rr.subject_key='ticker:SWKS' AND r.status='COMMITTED'"
        ).fetchone()[0] != 0:
            raise SolRecoveryError("SWKS already has a committed run")
        if storage.connection.execute(
            "SELECT count(*) FROM research_record rr JOIN run r ON rr.run_id=r.run_id "
            "WHERE rr.subject_key='ticker:MRNA' AND r.status='COMMITTED'"
        ).fetchone()[0] != 1:
            raise SolRecoveryError("MRNA committed run count changed")
        source_failed_attempt_id = str(recovery_source.get("failed_attempt_id") or "")
        if not source_failed_attempt_id:
            raise SolRecoveryError("source failed attempt provenance is missing")
        storage.get_artifact(spec.facts_artifact_id)

        analyst_request = json.loads(
            storage.get_artifact(str(analyst_row["request_artifact_id"])).payload
        )
        if analyst_request.get("interaction_role") != "ANALYST":
            raise SolRecoveryError("existing Analyst request identity mismatch")

    identity_value = {
        "method_version": FINALIZE_EXISTING_METHOD,
        "source_run_id": spec.source_run_id,
        "ticker": spec.ticker,
        "failed_attempt_id": spec.failed_attempt_id,
        "response_id": spec.response_id,
        "parsed_output_artifact_id": spec.parsed_output_artifact_id,
        "setup_record_sha256": spec.setup_record_sha256,
    }
    identity = hashlib.sha256(json.dumps(
        identity_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return ExistingCriticPlan(
        source_root=source,
        r2_root=root,
        spec=spec,
        analyst_interaction_id=str(analyst_row["llm_interaction_id"]),
        critic_interaction_id=str(critic_row["llm_interaction_id"]),
        analyst_started_at=str(analyst_row["started_at"]),
        analyst_finished_at=str(analyst_row["finished_at"]),
        critic_started_at=str(critic_row["started_at"]),
        analyst_prompt_artifact_id=str(analyst_request["client_effective_input_artifact_id"]),
        critic_prompt_artifact_id=str(critic_request["client_effective_input_artifact_id"]),
        source_failed_attempt_id=source_failed_attempt_id,
        old_research_id=old_research_id,
        old_setup_id=old_setup_id,
        idempotency_key=f"{FINALIZE_EXISTING_METHOD.lower()}:{identity}",
        total_cost_usd=str(usage["total_cost_usd"]),
    )


def _git_blob_id(repository_root: Path, commit: str, relative: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        windows_git = Path("C:/Program Files/Git/cmd/git.exe")
        executable = str(windows_git) if windows_git.is_file() else None
    if executable is None:
        raise SolRecoveryError("unable to locate Git for Setup blob verification")
    completed = subprocess.run(
        [executable, "rev-parse", f"{commit}:{relative}"], cwd=repository_root,
        check=False, capture_output=True, text=True,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) != 40:
        raise SolRecoveryError("unable to verify historical Setup code blob")
    return value


class ExistingCriticReplayProvider(RecordingUSAProvider):
    """Replay two verified values with their real R2 timestamps and no transport."""

    def __init__(
        self, *, analyst_value: Mapping[str, Any], critic_value: Mapping[str, Any],
        analyst_prompt: str, critic_prompt: str, plan: ExistingCriticPlan,
    ) -> None:
        super().__init__(requester=None)
        self._values = (deepcopy(dict(analyst_value)), deepcopy(dict(critic_value)))
        self._prompts = (analyst_prompt, critic_prompt)
        self._plan = plan
        self.prompt_sha256: dict[str, str] = {}

    def _request(self, prompt: str, schema: Mapping[str, object]) -> dict[str, object]:
        ordinal = len(self.invocations)
        if ordinal not in (0, 1):
            raise SolRecoveryError("offline finalizer attempted more than two replay stages")
        role = "ANALYST" if ordinal == 0 else "CRITIC"
        archived_prompt = self._prompts[ordinal]
        expected_prompt = (
            self._plan.analyst_prompt_artifact_id if ordinal == 0
            else self._plan.critic_prompt_artifact_id
        )
        if not archived_prompt or not expected_prompt.startswith("sha256:"):
            raise SolRecoveryError(f"offline {role} prompt artifact is invalid")
        started_at, finished_at = (
            (self._plan.analyst_started_at, self._plan.analyst_finished_at)
            if ordinal == 0 else
            (self._plan.critic_started_at, self._plan.spec.critic_generated_at)
        )
        provenance = {
            "provider": "OPENAI_RESPONSES_API",
            "transport": "OFFLINE_VERIFIED_R2_REPLAY",
            "model": self._plan.spec.model,
            "model_version": f"responses-api:{self._plan.spec.model}",
            "timeout_seconds": 0,
            "sandbox": "not-applicable",
            "skip_git_repo_check": False,
            "ignore_user_config": False,
        }
        self.provenance = provenance
        value = deepcopy(self._values[ordinal])
        self.prompt_sha256[role] = expected_prompt.removeprefix("sha256:")
        self.prompt_sha256[f"{role}_CURRENT_CODE"] = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        self.invocations.append(InvocationTrace(
            role=role, prompt=archived_prompt, schema=deepcopy(schema),
            parsed_value=deepcopy(value), started_at=started_at,
            finished_at=finished_at, provenance=deepcopy(provenance),
        ))
        return value


def finalize_existing(
    source_root: str | Path,
    r2_root: str | Path,
    spec: ExistingCriticSpec,
    *,
    confirmation: str,
    repository_root: str | Path | None = None,
) -> dict[str, Any]:
    """Finalize one trusted R2 checkpoint without constructing any API transport."""

    if confirmation != FINALIZE_EXISTING_CONFIRMATION:
        raise SolRecoveryError("explicit offline RECONSTRUCTION confirmation is required")
    result_path = (
        Path(r2_root).resolve() / "artifacts" / "offline_finalization_result.json"
    )
    if result_path.exists():
        raise SolRecoveryError("existing Critic checkpoint is already finalized")
    plan = preflight_finalize_existing(
        source_root, r2_root, spec, repository_root=repository_root,
    )

    analyst_value = _extract_output_json(_required_json(
        plan.r2_root / "model_responses" / "10001_SWKS_analyst.json"
    ))
    critic_value = _extract_output_json(_required_json(
        plan.r2_root / "model_responses" / "10002_SWKS_critic.json"
    ))
    source_manifest = _required_json(plan.source_root / "manifest.json")
    frozen_setup = load_frozen_historical_setup(
        plan.source_root, source_manifest, ticker=spec.ticker, as_of="2026-10-09",
        expected_file_sha256=spec.setup_file_sha256,
        expected_record_sha256=spec.setup_record_sha256,
    )
    repo = (
        Path(repository_root).resolve() if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    ledger_path = plan.r2_root / "ledger" / "trinity.sqlite3"
    attempt = None
    with LedgerStorage.open(ledger_path) as storage:
        existing = storage.connection.execute(
            "SELECT rr.run_request_id, r.run_id FROM run_request rr "
            "LEFT JOIN run r USING(run_request_id) "
            "WHERE rr.requested_by=? AND rr.idempotency_key=?",
            (FINALIZE_EXISTING_REQUESTED_BY, plan.idempotency_key),
        ).fetchall()
        if existing:
            raise SolRecoveryError("existing Critic finalization idempotency key already exists")
        request = storage.create_run_request(
            request_kind="RECONSTRUCTION",
            analysis_cutoff_at=spec.critic_generated_at,
            parameters={
                "historical_as_of_at": spec.original_analysis_reference_at,
            },
            idempotency_key=plan.idempotency_key,
            requested_by=FINALIZE_EXISTING_REQUESTED_BY,
            baseline_commit="c0558e7c95a90d37baa9cf3fbcc09376ad59e5dd",
        )
        attempt = storage.allocate_attempt(
            run_request_id=request.run_request_id,
            worker_identity="sol-existing-critic-finalizer:swks",
            code_commit="c0558e7c95a90d37baa9cf3fbcc09376ad59e5dd",
            environment_fingerprint="shadow:sol-existing-critic-finalizer:v1",
        )
        try:
            shared = _load_shared_definitions(
                storage, "c0558e7c95a90d37baa9cf3fbcc09376ad59e5dd",
            )
            stored_facts = json.loads(storage.get_artifact(spec.facts_artifact_id).payload)
            recovery_source = _required_json(
                plan.r2_root / "artifacts" / "recovery_source.json"
            )
            source_archive_id = str(recovery_source["source_archive_artifact_id"])
            source_archive = storage.get_artifact(source_archive_id).payload
            original_times = _source_times(storage, plan.source_failed_attempt_id)
            bars_path = (
                plan.source_root / "fresh_sources" / "trinity" / spec.ticker
                / "prices" / f"{spec.ticker}.json"
            )
            price_raw = _required_bytes(bars_path)
            bars = _json_bytes(price_raw, "archived OHLCV")
            if not isinstance(bars, list) or not bars:
                raise SolRecoveryError("archived OHLCV is invalid")
            as_of = str(bars[-1]["date"])
            pack, facts = _rebuild_archived_inputs(
                plan.source_root, spec.ticker, as_of,
                spec.research_cutoff_at[:10], original_times["price"], stored_facts,
                historical_reconciliation_id=str(
                    recovery_source["historical_reconciliation_id"]
                ),
            )
            persisted = _persist_inputs(
                storage, attempt, spec.ticker, pack, facts, bars, price_raw,
                shared["pit_policy"], as_of=spec.research_cutoff_at[:10],
                reference=spec.original_analysis_reference_at,
                research_source_bytes=source_archive,
                research_retrieved_at=original_times["research"],
                price_retrieved_at=original_times["price"],
                price_provider="TWELVE_DATA",
                price_provider_metadata={
                    "snapshot_id": recovery_source["snapshot_id"],
                    "snapshot_session": as_of,
                    "snapshot_sha256": recovery_source["snapshot_sha256"],
                },
                forward=True,
            )
            analyst_prompt = storage.get_artifact(
                plan.analyst_prompt_artifact_id
            ).payload.decode("utf-8")
            critic_prompt = storage.get_artifact(
                plan.critic_prompt_artifact_id
            ).payload.decode("utf-8")
            provider = ExistingCriticReplayProvider(
                analyst_value=analyst_value, critic_value=critic_value,
                analyst_prompt=analyst_prompt, critic_prompt=critic_prompt,
                plan=plan,
            )
            thesis = analyze_company(
                pack.company_input, spec.research_cutoff_at[:10], provider,
                facts_builder=lambda _value, _as_of: deepcopy(facts),
            )
            thesis = replace(
                thesis, **provider.revisions,
                claim_refs=provider.validated_claim_refs,
                rejected_claim_refs=provider.rejected_claim_refs,
            )
            old_research = storage.get_research_record(plan.old_research_id)
            if json.loads(storage.get_artifact(old_research.content_artifact_id).payload) != research_result_value(thesis):
                raise SolRecoveryError("offline replay changed the existing Critic research result")
            old_setup = storage.get_setup(plan.old_setup_id)
            if json.loads(storage.get_artifact(old_setup.result_artifact_id).payload) != setup_result_value(frozen_setup):
                raise SolRecoveryError("offline replay changed the frozen Setup")
            provenance = storage.insert_json_artifact(
                artifact_type="ledger.sol-existing-critic-finalization.v1",
                value={
                    **plan.to_dict(),
                    "finalization_requested_at": datetime.now(timezone.utc).isoformat(),
                },
                media_type="application/json",
            )
            storage.attach_attempt_artifact(
                attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
                artifact_id=provenance.artifact_id, role="DIAGNOSTIC",
            )
            identities = _persist_completed_pipeline(
                storage, repo, "c0558e7c95a90d37baa9cf3fbcc09376ad59e5dd",
                attempt, spec.ticker, thesis, frozen_setup, provider, persisted,
                shared["research_method"], shared["setup_policy"],
                reference=spec.original_analysis_reference_at,
                research_as_of_at=spec.research_cutoff_at,
                llm_post_reference_reconstruction=True,
                llm_reconstruction_support_artifact_ids=(
                    provenance.artifact_id,
                ),
            )
            run = storage.get_run(identities["run_id"])
            manifest = json.loads(
                storage.get_artifact(run.result_manifest_artifact_id).payload
            )
            if (
                manifest.get("request_kind") != "RECONSTRUCTION"
                or manifest.get("resolved_pit_class") != "RECONSTRUCTED_NOT_ARCHIVED"
                or manifest.get("pit_reference_at") != spec.original_analysis_reference_at
            ):
                raise SolRecoveryError("offline finalization classification mismatch")
            if storage.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SolRecoveryError("offline finalization Ledger integrity_check failed")
            if storage.connection.execute("PRAGMA foreign_key_check").fetchall():
                raise SolRecoveryError("offline finalization Ledger foreign_key_check failed")
        except Exception as exc:
            if storage.is_attempt_authorized(attempt.attempt_id, attempt.fence_token):
                _record_failure(storage, attempt, spec.ticker, exc)
            raise

    result = {
        **plan.to_dict(),
        "state": "COMPLETE_RECONSTRUCTION",
        "run_id": identities["run_id"],
        "attempt_id": attempt.attempt_id,
        "research_id": identities["research_id"],
        "setup_id": identities["setup_id"],
        "finalized_at": run.committed_at,
        "additional_openai_calls": 0,
        "additional_api_cost_usd": "0",
    }
    atomic_json(result_path, result)
    generate_manifest(plan.r2_root, run_id=plan.r2_root.name)
    return result


class RecoverySolProvider(RecordingUSAProvider):
    """Replay the manifested Analyst; delegate only the Critic to Responses API."""

    def __init__(
        self, transport: ResponsesAPI, *, ticker: str,
        analyst_value: Mapping[str, Any], source_run_id: str,
        analyst_sha256: str, critic_ordinal: int = 10_002,
    ) -> None:
        self.transport = transport
        self.ticker = ticker
        self.analyst_value = deepcopy(dict(analyst_value))
        self.source_run_id = source_run_id
        self.analyst_sha256 = analyst_sha256
        self.critic_ordinal = critic_ordinal
        self._ordinal = 0
        self.prompt_sha256: dict[str, str] = {}
        super().__init__(requester=self._invoke)

    def _invoke(self, prompt: str, schema: Mapping[str, object]) -> dict[str, object]:
        self._ordinal += 1
        role = "ANALYST" if self._ordinal == 1 else "CRITIC"
        self.prompt_sha256[role] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if role == "ANALYST":
            self.provenance = {
                "provider": "OPENAI_RESPONSES_API", "transport": "RESPONSES_API_HTTPS",
                "model": "gpt-5.6-sol", "model_version": "responses-api:gpt-5.6-sol",
                "timeout_seconds": 300, "sandbox": "not-applicable",
                "skip_git_repo_check": False, "ignore_user_config": False,
            }
            return deepcopy(self.analyst_value)
        if self._ordinal != 2:
            raise SolRecoveryError("recovery attempted more than Analyst plus Critic")
        self.provenance = {
            "provider": "OPENAI_RESPONSES_API", "transport": "RESPONSES_API_HTTPS",
            "model": "gpt-5.6-sol", "model_version": "responses-api:gpt-5.6-sol",
            "timeout_seconds": 300, "sandbox": "not-applicable",
            "skip_git_repo_check": False, "ignore_user_config": False,
        }
        return self.transport.invoke(
            model="gpt-5.6-sol", role="CRITIC", ticker=self.ticker, prompt=prompt,
            schema=schema, call_ordinal=self.critic_ordinal,
        ).parsed


def execute_sol_recovery(
    source_root: str | Path, recovery_root: str | Path, spec: SolRecoverySpec, *,
    confirmation: str,
    transport_factory: Callable[[Path, PersistentCostLedger], ResponsesAPI] | None = None,
    repository_root: str | Path | None = None,
    allow_historical_reconciliation: bool = False,
    reconciliation_attestation: str | Path | None = None,
    reconciliation_sha256: str | None = None,
) -> dict[str, Any]:
    """Create a separate recovery run and perform at most the missing Critic call."""
    if confirmation != CONFIRMATION:
        raise SolRecoveryError("explicit single-Critic VALIDATION confirmation is required")
    plan = preflight_sol_recovery(
        source_root, spec,
        allow_historical_reconciliation=allow_historical_reconciliation,
        reconciliation_attestation=reconciliation_attestation,
        reconciliation_sha256=reconciliation_sha256,
    )
    source_manifest_raw = _required_bytes(plan.source_root / "manifest.json")
    if hashlib.sha256(source_manifest_raw).hexdigest() != plan.source_manifest_sha256:
        raise SolRecoveryError("source manifest changed after recovery preflight")
    frozen_setup = load_frozen_historical_setup(
        plan.source_root, _json_bytes(source_manifest_raw, "source manifest"),
        ticker=spec.ticker, as_of=plan.historical_setup_as_of,
        expected_file_sha256=plan.historical_setup_sha256,
        expected_record_sha256=plan.historical_setup_record_sha256,
    )
    destination = Path(recovery_root).resolve()
    if destination.exists():
        raise SolRecoveryError("recovery destination already exists; refusing duplicate execution")
    destination.mkdir(parents=True, exist_ok=False)
    artifacts = destination / "artifacts"
    ledger_dir = destination / "ledger"
    model_dir = destination / "model_responses"
    artifacts.mkdir()
    ledger_dir.mkdir()
    model_dir.mkdir()
    source = plan.source_root
    shutil.copy2(source / "ledger" / "trinity.sqlite3", ledger_dir / "trinity.sqlite3")
    shutil.copy2(source / "artifacts" / "api_usage.json", artifacts / "api_usage.json")
    shutil.copy2(_inside(source, plan.analyst_raw_path), model_dir / Path(plan.analyst_raw_path).name)
    if plan.historical_reconciliation_sha256 is not None:
        if reconciliation_attestation is None:
            raise SolRecoveryError("verified reconciliation attestation path was lost")
        copied_attestation = artifacts / "historical_reconciliation.json"
        shutil.copy2(Path(reconciliation_attestation), copied_attestation)
        if hashlib.sha256(copied_attestation.read_bytes()).hexdigest() != \
                plan.historical_reconciliation_sha256:
            raise SolRecoveryError("copied reconciliation attestation SHA-256 mismatch")
    atomic_json(artifacts / "recovery_source.json", plan.to_dict())
    atomic_json(destination / "run_state.json", {
        "schema_name": "trinity.sol-recovery-state", "schema_version": "1",
        "run_id": destination.name, "state": "RUNNING", "error": None,
    })

    ledger_path = ledger_dir / "trinity.sqlite3"
    usage = PersistentCostLedger(artifacts / "api_usage.json")
    factory = transport_factory or (
        lambda root, cost: ResponsesAPI(run_root=root, cost_ledger=cost)
    )
    attempt = None
    try:
        analyst_envelope = _required_json(model_dir / Path(plan.analyst_raw_path).name)
        analyst_value = _extract_output_json(analyst_envelope)
        repo = (Path(repository_root).resolve() if repository_root is not None
                else Path(__file__).resolve().parents[2])
        with LedgerStorage.open(ledger_path) as storage:
            attempt = storage.allocate_attempt(
                run_request_id=plan.run_request_id,
                worker_identity=f"sol-recovery:{spec.ticker.lower()}",
                code_commit=spec.analytical_commit,
                environment_fingerprint="shadow:sol-critic-recovery:v1",
            )
            shared = _load_shared_definitions(storage, spec.analytical_commit)
            stored_facts = json.loads(storage.get_artifact(plan.facts_artifact_id).payload)
            source_archive = storage.get_artifact(plan.source_archive_artifact_id).payload
            request = storage.get_run_request(plan.run_request_id)
            original_times = _source_times(storage, plan.failed_attempt_id)
            bars_path = source / "fresh_sources" / "trinity" / spec.ticker / "prices" / f"{spec.ticker}.json"
            price_raw = _required_bytes(bars_path)
            bars = _json_bytes(price_raw, "archived OHLCV")
            if not isinstance(bars, list) or not bars:
                raise SolRecoveryError("archived OHLCV is invalid")
            as_of = str(bars[-1]["date"])
            pack, facts = _rebuild_archived_inputs(
                source, spec.ticker, as_of, plan.research_cutoff_utc[:10],
                original_times["price"], stored_facts,
                historical_reconciliation_id=plan.historical_reconciliation_id,
            )
            persisted = _persist_inputs(
                storage, attempt, spec.ticker, pack, facts, bars, price_raw,
                shared["pit_policy"], as_of=plan.research_cutoff_utc[:10],
                reference=request.analysis_cutoff_at,
                research_source_bytes=source_archive,
                research_retrieved_at=original_times["research"],
                price_retrieved_at=original_times["price"],
                price_provider="TWELVE_DATA",
                price_provider_metadata={
                    "snapshot_id": plan.snapshot_id,
                    "snapshot_session": as_of,
                    "snapshot_sha256": plan.snapshot_sha256,
                }, forward=True,
            )
            recovery_evidence = storage.insert_json_artifact(
                artifact_type="ledger.sol-recovery-provenance.v1",
                value=plan.to_dict(), media_type="application/json",
            )
            storage.attach_attempt_artifact(
                attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
                artifact_id=recovery_evidence.artifact_id, role="DIAGNOSTIC",
            )
            # No API client is constructed until every archived input has
            # reproduced the exact manifested fact packet.
            transport = factory(destination, usage)
            provider = RecoverySolProvider(
                transport, ticker=spec.ticker, analyst_value=analyst_value,
                source_run_id=spec.source_run_id,
                analyst_sha256=plan.analyst_raw_sha256,
            )
            thesis = analyze_company(
                pack.company_input, plan.research_cutoff_utc[:10], provider,
                facts_builder=lambda _value, _as_of: deepcopy(facts),
            )
            thesis = replace(
                thesis, **provider.revisions,
                claim_refs=provider.validated_claim_refs,
                rejected_claim_refs=provider.rejected_claim_refs,
            )
            identities = _persist_completed_pipeline(
                storage, repo, spec.analytical_commit, attempt, spec.ticker,
                thesis, frozen_setup, provider, persisted, shared["research_method"],
                shared["setup_policy"], reference=request.analysis_cutoff_at,
                research_as_of_at=plan.research_cutoff_utc,
            )
            if storage.connection.execute("PRAGMA foreign_key_check").fetchall():
                raise SolRecoveryError("recovery Ledger foreign_key_check failed")
            if storage.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SolRecoveryError("recovery Ledger integrity_check failed")
        final_usage = usage.read()
        result = {
            **plan.to_dict(), "recovery_run_id": destination.name,
            "state": "COMPLETE", "identities": identities,
            "reused_calls": [{"ticker": spec.ticker, "role": "ANALYST",
                              "raw_sha256": plan.analyst_raw_sha256}],
            "new_calls": [{"ticker": spec.ticker, "role": "CRITIC"}],
            "analyst_prompt_sha256": provider.prompt_sha256["ANALYST"],
            "critic_prompt_sha256": provider.prompt_sha256["CRITIC"],
            "original_cost_usd": plan.original_cost_usd,
            "final_cost_usd": str(final_usage["total_cost_usd"]),
        }
        atomic_json(artifacts / "recovery_result.json", result)
        atomic_json(destination / "run_state.json", {
            "schema_name": "trinity.sol-recovery-state", "schema_version": "1",
            "run_id": destination.name, "state": "COMPLETE", "error": None,
        })
        generate_manifest(destination, run_id=destination.name)
        return result
    except Exception as exc:
        if attempt is not None:
            with LedgerStorage.open(ledger_path) as storage:
                if storage.is_attempt_authorized(attempt.attempt_id, attempt.fence_token):
                    _record_failure(storage, attempt, spec.ticker, exc)
        atomic_json(destination / "run_state.json", {
            "schema_name": "trinity.sol-recovery-state", "schema_version": "1",
            "run_id": destination.name, "state": "FAILED_SOL_RECOVERY",
            "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
        })
        raise


def load_frozen_historical_setup(
    source_root: str | Path, manifest: Mapping[str, Any], *, ticker: str,
    as_of: str, expected_file_sha256: str | None = None,
    expected_record_sha256: str | None = None,
) -> SetupRecord:
    """Load one manifested Setup record from the authorized historical run."""
    root = Path(source_root).resolve()
    if root.name != HISTORICAL_RUN_ID or manifest.get("run_id") != HISTORICAL_RUN_ID:
        raise SolRecoveryError("frozen Setup source run is not authorized")
    entries = [
        item for item in manifest.get("files", [])
        if isinstance(item, Mapping) and item.get("path") == HISTORICAL_SETUP_PATH
    ]
    if len(entries) != 1:
        raise SolRecoveryError("historical setups artifact is not uniquely manifested")
    entry = entries[0]
    path = _inside(root, HISTORICAL_SETUP_PATH)
    raw = _required_bytes(path)
    actual_sha = hashlib.sha256(raw).hexdigest()
    if entry.get("bytes") != len(raw) or entry.get("sha256") != actual_sha:
        raise SolRecoveryError("historical setups artifact does not match source manifest")
    if expected_file_sha256 is not None and actual_sha != expected_file_sha256:
        raise SolRecoveryError("historical setups artifact identity changed after preflight")
    value = _json_bytes(raw, "historical setups artifact")
    if not isinstance(value, list):
        raise SolRecoveryError("historical setups artifact must be a JSON array")
    ticker_records = [
        item for item in value
        if isinstance(item, Mapping) and item.get("ticker") == ticker
    ]
    if len(ticker_records) != 1:
        raise SolRecoveryError("historical Setup ticker must have exactly one record")
    record = dict(ticker_records[0])
    if record.get("ticker") != ticker:
        raise SolRecoveryError("historical Setup ticker mismatch")
    if record.get("as_of") != as_of:
        raise SolRecoveryError("historical Setup as_of mismatch")
    expected_fields = {field.name for field in fields(SetupRecord)}
    if set(record) != expected_fields:
        raise SolRecoveryError("historical Setup schema mismatch")
    reason_codes = record.get("reason_codes")
    if not isinstance(reason_codes, list) or not all(
        isinstance(item, str) and item for item in reason_codes
    ):
        raise SolRecoveryError("historical Setup reason_codes are invalid")
    converted = {**record, "reason_codes": tuple(reason_codes)}
    try:
        setup = SetupRecord(**converted)
    except (TypeError, ValueError) as exc:
        raise SolRecoveryError(f"historical Setup is invalid: {exc}") from exc
    round_trip = setup.to_dict()
    round_trip["reason_codes"] = list(round_trip["reason_codes"])
    if round_trip != record:
        raise SolRecoveryError("historical Setup conversion is not lossless")
    record_sha = _setup_record_sha256(setup)
    if expected_record_sha256 is not None and record_sha != expected_record_sha256:
        raise SolRecoveryError("historical Setup record identity changed after preflight")
    return setup


def _setup_record_sha256(setup: SetupRecord) -> str:
    value = setup.to_dict()
    value["reason_codes"] = list(value["reason_codes"])
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _verify_ledger_closure(storage: LedgerStorage, spec: SolRecoverySpec) -> tuple[str, str]:
    rows = storage.connection.execute(
        "SELECT rr.run_request_id, rr.parameters_json, a.attempt_id, a.status, "
        "a.failure_class, a.failure_message, r.run_id "
        "FROM run_request rr JOIN attempt a USING(run_request_id) "
        "LEFT JOIN run r ON r.attempt_id=a.attempt_id ORDER BY a.attempt_ordinal"
    ).fetchall()
    by_ticker: dict[str, list[Any]] = {}
    for row in rows:
        ticker = str(json.loads(row["parameters_json"]).get("ticker") or "")
        by_ticker.setdefault(ticker, []).append(row)
    for ticker in spec.committed_tickers:
        matches = by_ticker.get(ticker, [])
        if len(matches) != 1 or matches[0]["status"] != "SUCCEEDED" or not matches[0]["run_id"]:
            raise SolRecoveryError(f"{ticker} is not uniquely committed in the source Ledger")
    target = by_ticker.get(spec.ticker, [])
    if len(target) != 1:
        raise SolRecoveryError("recovery ticker does not have exactly one source attempt")
    row = target[0]
    if row["status"] != "FAILED" or row["run_id"] is not None:
        raise SolRecoveryError("recovery ticker is already committed or not failed")
    if row["failure_class"] != "ResponsesTransportError" or "HTTP 429" not in str(row["failure_message"]):
        raise SolRecoveryError("source failure is not the expected Responses API HTTP 429")
    return str(row["run_request_id"]), str(row["attempt_id"])


def _load_shared_definitions(storage: LedgerStorage, code_commit: str) -> dict[str, Any]:
    """Reuse the immutable definitions already committed by the source run."""
    method = storage.connection.execute(
        "SELECT research_method_id FROM research_method "
        "WHERE method_version='USA_V2_FACTS_V3' AND code_commit=?", (code_commit,),
    ).fetchall()
    setup = storage.connection.execute(
        "SELECT setup_policy_id FROM setup_policy "
        "WHERE policy_version='USA_SETUP_V1' AND code_commit=?", (code_commit,),
    ).fetchall()
    pit = storage.connection.execute(
        "SELECT classification_policy_id FROM pit_classification_policy "
        "WHERE policy_kind='PIT_CLASSIFICATION' AND code_commit=?", (code_commit,),
    ).fetchall()
    if not (len(method) == len(setup) == len(pit) == 1):
        raise SolRecoveryError("source Ledger shared definitions are incomplete or ambiguous")
    return {
        "research_method": storage.get_research_method(str(method[0][0])),
        "setup_policy": storage.get_setup_policy(str(setup[0][0])),
        "pit_policy": storage.get_pit_classification_policy(str(pit[0][0])),
    }


def _rebuild_archived_inputs(
    source_root: Path, ticker: str, price_as_of: str, research_as_of: str,
    price_retrieved_at: str, stored_facts: Mapping[str, Any], *,
    historical_reconciliation_id: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Rebuild the exact fact packet from manifested files, never from a network."""
    ticker_root = source_root / "fresh_sources" / "trinity" / ticker
    luna = _required_json(source_root / "artifacts" / "luna_results.json")
    matches = [item for item in luna.get("results", []) if item.get("ticker") == ticker]
    continuity = tuple(matches[0].get("selected_evidence_ids", [])) if len(matches) == 1 else ()
    pack = load_company(
        ticker, price_as_of, prices_dir=ticker_root / "prices",
        news_dir=ticker_root / "news", documents_dir=ticker_root / "sec",
        forward=True, price_retrieved_at=price_retrieved_at,
        price_source="Twelve Data validated immutable OHLCV snapshot",
        news_source="EODHD live issuer/news API",
        continuity_evidence_ids=continuity, research_as_of=research_as_of,
        document_path_resolver=_archived_sec_path_resolver(
            source_root, ticker, historical_reconciliation_id,
        ),
    )
    native = build_facts_v3(
        with_expectation_comparisons(pack.company_input), research_as_of,
    )
    # Absolute runner paths are non-semantic but were part of the original fact
    # packet. Restore those exact strings before proving byte-equivalent facts.
    stored_evidence = {
        str(item.get("identifier")): item for item in stored_facts.get("evidence", [])
    }
    for item in native.get("evidence", []):
        original = stored_evidence.get(str(item.get("identifier")))
        if original is not None and "raw_path" in original:
            item["raw_path"] = original["raw_path"]
    try:
        native["price"]["acquisition"]["raw_path"] = stored_facts["price"]["acquisition"]["raw_path"]
    except (KeyError, TypeError):
        pass
    if _canonical_fact_numbers(native) != stored_facts:
        raise SolRecoveryError(
            "archived sources do not reconstruct the manifested Analyst fact packet"
        )
    return pack, native


def _archived_sec_path_resolver(
    source_root: Path, ticker: str, reconciliation_id: str | None,
) -> Callable[[str, str], Path] | None:
    """Authorize one exact historical SEC path namespace; never a fallback."""
    if reconciliation_id is None:
        return None
    if source_root.name != HISTORICAL_RUN_ID:
        raise SolRecoveryError("archived SEC relocation is not authorized for this run")
    try:
        trusted = historical_trust_record(source_root.name)
    except HistoricalReconciliationError as exc:
        raise SolRecoveryError("archived SEC relocation run is not trusted") from exc
    if reconciliation_id != trusted.attestation_id:
        raise SolRecoveryError("archived SEC relocation attestation is not authorized")

    verified_root = source_root.resolve()
    sec_root = (verified_root / "fresh_sources" / "trinity" / ticker / "sec").resolve()
    _require_inside(verified_root, sec_root, "archived SEC root")
    anchor = (source_root.name, "fresh_sources", "trinity", ticker, "sec")
    historical_prefix = (
        "/", "home", "runner", "work", "_temp", "trinity-shadow", *anchor,
    )

    def resolve(original_local_path: str, expected_sha256: str) -> Path:
        pure = PurePosixPath(original_local_path)
        if (
            not pure.is_absolute() or "\\" in original_local_path
            or ".." in pure.parts or original_local_path != pure.as_posix()
        ):
            raise SolRecoveryError("unsafe archived SEC local_path")
        parts = pure.parts
        positions = [
            index for index in range(len(parts) - len(anchor) + 1)
            if tuple(parts[index:index + len(anchor)]) == anchor
        ]
        if len(positions) != 1:
            raise SolRecoveryError("archived SEC local_path mapping is missing or ambiguous")
        if tuple(parts[:len(historical_prefix)]) != historical_prefix:
            raise SolRecoveryError("archived SEC local_path namespace is not authorized")
        suffix = parts[positions[0] + len(anchor):]
        if not suffix:
            raise SolRecoveryError("archived SEC local_path has no document suffix")
        candidate = sec_root.joinpath(*suffix).resolve()
        _require_inside(verified_root, candidate, "relocated SEC document")
        _require_inside(sec_root, candidate, "relocated SEC document")
        if not candidate.is_file():
            raise SolRecoveryError("relocated SEC document is missing")
        if not _valid_sha256(expected_sha256):
            raise SolRecoveryError("archived SEC document SHA-256 is invalid")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected_sha256:
            raise SolRecoveryError("relocated SEC document SHA-256 mismatch")
        return candidate

    return resolve


def _require_inside(root: Path, candidate: Path, label: str) -> None:
    if root != candidate and root not in candidate.parents:
        raise SolRecoveryError(f"{label} escapes verified source root")


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _single_artifact_for_attempt(
    storage: LedgerStorage, attempt_id: str, kind: str, *, via_node: bool,
) -> str:
    if via_node:
        rows = storage.connection.execute(
            "SELECT DISTINCT a.artifact_id FROM artifact a "
            "JOIN derivation_node d ON d.entity_id=a.artifact_id "
            "WHERE d.attempt_id=? AND d.entity_type='artifact' AND a.artifact_kind=?",
            (attempt_id, kind),
        ).fetchall()
    else:
        rows = storage.connection.execute(
            "SELECT DISTINCT a.artifact_id FROM artifact a "
            "JOIN input_observation io ON io.artifact_id=a.artifact_id "
            "WHERE io.observed_by_attempt_id=? AND a.artifact_kind=?",
            (attempt_id, kind),
        ).fetchall()
    if len(rows) != 1:
        raise SolRecoveryError(f"expected one verified {kind} artifact, found {len(rows)}")
    return str(rows[0][0])


def _source_times(storage: LedgerStorage, attempt_id: str) -> dict[str, str]:
    rows = storage.connection.execute(
        "SELECT source_record_key, retrieved_at FROM input_observation "
        "WHERE observed_by_attempt_id=?", (attempt_id,),
    ).fetchall()
    values = {str(row["source_record_key"]): str(row["retrieved_at"]) for row in rows}
    try:
        return {"research": values["research-source-bundle"], "price": values["ohlcv"]}
    except KeyError as exc:
        raise SolRecoveryError("source observation timestamps are incomplete") from exc


def _validate_analyst_shape(value: Mapping[str, Any]) -> None:
    required = {
        "fundamental_analysis", "earnings_and_news_analysis", "price_context",
        "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation",
        "proposed_status", "evidence_confidence", "thesis_strength", "event_assessments",
    }
    missing = sorted(required - set(value))
    if missing:
        raise SolRecoveryError("Analyst checkpoint lacks required fields: " + ", ".join(missing))


def _verify_checksums_file(root: Path, manifest: Mapping[str, Any]) -> None:
    expected = "".join(
        f"{item['sha256']}  {item['path']}\n" for item in manifest.get("files", [])
    ).encode("utf-8")
    if _required_bytes(root / "checksums.sha256") != expected:
        raise SolRecoveryError("checksums.sha256 differs from the source manifest")


def _open_source_ledger_read_only(path: Path) -> LedgerStorage:
    """Open immutable source evidence without migrations or journal creation."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return LedgerStorage(connection, path.resolve())


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if root != candidate and root not in candidate.parents:
        raise SolRecoveryError("manifest path escapes source root")
    return candidate


def _required_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise SolRecoveryError(f"required archived file is missing: {path.name}")
    return path.read_bytes()


def _json_bytes(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SolRecoveryError(f"{label} is not valid JSON") from exc


def _required_json(path: Path) -> Any:
    return _json_bytes(_required_bytes(path), path.name)


def _spec_from_args(args: argparse.Namespace) -> SolRecoverySpec:
    return SolRecoverySpec(
        source_run_id=args.source_run_id, ticker=args.ticker,
        committed_tickers=tuple(args.committed_ticker),
        analytical_commit=args.analytical_commit,
        workflow_commit=args.workflow_commit,
        snapshot_sha256=args.snapshot_sha256,
        research_cutoff_utc=args.research_cutoff_utc,
    )


def _existing_spec_from_args(args: argparse.Namespace) -> ExistingCriticSpec:
    return ExistingCriticSpec(
        source_run_id=args.source_run_id,
        recovery_run_id=args.recovery_run_id,
        ticker=args.ticker,
        failed_attempt_id=args.failed_attempt_id,
        response_id=args.expected_response_id,
        model=args.expected_model,
        api_usage_sha256=args.expected_api_usage_sha256,
        reconciliation_sha256=args.expected_reconciliation_sha256,
        recovery_source_sha256=args.expected_recovery_source_sha256,
        ledger_sha256=args.expected_ledger_sha256,
        run_state_sha256=args.expected_run_state_sha256,
        analyst_file_sha256=args.expected_analyst_file_sha256,
        critic_file_sha256=args.expected_critic_file_sha256,
        request_artifact_id=args.expected_request_artifact_id,
        success_artifact_id=args.expected_success_artifact_id,
        raw_output_artifact_id=args.expected_raw_output_artifact_id,
        parsed_output_artifact_id=args.expected_parsed_output_artifact_id,
        effective_stage_artifact_id=args.expected_effective_stage_artifact_id,
        facts_artifact_id=args.expected_facts_artifact_id,
        setup_file_sha256=args.expected_setup_file_sha256,
        setup_record_sha256=args.expected_setup_record_sha256,
        setup_code_blob=args.expected_setup_code_blob,
        research_cutoff_at=args.research_cutoff_at,
        original_analysis_reference_at=args.original_analysis_reference_at,
        critic_generated_at=args.critic_generated_at,
        critic_cost_usd=args.expected_critic_cost_usd,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "execute"):
        child = subparsers.add_parser(command)
        child.add_argument("--source-root", type=Path, required=True)
        child.add_argument("--source-run-id", required=True)
        child.add_argument("--ticker", required=True)
        child.add_argument("--committed-ticker", action="append", required=True)
        child.add_argument("--analytical-commit", required=True)
        child.add_argument("--workflow-commit", required=True)
        child.add_argument("--snapshot-sha256", required=True)
        child.add_argument("--research-cutoff-utc", required=True)
        child.add_argument("--allow-historical-reconciliation", action="store_true")
        child.add_argument("--reconciliation-attestation", type=Path)
        child.add_argument("--reconciliation-sha256")
    execute = subparsers.choices["execute"]
    execute.add_argument("--recovery-root", type=Path, required=True)
    execute.add_argument("--confirm", required=True)
    for command in ("preflight-finalize-existing", "finalize-existing"):
        child = subparsers.add_parser(command)
        child.add_argument("--source-root", type=Path, required=True)
        child.add_argument("--r2-root", type=Path, required=True)
        child.add_argument("--source-run-id", required=True)
        child.add_argument("--recovery-run-id", required=True)
        child.add_argument("--ticker", required=True)
        child.add_argument("--failed-attempt-id", required=True)
        child.add_argument("--expected-response-id", required=True)
        child.add_argument("--expected-model", required=True)
        child.add_argument("--expected-api-usage-sha256", required=True)
        child.add_argument("--expected-reconciliation-sha256", required=True)
        child.add_argument("--expected-recovery-source-sha256", required=True)
        child.add_argument("--expected-ledger-sha256", required=True)
        child.add_argument("--expected-run-state-sha256", required=True)
        child.add_argument("--expected-analyst-file-sha256", required=True)
        child.add_argument("--expected-critic-file-sha256", required=True)
        child.add_argument("--expected-request-artifact-id", required=True)
        child.add_argument("--expected-success-artifact-id", required=True)
        child.add_argument("--expected-raw-output-artifact-id", required=True)
        child.add_argument("--expected-parsed-output-artifact-id", required=True)
        child.add_argument("--expected-effective-stage-artifact-id", required=True)
        child.add_argument("--expected-facts-artifact-id", required=True)
        child.add_argument("--expected-setup-file-sha256", required=True)
        child.add_argument("--expected-setup-record-sha256", required=True)
        child.add_argument("--expected-setup-code-blob", required=True)
        child.add_argument("--research-cutoff-at", required=True)
        child.add_argument("--original-analysis-reference-at", required=True)
        child.add_argument("--critic-generated-at", required=True)
        child.add_argument("--expected-critic-cost-usd", required=True)
    subparsers.choices["finalize-existing"].add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.command in {"preflight-finalize-existing", "finalize-existing"}:
        existing_spec = _existing_spec_from_args(args)
        if args.command == "preflight-finalize-existing":
            print(json.dumps(preflight_finalize_existing(
                args.source_root, args.r2_root, existing_spec,
            ).to_dict(), indent=2, sort_keys=True))
            return 0
        result = finalize_existing(
            args.source_root, args.r2_root, existing_spec,
            confirmation=args.confirm,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    spec = _spec_from_args(args)
    if args.command == "preflight":
        print(json.dumps(preflight_sol_recovery(
            args.source_root, spec,
            allow_historical_reconciliation=args.allow_historical_reconciliation,
            reconciliation_attestation=args.reconciliation_attestation,
            reconciliation_sha256=args.reconciliation_sha256,
        ).to_dict(), indent=2, sort_keys=True))
        return 0
    result = execute_sol_recovery(
        args.source_root, args.recovery_root, spec, confirmation=args.confirm,
        allow_historical_reconciliation=args.allow_historical_reconciliation,
        reconciliation_attestation=args.reconciliation_attestation,
        reconciliation_sha256=args.reconciliation_sha256,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
