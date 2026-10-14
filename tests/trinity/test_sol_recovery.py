from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
from types import MappingProxyType, SimpleNamespace

import pytest

from trinity.ledger import LedgerStorage
from trinity.ledger.errors import ArtifactIntegrityError
from trinity.italia_v1 import build_facts_v3
from trinity.pilots.forward import run_forward_pilot
from trinity.pilots.jnj_telegram import _role_values
from trinity.pilots.multiticker import RecordingUSAProvider
from trinity.shadow.manifest import generate_manifest
from trinity.shadow.historical_reconciliation import (
    HISTORICAL_GITHUB_ARTIFACT_ID,
    HistoricalTrustRecord,
    HistoricalReconciliationError,
    attestation_bytes,
    verify_historical_reconciliation,
)
import trinity.shadow.historical_reconciliation as historical_reconciliation
from trinity.shadow.responses import (
    PersistentCostLedger, ResponsesAPI, ResponsesTransportError,
)
from trinity.shadow.sol_recovery import (
    CONFIRMATION, FINALIZE_EXISTING_CONFIRMATION,
    ExistingCriticSpec, SolRecoveryError, SolRecoverySpec,
    _EXISTING_CRITIC_TRUST_REGISTRY,
    _archived_sec_path_resolver, _require_inside,
    execute_sol_recovery, finalize_existing, load_frozen_historical_setup,
    preflight_finalize_existing, preflight_sol_recovery,
)
from trinity.usa_setup_v1 import (
    PRICE_CACHE, THESIS_CACHE, _load_latest_thesis, build_setup,
)
from trinity.usa_documents import load_document_coverage
from trinity.usa_v2 import AS_OF
from trinity.usa_v2 import with_expectation_comparisons

from tests.trinity.test_forward_pilot import _FixtureSource, _llm


RUN_ID = "SHADOW_USA_2026-10-09_38230389276_A1"
ANALYTICAL = "c0558e7c95a90d37baa9cf3fbcc09376ad59e5dd"
WORKFLOW = "b550a921d4f7bb02fe3cd196422e95f3d583bac6"
SNAPSHOT_SHA = "a" * 64
RESEARCH_CUTOFF = "2026-09-12T10:00:00.000000Z"


class _FailAfterAnalyst(RecordingUSAProvider):
    def __init__(self, ticker: str):
        thesis = _load_latest_thesis(ticker, AS_OF, THESIS_CACHE)
        analyst, _critic, _ = _role_values(thesis)
        calls = 0

        def request(_prompt, _schema):
            nonlocal calls
            calls += 1
            if calls == 1:
                return analyst
            raise ResponsesTransportError("Responses API HTTP 429")

        super().__init__(request)


def _envelope(value, response_id="resp_analyst"):
    return {
        "id": response_id,
        "output": [{"content": [{
            "type": "output_text",
            "text": json.dumps(value, ensure_ascii=False, sort_keys=True),
        }]}],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def recovery_source(tmp_path, monkeypatch):
    monkeypatch.setattr("trinity.pilots.forward._git_head", lambda _root: ANALYTICAL)
    root = tmp_path / RUN_ID
    (root / "ledger").mkdir(parents=True)
    database = root / "ledger" / "trinity.sqlite3"
    batch = run_forward_pilot(
        database, ["AAPL", "JNJ"], dry_run=True,
        source_provider_factory=lambda _root: _FixtureSource(),
        llm_provider_factory=lambda ticker: (
            _FailAfterAnalyst(ticker) if ticker == "JNJ" else _llm(ticker)
        ),
    )
    assert batch.results[0].run_status in {"SUCCESS", "NO_SETUP"}
    assert batch.results[1].run_status == "FAILED"

    artifacts = root / "artifacts"
    artifacts.mkdir()
    (root / "model_responses").mkdir()
    analyst, _critic, _ = _role_values(_load_latest_thesis("JNJ", AS_OF, THESIS_CACHE))
    raw_path = root / "model_responses" / "10001_JNJ_analyst.json"
    raw_path.write_text(json.dumps(_envelope(analyst)), encoding="utf-8")
    raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    usage = {
        "schema_name": "trinity.shadow-api-usage", "schema_version": "1",
        "pricing_version": "fixture", "hard_limit_usd": "5.00",
        "total_cost_usd": "0.1",
        "calls": [{
            "provider": "OPENAI_RESPONSES_API", "model": "gpt-5.6-sol",
            "role": "ANALYST", "ticker": "JNJ", "response_id": "resp_analyst",
            "calculated_cost_usd": "0.1", "raw_sha256": raw_sha,
            "raw_path": "model_responses/10001_JNJ_analyst.json",
        }],
    }
    (artifacts / "api_usage.json").write_text(json.dumps(usage), encoding="utf-8")
    (artifacts / "weekly_orchestrator.json").write_text(json.dumps({
        "mode": "VALIDATION", "official_benchmark_observation": False,
        "analytical_commit": ANALYTICAL,
        "workflow_registration_commit": WORKFLOW,
    }), encoding="utf-8")
    (artifacts / "research_cutoff.json").write_text(json.dumps({
        "captured_at_utc": RESEARCH_CUTOFF,
        "research_cutoff_utc": RESEARCH_CUTOFF,
        "technical_cutoff_utc": "2026-09-12T20:00:00Z",
        "verified_price_session": AS_OF,
    }), encoding="utf-8")
    (artifacts / "decision_cutoff.json").write_text(json.dumps({
        "utc": "2026-09-12T20:00:00Z", "session_date": AS_OF,
    }), encoding="utf-8")
    (artifacts / "price_snapshot_restore.json").write_text(json.dumps({
        "prefix": "prices/twelvedata/session=2026-09-12/run_id=fixture-A1/",
        "snapshot_id": "TWELVEDATA_2026-09-12_RUN_fixture-A1",
        "snapshot_sha256": SNAPSHOT_SHA, "target_market_session": AS_OF,
    }), encoding="utf-8")
    (artifacts / "baseline_restore.json").write_text(json.dumps({
        "prefix": "baseline/shadow-production-v1/version=" + "d" * 64 + "/",
        "file_count": 1, "total_bytes": 1,
    }), encoding="utf-8")
    (artifacts / "luna_results.json").write_text(json.dumps({
        "counts": {"failed": 0}, "escalate_tickers": ["AAPL", "JNJ"],
    }), encoding="utf-8")
    archived = _FixtureSource().acquire("JNJ")
    frozen_setup = build_setup(
        _load_latest_thesis("JNJ", AS_OF, THESIS_CACHE),
        archived.bars, AS_OF,
    )
    (artifacts / "setups.json").write_text(
        json.dumps([frozen_setup.to_dict()]), encoding="utf-8",
    )
    (root / "run_state.json").write_text(json.dumps({
        "run_id": RUN_ID, "state": "FAILED_SOL",
    }), encoding="utf-8")

    ticker_root = root / "fresh_sources" / "trinity" / "JNJ"
    (ticker_root / "prices").mkdir(parents=True)
    (ticker_root / "news").mkdir()
    (ticker_root / "sec").mkdir()
    (ticker_root / "prices" / "JNJ.json").write_bytes((PRICE_CACHE / "JNJ.json").read_bytes())
    (ticker_root / "news" / "archived.json").write_text("{}", encoding="utf-8")
    (ticker_root / "sec" / "archived.json").write_text("{}", encoding="utf-8")
    generate_manifest(root, run_id=RUN_ID)
    spec = SolRecoverySpec(
        source_run_id=RUN_ID, ticker="JNJ", committed_tickers=("AAPL",),
        analytical_commit=ANALYTICAL, workflow_commit=WORKFLOW,
        snapshot_sha256=SNAPSHOT_SHA, research_cutoff_utc=RESEARCH_CUTOFF,
    )
    return root, spec


def _identity(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    return {
        "path": path.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "status": "VERIFIED",
    }


def _r2(path: str, identity: dict[str, object], last_modified: str) -> dict[str, object]:
    return {
        "checksum_sha256": "UNKNOWN",
        "content_length": identity["bytes"],
        "key": (
            f"runs/shadow/run_id={RUN_ID}/" +
            ("ledger/" if path == "ledger/trinity.sqlite3" else "manifests/") + path
        ),
        "last_modified": last_modified,
        "metadata_sha256": identity["sha256"],
        "object_lock_legal_hold": "UNKNOWN",
        "object_lock_mode": "UNKNOWN",
        "object_lock_retain_until": "UNKNOWN",
        "status": "VERIFIED",
        "version_id": "UNKNOWN",
    }


def _historical_statement(root: Path) -> dict[str, object]:
    manifest = _identity(root / "manifest.json")
    manifest["path"] = "manifest.json"
    manifest["r2"] = _r2("manifest.json", manifest, "2026-10-12T11:14:49+00:00")
    ledger = _identity(root / "ledger" / "trinity.sqlite3")
    ledger["path"] = "ledger/trinity.sqlite3"
    ledger["r2"] = _r2(
        "ledger/trinity.sqlite3", ledger, "2026-10-12T11:14:36+00:00",
    )
    omissions = []
    for ticker, modified in (("MRNA", "2026-10-12T11:14:27+00:00"),
                             ("SWKS", "2026-10-12T11:14:34+00:00")):
        relative = f"fresh_sources/trinity/{ticker}/sec/manifest.json"
        item = _identity(root / Path(relative))
        item["path"] = relative
        item["r2"] = _r2(relative, item, modified)
        omissions.append(item)
    controls = []
    for relative in ("checksums.sha256", "r2_archive_result.json"):
        item = _identity(root / relative)
        item["path"] = relative
        controls.append(item)
    return {
        "analytical_commit": ANALYTICAL,
        "control_files": controls,
        "defect": {
            "classification": "GENERATOR_PATH_NAME_FILTER_DEFECT",
            "component": "trinity/shadow/manifest.py:eligible_files",
            "historical_predicate": "path.name not in {'manifest.json', 'checksums.sha256'}",
            "impact": "all nested files with either basename were omitted from the Shadow inventory",
        },
        "github_artifact": {
            "artifact_id": HISTORICAL_GITHUB_ARTIFACT_ID,
            "created_at": "2026-10-12T11:15:10Z",
            "digest": "sha256:834bf4de257eb1064bd3c335307a2c113760efa248fe271cbe300f426a69101f",
            "head_sha": WORKFLOW,
            "name": "trinity-weekly-shadow-38230389276-attempt-1",
            "size_in_bytes": 7327288,
            "status": "VERIFIED",
            "workflow_run_id": 38230389276,
        },
        "method_version": "SHADOW_MANIFEST_PATH_NAME_RECONCILIATION_V1",
        "mode": "VALIDATION",
        "official_benchmark_observation": False,
        "omissions": omissions,
        "original_shadow": {"ledger": ledger, "manifest": manifest},
        "source_run_id": RUN_ID,
        "unknown_metadata": [
            "r2.checksum_sha256", "r2.version_id", "r2.object_lock_mode",
            "r2.object_lock_retain_until", "r2.object_lock_legal_hold",
        ],
        "workflow_commit": WORKFLOW,
    }


@pytest.fixture
def historical_source(recovery_source, tmp_path, monkeypatch):
    root, spec = recovery_source
    for ticker in ("MRNA", "SWKS"):
        path = root / "fresh_sources" / "trinity" / ticker / "sec" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ticker": ticker, "source": "SEC EDGAR"}), encoding="utf-8")
    (root / "r2_archive_result.json").write_text(
        json.dumps({"object_count": 0, "total_bytes": 0}) + "\n", encoding="utf-8",
    )
    statement = _historical_statement(root)
    raw = attestation_bytes(statement)
    path = tmp_path / "historical-reconciliation.json"
    path.write_bytes(raw)
    manifest_sha = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    ledger_sha = hashlib.sha256((root / "ledger" / "trinity.sqlite3").read_bytes()).hexdigest()
    monkeypatch.setattr(
        historical_reconciliation, "_HISTORICAL_TRUST_REGISTRY",
        MappingProxyType({
            RUN_ID: HistoricalTrustRecord(
                source_run_id=RUN_ID,
                attestation_id=json.loads(raw)["attestation_id"],
                attestation_sha256=hashlib.sha256(raw).hexdigest(),
                original_manifest_sha256=manifest_sha,
                original_ledger_sha256=ledger_sha,
                snapshot_id="TWELVEDATA_2026-09-12_RUN_fixture-A1",
                snapshot_sha256=SNAPSHOT_SHA,
                analytical_commit=ANALYTICAL,
                workflow_commit=WORKFLOW,
            ),
        }),
    )
    return root, spec, statement, path, hashlib.sha256(raw).hexdigest()


def test_sol_recovery_preflight_proves_checkpoint_ledger_and_cost(recovery_source):
    root, spec = recovery_source
    plan = preflight_sol_recovery(root, spec)
    assert plan.ticker == "JNJ"
    assert plan.original_cost_usd == "0.1"
    assert plan.analyst_response_id == "resp_analyst"
    assert plan.snapshot_sha256 == SNAPSHOT_SHA
    assert plan.research_cutoff_utc == RESEARCH_CUTOFF


def test_sol_recovery_preflight_fails_closed_on_checkpoint_tamper(recovery_source):
    root, spec = recovery_source
    path = root / "model_responses" / "10001_JNJ_analyst.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(SolRecoveryError, match="manifest verification"):
        preflight_sol_recovery(root, spec)


def test_historical_reconciliation_is_valid_deterministic_and_preflight_opt_in(
    historical_source,
):
    root, spec, statement, path, digest = historical_source
    raw = path.read_bytes()
    assert raw == attestation_bytes(statement) == attestation_bytes(deepcopy(statement))
    manifest = json.loads((root / "manifest.json").read_text())
    first = verify_historical_reconciliation(
        root, manifest, raw, expected_attestation_sha256=digest,
    )
    second = verify_historical_reconciliation(
        root, manifest, raw, expected_attestation_sha256=digest,
    )
    assert first == second
    assert first["omission_paths"] == [
        "fresh_sources/trinity/MRNA/sec/manifest.json",
        "fresh_sources/trinity/SWKS/sec/manifest.json",
    ]
    with pytest.raises(SolRecoveryError, match="disabled"):
        preflight_sol_recovery(
            root, spec, reconciliation_attestation=path,
            reconciliation_sha256=digest,
        )
    plan = preflight_sol_recovery(
        root, spec, allow_historical_reconciliation=True,
        reconciliation_attestation=path, reconciliation_sha256=digest,
    )
    assert plan.historical_reconciliation_id == first["attestation_id"]
    assert plan.historical_reconciliation_sha256 == digest


def test_historical_reconciliation_rejects_file_tamper(historical_source):
    root, _spec, _statement, path, digest = historical_source
    target = root / "fresh_sources" / "trinity" / "SWKS" / "sec" / "manifest.json"
    target.write_bytes(target.read_bytes() + b" ")
    manifest = json.loads((root / "manifest.json").read_text())
    with pytest.raises(HistoricalReconciliationError, match="size mismatch"):
        verify_historical_reconciliation(
            root, manifest, path.read_bytes(), expected_attestation_sha256=digest,
        )


def test_historical_reconciliation_rejects_attestation_hash_tamper(historical_source):
    root, _spec, _statement, path, digest = historical_source
    manifest = json.loads((root / "manifest.json").read_text())
    raw = path.read_bytes() + b" "
    with pytest.raises(HistoricalReconciliationError, match="not authorized"):
        verify_historical_reconciliation(
            root, manifest, raw, expected_attestation_sha256=digest,
        )


def test_historical_reconciliation_rejects_missing_inventoried_file(historical_source):
    root, _spec, _statement, path, digest = historical_source
    manifest = json.loads((root / "manifest.json").read_text())
    inventoried = root / Path(manifest["files"][0]["path"])
    inventoried.unlink()
    with pytest.raises(HistoricalReconciliationError, match="attested file is missing"):
        verify_historical_reconciliation(
            root, manifest, path.read_bytes(), expected_attestation_sha256=digest,
        )


def test_historical_reconciliation_rejects_extra_file(historical_source):
    root, _spec, _statement, path, digest = historical_source
    extra = root / "fresh_sources" / "trinity" / "EXTRA" / "sec" / "manifest.json"
    extra.parent.mkdir(parents=True)
    extra.write_text("{}", encoding="utf-8")
    manifest = json.loads((root / "manifest.json").read_text())
    with pytest.raises(HistoricalReconciliationError, match="unexpected source files"):
        verify_historical_reconciliation(
            root, manifest, path.read_bytes(), expected_attestation_sha256=digest,
        )


@pytest.mark.parametrize("mutation,match", [
    ("duplicate", "duplicate attested path"),
    ("unsafe", "unsafe attested path"),
    ("official", "VALIDATION-only"),
])
def test_historical_reconciliation_rejects_invalid_attestation(
    historical_source, mutation, match, monkeypatch,
):
    root, _spec, statement, _path, _digest = historical_source
    changed = deepcopy(statement)
    if mutation == "duplicate":
        changed["omissions"].append(deepcopy(changed["omissions"][0]))
    elif mutation == "unsafe":
        changed["omissions"][0]["path"] = "../manifest.json"
    else:
        changed["mode"] = "OFFICIAL"
        changed["official_benchmark_observation"] = True
    raw = attestation_bytes(changed)
    trusted = historical_reconciliation._HISTORICAL_TRUST_REGISTRY[RUN_ID]
    monkeypatch.setattr(
        historical_reconciliation, "_HISTORICAL_TRUST_REGISTRY",
        MappingProxyType({
            RUN_ID: replace(
                trusted, attestation_sha256=hashlib.sha256(raw).hexdigest(),
            ),
        }),
    )
    manifest = json.loads((root / "manifest.json").read_text())
    with pytest.raises(HistoricalReconciliationError, match=match):
        verify_historical_reconciliation(
            root, manifest, raw,
            expected_attestation_sha256=hashlib.sha256(raw).hexdigest(),
        )


def test_repository_historical_attestation_has_frozen_identity():
    path = Path(__file__).parents[2] / "attestations" / "shadow" / (
        "SHADOW_USA_2026-10-09_38230389276_A1.reconciliation.json"
    )
    raw = path.read_bytes()
    value = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == (
        "d58658403e12f4c6d1df8e8a7615a982f0938e8ac6a95aa335f33e2ef8a1f5b1"
    )
    assert value["attestation_id"] == (
        "sha256:321d1907882de2667e3e74ea7bf82140484556619dfae10dc7d103f28c111cb8"
    )
    assert raw == attestation_bytes(value["statement"])
    trusted = historical_reconciliation._HISTORICAL_TRUST_REGISTRY[RUN_ID]
    assert trusted == HistoricalTrustRecord(
        source_run_id=RUN_ID,
        attestation_id="sha256:321d1907882de2667e3e74ea7bf82140484556619dfae10dc7d103f28c111cb8",
        attestation_sha256="d58658403e12f4c6d1df8e8a7615a982f0938e8ac6a95aa335f33e2ef8a1f5b1",
        original_manifest_sha256="a322cfd900658dee5fc787a79e877262f4aac93fb9cae80f5357502ace464878",
        original_ledger_sha256="c18969c615df6ee465530df6014813b7f55187d7ffa32c3db5fd528d2ef53ae8",
        snapshot_id="TWELVEDATA_2026-10-09_RUN_38230389276-A1",
        snapshot_sha256="7b9ed6ced475810365f7d7e791f528916333261c278e9cddfd27b08c210b36a7",
        analytical_commit=ANALYTICAL,
        workflow_commit=WORKFLOW,
    )


def test_production_trust_registry_rejects_reissued_attestation_and_cli_hash():
    path = Path(__file__).parents[2] / "attestations" / "shadow" / (
        "SHADOW_USA_2026-10-09_38230389276_A1.reconciliation.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["statement"]["omissions"][1]["sha256"] = "f" * 64
    value["statement"]["omissions"][1]["r2"]["metadata_sha256"] = "f" * 64
    raw = attestation_bytes(value["statement"])
    with pytest.raises(HistoricalReconciliationError, match="CLI attestation SHA-256 is not authorized"):
        verify_historical_reconciliation(
            Path(RUN_ID), {}, raw,
            expected_attestation_sha256=hashlib.sha256(raw).hexdigest(),
        )


def test_trust_registry_rejects_coherent_manifest_ledger_snapshot_substitution(
    historical_source, tmp_path,
):
    root, spec, statement, _path, _digest = historical_source
    ledger = root / "ledger" / "trinity.sqlite3"
    with sqlite3.connect(ledger) as connection:
        connection.execute("PRAGMA user_version = 987654")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    replacement_snapshot_sha = "b" * 64
    snapshot_path = root / "artifacts" / "price_snapshot_restore.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot.update({
        "snapshot_id": "TWELVEDATA_2026-09-12_RUN_reissued-A1",
        "snapshot_sha256": replacement_snapshot_sha,
    })
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        target = root / Path(item["path"])
        item["bytes"] = target.stat().st_size
        item["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest["total_bytes"] = sum(item["bytes"] for item in manifest["files"])
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "checksums.sha256").write_bytes(
        "".join(
            f"{item['sha256']}  {item['path']}\n" for item in manifest["files"]
        ).encode("utf-8"),
    )

    changed = deepcopy(statement)
    for name, target in (
        ("manifest", manifest_path), ("ledger", ledger),
    ):
        record = changed["original_shadow"][name]
        record["bytes"] = target.stat().st_size
        record["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        record["r2"]["content_length"] = record["bytes"]
        record["r2"]["metadata_sha256"] = record["sha256"]
    controls = {item["path"]: item for item in changed["control_files"]}
    checksum_record = controls["checksums.sha256"]
    checksum_path = root / "checksums.sha256"
    checksum_record["bytes"] = checksum_path.stat().st_size
    checksum_record["sha256"] = hashlib.sha256(checksum_path.read_bytes()).hexdigest()
    raw = attestation_bytes(changed)
    replacement_attestation = tmp_path / "reissued-reconciliation.json"
    replacement_attestation.write_bytes(raw)
    replacement_spec = replace(spec, snapshot_sha256=replacement_snapshot_sha)

    with pytest.raises(SolRecoveryError, match="attestation SHA-256 is not authorized"):
        preflight_sol_recovery(
            root, replacement_spec, allow_historical_reconciliation=True,
            reconciliation_attestation=replacement_attestation,
            reconciliation_sha256=hashlib.sha256(raw).hexdigest(),
        )


def _historical_sec_document(root: Path, ticker: str) -> tuple[str, bytes, Path]:
    relative = f"{ticker}/verified-document.html"
    target = root / "fresh_sources" / "trinity" / ticker / "sec" / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = f"immutable SEC bytes for {ticker}".encode("utf-8")
    target.write_bytes(raw)
    original = (
        f"/home/runner/work/_temp/trinity-shadow/{RUN_ID}/"
        f"fresh_sources/trinity/{ticker}/sec/{relative}"
    )
    return original, raw, target


@pytest.mark.parametrize("ticker", ["MRNA", "SWKS"])
def test_historical_sec_absolute_path_relocates_and_preserves_audit_path(tmp_path, ticker):
    root = tmp_path / RUN_ID
    original, raw, target = _historical_sec_document(root, ticker)
    sec_root = root / "fresh_sources" / "trinity" / ticker / "sec"
    manifest = {
        "as_of": "2026-10-12",
        "companies": {ticker: {"documents": [{
            "local_path": original,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "published_at": "2026-10-09",
            "filing_date": "2026-10-09",
            "evidence_id": f"sec:{ticker}:verified",
        }]}},
    }
    (sec_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    trusted = historical_reconciliation._HISTORICAL_TRUST_REGISTRY[RUN_ID]
    resolver = _archived_sec_path_resolver(root, ticker, trusted.attestation_id)
    assert resolver is not None
    assert resolver(original, hashlib.sha256(raw).hexdigest()) == target.resolve()
    loaded = load_document_coverage(
        ticker, "2026-10-12", cache_dir=sec_root,
        local_path_resolver=resolver,
    )
    assert loaded is not None
    assert loaded["documents"][0]["local_path"] == original


def test_historical_sec_relocation_rejects_digest_mismatch(tmp_path):
    root = tmp_path / RUN_ID
    original, _raw, _target = _historical_sec_document(root, "SWKS")
    trusted = historical_reconciliation._HISTORICAL_TRUST_REGISTRY[RUN_ID]
    resolver = _archived_sec_path_resolver(root, "SWKS", trusted.attestation_id)
    with pytest.raises(SolRecoveryError, match="SHA-256 mismatch"):
        resolver(original, "0" * 64)


def test_historical_sec_relocation_rejects_missing_and_duplicate_filename_fallback(tmp_path):
    root = tmp_path / RUN_ID
    sec_root = root / "fresh_sources" / "trinity" / "SWKS" / "sec"
    for directory in ("first", "second"):
        path = sec_root / directory / "same.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"duplicate filename")
    original = (
        f"/home/runner/work/_temp/trinity-shadow/{RUN_ID}/"
        "fresh_sources/trinity/SWKS/sec/expected/same.html"
    )
    trusted = historical_reconciliation._HISTORICAL_TRUST_REGISTRY[RUN_ID]
    resolver = _archived_sec_path_resolver(root, "SWKS", trusted.attestation_id)
    with pytest.raises(SolRecoveryError, match="is missing"):
        resolver(original, hashlib.sha256(b"duplicate filename").hexdigest())


@pytest.mark.parametrize("kind", ["traversal", "ambiguous", "substitution"])
def test_historical_sec_relocation_rejects_unsafe_or_ambiguous_suffix(tmp_path, kind):
    root = tmp_path / RUN_ID
    trusted = historical_reconciliation._HISTORICAL_TRUST_REGISTRY[RUN_ID]
    resolver = _archived_sec_path_resolver(root, "MRNA", trusted.attestation_id)
    anchor = f"{RUN_ID}/fresh_sources/trinity/MRNA/sec"
    original = (
        f"/home/runner/{anchor}/../outside.html" if kind == "traversal" else
        f"/home/runner/{anchor}/nested/{anchor}/document.html" if kind == "ambiguous" else
        f"/arbitrary/prefix/{anchor}/document.html"
    )
    with pytest.raises(SolRecoveryError, match="unsafe|ambiguous|not authorized"):
        resolver(original, "0" * 64)


def test_historical_sec_relocation_rejects_outside_root_and_unauthorized_run(tmp_path):
    root = (tmp_path / RUN_ID).resolve()
    outside = (tmp_path / "outside.html").resolve()
    with pytest.raises(SolRecoveryError, match="escapes verified source root"):
        _require_inside(root, outside, "relocated SEC document")
    unauthorized = tmp_path / "SHADOW_USA_UNAUTHORIZED"
    with pytest.raises(SolRecoveryError, match="not authorized for this run"):
        _archived_sec_path_resolver(
            unauthorized, "SWKS",
            historical_reconciliation._HISTORICAL_TRUST_REGISTRY[RUN_ID].attestation_id,
        )


def test_normal_sec_loader_path_behavior_is_unchanged(tmp_path):
    raw = b"ordinary production cache"
    document = tmp_path / "ordinary.html"
    document.write_bytes(raw)
    original = str(document)
    manifest = {
        "as_of": "2026-09-12",
        "companies": {"AAPL": {"documents": [{
            "local_path": original,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "published_at": "2026-09-12",
            "filing_date": "2026-09-12",
            "evidence_id": "sec:AAPL:ordinary",
        }]}},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded = load_document_coverage("AAPL", "2026-09-12", cache_dir=tmp_path)
    assert loaded is not None
    assert loaded["documents"][0]["local_path"] == original


class _OfflineCriticTransport:
    def __init__(self, ledger, critic):
        self.ledger = ledger
        self.critic = critic
        self.calls = []

    def invoke(self, **kwargs):
        assert kwargs["role"] == "CRITIC"
        assert kwargs["ticker"] == "JNJ"
        self.calls.append(kwargs)
        self.ledger.append({
            "provider": "OPENAI_RESPONSES_API", "model": "gpt-5.6-sol",
            "role": "CRITIC", "ticker": "JNJ", "response_id": "resp_critic",
            "calculated_cost_usd": "0.2", "raw_sha256": "e" * 64,
            "raw_path": "model_responses/10002_JNJ_critic.json",
        })
        return SimpleNamespace(parsed=self.critic)


def test_sol_recovery_calls_only_critic_preserves_source_and_is_idempotent(
    recovery_source, tmp_path, monkeypatch,
):
    root, spec = recovery_source
    source_ledger_sha = hashlib.sha256((root / "ledger" / "trinity.sqlite3").read_bytes()).hexdigest()
    _analyst, critic, _ = _role_values(_load_latest_thesis("JNJ", AS_OF, THESIS_CACHE))
    transports = []

    def factory(_destination, ledger):
        transport = _OfflineCriticTransport(ledger, critic)
        transports.append(transport)
        return transport

    archived = _FixtureSource().acquire("JNJ")
    native_facts = build_facts_v3(
        with_expectation_comparisons(archived.pack.company_input), AS_OF,
    )
    monkeypatch.setattr(
        "trinity.shadow.sol_recovery._rebuild_archived_inputs",
        lambda *_args, **_kwargs: (archived.pack, native_facts),
    )
    monkeypatch.setattr(
        "trinity.shadow.sol_recovery.build_setup",
        lambda *_args, **_kwargs: pytest.fail(
            "historical recovery must not call build_setup"
        ),
        raising=False,
    )

    destination = tmp_path / "RECOVERY_38230389276_SWKS_R1"
    result = execute_sol_recovery(
        root, destination, spec, confirmation=CONFIRMATION,
        transport_factory=factory,
    )
    assert result["state"] == "COMPLETE"
    assert result["original_cost_usd"] == "0.1"
    assert result["final_cost_usd"] == "0.3"
    assert len(transports) == 1 and len(transports[0].calls) == 1
    assert result["new_calls"] == [{"ticker": "JNJ", "role": "CRITIC"}]
    assert hashlib.sha256((root / "ledger" / "trinity.sqlite3").read_bytes()).hexdigest() == source_ledger_sha
    assert json.loads((root / "run_state.json").read_text())["state"] == "FAILED_SOL"
    with LedgerStorage.open(destination / "ledger" / "trinity.sqlite3") as storage:
        assert storage.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not storage.connection.execute("PRAGMA foreign_key_check").fetchall()
        statuses = storage.connection.execute(
            "SELECT status, count(*) FROM attempt GROUP BY status ORDER BY status"
        ).fetchall()
        assert [tuple(row) for row in statuses] == [("FAILED", 1), ("SUCCEEDED", 2)]
        assert storage.connection.execute("SELECT count(*) FROM run").fetchone()[0] == 2

    with pytest.raises(SolRecoveryError, match="already exists"):
        execute_sol_recovery(
            root, destination, spec, confirmation=CONFIRMATION,
            transport_factory=factory,
        )
    assert len(transports) == 1


def _historical_setup_fixture(tmp_path, records):
    root = tmp_path / RUN_ID
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "setups.json").write_text(json.dumps(records), encoding="utf-8")
    manifest = generate_manifest(root, run_id=RUN_ID)
    return root, manifest


def _swks_frozen_setup():
    return {
        "ticker": "SWKS", "as_of": "2026-10-09",
        "research_status": "INVESTIGATE", "evidence_confidence": "HIGH",
        "thesis_strength": "MEDIUM", "technical_regime": "UPTREND",
        "setup_type": "PULLBACK",
        "entry_condition": "BUY ON PULLBACK 76.1342-77.2496",
        "entry_level": 77.2496, "stop_level": 70.5574,
        "stop_distance_pct": 8.663, "tp1": 92.8, "tp2": 97.326,
        "risk_per_share": 6.6921, "reward_tp1": 15.5504,
        "reward_tp2": 20.0764, "rr_tp1": 2.3237, "rr_tp2": 3.0,
        "close": 76.35, "atr14": 4.4614, "rsi14": 26.8757,
        "sma20": 85.938, "sma50": 76.1342, "sma200": 66.1962,
        "distance_sma20_pct": -11.1569, "distance_sma50_pct": 0.2834,
        "distance_sma200_pct": 15.3389, "return_5d": -10.2082,
        "return_20d": -13.5823, "return_60d": 32.4831,
        "high_20d": 92.8, "low_20d": 73.93,
        "average_volume_20d": 9017730.0, "relative_volume": 1.0162,
        "reason_codes": ["UPTREND_CONFIRMED", "SMA_SUPPORT_NEARBY"],
        "invalidation": "Setup invalid below 70.5574 on a closing basis.",
    }


def test_frozen_historical_setup_loader_returns_exact_record_without_mutation(tmp_path):
    expected = _swks_frozen_setup()
    root, manifest = _historical_setup_fixture(tmp_path, [expected])
    before = (root / "artifacts" / "setups.json").read_bytes()
    loaded = load_frozen_historical_setup(
        root, manifest, ticker="SWKS", as_of="2026-10-09",
    )
    actual = loaded.to_dict()
    actual["reason_codes"] = list(actual["reason_codes"])
    assert actual == expected
    assert (loaded.entry_level, loaded.stop_level, loaded.tp1, loaded.tp2) == (
        77.2496, 70.5574, 92.8, 97.326,
    )
    assert (loaded.rr_tp1, loaded.rr_tp2) == (2.3237, 3.0)
    assert (root / "artifacts" / "setups.json").read_bytes() == before


def test_frozen_historical_setup_loader_rejects_missing_and_duplicate(tmp_path):
    root, manifest = _historical_setup_fixture(tmp_path / "missing", [])
    with pytest.raises(SolRecoveryError, match="exactly one"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )
    record = _swks_frozen_setup()
    root, manifest = _historical_setup_fixture(
        tmp_path / "duplicate", [record, deepcopy(record)],
    )
    with pytest.raises(SolRecoveryError, match="exactly one"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )


def test_frozen_historical_setup_loader_rejects_wrong_as_of(tmp_path):
    record = _swks_frozen_setup()
    record["as_of"] = "2026-10-08"
    root, manifest = _historical_setup_fixture(tmp_path, [record])
    with pytest.raises(SolRecoveryError, match="as_of mismatch"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )


def test_frozen_historical_setup_loader_rejects_manifest_hash_mismatch(tmp_path):
    root, manifest = _historical_setup_fixture(tmp_path, [_swks_frozen_setup()])
    path = root / "artifacts" / "setups.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(SolRecoveryError, match="does not match source manifest"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )


def test_frozen_historical_setup_loader_rejects_missing_or_unmanifested_file(tmp_path):
    root = tmp_path / "missing" / RUN_ID
    root.mkdir(parents=True)
    manifest = generate_manifest(root, run_id=RUN_ID)
    with pytest.raises(SolRecoveryError, match="not uniquely manifested"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )
    root, manifest = _historical_setup_fixture(
        tmp_path / "unmanifested", [_swks_frozen_setup()],
    )
    manifest["files"] = [
        item for item in manifest["files"]
        if item["path"] != "artifacts/setups.json"
    ]
    with pytest.raises(SolRecoveryError, match="not uniquely manifested"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )


def test_frozen_historical_setup_loader_rejects_invalid_json_and_schema(tmp_path):
    root = tmp_path / "invalid-json" / RUN_ID
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "setups.json").write_text("{", encoding="utf-8")
    manifest = generate_manifest(root, run_id=RUN_ID)
    with pytest.raises(SolRecoveryError, match="historical setups artifact"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )
    record = _swks_frozen_setup()
    record["unexpected"] = True
    root, manifest = _historical_setup_fixture(tmp_path / "schema", [record])
    with pytest.raises(SolRecoveryError, match="schema mismatch"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )


def test_frozen_historical_setup_loader_rejects_wrong_ticker(tmp_path):
    record = _swks_frozen_setup()
    record["ticker"] = "MRNA"
    root, manifest = _historical_setup_fixture(tmp_path, [record])
    with pytest.raises(SolRecoveryError, match="exactly one"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )


def test_frozen_historical_setup_loader_rejects_unauthorized_source_run(tmp_path):
    root = tmp_path / "SHADOW_USA_UNAUTHORIZED"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "setups.json").write_text(
        json.dumps([_swks_frozen_setup()]), encoding="utf-8",
    )
    manifest = generate_manifest(root, run_id=root.name)
    with pytest.raises(SolRecoveryError, match="source run is not authorized"):
        load_frozen_historical_setup(
            root, manifest, ticker="SWKS", as_of="2026-10-09",
        )


def test_sol_recovery_requires_explicit_validation_confirmation(recovery_source, tmp_path):
    root, spec = recovery_source
    with pytest.raises(SolRecoveryError, match="confirmation"):
        execute_sol_recovery(
            root, tmp_path / "recovery", spec, confirmation="OFFICIAL",
            transport_factory=lambda *_args: pytest.fail("transport must not be created"),
        )


def test_sol_recovery_source_mismatch_blocks_before_transport(
    recovery_source, tmp_path, monkeypatch,
):
    root, spec = recovery_source
    monkeypatch.setattr(
        "trinity.shadow.sol_recovery._rebuild_archived_inputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SolRecoveryError("archived sources do not reconstruct fact packet")
        ),
    )
    with pytest.raises(SolRecoveryError, match="do not reconstruct"):
        execute_sol_recovery(
            root, tmp_path / "recovery-source-mismatch", spec,
            confirmation=CONFIRMATION,
            transport_factory=lambda *_args: pytest.fail(
                "transport must not be constructed before source verification"
            ),
        )


class _ErrorResponse:
    status_code = 429
    headers = {"x-request-id": "req_safe_123"}
    content = json.dumps({
        "error": {"code": "insufficient_quota", "type": "insufficient_quota",
                  "message": "must not be persisted"},
    }).encode()


class _ErrorSession:
    def post(self, *_args, **_kwargs):
        return _ErrorResponse()


def test_responses_429_persists_only_sanitized_diagnostics(tmp_path):
    api = ResponsesAPI(
        run_root=tmp_path,
        cost_ledger=PersistentCostLedger(tmp_path / "api_usage.json"),
        api_key="unit-secret", session=_ErrorSession(),
    )
    with pytest.raises(ResponsesTransportError) as captured:
        api.invoke(
            model="gpt-5.6-sol", role="CRITIC", ticker="SWKS", prompt="secret prompt",
            schema={"type": "object"}, call_ordinal=10002,
        )
    error = captured.value
    assert error.error_code == "insufficient_quota"
    assert error.request_id == "req_safe_123"
    assert error.classification == "BILLING_OR_QUOTA"
    assert error.retryable is False
    diagnostic = json.loads((
        tmp_path / "transport_errors" / "10002_SWKS_critic.json"
    ).read_text())
    assert diagnostic["error_code"] == "insufficient_quota"
    assert diagnostic["request_id"] == "req_safe_123"
    serialized = json.dumps(diagnostic)
    assert "unit-secret" not in serialized
    assert "secret prompt" not in serialized
    assert "must not be persisted" not in serialized
    assert not (tmp_path / "api_usage.json").exists()


REAL_SOURCE_ROOT = Path(
    "C:/Users/giopa/trinity_run_38230389276_analysis/"
    "SHADOW_USA_2026-10-09_38230389276_A1"
)
REAL_R2_ROOT = Path(
    "C:/Users/giopa/trinity_run_38230389276_analysis/"
    "RECOVERY_SHADOW_USA_2026-10-09_38230389276_A1_SWKS_R2"
)


def _real_existing_critic_spec() -> ExistingCriticSpec:
    return _EXISTING_CRITIC_TRUST_REGISTRY[(RUN_ID, "SWKS")]


def _require_real_r2_fixture() -> None:
    if not REAL_SOURCE_ROOT.is_dir() or not REAL_R2_ROOT.is_dir():
        pytest.skip("authorized immutable historical R2 fixture is not locally available")


def test_existing_critic_preflight_real_archive_is_read_only_and_exact():
    _require_real_r2_fixture()
    before = {
        path.relative_to(REAL_R2_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in REAL_R2_ROOT.rglob("*") if path.is_file()
    }
    plan = preflight_finalize_existing(
        REAL_SOURCE_ROOT, REAL_R2_ROOT, _real_existing_critic_spec(),
    )
    after = {
        path.relative_to(REAL_R2_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in REAL_R2_ROOT.rglob("*") if path.is_file()
    }
    assert before == after
    assert plan.spec.response_id == (
        "resp_049a7d460a02ea8c016ace3db1d28c87d2b31136f5b772754f"
    )
    assert plan.spec.model == "gpt-5.6-sol"
    assert plan.spec.research_cutoff_at == "2026-10-12T11:09:36.364663Z"
    assert plan.spec.original_analysis_reference_at == "2026-10-12T11:43:00.974465Z"
    assert plan.spec.critic_generated_at == "2026-10-13T14:19:31.462433Z"
    value = plan.to_dict()
    assert value["request_kind"] == "RECONSTRUCTION"
    assert value["pit_class"] == "RECONSTRUCTED_NOT_ARCHIVED"
    assert value["official_benchmark_observation"] is False
    assert value["telegram_delivery"] is False
    assert value["broker_execution"] is False
    assert value["cloud_write"] is False
    assert value["network_enabled"] is False


@pytest.mark.parametrize("field,replacement", [
    ("response_id", "resp_substituted"),
    ("model", "gpt-substituted"),
    ("critic_file_sha256", "0" * 64),
    ("raw_output_artifact_id", "sha256:" + "1" * 64),
    ("parsed_output_artifact_id", "sha256:" + "2" * 64),
    ("critic_cost_usd", "0.000001"),
    ("research_cutoff_at", "2026-10-12T11:09:35.000000Z"),
    ("original_analysis_reference_at", "2026-10-12T11:42:59.000000Z"),
    ("critic_generated_at", "2026-10-12T11:43:00.974465Z"),
])
def test_existing_critic_preflight_rejects_cli_substitution_before_io(
    tmp_path, field, replacement,
):
    changed = replace(_real_existing_critic_spec(), **{field: replacement})
    with pytest.raises(SolRecoveryError, match="not authorized"):
        preflight_finalize_existing(
            tmp_path / RUN_ID,
            tmp_path / changed.recovery_run_id,
            changed,
        )


def test_existing_critic_preflight_rejects_file_tamper(tmp_path):
    _require_real_r2_fixture()
    copied = tmp_path / REAL_R2_ROOT.name
    shutil.copytree(REAL_R2_ROOT, copied)
    critic = copied / "model_responses" / "10002_SWKS_critic.json"
    critic.write_bytes(critic.read_bytes() + b" ")
    with pytest.raises(SolRecoveryError, match="SHA-256 mismatch"):
        preflight_finalize_existing(
            REAL_SOURCE_ROOT, copied, _real_existing_critic_spec(),
        )


def test_finalize_existing_uses_only_verified_r2_bytes_on_temporary_copy(
    tmp_path, monkeypatch,
):
    _require_real_r2_fixture()
    copied = tmp_path / REAL_R2_ROOT.name
    shutil.copytree(REAL_R2_ROOT, copied)
    spec = _real_existing_critic_spec()
    ledger = copied / "ledger" / "trinity.sqlite3"
    usage = copied / "artifacts" / "api_usage.json"
    original_usage = usage.read_bytes()

    monkeypatch.setattr(
        "trinity.shadow.sol_recovery.ResponsesAPI",
        lambda *_args, **_kwargs: pytest.fail("OpenAI transport must not be constructed"),
    )
    monkeypatch.setattr(
        "trinity.shadow.sol_recovery.build_setup",
        lambda *_args, **_kwargs: pytest.fail("build_setup must not be called"),
        raising=False,
    )
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )
    real_getenv = os.getenv
    monkeypatch.setattr(
        os, "getenv",
        lambda name, *args: (
            pytest.fail("OPENAI_API_KEY must not be read")
            if name == "OPENAI_API_KEY" else real_getenv(name, *args)
        ),
    )
    source = inspect.getsource(finalize_existing)
    assert "ResponsesAPI" not in source
    assert "OPENAI_API_KEY" not in source
    assert "build_setup" not in source

    result = finalize_existing(
        REAL_SOURCE_ROOT, copied, spec,
        confirmation=FINALIZE_EXISTING_CONFIRMATION,
    )
    assert result["state"] == "COMPLETE_RECONSTRUCTION"
    assert result["request_kind"] == "RECONSTRUCTION"
    assert result["pit_class"] == "RECONSTRUCTED_NOT_ARCHIVED"
    assert result["additional_openai_calls"] == 0
    assert result["additional_api_cost_usd"] == "0"
    assert result["critic_generated_at"] == spec.critic_generated_at
    assert result["finalized_at"] > spec.critic_generated_at
    assert usage.read_bytes() == original_usage

    with LedgerStorage.open(ledger) as storage:
        assert storage.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not storage.connection.execute("PRAGMA foreign_key_check").fetchall()
        old_attempt = storage.get_attempt(spec.failed_attempt_id)
        assert old_attempt.status == "FAILED"
        old_research = storage.get_research_record(
            "5a891cd4-49a5-4513-afda-e2d961f36590"
        )
        old_setup = storage.get_setup("a7594497-4ce5-4a01-8d5a-12a8a447c5c7")
        assert old_research.run_id is None and old_setup.run_id is None
        committed = storage.connection.execute(
            "SELECT r.run_id, rr.request_kind, a.attempt_id "
            "FROM run r JOIN run_request rr USING(run_request_id) "
            "JOIN attempt a ON a.attempt_id=r.attempt_id "
            "JOIN research_record rec ON rec.run_id=r.run_id "
            "WHERE rec.subject_key='ticker:SWKS' AND r.status='COMMITTED'"
        ).fetchall()
        assert len(committed) == 1
        assert committed[0]["request_kind"] == "RECONSTRUCTION"
        assert committed[0]["attempt_id"] != spec.failed_attempt_id
        run = storage.get_run(str(committed[0]["run_id"]))
        manifest = json.loads(
            storage.get_artifact(run.result_manifest_artifact_id).payload
        )
        assert manifest["resolved_pit_class"] == "RECONSTRUCTED_NOT_ARCHIVED"
        assert manifest["pit_reference_at"] == spec.original_analysis_reference_at
        assert manifest["analysis_cutoff_at"] == spec.critic_generated_at
        assert storage.connection.execute(
            "SELECT count(*) FROM research_record rr JOIN run r ON rr.run_id=r.run_id "
            "WHERE rr.subject_key='ticker:MRNA' AND r.status='COMMITTED'"
        ).fetchone()[0] == 1
        new_setup = storage.connection.execute(
            "SELECT result_artifact_id FROM setup WHERE run_id=?",
            (run.run_id,),
        ).fetchone()
        payload = json.loads(storage.get_artifact(new_setup[0]).payload)
        assert payload["setup_type"] == "PULLBACK"
        assert payload["entry_condition"] == "BUY ON PULLBACK 76.1342-77.2496"
        assert payload["entry_level"] == "77.2496"
        assert payload["stop_level"] == "70.5574"
        assert payload["tp1"] == "92.8"
        assert payload["tp2"] == "97.326"
        assert payload["rr_tp1"] == "2.3237"
        assert payload["rr_tp2"] == "3"

    with pytest.raises(SolRecoveryError, match="SHA-256 mismatch|already finalized"):
        finalize_existing(
            REAL_SOURCE_ROOT, copied, spec,
            confirmation=FINALIZE_EXISTING_CONFIRMATION,
        )
    with LedgerStorage.open(ledger) as storage:
        assert storage.connection.execute(
            "SELECT count(*) FROM research_record rr JOIN run r ON rr.run_id=r.run_id "
            "WHERE rr.subject_key='ticker:SWKS' AND r.status='COMMITTED'"
        ).fetchone()[0] == 1


def test_finalize_existing_offline_transport_rejects_pit_safe_request(
    tmp_path, monkeypatch,
):
    _require_real_r2_fixture()
    copied = tmp_path / REAL_R2_ROOT.name
    shutil.copytree(REAL_R2_ROOT, copied)
    original = LedgerStorage.create_run_request

    def force_pit_safe(storage, *args, **kwargs):
        kwargs["request_kind"] = "PIT_SAFE_DECISION"
        kwargs["parameters"] = {}
        return original(storage, *args, **kwargs)

    monkeypatch.setattr(LedgerStorage, "create_run_request", force_pit_safe)
    monkeypatch.setattr(
        "trinity.shadow.sol_recovery.ResponsesAPI",
        lambda *_args, **_kwargs: pytest.fail("OpenAI transport must not be constructed"),
    )
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )
    with pytest.raises(
        ArtifactIntegrityError,
        match="offline replay transport requires a RECONSTRUCTION request",
    ):
        finalize_existing(
            REAL_SOURCE_ROOT, copied, _real_existing_critic_spec(),
            confirmation=FINALIZE_EXISTING_CONFIRMATION,
        )
