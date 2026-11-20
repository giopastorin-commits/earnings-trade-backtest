from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from trinity.h2_v2_1_inputs import (
    EXCLUSION_COLUMNS,
    INPUT_COLUMNS,
    canonical_json_bytes,
    file_inventory,
    write_deterministic_csv,
)
import trinity.twelvedata_splits as splits
from trinity.twelvedata_prices import CreditGuardError, canonical_bytes


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


def _usage(*, daily_limit: int, daily_usage: int = 0) -> bytes:
    return json.dumps({
        "timestamp": "2026-11-20 15:00:00", "current_usage": 0,
        "plan_limit": 100, "plan_category": "test",
        "daily_usage": daily_usage, "plan_daily_limit": daily_limit,
    }).encode()


def _client(outcomes):
    session = _Session(outcomes)
    clock = iter(float(item) for item in range(10000))
    client = splits.TwelveDataSplitClient(
        api_key="unit-secret", session=session, throttle=_NoThrottle(),
        split_throttle=_NoThrottle(),
        now=lambda: datetime(2026, 11, 20, 15, tzinfo=timezone.utc),
        monotonic=lambda: next(clock), sleep=lambda _seconds: None,
    )
    return client, session


def _row(**updates):
    row = {column: "" for column in INPUT_COLUMNS}
    row.update({
        "sample": "DEV", "release_period": "DEV_PERIOD",
        "row_status": "INCLUDED", "current_release_id": "A-1",
        "ticker": "A", "release_at": "2026-09-10T21:00:00Z",
        "event_timing": "AFTER_MARKET", "reference_session_date": "2026-09-10",
        "price_window_start": "2026-09-01",
    })
    row.update(updates)
    return row


def _parent(root: Path, *, include_full_universe: bool = False) -> Path:
    root.mkdir(parents=True)
    write_deterministic_csv(
        root / "h2_v2_1_inputs_dev.csv",
        [
            _row(row_status="EXCLUDED", exclusion_reasons="PAIRING_FALLBACK"),
            _row(current_release_id="B-1", ticker="B"),
        ], INPUT_COLUMNS,
    )
    write_deterministic_csv(
        root / "h2_v2_1_inputs_prosp_batch1.csv",
        [_row(
            sample="PROSP_BATCH1", release_period="PROSP_PERIOD",
            current_release_id="SKYD-1", ticker="SKYD",
        )], INPUT_COLUMNS,
    )
    exclusions = []
    if include_full_universe:
        present = {"A", "B", "SKYD"}
        for ticker in _tickers():
            if ticker in present:
                continue
            exclusions.append({
                "sample": "DEV", "release_period": "DEV_PERIOD", "ticker": ticker,
                "current_release_id": f"{ticker}-X", "release_at": "", "event_timing": "",
                "reference_session_date": "", "reason": "NO_EX99", "detail": "",
            })
    write_deterministic_csv(
        root / "h2_v2_1_exclusions.csv", exclusions, EXCLUSION_COLUMNS,
    )
    return root


def _tickers() -> list[str]:
    extras = [f"T{index:03d}" for index in range(splits.SPLIT_CANONICAL_TICKER_COUNT - 3)]
    return sorted(["A", "B", "SKYD", *extras])


def _snapshot(root: Path) -> Path:
    normalized = root / "normalized"
    normalized.mkdir(parents=True)
    action_map = {
        "A": [
            {"date": "2026-09-01", "description": "at lower bound"},
            {"date": "2026-09-05", "description": "2-for-1 split"},
            {"date": "2026-09-10", "description": "at upper bound"},
            {"date": "2026-09-11", "description": "after reference"},
        ],
        "B": [{"date": "2026-09-01", "description": "lower bound only"}],
        "SKYD": [{"date": "2026-09-06", "description": "1-for-5 reverse split"}],
    }
    entries = []
    tickers = _tickers()
    for ticker in tickers:
        payload = canonical_bytes(action_map.get(ticker, []))
        (normalized / f"{ticker}.json").write_bytes(payload)
        entries.append({
            "canonical_ticker": ticker,
            "requested_provider_symbol": "PSKY" if ticker == "SKYD" else ticker,
            "provider_symbol": "PSKY" if ticker == "SKYD" else ticker,
            "classification": "COMPLETE", "provider_row_count": len(action_map.get(ticker, [])),
            "normalized_row_count": len(action_map.get(ticker, [])),
            "content_sha256": hashlib.sha256(payload).hexdigest(), "raw_sha256": None,
            "attempts": [], "retry_count": 0, "error": None,
        })
    inventory = file_inventory(root, exclude_names=frozenset({"split_snapshot_manifest.json"}))
    manifest = {
        "schema_name": splits.SPLIT_SCHEMA,
        "schema_version": splits.SPLIT_SCHEMA_VERSION,
        "provider": "TWELVE_DATA", "endpoint": splits.SPLITS_ENDPOINT,
        "canonical_ticker_count": len(tickers), "canonical_tickers": tickers,
        "canonical_tickers_sha256": hashlib.sha256(canonical_bytes(tickers)).hexdigest(),
        "coverage_gate": {"ready": True, "blocking_tickers": []},
        "tickers": entries, "files": inventory,
        "ensemble_sha256": hashlib.sha256(canonical_bytes(inventory)).hexdigest(),
    }
    (root / "split_snapshot_manifest.json").write_bytes(
        canonical_json_bytes(manifest, newline=True)
    )
    return root


def test_normalization_preserves_splits_and_filters_frozen_dates():
    raw = json.dumps({
        "meta": {"symbol": "A"},
        "splits": [
            {"date": "2025-08-23", "description": "too early", "ratio": 0.5},
            {"date": "2025-08-24", "description": "2-for-1 split", "ratio": 0.5},
            {"date": "2026-11-20", "description": "1-for-5 reverse split", "ratio": 5},
            {"date": "2026-11-21", "description": "too late", "ratio": 2},
        ],
    }).encode()
    result = splits.normalize_split_response(raw, expected_provider_symbol="A")
    assert result.classification == "COMPLETE"
    assert result.provider_row_count == 4
    assert [row["date"] for row in result.actions] == ["2025-08-24", "2026-11-20"]
    assert [row["description"] for row in result.actions] == [
        "2-for-1 split", "1-for-5 reverse split",
    ]


def test_credit_guard_fails_before_first_split_request(tmp_path):
    parent = _parent(tmp_path / "parent", include_full_universe=True)
    client, session = _client([_Response(_usage(daily_limit=9191))])
    root = tmp_path / "splits"
    with pytest.raises(CreditGuardError, match="INSUFFICIENT_DAILY_CREDITS"):
        splits.acquire_split_snapshot(
            parent_root=parent, output_root=root, client=client, seal_read_only=False,
        )
    assert len(session.calls) == 1
    evidence = json.loads((root / "acquisition_failure.json").read_text())
    assert evidence["required"] == 9192
    assert not (root / "split_snapshot_manifest.json").exists()


def test_psky_is_requested_without_skyd_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(splits, "SPLIT_CANONICAL_TICKER_COUNT", 1)
    monkeypatch.setattr(splits, "split_universe", lambda _root: ["SKYD"])
    client, session = _client([
        _Response(_usage(daily_limit=1000)),
        _Response(b'{"status":"error","code":400,"message":"not available"}', status=400),
    ])
    manifest = splits.acquire_split_snapshot(
        parent_root=tmp_path / "unused", output_root=tmp_path / "splits",
        client=client, seal_read_only=False,
    )
    assert len(session.calls) == 2
    assert session.calls[1][1]["params"]["symbol"] == "PSKY"
    assert all(call[1]["params"].get("symbol") != "SKYD" for call in session.calls[1:])
    assert manifest["tickers"][0]["classification"] == "PROVIDER_FAILURE"
    assert manifest["coverage_gate"]["ready"] is False


def test_split_screen_boundaries_preserve_reasons_and_are_deterministic(tmp_path):
    parent = _parent(tmp_path / "parent")
    snapshot = _snapshot(tmp_path / "snapshot")
    fixed_now = lambda: datetime(2026, 11, 20, 16, tzinfo=timezone.utc)
    first = splits.apply_split_screen(
        parent_root=parent, split_root=snapshot, output_root=tmp_path / "out1", now=fixed_now,
    )
    second = splits.apply_split_screen(
        parent_root=parent, split_root=snapshot, output_root=tmp_path / "out2", now=fixed_now,
    )
    assert first == second
    for name in (
        "h2_v2_1_inputs_dev.csv", "h2_v2_1_inputs_prosp_batch1.csv",
        "h2_v2_1_exclusions.csv", "h2_v2_1_phase1_1_manifest.json",
        "h2_v2_1_split_source_manifest.json",
    ):
        assert (tmp_path / "out1" / name).read_bytes() == (tmp_path / "out2" / name).read_bytes()
    dev = splits._read_csv(tmp_path / "out1" / "h2_v2_1_inputs_dev.csv")
    a = next(row for row in dev if row["ticker"] == "A")
    b = next(row for row in dev if row["ticker"] == "B")
    assert a["row_status"] == "EXCLUDED"
    assert a["exclusion_reasons"] == f"PAIRING_FALLBACK;{splits.SPLIT_REASON}"
    assert b["row_status"] == "INCLUDED"  # action exactly at lower bound is excluded
    exclusions = splits._read_csv(tmp_path / "out1" / "h2_v2_1_exclusions.csv")
    a_exclusion = next(row for row in exclusions if row["current_release_id"] == "A-1")
    assert a_exclusion["reason"] == splits.SPLIT_REASON
    assert a_exclusion["detail"].endswith("2026-09-05,2026-09-10")
    assert first["corporate_action_split_event_count"] == {"DEV": 1, "PROSP_BATCH1": 1}
    assert first["primary_eligible_count"] == {"DEV": 1, "PROSP_BATCH1": 0}


def test_split_module_has_no_outcome_engine_dependency():
    source = inspect.getsource(splits)
    assert "calculate_event_outcomes" not in source
    assert "build_experimental_dataset" not in source
    assert "trinity.outcomes" not in source
