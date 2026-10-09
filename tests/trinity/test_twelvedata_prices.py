from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest
import requests

from trinity.twelvedata_prices import (
    ADJUSTMENT,
    DAILY_CREDIT_RESERVE,
    INTERVAL,
    ApiUsageCapture,
    CreditGuardError,
    DailyQuotaExhausted,
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
    required_daily_credits,
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
    def __init__(self, content, status=200, headers=None):
        self.content = content
        self.status_code = status
        self.headers = headers or {}


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


def _usage(*, daily_usage=100, daily_limit=800):
    return json.dumps({
        "timestamp": "2026-10-09 00:01:00",
        "current_usage": 1,
        "plan_limit": 8,
        "plan_category": "Basic",
        "daily_usage": daily_usage,
        "plan_daily_limit": daily_limit,
    }).encode()


def _registry(path: Path, tickers=("AAPL",)):
    path.write_text(json.dumps({
        "schema_name": "trinity.usa-issuer-registry", "schema_version": "1",
        "canonical_ticker_count": len(tickers),
        "issuers": [{
            "ticker": ticker, "provider_symbol": f"{ticker}.US",
            "company_name": ticker, "sec_cik": f"{index:010d}",
            "sec_ticker": ticker, "sec_issuer_name": ticker,
            "sec_sic": 3571, "sec_sic_description": "Electronic Computers",
            "schema_type": "TECHNOLOGY", "aliases": [ticker], "supported": True,
            "unsupported_reason": None, "provenance": {"test": "fixture"},
        } for index, ticker in enumerate(tickers, start=1)],
    }), encoding="utf-8")
    return path


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


def test_daily_credit_threshold_is_518_plus_conservative_reserve():
    assert DAILY_CREDIT_RESERVE == 32
    assert required_daily_credits(518) == 550
    assert required_daily_credits(1) == 550


def test_api_usage_actual_schema_with_sufficient_credits_allows_acquisition(tmp_path):
    client, session = _client([
        _Response(_usage(daily_usage=200), headers={
            "api-credits-used": "1", "api-credits-left": "7",
        }),
        _Response(_payload()),
    ])
    manifest = acquire_snapshot(
        target_session=TARGET, output_root=tmp_path / "snapshot", run_id="usage-ok",
        client=client, registry_path=_registry(tmp_path / "registry.json"),
        now=lambda: datetime(2026, 10, 9, tzinfo=timezone.utc),
    )
    assert len(session.calls) == 2
    assert manifest["coverage_gate"]["ready"] is True
    evidence = json.loads(
        (tmp_path / "snapshot" / "provider_credit_telemetry.json").read_text()
    )
    assert evidence["provider_response"] == {
        "timestamp": "2026-10-09 00:01:00", "current_usage": 1,
        "plan_limit": 8, "plan_category": "Basic", "daily_usage": 200,
        "plan_daily_limit": 800,
    }
    assert evidence["response_headers"]["api_credits_left"] == 7


def test_insufficient_api_usage_fails_before_any_ticker_request(tmp_path):
    client, session = _client([_Response(_usage(daily_usage=251))])
    root = tmp_path / "snapshot"
    with pytest.raises(CreditGuardError, match="INSUFFICIENT_DAILY_CREDITS") as error:
        acquire_snapshot(
            target_session=TARGET, output_root=root, run_id="usage-low",
            client=client, registry_path=_registry(tmp_path / "registry.json"),
        )
    assert len(session.calls) == 1
    assert (error.value.required, error.value.available, error.value.shortfall) == (550, 549, 1)
    failure = json.loads((root / "acquisition_failure.json").read_text())
    assert failure["classification"] == "INSUFFICIENT_DAILY_CREDITS"


def test_continuous_guard_stops_retry_when_headroom_would_be_consumed():
    client, session = _client([
        _Response(_usage(daily_usage=767)), requests.ReadTimeout(), _Response(_payload()),
    ])
    usage = client.fetch_api_usage()
    assert usage.daily_remaining == 33
    with pytest.raises(CreditGuardError, match="INSUFFICIENT_DAILY_CREDITS"):
        client.fetch(
            "AAPL", start_date=date(2025, 7, 14), target_session=TARGET,
            remaining_tickers=1,
        )
    assert len(session.calls) == 2  # /api_usage plus one ticker attempt; no retry


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


def test_daily_quota_429_uses_archived_payload_semantics_and_is_not_retried():
    archived = (
        Path(__file__).parents[1] / "fixtures" / "twelvedata_daily_quota_429.fixture"
    ).read_bytes()
    client, session = _client([_Response(archived, 429), _Response(_payload())])
    with pytest.raises(DailyQuotaExhausted, match="DAILY_QUOTA_EXHAUSTED") as error:
        client.fetch("AAPL", start_date=date(2025, 7, 14), target_session=TARGET)
    assert len(session.calls) == len(error.value.attempts) == 1


def test_minute_rate_429_without_daily_semantics_remains_bounded_retry():
    minute_limit = b'{"status":"error","code":429,"message":"minute rate limit exceeded; retry shortly"}'
    client, session = _client([_Response(minute_limit, 429), _Response(_payload())])
    capture = client.fetch("AAPL", start_date=date(2025, 7, 14), target_session=TARGET)
    assert len(session.calls) == len(capture.attempts) == 2


def test_daily_exhaustion_aborts_broader_acquisition_and_persists_sanitized_evidence(tmp_path):
    archived = (
        Path(__file__).parents[1] / "fixtures" / "twelvedata_daily_quota_429.fixture"
    ).read_bytes()
    client, session = _client([
        _Response(_usage(daily_usage=100)), _Response(archived, 429), _Response(_payload("MSFT")),
    ])
    root = tmp_path / "snapshot"
    with pytest.raises(DailyQuotaExhausted):
        acquire_snapshot(
            target_session=TARGET, output_root=root, run_id="daily-stop",
            client=client,
            registry_path=_registry(tmp_path / "registry.json", ("AAPL", "MSFT")),
        )
    assert len(session.calls) == 2  # /api_usage and AAPL only
    failure = json.loads((root / "acquisition_failure.json").read_text())
    assert failure["classification"] == "DAILY_QUOTA_EXHAUSTED"
    persisted = b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
    assert b"unit-secret" not in persisted


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
        credit_events = []
        daily_remaining = 800
        def fetch_api_usage(self):
            return ApiUsageCapture(
                usage={
                    "timestamp": "2026-10-08 00:00:00", "current_usage": 1,
                    "plan_limit": 8, "plan_category": "Basic", "daily_usage": 0,
                    "plan_daily_limit": 800,
                },
                daily_usage=0, plan_daily_limit=800, daily_remaining=800,
                retrieved_at_utc="2026-10-08T00:00:00.000000Z", response_headers={},
            )
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


def test_mocked_518_credit_successful_path_remains_ready(tmp_path):
    class FullClient:
        api_key = "unit-secret"
        credit_events = []
        daily_remaining = 799

        def fetch_api_usage(self):
            return ApiUsageCapture(
                usage={
                    "timestamp": "2026-10-09 00:00:00", "current_usage": 1,
                    "plan_limit": 8, "plan_category": "Basic", "daily_usage": 1,
                    "plan_daily_limit": 800,
                },
                daily_usage=1, plan_daily_limit=800, daily_remaining=799,
                retrieved_at_utc="2026-10-09T00:00:00.000000Z", response_headers={},
            )

        def fetch(self, symbol, **_kwargs):
            self.daily_remaining -= 1
            if symbol == "WBD":
                stale = {
                    "datetime": "2026-10-06", "open": "30.95", "high": "30.95",
                    "low": "30.95", "close": "30.95", "volume": "0",
                }
                raw = _payload("WBD", [stale, _row("2026-10-05", value=30.0)])
            else:
                raw = _payload(symbol)
            return ProviderCapture(raw, ({"attempt": 1, "http_status": 200},))

    manifest = acquire_snapshot(
        target_session=TARGET, output_root=tmp_path / "full", run_id="full-mock",
        client=FullClient(), now=lambda: datetime(2026, 10, 9, tzinfo=timezone.utc),
    )
    assert manifest["canonical_ticker_count"] == 518
    assert manifest["active_complete_count"] == 517
    assert manifest["explicit_exception_count"] == 1
    assert manifest["coverage_gate"]["ready"] is True
