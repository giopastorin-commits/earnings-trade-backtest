from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trinity.shadow.live_preflight import (
    LivePreflightError,
    PROBE_BYTES,
    run_live_preflight,
)
from trinity.twelvedata_prices import required_daily_credits


ANALYTICAL_SHA = "a" * 40


def _env(**overrides: str) -> dict[str, str]:
    values = {
        "TRINITY_R2_ENDPOINT": "https://unit.invalid",
        "TRINITY_R2_ACCESS_KEY_ID": "access-secret",
        "TRINITY_R2_SECRET_ACCESS_KEY": "r2-secret",
        "TRINITY_R2_BUCKET": "trinity-raw",
        "TRINITY_BASELINE_PREFIX": "baselines/shadow/version=1/",
        "TWELVEDATA_API_KEY": "td-secret",
        "OPENAI_API_KEY": "openai-secret",
        "EODHD_API_KEY": "eodhd-secret",
        "TRINITY_ANALYTICAL_REF": ANALYTICAL_SHA,
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "2",
    }
    values.update(overrides)
    return values


class _R2:
    def __init__(self, *, fail_read=False, fail_write=False, fail_cleanup=False):
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.fail_cleanup = fail_cleanup
        self.deleted: list[str] = []

    def list_objects_v2(self, **_kwargs):
        if self.fail_read:
            raise RuntimeError("read denied")
        return {"Contents": []}

    def put_object(self, *, Key, Body, Metadata, **_kwargs):
        if self.fail_write:
            raise RuntimeError("write denied")
        self.objects[Key] = (bytes(Body), dict(Metadata))

    def head_object(self, *, Key, **_kwargs):
        body, metadata = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": metadata}

    def get_object(self, *, Key, **_kwargs):
        body, _metadata = self.objects[Key]
        return {"Body": io.BytesIO(body)}

    def delete_object(self, *, Key, **_kwargs):
        if self.fail_cleanup:
            raise RuntimeError("delete denied")
        self.deleted.append(Key)
        del self.objects[Key]


def _restore(_client, _bucket, _prefix, destination):
    path = Path(destination) / "sec_compact" / "AAA" / "sec" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"companies": {"AAA": {"company": "AAA Corp", "documents": []}}}))
    return {"files": [{"relative_path": "sec_compact/AAA/sec/manifest.json"}]}


def _td(remaining: int):
    class Client:
        def __init__(self, *, api_key):
            assert api_key == "td-secret"

        def fetch_api_usage(self):
            return SimpleNamespace(daily_remaining=remaining)

    return Client


def _run(tmp_path, *, env=None, r2=None, restore=_restore, credits=550):
    client = r2 or _R2()
    result = run_live_preflight(
        tmp_path / "preflight",
        environ=env or _env(),
        r2_factory=lambda: (client, "trinity-raw"),
        baseline_restorer=restore,
        twelvedata_factory=_td(credits),
    )
    return result, client


@pytest.mark.parametrize("missing", [
    "TRINITY_R2_ENDPOINT", "TRINITY_R2_ACCESS_KEY_ID", "TRINITY_R2_SECRET_ACCESS_KEY",
    "TRINITY_BASELINE_PREFIX", "TWELVEDATA_API_KEY", "OPENAI_API_KEY", "EODHD_API_KEY",
])
def test_missing_required_configuration_fails_before_external_access(tmp_path, missing):
    env = _env()
    env[missing] = ""
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError

    with pytest.raises(LivePreflightError, match="missing required configuration"):
        run_live_preflight(tmp_path / "gate", environ=env, r2_factory=factory)
    assert not called


def test_success_verifies_r2_baseline_sec_and_full_universe_quota(tmp_path):
    result, client = _run(tmp_path, credits=550)
    assert result.config_ready and result.r2_ready and result.baseline_ready
    assert result.twelvedata_quota_ready
    assert result.required_daily_credits == required_daily_credits(518) == 550
    assert result.available_daily_credits == 550
    assert result.analytical_sha == ANALYTICAL_SHA
    key = "preflight/github_run_id=12345/attempt=2/probe.bin"
    assert client.deleted == [key] and key not in client.objects
    assert hashlib.sha256(PROBE_BYTES).hexdigest()


@pytest.mark.parametrize("failure,pattern", [
    ({"fail_read": True}, "R2 read/write"),
    ({"fail_write": True}, "R2 read/write"),
    ({"fail_cleanup": True}, "cleanup failed"),
])
def test_r2_failure_blocks_readiness(tmp_path, failure, pattern):
    with pytest.raises(LivePreflightError, match=pattern):
        _run(tmp_path, r2=_R2(**failure))


def test_baseline_restore_failure_blocks_before_quota(tmp_path):
    td_called = False

    def restore(*_args):
        raise RuntimeError("missing baseline")

    class TD:
        def __init__(self, **_kwargs):
            nonlocal td_called
            td_called = True

    with pytest.raises(RuntimeError, match="missing baseline"):
        run_live_preflight(
            tmp_path / "gate", environ=_env(),
            r2_factory=lambda: (_R2(), "trinity-raw"),
            baseline_restorer=restore, twelvedata_factory=TD,
        )
    assert not td_called


def test_missing_or_unusable_sec_compact_blocks(tmp_path):
    def missing(_client, _bucket, _prefix, destination):
        Path(destination).mkdir(parents=True)
        return {}

    with pytest.raises(LivePreflightError, match="lacks sec_compact directory"):
        _run(tmp_path / "missing", restore=missing)

    def unusable(_client, _bucket, _prefix, destination):
        path = Path(destination) / "sec_compact" / "AAA" / "sec" / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"companies":{"BBB":{}}}')
        return {}

    with pytest.raises(LivePreflightError, match="invalid sec_compact evidence"):
        _run(tmp_path / "unusable", restore=unusable)


def test_credit_below_threshold_blocks_and_equal_threshold_passes(tmp_path):
    with pytest.raises(LivePreflightError, match="available=549 required=550"):
        _run(tmp_path / "low", credits=549)
    result, _client = _run(tmp_path / "equal", credits=550)
    assert result.twelvedata_quota_ready


def test_moving_ref_and_mutable_baseline_are_rejected_before_external_access(tmp_path):
    with pytest.raises(LivePreflightError, match="immutable Git SHA"):
        _run(tmp_path / "branch", env=_env(TRINITY_ANALYTICAL_REF="main"))
    with pytest.raises(LivePreflightError, match="immutable"):
        _run(
            tmp_path / "latest",
            env=_env(TRINITY_BASELINE_PREFIX="baselines/latest/"),
        )


def test_probe_namespace_is_isolated_from_production_prefixes(tmp_path):
    _result, client = _run(tmp_path)
    assert all(key.startswith("preflight/") for key in client.deleted)
    assert not any(
        key.startswith(prefix)
        for key in client.deleted
        for prefix in ("prices/", "runs/", "observations/", "orchestration/")
    )
