from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import exchange_calendars
import pytest

from trinity.twelvedata_prices import (
    HISTORY_CALENDAR_DAYS,
    canonical_bytes,
    exclusive_end_date,
    normalize_response,
    snapshot_hash,
)
from trinity.validation.stock_observation import (
    StockObservationError,
    expected_ticker_set_sha256,
    load_stock_observation_snapshot,
)


TARGET = date(2026, 10, 9)


def _sessions(target: date) -> list[str]:
    start = target - timedelta(days=HISTORY_CALENDAR_DAYS)
    calendar = exchange_calendars.get_calendar("XNYS", start=start, end=target)
    return [item.date().isoformat() for item in calendar.sessions]


def _row(day: str, index: int) -> dict[str, str]:
    value = 100.0 + index / 100.0
    return {
        "datetime": day, "open": str(value), "high": str(value + 1),
        "low": str(value - 1), "close": str(value + 0.5), "volume": "1000",
    }


def _raw(ticker: str, dates: list[str]) -> bytes:
    return canonical_bytes({
        "meta": {"symbol": ticker, "interval": "1day"},
        "values": [_row(day, index) for index, day in enumerate(reversed(dates))],
        "status": "ok",
    })


def _terminal_wbd_raw(target: date) -> bytes:
    dates = [item for item in _sessions(target) if item <= "2026-10-05"]
    values = [_row(day, index) for index, day in enumerate(reversed(dates))]
    values.insert(0, {
        "datetime": "2026-10-06", "open": "30.95", "high": "30.95",
        "low": "30.95", "close": "30.95", "volume": "0",
    })
    return canonical_bytes({
        "meta": {"symbol": "WBD", "interval": "1day"},
        "values": values, "status": "ok",
    })


def _write_snapshot(
    root: Path, tickers=("AAPL",), *, target: date = TARGET,
    dates_by_ticker: dict[str, list[str]] | None = None,
    classifications: dict[str, str] | None = None,
    normalized_transform=None,
    started: datetime | None = None,
    completed: datetime | None = None,
) -> dict:
    expected_dates = _sessions(target)
    _calendar = exchange_calendars.get_calendar(
        "XNYS", start=target - timedelta(days=2), end=target + timedelta(days=2),
    )
    close = _calendar.schedule.loc[target.isoformat()]["close"].to_pydatetime().astimezone(
        timezone.utc,
    )
    started = started or close + timedelta(minutes=20)
    completed = completed or started + timedelta(minutes=10)
    entries = []
    for ticker in tickers:
        raw = _terminal_wbd_raw(target) if ticker == "WBD" else _raw(
            ticker, (dates_by_ticker or {}).get(ticker, expected_dates),
        )
        capture = normalize_response(ticker, raw, target_session=target)
        bars = [dict(item) for item in capture.bars]
        if normalized_transform is not None:
            bars = normalized_transform(ticker, bars)
        raw_path = root / "raw" / f"{ticker}.json"
        normalized_path = root / "normalized" / f"{ticker}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw)
        normalized = canonical_bytes(bars)
        normalized_path.write_bytes(normalized)
        classification = (classifications or {}).get(ticker, capture.classification)
        entries.append({
            "canonical_ticker": ticker, "provider_symbol": capture.provider_symbol,
            "classification": classification,
            "first_date": bars[0]["date"], "last_valid_date": bars[-1]["date"],
            "bar_count": len(bars), "content_sha256": hashlib.sha256(normalized).hexdigest(),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "excluded_rows": list(capture.excluded_rows),
            "attempts": [{
                "attempt": 1,
                "requested_at_utc": (started + timedelta(seconds=1)).isoformat(),
                "http_status": 200,
            }],
            "retry_count": 0, "error": None,
        })
    allowed = {"ACTIVE_COMPLETE", "CORPORATE_ACTION_NO_LONGER_TRADING"}
    manifest = {
        "schema_name": "trinity.twelvedata-price-snapshot", "schema_version": "1",
        "snapshot_id": f"TWELVEDATA_{target.isoformat()}_RUN_OBSERVATION_A1",
        "provider": "TWELVE_DATA", "interval": "1day", "adjust": "none",
        "target_market_session": target.isoformat(),
        "request_start_date": (target - timedelta(days=HISTORY_CALENDAR_DAYS)).isoformat(),
        "request_end_date_exclusive": exclusive_end_date(target).isoformat(),
        "acquisition_started_at_utc": started.isoformat(),
        "acquisition_completed_at_utc": completed.isoformat(),
        "canonical_ticker_count": len(entries),
        "active_complete_count": sum(i["classification"] == "ACTIVE_COMPLETE" for i in entries),
        "explicit_exception_count": sum(
            i["classification"] == "CORPORATE_ACTION_NO_LONGER_TRADING" for i in entries
        ),
        "provider_failure_count": 0, "invalid_data_count": 0,
        "classification_counts": {},
        "coverage_gate": {
            "ready": all(i["classification"] in allowed for i in entries),
            "allowed_states": sorted(allowed), "blocking_tickers": [],
        },
        "throttle": {"maximum_credits": 7, "rolling_window_seconds": 61.0},
        "tickers": entries,
    }
    manifest["snapshot_sha256"] = snapshot_hash(manifest)
    (root / "snapshot_manifest.json").write_bytes(canonical_bytes(manifest))
    return manifest


def _rehash(root: Path, manifest: dict) -> None:
    manifest["snapshot_sha256"] = snapshot_hash(manifest)
    (root / "snapshot_manifest.json").write_bytes(canonical_bytes(manifest))


def _load(root: Path, tickers=("AAPL",), target: date = TARGET):
    return load_stock_observation_snapshot(
        root, expected_tickers=tickers,
        expected_observation_target_session=target.isoformat(),
    )


def test_exact_frozen_ticker_set_and_source_identity_pass(tmp_path):
    manifest = _write_snapshot(tmp_path, ("VEEV", "AAPL", "HOOD"))
    snapshot = _load(tmp_path, ("AAPL", "HOOD", "VEEV"))
    assert snapshot.expected_tickers == ("AAPL", "HOOD", "VEEV")
    assert snapshot.source_snapshot_id == manifest["snapshot_id"]
    assert snapshot.source_snapshot_sha256 == manifest["snapshot_sha256"]


def test_ticker_order_and_outer_whitespace_have_stable_set_sha():
    left = expected_ticker_set_sha256(["AAPL", "HOOD", "VEEV"])
    right = expected_ticker_set_sha256([" veev ", "aapl", "HOOD"])
    assert left == right


@pytest.mark.parametrize("tickers", [[], ["AAPL", "aapl"], ["AAPL", "BAD TICKER"]])
def test_invalid_expected_ticker_set_is_rejected(tickers):
    with pytest.raises(StockObservationError):
        expected_ticker_set_sha256(tickers)


def test_missing_expected_ticker_is_rejected(tmp_path):
    _write_snapshot(tmp_path, ("AAPL",))
    with pytest.raises(StockObservationError, match="missing=MSFT"):
        _load(tmp_path, ("AAPL", "MSFT"))


def test_unexpected_ticker_is_rejected(tmp_path):
    _write_snapshot(tmp_path, ("AAPL", "MSFT"))
    with pytest.raises(StockObservationError, match="unexpected=MSFT"):
        _load(tmp_path, ("AAPL",))


def test_duplicate_snapshot_entry_is_rejected(tmp_path):
    manifest = _write_snapshot(tmp_path)
    manifest["tickers"].append(dict(manifest["tickers"][0]))
    manifest["canonical_ticker_count"] = 2
    _rehash(tmp_path, manifest)
    with pytest.raises(StockObservationError, match="duplicate"):
        _load(tmp_path)


def test_target_session_mismatch_is_rejected(tmp_path):
    _write_snapshot(tmp_path)
    with pytest.raises(StockObservationError, match="target session mismatch"):
        load_stock_observation_snapshot(
            tmp_path, expected_tickers=("AAPL",),
            expected_observation_target_session="2026-10-08",
        )


def test_provider_contract_drift_is_rejected(tmp_path):
    manifest = _write_snapshot(tmp_path)
    manifest["adjust"] = "all"
    _rehash(tmp_path, manifest)
    with pytest.raises(StockObservationError, match="provider contract"):
        _load(tmp_path)


@pytest.mark.parametrize(("artifact", "message"), [
    ("manifest", "manifest"), ("normalized", "normalized"), ("raw", "raw"),
])
def test_corrupted_source_artifacts_are_rejected(tmp_path, artifact, message):
    _write_snapshot(tmp_path)
    path = {
        "manifest": tmp_path / "snapshot_manifest.json",
        "normalized": tmp_path / "normalized" / "AAPL.json",
        "raw": tmp_path / "raw" / "AAPL.json",
    }[artifact]
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(StockObservationError, match=message):
        _load(tmp_path)


def test_active_complete_requires_full_requested_xnys_window(tmp_path):
    _write_snapshot(tmp_path)
    series = _load(tmp_path).series("AAPL")
    assert len(series.bars) == len(_sessions(TARGET))
    assert series.first_session == _sessions(TARGET)[0]
    assert series.last_session == TARGET.isoformat()


@pytest.mark.parametrize("mutation", ["suffix", "first", "gap", "target", "extra"])
def test_active_incomplete_or_extra_coverage_is_rejected(tmp_path, mutation):
    sessions = _sessions(TARGET)
    values = {
        "suffix": sessions[-10:],
        "first": sessions[1:],
        "gap": sessions[:100] + sessions[101:],
        "target": sessions[:-1],
        "extra": ["2025-07-15", *sessions],
    }[mutation]
    _write_snapshot(
        tmp_path, dates_by_ticker={"AAPL": values},
        classifications={"AAPL": "ACTIVE_COMPLETE"},
    )
    with pytest.raises(StockObservationError, match="coverage mismatch"):
        _load(tmp_path)


def test_unordered_normalized_sessions_are_rejected(tmp_path):
    _write_snapshot(tmp_path, normalized_transform=lambda _ticker, bars: list(reversed(bars)))
    with pytest.raises(StockObservationError, match="unique and ordered"):
        _load(tmp_path)


def test_non_xnys_session_is_rejected(tmp_path):
    dates = [*_sessions(TARGET)[:-1], "2026-10-04", TARGET.isoformat()]
    _write_snapshot(
        tmp_path, dates_by_ticker={"AAPL": dates},
        classifications={"AAPL": "ACTIVE_COMPLETE"},
    )
    with pytest.raises(StockObservationError, match="coverage mismatch"):
        _load(tmp_path)


def test_terminal_corporate_action_is_preserved_without_forward_fill(tmp_path):
    _write_snapshot(tmp_path, ("WBD",))
    series = _load(tmp_path, ("WBD",)).series("WBD")
    assert series.classification == "CORPORATE_ACTION_NO_LONGER_TRADING"
    assert series.last_session == "2026-10-05"
    assert all(row["date"] <= "2026-10-05" for row in series.bars)
    assert not any(row["date"] == TARGET.isoformat() for row in series.bars)


def test_close_plus_19m59s_is_rejected_and_close_plus_20m_passes(tmp_path):
    close = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)
    early = tmp_path / "early"
    _write_snapshot(early, started=close + timedelta(minutes=19, seconds=59))
    with pytest.raises(StockObservationError, match="not final"):
        _load(early)
    exact = tmp_path / "exact"
    _write_snapshot(exact, started=close + timedelta(minutes=20))
    assert _load(exact).observation_target_session == TARGET.isoformat()


@pytest.mark.parametrize(("target", "close"), [
    (date(2026, 3, 9), datetime(2026, 3, 9, 20, 0, tzinfo=timezone.utc)),
    (date(2026, 11, 2), datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)),
    (date(2026, 11, 27), datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)),
])
def test_finality_uses_xnys_edt_est_and_early_close(tmp_path, target, close):
    _write_snapshot(tmp_path, target=target, started=close + timedelta(minutes=20))
    assert _load(tmp_path, target=target).observation_target_session == target.isoformat()


def test_reversed_acquisition_timestamps_are_rejected(tmp_path):
    start = datetime(2026, 10, 9, 20, 30, tzinfo=timezone.utc)
    _write_snapshot(tmp_path, started=start, completed=start - timedelta(seconds=1))
    with pytest.raises(StockObservationError, match="timestamps are reversed"):
        _load(tmp_path)


def test_naive_acquisition_timestamp_is_rejected(tmp_path):
    manifest = _write_snapshot(tmp_path)
    manifest["acquisition_started_at_utc"] = "2026-10-09T20:20:00"
    _rehash(tmp_path, manifest)
    with pytest.raises(StockObservationError, match="timezone-aware"):
        _load(tmp_path)


def test_registry_state_is_not_consulted(monkeypatch, tmp_path):
    _write_snapshot(tmp_path, ("AAPL",))
    monkeypatch.setattr(
        "trinity.twelvedata_prices.verify_canonical_universe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("registry consulted")),
    )
    assert _load(tmp_path, ("AAPL",)).expected_tickers == ("AAPL",)


def test_frozen_ticker_absent_from_future_registry_is_still_accepted(tmp_path):
    _write_snapshot(tmp_path, ("ZZZZ",))
    assert _load(tmp_path, ("ZZZZ",)).series("ZZZZ").ticker == "ZZZZ"


def test_provider_symbol_mismatch_is_fail_closed(tmp_path):
    manifest = _write_snapshot(tmp_path)
    manifest["tickers"][0]["provider_symbol"] = "MSFT"
    _rehash(tmp_path, manifest)
    with pytest.raises(StockObservationError, match="provider symbol"):
        _load(tmp_path)


def test_tradable_at_is_real_xnys_open(tmp_path):
    target = date(2026, 11, 2)
    _write_snapshot(
        tmp_path, target=target,
        started=datetime(2026, 11, 2, 21, 20, tzinfo=timezone.utc),
    )
    series = _load(tmp_path, target=target).series("AAPL")
    target_bar = next(row for row in series.bars if row["date"] == target.isoformat())
    assert target_bar["tradable_at"] == "2026-11-02T14:30:00.000000Z"


def test_request_window_manifest_is_recomputed(tmp_path):
    manifest = _write_snapshot(tmp_path)
    manifest["request_start_date"] = "2025-07-17"
    _rehash(tmp_path, manifest)
    with pytest.raises(StockObservationError, match="request window"):
        _load(tmp_path)


def test_requested_at_provenance_is_checked_when_present(tmp_path):
    manifest = _write_snapshot(tmp_path)
    manifest["tickers"][0]["attempts"][0]["requested_at_utc"] = \
        "2026-10-09T20:19:59+00:00"
    _rehash(tmp_path, manifest)
    with pytest.raises(StockObservationError, match="outside acquisition bounds"):
        _load(tmp_path)
