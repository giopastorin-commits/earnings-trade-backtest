from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest
import requests

from trinity.twelvedata_prices import (
    ADJUSTMENT,
    INTERVAL,
    PriceSnapshotError,
    ProviderCapture,
    ProviderRequestError,
    RollingWindowThrottle,
    TwelveDataClient,
    ValidatedPriceSnapshot,
    acquire_snapshot,
    canonical_bytes,
    exclusive_end_date,
    normalize_response,
    snapshot_hash,
    verify_snapshot,
)


TARGET = date(2026, 10, 7)


def _row(day="2026-10-07", *, value=100.0, volume=1000):
    return {
        "datetime": day, "open": str(value), "high": str(value + 1),
        "low": str(value - 1), "close": str(value + 0.5), "volume": str(volume),
    }


def _payload(symbol="AAPL", values=None):
    return json.dumps({
        "meta": {"symbol": symbol, "interval": "1day"},
        "values": values or [_row()], "status": "ok",
    }).encode()


class _Response:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status


class _Session:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = next(self.outcomes)
        if isinstance(value, Exception):
            raise value
        return value


class _NoThrottle:
    def acquire(self):
        return 0.0


def _client(outcomes, *, sleep=lambda _seconds: None):
    session = _Session(outcomes)
    clock = iter(float(item) for item in range(1000))
    client = TwelveDataClient(
        api_key="unit-secret", session=session, throttle=_NoThrottle(),
        now=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc),
        monotonic=lambda: next(clock), sleep=sleep,
    )
    return client, session


def test_response_normalization_is_ascending_raw_ohlcv_only():
    capture = normalize_response(
        "AAPL", _payload(values=[_row("2026-10-07"), _row("2026-10-06", value=99)]),
        target_session=TARGET,
    )
    assert capture.classification == "ACTIVE_COMPLETE"
    assert [item["date"] for item in capture.bars] == ["2026-10-06", "2026-10-07"]
    assert set(capture.bars[0]) == {"date", "open", "high", "low", "close", "volume"}


def test_adjust_none_and_exclusive_end_are_enforced_for_bf_and_brk():
    client, session = _client([_Response(_payload("BF.B")), _Response(_payload("BRK.B"))])
    for ticker in ("BF.B", "BRK.B"):
        result = client.fetch(ticker, start_date=date(2025, 7, 14), target_session=TARGET)
        assert normalize_response(ticker, result.raw, target_session=TARGET).provider_symbol == ticker
    assert exclusive_end_date(TARGET) == date(2026, 10, 8)
    for _url, kwargs in session.calls:
        assert kwargs["params"]["adjust"] == ADJUSTMENT == "none"
        assert kwargs["params"]["interval"] == INTERVAL == "1day"
        assert kwargs["params"]["end_date"] == "2026-10-08"


def test_rolling_throttle_never_starts_eighth_credit_inside_61_seconds():
    state = {"now": 0.0}
    starts = []
    def monotonic():
        return state["now"]
    def sleep(seconds):
        state["now"] += seconds
    throttle = RollingWindowThrottle(monotonic=monotonic, sleep=sleep)
    for _ in range(8):
        starts.append(throttle.acquire())
    assert starts[:7] == [0.0] * 7
    assert starts[7] >= 61.0


def test_transient_failures_retry_and_record_every_attempt():
    sleeps = []
    client, session = _client([
        requests.ReadTimeout(), _Response(b'{"status":"error","code":429,"message":"wait"}'),
        _Response(_payload()),
    ], sleep=sleeps.append)
    capture = client.fetch("AAPL", start_date=date(2025, 7, 14), target_session=TARGET)
    assert len(capture.attempts) == len(session.calls) == 3
    assert [item.get("exception") for item in capture.attempts] == ["ReadTimeout", None, None]
    assert capture.attempts[1]["provider_code"] == 429
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_http_failure_is_not_retried(status):
    client, session = _client([_Response(b"{}", status)])
    with pytest.raises(ProviderRequestError, match=rf"HTTP {status}") as error:
        client.fetch("AAPL", start_date=date(2025, 7, 14), target_session=TARGET)
    assert len(session.calls) == len(error.value.attempts) == 1


def test_wbd_stale_zero_volume_row_is_excluded_as_explicit_exception():
    stale = {
        "datetime": "2026-10-06", "open": "30.95", "high": "30.95",
        "low": "30.95", "close": "30.95", "volume": "0",
    }
    normal = _row("2026-10-05", value=30.0, volume=435632600)
    capture = normalize_response(
        "WBD", _payload("WBD", [stale, normal]), target_session=TARGET,
    )
    assert capture.classification == "CORPORATE_ACTION_NO_LONGER_TRADING"
    assert [item["date"] for item in capture.bars] == ["2026-10-05"]
    assert capture.excluded_rows[0]["reason"] == "CORPORATE_ACTION_ARTIFACT_STALE_CARRY_FORWARD"


def _manifest(root: Path, classifications: dict[str, str], *, corrupt=False):
    entries = []
    for ticker, classification in classifications.items():
        bars = [{
            "date": "2026-10-05" if ticker == "WBD" else "2026-10-07",
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
            "volume": 1000.0,
        }]
        raw = canonical_bytes(bars)
        path = root / "normalized" / f"{ticker}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        entries.append({
            "canonical_ticker": ticker, "provider_symbol": ticker,
            "classification": classification, "first_date": bars[0]["date"],
            "last_valid_date": bars[-1]["date"], "bar_count": 1,
            "content_sha256": "bad" if corrupt else __import__("hashlib").sha256(raw).hexdigest(),
            "raw_sha256": None, "excluded_rows": [], "attempts": [],
            "retry_count": 0, "error": None,
        })
    allowed = {"ACTIVE_COMPLETE", "CORPORATE_ACTION_NO_LONGER_TRADING"}
    ready = all(item["classification"] in allowed for item in entries)
    value = {
        "schema_name": "trinity.twelvedata-price-snapshot", "schema_version": "1",
        "snapshot_id": "TWELVEDATA_2026-10-07_RUN_test", "provider": "TWELVE_DATA",
        "interval": "1day", "adjust": "none", "target_market_session": "2026-10-07",
        "request_start_date": "2025-07-14", "request_end_date_exclusive": "2026-10-08",
        "acquisition_started_at_utc": "2026-10-08T00:00:00.000000Z",
        "acquisition_completed_at_utc": "2026-10-08T01:00:00.000000Z",
        "canonical_ticker_count": len(entries),
        "active_complete_count": sum(i["classification"] == "ACTIVE_COMPLETE" for i in entries),
        "explicit_exception_count": sum(i["classification"] == "CORPORATE_ACTION_NO_LONGER_TRADING" for i in entries),
        "provider_failure_count": sum(i["classification"] == "PROVIDER_FAILURE" for i in entries),
        "invalid_data_count": sum(i["classification"] == "INVALID_DATA" for i in entries),
        "classification_counts": {},
        "coverage_gate": {
            "ready": ready, "allowed_states": sorted(allowed),
            "blocking_tickers": [i["canonical_ticker"] for i in entries if i["classification"] not in allowed],
        },
        "throttle": {"maximum_credits": 7, "rolling_window_seconds": 61.0},
        "tickers": entries,
    }
    value["snapshot_sha256"] = snapshot_hash(value)
    (root / "snapshot_manifest.json").write_bytes(canonical_bytes(value))
    return value


def test_coverage_gate_accepts_only_active_and_corporate_exception(tmp_path):
    value = _manifest(tmp_path, {
        "AAPL": "ACTIVE_COMPLETE", "WBD": "CORPORATE_ACTION_NO_LONGER_TRADING",
    })
    assert verify_snapshot(tmp_path)["snapshot_sha256"] == value["snapshot_sha256"]


@pytest.mark.parametrize("classification", [
    "ACTIVE_MISSING_LATEST", "PROVIDER_FAILURE", "SYMBOL_MAPPING_REQUIRED", "INVALID_DATA",
])
def test_missing_or_invalid_active_ticker_blocks_snapshot(tmp_path, classification):
    _manifest(tmp_path, {"AAPL": classification})
    with pytest.raises(PriceSnapshotError, match="NOT READY"):
        verify_snapshot(tmp_path)


def test_snapshot_hash_is_deterministic_and_normalized_hash_is_enforced(tmp_path):
    value = _manifest(tmp_path, {"AAPL": "ACTIVE_COMPLETE"})
    assert snapshot_hash(value) == snapshot_hash(json.loads(json.dumps(value)))
    (tmp_path / "normalized" / "AAPL.json").write_text("[]", encoding="utf-8")
    with pytest.raises(PriceSnapshotError, match="hash mismatch"):
        verify_snapshot(tmp_path)


def test_validated_snapshot_exposes_exact_provenance(tmp_path):
    value = _manifest(tmp_path, {"AAPL": "ACTIVE_COMPLETE"})
    snapshot = ValidatedPriceSnapshot(tmp_path, enforce_canonical_universe=False)
    assert snapshot.provenance() == {
        "price_provider": "TWELVE_DATA",
        "price_snapshot_id": value["snapshot_id"],
        "price_snapshot_session": "2026-10-07",
        "price_snapshot_sha256": value["snapshot_sha256"],
    }


def test_analytical_snapshot_loader_rejects_self_consistent_incomplete_universe(tmp_path):
    _manifest(tmp_path, {"AAPL": "ACTIVE_COMPLETE"})
    with pytest.raises(PriceSnapshotError, match="canonical universe mismatch"):
        ValidatedPriceSnapshot(tmp_path)


def test_api_key_never_appears_in_persistable_telemetry():
    client, _session = _client([_Response(_payload())])
    capture = client.fetch("AAPL", start_date=date(2025, 7, 14), target_session=TARGET)
    assert "unit-secret" not in json.dumps(capture.attempts)


def test_snapshot_preserves_raw_response_separately_from_normalized_ohlcv(tmp_path):
    registry = tmp_path / "registry.fixture"
    registry.write_text(json.dumps({
        "schema_name": "trinity.usa-issuer-registry", "schema_version": "1",
        "canonical_ticker_count": 1,
        "issuers": [{
            "ticker": "AAPL", "provider_symbol": "AAPL.US", "company_name": "Apple Inc.",
            "sec_cik": "0000320193", "sec_ticker": "AAPL", "sec_issuer_name": "Apple Inc.",
            "sec_sic": 3571, "sec_sic_description": "Electronic Computers",
            "schema_type": "TECHNOLOGY", "aliases": ["Apple"], "supported": True,
            "unsupported_reason": None, "provenance": {"test": "fixture"},
        }],
    }), encoding="utf-8")
    raw = _payload("AAPL", [_row("2026-10-07"), _row("2026-10-06", value=99)])

    class Client:
        api_key = "unit-secret"
        def fetch(self, symbol, **_kwargs):
            assert symbol == "AAPL"
            return ProviderCapture(raw, ({"attempt": 1, "http_status": 200},))

    root = tmp_path / "snapshot"
    manifest = acquire_snapshot(
        target_session=TARGET, output_root=root, run_id="fixture",
        client=Client(), registry_path=registry,
        now=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc),
    )
    assert manifest["coverage_gate"]["ready"] is True
    assert (root / "raw" / "AAPL.json").read_bytes() == raw
    normalized = json.loads((root / "normalized" / "AAPL.json").read_bytes())
    assert [item["date"] for item in normalized] == ["2026-10-06", "2026-10-07"]
    assert (root / "normalized" / "AAPL.json").read_bytes() != raw
    assert b"unit-secret" not in b"".join(
        path.read_bytes() for path in root.rglob("*") if path.is_file()
    )
