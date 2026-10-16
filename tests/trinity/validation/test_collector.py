from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import sqlite3

import pytest

from trinity.validation.collector import CollectionError, collect_run


SHA = "a" * 64
GIT_SHA = "a" * 40


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _fixture(root: Path) -> Path:
    artifacts = root / "artifacts"
    snapshot = {
        "price_provider": "TWELVE_DATA", "price_snapshot_id": "snap",
        "price_snapshot_session": "2026-10-09", "price_snapshot_sha256": SHA,
    }
    funnel_snapshot = {"provider": "TWELVE_DATA", "snapshot_id": "snap",
                       "session": "2026-10-09", "sha256": SHA}
    scanner = [
        {"ticker": ticker, "screening_state": "READY_TECHNICALLY",
         "primary_reason": "ready", "latest_session": "2026-10-09"}
        for ticker in ("AAA", "BBB", "CCC", "DDD")
    ]
    _write_json(artifacts / "pre_research_funnel.json", {
        "counts": {"universe": 4, "ready_technically": 4}, "survivors": scanner,
        "universe_source": "/frozen/trinity/usa_issuer_registry_v1.fixture",
        "inputs": {ticker: {} for ticker in ("AAA", "BBB", "CCC", "DDD")},
        "price_snapshot": funnel_snapshot,
    })
    setups = []
    for ticker in ("AAA", "BBB", "CCC", "DDD"):
        setups.append({
            "ticker": ticker, "as_of": "2026-10-09", "setup_type": "PULLBACK",
            "entry_condition": "BUY ON PULLBACK 99-100", "entry_level": 100,
            "stop_level": 95, "tp1": 110, "tp2": 115, "rr_tp1": 2, "rr_tp2": 3,
        })
    _write_json(artifacts / "setups.json", setups)
    _write_json(artifacts / "luna_results.json", {"results": [
        {"ticker": "AAA", "status": "SUCCESS", "decision": "WATCH", "model": "luna",
         "prompt_version": "v1", "input_sha256": SHA, "raw_response_sha256": SHA},
        {"ticker": "BBB", "status": "SUCCESS", "decision": "ESCALATE", "model": "luna",
         "prompt_version": "v1", "input_sha256": SHA, "raw_response_sha256": SHA},
        {"ticker": "CCC", "status": "FAILED", "model": "luna", "prompt_version": "v1",
         "input_sha256": SHA},
    ]})
    execution = {
        "setup_id": "setup-b", "setup_sha256": SHA, "price_retrieved_at": "2026-10-15T10:00:00Z",
        "current_price": "100", "price_as_of": "2026-10-09", "price_provider": "TWELVE_DATA",
        "execution_status": "VALID", "current_rr1": "2", "current_rr2": "3",
        "reason_codes": ["OK"], "policy_version": "ENTRY_FRESHNESS_V1",
    }
    _write_json(artifacts / "sol_results.json", [{
        "ticker": "BBB", "run_status": "SUCCESS", "research_status": "INVESTIGATE",
        "cost_usd": "0.2", "setup_id": "setup-b", "execution_status": execution,
    }])
    _write_json(artifacts / "final_report.json", {
        "run_id": "SHADOW_TEST", "git_commit": GIT_SHA, "price_snapshot": snapshot,
        "research_cutoff": {"research_cutoff_utc": "2026-10-12T10:00:00Z"},
    })
    _write_json(artifacts / "api_usage.json", {
        "total_cost_usd": "0.21", "calls": [
            {"ticker": "AAA", "role": "LUNA_TRIAGE", "calculated_cost_usd": "0.01"},
            {"ticker": "BBB", "role": "LUNA_TRIAGE", "calculated_cost_usd": "0.01",
             "model": "luna"},
            {"ticker": "BBB", "role": "SOL_CRITIC", "calculated_cost_usd": "0.2",
             "model": "sol"},
            {"ticker": "CCC", "role": "LUNA_TRIAGE", "calculated_cost_usd": "0.01"},
        ],
    })
    _write_json(artifacts / "weekly_orchestrator.json", {
        "mode": "VALIDATION", "official_benchmark_observation": False,
        "analytical_commit": GIT_SHA, "target_market_session": "2026-10-09",
    })
    _write_json(artifacts / "price_snapshot_restore.json", {
        "snapshot_id": "snap", "snapshot_sha256": SHA,
    })
    _write_json(root / "run_state.json", {"run_id": "SHADOW_TEST", "state": "COMPLETE"})
    (root / "ledger").mkdir(parents=True)
    ledger = sqlite3.connect(root / "ledger" / "trinity.sqlite3")
    ledger.executescript("""
        CREATE TABLE attempt (attempt_id TEXT PRIMARY KEY, worker_identity TEXT NOT NULL);
        CREATE TABLE llm_interaction (
            llm_interaction_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, model TEXT NOT NULL,
            model_version TEXT NOT NULL, request_artifact_id TEXT NOT NULL,
            response_artifact_id TEXT, status TEXT NOT NULL
        );
        CREATE TABLE research_llm_interaction (
            research_id TEXT NOT NULL, llm_interaction_id TEXT NOT NULL,
            interaction_role TEXT NOT NULL, ordinal INTEGER NOT NULL
        );
    """)
    ledger.execute("INSERT INTO attempt VALUES (?, ?)", ("attempt-b", "forward-pilot:bbb"))
    ledger.execute(
        "INSERT INTO llm_interaction VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("critic-b", "attempt-b", "sol", "responses-api:sol", "sha256:" + "b" * 64,
         "sha256:" + "c" * 64, "SUCCEEDED"),
    )
    ledger.execute(
        "INSERT INTO research_llm_interaction VALUES (?, ?, ?, ?)",
        ("research-b", "critic-b", "CRITIQUE", 1),
    )
    ledger.commit(); ledger.close()
    _write_json(root / "model_responses" / "10002_BBB_critic.json", {"status": "completed"})
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        files.append({"path": relative, "bytes": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest()})
    _write_json(root / "manifest.json", {
        "schema_name": "trinity.shadow-run-manifest", "schema_version": "1",
        "run_id": "SHADOW_TEST", "files": files,
    })
    return root


def test_collector_reconstructs_memberships_setup_freshness_cost_and_provenance(tmp_path):
    result = collect_run(_fixture(tmp_path / "run"))
    by_ticker = {item.ticker: item for item in result.selections}
    assert len(result.selections) == 4 and len(result.setups) == 4
    assert by_ticker["AAA"].luna_decision == "WATCH"
    assert by_ticker["CCC"].luna_decision == "EVALUATED_FAILED"
    assert by_ticker["DDD"].luna_decision == "NOT_EVALUATED"
    assert by_ticker["AAA"].sol_status == "NOT_EVALUATED"
    assert by_ticker["BBB"].sol_status == "PROMOTED"
    assert by_ticker["BBB"].sol_cost_usd == "0.2"
    assert by_ticker["BBB"].sol_input_sha256 == "b" * 64
    assert by_ticker["BBB"].sol_output_sha256 == "c" * 64
    assert result.executions[0].execution_status == "VALID"
    assert result.total_api_cost_usd == "0.21"
    assert by_ticker["AAA"].universe_version == "usa_issuer_registry_v1.fixture"
    assert all(item.analytical_commit_sha == GIT_SHA for item in result.selections)


def test_collector_has_no_network_dependency(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted")
    ))
    assert collect_run(_fixture(tmp_path / "run")).run_id == "SHADOW_TEST"


def test_snapshot_lineage_mismatch_fails_closed(tmp_path):
    root = _fixture(tmp_path / "run")
    report_path = root / "artifacts" / "final_report.json"
    report = json.loads(report_path.read_text())
    report["price_snapshot"]["price_snapshot_sha256"] = "b" * 64
    _write_json(report_path, report)
    # Refresh only the inventory to isolate semantic lineage validation.
    manifest = json.loads((root / "manifest.json").read_text())
    row = next(item for item in manifest["files"] if item["path"] == "artifacts/final_report.json")
    raw = report_path.read_bytes(); row.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    _write_json(root / "manifest.json", manifest)
    with pytest.raises(CollectionError, match="snapshot lineage"):
        collect_run(root)


def test_universe_membership_count_mismatch_fails_closed(tmp_path):
    root = _fixture(tmp_path / "run")
    funnel_path = root / "artifacts" / "pre_research_funnel.json"
    funnel = json.loads(funnel_path.read_text())
    funnel["counts"]["universe"] = 5
    _write_json(funnel_path, funnel)
    manifest = json.loads((root / "manifest.json").read_text())
    row = next(item for item in manifest["files"] if item["path"] == "artifacts/pre_research_funnel.json")
    raw = funnel_path.read_bytes(); row.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    _write_json(root / "manifest.json", manifest)
    with pytest.raises(CollectionError, match="universe identity"):
        collect_run(root)


def test_source_artifact_mutation_is_rejected_by_manifest(tmp_path):
    root = _fixture(tmp_path / "run")
    (root / "artifacts" / "setups.json").write_text("[]", encoding="utf-8")
    with pytest.raises(CollectionError, match="inventoried artifact mismatch"):
        collect_run(root)


def test_collector_never_mutates_source(tmp_path):
    root = _fixture(tmp_path / "run")
    before = {p.relative_to(root): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file()}
    collect_run(root)
    after = {p.relative_to(root): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file()}
    assert before == after
