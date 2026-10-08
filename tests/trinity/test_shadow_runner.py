from __future__ import annotations

from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trinity.ledger import LedgerStorage
from trinity.paths import (
    LEGACY_BASELINE_ROOT, baseline_root, price_cache, price_snapshot_root, run_root,
)
from trinity.shadow import cli as shadow_cli
from trinity.shadow.cutoff import cutoff_for_verified_session, filter_timestamped_records
from trinity.shadow.manifest import generate_manifest, verify_manifest
from trinity.shadow.model_adapters import ResponsesSolProvider
from trinity.shadow.r2 import (
    R2Error, archive_price_snapshot, archive_run, restore_baseline,
    restore_price_snapshot,
)
from trinity.shadow.responses import CostGuardStop, PersistentCostLedger, ResponsesAPI, ResponsesTransportError
from trinity.shadow.sqlite_lifecycle import close_and_verify, initialize_fresh_ledger
from trinity.shadow.state import RunState
from trinity.twelvedata_prices import canonical_bytes, snapshot_hash


class _Response:
    status_code = 200

    def __init__(self, value):
        self.content = json.dumps(value).encode()


class _Session:
    def __init__(self, value):
        self.value = value
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return _Response(self.value)


def _envelope(text='{"ok":true}'):
    return {
        "id": "resp_test", "output": [{"content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                  "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 80},
                  "output_tokens_details": {"reasoning_tokens": 5}},
    }


def test_portable_paths_preserve_legacy_default_and_configured_layout(tmp_path):
    assert baseline_root({}) == LEGACY_BASELINE_ROOT
    env = {"TRINITY_BASELINE_ROOT": str(tmp_path / "baseline"),
           "TRINITY_RUN_ROOT": str(tmp_path / "runs"),
           "TRINITY_PRICE_SNAPSHOT_ROOT": str(tmp_path / "price_snapshot")}
    assert price_cache(env) == tmp_path / "baseline" / "eodhd_prices_518_daily_20220101_20260913_v1" / "provider_raw"
    assert price_snapshot_root(env) == tmp_path / "price_snapshot"
    assert run_root(env) == tmp_path / "runs"


def test_responses_adapter_uses_store_false_and_checkpoints_before_parse(tmp_path):
    session = _Session(_envelope("not-json"))
    ledger = PersistentCostLedger(tmp_path / "api_usage.json")
    api = ResponsesAPI(run_root=tmp_path, cost_ledger=ledger, api_key="unit-secret", session=session)
    with pytest.raises(ResponsesTransportError, match="output_text"):
        api.invoke(model="gpt-5.6-luna", role="LUNA_TRIAGE", ticker="XYZ", prompt="p",
                   schema={"type": "object"}, call_ordinal=1)
    raw = tmp_path / "model_responses" / "0001_XYZ_luna_triage.json"
    assert raw.is_file()
    assert ledger.read()["calls"][0]["response_id"] == "resp_test"
    assert ledger.read()["calls"][0]["calculated_cost_usd"] == "0.0000462"
    request = session.request[1]
    assert request["json"]["store"] is False
    persisted = raw.read_text() + (tmp_path / "api_usage.json").read_text()
    assert "unit-secret" not in persisted and "Authorization" not in persisted


def test_cost_guard_persists_and_stops_before_next_request(tmp_path):
    ledger = PersistentCostLedger(tmp_path / "usage.json", Decimal("5.00"))
    ledger.append({"calculated_cost_usd": "5.00"})
    with pytest.raises(CostGuardStop):
        ledger.assert_call_allowed()


def test_cutoff_consensus_and_post_cutoff_filtering():
    cutoff = cutoff_for_verified_session("2026-10-05", ["2026-10-05"] * 9 + ["2026-10-02"])
    assert cutoff.utc == "2026-10-05T20:00:00Z"
    kept, excluded = filter_timestamped_records(
        [{"date": "2026-10-05T19:59:00Z"}, {"date": "2026-10-05T20:01:00Z"}],
        cutoff.utc, fields=("date",),
    )
    assert len(kept) == 1 and excluded == 1
    with pytest.raises(ValueError, match="consensus"):
        cutoff_for_verified_session("2026-10-05", ["2026-10-05", "2026-10-02"])


def test_run_state_has_one_immutable_terminal_state(tmp_path):
    state = RunState(tmp_path / "state.json", "run")
    state.write("FAILED_DATA", "fixture")
    with pytest.raises(RuntimeError, match="terminal"):
        state.write("COMPLETE")


def test_manifest_excludes_sqlite_sidecars_and_verifies(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "db.sqlite3-wal").write_bytes(b"secret")
    manifest = generate_manifest(tmp_path, run_id="run")
    assert [item["path"] for item in manifest["files"]] == ["a.json"]
    verify_manifest(tmp_path, manifest)


class _R2:
    def __init__(self, existing=None, metadata=None):
        self.existing = existing or []
        self.metadata = metadata or {}
        self.uploaded = {}

    def list_objects_v2(self, **kwargs):
        return {"Contents": self.existing, "IsTruncated": False}

    def head_object(self, Bucket, Key):
        value = self.uploaded.get(Key) or self.metadata[Key]
        return {"ContentLength": value[0], "Metadata": {"sha256": value[1]}}

    def upload_file(self, path, bucket, key, ExtraArgs):
        raw = Path(path).read_bytes()
        self.uploaded[key] = (len(raw), ExtraArgs["Metadata"]["sha256"])


def test_r2_collision_protection_and_verified_upload(tmp_path):
    (tmp_path / "x.json").write_text("{}")
    bad = _R2(existing=[{"Key": "runs/shadow/run_id=r/artifacts/x.json"}],
              metadata={"runs/shadow/run_id=r/artifacts/x.json": (99, "bad")})
    with pytest.raises(R2Error, match="collision"):
        archive_run(bad, "trinity-raw", "r", tmp_path)
    good = _R2()
    result = archive_run(good, "trinity-raw", "r", tmp_path)
    assert result == {"object_count": 1, "total_bytes": 2}


def test_price_snapshot_archive_uses_immutable_session_and_run_namespace(tmp_path):
    bars = [{"date": "2026-10-07", "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 10.0}]
    normalized = canonical_bytes(bars)
    raw = b'{"provider":"raw"}'
    (tmp_path / "normalized").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "normalized" / "AAPL.json").write_bytes(normalized)
    (tmp_path / "raw" / "AAPL.json").write_bytes(raw)
    entry = {
        "canonical_ticker": "AAPL", "provider_symbol": "AAPL",
        "classification": "ACTIVE_COMPLETE", "first_date": "2026-10-07",
        "last_valid_date": "2026-10-07", "bar_count": 1,
        "content_sha256": hashlib.sha256(normalized).hexdigest(),
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "excluded_rows": [],
        "attempts": [], "retry_count": 0, "error": None,
    }
    manifest = {
        "schema_name": "trinity.twelvedata-price-snapshot", "schema_version": "1",
        "snapshot_id": "TWELVEDATA_2026-10-07_RUN_123-A1",
        "provider": "TWELVE_DATA", "interval": "1day", "adjust": "none",
        "target_market_session": "2026-10-07", "request_start_date": "2025-07-14",
        "request_end_date_exclusive": "2026-10-08",
        "acquisition_started_at_utc": "2026-10-08T00:00:00.000000Z",
        "acquisition_completed_at_utc": "2026-10-08T01:00:00.000000Z",
        "canonical_ticker_count": 1, "active_complete_count": 1,
        "explicit_exception_count": 0, "provider_failure_count": 0,
        "invalid_data_count": 0, "classification_counts": {"ACTIVE_COMPLETE": 1},
        "coverage_gate": {"ready": True, "allowed_states": [
            "ACTIVE_COMPLETE", "CORPORATE_ACTION_NO_LONGER_TRADING",
        ], "blocking_tickers": []},
        "throttle": {"maximum_credits": 7, "rolling_window_seconds": 61.0},
        "tickers": [entry],
    }
    manifest["snapshot_sha256"] = snapshot_hash(manifest)
    (tmp_path / "snapshot_manifest.json").write_bytes(canonical_bytes(manifest))
    client = _R2()
    result = archive_price_snapshot(
        client, "bucket", tmp_path, run_id="123-A1", enforce_canonical_universe=False,
    )
    assert result["prefix"] == "prices/twelvedata/session=2026-10-07/run_id=123-A1/"
    assert result["snapshot_sha256"] == manifest["snapshot_sha256"]
    assert all(key.startswith(result["prefix"]) for key in client.uploaded)


class _Body:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class _RestoreR2:
    def __init__(self, manifest, objects):
        self.manifest = manifest
        self.objects = objects

    def get_object(self, Bucket, Key):
        if Key.endswith("manifest.json"):
            return {"Body": _Body(self.manifest)}
        return {"Body": _Body(self.objects[Key])}


def test_restore_accepts_golden_manifest_schema_and_verifies_manifest_hash(tmp_path):
    payload = b"[]"
    manifest = json.dumps({
        "files": [{"relative_path": "prices/X.json", "size": len(payload),
                   "sha256": hashlib.sha256(payload).hexdigest()}],
        "file_count": 1, "total_bytes": len(payload),
    }, sort_keys=True).encode()
    prefix = "baseline/test/version=abc/"
    client = _RestoreR2(manifest, {prefix + "prices/X.json": payload})
    result = restore_baseline(
        client, "bucket", prefix, tmp_path,
        expected_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
    )
    assert (tmp_path / "prices/X.json").read_bytes() == payload
    assert (tmp_path / "manifest.json").read_bytes() == manifest
    assert result["restored_manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
    with pytest.raises(R2Error, match="manifest SHA-256"):
        restore_baseline(client, "bucket", prefix, tmp_path / "bad", expected_manifest_sha256="bad")


def test_restore_price_snapshot_requires_explicit_hash_and_verifies_normalized_bytes(tmp_path):
    from tests.trinity.test_twelvedata_prices import _manifest

    source = tmp_path / "source"
    manifest = _manifest(source, {"AAPL": "ACTIVE_COMPLETE"})
    manifest_raw = (source / "snapshot_manifest.json").read_bytes()
    normalized_raw = (source / "normalized" / "AAPL.json").read_bytes()
    prefix = "prices/twelvedata/session=2026-10-07/run_id=test/"
    client = _RestoreR2(manifest_raw, {
        prefix + "normalized/AAPL.json": normalized_raw,
    })
    restored = restore_price_snapshot(
        client, "bucket", prefix, tmp_path / "restored",
        expected_snapshot_sha256=manifest["snapshot_sha256"],
        enforce_canonical_universe=False,
    )
    assert restored["snapshot_id"] == manifest["snapshot_id"]
    with pytest.raises(R2Error, match="identity hash"):
        restore_price_snapshot(
            client, "bucket", prefix, tmp_path / "bad", expected_snapshot_sha256="bad",
            enforce_canonical_universe=False,
        )


def test_fresh_sqlite_is_closed_and_integral(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    initialize_fresh_ledger(database)
    close_and_verify(database)
    assert database.is_file()
    assert not Path(str(database) + "-wal").exists()


def test_responses_provider_provenance_is_additive(tmp_path):
    provider = ResponsesSolProvider.__new__(ResponsesSolProvider)
    # The class source and migration both preserve the historical tuple while
    # defining the new Responses tuple; existing migration tests exercise old readers.
    source = Path("trinity/ledger/migrations/0008_responses_api_provenance.sql").read_text()
    assert "OPENAI_CODEX_CLI" in source
    assert "OPENAI_RESPONSES_API" in source


def _configure_renamed_run(monkeypatch, tmp_path, production_runner):
    runs = tmp_path / "runs"
    monkeypatch.setenv("TRINITY_RUN_ROOT", str(runs))
    monkeypatch.setenv("TRINITY_BASELINE_ROOT", str(tmp_path / "baseline"))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-openai-key")
    monkeypatch.setenv("EODHD_API_KEY", "unit-eodhd-key")

    snapshot_root = tmp_path / "snapshot"
    normalized = snapshot_root / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "XYZ.json").write_text('[{"date":"2026-10-06"}]', encoding="utf-8")

    class Snapshot:
        target_session = "2026-10-06"
        normalized_root = normalized
        snapshot_id = "snap-test"
        snapshot_sha256 = "a" * 64
        manifest = {"tickers": [{
            "canonical_ticker": "XYZ", "classification": "ACTIVE_COMPLETE",
            "last_valid_date": "2026-10-06",
        }]}
        def provenance(self):
            return {
                "price_provider": "TWELVE_DATA", "price_snapshot_id": self.snapshot_id,
                "price_snapshot_session": self.target_session,
                "price_snapshot_sha256": self.snapshot_sha256,
            }

    snapshot = Snapshot()
    monkeypatch.setattr(shadow_cli, "ValidatedPriceSnapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(shadow_cli, "SnapshotPrices", lambda value: SimpleNamespace(snapshot=value))

    def fake_funnel(*, output_path, provider):
        preliminary_state = json.loads(
            (output_path.parent.parent / "run_state.json").read_text(encoding="utf-8")
        )
        assert preliminary_state["run_id"] == "SHADOW_USA_PENDING_123_A1"
        assert preliminary_state["state"] == "RUNNING"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"inputs": {}, "survivors": []}', encoding="utf-8")
        assert provider.snapshot is snapshot
        return SimpleNamespace(
            latest_completed_session="2026-10-06", counts={},
        )

    monkeypatch.setattr(shadow_cli, "run_funnel", fake_funnel)
    monkeypatch.setattr(shadow_cli, "run_production_funnel", production_runner)
    return runs, snapshot


def test_run_command_rebases_downstream_paths_after_real_directory_rename(
    monkeypatch, tmp_path,
):
    captured = {}

    def fail_before_triage(**kwargs):
        captured.update(kwargs)
        running = json.loads(
            (kwargs["funnel_path"].parent.parent / "run_state.json").read_text(
                encoding="utf-8",
            )
        )
        assert running["run_id"] == "SHADOW_USA_2026-10-06_123_A1"
        assert running["state"] == "RUNNING"
        raise RuntimeError("setup-stage fixture")

    runs, snapshot = _configure_renamed_run(monkeypatch, tmp_path, fail_before_triage)

    assert shadow_cli.run_command() == 1
    pending = runs / "SHADOW_USA_PENDING_123_A1"
    final = runs / "SHADOW_USA_2026-10-06_123_A1"
    expected_funnel = final / "artifacts" / "pre_research_funnel.json"
    expected_prices = snapshot.normalized_root
    assert not pending.exists()
    assert final.is_dir()
    assert captured["funnel_path"] == expected_funnel
    assert captured["price_dir"] == expected_prices
    assert expected_funnel.is_file()
    assert expected_prices.is_dir()
    assert pending not in captured["funnel_path"].parents
    assert pending not in captured["price_dir"].parents
    state = json.loads((final / "run_state.json").read_text(encoding="utf-8"))
    assert state["run_id"] == "SHADOW_USA_2026-10-06_123_A1"
    assert state["state"] == "FAILED_SETUP"


def test_run_command_labels_failure_after_triage_entry_as_failed_luna(
    monkeypatch, tmp_path,
):
    def enter_triage(**kwargs):
        return kwargs["triage_runner"](
            ready_entries=[{"ticker": "XYZ"}],
            funnel_path=kwargs["funnel_path"],
            output_path=kwargs["luna_output_path"],
            source_root=kwargs["luna_source_root"],
            expected_count=1,
        )

    runs, _snapshot = _configure_renamed_run(monkeypatch, tmp_path, enter_triage)
    monkeypatch.setattr(shadow_cli, "FreshTriageSources", lambda **kwargs: object())

    def fail_in_triage(**kwargs):
        raise RuntimeError("luna-stage fixture")

    monkeypatch.setattr(shadow_cli, "run_triage", fail_in_triage)

    assert shadow_cli.run_command() == 1
    final = runs / "SHADOW_USA_2026-10-06_123_A1"
    state = json.loads((final / "run_state.json").read_text(encoding="utf-8"))
    assert state["run_id"] == "SHADOW_USA_2026-10-06_123_A1"
    assert state["state"] == "FAILED_LUNA"


def test_successful_shadow_run_records_snapshot_in_final_report_and_ledger(
    monkeypatch, tmp_path,
):
    runs = tmp_path / "runs"
    normalized = tmp_path / "snapshot" / "normalized"
    normalized.mkdir(parents=True)

    class Snapshot:
        target_session = "2026-10-06"
        normalized_root = normalized
        snapshot_id = "TWELVEDATA_2026-10-06_RUN_123-A1"
        snapshot_sha256 = "b" * 64
        manifest = {"tickers": [{
            "canonical_ticker": "AAPL", "classification": "ACTIVE_COMPLETE",
            "last_valid_date": "2026-10-06",
        }]}
        def provenance(self):
            return {
                "price_provider": "TWELVE_DATA", "price_snapshot_id": self.snapshot_id,
                "price_snapshot_session": self.target_session,
                "price_snapshot_sha256": self.snapshot_sha256,
            }

    snapshot = Snapshot()
    monkeypatch.setenv("TRINITY_RUN_ROOT", str(runs))
    monkeypatch.setenv("TRINITY_BASELINE_ROOT", str(tmp_path / "baseline"))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-openai-key")
    monkeypatch.setattr(shadow_cli, "ValidatedPriceSnapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(shadow_cli, "SnapshotPrices", lambda value: SimpleNamespace(snapshot=value))
    monkeypatch.setattr(shadow_cli, "_git_head", lambda: "test-commit")

    def fake_funnel(*, output_path, provider):
        assert provider.snapshot is snapshot
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"inputs":{},"survivors":[]}', encoding="utf-8")
        return SimpleNamespace(
            latest_completed_session="2026-10-06", counts={"universe": 518},
        )

    result = SimpleNamespace(
        setups=(), failed_tickers=(), sol_result=None,
        operational_setup_count=0, no_setup_count=0,
        watch_tickers=(), escalate_tickers=(), drop_tickers=(),
    )
    monkeypatch.setattr(shadow_cli, "run_funnel", fake_funnel)
    monkeypatch.setattr(shadow_cli, "run_production_funnel", lambda **kwargs: result)

    assert shadow_cli.run_command() == 0
    final_root = runs / "SHADOW_USA_2026-10-06_123_A1"
    report = json.loads((final_root / "artifacts" / "final_report.json").read_text())
    assert report["price_snapshot"] == snapshot.provenance()
    with LedgerStorage.open(final_root / "ledger" / "trinity.sqlite3") as storage:
        count = storage.connection.execute(
            "SELECT count(*) FROM artifact "
            "WHERE artifact_kind='ledger.price-snapshot-provenance.v1'"
        ).fetchone()[0]
    assert count == 1


def test_archive_completion_preserves_canonical_run_id(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    canonical_id = "SHADOW_USA_2026-10-06_123_A1"
    final = runs / canonical_id
    final.mkdir(parents=True)
    RunState(final / "run_state.json", canonical_id)
    (runs / "current_run.json").write_text(
        json.dumps({"run_id": canonical_id, "path": str(final)}), encoding="utf-8",
    )
    client = _R2()
    monkeypatch.setenv("TRINITY_RUN_ROOT", str(runs))
    monkeypatch.setattr(
        shadow_cli, "client_from_environment", lambda: (client, "trinity-raw"),
    )

    assert shadow_cli.archive_command() == 0
    persisted = json.loads((final / "run_state.json").read_text(encoding="utf-8"))
    assert persisted["run_id"] == canonical_id
    assert persisted["state"] == "COMPLETE"
