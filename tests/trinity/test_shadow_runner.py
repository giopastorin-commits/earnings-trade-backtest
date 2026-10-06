from __future__ import annotations

from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path

import pytest

from trinity.paths import LEGACY_BASELINE_ROOT, baseline_root, price_cache, run_root
from trinity.shadow.cutoff import cutoff_for_verified_session, filter_timestamped_records
from trinity.shadow.manifest import generate_manifest, verify_manifest
from trinity.shadow.model_adapters import ResponsesSolProvider
from trinity.shadow.r2 import R2Error, archive_run, restore_baseline
from trinity.shadow.responses import CostGuardStop, PersistentCostLedger, ResponsesAPI, ResponsesTransportError
from trinity.shadow.sqlite_lifecycle import close_and_verify, initialize_fresh_ledger
from trinity.shadow.state import RunState


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
           "TRINITY_RUN_ROOT": str(tmp_path / "runs")}
    assert price_cache(env) == tmp_path / "baseline" / "eodhd_prices_518_daily_20220101_20260913_v1" / "provider_raw"
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
