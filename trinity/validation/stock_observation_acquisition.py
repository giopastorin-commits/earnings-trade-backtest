"""Production acquisition for immutable, registry-independent stock observations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from trinity.twelvedata_prices import (
    ADJUSTMENT,
    ALLOWED_READY_STATES,
    DAILY_CREDIT_RESERVE,
    HISTORY_CALENDAR_DAYS,
    INTERVAL,
    PRICE_PROVIDER,
    RATE_CREDITS,
    RATE_WINDOW_SECONDS,
    ApiUsageCapture,
    CreditGuardError,
    DailyQuotaExhausted,
    NormalizedCapture,
    PriceSnapshotError,
    ProviderRequestError,
    TwelveDataClient,
    canonical_bytes,
    exclusive_end_date,
    normalize_response,
    snapshot_hash,
    utc_timestamp,
)

from .contracts import ValidationContractError
from .market_calendar import session_times
from .protocol_v1 import PROTOCOL_HASH
from .stock_observation import (
    STOCK_OBSERVATION_IDENTITY_SCHEMA,
    STOCK_OBSERVATION_SCOPE,
    STOCK_OBSERVATION_FINALITY_GRACE_MINUTES,
    StockObservationError,
    expected_ticker_set_sha256,
    load_stock_observation_snapshot,
    normalize_expected_tickers,
)


class StockObservationAcquisitionError(ValidationContractError):
    """A subset acquisition cannot produce an authoritative stock observation."""


def required_subset_daily_credits(ticker_count: int) -> int:
    if type(ticker_count) is not int or ticker_count <= 0:
        raise StockObservationAcquisitionError("subset ticker count must be positive")
    return ticker_count + DAILY_CREDIT_RESERVE


def _require_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 \
            or any(char not in "0123456789abcdef" for char in value):
        raise StockObservationAcquisitionError(f"{field} must be a lowercase SHA-256")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StockObservationAcquisitionError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def stock_observation_identity(
    *, target_session: date, expectation_set_sha256: str,
    expected_tickers: Iterable[str], acquisition_attempt: int,
) -> dict[str, Any]:
    tickers = normalize_expected_tickers(expected_tickers)
    expectation_sha = _require_sha(expectation_set_sha256, "expectation_set_sha256")
    if type(acquisition_attempt) is not int or acquisition_attempt < 1:
        raise StockObservationAcquisitionError("acquisition_attempt must be an integer >= 1")
    ticker_sha = expected_ticker_set_sha256(tickers)
    core = {
        "schema": STOCK_OBSERVATION_IDENTITY_SCHEMA,
        "protocol_hash": PROTOCOL_HASH,
        "observation_target_session": target_session.isoformat(),
        "expectation_set_sha256": expectation_sha,
        "expected_ticker_set_sha256": ticker_sha,
        "acquisition_attempt": acquisition_attempt,
    }
    digest = hashlib.sha256(canonical_bytes(core)).hexdigest()
    return {
        "identity_core": core,
        "identity_sha256": digest,
        "snapshot_id": f"{STOCK_OBSERVATION_IDENTITY_SCHEMA}_{digest}",
        "expected_tickers": tickers,
        "expected_ticker_set_sha256": ticker_sha,
    }


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _secret_bytes(provider: Any) -> bytes | None:
    value = getattr(provider, "api_key", None)
    return value.encode("utf-8") if isinstance(value, str) and value else None


def _safe_payload(raw: bytes, provider: Any) -> bool:
    secret = _secret_bytes(provider)
    return secret is None or secret not in raw


def _sanitized_error(value: object, provider: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    secret = getattr(provider, "api_key", None)
    if isinstance(secret, str) and secret:
        text = text.replace(secret, "[REDACTED]")
    return text[:1000]


def _telemetry(
    usage: ApiUsageCapture, provider: Any, *, required: int,
) -> dict[str, Any]:
    available = getattr(provider, "daily_remaining", usage.daily_remaining)
    return {
        "schema_name": "trinity.twelvedata-credit-telemetry",
        "schema_version": "1",
        "provider": PRICE_PROVIDER,
        "retrieved_at_utc": usage.retrieved_at_utc,
        "provider_response": usage.usage,
        "response_headers": usage.response_headers,
        "guard": {
            "minimum_required_remaining": required,
            "safety_reserve": DAILY_CREDIT_RESERVE,
            "available_daily_credits": available,
            "shortfall": max(0, required - available) if available is not None else None,
        },
        "events": list(getattr(provider, "credit_events", ())),
    }


def _provider_metadata_valid(raw: bytes, ticker: str) -> bool:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    meta = value.get("meta") if isinstance(value, dict) else None
    return isinstance(meta, dict) \
        and str(meta.get("symbol") or "").upper() == ticker \
        and meta.get("interval") == INTERVAL


def acquire_stock_observation(
    *, target_session: date, expected_tickers: Iterable[str],
    expectation_set_sha256: str, output_root: str | Path,
    acquisition_attempt: int = 1, client: TwelveDataClient | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Acquire and self-verify one exact immutable stock-observation subset."""

    if not isinstance(target_session, date) or isinstance(target_session, datetime):
        raise StockObservationAcquisitionError("target_session must be a date")
    identity = stock_observation_identity(
        target_session=target_session, expectation_set_sha256=expectation_set_sha256,
        expected_tickers=expected_tickers, acquisition_attempt=acquisition_attempt,
    )
    tickers = identity["expected_tickers"]
    root = Path(output_root)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise StockObservationAcquisitionError("stock observation output root must be new or empty")
    started = _utc(now(), "acquisition_started_at_utc")
    try:
        _opened, closed = session_times(target_session.isoformat())
    except ValidationContractError as exc:
        raise StockObservationAcquisitionError("target_session is not an XNYS session") from exc
    finality = closed + timedelta(minutes=STOCK_OBSERVATION_FINALITY_GRACE_MINUTES)
    if started < finality:
        raise StockObservationAcquisitionError("stock observation target session is not final")

    provider = client or TwelveDataClient()
    try:
        usage = provider.fetch_api_usage()
    except (PriceSnapshotError, TypeError, ValueError) as exc:
        raise StockObservationAcquisitionError(
            f"Twelve Data credit preflight failed: {_sanitized_error(exc, provider)}"
        ) from exc
    required = required_subset_daily_credits(len(tickers))
    root.mkdir(parents=True, exist_ok=True)
    telemetry_raw = canonical_bytes(_telemetry(usage, provider, required=required))
    if not _safe_payload(telemetry_raw, provider):
        raise StockObservationAcquisitionError("provider credit telemetry contained configured secret")
    _atomic_write(root / "provider_credit_telemetry.json", telemetry_raw)
    if usage.daily_remaining < required:
        raise StockObservationAcquisitionError(
            "insufficient Twelve Data daily credits for stock observation subset: "
            f"required={required} available={usage.daily_remaining}"
        )

    history_start = target_session - timedelta(days=HISTORY_CALENDAR_DAYS)
    entries: list[dict[str, Any]] = []
    for index, ticker in enumerate(tickers):
        attempts: Sequence[Mapping[str, Any]] = ()
        raw: bytes | None = None
        error: str | None = None
        normalized = NormalizedCapture(ticker, "PROVIDER_FAILURE", (), (), None)
        try:
            capture = provider.fetch(
                ticker, start_date=history_start, target_session=target_session,
                remaining_tickers=len(tickers) - index,
            )
            raw, attempts = capture.raw, capture.attempts
            if not _safe_payload(raw, provider):
                raw = None
                error = f"{ticker}: configured secret detected; provider evidence rejected"
                normalized = NormalizedCapture(ticker, "INVALID_DATA", (), (), error)
            elif not _provider_metadata_valid(raw, ticker):
                error = f"{ticker}: provider symbol or interval contract mismatch"
                normalized = NormalizedCapture(ticker, "INVALID_DATA", (), (), error)
            else:
                normalized = normalize_response(ticker, raw, target_session=target_session)
        except (DailyQuotaExhausted, CreditGuardError) as exc:
            raise StockObservationAcquisitionError(str(exc)) from exc
        except ProviderRequestError as exc:
            attempts, error = exc.attempts, str(exc)
            raw = exc.raw if exc.raw is not None and _safe_payload(exc.raw, provider) else None
        except (PriceSnapshotError, TypeError, ValueError) as exc:
            error = str(exc)
            normalized = NormalizedCapture(ticker, "INVALID_DATA", (), (), error)
        attempts_raw = canonical_bytes([dict(item) for item in attempts])
        if not _safe_payload(attempts_raw, provider):
            attempts = ()
            raw = None
            error = f"{ticker}: configured secret detected; provider provenance rejected"
            normalized = NormalizedCapture(ticker, "INVALID_DATA", (), (), error)
        if normalized.classification in ALLOWED_READY_STATES and not attempts:
            raw = None
            error = f"{ticker}: provider attempt provenance is missing"
            normalized = NormalizedCapture(ticker, "INVALID_DATA", (), (), error)
        error = _sanitized_error(error or normalized.error, provider)
        if raw is not None:
            _atomic_write(root / "raw" / f"{ticker}.json", raw)
        bars = list(normalized.bars)
        normalized_raw = canonical_bytes(bars)
        _atomic_write(root / "normalized" / f"{ticker}.json", normalized_raw)
        entries.append({
            "canonical_ticker": ticker,
            "provider_symbol": normalized.provider_symbol,
            "classification": normalized.classification,
            "first_date": bars[0]["date"] if bars else None,
            "last_valid_date": bars[-1]["date"] if bars else None,
            "bar_count": len(bars),
            "content_sha256": hashlib.sha256(normalized_raw).hexdigest(),
            "raw_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "excluded_rows": list(normalized.excluded_rows),
            "attempts": [dict(item) for item in attempts],
            "retry_count": max(0, len(attempts) - 1),
            "error": error,
        })
        telemetry_raw = canonical_bytes(_telemetry(usage, provider, required=required))
        if not _safe_payload(telemetry_raw, provider):
            raise StockObservationAcquisitionError("provider telemetry contained configured secret")
        _atomic_write(root / "provider_credit_telemetry.json", telemetry_raw)

    completed = _utc(now(), "acquisition_completed_at_utc")
    if completed < started:
        raise StockObservationAcquisitionError("acquisition timestamps are reversed")
    states = (
        "ACTIVE_COMPLETE", "ACTIVE_MISSING_LATEST",
        "CORPORATE_ACTION_NO_LONGER_TRADING", "SYMBOL_MAPPING_REQUIRED",
        "PROVIDER_FAILURE", "INVALID_DATA",
    )
    counts = {state: sum(item["classification"] == state for item in entries) for state in states}
    ready = all(item["classification"] in ALLOWED_READY_STATES for item in entries)
    telemetry_path = root / "provider_credit_telemetry.json"
    manifest: dict[str, Any] = {
        "schema_name": "trinity.twelvedata-price-snapshot",
        "schema_version": "1",
        "snapshot_scope": STOCK_OBSERVATION_SCOPE,
        "snapshot_id": identity["snapshot_id"],
        "provider": PRICE_PROVIDER,
        "interval": INTERVAL,
        "adjust": ADJUSTMENT,
        "protocol_hash": PROTOCOL_HASH,
        "target_market_session": target_session.isoformat(),
        "request_start_date": history_start.isoformat(),
        "request_end_date_exclusive": exclusive_end_date(target_session).isoformat(),
        "acquisition_started_at_utc": utc_timestamp(started),
        "acquisition_completed_at_utc": utc_timestamp(completed),
        "expectation_set_sha256": expectation_set_sha256,
        "expected_ticker_count": len(tickers),
        "expected_ticker_set_sha256": identity["expected_ticker_set_sha256"],
        "acquisition_attempt": acquisition_attempt,
        "identity_core": identity["identity_core"],
        "identity_sha256": identity["identity_sha256"],
        "canonical_ticker_count": len(tickers),
        "active_complete_count": counts["ACTIVE_COMPLETE"],
        "explicit_exception_count": counts["CORPORATE_ACTION_NO_LONGER_TRADING"],
        "provider_failure_count": counts["PROVIDER_FAILURE"],
        "invalid_data_count": counts["INVALID_DATA"],
        "classification_counts": counts,
        "coverage_gate": {
            "ready": ready,
            "allowed_states": sorted(ALLOWED_READY_STATES),
            "blocking_tickers": [
                item["canonical_ticker"] for item in entries
                if item["classification"] not in ALLOWED_READY_STATES
            ],
        },
        "throttle": {
            "maximum_credits": RATE_CREDITS,
            "rolling_window_seconds": RATE_WINDOW_SECONDS,
        },
        "credit_guard": {
            "minimum_required_remaining": required,
            "safety_reserve": DAILY_CREDIT_RESERVE,
            "telemetry_file": "provider_credit_telemetry.json",
            "telemetry_sha256": hashlib.sha256(telemetry_path.read_bytes()).hexdigest(),
        },
        "tickers": entries,
    }
    manifest["snapshot_sha256"] = snapshot_hash(manifest)
    _atomic_write(root / "snapshot_manifest.json", canonical_bytes(manifest))
    if not ready:
        raise StockObservationAcquisitionError("stock observation subset is NOT READY")
    try:
        load_stock_observation_snapshot(
            root, expected_tickers=tickers,
            expected_observation_target_session=target_session.isoformat(),
        )
    except StockObservationError as exc:
        manifest["coverage_gate"]["ready"] = False
        manifest["coverage_gate"]["blocking_tickers"] = list(tickers)
        manifest["coverage_gate"]["verification_error"] = "AUTHORITATIVE_VERIFICATION_FAILED"
        manifest["snapshot_sha256"] = snapshot_hash(manifest)
        _atomic_write(root / "snapshot_manifest.json", canonical_bytes(manifest))
        raise StockObservationAcquisitionError(
            f"authoritative stock observation verification failed: {exc}"
        ) from exc
    return manifest
