"""Frozen H2 V2.1 Phase 2 DEV estimation and outcome-free sealing."""

from __future__ import annotations

from collections import Counter
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import platform
import re
from typing import Any, Mapping, Sequence

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from trinity.h2_v2_1_inputs import (
    canonical_json_bytes,
    sha256_file,
    verify_price_ensemble,
    write_deterministic_csv,
)
from trinity.outcomes import OutcomeInputError, calculate_event_outcomes
from trinity.twelvedata_split_factors import verify_split_adjusted_snapshot


DEV_SHA256 = "e1f3e7671422ece25cc6a0ffba9c4bbca4e11ad84d7e080bc349932466817501"
PROSP_SHA256 = "2a01fb5d0cbf8b110779bd61d2f94116877dde15fbef1dd4a91d757d3e9a1493"
NONE_ENSEMBLE_SHA256 = "8dd59aae14c92f9d8dd9a6c055c166157e8b49b0d6ce0b48591080277ec7a54f"
SPLIT_ENSEMBLE_SHA256 = "44dc22079a57f4e60a07822c259503377bd80f72a04929bffc25db9a802c50d2"
PROVENANCE_REPORT_SHA256 = "a922e4e7e0ea0d32ac4e0e91384a1017e27023679d155a846c44a6e7a0944adf"
PRE_EVENT_COMMIT = "bee6463372e541519aae88cd98481bba27839875"
PHASE2_SCHEMA = "trinity.h2-v2-1-phase2-model"
MODEL_VERSION = "1"
SPLIT_REASON = "CORPORATE_ACTION_SPLIT"
SPLIT_UNAVAILABLE = "SPLIT_CHECK_UNAVAILABLE"
PART_A_BOOTSTRAP_REPS = 10_000
PART_A_BOOTSTRAP_SEED = 20_261_118
PART_A_MIN_VALID_REPS = 9_500

OUTCOME_COLUMNS = (
    "current_release_id", "ticker", "release_at", "event_timing",
    "reference_session_date", "future_session_date_20", "outcome_status_20",
    "raw_return_pct_20", "benchmark_return_pct_20", "excess_return_pct_20",
    "absolute_return_pct_20", "phase2_row_status", "phase2_exclusion_reasons",
)
PREDICTION_COLUMNS = (
    "current_release_id", "ticker", "reference_session_date",
    "pred_0", "pred_1", "pred_0x", "pred_1x", "pred_0e", "pred_1e",
)
MODEL_COLUMNS = {
    "M0": ("intercept", "ln_sigma_pre_60"),
    "M1": ("intercept", "ln_sigma_pre_60", "h2_novelty"),
    "M0x": (
        "intercept", "ln_sigma_pre_60", "ln_dollar_volume_pre_60",
        "log_word_ratio", "log_word_count", "after_market",
    ),
    "M1x": (
        "intercept", "ln_sigma_pre_60", "ln_dollar_volume_pre_60",
        "log_word_ratio", "log_word_count", "after_market", "h2_novelty",
    ),
    "M0e": ("intercept", "ln_sigma_pre_60"),
    "M1e": ("intercept", "ln_sigma_pre_60", "h2_novelty"),
}


class Phase2Error(ValueError):
    """The frozen Phase 2 contract was violated."""


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise Phase2Error(f"invalid CSV: {path}") from exc
    ids = [str(row.get("current_release_id") or "") for row in rows]
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if "" in ids or duplicates:
        raise Phase2Error(f"invalid or duplicate accessions in {path}: {duplicates[:5]}")
    return rows


def verify_frozen_sources(
    *, dev_path: str | Path, prosp_path: str | Path,
    price_root: str | Path, split_root: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if sha256_file(dev_path) != DEV_SHA256:
        raise Phase2Error("frozen DEV SHA-256 mismatch")
    if sha256_file(prosp_path) != PROSP_SHA256:
        raise Phase2Error("frozen PROSP_BATCH1 SHA-256 mismatch")
    price_manifest = verify_price_ensemble(price_root)
    if price_manifest.get("ensemble_sha256") != NONE_ENSEMBLE_SHA256:
        raise Phase2Error("frozen unadjusted ensemble SHA-256 mismatch")
    split_manifest = verify_split_adjusted_snapshot(split_root)
    if split_manifest.get("ensemble_sha256") != SPLIT_ENSEMBLE_SHA256:
        raise Phase2Error("frozen split-adjusted ensemble SHA-256 mismatch")
    return price_manifest, split_manifest


_DATE_PREFIX = re.compile(br'^\{"date":"(\d{4}-\d{2}-\d{2})"')


def _row_date(line: bytes) -> date:
    match = _DATE_PREFIX.match(line)
    if match is None:
        raise Phase2Error("price JSONL row lacks the required leading date")
    return date.fromisoformat(match.group(1).decode("ascii"))


def _read_through_horizon(path: Path, reference: date, horizon: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous: date | None = None
    reference_seen = False
    future_count = 0
    with path.open("rb") as handle:
        for line in handle:
            day = _row_date(line)
            if previous is not None and day <= previous:
                raise Phase2Error(f"price sessions not strictly ascending: {path}")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise Phase2Error(f"price JSONL row is not an object: {path}")
            rows.append(row)
            previous = day
            if day == reference:
                reference_seen = True
            elif reference_seen and day > reference:
                future_count += 1
                if future_count == horizon:
                    break
    if not reference_seen:
        raise Phase2Error(f"reference session absent from DEV price source: {path.name}")
    if future_count < horizon:
        return rows
    return rows


def _read_at_or_before(path: Path, cutoff: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous: date | None = None
    with path.open("rb") as handle:
        for line in handle:
            day = _row_date(line)
            if previous is not None and day <= previous:
                raise Phase2Error(f"price sessions not strictly ascending: {path}")
            if day > cutoff:
                break
            row = json.loads(line)
            if not isinstance(row, dict):
                raise Phase2Error(f"price JSONL row is not an object: {path}")
            rows.append(row)
            previous = day
    return rows


def _close(row: Mapping[str, Any], ticker: str, day: date) -> Decimal:
    try:
        value = Decimal(str(row["close"]))
    except (KeyError, InvalidOperation) as exc:
        raise Phase2Error(f"invalid split-factor close: {ticker} {day}") from exc
    if not value.is_finite() or value <= 0:
        raise Phase2Error(f"invalid split-factor close: {ticker} {day}")
    return value


def _post_reference_split_reason(
    *, ticker: str, reference: date, future: date,
    none_bars: Sequence[Mapping[str, Any]], split_path: Path,
) -> str | None:
    split_bars = _read_at_or_before(split_path, future)
    none_by_date = {
        date.fromisoformat(str(row["date"])): row
        for row in none_bars
        if reference <= date.fromisoformat(str(row["date"])) <= future
    }
    split_by_date = {
        date.fromisoformat(str(row["date"])): row
        for row in split_bars
        if reference <= date.fromisoformat(str(row["date"])) <= future
    }
    required = sorted(none_by_date)
    if not required or required[0] != reference or required[-1] != future \
            or any(day not in split_by_date for day in required):
        return SPLIT_UNAVAILABLE
    previous_ratio: Decimal | None = None
    with localcontext() as context:
        context.prec = 34
        for day in required:
            ratio = _close(split_by_date[day], ticker, day) / _close(
                none_by_date[day], ticker, day,
            )
            if previous_ratio is not None:
                change = ratio / previous_ratio
                if day > reference and abs(change - Decimal(1)) > Decimal("0.01"):
                    return SPLIT_REASON
            previous_ratio = ratio
    return None


def _blank_outcome(row: Mapping[str, str]) -> dict[str, Any]:
    return {
        "current_release_id": row["current_release_id"],
        "ticker": row.get("ticker", ""),
        "release_at": row.get("release_at", ""),
        "event_timing": row.get("event_timing", ""),
        "reference_session_date": row.get("reference_session_date", ""),
        "future_session_date_20": "",
        "outcome_status_20": "",
        "raw_return_pct_20": "",
        "benchmark_return_pct_20": "",
        "excess_return_pct_20": "",
        "absolute_return_pct_20": "",
        "phase2_row_status": "EXCLUDED",
        "phase2_exclusion_reasons": row.get("exclusion_reasons", ""),
    }


def _bool_false(value: object) -> bool:
    return str(value).strip().lower() in {"false", "0"}


def _primary_contract_reasons(row: Mapping[str, str], outcome: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if row.get("pairing_quality") != "EXACT":
        reasons.append("PAIRING_NOT_EXACT")
    if row.get("h2_method") != "TFIDF_COSINE_V1":
        reasons.append("H2_METHOD_INVALID")
    if not str(row.get("h2_novelty") or "").strip():
        reasons.append("H2_NOVELTY_MISSING")
    if outcome.get("outcome_status") != "COMPLETE":
        reasons.append("OUTCOME_STATUS_20_NOT_COMPLETE")
    if outcome.get("absolute_return_pct") is None:
        reasons.append("ABSOLUTE_RETURN_20_MISSING")
    if not _bool_false(row.get("timing_ambiguous")):
        reasons.append("TIMING_AMBIGUOUS")
    if row.get("reference_resolution") == "AMBIGUOUS":
        reasons.append("REFERENCE_AMBIGUOUS")
    if not str(row.get("sigma_pre_60") or "").strip():
        reasons.append("SIGMA_PRE_60_MISSING")
    return reasons


def build_dev_outcomes(
    *, dev_path: str | Path, price_root: str | Path,
    split_root: str | Path, output_path: str | Path,
) -> dict[str, Any]:
    rows = _read_csv(dev_path)
    none_jsonl = Path(price_root) / "bounded_jsonl"
    split_jsonl = Path(split_root) / "bounded_jsonl"
    output_rows: list[dict[str, Any]] = []
    for source in sorted(rows, key=lambda item: item["current_release_id"]):
        if source.get("row_status") != "INCLUDED":
            output_rows.append(_blank_outcome(source))
            continue
        ticker = str(source["ticker"])
        reference = date.fromisoformat(str(source["reference_session_date"]))
        issuer_bars = _read_through_horizon(
            none_jsonl / f"{ticker}.jsonl", reference, 20,
        )
        try:
            result = calculate_event_outcomes(
                ticker=ticker,
                earnings_event_at=str(source["release_at"]),
                timing=str(source["event_timing"]),
                ohlcv=issuer_bars,
                benchmark_ohlcv=_read_at_or_before(
                    none_jsonl / "SPY.jsonl",
                    date.fromisoformat(str(issuer_bars[-1]["date"])),
                ),
                horizons=(20,),
            )[0]
        except OutcomeInputError as exc:
            raise Phase2Error(f"Outcome Engine rejected DEV accession {source['current_release_id']}") from exc
        if result.get("reference_session_date") != reference.isoformat():
            raise Phase2Error(f"reference mismatch for DEV accession {source['current_release_id']}")
        reasons = _primary_contract_reasons(source, result)
        future_text = str(result.get("future_session_date") or "")
        if result.get("outcome_status") == "COMPLETE":
            if not future_text:
                raise Phase2Error("complete DEV outcome has no D20 session")
            split_reason = _post_reference_split_reason(
                ticker=ticker, reference=reference, future=date.fromisoformat(future_text),
                none_bars=issuer_bars,
                split_path=split_jsonl / f"{ticker}.jsonl",
            )
            if split_reason:
                reasons.append(split_reason)
        if result.get("outcome_status") == "COMPLETE" \
                and result.get("excess_return_pct") is None:
            raise Phase2Error(f"SPY benchmark outcome missing for {source['current_release_id']}")
        output_rows.append({
            "current_release_id": source["current_release_id"],
            "ticker": ticker,
            "release_at": source["release_at"],
            "event_timing": source["event_timing"],
            "reference_session_date": reference.isoformat(),
            "future_session_date_20": future_text,
            "outcome_status_20": result.get("outcome_status") or "",
            "raw_return_pct_20": result.get("raw_return_pct"),
            "benchmark_return_pct_20": result.get("benchmark_return_pct"),
            "excess_return_pct_20": result.get("excess_return_pct"),
            "absolute_return_pct_20": result.get("absolute_return_pct"),
            "phase2_row_status": "INCLUDED" if not reasons else "EXCLUDED",
            "phase2_exclusion_reasons": ";".join(dict.fromkeys(reasons)),
        })
    write_deterministic_csv(output_path, output_rows, OUTCOME_COLUMNS)
    reason_counts: Counter[str] = Counter()
    for row in output_rows:
        for reason in str(row["phase2_exclusion_reasons"] or "").split(";"):
            if reason:
                reason_counts[reason] += 1
    return {
        "dev_row_count": len(output_rows),
        "admitted_count": sum(row["phase2_row_status"] == "INCLUDED" for row in output_rows),
        "excluded_count": sum(row["phase2_row_status"] != "INCLUDED" for row in output_rows),
        "exclusion_counts": dict(sorted(reason_counts.items())),
        "sha256": sha256_file(output_path),
    }


def _number(row: Mapping[str, Any], name: str) -> float:
    try:
        value = float(row[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise Phase2Error(f"invalid {name} for {row.get('current_release_id')}") from exc
    if not math.isfinite(value):
        raise Phase2Error(f"non-finite {name} for {row.get('current_release_id')}")
    return value


def _features(row: Mapping[str, Any]) -> dict[str, float]:
    sigma = _number(row, "sigma_pre_60")
    if sigma <= 0:
        raise Phase2Error("sigma_pre_60 must be positive for its frozen logarithm")
    current_words = _number(row, "current_word_count")
    previous_words = _number(row, "previous_word_count")
    if current_words <= 0 or previous_words <= 0:
        raise Phase2Error("word counts must be positive for their frozen logarithms")
    values = {
        "intercept": 1.0,
        "ln_sigma_pre_60": math.log(sigma),
        "h2_novelty": _number(row, "h2_novelty"),
        "log_word_ratio": math.log(current_words / previous_words),
        "log_word_count": math.log(current_words),
        "after_market": 1.0 if row.get("event_timing") == "AFTER_MARKET" else 0.0,
    }
    dv_text = str(row.get("dollar_volume_pre_60") or "").strip()
    if dv_text:
        dv = _number(row, "dollar_volume_pre_60")
        if dv > 0:
            values["ln_dollar_volume_pre_60"] = math.log(dv)
    return values


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value, newline=False)).hexdigest()


def _fit_model(
    name: str, records: Sequence[tuple[str, dict[str, float], float]],
    response: str,
) -> dict[str, Any]:
    columns = MODEL_COLUMNS[name]
    ordered = sorted(records, key=lambda item: item[0])
    x = np.asarray([[features[column] for column in columns] for _, features, _ in ordered], dtype=np.float64)
    y = np.asarray([target for _, _, target in ordered], dtype=np.float64)
    if x.ndim != 2 or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise Phase2Error(f"non-finite estimation matrix for {name}")
    n, k = x.shape
    rank = int(np.linalg.matrix_rank(x))
    if n <= k or rank != k:
        raise Phase2Error(f"non-estimable full-rank OLS model {name}: N={n} K={k} rank={rank}")
    coefficients, _residuals, fitted_rank, _singular = np.linalg.lstsq(x, y, rcond=None)
    if int(fitted_rank) != k or not np.all(np.isfinite(coefficients)):
        raise Phase2Error(f"OLS failed for {name}")
    estimation_payload = [
        {"current_release_id": accession, "x": [features[col] for col in columns], "y": target}
        for accession, features, target in ordered
    ]
    return {
        "columns": list(columns),
        "coefficients": {
            column: float(value) for column, value in zip(columns, coefficients, strict=True)
        },
        "N": n,
        "K": k,
        "matrix_rank": rank,
        "response": response,
        "transformation": "ln(1 + outcome_in_percentage_points)",
        "estimation_data_sha256": _canonical_hash(estimation_payload),
        "estimation_accessions": [item[0] for item in ordered],
    }


def build_model(
    *, dev_path: str | Path, outcomes_path: str | Path,
    output_path: str | Path, built_at: datetime | None = None,
) -> dict[str, Any]:
    inputs = {row["current_release_id"]: row for row in _read_csv(dev_path)}
    outcomes = _read_csv(outcomes_path)
    primary: list[tuple[str, dict[str, float], float, float]] = []
    extended: list[tuple[str, dict[str, float], float]] = []
    for outcome in outcomes:
        if outcome.get("phase2_row_status") != "INCLUDED":
            continue
        accession = outcome["current_release_id"]
        source = inputs.get(accession)
        if source is None:
            raise Phase2Error(f"outcome accession absent from DEV input: {accession}")
        features = _features(source)
        absolute = _number(outcome, "absolute_return_pct_20")
        excess = abs(_number(outcome, "excess_return_pct_20"))
        y_absolute = math.log1p(absolute)
        y_excess = math.log1p(excess)
        primary.append((accession, features, y_absolute, y_excess))
        if "ln_dollar_volume_pre_60" in features:
            extended.append((accession, features, y_absolute))
    models = {
        "M0": _fit_model("M0", [(a, f, y) for a, f, y, _ in primary], "absolute_return_pct_20"),
        "M1": _fit_model("M1", [(a, f, y) for a, f, y, _ in primary], "absolute_return_pct_20"),
        "M0x": _fit_model("M0x", extended, "absolute_return_pct_20"),
        "M1x": _fit_model("M1x", extended, "absolute_return_pct_20"),
        "M0e": _fit_model("M0e", [(a, f, y) for a, f, _, y in primary], "abs(excess_return_pct_20)"),
        "M1e": _fit_model("M1e", [(a, f, y) for a, f, _, y in primary], "abs(excess_return_pct_20)"),
    }
    artifact = {
        "schema_name": PHASE2_SCHEMA,
        "schema_version": MODEL_VERSION,
        "built_at_utc": (built_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "protocols": ["H2_PROTOCOL_V2_1.md", "H2_PROTOCOL_V2.md"],
        "ols": {"library": "numpy", "function": "numpy.linalg.lstsq", "rcond": None},
        "versions": {
            "python": platform.python_version(), "numpy": np.__version__,
            "pandas": pd.__version__, "exchange_calendars": xcals.__version__,
        },
        "dev_input_sha256": sha256_file(dev_path),
        "dev_outcomes_sha256": sha256_file(outcomes_path),
        "models": models,
    }
    Path(output_path).write_bytes(canonical_json_bytes(artifact, newline=True))
    return artifact


def _predict(model: Mapping[str, Any], features: Mapping[str, float]) -> float:
    columns = model.get("columns")
    coefficients = model.get("coefficients")
    if not isinstance(columns, list) or not isinstance(coefficients, dict):
        raise Phase2Error("invalid frozen model structure")
    y_hat = sum(float(coefficients[column]) * features[column] for column in columns)
    value = math.expm1(y_hat)
    if not math.isfinite(value):
        raise Phase2Error("non-finite frozen-model prediction")
    return value


def build_predictions(
    *, prosp_path: str | Path, model_path: str | Path, output_path: str | Path,
) -> dict[str, Any]:
    """Build predictions without accepting or opening any price source."""

    if sha256_file(prosp_path) != PROSP_SHA256:
        raise Phase2Error("frozen PROSP_BATCH1 SHA-256 mismatch")
    try:
        artifact = json.loads(Path(model_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase2Error("invalid frozen model JSON") from exc
    if artifact.get("schema_name") != PHASE2_SCHEMA or artifact.get("schema_version") != MODEL_VERSION:
        raise Phase2Error("unsupported frozen model")
    models = artifact.get("models")
    if not isinstance(models, dict) or set(models) != set(MODEL_COLUMNS):
        raise Phase2Error("frozen model set mismatch")
    rows: list[dict[str, Any]] = []
    inputs = _read_csv(prosp_path)
    for source in sorted(inputs, key=lambda item: item["current_release_id"]):
        if source.get("row_status") != "INCLUDED":
            continue
        features = _features(source)
        extended = "ln_dollar_volume_pre_60" in features
        rows.append({
            "current_release_id": source["current_release_id"],
            "ticker": source["ticker"],
            "reference_session_date": source["reference_session_date"],
            "pred_0": _predict(models["M0"], features),
            "pred_1": _predict(models["M1"], features),
            "pred_0x": _predict(models["M0x"], features) if extended else "",
            "pred_1x": _predict(models["M1x"], features) if extended else "",
            "pred_0e": _predict(models["M0e"], features),
            "pred_1e": _predict(models["M1e"], features),
        })
    write_deterministic_csv(output_path, rows, PREDICTION_COLUMNS)
    return {
        "predicted_count": len(rows),
        "not_predicted_count": len(inputs) - len(rows),
        "sha256": sha256_file(output_path),
    }


def _d20(reference: str) -> str:
    calendar = xcals.get_calendar("XNYS")
    return calendar.session_offset(pd.Timestamp(reference), 20).date().isoformat()


def _last_completed_session(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise Phase2Error("seal time must be timezone-aware")
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(
        (now.date() - pd.Timedelta(days=10)).isoformat(), now.date().isoformat(),
    )
    completed = [session for session in sessions if calendar.session_close(session).to_pydatetime() < now]
    if not completed:
        raise Phase2Error("no completed XNYS session found for seal")
    return completed[-1].date().isoformat()


def build_seal_manifest(
    *, repo_root: str | Path, dev_path: str | Path, prosp_path: str | Path,
    outcomes_path: str | Path, model_path: str | Path, predictions_path: str | Path,
    price_manifest: Mapping[str, Any], split_manifest: Mapping[str, Any],
    outcome_summary: Mapping[str, Any], prediction_summary: Mapping[str, Any],
    output_path: str | Path, now: datetime | None = None,
) -> dict[str, Any]:
    seal_time = now or datetime.now(timezone.utc)
    prosp = _read_csv(prosp_path)
    window_hashes = [
        {
            "current_release_id": row["current_release_id"],
            "price_window_sha256": row.get("price_window_sha256") or None,
        }
        for row in sorted(prosp, key=lambda item: item["current_release_id"])
    ]
    included = [row for row in prosp if row.get("row_status") == "INCLUDED"]
    s_seal = _last_completed_session(seal_time)
    matured = sum(_d20(row["reference_session_date"]) <= s_seal for row in included)
    repo = Path(repo_root)
    manifest = {
        "schema_name": "trinity.h2-v2-1-seal-manifest",
        "schema_version": "1",
        "generated_at_utc": seal_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "branch": "research/h2-v2-1-build-inputs",
        "fallback": {
            "decision": "D-001",
            "registry_repository": "giopastorin-commits/trinity-online",
            "registry_commit": "40ae6cb",
            "provenance_report_sha256": PROVENANCE_REPORT_SHA256,
            "dev_input_sha256": sha256_file(dev_path),
        },
        "pre_event_covariates": {
            "commit_sha": PRE_EVENT_COMMIT,
            "module_sha256": sha256_file(repo / "trinity" / "pre_event_covariates.py"),
            "tests_sha256": sha256_file(repo / "tests" / "trinity" / "test_pre_event_covariates.py"),
        },
        "dev": {
            "outcomes_sha256": sha256_file(outcomes_path),
            "model_sha256": sha256_file(model_path),
            "outcome_counts": dict(outcome_summary),
        },
        "prosp_batch1": {
            "input_sha256": sha256_file(prosp_path),
            "predictions_sha256": sha256_file(predictions_path),
            "price_window_hashes": window_hashes,
            "price_window_hashes_ensemble_sha256": _canonical_hash(window_hashes),
            "included_prediction_count": len(included),
            "prediction_counts": dict(prediction_summary),
            "s_seal": s_seal,
            "d20_on_or_before_s_seal_count": matured,
            "s_seal_membership_rule": "D20 > S_seal",
        },
        "price_sources": {
            "unadjusted_ensemble_sha256": price_manifest.get("ensemble_sha256"),
            "unadjusted_manifest_sha256": sha256_file(Path(price_manifest["_manifest_path"])),
            "split_adjusted_ensemble_sha256": split_manifest.get("ensemble_sha256"),
            "split_adjusted_manifest_sha256": sha256_file(Path(split_manifest["_manifest_path"])),
        },
    }
    Path(output_path).write_bytes(canonical_json_bytes(manifest, newline=True))
    return manifest


def _cluster_cr1(
    x: np.ndarray, y: np.ndarray, beta: np.ndarray, clusters: Sequence[str],
) -> tuple[np.ndarray, np.ndarray, int]:
    n, k = x.shape
    labels = sorted(set(clusters))
    g = len(labels)
    if n <= k or g < 2:
        raise Phase2Error("CR1 requires N > K and at least two ticker clusters")
    codes = {label: index for index, label in enumerate(labels)}
    residuals = y - x @ beta
    bread = np.linalg.inv(x.T @ x)
    meat = np.zeros((k, k), dtype=np.float64)
    for label in labels:
        mask = np.asarray([item == label for item in clusters], dtype=bool)
        score = x[mask].T @ residuals[mask]
        meat += np.outer(score, score)
    correction = (g / (g - 1)) * ((n - 1) / (n - k))
    covariance = correction * bread @ meat @ bread
    diagonal = np.diag(covariance)
    if np.any(~np.isfinite(diagonal)) or np.any(diagonal <= 0):
        raise Phase2Error("invalid ticker-clustered CR1 covariance")
    return np.sqrt(diagonal), residuals, g


def _wild_cluster_p_value(
    x: np.ndarray, y: np.ndarray, beta: np.ndarray, clusters: Sequence[str],
    novelty_index: int, *, reps: int = PART_A_BOOTSTRAP_REPS,
    seed: int = PART_A_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    observed_se, _residuals, g = _cluster_cr1(x, y, beta, clusters)
    observed_t = float(beta[novelty_index] / observed_se[novelty_index])
    restricted_x = np.delete(x, novelty_index, axis=1)
    restricted_beta, _r, rank, _s = np.linalg.lstsq(restricted_x, y, rcond=None)
    if int(rank) != restricted_x.shape[1]:
        raise Phase2Error("restricted-null Part A model is singular")
    fitted_null = restricted_x @ restricted_beta
    restricted_residuals = y - fitted_null
    labels = sorted(set(clusters))
    codes = np.asarray([labels.index(label) for label in clusters], dtype=np.int64)
    rng = np.random.Generator(np.random.PCG64(seed))
    exceedances = 0
    valid = 0
    for _ in range(reps):
        weights = 2 * rng.integers(0, 2, size=g, dtype=np.int64) - 1
        y_star = fitted_null + restricted_residuals * weights[codes]
        beta_star, _resid, rank_star, _singular = np.linalg.lstsq(x, y_star, rcond=None)
        if int(rank_star) != x.shape[1]:
            continue
        try:
            se_star, _u, _g = _cluster_cr1(x, y_star, beta_star, clusters)
        except (Phase2Error, np.linalg.LinAlgError):
            continue
        t_star = float(beta_star[novelty_index] / se_star[novelty_index])
        if not math.isfinite(t_star):
            continue
        valid += 1
        exceedances += int(t_star >= observed_t)
    minimum = math.ceil(0.95 * reps)
    return {
        "coefficient": float(beta[novelty_index]),
        "cr1_standard_error": float(observed_se[novelty_index]),
        "observed_t": observed_t,
        "one_sided_p_value": (1 + exceedances) / (1 + valid) if valid >= minimum else None,
        "bootstrap_reps": reps,
        "bootstrap_seed": seed,
        "valid_reps": valid,
        "invalid_reps": reps - valid,
        "ticker_clusters": g,
        "estimable": valid >= minimum,
    }


def _part_a_data(
    dev_path: str | Path, outcomes_path: str | Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    inputs = {row["current_release_id"]: row for row in _read_csv(dev_path)}
    outcomes = {
        row["current_release_id"]: row
        for row in _read_csv(outcomes_path)
        if row.get("phase2_row_status") == "INCLUDED"
    }
    missing = sorted(set(outcomes) - set(inputs))
    if missing:
        raise Phase2Error(f"Part A outcomes absent from DEV input: {missing[:5]}")
    return inputs, outcomes


def _matrix_from_frozen_model(
    name: str, model: Mapping[str, Any], inputs: Mapping[str, Mapping[str, str]],
    outcomes: Mapping[str, Mapping[str, str]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str], list[str]]:
    accessions = model.get("estimation_accessions")
    columns = model.get("columns")
    coefficients = model.get("coefficients")
    if not isinstance(accessions, list) or not isinstance(columns, list) \
            or not isinstance(coefficients, dict):
        raise Phase2Error(f"invalid frozen {name} model")
    records = []
    clusters = []
    for accession in accessions:
        source = inputs[str(accession)]
        outcome = outcomes[str(accession)]
        features = _features(source)
        response = (
            math.log1p(abs(_number(outcome, "excess_return_pct_20")))
            if name.endswith("e") else math.log1p(_number(outcome, "absolute_return_pct_20"))
        )
        records.append(([features[column] for column in columns], response))
        clusters.append(str(source["ticker"]))
    x = np.asarray([item[0] for item in records], dtype=np.float64)
    y = np.asarray([item[1] for item in records], dtype=np.float64)
    beta = np.asarray([float(coefficients[column]) for column in columns], dtype=np.float64)
    return x, y, beta, clusters, [str(item) for item in accessions]


def _r_squared(y: np.ndarray, fitted: np.ndarray) -> float:
    denominator = float(np.sum((y - np.mean(y)) ** 2))
    if denominator <= 0:
        raise Phase2Error("R-squared is undefined for a constant response")
    return 1.0 - float(np.sum((y - fitted) ** 2)) / denominator


def _quarter_sensitivity(
    inputs: Mapping[str, Mapping[str, str]], outcomes: Mapping[str, Mapping[str, str]],
    accessions: Sequence[str], clusters: Sequence[str],
) -> dict[str, Any]:
    base_rows = []
    quarters = []
    y = []
    for accession in accessions:
        source = inputs[accession]
        reference = date.fromisoformat(source["reference_session_date"])
        quarters.append(f"{reference.year}-Q{((reference.month - 1) // 3) + 1}")
        features = _features(source)
        base_rows.append([1.0, features["ln_sigma_pre_60"], features["h2_novelty"]])
        y.append(math.log1p(_number(outcomes[accession], "absolute_return_pct_20")))
    levels = sorted(set(quarters))
    x = np.asarray([
        row + [1.0 if quarter == level else 0.0 for level in levels[1:]]
        for row, quarter in zip(base_rows, quarters, strict=True)
    ], dtype=np.float64)
    response = np.asarray(y, dtype=np.float64)
    beta, _r, rank, _s = np.linalg.lstsq(x, response, rcond=None)
    if int(rank) != x.shape[1]:
        raise Phase2Error("quarter fixed-effect sensitivity is singular")
    se, _u, g = _cluster_cr1(x, response, beta, clusters)
    return {
        "coefficient_h2_novelty": float(beta[2]),
        "cr1_standard_error": float(se[2]),
        "calendar_quarter_levels": levels,
        "N": len(response),
        "ticker_clusters": g,
    }


def _year_subperiods(
    inputs: Mapping[str, Mapping[str, str]], outcomes: Mapping[str, Mapping[str, str]],
    accessions: Sequence[str],
) -> list[dict[str, Any]]:
    by_year: dict[int, list[str]] = {}
    for accession in accessions:
        year = int(str(inputs[accession]["release_at"])[:4])
        by_year.setdefault(year, []).append(accession)
    result = []
    for year, year_ids in sorted(by_year.items()):
        x = np.asarray([
            [1.0, _features(inputs[item])["ln_sigma_pre_60"], _features(inputs[item])["h2_novelty"]]
            for item in year_ids
        ], dtype=np.float64)
        y = np.asarray([
            math.log1p(_number(outcomes[item], "absolute_return_pct_20")) for item in year_ids
        ], dtype=np.float64)
        rank = int(np.linalg.matrix_rank(x))
        coefficient = None
        if len(year_ids) > x.shape[1] and rank == x.shape[1]:
            beta, _r, _rank, _s = np.linalg.lstsq(x, y, rcond=None)
            coefficient = float(beta[2])
        result.append({"year": year, "N": len(year_ids), "coefficient_h2_novelty": coefficient})
    return result


def run_part_a(
    *, dev_path: str | Path, outcomes_path: str | Path,
    model_path: str | Path, output_path: str | Path,
    reps: int = PART_A_BOOTSTRAP_REPS, seed: int = PART_A_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    try:
        artifact = json.loads(Path(model_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase2Error("invalid frozen model for Part A") from exc
    inputs, outcomes = _part_a_data(dev_path, outcomes_path)
    results: dict[str, Any] = {}
    matrices: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, list[str], list[str]]] = {}
    for name in ("M1", "M1x"):
        model = artifact["models"][name]
        matrix = _matrix_from_frozen_model(name, model, inputs, outcomes)
        matrices[name] = matrix
        x, y, beta, clusters, _ids = matrix
        results[name] = _wild_cluster_p_value(
            x, y, beta, clusters, list(model["columns"]).index("h2_novelty"),
            reps=reps, seed=seed,
        )
        results[name]["N"] = len(y)
    x1, y1, beta1, clusters1, ids1 = matrices["M1"]
    x0, y0, beta0, _clusters0, _ids0 = _matrix_from_frozen_model(
        "M0", artifact["models"]["M0"], inputs, outcomes,
    )
    if not np.array_equal(y0, y1):
        raise Phase2Error("M0/M1 estimation samples differ")
    pred0 = np.expm1(x0 @ beta0)
    pred1 = np.expm1(x1 @ beta1)
    actual = np.expm1(y1)
    descriptive = {
        "r_squared_M0": _r_squared(y0, x0 @ beta0),
        "r_squared_M1": _r_squared(y1, x1 @ beta1),
        "delta_r_squared_M1_minus_M0": _r_squared(y1, x1 @ beta1) - _r_squared(y0, x0 @ beta0),
        "mae_M0_percentage_points": float(np.mean(np.abs(actual - pred0))),
        "mae_M1_percentage_points": float(np.mean(np.abs(actual - pred1))),
        "delta_mae_M1_minus_M0_percentage_points": float(
            np.mean(np.abs(actual - pred1)) - np.mean(np.abs(actual - pred0))
        ),
    }
    results["quarter_fixed_effect_sensitivity"] = _quarter_sensitivity(
        inputs, outcomes, ids1, clusters1,
    )
    results["release_year_subperiods"] = _year_subperiods(inputs, outcomes, ids1)
    results["in_sample_descriptive"] = descriptive
    results["bootstrap"] = {
        "weights": "Rademacher", "null_imposed": True, "reps": reps,
        "seed": seed, "minimum_valid_reps": math.ceil(0.95 * reps),
    }
    lines = [
        "# Analisi di robustezza, non confermativa (N piccolo)", "",
        "Solo S-DEV. Nessun outcome PROSP è stato caricato o calcolato.", "",
        "## Inferenza CR1 e wild cluster bootstrap-t", "",
        "| Modello | N | Cluster ticker | coefficiente H2 | SE CR1 | t | p unilaterale | repliche valide |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("M1", "M1x"):
        item = results[name]
        lines.append(
            f"| {name} | {item['N']} | {item['ticker_clusters']} | "
            f"{item['coefficient']:.17g} | {item['cr1_standard_error']:.17g} | "
            f"{item['observed_t']:.17g} | "
            f"{item['one_sided_p_value'] if item['one_sided_p_value'] is not None else 'NOT ESTIMABLE'} | "
            f"{item['valid_reps']} / {item['bootstrap_reps']} |"
        )
    quarter = results["quarter_fixed_effect_sensitivity"]
    lines.extend([
        "", "## Sensibilità con effetti fissi di trimestre", "",
        f"N={quarter['N']}; coefficiente H2={quarter['coefficient_h2_novelty']:.17g}; "
        f"SE CR1={quarter['cr1_standard_error']:.17g}; livelli="
        + ", ".join(quarter["calendar_quarter_levels"]) + ".",
        "", "## Sottoperiodi per anno di release_at", "",
        "| Anno | N | coefficiente H2 |", "| --- | ---: | ---: |",
    ])
    for item in results["release_year_subperiods"]:
        coefficient = item["coefficient_h2_novelty"]
        lines.append(
            f"| {item['year']} | {item['N']} | "
            f"{coefficient if coefficient is not None else 'NOT ESTIMABLE'} |"
        )
    d = descriptive
    lines.extend([
        "", "## Miglioramento descrittivo in campione", "",
        "R² è calcolato sulla risposta trasformata; MAE è in punti percentuali "
        "sulla scala originale di absolute_return_pct_20.", "",
        "| Metrica | M0 | M1 | Δ M1−M0 |", "| --- | ---: | ---: | ---: |",
        f"| R² | {d['r_squared_M0']:.17g} | {d['r_squared_M1']:.17g} | {d['delta_r_squared_M1_minus_M0']:.17g} |",
        f"| MAE | {d['mae_M0_percentage_points']:.17g} | {d['mae_M1_percentage_points']:.17g} | {d['delta_mae_M1_minus_M0_percentage_points']:.17g} |",
        "", f"Bootstrap: Rademacher, ipotesi nulla imposta, {reps} repliche, seed {seed}.", "",
    ])
    Path(output_path).write_text("\n".join(lines), encoding="utf-8", newline="\n")
    results["report_sha256"] = sha256_file(output_path)
    return results
