from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
    BENCHMARK_FINALITY_GRACE_MINUTES, BenchmarkSnapshotError, _snapshot_hash,
    acquire_spy_benchmark,
    benchmark_snapshot_id, load_spy_benchmark_snapshot,
    verify_spy_benchmark_snapshot,
)


PROTOCOL_HASH_V1 = "38980fc7bdc0ab8912f23cbc8ce67325f0d0ffe896a8d29838fe81ed1d094a1a"
TARGET = date(2026, 10, 9)
RUN_ID = "OFFICIAL_2026-10-12_A1"


def _sessions(count: int | None = None, *, target: date = TARGET) -> list[str]:
    calendar = exchange_calendars.get_calendar(
        "XNYS", start=target - timedelta(days=450), end=target,
    )
    sessions = [item.date().isoformat() for item in calendar.sessions]
    return sessions if count is None else sessions[-count:]


def _provider_payload(*, sessions: list[str] | None = None, offset: float = 0.0,
                      extra: dict[str, object] | None = None,
                      target: date = TARGET) -> bytes:
    days = _sessions(target=target) if sessions is None else sessions
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

    def __init__(self, raw: bytes, *, attempts: tuple[dict[str, object], ...] | None = None) -> None:
        self.raw = raw
        self.attempts = attempts or ({"attempt": 1, "http_status": 200},)
        self.calls: list[tuple[object, ...]] = []

    def fetch(self, symbol, *, start_date, target_session, remaining_tickers=None):
        self.calls.append((symbol, start_date, target_session, remaining_tickers))
        return ProviderCapture(self.raw, self.attempts)


def _clock(started_at: datetime | None = None):
    start = started_at or datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    values = iter((
        start, start + timedelta(seconds=1),
    ))
    return lambda: next(values)


def _acquire(root: Path, *, raw: bytes | None = None, target: date = TARGET,
             started_at: datetime | None = None,
             attempts: tuple[dict[str, object], ...] | None = None):
    start = started_at or datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    effective_attempts = attempts or ({
        "attempt": 1,
        "requested_at_utc": (start + timedelta(milliseconds=500)).isoformat(),
        "http_status": 200,
    },)
    provider = FakeProvider(
        raw or _provider_payload(target=target), attempts=effective_attempts,
    )
    manifest = acquire_spy_benchmark(
        target_session=target, run_id=RUN_ID, output_root=root,
        client=provider, now=_clock(start),
    )
    return manifest, provider


def _session_times(target: date) -> tuple[datetime, datetime]:
    calendar = exchange_calendars.get_calendar(
        "XNYS", start=target - timedelta(days=1), end=target + timedelta(days=1),
    )
    row = calendar.schedule.loc[target.isoformat()]
    return (
        row["open"].to_pydatetime().astimezone(timezone.utc),
        row["close"].to_pydatetime().astimezone(timezone.utc),
    )


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


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda payload: payload.pop("meta"), "metadata"),
        (lambda payload: payload.__setitem__("meta", "SPY"), "metadata"),
        (lambda payload: payload["meta"].pop("symbol"), "symbol"),
        (lambda payload: payload["meta"].__setitem__("symbol", "QQQ"), "symbol"),
        (lambda payload: payload["meta"].pop("interval"), "interval"),
        (lambda payload: payload["meta"].__setitem__("interval", "5min"), "interval"),
    ],
    ids=("no-meta", "invalid-meta", "no-symbol", "wrong-symbol", "no-interval", "wrong-interval"),
)
def test_provider_metadata_is_mandatory_and_fail_closed(tmp_path, mutation, match):
    payload = json.loads(_provider_payload())
    mutation(payload)
    root = tmp_path / match
    with pytest.raises(BenchmarkSnapshotError, match=match):
        _acquire(root, raw=canonical_bytes(payload))
    assert not (root / "READY").exists()


def test_exact_xnys_coverage_accepts_complete_calendar(tmp_path):
    manifest, _provider = _acquire(tmp_path / "complete")
    assert len(_sessions()) == 312
    assert manifest["bar_count"] == len(_sessions())
    assert manifest["first_session"] == _sessions()[0]
    assert manifest["last_session"] == TARGET.isoformat()
    assert manifest["expected_first_session"] == "2025-07-16"
    assert manifest["expected_last_session"] == TARGET.isoformat()
    assert manifest["expected_session_count"] == manifest["actual_session_count"] == 312


@pytest.mark.parametrize("count", [1, 2, 10, 25, 137])
def test_contiguous_suffix_of_requested_window_fails_closed(tmp_path, count):
    root = tmp_path / f"suffix-{count}"
    with pytest.raises(BenchmarkSnapshotError, match="requested-window XNYS coverage mismatch"):
        _acquire(root, raw=_provider_payload(sessions=_sessions(count)))
    assert not (root / "READY").exists()


def test_wrong_first_session_and_extra_session_fail_closed(tmp_path):
    sessions = _sessions()
    wrong_first_root = tmp_path / "wrong-first"
    with pytest.raises(BenchmarkSnapshotError, match="requested-window XNYS coverage mismatch"):
        _acquire(wrong_first_root, raw=_provider_payload(sessions=sessions[1:]))
    assert not (wrong_first_root / "READY").exists()

    extended_calendar = exchange_calendars.get_calendar(
        "XNYS", start=TARGET - timedelta(days=470), end=TARGET,
    )
    prior = [item.date().isoformat() for item in extended_calendar.sessions
             if item.date().isoformat() < sessions[0]][-1]
    extra_root = tmp_path / "extra"
    with pytest.raises(BenchmarkSnapshotError, match="requested-window XNYS coverage mismatch"):
        _acquire(extra_root, raw=_provider_payload(sessions=[prior, *sessions]))
    assert not (extra_root / "READY").exists()


def test_intermediate_xnys_gap_fails_closed(tmp_path):
    sessions = _sessions()
    missing = sessions[len(sessions) // 2]
    root = tmp_path / "gap"
    with pytest.raises(BenchmarkSnapshotError, match="coverage mismatch"):
        _acquire(root, raw=_provider_payload(sessions=[item for item in sessions if item != missing]))
    assert not (root / "READY").exists()


def test_non_xnys_session_fails_closed(tmp_path):
    sessions = _sessions()
    root = tmp_path / "non-xnys"
    with pytest.raises(BenchmarkSnapshotError, match="coverage mismatch"):
        _acquire(root, raw=_provider_payload(sessions=sessions[:-1] + ["2026-10-03", sessions[-1]]))
    assert not (root / "READY").exists()


@pytest.mark.parametrize(
    "offset,ready",
    [
        (timedelta(seconds=-1), False),
        (timedelta(minutes=19, seconds=59), False),
        (timedelta(minutes=20), True),
        (timedelta(minutes=21), True),
    ],
    ids=("before-close", "close-plus-19m59s", "exactly-close-plus-20m", "after-grace"),
)
def test_target_bar_finality_uses_acquisition_start(tmp_path, offset, ready):
    _opened, closed = _session_times(TARGET)
    root = tmp_path / str(ready) / str(offset.total_seconds())
    if ready:
        manifest, provider = _acquire(root, started_at=closed + offset)
        assert provider.calls
        assert manifest["finality_grace_minutes"] == BENCHMARK_FINALITY_GRACE_MINUTES
    else:
        provider = FakeProvider(_provider_payload())
        with pytest.raises(BenchmarkSnapshotError, match="not final"):
            acquire_spy_benchmark(
                target_session=TARGET, run_id=RUN_ID, output_root=root,
                client=provider, now=_clock(closed + offset),
            )
        assert provider.calls == []
        assert not (root / "READY").exists()


def test_future_target_fails_before_provider_request(tmp_path):
    target = date(2026, 10, 13)
    provider = FakeProvider(_provider_payload(target=target))
    root = tmp_path / "future"
    with pytest.raises(BenchmarkSnapshotError, match="not final"):
        acquire_spy_benchmark(
            target_session=target, run_id=RUN_ID, output_root=root,
            client=provider, now=_clock(datetime(2026, 10, 12, 10, tzinfo=timezone.utc)),
        )
    assert provider.calls == [] and not (root / "READY").exists()


@pytest.mark.parametrize(
    "target,expected_open,expected_close",
    [
        (date(2026, 10, 9), "2026-10-09T13:30:00.000000Z", "2026-10-09T20:00:00.000000Z"),
        (date(2026, 11, 9), "2026-11-09T14:30:00.000000Z", "2026-11-09T21:00:00.000000Z"),
    ],
    ids=("EDT", "EST"),
)
def test_finality_and_tradable_at_follow_xnys_dst(
    tmp_path, target, expected_open, expected_close,
):
    _opened, closed = _session_times(target)
    manifest, _provider = _acquire(
        tmp_path / target.isoformat(), target=target,
        started_at=closed + timedelta(minutes=BENCHMARK_FINALITY_GRACE_MINUTES),
    )
    assert manifest["target_session_open_utc"] == expected_open
    assert manifest["target_session_close_utc"] == expected_close
    bars = json.loads((tmp_path / target.isoformat() / "normalized" / "SPY.json").read_bytes())
    assert bars[-1]["tradable_at"] == expected_open


def test_finality_uses_early_close_from_xnys_calendar(tmp_path):
    target = date(2026, 11, 27)
    _opened, closed = _session_times(target)
    assert closed == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    manifest, _provider = _acquire(
        tmp_path / "early-close", target=target,
        started_at=closed + timedelta(minutes=BENCHMARK_FINALITY_GRACE_MINUTES),
    )
    assert manifest["target_session_close_utc"] == "2026-11-27T18:00:00.000000Z"
    assert manifest["finality_eligible_at_utc"] == "2026-11-27T18:20:00.000000Z"


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

    ready_root = tmp_path / "ready"; _acquire(ready_root)
    marker = json.loads((ready_root / "READY").read_bytes())
    marker["manifest_sha256"] = "0" * 64
    (ready_root / "READY").write_bytes(canonical_bytes(marker))
    with pytest.raises(BenchmarkSnapshotError, match="READY marker or manifest hash"):
        verify_spy_benchmark_snapshot(ready_root)


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


@pytest.mark.parametrize("field", ["access_token", "client_secret", "Authorization", "Cookie"])
def test_sensitive_raw_provider_fields_are_rejected(tmp_path, field):
    root = tmp_path / field
    with pytest.raises(BenchmarkSnapshotError, match="sensitive field"):
        _acquire(root, raw=_provider_payload(extra={field: "do-not-persist"}))
    assert not (root / "READY").exists()
    assert all(b"do-not-persist" not in path.read_bytes()
               for path in root.rglob("*") if path.is_file())


@pytest.mark.parametrize(
    "field", ["api_key", "access_token", "refresh_token", "client_secret", "headers", "url"],
)
def test_sensitive_provider_attempt_fields_are_rejected(tmp_path, field):
    root = tmp_path / field
    attempts = ({"attempt": 1, "http_status": 200, field: "do-not-persist"},)
    with pytest.raises(BenchmarkSnapshotError, match="sensitive field"):
        _acquire(root, attempts=attempts)
    assert not (root / "READY").exists()
    assert all(b"do-not-persist" not in path.read_bytes()
               for path in root.rglob("*") if path.is_file())


def test_configured_api_key_value_in_provider_attempt_is_rejected(tmp_path):
    root = tmp_path / "attempt-api-key"
    attempts = ({"attempt": 1, "provider_message": "fixture-secret"},)
    with pytest.raises(BenchmarkSnapshotError, match="configured secret"):
        _acquire(root, attempts=attempts)
    assert not (root / "READY").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_message", "token=abc"),
        ("provider_message", "https://example.test/x?token=abc"),
        ("provider_message", "Bearer abc.def.ghi"),
        ("provider_message", "refresh_token=abc"),
        ("exception", "request failed with token=abc"),
    ],
    ids=("token", "url-token", "bearer", "refresh-token", "exception-token"),
)
def test_sensitive_strings_in_allowlisted_attempt_fields_are_rejected(
    tmp_path, field, value,
):
    root = tmp_path / field / str(abs(hash(value)))
    attempts = ({
        "attempt": 1,
        "requested_at_utc": "2026-10-12T10:00:00.500000Z",
        field: value,
    },)
    with pytest.raises(BenchmarkSnapshotError, match="sensitive value"):
        _acquire(root, attempts=attempts)
    assert not (root / "READY").exists()


def test_harmless_provider_attempt_message_is_persisted(tmp_path):
    attempts = ({
        "attempt": 1,
        "requested_at_utc": "2026-10-12T10:00:00.500000Z",
        "http_status": 429,
        "provider_message": "rate limit; retry scheduled",
    }, {
        "attempt": 2,
        "requested_at_utc": "2026-10-12T10:00:00.750000Z",
        "http_status": 200,
    })
    manifest, _provider = _acquire(tmp_path / "harmless-message", attempts=attempts)
    assert manifest["provider_attempts"][0]["provider_message"] == \
        "rate limit; retry scheduled"


def test_provider_attempts_use_allowlist(tmp_path):
    attempts = ({
        "attempt": 1, "requested_at_utc": "2026-10-12T10:00:00.000000Z",
        "http_status": 200, "elapsed_seconds": 0.25,
        "api_credits_left": 999, "harmless_internal_detail": "not-persisted",
    },)
    manifest, _provider = _acquire(tmp_path / "allowlist", attempts=attempts)
    assert manifest["provider_attempts"] == [{
        "attempt": 1, "requested_at_utc": "2026-10-12T10:00:00.000000Z",
        "http_status": 200, "elapsed_seconds": 0.25, "api_credits_left": 999,
    }]
    assert b"harmless_internal_detail" not in (
        tmp_path / "allowlist" / "benchmark_manifest.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "attempts,started_at",
    [
        (({"attempt": 1, "requested_at_utc": "garbage"},), None),
        (({"attempt": 1, "requested_at_utc": "2026-10-12T09:59:59.000000Z"},), None),
        (({"attempt": 1, "requested_at_utc": "2026-10-12T10:00:02.000000Z"},), None),
        (({"attempt": 1, "requested_at_utc": "2026-10-09T20:19:59.000000Z"},),
         datetime(2026, 10, 9, 20, 20, tzinfo=timezone.utc)),
        (({"attempt": 1, "requested_at_utc": "2026-10-12T10:00:00.750000Z"},
          {"attempt": 2, "requested_at_utc": "2026-10-12T10:00:00.250000Z"}), None),
    ],
    ids=("garbage", "before-start", "after-completed", "before-finality", "nonmonotonic"),
)
def test_invalid_provider_attempt_timestamps_fail_closed(
    tmp_path, attempts, started_at,
):
    root = tmp_path / "invalid-attempt-time"
    with pytest.raises(BenchmarkSnapshotError, match="requested_at_utc"):
        _acquire(root, attempts=attempts, started_at=started_at)
    assert not (root / "READY").exists()


def test_missing_provider_attempt_timestamp_fails_closed(tmp_path):
    root = tmp_path / "missing-attempt-time"
    with pytest.raises(BenchmarkSnapshotError, match="attempt is malformed"):
        _acquire(root, attempts=({"attempt": 1, "http_status": 200},))
    assert not (root / "READY").exists()


def test_multiple_ordered_provider_attempt_timestamps_are_valid(tmp_path):
    attempts = ({
        "attempt": 1, "requested_at_utc": "2026-10-12T10:00:00.250000Z",
        "http_status": 429,
    }, {
        "attempt": 2, "requested_at_utc": "2026-10-12T10:00:00.750000Z",
        "http_status": 200,
    })
    manifest, _provider = _acquire(tmp_path / "ordered-attempts", attempts=attempts)
    assert [item["attempt"] for item in manifest["provider_attempts"]] == [1, 2]


def test_rehashed_invalid_attempt_timestamp_is_rejected_by_loader(tmp_path):
    root = tmp_path / "rehashed-attempt-time"
    _acquire(root)
    manifest = json.loads((root / "benchmark_manifest.json").read_bytes())
    manifest["provider_attempts"][0]["requested_at_utc"] = "garbage"
    _rewrite_manifest_and_ready(root, manifest)
    with pytest.raises(BenchmarkSnapshotError, match="requested_at_utc"):
        verify_spy_benchmark_snapshot(root)


def test_rehashed_requested_window_provenance_is_rejected(tmp_path):
    root = tmp_path / "rehashed-coverage"
    _acquire(root)
    manifest = json.loads((root / "benchmark_manifest.json").read_bytes())
    manifest["expected_first_session"] = "2026-09-01"
    manifest["expected_session_count"] = 25
    _rewrite_manifest_and_ready(root, manifest)
    with pytest.raises(BenchmarkSnapshotError, match="coverage provenance mismatch"):
        verify_spy_benchmark_snapshot(root)


def test_canonical_normalized_hash_reproducibility_and_content_sensitivity(tmp_path):
    first, _ = _acquire(tmp_path / "first")
    second, _ = _acquire(tmp_path / "second")
    changed, _ = _acquire(tmp_path / "changed", raw=_provider_payload(offset=10.0))
    assert first["normalized_sha256"] == second["normalized_sha256"]
    assert changed["normalized_sha256"] != first["normalized_sha256"]


def test_protocol_identity_is_unchanged():
    assert PROTOCOL_HASH == PROTOCOL_HASH_V1
    assert PROTOCOL_EPOCH == 1
