from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path

import pytest

from tests.trinity.validation.test_stock_observation import (
    _raw, _sessions, _terminal_wbd_raw,
)
from trinity.shadow.r2 import (
    R2Error,
    archive_stock_observation_snapshot,
    restore_stock_observation_snapshot,
    stock_observation_prefix,
)
from trinity.twelvedata_prices import (
    DAILY_CREDIT_RESERVE,
    HISTORY_CALENDAR_DAYS,
    RATE_CREDITS,
    RATE_WINDOW_SECONDS,
    ApiUsageCapture,
    ProviderCapture,
    canonical_bytes,
    required_daily_credits,
    snapshot_hash,
)
from trinity.validation.baseline_v1 import BASELINE_HASH
from trinity.validation.market_calendar import session_times
from trinity.validation.protocol_v1 import PROTOCOL_HASH
from trinity.validation.stock_observation import (
    StockObservationError,
    expected_ticker_set_sha256,
    load_stock_observation_snapshot,
)
from trinity.validation.stock_observation_acquisition import (
    StockObservationAcquisitionError,
    acquire_stock_observation,
    required_subset_daily_credits,
    stock_observation_identity,
)


TARGET = date(2026, 10, 9)
EXPECTATION_SHA = "e" * 64
EXPECTED_PROTOCOL_HASH = "c04e4009962e2d93265b725e0c3213000151202e8ecd3dd45e60cf9f92ed59d3"
EXPECTED_BASELINE_HASH = "2c67a7375e27dbd8eebaef3b14bec8f057fc773083db59242e305117b2fac57a"


def _eligible_time(target: date = TARGET) -> datetime:
    return session_times(target.isoformat())[1] + timedelta(minutes=20)


class Clock:
    def __init__(self, started: datetime):
        self.started = started
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.started if self.calls == 1 else self.started + timedelta(seconds=5)


class FakeProvider:
    def __init__(
        self, raws: dict[str, bytes], *, credits: int = 1000,
        requested_at: datetime | None = None, api_key: str = "TEST_SECRET",
    ):
        self.raws = raws
        self.daily_remaining = credits
        self.requested_at = requested_at or (_eligible_time() + timedelta(seconds=1))
        self.api_key = api_key
        self.credit_events = []
        self.fetches = []
        self.usage_calls = 0

    def fetch_api_usage(self):
        self.usage_calls += 1
        return ApiUsageCapture(
            usage={"daily_usage": 1, "plan_daily_limit": 1000},
            daily_usage=1, plan_daily_limit=1000, daily_remaining=self.daily_remaining,
            retrieved_at_utc=self.requested_at.isoformat(), response_headers={},
        )

    def fetch(self, symbol, *, start_date, target_session, remaining_tickers=None):
        self.fetches.append((symbol, start_date, target_session, remaining_tickers))
        self.credit_events.append({
            "endpoint": "/time_series", "symbol": symbol,
            "retrieved_at_utc": self.requested_at.isoformat(), "http_status": 200,
        })
        return ProviderCapture(self.raws[symbol], ({
            "attempt": 1, "requested_at_utc": self.requested_at.isoformat(),
            "http_status": 200, "elapsed_seconds": 0.1,
            "provider_code": None, "provider_message": None,
        },))


class FakeR2:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.head_calls: list[str] = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        del bucket
        self.objects[key] = (Path(filename).read_bytes(), dict((ExtraArgs or {}).get("Metadata", {})))

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        del Bucket, ContinuationToken
        return {"Contents": [
            {"Key": key, "Size": len(raw)}
            for key, (raw, _meta) in sorted(self.objects.items()) if key.startswith(Prefix)
        ], "IsTruncated": False}

    def head_object(self, *, Bucket, Key):
        del Bucket
        self.head_calls.append(Key)
        raw, metadata = self.objects[Key]
        return {"ContentLength": len(raw), "Metadata": metadata}

    def get_object(self, *, Bucket, Key):
        del Bucket
        return {"Body": BytesIO(self.objects[Key][0])}


def _provider(tickers=("AAPL",), *, target: date = TARGET, **kwargs):
    dates = _sessions(target)
    raws = {
        ticker: _terminal_wbd_raw(target) if ticker == "WBD" else _raw(ticker, dates)
        for ticker in tickers
    }
    requested_at = kwargs.pop("requested_at", _eligible_time(target) + timedelta(seconds=1))
    return FakeProvider(raws, requested_at=requested_at, **kwargs)


def _acquire(root: Path, tickers=("AAPL",), *, target: date = TARGET,
             provider=None, attempt=1, started=None):
    provider = provider or _provider(tickers, target=target)
    return acquire_stock_observation(
        target_session=target, expected_tickers=tickers,
        expectation_set_sha256=EXPECTATION_SHA, output_root=root,
        acquisition_attempt=attempt, client=provider,
        now=Clock(started or _eligible_time(target)),
    )


def test_identity_normalizes_exact_subset_and_has_no_cycle_time_or_path():
    first = stock_observation_identity(
        target_session=TARGET, expectation_set_sha256=EXPECTATION_SHA,
        expected_tickers=(" msft ", "aapl"), acquisition_attempt=1,
    )
    replay = stock_observation_identity(
        target_session=TARGET, expectation_set_sha256=EXPECTATION_SHA,
        expected_tickers=("AAPL", "MSFT"), acquisition_attempt=1,
    )
    second = stock_observation_identity(
        target_session=TARGET, expectation_set_sha256=EXPECTATION_SHA,
        expected_tickers=("AAPL", "MSFT"), acquisition_attempt=2,
    )
    assert first == replay and first["snapshot_id"] != second["snapshot_id"]
    assert first["expected_tickers"] == ("AAPL", "MSFT")
    assert first["expected_ticker_set_sha256"] == expected_ticker_set_sha256(("MSFT", "AAPL"))
    serialized = canonical_bytes(first["identity_core"])
    assert b"observation_cycle" not in serialized and b"20:" not in serialized
    assert b"C:\\" not in serialized and b"/tmp" not in serialized


@pytest.mark.parametrize("tickers", [(), ("AAPL", "aapl"), ("BAD TICKER",), (1,)])
def test_invalid_or_duplicate_ticker_sets_fail_closed(tickers):
    with pytest.raises((StockObservationError, StockObservationAcquisitionError)):
        stock_observation_identity(
            target_session=TARGET, expectation_set_sha256=EXPECTATION_SHA,
            expected_tickers=tickers, acquisition_attempt=1,
        )


@pytest.mark.parametrize("sha,attempt", [("bad", 1), ("e" * 64, 0), ("e" * 64, True)])
def test_invalid_expectation_sha_or_attempt_fails_closed(sha, attempt):
    with pytest.raises(StockObservationAcquisitionError):
        stock_observation_identity(
            target_session=TARGET, expectation_set_sha256=sha,
            expected_tickers=("AAPL",), acquisition_attempt=attempt,
        )


def test_exact_multi_ticker_acquisition_reuses_provider_contract_and_persists_evidence(tmp_path):
    provider = _provider(("MSFT", "AAPL"))
    manifest = _acquire(tmp_path, (" msft ", "aapl"), provider=provider)
    assert [item[0] for item in provider.fetches] == ["AAPL", "MSFT"]
    assert all(item[1] == TARGET - timedelta(days=HISTORY_CALENDAR_DAYS) for item in provider.fetches)
    assert all(item[2] == TARGET for item in provider.fetches)
    assert manifest["interval"] == "1day" and manifest["adjust"] == "none"
    assert manifest["expected_ticker_count"] == manifest["canonical_ticker_count"] == 2
    assert manifest["coverage_gate"]["ready"] is True
    for ticker in ("AAPL", "MSFT"):
        entry = next(item for item in manifest["tickers"] if item["canonical_ticker"] == ticker)
        assert (tmp_path / "raw" / f"{ticker}.json").is_file()
        assert (tmp_path / "normalized" / f"{ticker}.json").is_file()
        assert entry["raw_sha256"] == hashlib.sha256(
            (tmp_path / "raw" / f"{ticker}.json").read_bytes()
        ).hexdigest()
        assert entry["content_sha256"] == hashlib.sha256(
            (tmp_path / "normalized" / f"{ticker}.json").read_bytes()
        ).hexdigest()
    loaded = load_stock_observation_snapshot(
        tmp_path, expected_tickers=("AAPL", "MSFT"),
        expected_observation_target_session=TARGET.isoformat(),
    )
    assert loaded.expected_tickers == ("AAPL", "MSFT")


def test_registry_is_never_consulted(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "trinity.usa_issuer_registry.load_registry",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("registry consulted")),
    )
    assert _acquire(tmp_path, ("ZZZZ",), provider=_provider(("ZZZZ",)))[
        "expected_ticker_count"
    ] == 1


@pytest.mark.parametrize("offset,allowed", [(timedelta(minutes=19, seconds=59), False),
                                               (timedelta(minutes=20), True)])
def test_finality_boundary_blocks_before_provider(offset, allowed, tmp_path):
    close = session_times(TARGET.isoformat())[1]
    provider = _provider()
    if allowed:
        _acquire(tmp_path, provider=provider, started=close + offset)
        assert provider.usage_calls == 1
    else:
        with pytest.raises(StockObservationAcquisitionError, match="not final"):
            _acquire(tmp_path, provider=provider, started=close + offset)
        assert provider.usage_calls == 0


@pytest.mark.parametrize("target", [date(2026, 3, 9), date(2026, 11, 2), date(2026, 11, 27)])
def test_finality_uses_xnys_edt_est_and_early_close(target, tmp_path):
    manifest = _acquire(tmp_path, target=target, provider=_provider(target=target))
    assert manifest["target_market_session"] == target.isoformat()


def test_naive_or_reversed_acquisition_clock_fails(tmp_path):
    with pytest.raises(StockObservationAcquisitionError, match="timezone-aware"):
        _acquire(tmp_path / "naive", started=datetime(2026, 10, 9, 20, 20))

    class ReversedClock:
        def __init__(self): self.calls = 0
        def __call__(self):
            self.calls += 1
            return _eligible_time() if self.calls == 1 else _eligible_time() - timedelta(seconds=1)

    with pytest.raises(StockObservationAcquisitionError, match="reversed"):
        acquire_stock_observation(
            target_session=TARGET, expected_tickers=("AAPL",),
            expectation_set_sha256=EXPECTATION_SHA, output_root=tmp_path / "reversed",
            client=_provider(), now=ReversedClock(),
        )


def test_attempt_before_finality_and_provider_symbol_mismatch_fail_not_ready(tmp_path):
    early_provider = _provider(requested_at=_eligible_time() - timedelta(seconds=1))
    with pytest.raises(StockObservationAcquisitionError, match="authoritative"):
        _acquire(tmp_path / "early", provider=early_provider)
    early_manifest = json.loads((tmp_path / "early" / "snapshot_manifest.json").read_bytes())
    assert early_manifest["coverage_gate"]["ready"] is False

    wrong = FakeProvider(
        {"AAPL": _raw("MSFT", _sessions(TARGET))}, requested_at=_eligible_time() + timedelta(seconds=1),
    )
    with pytest.raises(StockObservationAcquisitionError, match="NOT READY"):
        _acquire(tmp_path / "symbol", provider=wrong)
    wrong_manifest = json.loads((tmp_path / "symbol" / "snapshot_manifest.json").read_bytes())
    assert wrong_manifest["coverage_gate"]["ready"] is False


def test_secret_is_never_persisted_and_snapshot_is_not_ready(tmp_path):
    contaminated = canonical_bytes({
        "meta": {"symbol": "AAPL", "interval": "1day"},
        "values": [], "message": "TEST_SECRET",
    })
    provider = FakeProvider(
        {"AAPL": contaminated}, requested_at=_eligible_time() + timedelta(seconds=1),
    )
    with pytest.raises(StockObservationAcquisitionError, match="NOT READY"):
        _acquire(tmp_path, provider=provider)
    persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert b"TEST_SECRET" not in persisted
    assert not (tmp_path / "raw" / "AAPL.json").exists()


def test_subset_credit_guard_is_exact_and_full_universe_floor_is_unchanged(tmp_path):
    assert required_subset_daily_credits(1) == 33
    assert required_subset_daily_credits(3) == 35
    assert DAILY_CREDIT_RESERVE == 32
    assert required_daily_credits(1) == 550
    provider = _provider(credits=32)
    with pytest.raises(StockObservationAcquisitionError, match="insufficient"):
        _acquire(tmp_path, provider=provider)
    assert provider.fetches == []
    assert RATE_CREDITS == 7 and RATE_WINDOW_SECONDS == 61.0


def test_identity_core_tampering_fails_even_after_snapshot_rehash(tmp_path):
    _acquire(tmp_path)
    path = tmp_path / "snapshot_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["identity_core"]["acquisition_attempt"] = 9
    manifest["snapshot_sha256"] = snapshot_hash(manifest)
    path.write_bytes(canonical_bytes(manifest))
    with pytest.raises(StockObservationError, match="deterministic identity"):
        load_stock_observation_snapshot(
            tmp_path, expected_tickers=("AAPL",),
            expected_observation_target_session=TARGET.isoformat(),
        )


def test_r2_roundtrip_restores_raw_normalized_telemetry_and_authoritative_load(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    manifest = _acquire(source, ("AAPL", "MSFT"), provider=_provider(("AAPL", "MSFT")))
    r2 = FakeR2()
    archived = archive_stock_observation_snapshot(r2, "bucket", source)
    assert "latest" not in archived["prefix"].lower()
    assert manifest["snapshot_id"] in archived["prefix"]
    assert archived["object_count"] == 6
    result = restore_stock_observation_snapshot(
        r2, "bucket", archived["prefix"], restored,
        expected_snapshot_id=manifest["snapshot_id"],
        expected_snapshot_sha256=manifest["snapshot_sha256"],
        expected_target_session=TARGET.isoformat(),
        expected_tickers=("MSFT", "AAPL"),
        expected_expectation_set_sha256=EXPECTATION_SHA,
    )
    loaded = load_stock_observation_snapshot(
        restored, expected_tickers=("AAPL", "MSFT"),
        expected_observation_target_session=TARGET.isoformat(),
    )
    assert result["restored_object_count"] == 6
    assert loaded.source_snapshot_id == manifest["snapshot_id"]
    assert loaded.source_snapshot_sha256 == manifest["snapshot_sha256"]
    assert loaded.expected_tickers == ("AAPL", "MSFT")
    assert loaded.expected_ticker_set_sha256 == manifest["expected_ticker_set_sha256"]
    assert r2.head_calls


def test_r2_idempotency_collision_unexpected_and_restore_identity_fail_closed(tmp_path):
    source = tmp_path / "source"
    manifest = _acquire(source)
    r2 = FakeR2()
    first = archive_stock_observation_snapshot(r2, "bucket", source)
    assert archive_stock_observation_snapshot(r2, "bucket", source) == first
    raw_key = first["prefix"] + "raw/AAPL.json"
    original = r2.objects[raw_key]
    r2.objects[raw_key] = (original[0] + b"x", original[1])
    with pytest.raises(R2Error, match="collision"):
        archive_stock_observation_snapshot(r2, "bucket", source)
    r2.objects[raw_key] = original
    r2.objects[first["prefix"] + "unexpected.json"] = (b"x", {"sha256": hashlib.sha256(b"x").hexdigest()})
    with pytest.raises(R2Error, match="unexpected"):
        archive_stock_observation_snapshot(r2, "bucket", source)
    del r2.objects[first["prefix"] + "unexpected.json"]
    common = dict(
        client=r2, bucket="bucket", prefix=first["prefix"],
        expected_snapshot_id=manifest["snapshot_id"],
        expected_snapshot_sha256=manifest["snapshot_sha256"],
        expected_target_session=TARGET.isoformat(), expected_tickers=("AAPL",),
        expected_expectation_set_sha256=EXPECTATION_SHA,
    )
    with pytest.raises(R2Error):
        restore_stock_observation_snapshot(
            destination=tmp_path / "wrong", **{**common, "expected_snapshot_id": "WRONG"},
        )
    r2.objects[raw_key] = (original[0] + b"corrupt", original[1])
    with pytest.raises(R2Error, match="integrity"):
        restore_stock_observation_snapshot(destination=tmp_path / "corrupt", **common)


def test_r2_missing_raw_or_normalized_and_wrong_bindings_fail_closed(tmp_path):
    source = tmp_path / "source"
    manifest = _acquire(source)
    for missing in ("raw/AAPL.json", "normalized/AAPL.json"):
        r2 = FakeR2()
        archived = archive_stock_observation_snapshot(r2, "bucket", source)
        del r2.objects[archived["prefix"] + missing]
        with pytest.raises(R2Error, match="closure"):
            restore_stock_observation_snapshot(
                r2, "bucket", archived["prefix"], tmp_path / missing.split("/")[0],
                expected_snapshot_id=manifest["snapshot_id"],
                expected_snapshot_sha256=manifest["snapshot_sha256"],
                expected_target_session=TARGET.isoformat(), expected_tickers=("AAPL",),
                expected_expectation_set_sha256=EXPECTATION_SHA,
            )
    r2 = FakeR2()
    archived = archive_stock_observation_snapshot(r2, "bucket", source)
    for field, value in (
        ("expected_snapshot_sha256", "f" * 64),
        ("expected_target_session", "2026-10-08"),
        ("expected_tickers", ("MSFT",)),
        ("expected_expectation_set_sha256", "f" * 64),
    ):
        kwargs = {
            "expected_snapshot_id": manifest["snapshot_id"],
            "expected_snapshot_sha256": manifest["snapshot_sha256"],
            "expected_target_session": TARGET.isoformat(),
            "expected_tickers": ("AAPL",),
            "expected_expectation_set_sha256": EXPECTATION_SHA,
        }
        kwargs[field] = value
        with pytest.raises(R2Error):
            restore_stock_observation_snapshot(
                r2, "bucket", archived["prefix"], tmp_path / f"wrong-{field}", **kwargs,
            )


def test_frozen_hashes_unchanged():
    assert PROTOCOL_HASH == EXPECTED_PROTOCOL_HASH
    assert BASELINE_HASH == EXPECTED_BASELINE_HASH
