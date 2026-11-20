"""Resumable split-adjusted snapshots and the H2 Phase 1.1-B factor screen."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import stat
from typing import Any, Callable, Mapping, Sequence

from trinity.h2_v2_1_inputs import (
    EXCLUSION_COLUMNS,
    INPUT_COLUMNS,
    PriceCoverageError,
    canonical_json_bytes,
    exclusion_sort_key,
    file_inventory,
    input_sort_key,
    normalized_json_to_jsonl,
    read_bars_through_reference,
    sha256_file,
    verify_price_ensemble,
    write_deterministic_csv,
)
from trinity.twelvedata_prices import (
    ADJUSTMENT,
    DAILY_CREDIT_RESERVE,
    INTERVAL,
    PRICE_PROVIDER,
    CreditGuardError,
    DailyQuotaExhausted,
    PriceSnapshotError,
    ProviderRequestError,
    TwelveDataClient,
    canonical_bytes,
    normalize_response,
    utc_timestamp,
)


ADJUSTED_MODE = "splits"
HISTORY_START = date(2025, 8, 24)
TARGET_SESSION = date(2026, 11, 17)
REQUEST_END_DATE = date(2026, 11, 18)
OUTPUTSIZE = 5000
CANONICAL_TICKER_COUNT = 458
PROVIDER_OVERRIDES = {"SKYD": "PSKY"}
PARENT_SNAPSHOT_COMMIT = "546ea38f7376adf9480a0da07048ed8ecc561ea3"
NONE_ENSEMBLE_SHA256 = "8dd59aae14c92f9d8dd9a6c055c166157e8b49b0d6ce0b48591080277ec7a54f"
CHECKPOINT_SCHEMA = "trinity.h2-v2-1-split-factor-checkpoint"
SNAPSHOT_SCHEMA = "trinity.h2-v2-1-split-adjusted-snapshot"
SCREEN_SCHEMA = "trinity.h2-v2-1-phase1-1-b-split-factor-screen"
SCHEMA_VERSION = "1"
SPLIT_REASON = "CORPORATE_ACTION_SPLIT"
FACTOR_COLUMNS = (
    "ticker", "provider_symbol", "date", "previous_common_date",
    "factor_change_ratio",
)


class SplitFactorError(PriceSnapshotError):
    """The split-factor source or screen violates the frozen contract."""


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SplitFactorError(f"invalid Phase 1 CSV: {path}") from exc


def split_factor_universe(parent_root: str | Path) -> list[str]:
    base = Path(parent_root)
    rows = _read_csv(base / "h2_v2_1_inputs_dev.csv")
    rows.extend(_read_csv(base / "h2_v2_1_inputs_prosp_batch1.csv"))
    exclusions = _read_csv(base / "h2_v2_1_exclusions.csv")
    tickers = {
        str(row.get("ticker") or "").strip().upper()
        for row in rows
    }
    tickers.update(
        str(row.get("ticker") or "").strip().upper()
        for row in exclusions
        if row.get("sample") in {"DEV", "PROSP_BATCH1"}
        and row.get("reason") != "DUPLICATE_SHARE_CLASS"
    )
    if "" in tickers:
        raise SplitFactorError("split-factor universe contains an empty ticker")
    result = sorted(tickers)
    if len(result) != CANONICAL_TICKER_COUNT:
        raise SplitFactorError(
            f"split-factor universe mismatch: expected={CANONICAL_TICKER_COUNT} "
            f"actual={len(result)}"
        )
    return result


def _configuration(tickers: Sequence[str]) -> dict[str, Any]:
    return {
        "endpoint": "https://api.twelvedata.com/time_series",
        "parameters": {
            "interval": INTERVAL,
            "start_date": HISTORY_START.isoformat(),
            "end_date": REQUEST_END_DATE.isoformat(),
            "outputsize": OUTPUTSIZE,
            "adjust": ADJUSTED_MODE,
            "symbol": "{provider_symbol}",
        },
        "parent_snapshot_commit": PARENT_SNAPSHOT_COMMIT,
        "canonical_tickers": list(tickers),
        "canonical_tickers_sha256": hashlib.sha256(canonical_bytes(list(tickers))).hexdigest(),
        "provider_symbol_overrides": dict(PROVIDER_OVERRIDES),
    }


def _checkpoint_path(root: Path) -> Path:
    return root / "split_factor_checkpoint.json"


def _write_checkpoint(root: Path, checkpoint: Mapping[str, Any]) -> None:
    path = _checkpoint_path(root)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_json_bytes(dict(checkpoint), newline=True))
    temporary.replace(path)


def _load_checkpoint(root: Path, configuration: Mapping[str, Any]) -> dict[str, Any]:
    path = _checkpoint_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SplitFactorError("invalid split-factor checkpoint") from exc
    if not isinstance(value, dict) or value.get("schema_name") != CHECKPOINT_SCHEMA \
            or value.get("schema_version") != SCHEMA_VERSION:
        raise SplitFactorError("unsupported split-factor checkpoint")
    if value.get("configuration") != configuration:
        raise SplitFactorError("split-factor checkpoint configuration mismatch")
    entries = value.get("entries")
    batches = value.get("batches")
    if not isinstance(entries, list) or not isinstance(batches, list):
        raise SplitFactorError("split-factor checkpoint is malformed")
    completed: set[str] = set()
    expected_files = {"split_factor_checkpoint.json"}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("canonical_ticker"), str):
            raise SplitFactorError("split-factor checkpoint entry is malformed")
        ticker = entry["canonical_ticker"]
        if ticker in completed:
            raise SplitFactorError(f"duplicate completed split-factor ticker: {ticker}")
        completed.add(ticker)
        normalized = root / "normalized" / f"{ticker}.json"
        if not normalized.is_file() or sha256_file(normalized) != entry.get("content_sha256"):
            raise SplitFactorError(f"changed completed normalized file: {ticker}")
        expected_files.add(normalized.relative_to(root).as_posix())
        raw_sha = entry.get("raw_sha256")
        if raw_sha:
            raw = root / "raw" / f"{ticker}.json"
            if not raw.is_file() or sha256_file(raw) != raw_sha:
                raise SplitFactorError(f"changed completed raw file: {ticker}")
            expected_files.add(raw.relative_to(root).as_posix())
    for batch in batches:
        if not isinstance(batch, dict):
            raise SplitFactorError("split-factor batch metadata is malformed")
        telemetry = batch.get("telemetry_file")
        if telemetry:
            telemetry_path = root / str(telemetry)
            if not telemetry_path.is_file() \
                    or sha256_file(telemetry_path) != batch.get("telemetry_sha256"):
                raise SplitFactorError("split-factor batch telemetry hash mismatch")
            expected_files.add(telemetry_path.relative_to(root).as_posix())
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*") if path.is_file()
        and path.name != "split_adjusted_snapshot_manifest.json"
    }
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        raise SplitFactorError(
            f"split-factor checkpoint file set mismatch: "
            f"unexpected={unexpected[:5]} missing={missing[:5]}"
        )
    return value


def _new_checkpoint(configuration: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "schema_name": CHECKPOINT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": utc_timestamp(now),
        "configuration": dict(configuration),
        "entries": [],
        "batches": [],
    }


def _telemetry(
    usage: object, client: TwelveDataClient, *, batch_number: int,
) -> dict[str, Any]:
    return {
        "schema_name": "trinity.h2-v2-1-split-factor-credit-telemetry",
        "schema_version": "1",
        "batch_number": batch_number,
        "provider_response": usage.usage,
        "response_headers": usage.response_headers,
        "retrieved_at_utc": usage.retrieved_at_utc,
        "events": list(client.credit_events),
        "safety_reserve": DAILY_CREDIT_RESERVE,
    }


def _write_telemetry(
    root: Path, usage: object, client: TwelveDataClient, *, batch_number: int,
) -> tuple[str, str]:
    relative = f"telemetry/provider_credit_batch_{batch_number:03d}.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(
        _telemetry(usage, client, batch_number=batch_number), newline=True,
    ))
    return relative, sha256_file(path)


def _progress(checkpoint: Mapping[str, Any], total: int) -> dict[str, Any]:
    entries = checkpoint.get("entries")
    completed = len(entries) if isinstance(entries, list) else 0
    return {
        "complete": False,
        "completed_ticker_count": completed,
        "remaining_ticker_count": total - completed,
        "checkpoint_sha256": sha256_file_value(checkpoint),
    }


def sha256_file_value(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value, newline=True)).hexdigest()


def acquire_split_adjusted_snapshot(
    *, parent_root: str | Path, output_root: str | Path,
    client: TwelveDataClient | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    seal_read_only: bool = True,
) -> dict[str, Any]:
    root = Path(output_root)
    final_path = root / "split_adjusted_snapshot_manifest.json"
    if final_path.is_file():
        manifest = verify_split_adjusted_snapshot(root)
        return {**manifest, "manifest_file_sha256": sha256_file(final_path)}
    tickers = split_factor_universe(parent_root)
    configuration = _configuration(tickers)
    if _checkpoint_path(root).is_file():
        checkpoint = _load_checkpoint(root, configuration)
        for batch in checkpoint["batches"]:
            if batch.get("status") == "RUNNING":
                batch["status"] = "INTERRUPTED_BEFORE_TELEMETRY"
    else:
        if root.exists() and any(root.iterdir()):
            raise SplitFactorError(f"split-factor root is not a valid new/resume root: {root}")
        (root / "raw").mkdir(parents=True, exist_ok=True)
        (root / "normalized").mkdir(parents=True, exist_ok=True)
        checkpoint = _new_checkpoint(configuration, now())
        _write_checkpoint(root, checkpoint)
    completed = {entry["canonical_ticker"] for entry in checkpoint["entries"]}
    remaining = [ticker for ticker in tickers if ticker not in completed]
    if not remaining:
        return _seal_snapshot(root, checkpoint, now=now, seal_read_only=seal_read_only)

    provider = client or TwelveDataClient()
    usage = provider.fetch_api_usage()
    available = int(usage.daily_remaining)
    capacity = max(0, min(len(remaining), available - DAILY_CREDIT_RESERVE))
    batch_number = len(checkpoint["batches"]) + 1
    batch: dict[str, Any] = {
        "batch_number": batch_number,
        "started_at_utc": utc_timestamp(now()),
        "available_daily_credits": available,
        "planned_ticker_count": capacity,
        "status": "RUNNING",
    }
    checkpoint["batches"].append(batch)
    _write_checkpoint(root, checkpoint)
    stop_classification: str | None = None

    for index, ticker in enumerate(remaining[:capacity]):
        provider_symbol = PROVIDER_OVERRIDES.get(ticker, ticker)
        raw: bytes | None = None
        attempts: Sequence[Mapping[str, Any]] = ()
        error: str | None = None
        classification = "PROVIDER_FAILURE"
        returned_provider_symbol = provider_symbol
        bars: list[dict[str, Any]] = []
        try:
            capture = provider.fetch(
                provider_symbol, start_date=HISTORY_START,
                target_session=TARGET_SESSION,
                remaining_tickers=capacity - index, adjust=ADJUSTED_MODE,
            )
            raw, attempts = capture.raw, capture.attempts
            normalized = normalize_response(
                ticker, raw, target_session=TARGET_SESSION,
                expected_provider_symbol=provider_symbol,
            )
            returned_provider_symbol = normalized.provider_symbol
            classification = normalized.classification
            bars = list(normalized.bars)
            error = normalized.error
        except (CreditGuardError, DailyQuotaExhausted) as exc:
            stop_classification = getattr(exc, "classification", type(exc).__name__)
            break
        except ProviderRequestError as exc:
            raw, attempts, error = exc.raw, exc.attempts, str(exc)
        except (PriceSnapshotError, TypeError, ValueError) as exc:
            classification, error = "INVALID_DATA", str(exc)
        configured_secret = getattr(provider, "api_key", None)
        if raw is not None and isinstance(configured_secret, str) \
                and configured_secret.encode("utf-8") in raw:
            raw = None
            classification = "INVALID_DATA"
            error = f"{ticker}: adjusted response contained configured secret"
            bars = []
        raw_path = root / "raw" / f"{ticker}.json"
        if raw is not None:
            raw_path.write_bytes(raw)
        normalized_path = root / "normalized" / f"{ticker}.json"
        normalized_path.write_bytes(canonical_bytes(bars))
        checkpoint["entries"].append({
            "canonical_ticker": ticker,
            "requested_provider_symbol": provider_symbol,
            "provider_symbol": returned_provider_symbol,
            "request_parameters": {
                "interval": INTERVAL,
                "start_date": HISTORY_START.isoformat(),
                "end_date": REQUEST_END_DATE.isoformat(),
                "outputsize": OUTPUTSIZE,
                "adjust": ADJUSTED_MODE,
            },
            "acquired_at_utc": (
                attempts[0].get("requested_at_utc") if attempts else utc_timestamp(now())
            ),
            "classification": classification,
            "bar_count": len(bars),
            "content_sha256": sha256_file(normalized_path),
            "raw_sha256": sha256_file(raw_path) if raw is not None else None,
            "attempts": [dict(item) for item in attempts],
            "retry_count": max(0, len(attempts) - 1),
            "error": error,
        })
        _write_checkpoint(root, checkpoint)

    telemetry_file, telemetry_sha = _write_telemetry(
        root, usage, provider, batch_number=batch_number,
    )
    completed_after = len(checkpoint["entries"])
    batch.update({
        "completed_at_utc": utc_timestamp(now()),
        "completed_ticker_count": completed_after - len(completed),
        "telemetry_file": telemetry_file,
        "telemetry_sha256": telemetry_sha,
        "status": stop_classification or (
            "COMPLETE" if completed_after == len(tickers) else "DAILY_BATCH_COMPLETE"
        ),
    })
    _write_checkpoint(root, checkpoint)
    if completed_after < len(tickers):
        return _progress(checkpoint, len(tickers))
    return _seal_snapshot(root, checkpoint, now=now, seal_read_only=seal_read_only)


def _seal_snapshot(
    root: Path, checkpoint: Mapping[str, Any], *,
    now: Callable[[], datetime], seal_read_only: bool,
) -> dict[str, Any]:
    configuration = checkpoint["configuration"]
    tickers = configuration["canonical_tickers"]
    jsonl_root = root / "bounded_jsonl"
    for ticker in tickers:
        normalized_json_to_jsonl(
            root / "normalized" / f"{ticker}.json",
            jsonl_root / f"{ticker}.jsonl",
        )
    inventory = file_inventory(
        root, exclude_names=frozenset({"split_adjusted_snapshot_manifest.json"}),
    )
    entries = sorted(checkpoint["entries"], key=lambda item: item["canonical_ticker"])
    manifest: dict[str, Any] = {
        "schema_name": SNAPSHOT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "provider": PRICE_PROVIDER,
        "configuration": configuration,
        "acquisition_started_at_utc": checkpoint["created_at_utc"],
        "acquisition_completed_at_utc": utc_timestamp(now()),
        "canonical_ticker_count": len(tickers),
        "terminal_response_count": len(entries),
        "entries": entries,
        "batches": checkpoint["batches"],
        "checkpoint_sha256": sha256_file(_checkpoint_path(root)),
        "files": inventory,
        "ensemble_sha256": hashlib.sha256(canonical_bytes(inventory)).hexdigest(),
    }
    manifest_path = root / "split_adjusted_snapshot_manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest, newline=True))
    if seal_read_only:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            path.chmod(stat.S_IREAD)
    manifest["manifest_file_sha256"] = sha256_file(manifest_path)
    return manifest


def verify_split_adjusted_snapshot(root: str | Path) -> dict[str, Any]:
    base = Path(root)
    manifest_path = base / "split_adjusted_snapshot_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SplitFactorError("invalid split-adjusted snapshot manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_name") != SNAPSHOT_SCHEMA \
            or manifest.get("schema_version") != SCHEMA_VERSION:
        raise SplitFactorError("unsupported split-adjusted snapshot schema")
    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise SplitFactorError("split-adjusted snapshot lacks configuration")
    tickers = configuration.get("canonical_tickers")
    if not isinstance(tickers, list) or len(tickers) != CANONICAL_TICKER_COUNT \
            or tickers != sorted(tickers) or len(set(tickers)) != len(tickers):
        raise SplitFactorError("split-adjusted ticker universe is invalid")
    expected_configuration = _configuration(tickers)
    if configuration != expected_configuration:
        raise SplitFactorError("split-adjusted request configuration mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != len(tickers):
        raise SplitFactorError("split-adjusted terminal response count mismatch")
    if [entry.get("canonical_ticker") for entry in entries] != tickers:
        raise SplitFactorError("split-adjusted entries are not canonical")
    for entry in entries:
        ticker = entry["canonical_ticker"]
        if entry.get("request_parameters") != {
            "interval": INTERVAL, "start_date": HISTORY_START.isoformat(),
            "end_date": REQUEST_END_DATE.isoformat(), "outputsize": OUTPUTSIZE,
            "adjust": ADJUSTED_MODE,
        }:
            raise SplitFactorError(f"split-adjusted parameters changed: {ticker}")
        normalized = base / "normalized" / f"{ticker}.json"
        if not normalized.is_file() or sha256_file(normalized) != entry.get("content_sha256"):
            raise SplitFactorError(f"split-adjusted normalized hash mismatch: {ticker}")
        raw_sha = entry.get("raw_sha256")
        if raw_sha:
            raw = base / "raw" / f"{ticker}.json"
            if not raw.is_file() or sha256_file(raw) != raw_sha:
                raise SplitFactorError(f"split-adjusted raw hash mismatch: {ticker}")
        if not (base / "bounded_jsonl" / f"{ticker}.jsonl").is_file():
            raise SplitFactorError(f"split-adjusted bounded JSONL is missing: {ticker}")
    actual = file_inventory(
        base, exclude_names=frozenset({"split_adjusted_snapshot_manifest.json"}),
    )
    if actual != manifest.get("files"):
        raise SplitFactorError("split-adjusted inventory mismatch")
    if hashlib.sha256(canonical_bytes(actual)).hexdigest() != manifest.get("ensemble_sha256"):
        raise SplitFactorError("split-adjusted ensemble hash mismatch")
    if sha256_file(_checkpoint_path(base)) != manifest.get("checkpoint_sha256"):
        raise SplitFactorError("split-adjusted checkpoint hash mismatch")
    return manifest


def _valid_cutoffs(parent: Path) -> dict[str, date]:
    rows = _read_csv(parent / "h2_v2_1_inputs_dev.csv")
    rows.extend(_read_csv(parent / "h2_v2_1_inputs_prosp_batch1.csv"))
    result: dict[str, date] = {}
    for row in rows:
        lower = str(row.get("price_window_start") or "").strip()
        upper = str(row.get("reference_session_date") or "").strip()
        if not lower or not upper:
            continue
        try:
            date.fromisoformat(lower)
            cutoff = date.fromisoformat(upper)
        except ValueError as exc:
            raise SplitFactorError("invalid Phase 1 interval date") from exc
        ticker = str(row.get("ticker") or "")
        if ticker not in result or cutoff > result[ticker]:
            result[ticker] = cutoff
    return result


def _decimal_close(row: Mapping[str, object], ticker: str, day: date) -> Decimal:
    try:
        value = Decimal(str(row["close"]))
    except (KeyError, InvalidOperation) as exc:
        raise SplitFactorError(f"invalid close for {ticker} on {day.isoformat()}") from exc
    if not value.is_finite() or value <= 0:
        raise SplitFactorError(f"non-positive/non-finite close for {ticker} on {day.isoformat()}")
    return value


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def detect_split_factors(
    *, parent_root: str | Path, none_price_root: str | Path,
    adjusted_root: str | Path,
) -> list[dict[str, str]]:
    parent = Path(parent_root)
    none_root = Path(none_price_root)
    adjusted = Path(adjusted_root)
    none_manifest = verify_price_ensemble(none_root)
    if none_manifest.get("ensemble_sha256") != NONE_ENSEMBLE_SHA256 \
            or none_manifest.get("adjustment_policy", {}).get("provider_adjust") != ADJUSTMENT:
        raise SplitFactorError("unadjusted comparator is not the frozen A2 ensemble")
    adjusted_manifest = verify_split_adjusted_snapshot(adjusted)
    entries = {
        entry["canonical_ticker"]: entry
        for entry in adjusted_manifest["entries"]
    }
    detections: list[dict[str, str]] = []
    for ticker, cutoff in sorted(_valid_cutoffs(parent).items()):
        entry = entries.get(ticker)
        if entry is None:
            raise SplitFactorError(f"missing adjusted response metadata: {ticker}")
        try:
            none_bars = read_bars_through_reference(
                none_root / "bounded_jsonl" / f"{ticker}.jsonl", cutoff,
            )
            split_bars = read_bars_through_reference(
                adjusted / "bounded_jsonl" / f"{ticker}.jsonl", cutoff,
            )
        except (OSError, PriceCoverageError, ValueError) as exc:
            raise SplitFactorError(
                f"bounded split-factor coverage unavailable for {ticker}: {exc}"
            ) from exc
        none_by_date = {date.fromisoformat(str(row["date"])): row for row in none_bars}
        split_by_date = {date.fromisoformat(str(row["date"])): row for row in split_bars}
        common = sorted(set(none_by_date) & set(split_by_date))
        previous_day: date | None = None
        previous_ratio: Decimal | None = None
        with localcontext() as context:
            context.prec = 34
            for day in common:
                ratio = _decimal_close(split_by_date[day], ticker, day) / _decimal_close(
                    none_by_date[day], ticker, day,
                )
                if previous_ratio is not None and previous_day is not None:
                    factor_change = ratio / previous_ratio
                    if abs(factor_change - Decimal(1)) > Decimal("0.01"):
                        detections.append({
                            "ticker": ticker,
                            "provider_symbol": str(entry["requested_provider_symbol"]),
                            "date": day.isoformat(),
                            "previous_common_date": previous_day.isoformat(),
                            "factor_change_ratio": _decimal_text(factor_change),
                        })
                previous_day, previous_ratio = day, ratio
    return sorted(
        detections,
        key=lambda row: (
            row["ticker"], row["date"], row["previous_common_date"],
            row["factor_change_ratio"],
        ),
    )


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SplitFactorError(f"invalid input date: {text}") from exc


def apply_split_factor_screen(
    *, parent_root: str | Path, none_price_root: str | Path,
    adjusted_root: str | Path, output_root: str | Path,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    parent = Path(parent_root)
    output = Path(output_root)
    if output.exists() and any(output.iterdir()):
        raise SplitFactorError(f"Phase 1.1-B output must be new and empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    detections = detect_split_factors(
        parent_root=parent, none_price_root=none_price_root,
        adjusted_root=adjusted_root,
    )
    factor_path = output / "h2_v2_1_detected_split_factors.csv"
    write_deterministic_csv(factor_path, detections, FACTOR_COLUMNS)
    by_ticker: dict[str, list[dict[str, str]]] = {}
    for detection in detections:
        by_ticker.setdefault(detection["ticker"], []).append(detection)
    dev = _read_csv(parent / "h2_v2_1_inputs_dev.csv")
    prosp = _read_csv(parent / "h2_v2_1_inputs_prosp_batch1.csv")
    exclusions = _read_csv(parent / "h2_v2_1_exclusions.csv")
    added: list[dict[str, str]] = []

    def screen(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        screened: list[dict[str, str]] = []
        for source in rows:
            row = dict(source)
            lower = _parse_date(row.get("price_window_start"))
            upper = _parse_date(row.get("reference_session_date"))
            matching = []
            if lower is not None and upper is not None:
                matching = [
                    detection for detection in by_ticker.get(str(row.get("ticker") or ""), [])
                    if lower < date.fromisoformat(detection["date"]) <= upper
                ]
            if matching:
                reasons = [
                    reason for reason in str(row.get("exclusion_reasons") or "").split(";")
                    if reason
                ]
                if SPLIT_REASON not in reasons:
                    reasons.append(SPLIT_REASON)
                row["exclusion_reasons"] = ";".join(reasons)
                row["row_status"] = "EXCLUDED"
                details = "|".join(
                    f"{item['date']}:{item['factor_change_ratio']}" for item in matching
                )
                added.append({
                    "sample": str(row.get("sample") or ""),
                    "release_period": str(row.get("release_period") or ""),
                    "ticker": str(row.get("ticker") or ""),
                    "current_release_id": str(row.get("current_release_id") or ""),
                    "release_at": str(row.get("release_at") or ""),
                    "event_timing": str(row.get("event_timing") or ""),
                    "reference_session_date": str(row.get("reference_session_date") or ""),
                    "reason": SPLIT_REASON,
                    "detail": f"factor_changes={details}",
                })
            screened.append(row)
        return screened

    dev_screened = screen(dev)
    prosp_screened = screen(prosp)
    exclusions.extend(added)
    paths = {
        "dev": output / "h2_v2_1_inputs_dev.csv",
        "prosp": output / "h2_v2_1_inputs_prosp_batch1.csv",
        "exclusions": output / "h2_v2_1_exclusions.csv",
    }
    write_deterministic_csv(paths["dev"], sorted(dev_screened, key=input_sort_key), INPUT_COLUMNS)
    write_deterministic_csv(
        paths["prosp"], sorted(prosp_screened, key=input_sort_key), INPUT_COLUMNS,
    )
    write_deterministic_csv(
        paths["exclusions"], sorted(exclusions, key=exclusion_sort_key), EXCLUSION_COLUMNS,
    )
    adjusted_manifest = verify_split_adjusted_snapshot(adjusted_root)
    source_path = output / "h2_v2_1_split_adjusted_source_manifest.json"
    source_path.write_bytes(canonical_json_bytes(adjusted_manifest, newline=True))
    verify_price_ensemble(none_price_root)
    verify_split_adjusted_snapshot(adjusted_root)
    counts = {
        sample: len({row["current_release_id"] for row in added if row["sample"] == sample})
        for sample in ("DEV", "PROSP_BATCH1")
    }
    eligible = {
        "DEV": sum(row.get("row_status") == "INCLUDED" for row in dev_screened),
        "PROSP_BATCH1": sum(row.get("row_status") == "INCLUDED" for row in prosp_screened),
    }
    manifest = {
        "schema_name": SCREEN_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "built_at_utc": utc_timestamp(now()),
        "parent_snapshot_commit": PARENT_SNAPSHOT_COMMIT,
        "none_ensemble_sha256": NONE_ENSEMBLE_SHA256,
        "split_adjusted_ensemble_sha256": adjusted_manifest["ensemble_sha256"],
        "split_adjusted_manifest_sha256": sha256_file(
            Path(adjusted_root) / "split_adjusted_snapshot_manifest.json"
        ),
        "detected_factors_sha256": sha256_file(factor_path),
        "detected_factor_count": len(detections),
        "csv_sha256": {name: sha256_file(path) for name, path in paths.items()},
        "corporate_action_split_event_count": counts,
        "primary_eligible_count": eligible,
        "interval_rule": "price_window_start < detected_split_date <= reference_session_date",
    }
    manifest_path = output / "h2_v2_1_phase1_1_b_manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest, newline=True))
    manifest["manifest_file_sha256"] = sha256_file(manifest_path)
    return manifest
