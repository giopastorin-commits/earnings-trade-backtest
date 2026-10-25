from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import io
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path.cwd()))

from trinity.validation.collector import collect_run
from trinity.validation.outcome_expectations import OUTCOME_EXPECTATION
from trinity.validation.slot_assignment import (
    EXPECTATION_CYCLE_ASSIGNMENT,
    OBSERVATION_SLOT_CLOSURE,
)
from trinity.validation.storage import ValidationStorageConflict, ValidationStore


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
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeR2:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def get_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise MissingObject(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, **_kwargs):
        del Bucket
        self.puts += 1
        self.objects[Key] = bytes(Body)

    def list_objects_v2(self, *, Bucket, Prefix, **_kwargs):
        del Bucket
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


def _official(root: Path) -> Path:
    return _fixture(root, mode="OFFICIAL", official=True)


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
    first = p3.ingest_official_run_r2(
        remote, "bucket", run_root=run, local_store=tmp_path / "first.sqlite3",
    )
    second = p3.ingest_official_run_r2(
        remote, "bucket", run_root=run, local_store=tmp_path / "second.sqlite3",
    )
    assert first["idempotent"] is False and second["idempotent"] is True
    assert remote.puts == 1
    key = first["authoritative_commit_key"]
    with pytest.raises(p3.AutomaticOutcomesError, match="collision"):
        p3.put_immutable(remote, "bucket", key, b"divergent")
    with ValidationStore(tmp_path / "restored.sqlite3") as restored:
        p3.hydrate_authoritative_store(remote, "bucket", restored)
        assert len(restored.records(OUTCOME_EXPECTATION)) == 18


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


def test_workflows_are_fail_closed_model_free_and_production_marker_follows_ingestion():
    outcome = (ROOT / ".github/workflows/trinity-weekly-outcomes.yml").read_text()
    production = (ROOT / ".github/workflows/trinity-weekly-production.yml").read_text()
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
    ingestion = production.index("Persist immutable OFFICIAL prospective cohort")
    marker = production.index("Commit immutable OFFICIAL cycle marker")
    telegram = production.index("Deliver committed post-archive Telegram")
    assert archive < ingestion < marker < telegram
    ingestion_block = production[ingestion:marker]
    assert "mode == 'OFFICIAL'" in ingestion_block


def test_offline_suite_blocks_network(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("network attempted")),
    )
    remote = FakeR2()
    assert p3._list_keys(remote, "bucket", p3.NAMESPACE_ROOT) == ()
