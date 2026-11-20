from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from trinity.h2_v2_1_inputs import (
    EXCLUSION_COLUMNS,
    INPUT_COLUMNS,
    canonical_json_bytes,
    ordered_bar_bytes,
    write_deterministic_csv,
)
import trinity.twelvedata_split_factors as factors
from trinity.twelvedata_prices import TwelveDataClient


class _Response:
    def __init__(self, content: bytes, status: int = 200, headers=None):
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


def _usage(limit: int) -> bytes:
    return json.dumps({
        "timestamp": "2026-11-20 18:00:00", "current_usage": 0,
        "plan_limit": 8, "plan_category": "Basic",
        "daily_usage": 0, "plan_daily_limit": limit,
    }).encode()


def _bar(day: str, close: str = "100") -> dict[str, str]:
    return {
        "datetime": day, "open": close, "high": close,
        "low": close, "close": close, "volume": "1000",
    }


def _payload(symbol: str, close: str = "100") -> bytes:
    return json.dumps({
        "meta": {"symbol": symbol, "interval": "1day"},
        "values": [_bar("2026-11-17", close)], "status": "ok",
    }).encode()


def _client(outcomes):
    session = _Session(outcomes)
    clock = iter(float(item) for item in range(10000))
    client = TwelveDataClient(
        api_key="unit-secret", session=session, throttle=_NoThrottle(),
        now=lambda: datetime(2026, 11, 20, 18, tzinfo=timezone.utc),
        monotonic=lambda: next(clock), sleep=lambda _seconds: None,
    )
    return client, session


def _input_row(ticker: str, accession: str, **updates) -> dict[str, str]:
    row = {column: "" for column in INPUT_COLUMNS}
    row.update({
        "sample": "DEV", "release_period": "DEV_PERIOD",
        "row_status": "INCLUDED", "current_release_id": accession,
        "ticker": ticker, "release_at": "2026-09-10T21:00:00Z",
        "event_timing": "AFTER_MARKET", "reference_session_date": "2026-09-10",
        "price_window_start": "2026-09-01",
    })
    row.update(updates)
    return row


def _parent(root: Path, tickers=("A", "B", "SKYD")) -> Path:
    root.mkdir(parents=True)
    dev = [_input_row(ticker, f"{ticker}-1") for ticker in tickers]
    write_deterministic_csv(root / "h2_v2_1_inputs_dev.csv", dev, INPUT_COLUMNS)
    write_deterministic_csv(
        root / "h2_v2_1_inputs_prosp_batch1.csv", [], INPUT_COLUMNS,
    )
    write_deterministic_csv(root / "h2_v2_1_exclusions.csv", [], EXCLUSION_COLUMNS)
    return root


def test_resumable_batches_keep_a2_parameters_and_psky_mapping(monkeypatch, tmp_path):
    monkeypatch.setattr(factors, "CANONICAL_TICKER_COUNT", 3)
    parent = _parent(tmp_path / "parent")
    root = tmp_path / "adjusted"
    first_client, first_session = _client([
        _Response(_usage(34)), _Response(_payload("A")), _Response(_payload("B")),
    ])
    first = factors.acquire_split_adjusted_snapshot(
        parent_root=parent, output_root=root, client=first_client,
        seal_read_only=False,
    )
    assert first["complete"] is False
    assert (first["completed_ticker_count"], first["remaining_ticker_count"]) == (2, 1)
    assert [call[1]["params"]["symbol"] for call in first_session.calls[1:]] == ["A", "B"]
    for _url, call in first_session.calls[1:]:
        assert call["params"] == {
            "symbol": call["params"]["symbol"], "interval": "1day",
            "adjust": "splits", "start_date": "2025-08-24",
            "end_date": "2026-11-18", "outputsize": 5000,
            "apikey": "unit-secret",
        }

    second_client, second_session = _client([
        _Response(_usage(100)), _Response(_payload("PSKY")),
    ])
    final = factors.acquire_split_adjusted_snapshot(
        parent_root=parent, output_root=root, client=second_client,
        seal_read_only=False,
    )
    assert final["canonical_ticker_count"] == 3
    assert final["terminal_response_count"] == 3
    assert second_session.calls[1][1]["params"]["symbol"] == "PSKY"
    assert all(call[1]["params"].get("symbol") != "SKYD" for call in second_session.calls[1:])
    verified = factors.verify_split_adjusted_snapshot(root)
    assert verified["ensemble_sha256"] == final["ensemble_sha256"]


def test_resume_refuses_changed_completed_file(monkeypatch, tmp_path):
    monkeypatch.setattr(factors, "CANONICAL_TICKER_COUNT", 3)
    parent = _parent(tmp_path / "parent")
    root = tmp_path / "adjusted"
    client, _session = _client([
        _Response(_usage(33)), _Response(_payload("A")),
    ])
    progress = factors.acquire_split_adjusted_snapshot(
        parent_root=parent, output_root=root, client=client, seal_read_only=False,
    )
    assert progress["completed_ticker_count"] == 1
    (root / "normalized" / "A.json").write_bytes(b"[]")
    resume_client, resume_session = _client([_Response(_usage(100))])
    with pytest.raises(factors.SplitFactorError, match="changed completed normalized"):
        factors.acquire_split_adjusted_snapshot(
            parent_root=parent, output_root=root, client=resume_client,
            seal_read_only=False,
        )
    assert resume_session.calls == []


def _jsonl(path: Path, rows: list[tuple[str, str]], *, bad_future: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(
        ordered_bar_bytes({
            "date": day, "open": close, "high": close,
            "low": close, "close": close, "volume": 1000,
        })
        for day, close in rows
    )
    if bad_future:
        payload += f'{{"date":"{bad_future}",BROKEN\n'.encode()
    path.write_bytes(payload)


def _factor_sources(tmp_path: Path):
    none_root = tmp_path / "none"
    adjusted_root = tmp_path / "adjusted"
    dates = ["2026-09-01", "2026-09-02", "2026-09-05", "2026-09-10"]
    _jsonl(
        none_root / "bounded_jsonl" / "A.jsonl",
        [(day, "100") for day in dates], bad_future="2026-09-11",
    )
    _jsonl(
        adjusted_root / "bounded_jsonl" / "A.jsonl",
        [("2026-09-01", "100"), ("2026-09-02", "101"),
         ("2026-09-05", "200"), ("2026-09-10", "400")],
        bad_future="2026-09-11",
    )
    _jsonl(
        none_root / "bounded_jsonl" / "B.jsonl",
        [(day, "100") for day in dates], bad_future="2026-09-11",
    )
    _jsonl(
        adjusted_root / "bounded_jsonl" / "B.jsonl",
        [("2026-09-01", "100"), ("2026-09-02", "100.5"),
         ("2026-09-05", "100.5"), ("2026-09-10", "100.5")],
        bad_future="2026-09-11",
    )
    (adjusted_root / "split_adjusted_snapshot_manifest.json").write_bytes(b"{}\n")
    adjusted_manifest = {
        "ensemble_sha256": "adjusted-ensemble",
        "entries": [
            {"canonical_ticker": "A", "requested_provider_symbol": "A"},
            {"canonical_ticker": "B", "requested_provider_symbol": "B"},
        ],
    }
    return none_root, adjusted_root, adjusted_manifest


def test_detection_is_bounded_common_date_decimal_and_strict(monkeypatch, tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    write_deterministic_csv(
        parent / "h2_v2_1_inputs_dev.csv",
        [
            _input_row("A", "A-EARLY", reference_session_date="2026-09-05"),
            _input_row("A", "A-LATE", reference_session_date="2026-09-10"),
            _input_row("B", "B-1"),
        ], INPUT_COLUMNS,
    )
    write_deterministic_csv(parent / "h2_v2_1_inputs_prosp_batch1.csv", [], INPUT_COLUMNS)
    write_deterministic_csv(parent / "h2_v2_1_exclusions.csv", [], EXCLUSION_COLUMNS)
    none_root, adjusted_root, adjusted_manifest = _factor_sources(tmp_path)
    monkeypatch.setattr(factors, "verify_price_ensemble", lambda _root: {
        "ensemble_sha256": factors.NONE_ENSEMBLE_SHA256,
        "adjustment_policy": {"provider_adjust": "none"},
    })
    monkeypatch.setattr(
        factors, "verify_split_adjusted_snapshot", lambda _root: adjusted_manifest,
    )
    detections = factors.detect_split_factors(
        parent_root=parent, none_price_root=none_root, adjusted_root=adjusted_root,
    )
    assert [(item["ticker"], item["date"]) for item in detections] == [
        ("A", "2026-09-05"), ("A", "2026-09-10"),
    ]
    assert detections[0]["previous_common_date"] == "2026-09-02"
    assert detections[1]["factor_change_ratio"] == "2"
    # 2026-09-02 is exactly +1%; B remains below 1%. Neither is detected.


def test_event_bounds_reasons_and_outputs_are_deterministic(monkeypatch, tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    write_deterministic_csv(
        parent / "h2_v2_1_inputs_dev.csv",
        [
            _input_row(
                "A", "A-LOWER", price_window_start="2026-09-05",
                row_status="EXCLUDED", exclusion_reasons="PAIRING_FALLBACK",
            ),
            _input_row(
                "A", "A-UPPER", price_window_start="2026-09-01",
                reference_session_date="2026-09-05",
            ),
            _input_row("B", "B-1"),
        ], INPUT_COLUMNS,
    )
    write_deterministic_csv(parent / "h2_v2_1_inputs_prosp_batch1.csv", [], INPUT_COLUMNS)
    write_deterministic_csv(parent / "h2_v2_1_exclusions.csv", [], EXCLUSION_COLUMNS)
    none_root, adjusted_root, adjusted_manifest = _factor_sources(tmp_path)
    monkeypatch.setattr(factors, "verify_price_ensemble", lambda _root: {
        "ensemble_sha256": factors.NONE_ENSEMBLE_SHA256,
        "adjustment_policy": {"provider_adjust": "none"},
    })
    monkeypatch.setattr(
        factors, "verify_split_adjusted_snapshot", lambda _root: adjusted_manifest,
    )
    fixed_now = lambda: datetime(2026, 11, 20, 19, tzinfo=timezone.utc)
    first = factors.apply_split_factor_screen(
        parent_root=parent, none_price_root=none_root, adjusted_root=adjusted_root,
        output_root=tmp_path / "out1", now=fixed_now,
    )
    second = factors.apply_split_factor_screen(
        parent_root=parent, none_price_root=none_root, adjusted_root=adjusted_root,
        output_root=tmp_path / "out2", now=fixed_now,
    )
    assert first == second
    for name in (
        "h2_v2_1_inputs_dev.csv", "h2_v2_1_inputs_prosp_batch1.csv",
        "h2_v2_1_exclusions.csv", "h2_v2_1_detected_split_factors.csv",
        "h2_v2_1_phase1_1_b_manifest.json",
    ):
        assert (tmp_path / "out1" / name).read_bytes() == (tmp_path / "out2" / name).read_bytes()
    rows = factors._read_csv(tmp_path / "out1" / "h2_v2_1_inputs_dev.csv")
    lower = next(row for row in rows if row["current_release_id"] == "A-LOWER")
    upper = next(row for row in rows if row["current_release_id"] == "A-UPPER")
    assert lower["exclusion_reasons"] == "PAIRING_FALLBACK;CORPORATE_ACTION_SPLIT"
    assert upper["exclusion_reasons"] == "CORPORATE_ACTION_SPLIT"
    exclusions = factors._read_csv(tmp_path / "out1" / "h2_v2_1_exclusions.csv")
    lower_exclusion = next(row for row in exclusions if row["current_release_id"] == "A-LOWER")
    assert "2026-09-05" not in lower_exclusion["detail"]
    assert "2026-09-10:2" in lower_exclusion["detail"]
    upper_exclusion = next(row for row in exclusions if row["current_release_id"] == "A-UPPER")
    assert "2026-09-05" in upper_exclusion["detail"]
    assert first["corporate_action_split_event_count"] == {"DEV": 2, "PROSP_BATCH1": 0}


def test_factor_module_has_no_outcome_dependency():
    source = inspect.getsource(factors)
    assert "calculate_event_outcomes" not in source
    assert "build_experimental_dataset" not in source
    assert "trinity.outcomes" not in source
