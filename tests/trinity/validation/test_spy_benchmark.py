from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import socket

import exchange_calendars
import pytest

from trinity.twelvedata_prices import ProviderCapture, canonical_bytes
from trinity.usa_issuer_registry import DEFAULT_REGISTRY, REGISTRY
from trinity.validation.contracts import OFFICIAL_ELIGIBLE
from trinity.validation.outcomes import calculate_selection_outcome
from trinity.validation.protocol_v1 import PROTOCOL_EPOCH, PROTOCOL_HASH
from trinity.validation.spy_benchmark import (
    BenchmarkSnapshotError, _snapshot_hash, acquire_spy_benchmark,
    benchmark_snapshot_id, load_spy_benchmark_snapshot,
    verify_spy_benchmark_snapshot,
)


PROTOCOL_HASH_V1 = "38980fc7bdc0ab8912f23cbc8ce67325f0d0ffe896a8d29838fe81ed1d094a1a"
TARGET = date(2026, 10, 9)
RUN_ID = "OFFICIAL_2026-10-12_A1"


def _sessions(count: int = 25) -> list[str]:
    calendar = exchange_calendars.get_calendar("XNYS", start="2026-08-01", end=TARGET)
    return [item.date().isoformat() for item in calendar.sessions[-count:]]


def _provider_payload(*, sessions: list[str] | None = None, offset: float = 0.0,
                      extra: dict[str, object] | None = None) -> bytes:
    days = sessions or _sessions()
    values = []
    for index, session in enumerate(days):
        opened = 100.0 + offset + index
        values.append({
            "datetime": session, "open": str(opened), "high": str(opened + 2),
            "low": str(opened - 1), "close": str(opened + 1),
            "volume": str(1_000_000 + index),
        })
    payload: dict[str, object] = {
        "meta": {"symbol": "SPY", "interval": "1day"},
        "values": list(reversed(values)), "status": "ok",
    }
    if extra:
        payload.update(extra)
    return canonical_bytes(payload)


class FakeProvider:
    api_key = "fixture-secret"

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.calls: list[tuple[object, ...]] = []

    def fetch(self, symbol, *, start_date, target_session, remaining_tickers=None):
        self.calls.append((symbol, start_date, target_session, remaining_tickers))
        return ProviderCapture(self.raw, ({"attempt": 1, "http_status": 200},))


def _clock():
    values = iter((
        datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 12, 10, 0, 1, tzinfo=timezone.utc),
    ))
    return lambda: next(values)


def _acquire(root: Path, *, raw: bytes | None = None):
    provider = FakeProvider(raw or _provider_payload())
    manifest = acquire_spy_benchmark(
        target_session=TARGET, run_id=RUN_ID, output_root=root,
        client=provider, now=_clock(),
    )
    return manifest, provider


def _rewrite_manifest_and_ready(root: Path, manifest: dict[str, object]) -> None:
    manifest["benchmark_snapshot_sha256"] = _snapshot_hash(manifest)
    manifest_raw = canonical_bytes(manifest)
    (root / "benchmark_manifest.json").write_bytes(manifest_raw)
    marker = json.loads((root / "READY").read_bytes())
    marker["benchmark_snapshot_sha256"] = manifest["benchmark_snapshot_sha256"]
    marker["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    (root / "READY").write_bytes(canonical_bytes(marker))


def test_identity_contract_snapshot_id_and_loader(tmp_path):
    root = tmp_path / "benchmark"
    manifest, provider = _acquire(root)
    assert provider.calls[0][0] == "SPY"
    assert manifest["symbol"] == "SPY"
    assert manifest["universe_role"] == "BENCHMARK_ONLY"
    assert manifest["return_kind"] == "PRICE_RETURN"
    assert manifest["provider"] == "TWELVE_DATA"
    assert manifest["interval"] == "1day" and manifest["adjustment"] == "none"
    assert manifest["benchmark_snapshot_id"] == \
        "SPY_TWELVEDATA_2026-10-09_RUN_OFFICIAL_2026-10-12_A1"
    series = load_spy_benchmark_snapshot(root, trinity_target_session=TARGET)
    assert series.symbol == "SPY" and series.return_kind == "PRICE_RETURN"
    assert series.provider == "TWELVE_DATA" and series.adjustment == "none"
    assert series.interval == "1day" and series.bars[-1]["date"] == TARGET.isoformat()
    # October 2026 is EDT: XNYS open is 13:30Z, derived from the calendar.
    assert series.bars[-1]["tradable_at"] == "2026-10-09T13:30:00.000000Z"


def test_output_directory_must_be_empty_and_snapshot_id_is_safe(tmp_path):
    occupied = tmp_path / "occupied"; occupied.mkdir(); (occupied / "x").write_text("x")
    with pytest.raises(BenchmarkSnapshotError, match="new and empty"):
        _acquire(occupied)
    with pytest.raises(BenchmarkSnapshotError, match="unsafe"):
        benchmark_snapshot_id(TARGET, "../escape")


def test_same_snapshot_id_cannot_be_overwritten_by_different_content(tmp_path):
    root = tmp_path / "immutable"; original, _ = _acquire(root)
    before = {path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.rglob("*") if path.is_file()}
    with pytest.raises(BenchmarkSnapshotError, match="new and empty"):
        _acquire(root, raw=_provider_payload(offset=10.0))
    after = {path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in root.rglob("*") if path.is_file()}
    assert before == after
    assert verify_spy_benchmark_snapshot(root) == original


@pytest.mark.parametrize("mutation", ["duplicate", "invalid_ohlc"])
def test_provider_duplicate_or_invalid_ohlc_fails_without_ready(tmp_path, mutation):
    sessions = _sessions()
    raw = _provider_payload(sessions=sessions + [sessions[-1]])
    if mutation == "invalid_ohlc":
        payload = json.loads(_provider_payload())
        payload["values"][0]["high"] = "1"
        raw = canonical_bytes(payload)
    root = tmp_path / mutation
    with pytest.raises(BenchmarkSnapshotError):
        _acquire(root, raw=raw)
    assert not (root / "READY").exists()


def test_target_session_missing_is_not_ready(tmp_path):
    root = tmp_path / "missing-target"
    with pytest.raises(BenchmarkSnapshotError, match="target session missing"):
        _acquire(root, raw=_provider_payload(sessions=_sessions()[:-1]))
    manifest = json.loads((root / "benchmark_manifest.json").read_bytes())
    assert manifest["coverage_gate"]["ready"] is False
    assert not (root / "READY").exists()


def test_raw_normalized_and_manifest_hash_mismatches_fail(tmp_path):
    raw_root = tmp_path / "raw"; _acquire(raw_root)
    (raw_root / "raw" / "SPY.json").write_bytes(b"{}")
    with pytest.raises(BenchmarkSnapshotError, match="raw benchmark hash"):
        verify_spy_benchmark_snapshot(raw_root)

    normalized_root = tmp_path / "normalized"; _acquire(normalized_root)
    (normalized_root / "normalized" / "SPY.json").write_bytes(b"[]")
    with pytest.raises(BenchmarkSnapshotError, match="normalized benchmark hash"):
        verify_spy_benchmark_snapshot(normalized_root)

    manifest_root = tmp_path / "manifest"; _acquire(manifest_root)
    manifest = json.loads((manifest_root / "benchmark_manifest.json").read_bytes())
    manifest["acquisition_completed_at_utc"] = "2026-10-12T10:00:02.000000Z"
    manifest["benchmark_snapshot_sha256"] = _snapshot_hash(manifest)
    (manifest_root / "benchmark_manifest.json").write_bytes(canonical_bytes(manifest))
    with pytest.raises(BenchmarkSnapshotError, match="READY marker or manifest hash"):
        verify_spy_benchmark_snapshot(manifest_root)


def test_unordered_normalized_sessions_fail_even_with_rehashed_envelope(tmp_path):
    root = tmp_path / "unordered"; _acquire(root)
    normalized_path = root / "normalized" / "SPY.json"
    bars = json.loads(normalized_path.read_bytes()); bars[0], bars[1] = bars[1], bars[0]
    normalized_raw = canonical_bytes(bars); normalized_path.write_bytes(normalized_raw)
    manifest = json.loads((root / "benchmark_manifest.json").read_bytes())
    manifest["normalized_sha256"] = hashlib.sha256(normalized_raw).hexdigest()
    _rewrite_manifest_and_ready(root, manifest)
    with pytest.raises(BenchmarkSnapshotError, match="unordered or duplicated"):
        verify_spy_benchmark_snapshot(root)


def test_loader_rejects_contract_and_target_session_mismatch(tmp_path):
    root = tmp_path / "contract"; _acquire(root)
    with pytest.raises(BenchmarkSnapshotError, match="target sessions differ"):
        load_spy_benchmark_snapshot(root, trinity_target_session="2026-10-08")
    manifest = json.loads((root / "benchmark_manifest.json").read_bytes())
    manifest["return_kind"] = "TOTAL_RETURN"
    _rewrite_manifest_and_ready(root, manifest)
    with pytest.raises(BenchmarkSnapshotError, match="contract mismatch"):
        load_spy_benchmark_snapshot(root, trinity_target_session=TARGET)


def test_mature_outcome_with_verified_spy_is_complete(tmp_path):
    root = tmp_path / "outcome"; _acquire(root)
    spy = load_spy_benchmark_snapshot(root, trinity_target_session=TARGET)
    stock = [dict(bar, open=float(bar["open"]) * 2, close=float(bar["close"]) * 2)
             for bar in spy.bars]
    decision = "2026-09-04T20:00:00Z"
    complete = calculate_selection_outcome(
        run_id="official", ticker="ABC", cohort="SCANNER",
        decision_timestamp_utc=decision, horizon_sessions=20, stock_bars=stock,
        benchmark=spy, source_snapshot_id="stock-snapshot",
        source_snapshot_sha256="a" * 64, eligibility=OFFICIAL_ELIGIBLE,
    )
    assert complete.maturity_status == "COMPLETE"
    assert complete.benchmark_return_pct is not None
    assert complete.active_return_pct == pytest.approx(
        complete.stock_return_pct - complete.benchmark_return_pct
    )
    missing = calculate_selection_outcome(
        run_id="official", ticker="ABC", cohort="SCANNER",
        decision_timestamp_utc=decision, horizon_sessions=20, stock_bars=stock,
        benchmark=None, source_snapshot_id="stock-snapshot",
        source_snapshot_sha256="a" * 64, eligibility=OFFICIAL_ELIGIBLE,
    )
    assert missing.maturity_status == "DATA_QUALITY_FAILURE"
    pending = calculate_selection_outcome(
        run_id="official", ticker="ABC", cohort="SCANNER",
        decision_timestamp_utc=decision, horizon_sessions=20, stock_bars=stock[:19],
        benchmark=None, source_snapshot_id="stock-snapshot",
        source_snapshot_sha256="a" * 64, eligibility=OFFICIAL_ELIGIBLE,
    )
    assert pending.maturity_status == "PENDING"


def test_registry_is_unchanged_and_spy_is_not_ticker_519(tmp_path):
    before = hashlib.sha256(DEFAULT_REGISTRY.read_bytes()).hexdigest()
    _acquire(tmp_path / "benchmark")
    after = hashlib.sha256(DEFAULT_REGISTRY.read_bytes()).hexdigest()
    assert before == after
    assert len(REGISTRY.records) == len(REGISTRY.supported) == 518
    assert all(record.ticker != "SPY" for record in REGISTRY.records)


def test_no_network_and_no_secret_persistence(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted")
    ))
    _acquire(tmp_path / "offline")
    secret_root = tmp_path / "secret"
    with pytest.raises(BenchmarkSnapshotError, match="configured secret"):
        _acquire(secret_root, raw=_provider_payload(extra={"note": "fixture-secret"}))
    assert all(b"fixture-secret" not in path.read_bytes()
               for path in secret_root.rglob("*") if path.is_file())


def test_canonical_normalized_hash_reproducibility_and_content_sensitivity(tmp_path):
    first, _ = _acquire(tmp_path / "first")
    second, _ = _acquire(tmp_path / "second")
    changed, _ = _acquire(tmp_path / "changed", raw=_provider_payload(offset=10.0))
    assert first["normalized_sha256"] == second["normalized_sha256"]
    assert changed["normalized_sha256"] != first["normalized_sha256"]


def test_protocol_identity_is_unchanged():
    assert PROTOCOL_HASH == PROTOCOL_HASH_V1
    assert PROTOCOL_EPOCH == 1
