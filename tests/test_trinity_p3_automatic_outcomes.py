from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path.cwd()))

from trinity.validation.collector import collect_run
from trinity.validation.outcome_expectations import OUTCOME_EXPECTATION
from trinity.validation.slot_assignment import (
    EXPECTATION_CYCLE_ASSIGNMENT,
    OBSERVATION_SLOT_CLOSURE,
)
from trinity.validation.storage import ValidationStorageConflict, ValidationStore
from trinity.shadow.r2 import archive_run


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


p3 = _load("trinity_p3_automatic_outcomes", ".github/scripts/trinity_p3_automatic_outcomes.py")
schedule = _load("trinity_p3_outcome_schedule", ".github/scripts/trinity_p3_outcome_schedule.py")

_collector_spec = importlib.util.spec_from_file_location(
    "p3_frozen_test_collector",
    Path.cwd() / "tests/trinity/validation/test_collector.py",
)
assert _collector_spec and _collector_spec.loader
_collector_tests = importlib.util.module_from_spec(_collector_spec)
_collector_spec.loader.exec_module(_collector_tests)
_fixture = _collector_tests._fixture


class MissingObject(Exception):
    response = {
        "Error": {"Code": "NoSuchKey"},
        "ResponseMetadata": {"HTTPStatusCode": 404},
    }


class ConditionalConflict(Exception):
    response = {
        "Error": {"Code": "PreconditionFailed"},
        "ResponseMetadata": {"HTTPStatusCode": 412},
    }


class FakeR2:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.puts = 0
        self.put_attempts = 0
        self._lock = threading.Lock()

    def get_object(self, *, Bucket, Key):
        del Bucket
        with self._lock:
            if Key not in self.objects:
                raise MissingObject(Key)
            body = self.objects[Key]
            metadata = dict(self.metadata.get(Key, {}))
        return {"Body": io.BytesIO(body), "Metadata": metadata}

    def head_object(self, *, Bucket, Key):
        del Bucket
        with self._lock:
            if Key not in self.objects:
                raise MissingObject(Key)
            return {
                "ContentLength": len(self.objects[Key]),
                "Metadata": dict(self.metadata.get(Key, {})),
            }

    def put_object(self, *, Bucket, Key, Body, Metadata=None, IfNoneMatch=None, **_kwargs):
        del Bucket
        raw = Body.read() if hasattr(Body, "read") else bytes(Body)
        with self._lock:
            self.put_attempts += 1
            if IfNoneMatch == "*" and Key in self.objects:
                raise ConditionalConflict(Key)
            self.objects[Key] = raw
            self.metadata[Key] = dict(Metadata or {})
            self.puts += 1

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        extra = dict(ExtraArgs or {})
        self.put_object(
            Bucket=Bucket,
            Key=Key,
            Body=Path(Filename).read_bytes(),
            Metadata=extra.get("Metadata"),
        )

    def list_objects_v2(self, *, Bucket, Prefix, **_kwargs):
        del Bucket
        with self._lock:
            keys = sorted(key for key in self.objects if key.startswith(Prefix))
            contents = [
                {"Key": key, "Size": len(self.objects[key])} for key in keys
            ]
        return {"Contents": contents, "IsTruncated": False}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _refresh_manifest(root: Path) -> None:
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        source = root / item["path"]
        raw = source.read_bytes()
        item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    _write_json(path, manifest)


def _official(root: Path) -> Path:
    run = _fixture(root, mode="OFFICIAL", official=True)
    orchestrator_path = run / "artifacts" / "weekly_orchestrator.json"
    orchestrator = json.loads(orchestrator_path.read_text(encoding="utf-8"))
    orchestrator.update({
        "method_version": p3.ORCHESTRATOR_METHOD,
        "intended_monday_europe_rome": "2026-10-05",
        "workflow_registration_commit": "d" * 40,
    })
    _write_json(orchestrator_path, orchestrator)
    restore_path = run / "artifacts" / "price_snapshot_restore.json"
    restore = json.loads(restore_path.read_text(encoding="utf-8"))
    restore["prefix"] = "prices/twelvedata/session=2026-10-09/run_id=fixture/"
    _write_json(restore_path, restore)
    _refresh_manifest(run)
    return run


def _set_run_id(run: Path, run_id: str) -> None:
    for relative in ("artifacts/final_report.json", "run_state.json"):
        path = run / relative
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["run_id"] = run_id
        _write_json(path, payload)
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = run_id
    _write_json(manifest_path, manifest)
    _refresh_manifest(run)


def _archive_and_mark(remote: FakeR2, run: Path) -> dict[str, int]:
    collected = collect_run(run)
    archived = archive_run(remote, "bucket", collected.run_id, run)
    orchestrator = json.loads(
        (run / "artifacts" / "weekly_orchestrator.json").read_text(encoding="utf-8")
    )
    restore = json.loads(
        (run / "artifacts" / "price_snapshot_restore.json").read_text(encoding="utf-8")
    )
    payload = {
        "schema": "trinity.weekly-cycle-completion.v1",
        "mode": "OFFICIAL",
        "method_version": p3.ORCHESTRATOR_METHOD,
        "intended_monday_europe_rome": orchestrator["intended_monday_europe_rome"],
        "target_market_session": orchestrator["target_market_session"],
        "analytical_commit": collected.analytical_commit_sha,
        "workflow_registration_commit": orchestrator["workflow_registration_commit"],
        "price_snapshot_id": collected.snapshot_id,
        "price_snapshot_sha256": collected.snapshot_sha256,
        "price_snapshot_r2_prefix": restore["prefix"],
        "analytical_run_id": collected.run_id,
        "analytical_archive": archived,
        "completed_at_utc": "2026-10-13T00:00:00Z",
    }
    body = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode()
    digest = hashlib.sha256(body).hexdigest()
    remote.put_object(
        Bucket="bucket",
        Key=p3.official_completion_marker_key(
            payload["intended_monday_europe_rome"], payload["target_market_session"],
        ),
        Body=body,
        Metadata={"sha256": digest},
        IfNoneMatch="*",
    )
    return archived


def test_official_ingestion_is_atomic_complete_and_idempotent(tmp_path):
    run = _official(tmp_path / "run")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        first = p3.ingest_official_run(store, run)
        counts = {
            kind: len(store.records(kind))
            for kind in (
                "RUN_OBSERVATION", "SELECTION", "SETUP", "EXECUTION",
                OUTCOME_EXPECTATION, p3.INGESTION_RECEIPT,
            )
        }
        second = p3.ingest_official_run(store, run)
        replay_counts = {kind: len(store.records(kind)) for kind in counts}
    assert first["eligibility"] == "OFFICIAL_ELIGIBLE"
    assert counts == replay_counts
    assert counts["RUN_OBSERVATION"] == counts[p3.INGESTION_RECEIPT] == 1
    assert counts[OUTCOME_EXPECTATION] == 18
    assert second["outcome_expectations_appended"] == 0


def test_validation_ingestion_is_rejected_without_mutation(tmp_path):
    run = _fixture(tmp_path / "validation")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        with pytest.raises(p3.AutomaticOutcomesError, match="OFFICIAL_ELIGIBLE"):
            p3.ingest_official_run(store, run)
        assert store.connection.execute("SELECT count(*) FROM validation_record").fetchone()[0] == 0


def test_divergent_official_replay_fails_closed(monkeypatch, tmp_path):
    run = _official(tmp_path / "run")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        p3.ingest_official_run(store, run)
        original = collect_run(run)
        monkeypatch.setattr(
            p3, "collect_run",
            lambda _root: replace(original, source_manifest_sha256="f" * 64),
        )
        with pytest.raises(ValidationStorageConflict):
            p3.ingest_official_run(store, run)
        assert len(store.records("RUN_OBSERVATION")) == 1


def test_no_mature_expectations_is_clean_noop_with_no_provider_call(tmp_path):
    run = _official(tmp_path / "run")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        p3.ingest_official_run(store, run)
        plan = p3.plan_observation_slot(
            store, iso_year=2026, iso_week=42,
            now_utc=datetime(2026, 10, 13, 8, tzinfo=timezone.utc),
        )
        class NeverProvider:
            def __getattr__(self, _name):
                raise AssertionError("provider must not be touched")
        result = p3.execute_observation_slot(
            store, plan=plan, provider=NeverProvider(), work_root=tmp_path / "work",
        )
        assert store.records(OBSERVATION_SLOT_CLOSURE) == []
        assert store.records(EXPECTATION_CYCLE_ASSIGNMENT) == []
    assert plan.status == result["status"] == p3.NO_MATURE_EXPECTATIONS


def test_exact_maturity_sets_progress_d5_d20_d60_deterministically(tmp_path):
    run = _official(tmp_path / "run")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        p3.ingest_official_run(store, run)
        d5 = p3.plan_observation_slot(
            store, iso_year=2026, iso_week=43,
            now_utc=datetime(2026, 10, 20, 8, tzinfo=timezone.utc),
        )
        d20 = p3.plan_observation_slot(
            store, iso_year=2026, iso_week=46,
            now_utc=datetime(2026, 11, 10, 8, tzinfo=timezone.utc),
        )
        d60 = p3.plan_observation_slot(
            store, iso_year=2027, iso_week=2,
            now_utc=datetime(2027, 1, 12, 8, tzinfo=timezone.utc),
        )
        d5_replay = p3.plan_observation_slot(
            store, iso_year=2026, iso_week=43,
            now_utc=datetime(2026, 10, 20, 9, tzinfo=timezone.utc),
        )
    assert {item.horizon_sessions for item in d5.expectations} == {5}
    assert {item.horizon_sessions for item in d20.expectations} == {5, 20}
    assert {item.horizon_sessions for item in d60.expectations} == {5, 20, 60}
    assert d5.expectation_set_sha256 == d5_replay.expectation_set_sha256
    assert tuple(item.key for item in d5.expectations) == tuple(sorted(
        item.key for item in d5.expectations
    ))


def test_quota_shortfall_fails_before_stock_or_spy_acquisition(tmp_path):
    run = _official(tmp_path / "run")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        p3.ingest_official_run(store, run)
        plan = p3.plan_observation_slot(
            store, iso_year=2026, iso_week=43,
            now_utc=datetime(2026, 10, 20, 8, tzinfo=timezone.utc),
        )
        calls: list[str] = []
        usage = type("Usage", (), {"daily_remaining": 1})()
        provider = type("Provider", (), {"fetch_api_usage": lambda self: usage})()
        with pytest.raises(p3.AutomaticOutcomesError, match="insufficient combined"):
            p3.execute_observation_slot(
                store,
                plan=plan,
                provider=provider,
                work_root=tmp_path / "work",
                stock_acquirer=lambda **_kw: calls.append("stock"),
                spy_acquirer=lambda **_kw: calls.append("spy"),
            )
    assert calls == []


def test_atomic_commit_roundtrip_and_divergent_object_collision(tmp_path):
    run = _official(tmp_path / "run")
    remote = FakeR2()
    _archive_and_mark(remote, run)
    puts_before_ingestion = remote.puts
    first = p3.ingest_official_run_r2(
        remote, "bucket", run_root=run, local_store=tmp_path / "first.sqlite3",
    )
    second = p3.ingest_official_run_r2(
        remote, "bucket", run_root=run, local_store=tmp_path / "second.sqlite3",
    )
    assert first["idempotent"] is False and second["idempotent"] is True
    assert remote.puts == puts_before_ingestion + 1
    key = first["authoritative_commit_key"]
    with pytest.raises(p3.AutomaticOutcomesError, match="collision"):
        p3.put_immutable(remote, "bucket", key, b"divergent")
    with ValidationStore(tmp_path / "restored.sqlite3") as restored:
        p3.hydrate_authoritative_store(remote, "bucket", restored)
        assert len(restored.records(OUTCOME_EXPECTATION)) == 18


def test_authoritative_receipt_is_deterministic_across_registration_retries(
    monkeypatch, tmp_path,
):
    run = _official(tmp_path / "run")
    receipts = []
    for index, sha in enumerate(("1" * 40, "2" * 40)):
        monkeypatch.setenv("GITHUB_SHA", sha)
        with ValidationStore(tmp_path / f"store-{index}.sqlite3") as store:
            p3.ingest_official_run(store, run)
            receipts.append(store.records(p3.INGESTION_RECEIPT))
    assert receipts[0] == receipts[1]
    assert "registration_commit_sha" not in receipts[0][0]


def test_official_ingestion_requires_completed_matching_r2_marker(tmp_path):
    run = _official(tmp_path / "run")
    remote = FakeR2()
    archive_run(remote, "bucket", collect_run(run).run_id, run)
    store_path = tmp_path / "store.sqlite3"
    with pytest.raises(p3.AutomaticOutcomesError, match="marker is missing"):
        p3.ingest_official_run_r2(
            remote, "bucket", run_root=run, local_store=store_path,
        )
    assert not store_path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("analytical_run_id", "SHADOW_OTHER"),
        ("price_snapshot_id", "other-snapshot"),
        ("analytical_commit", "e" * 40),
        ("target_market_session", "2026-10-10"),
    ],
)
def test_official_marker_lineage_mismatch_is_rejected_before_ingestion(
    field, value, tmp_path,
):
    run = _official(tmp_path / "run")
    remote = FakeR2()
    _archive_and_mark(remote, run)
    key = p3.official_completion_marker_key("2026-10-05", "2026-10-09")
    payload = json.loads(remote.objects[key])
    payload[field] = value
    raw = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode()
    remote.objects[key] = raw
    remote.metadata[key] = {"sha256": hashlib.sha256(raw).hexdigest()}
    store_path = tmp_path / "store.sqlite3"
    with pytest.raises(p3.AutomaticOutcomesError, match="lineage mismatch"):
        p3.ingest_official_run_r2(
            remote, "bucket", run_root=run, local_store=store_path,
        )
    assert not store_path.exists()


def test_put_immutable_serializes_two_concurrent_writers():
    remote = FakeR2()
    key = "immutable/concurrent.json"
    body = b'{"same":true}'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(
            lambda _index: p3.put_immutable(remote, "bucket", key, body),
            range(2),
        ))
    assert len(set(results)) == 1
    assert remote.put_attempts == 2
    assert remote.puts == 1
    assert remote.objects[key] == body
    with pytest.raises(p3.AutomaticOutcomesError, match="collision"):
        p3.put_immutable(remote, "bucket", key, b'{"same":false}')


def test_put_immutable_rejects_divergent_concurrent_writer():
    remote = FakeR2()
    key = "immutable/divergent-concurrent.json"
    bodies = (b'{"writer":1}', b'{"writer":2}')

    def write(body):
        try:
            return ("ok", p3.put_immutable(remote, "bucket", key, body))
        except p3.AutomaticOutcomesError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(write, bodies))
    assert sorted(status for status, _value in results) == ["error", "ok"]
    assert remote.puts == 1
    assert remote.objects[key] in bodies
    assert "collision" in next(value for status, value in results if status == "error")


def test_successful_workflow_without_official_archive_is_clean_noop(tmp_path):
    remote = FakeR2()
    result = p3.reconcile_official_run_r2(
        remote,
        "bucket",
        local_store=tmp_path / "store.sqlite3",
        restore_root=tmp_path / "restore",
        github_run_id="40432096545",
        github_run_attempt="1",
        allow_no_official_cycle=True,
    )
    assert result == {
        "status": p3.NO_OFFICIAL_CYCLE,
        "github_run_id": "40432096545",
        "github_run_attempt": "1",
        "records_appended": 0,
        "provider_calls": 0,
    }
    assert remote.puts == 0
    assert not (tmp_path / "store.sqlite3").exists()
    with pytest.raises(p3.AutomaticOutcomesError, match="found=0"):
        p3.reconcile_official_run_r2(
            remote,
            "bucket",
            local_store=tmp_path / "manual.sqlite3",
            restore_root=tmp_path / "manual-restore",
            github_run_id="40432096545",
            github_run_attempt="1",
        )


@pytest.mark.parametrize(
    ("started", "cron", "target", "runs"),
    [
        (datetime(2026, 7, 7, 4, tzinfo=timezone.utc), "0 4 * * 2", "2026-07-06", True),
        (datetime(2026, 9, 8, 4, tzinfo=timezone.utc), "0 4 * * 2", "2026-09-08", False),
        (datetime(2026, 9, 9, 4, tzinfo=timezone.utc), "0 4 * * 3", "2026-09-08", True),
        (datetime(2026, 3, 17, 5, tzinfo=timezone.utc), "0 5 * * 2", "2026-03-16", True),
        (datetime(2026, 10, 27, 5, tzinfo=timezone.utc), "0 5 * * 2", "2026-10-26", True),
    ],
)
def test_outcome_schedule_reuses_frozen_slot_and_dst_twins(started, cron, target, runs):
    decision = schedule.decide_outcome_schedule(
        event_schedule=cron, run_started_at=started,
    )
    assert decision.target_market_session == target
    assert decision.should_run is runs
    assert decision.target_market_session != "2026-09-04"


def test_runner_delay_does_not_retarget_outcome_slot():
    result = schedule.decide_outcome_schedule(
        event_schedule="0 5 * * 2",
        run_started_at=datetime(2026, 10, 27, 17, tzinfo=timezone.utc),
    )
    assert result.should_run is True
    assert result.target_market_session == "2026-10-26"


def test_workflows_are_fail_closed_and_feedback_is_separate_from_production():
    outcome = (ROOT / ".github/workflows/trinity-weekly-outcomes.yml").read_text()
    production = (ROOT / ".github/workflows/trinity-weekly-production.yml").read_text()
    reconciliation = (ROOT / ".github/workflows/trinity-official-feedback-reconciliation.yml").read_text()
    lower = outcome.lower()
    assert "trinity-prospective-outcomes-v1" in outcome
    assert "eb15c45b041b8890194d2f98b025aa98ed7fa527" in outcome
    assert "persist-credentials: false" in outcome
    assert "openai_api_key" not in lower and "eodhd_api_key" not in lower
    for forbidden in (
        "trinity.pilots.pre_research", "trinity.pilots.luna_triage",
        "trinity.pilots.multiticker", "notifications.telegram", "alpaca",
        "broker execution",
    ):
        assert forbidden not in lower
    archive = production.index("Archive and verify analytical run in R2")
    marker = production.index("Commit immutable OFFICIAL cycle marker")
    telegram = production.index("Deliver committed post-archive Telegram")
    assert archive < marker < telegram
    assert "Persist immutable OFFICIAL prospective cohort" not in production
    assert "workflow_run:" in reconciliation
    assert "reconcile-official-r2" in reconciliation
    assert "restore archived official run" in reconciliation.lower()
    assert "openai_api_key" not in reconciliation.lower()
    assert "eodhd_api_key" not in reconciliation.lower()
    assert '--run-id "${{ inputs.official_run_id }}"' not in reconciliation
    assert '--github-run-id "${{ github.event.workflow_run.id }}"' not in reconciliation
    assert "MANUAL_OFFICIAL_RUN_ID: ${{ inputs.official_run_id }}" in reconciliation
    assert "--allow-no-official-cycle" in reconciliation


def test_offline_suite_blocks_network(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("network attempted")),
    )
    remote = FakeR2()
    assert p3._list_keys(remote, "bucket", p3.NAMESPACE_ROOT) == ()


def test_no_mature_retry_persists_deterministic_scorecard_without_provider(tmp_path):
    remote = FakeR2()

    class NeverProvider:
        def __getattr__(self, _name):
            raise AssertionError("provider must not be touched")

    first = p3.run_outcomes_r2(
        remote,
        "bucket",
        iso_year=2026,
        iso_week=42,
        local_store=tmp_path / "first.sqlite3",
        work_root=tmp_path / "first-work",
        provider=NeverProvider(),
        now=lambda: datetime(2026, 10, 20, 8, tzinfo=timezone.utc),
    )
    second = p3.run_outcomes_r2(
        remote,
        "bucket",
        iso_year=2026,
        iso_week=42,
        local_store=tmp_path / "second.sqlite3",
        work_root=tmp_path / "second-work",
        provider=NeverProvider(),
        now=lambda: datetime(2026, 10, 20, 8, tzinfo=timezone.utc),
    )
    assert first["status"] == second["status"] == p3.NO_MATURE_EXPECTATIONS
    assert first["scorecard"]["prefix"] == second["scorecard"]["prefix"]
    assert any("/scorecards/" in key for key in remote.objects)


def test_scorecard_artifact_is_deterministic_and_covers_all_horizons(tmp_path):
    run = _official(tmp_path / "run")
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        p3.ingest_official_run(store, run)
        first = p3.build_scorecard_artifact(store)
        second = p3.build_scorecard_artifact(store)
    assert first == second
    assert set(first["horizons"]) == {"5", "20", "60"}
    assert first["official_run_count"] == 1
    assert first["horizons"]["20"]["cohorts"]["SCANNER"]["n_pending"] == 1
    assert first["selection_error_diagnostics"]["missed_winners_by_luna"] == []


def test_archived_official_run_discovery_and_safe_restore(tmp_path):
    remote = FakeR2()
    run_id = "SHADOW_USA_2026-10-26_40432096545_A1"
    prefix = f"runs/shadow/run_id={run_id}/"
    remote.objects[prefix + "artifacts/run_state.json"] = b'{"state":"COMPLETE"}'
    remote.objects[prefix + "artifacts/artifacts/final_report.json"] = b"{}"
    discovered = p3.discover_archived_official_run(
        remote,
        "bucket",
        github_run_id="40432096545",
        github_run_attempt="1",
    )
    restored = p3.restore_archived_run(
        remote,
        "bucket",
        run_id=discovered,
        destination=tmp_path / "restored",
    )
    assert discovered == run_id
    assert (restored / "run_state.json").read_bytes() == b'{"state":"COMPLETE"}'
    assert (restored / "artifacts/final_report.json").read_bytes() == b"{}"


def test_real_archive_run_format_restores_and_ingests_end_to_end(tmp_path):
    remote = FakeR2()
    run_id = "SHADOW_USA_2026-10-09_40432096545_A1"
    source = _official(tmp_path / "source")
    _set_run_id(source, run_id)
    archived = _archive_and_mark(remote, source)
    result = p3.reconcile_official_run_r2(
        remote,
        "bucket",
        local_store=tmp_path / "store.sqlite3",
        restore_root=tmp_path / "restored",
        github_run_id="40432096545",
        github_run_attempt="1",
    )
    assert archived["object_count"] > 0
    assert result["restored_run_id"] == run_id
    assert result["idempotent"] is False
    assert result["official_completion_marker"]["sha256"]
    with ValidationStore(tmp_path / "store.sqlite3") as store:
        assert len(store.records("SELECTION")) == 4
        assert len(store.records(OUTCOME_EXPECTATION)) == 18


def test_selection_diagnostics_separate_pipeline_and_sol_rejections(monkeypatch):
    run = SimpleNamespace(
        eligibility="OFFICIAL_ELIGIBLE",
        run_id="RUN",
        decision_target_session="2026-10-09",
        scanner_tickers=("AAA", "BBB", "CCC"),
        luna_tickers=("AAA", "BBB", "CCC"),
        sol_tickers=(),
    )
    finals = [
        SimpleNamespace(
            expectation_key=p3.outcome_identity("RUN", ticker, "SCANNER", 20),
            status=p3.COMPLETE,
            active_return_pct=10,
        )
        for ticker in run.scanner_tickers
    ]
    payloads = {
        "AAA": {"run_id": "RUN", "ticker": "AAA", "sol_evaluated": True,
                "sol_status": "REJECTED"},
        "BBB": {"run_id": "RUN", "ticker": "BBB", "sol_evaluated": False,
                "sol_status": "NOT_EVALUATED"},
        "CCC": {"run_id": "RUN", "ticker": "CCC", "sol_evaluated": True,
                "sol_status": "EVALUATED_FAILED"},
    }
    store = SimpleNamespace(record_items=lambda kind: [
        (ticker, payload) for ticker, payload in payloads.items()
    ] if kind == "SELECTION" else [])
    monkeypatch.setattr(p3, "load_run_observations", lambda _store: (run,))
    monkeypatch.setattr(p3, "load_current_expectations", lambda _store: ())
    monkeypatch.setattr(p3, "load_outcome_finals", lambda _store, _expectations: finals)
    result = p3._selection_error_diagnostics(store)
    assert [item["ticker"] for item in result["pipeline_not_promoted_winners"]] \
        == ["AAA", "BBB", "CCC"]
    assert [item["ticker"] for item in result["sol_evaluated_rejected_winners"]] \
        == ["AAA"]
    assert [item["ticker"] for item in result["not_routed_to_sol_winners"]] \
        == ["BBB"]
    assert [item["ticker"] for item in result["sol_evaluation_failed_winners"]] \
        == ["CCC"]
