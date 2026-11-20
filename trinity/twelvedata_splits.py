"""Immutable Twelve Data split snapshots and the H2 Phase 1.1 screen."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
from typing import Any, Callable, Mapping, Sequence

import requests

from trinity.h2_v2_1_inputs import (
    EXCLUSION_COLUMNS,
    INPUT_COLUMNS,
    canonical_json_bytes,
    exclusion_sort_key,
    file_inventory,
    input_sort_key,
    sha256_file,
    write_deterministic_csv,
)
from trinity.twelvedata_prices import (
    DAILY_CREDIT_RESERVE,
    PRICE_PROVIDER,
    RETRYABLE_HTTP_STATUSES,
    CreditGuardError,
    DailyQuotaExhausted,
    PriceSnapshotError,
    ProviderCapture,
    ProviderRequestError,
    RollingWindowThrottle,
    TwelveDataClient,
    _is_daily_quota_payload,
    canonical_bytes,
    utc_timestamp,
)


SPLITS_ENDPOINT = "https://api.twelvedata.com/splits"
SPLIT_CREDIT_COST = 20
SPLIT_START_DATE = date(2025, 8, 24)
SPLIT_END_DATE = date(2026, 11, 20)
SPLIT_CANONICAL_TICKER_COUNT = 458
SPLIT_PROVIDER_OVERRIDES = {"SKYD": "PSKY"}
SPLIT_SCHEMA = "trinity.h2-v2-1-split-snapshot"
SPLIT_SCHEMA_VERSION = "1"
PHASE1_1_SCHEMA = "trinity.h2-v2-1-phase1-1-split-screen"
PHASE1_1_SCHEMA_VERSION = "1"
SPLIT_REASON = "CORPORATE_ACTION_SPLIT"
SPLIT_PARENT_COMMIT = "546ea38f7376adf9480a0da07048ed8ecc561ea3"


class SplitSnapshotError(PriceSnapshotError):
    """A split source or screen violates the frozen Phase 1.1 contract."""


@dataclass(frozen=True)
class NormalizedSplits:
    provider_symbol: str
    classification: str
    actions: tuple[dict[str, Any], ...]
    provider_row_count: int
    error: str | None = None


class TwelveDataSplitClient(TwelveDataClient):
    """Twelve Data client for the weighted-credit ``/splits`` endpoint."""

    def __init__(self, *args: Any, split_throttle: object | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.split_throttle = split_throttle or RollingWindowThrottle(
            limit=1, window_seconds=61.0,
            monotonic=self.monotonic, sleep=self.sleep,
        )

    def configure_split_rate(self, plan_limit: int) -> int:
        request_limit = max(1, int(plan_limit) // SPLIT_CREDIT_COST)
        if isinstance(self.split_throttle, RollingWindowThrottle):
            self.split_throttle.limit = request_limit
        return request_limit

    def _consume_split_credit(self, remaining_symbols: int) -> None:
        available = self.daily_remaining
        required = remaining_symbols * SPLIT_CREDIT_COST + DAILY_CREDIT_RESERVE
        if available is not None and available < required:
            raise CreditGuardError(
                "INSUFFICIENT_DAILY_CREDITS",
                "insufficient Twelve Data daily credits to complete split acquisition",
                required=required, available=available, shortfall=required - available,
            )
        if available is not None:
            self._daily_remaining = available - SPLIT_CREDIT_COST

    def fetch_split(self, symbol: str, *, remaining_symbols: int) -> ProviderCapture:
        params = {"symbol": symbol, "apikey": self.api_key}
        attempts: list[dict[str, Any]] = []
        last_raw: bytes | None = None
        retryable_exceptions = (requests.Timeout, requests.ConnectionError)
        for ordinal in range(1, self.max_attempts + 1):
            self._consume_split_credit(remaining_symbols)
            self.split_throttle.acquire()
            requested_at = self.now()
            started = self.monotonic()
            status: int | None = None
            provider_code: int | str | None = None
            provider_message: str | None = None
            try:
                response = self.session.get(
                    SPLITS_ENDPOINT, params=params, timeout=self.timeout_seconds,
                )
                status = int(response.status_code)
                last_raw = bytes(response.content)
                credit_headers = self._record_credit_event(
                    endpoint="/splits", symbol=symbol, requested_at=requested_at,
                    status=status, headers=getattr(response, "headers", None),
                )
                try:
                    envelope = json.loads(last_raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    envelope = None
                if isinstance(envelope, dict) and envelope.get("status") == "error":
                    provider_code = envelope.get("code")
                    provider_message = str(
                        envelope.get("message") or "provider error"
                    ).replace(self.api_key, "[REDACTED]")[:500]
                attempts.append({
                    "attempt": ordinal,
                    "requested_at_utc": utc_timestamp(requested_at),
                    "http_status": status,
                    "elapsed_seconds": round(self.monotonic() - started, 6),
                    "provider_code": provider_code,
                    "provider_message": provider_message,
                    **credit_headers,
                })
                if _is_daily_quota_payload(provider_code, provider_message):
                    self._daily_remaining = 0
                    raise DailyQuotaExhausted(
                        f"DAILY_QUOTA_EXHAUSTED: {symbol}: Twelve Data daily quota exhausted",
                        attempts=attempts, raw=last_raw,
                    )
                retryable_provider = str(provider_code) in {
                    str(item) for item in RETRYABLE_HTTP_STATUSES
                }
                if status in RETRYABLE_HTTP_STATUSES or retryable_provider:
                    if ordinal < self.max_attempts:
                        self.sleep(float(ordinal))
                        continue
                    raise ProviderRequestError(
                        f"{symbol}: transient Twelve Data split failure exhausted retries",
                        attempts=attempts, raw=last_raw,
                    )
                if status < 200 or status >= 300:
                    raise ProviderRequestError(
                        f"{symbol}: Twelve Data split request failed HTTP {status}",
                        attempts=attempts, raw=last_raw,
                    )
                if provider_code is not None:
                    raise ProviderRequestError(
                        f"{symbol}: Twelve Data split error {provider_code}: {provider_message}",
                        attempts=attempts, raw=last_raw,
                    )
                return ProviderCapture(last_raw, tuple(attempts))
            except retryable_exceptions as exc:
                self._record_credit_event(
                    endpoint="/splits", symbol=symbol, requested_at=requested_at,
                    status=status, headers=None,
                )
                attempts.append({
                    "attempt": ordinal,
                    "requested_at_utc": utc_timestamp(requested_at),
                    "http_status": status,
                    "elapsed_seconds": round(self.monotonic() - started, 6),
                    "exception": type(exc).__name__,
                })
                if ordinal < self.max_attempts:
                    self.sleep(float(ordinal))
                    continue
                raise ProviderRequestError(
                    f"{symbol}: Twelve Data split request failed ({type(exc).__name__})",
                    attempts=attempts, raw=last_raw,
                ) from exc
        raise AssertionError("unreachable split retry state")


def required_split_credits(symbol_count: int) -> int:
    if symbol_count < 0:
        raise ValueError("symbol count must not be negative")
    return symbol_count * SPLIT_CREDIT_COST + DAILY_CREDIT_RESERVE


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SplitSnapshotError(f"invalid Phase 1 CSV: {path}") from exc


def split_universe(parent_root: str | Path) -> list[str]:
    base = Path(parent_root)
    dev = _read_csv(base / "h2_v2_1_inputs_dev.csv")
    prosp = _read_csv(base / "h2_v2_1_inputs_prosp_batch1.csv")
    exclusions = _read_csv(base / "h2_v2_1_exclusions.csv")
    tickers = {
        str(row.get("ticker") or "").strip().upper()
        for row in dev + prosp
    }
    tickers.update(
        str(row.get("ticker") or "").strip().upper()
        for row in exclusions
        if row.get("sample") in {"DEV", "PROSP_BATCH1"}
        and row.get("reason") != "DUPLICATE_SHARE_CLASS"
    )
    if "" in tickers:
        raise SplitSnapshotError("split universe contains an empty ticker")
    result = sorted(tickers)
    if len(result) != SPLIT_CANONICAL_TICKER_COUNT:
        raise SplitSnapshotError(
            f"split universe mismatch: expected={SPLIT_CANONICAL_TICKER_COUNT} "
            f"actual={len(result)}"
        )
    return result


def normalize_split_response(
    raw: bytes, *, expected_provider_symbol: str,
    start_date: date = SPLIT_START_DATE, end_date: date = SPLIT_END_DATE,
) -> NormalizedSplits:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SplitSnapshotError("Twelve Data split response is not JSON") from exc
    if not isinstance(payload, dict):
        raise SplitSnapshotError("Twelve Data split response is not an object")
    meta = payload.get("meta")
    returned_symbol = str(
        meta.get("symbol") if isinstance(meta, dict) and meta.get("symbol")
        else expected_provider_symbol
    ).strip().upper()
    expected = expected_provider_symbol.strip().upper()
    if returned_symbol != expected:
        return NormalizedSplits(
            returned_symbol, "SYMBOL_MAPPING_REQUIRED", (), 0,
            f"provider returned {returned_symbol}; expected {expected}",
        )
    rows = payload.get("splits")
    if not isinstance(rows, list):
        raise SplitSnapshotError("Twelve Data split response lacks a splits array")
    normalized: dict[bytes, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise SplitSnapshotError("Twelve Data split row is malformed")
        try:
            action_date = date.fromisoformat(row["date"])
        except ValueError as exc:
            raise SplitSnapshotError("Twelve Data split date is invalid") from exc
        if not start_date <= action_date <= end_date:
            continue
        item = dict(row)
        item["date"] = action_date.isoformat()
        try:
            key = canonical_bytes(item)
        except (TypeError, ValueError) as exc:
            raise SplitSnapshotError("Twelve Data split row is not canonical JSON") from exc
        normalized[key] = item
    actions = tuple(
        normalized[key]
        for key in sorted(normalized, key=lambda value: (json.loads(value)["date"], value))
    )
    return NormalizedSplits(returned_symbol, "COMPLETE", actions, len(rows))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _credit_evidence(usage: object, client: TwelveDataSplitClient, required: int) -> dict[str, Any]:
    return {
        "schema_name": "trinity.twelvedata-split-credit-telemetry",
        "schema_version": "1",
        "provider": PRICE_PROVIDER,
        "retrieved_at_utc": usage.retrieved_at_utc,
        "provider_response": usage.usage,
        "response_headers": usage.response_headers,
        "guard": {
            "required_daily_credits": required,
            "available_daily_credits": client.daily_remaining,
            "credit_cost_per_symbol": SPLIT_CREDIT_COST,
            "safety_reserve": DAILY_CREDIT_RESERVE,
        },
        "events": list(client.credit_events),
    }


def _persist_credit(root: Path, usage: object, client: TwelveDataSplitClient, required: int) -> None:
    _atomic_write(
        root / "provider_credit_telemetry.json",
        canonical_bytes(_credit_evidence(usage, client, required)),
    )


def _persist_failure(root: Path, evidence: Mapping[str, Any]) -> None:
    _atomic_write(root / "acquisition_failure.json", canonical_bytes(dict(evidence)))


def acquire_split_snapshot(
    *, parent_root: str | Path, output_root: str | Path,
    client: TwelveDataSplitClient | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    seal_read_only: bool = True,
) -> dict[str, Any]:
    root = Path(output_root)
    if root.exists() and any(root.iterdir()):
        raise SplitSnapshotError(f"split snapshot output must be new and empty: {root}")
    raw_root, normalized_root = root / "raw", root / "normalized"
    raw_root.mkdir(parents=True, exist_ok=True)
    normalized_root.mkdir(parents=True, exist_ok=True)
    tickers = split_universe(parent_root)
    universe_sha = hashlib.sha256(canonical_bytes(tickers)).hexdigest()
    provider = client or TwelveDataSplitClient()
    usage = provider.fetch_api_usage()
    required = required_split_credits(len(tickers))
    request_limit = provider.configure_split_rate(int(usage.usage.get("plan_limit", 0)))
    _persist_credit(root, usage, provider, required)
    if usage.daily_remaining < required:
        failure = CreditGuardError(
            "INSUFFICIENT_DAILY_CREDITS",
            "insufficient Twelve Data daily credits to start split acquisition",
            required=required, available=usage.daily_remaining,
            shortfall=required - usage.daily_remaining,
        )
        _persist_failure(root, failure.evidence())
        raise failure
    started_at = now()
    entries: list[dict[str, Any]] = []
    for index, ticker in enumerate(tickers):
        provider_symbol = SPLIT_PROVIDER_OVERRIDES.get(ticker, ticker)
        raw: bytes | None = None
        attempts: Sequence[Mapping[str, Any]] = ()
        error: str | None = None
        normalized = NormalizedSplits(provider_symbol, "PROVIDER_FAILURE", (), 0)
        try:
            capture = provider.fetch_split(
                provider_symbol, remaining_symbols=len(tickers) - index,
            )
            raw, attempts = capture.raw, capture.attempts
            normalized = normalize_split_response(
                raw, expected_provider_symbol=provider_symbol,
            )
        except (DailyQuotaExhausted, CreditGuardError) as exc:
            _persist_credit(root, usage, provider, required)
            evidence = exc.evidence() if isinstance(exc, CreditGuardError) else {
                "classification": exc.classification,
                "message": str(exc),
                "attempts": [dict(item) for item in exc.attempts],
            }
            _persist_failure(root, {**evidence, "ticker": ticker})
            raise
        except ProviderRequestError as exc:
            raw, attempts, error = exc.raw, exc.attempts, str(exc)
        except (SplitSnapshotError, TypeError, ValueError) as exc:
            error = str(exc)
            normalized = NormalizedSplits(provider_symbol, "INVALID_DATA", (), 0, error)
        configured_secret = getattr(provider, "api_key", None)
        if raw is not None and isinstance(configured_secret, str) \
                and configured_secret.encode("utf-8") in raw:
            raw = None
            error = f"{ticker}: provider split response contained configured secret"
            normalized = NormalizedSplits(provider_symbol, "INVALID_DATA", (), 0, error)
        if raw is not None:
            _atomic_write(raw_root / f"{ticker}.json", raw)
        actions_bytes = canonical_bytes(list(normalized.actions))
        _atomic_write(normalized_root / f"{ticker}.json", actions_bytes)
        entries.append({
            "canonical_ticker": ticker,
            "requested_provider_symbol": provider_symbol,
            "provider_symbol": normalized.provider_symbol,
            "classification": normalized.classification,
            "provider_row_count": normalized.provider_row_count,
            "normalized_row_count": len(normalized.actions),
            "content_sha256": hashlib.sha256(actions_bytes).hexdigest(),
            "raw_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "attempts": [dict(item) for item in attempts],
            "retry_count": max(0, len(attempts) - 1),
            "error": error or normalized.error,
        })
        _persist_credit(root, usage, provider, required)
    completed_at = now()
    inventory = file_inventory(root, exclude_names=frozenset({"split_snapshot_manifest.json"}))
    ready = len(entries) == len(tickers) and all(
        entry["classification"] == "COMPLETE" for entry in entries
    )
    manifest: dict[str, Any] = {
        "schema_name": SPLIT_SCHEMA,
        "schema_version": SPLIT_SCHEMA_VERSION,
        "provider": PRICE_PROVIDER,
        "endpoint": SPLITS_ENDPOINT,
        "parameters": {"symbol": "{provider_symbol}"},
        "filter_start_date_inclusive": SPLIT_START_DATE.isoformat(),
        "filter_end_date_inclusive": SPLIT_END_DATE.isoformat(),
        "acquisition_started_at_utc": utc_timestamp(started_at),
        "acquisition_completed_at_utc": utc_timestamp(completed_at),
        "parent_snapshot_commit": SPLIT_PARENT_COMMIT,
        "canonical_ticker_count": len(tickers),
        "canonical_tickers": tickers,
        "canonical_tickers_sha256": universe_sha,
        "provider_symbol_overrides": dict(SPLIT_PROVIDER_OVERRIDES),
        "credit_cost_per_symbol": SPLIT_CREDIT_COST,
        "required_daily_credits": required,
        "maximum_requests_per_61_seconds": request_limit,
        "coverage_gate": {
            "ready": ready,
            "blocking_tickers": [
                entry["canonical_ticker"] for entry in entries
                if entry["classification"] != "COMPLETE"
            ],
        },
        "tickers": entries,
        "files": inventory,
        "ensemble_sha256": hashlib.sha256(canonical_bytes(inventory)).hexdigest(),
    }
    manifest_path = root / "split_snapshot_manifest.json"
    _atomic_write(manifest_path, canonical_json_bytes(manifest, newline=True))
    if seal_read_only:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            path.chmod(stat.S_IREAD)
    manifest["manifest_file_sha256"] = sha256_file(manifest_path)
    return manifest


def verify_split_snapshot(
    root: str | Path, *, require_ready: bool = True, require_raw: bool = True,
) -> dict[str, Any]:
    base = Path(root)
    try:
        manifest = json.loads(
            (base / "split_snapshot_manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SplitSnapshotError("invalid split snapshot manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_name") != SPLIT_SCHEMA \
            or manifest.get("schema_version") != SPLIT_SCHEMA_VERSION:
        raise SplitSnapshotError("unsupported split snapshot schema")
    if manifest.get("endpoint") != SPLITS_ENDPOINT \
            or manifest.get("provider") != PRICE_PROVIDER:
        raise SplitSnapshotError("split provider contract mismatch")
    actual = file_inventory(base, exclude_names=frozenset({"split_snapshot_manifest.json"}))
    if actual != manifest.get("files"):
        raise SplitSnapshotError("split snapshot inventory mismatch")
    if hashlib.sha256(canonical_bytes(actual)).hexdigest() != manifest.get("ensemble_sha256"):
        raise SplitSnapshotError("split snapshot ensemble hash mismatch")
    entries = manifest.get("tickers")
    if not isinstance(entries, list) or len(entries) != SPLIT_CANONICAL_TICKER_COUNT:
        raise SplitSnapshotError("split snapshot ticker count mismatch")
    expected_tickers = manifest.get("canonical_tickers")
    if not isinstance(expected_tickers, list) or sorted(expected_tickers) != expected_tickers \
            or len(set(expected_tickers)) != len(expected_tickers):
        raise SplitSnapshotError("split snapshot canonical ticker list is invalid")
    if hashlib.sha256(canonical_bytes(expected_tickers)).hexdigest() \
            != manifest.get("canonical_tickers_sha256"):
        raise SplitSnapshotError("split snapshot universe hash mismatch")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("canonical_ticker") not in expected_tickers:
            raise SplitSnapshotError("split snapshot ticker metadata is invalid")
        ticker = str(entry["canonical_ticker"])
        normalized_path = base / "normalized" / f"{ticker}.json"
        try:
            content = normalized_path.read_bytes()
            actions = json.loads(content)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SplitSnapshotError(f"invalid normalized split file: {ticker}") from exc
        if hashlib.sha256(content).hexdigest() != entry.get("content_sha256") \
                or not isinstance(actions, list) \
                or len(actions) != entry.get("normalized_row_count"):
            raise SplitSnapshotError(f"normalized split hash/count mismatch: {ticker}")
        if require_raw and entry.get("raw_sha256"):
            raw_path = base / "raw" / f"{ticker}.json"
            if not raw_path.is_file() \
                    or sha256_file(raw_path) != entry.get("raw_sha256"):
                raise SplitSnapshotError(f"raw split hash mismatch: {ticker}")
    gate = manifest.get("coverage_gate")
    if not isinstance(gate, dict) or type(gate.get("ready")) is not bool:
        raise SplitSnapshotError("split snapshot lacks coverage gate")
    calculated_ready = all(
        isinstance(entry, dict) and entry.get("classification") == "COMPLETE"
        for entry in entries
    )
    if gate["ready"] != calculated_ready:
        raise SplitSnapshotError("split snapshot coverage gate is inconsistent")
    if require_ready and not gate["ready"]:
        blockers = ", ".join(str(item) for item in gate.get("blocking_tickers", []))
        raise SplitSnapshotError(f"split snapshot is NOT READY: {blockers}")
    return manifest


def _action_dates(root: Path, manifest: Mapping[str, Any]) -> dict[str, tuple[date, ...]]:
    result: dict[str, tuple[date, ...]] = {}
    tickers = manifest.get("canonical_tickers")
    assert isinstance(tickers, list)
    for ticker_value in tickers:
        ticker = str(ticker_value)
        rows = json.loads((root / "normalized" / f"{ticker}.json").read_text("utf-8"))
        result[ticker] = tuple(sorted({date.fromisoformat(str(row["date"])) for row in rows}))
    return result


def _parse_optional_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SplitSnapshotError(f"invalid input date: {text}") from exc


def apply_split_screen(
    *, parent_root: str | Path, split_root: str | Path, output_root: str | Path,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    parent = Path(parent_root)
    split_base = Path(split_root)
    output = Path(output_root)
    if output.exists() and any(output.iterdir()):
        raise SplitSnapshotError(f"Phase 1.1 output must be new and empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    split_manifest = verify_split_snapshot(split_base, require_ready=True, require_raw=True)
    actions = _action_dates(split_base, split_manifest)
    dev = _read_csv(parent / "h2_v2_1_inputs_dev.csv")
    prosp = _read_csv(parent / "h2_v2_1_inputs_prosp_batch1.csv")
    exclusions = _read_csv(parent / "h2_v2_1_exclusions.csv")
    added: list[dict[str, str]] = []

    def screen(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        screened: list[dict[str, str]] = []
        for source in rows:
            row = dict(source)
            lower = _parse_optional_date(row.get("price_window_start"))
            upper = _parse_optional_date(row.get("reference_session_date"))
            matches = (
                tuple(day for day in actions.get(str(row.get("ticker") or ""), ()) if lower < day <= upper)
                if lower is not None and upper is not None else ()
            )
            if matches:
                reasons = [item for item in str(row.get("exclusion_reasons") or "").split(";") if item]
                if SPLIT_REASON not in reasons:
                    reasons.append(SPLIT_REASON)
                row["exclusion_reasons"] = ";".join(reasons)
                row["row_status"] = "EXCLUDED"
                added.append({
                    "sample": str(row.get("sample") or ""),
                    "release_period": str(row.get("release_period") or ""),
                    "ticker": str(row.get("ticker") or ""),
                    "current_release_id": str(row.get("current_release_id") or ""),
                    "release_at": str(row.get("release_at") or ""),
                    "event_timing": str(row.get("event_timing") or ""),
                    "reference_session_date": str(row.get("reference_session_date") or ""),
                    "reason": SPLIT_REASON,
                    "detail": "provider=TWELVE_DATA;action_dates=" + ",".join(
                        day.isoformat() for day in matches
                    ),
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
    split_source_path = output / "h2_v2_1_split_source_manifest.json"
    split_source_path.write_bytes(
        canonical_json_bytes(split_manifest, newline=True),
    )
    csv_hashes = {name: sha256_file(path) for name, path in paths.items()}
    counts = {
        sample: len({row["current_release_id"] for row in added if row["sample"] == sample})
        for sample in ("DEV", "PROSP_BATCH1")
    }
    eligible = {
        "DEV": sum(row.get("row_status") == "INCLUDED" for row in dev_screened),
        "PROSP_BATCH1": sum(row.get("row_status") == "INCLUDED" for row in prosp_screened),
    }
    parent_hashes = {
        name: sha256_file(parent / filename)
        for name, filename in (
            ("dev", "h2_v2_1_inputs_dev.csv"),
            ("prosp", "h2_v2_1_inputs_prosp_batch1.csv"),
            ("exclusions", "h2_v2_1_exclusions.csv"),
        )
    }
    manifest = {
        "schema_name": PHASE1_1_SCHEMA,
        "schema_version": PHASE1_1_SCHEMA_VERSION,
        "built_at_utc": utc_timestamp(now()),
        "parent_snapshot_commit": SPLIT_PARENT_COMMIT,
        "parent_csv_sha256": parent_hashes,
        "split_ensemble_sha256": split_manifest["ensemble_sha256"],
        "split_manifest_sha256": sha256_file(split_base / "split_snapshot_manifest.json"),
        "split_source_commit_view_sha256": sha256_file(split_source_path),
        "csv_sha256": csv_hashes,
        "corporate_action_split_event_count": counts,
        "primary_eligible_count": eligible,
        "interval_rule": "price_window_start < split_date <= reference_session_date",
    }
    manifest_path = output / "h2_v2_1_phase1_1_manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest, newline=True))
    manifest["manifest_file_sha256"] = sha256_file(manifest_path)
    return manifest
