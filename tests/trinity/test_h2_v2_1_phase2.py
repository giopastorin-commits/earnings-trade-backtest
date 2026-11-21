from __future__ import annotations

import csv
from datetime import date, timedelta
import inspect
import json
import math
from pathlib import Path

import pytest

from trinity.h2_v2_1_inputs import ordered_bar_bytes
import trinity.h2_v2_1_phase2 as phase2


def _sessions(start: date, count: int) -> list[date]:
    result = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _jsonl(path: Path, sessions: list[date], multiplier: float = 1.0, *, bad_future=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(
        ordered_bar_bytes({
            "date": day.isoformat(),
            "open": (100 + index) * multiplier,
            "high": (101 + index) * multiplier,
            "low": (99 + index) * multiplier,
            "close": (100 + index) * multiplier,
            "volume": 1000 + index,
        })
        for index, day in enumerate(sessions)
    )
    if bad_future:
        payload += f'{{"date":"{bad_future}",BROKEN\n'.encode()
    path.write_bytes(payload)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    columns = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _input(accession: str, ticker: str, reference: date, index: int = 0):
    return {
        "current_release_id": accession,
        "ticker": ticker,
        "release_at": reference.isoformat(),
        "event_timing": "PRE_MARKET" if index % 3 == 2 else "AFTER_MARKET",
        "reference_session_date": reference.isoformat(),
        "row_status": "INCLUDED",
        "exclusion_reasons": "",
        "pairing_quality": "EXACT",
        "h2_method": "TFIDF_COSINE_V1",
        "h2_novelty": 0.05 + ((index * 7) % 13) / 20,
        "timing_ambiguous": "false",
        "reference_resolution": "CALENDAR_AFTER_MARKET",
        "sigma_pre_60": math.exp(1 + 0.04 * index),
        "dollar_volume_pre_60": math.exp(10 + 0.02 * index * index),
        "current_word_count": 100 + index ** 3 + index,
        "previous_word_count": 70 + index ** 2,
        "price_window_sha256": f"window-{index}",
    }


def test_dev_outcome_reader_stops_at_d20_and_writes_no_prices(tmp_path):
    sessions = _sessions(date(2026, 1, 2), 30)
    reference = sessions[5]
    dev = tmp_path / "dev.csv"
    _write_csv(dev, [_input("ACC-1", "AAA", reference)])
    none = tmp_path / "none" / "bounded_jsonl"
    split = tmp_path / "split" / "bounded_jsonl"
    _jsonl(none / "AAA.jsonl", sessions[:26], bad_future=sessions[26].isoformat())
    _jsonl(none / "SPY.jsonl", sessions[:26], multiplier=4, bad_future=sessions[26].isoformat())
    _jsonl(split / "AAA.jsonl", sessions[:26], bad_future=sessions[26].isoformat())
    output = tmp_path / "outcomes.csv"
    result = phase2.build_dev_outcomes(
        dev_path=dev, price_root=tmp_path / "none",
        split_root=tmp_path / "split", output_path=output,
    )
    assert result["admitted_count"] == 1
    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["future_session_date_20"] == sessions[25].isoformat()
    assert row["outcome_status_20"] == "COMPLETE"
    assert "reference_price" not in row
    assert "future_close" not in row


def test_post_reference_factor_change_excludes_dev_event(tmp_path):
    sessions = _sessions(date(2026, 1, 2), 30)
    reference = sessions[5]
    dev = tmp_path / "dev.csv"
    _write_csv(dev, [_input("ACC-1", "AAA", reference)])
    none = tmp_path / "none" / "bounded_jsonl"
    split = tmp_path / "split" / "bounded_jsonl"
    _jsonl(none / "AAA.jsonl", sessions[:26])
    _jsonl(none / "SPY.jsonl", sessions[:26], multiplier=4)
    split.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, day in enumerate(sessions[:26]):
        factor = 2 if index >= 10 else 1
        rows.append(ordered_bar_bytes({
            "date": day.isoformat(), "open": (100 + index) * factor,
            "high": (101 + index) * factor, "low": (99 + index) * factor,
            "close": (100 + index) * factor, "volume": 1000 + index,
        }))
    (split / "AAA.jsonl").write_bytes(b"".join(rows))
    output = tmp_path / "outcomes.csv"
    result = phase2.build_dev_outcomes(
        dev_path=dev, price_root=tmp_path / "none",
        split_root=tmp_path / "split", output_path=output,
    )
    assert result["admitted_count"] == 0
    assert result["exclusion_counts"] == {"CORPORATE_ACTION_SPLIT": 1}


def _estimation_files(tmp_path: Path):
    inputs = [_input(f"ACC-{index:02d}", f"T{index % 5}", date(2026, 1, 5), index) for index in range(14)]
    outcomes = []
    for index, row in enumerate(inputs):
        outcomes.append({
            "current_release_id": row["current_release_id"],
            "ticker": row["ticker"],
            "release_at": row["release_at"],
            "event_timing": row["event_timing"],
            "reference_session_date": row["reference_session_date"],
            "future_session_date_20": "2026-02-02",
            "outcome_status_20": "COMPLETE",
            "raw_return_pct_20": 1 + index / 10,
            "benchmark_return_pct_20": 0.2,
            "excess_return_pct_20": (-1 if index % 2 else 1) * (0.8 + index / 20),
            "absolute_return_pct_20": 1 + index / 10,
            "phase2_row_status": "INCLUDED",
            "phase2_exclusion_reasons": "",
        })
    dev = tmp_path / "dev.csv"
    outcome_path = tmp_path / "outcomes.csv"
    _write_csv(dev, inputs)
    _write_csv(outcome_path, outcomes)
    return dev, outcome_path, inputs


def test_models_and_predictions_are_deterministic_and_outcome_free(monkeypatch, tmp_path):
    dev, outcomes, inputs = _estimation_files(tmp_path)
    model1 = tmp_path / "model1.json"
    model2 = tmp_path / "model2.json"
    from datetime import datetime, timezone
    fixed = datetime(2026, 11, 21, 11, tzinfo=timezone.utc)
    phase2.build_model(dev_path=dev, outcomes_path=outcomes, output_path=model1, built_at=fixed)
    phase2.build_model(dev_path=dev, outcomes_path=outcomes, output_path=model2, built_at=fixed)
    assert model1.read_bytes() == model2.read_bytes()
    artifact = json.loads(model1.read_text())
    assert set(artifact["models"]) == {"M0", "M1", "M0x", "M1x", "M0e", "M1e"}
    assert artifact["models"]["M1x"]["columns"][-1] == "h2_novelty"

    prosp = tmp_path / "prosp.csv"
    _write_csv(prosp, inputs[:3])
    monkeypatch.setattr(phase2, "PROSP_SHA256", phase2.sha256_file(prosp))
    predictions = tmp_path / "predictions.csv"
    result = phase2.build_predictions(
        prosp_path=prosp, model_path=model1, output_path=predictions,
    )
    assert result["predicted_count"] == 3
    header = predictions.read_text().splitlines()[0]
    assert "outcome" not in header
    assert "price" not in header
    signature = inspect.signature(phase2.build_predictions)
    assert "price_root" not in signature.parameters
    source = inspect.getsource(phase2.build_predictions)
    assert "calculate_event_outcomes" not in source


def test_missing_dollar_volume_blanks_only_extended_predictions(monkeypatch, tmp_path):
    dev, outcomes, inputs = _estimation_files(tmp_path)
    model = tmp_path / "model.json"
    phase2.build_model(dev_path=dev, outcomes_path=outcomes, output_path=model)
    inputs[0]["dollar_volume_pre_60"] = ""
    prosp = tmp_path / "prosp.csv"
    _write_csv(prosp, [inputs[0]])
    monkeypatch.setattr(phase2, "PROSP_SHA256", phase2.sha256_file(prosp))
    predictions = tmp_path / "predictions.csv"
    phase2.build_predictions(prosp_path=prosp, model_path=model, output_path=predictions)
    with predictions.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["pred_0x"] == ""
    assert row["pred_1x"] == ""
    assert row["pred_0"] != ""
    assert row["pred_1e"] != ""


def test_d20_uses_xnys_calendar():
    assert phase2._d20("2026-10-21") == "2026-11-18"


def test_part_a_is_deterministic_on_dev_only(tmp_path):
    dev, outcomes, _inputs = _estimation_files(tmp_path)
    model = tmp_path / "model.json"
    phase2.build_model(dev_path=dev, outcomes_path=outcomes, output_path=model)
    first_report = tmp_path / "part_a_1.md"
    second_report = tmp_path / "part_a_2.md"
    first = phase2.run_part_a(
        dev_path=dev, outcomes_path=outcomes, model_path=model,
        output_path=first_report, reps=100, seed=20261118,
    )
    second = phase2.run_part_a(
        dev_path=dev, outcomes_path=outcomes, model_path=model,
        output_path=second_report, reps=100, seed=20261118,
    )
    assert first_report.read_bytes() == second_report.read_bytes()
    assert first["M1"]["valid_reps"] == second["M1"]["valid_reps"]
    assert first["bootstrap"] == {
        "weights": "Rademacher", "null_imposed": True, "reps": 100,
        "seed": 20261118, "minimum_valid_reps": 95,
    }
    assert first_report.read_text(encoding="utf-8").startswith(
        "# Analisi di robustezza, non confermativa (N piccolo)"
    )


def test_last_completed_session_before_saturday_seal():
    from datetime import datetime, timezone

    assert phase2._last_completed_session(
        datetime(2026, 11, 21, 11, tzinfo=timezone.utc)
    ) == "2026-11-20"
