"""Immutable, registry-independent stock observation snapshot contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

import exchange_calendars

from trinity.twelvedata_prices import (
    ADJUSTMENT,
    HISTORY_CALENDAR_DAYS,
    INTERVAL,
    PRICE_PROVIDER,
    PriceSnapshotError,
    canonical_bytes,
    exclusive_end_date,
    normalize_response,
    verify_snapshot,
)

from .contracts import ValidationContractError
from .market_calendar import CALENDAR_NAME, CALENDAR_VERSION, add_tradable_at, session_times
from .protocol_v1 import PROTOCOL_HASH


STOCK_OBSERVATION_FINALITY_GRACE_MINUTES = 20
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.-]*\Z")


class StockObservationError(ValidationContractError):
    """A source price snapshot cannot satisfy the stock observation contract."""


@dataclass(frozen=True)
class StockObservationSeries:
    ticker: str
    provider_symbol: str
    classification: str
    first_session: str
    last_session: str
    bars: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class StockObservationSnapshot:
    provider: str
    interval: str
    adjustment: str
    source_snapshot_id: str
    source_snapshot_sha256: str
    observation_target_session: str
    acquisition_started_at_utc: str
    acquisition_completed_at_utc: str
    expected_tickers: tuple[str, ...]
    expected_ticker_set_sha256: str
    calendar: str
    calendar_version: str
    tickers: tuple[StockObservationSeries, ...]

    def series(self, ticker: str) -> StockObservationSeries:
        normalized = normalize_expected_tickers((ticker,))[0]
        for item in self.tickers:
            if item.ticker == normalized:
                return item
        raise StockObservationError(f"ticker absent from stock observation: {normalized}")


STOCK_OBSERVATION_SCOPE = "STOCK_OBSERVATION_SUBSET"
STOCK_OBSERVATION_IDENTITY_SCHEMA = "TRINITY_STOCK_OBSERVATION_V1"


def normalize_expected_tickers(expected_tickers: Iterable[str]) -> tuple[str, ...]:
    """Canonicalize the frozen subset without consulting the current registry."""
    if isinstance(expected_tickers, (str, bytes)):
        raise StockObservationError("expected ticker set must be an iterable of tickers")
    normalized: list[str] = []
    try:
        values = tuple(expected_tickers)
    except TypeError as exc:
        raise StockObservationError("expected ticker set is invalid") from exc
    if not values:
        raise StockObservationError("expected ticker set must not be empty")
    for value in values:
        if not isinstance(value, str):
            raise StockObservationError("expected ticker must be a string")
        ticker = value.strip().upper()
        if not ticker or not _TICKER.fullmatch(ticker):
            raise StockObservationError(f"invalid expected ticker: {value!r}")
        normalized.append(ticker)
    if len(normalized) != len(set(normalized)):
        raise StockObservationError("expected ticker set contains duplicates")
    return tuple(sorted(normalized))


def expected_ticker_set_sha256(expected_tickers: Iterable[str]) -> str:
    """Return the stable identity of a normalized, sorted expected ticker set."""

    normalized = normalize_expected_tickers(expected_tickers)
    return hashlib.sha256(canonical_bytes(list(normalized))).hexdigest()


def verify_stock_observation_manifest_identity(
    manifest: Mapping[str, Any], *, expected_tickers: Iterable[str] | None = None,
    expected_expectation_set_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify additive subset identity fields independently of local paths."""

    if manifest.get("snapshot_scope") != STOCK_OBSERVATION_SCOPE:
        raise StockObservationError("stock observation snapshot scope mismatch")
    if manifest.get("protocol_hash") != PROTOCOL_HASH:
        raise StockObservationError("stock observation protocol hash mismatch")
    expectation_sha = manifest.get("expectation_set_sha256")
    if not isinstance(expectation_sha, str) or len(expectation_sha) != 64 \
            or any(char not in "0123456789abcdef" for char in expectation_sha):
        raise StockObservationError("expectation_set_sha256 must be a lowercase SHA-256")
    if expected_expectation_set_sha256 is not None \
            and expectation_sha != expected_expectation_set_sha256:
        raise StockObservationError("stock observation expectation-set mismatch")
    entries = manifest.get("tickers")
    if not isinstance(entries, list):
        raise StockObservationError("stock observation ticker metadata is malformed")
    manifest_tickers = tuple(sorted(str(item.get("canonical_ticker") or "") for item in entries))
    normalized = normalize_expected_tickers(
        manifest_tickers if expected_tickers is None else expected_tickers
    )
    if manifest_tickers != normalized or manifest.get("expected_ticker_count") != len(normalized):
        raise StockObservationError("stock observation expected ticker membership mismatch")
    ticker_sha = expected_ticker_set_sha256(normalized)
    if manifest.get("expected_ticker_set_sha256") != ticker_sha:
        raise StockObservationError("stock observation expected ticker-set SHA mismatch")
    attempt = manifest.get("acquisition_attempt")
    if type(attempt) is not int or attempt < 1:
        raise StockObservationError("stock observation acquisition attempt is invalid")
    target = manifest.get("target_market_session")
    core = {
        "schema": STOCK_OBSERVATION_IDENTITY_SCHEMA,
        "protocol_hash": PROTOCOL_HASH,
        "observation_target_session": target,
        "expectation_set_sha256": expectation_sha,
        "expected_ticker_set_sha256": ticker_sha,
        "acquisition_attempt": attempt,
    }
    identity_sha = hashlib.sha256(canonical_bytes(core)).hexdigest()
    snapshot_id = f"{STOCK_OBSERVATION_IDENTITY_SCHEMA}_{identity_sha}"
    if manifest.get("identity_core") != core \
            or manifest.get("identity_sha256") != identity_sha \
            or manifest.get("snapshot_id") != snapshot_id:
        raise StockObservationError("stock observation deterministic identity mismatch")
    return {
        "expected_tickers": normalized,
        "expected_ticker_set_sha256": ticker_sha,
        "expectation_set_sha256": expectation_sha,
        "identity_core": core,
        "identity_sha256": identity_sha,
        "snapshot_id": snapshot_id,
    }


def _aware(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise StockObservationError(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        raise StockObservationError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _expected_xnys_sessions(start: date, target: date) -> tuple[str, ...]:
    calendar = exchange_calendars.get_calendar(CALENDAR_NAME, start=start, end=target)
    sessions = tuple(item.date().isoformat() for item in calendar.sessions)
    if not sessions or sessions[-1] != target.isoformat():
        raise StockObservationError(f"target is not an XNYS session: {target}")
    return sessions


def _validate_attempt_timestamps(
    attempts: object, *, started: datetime, completed: datetime, finality_cutoff: datetime,
    ticker: str,
) -> None:
    if not isinstance(attempts, list):
        raise StockObservationError(f"{ticker}: provider attempts are malformed")
    with_timestamp = [
        item for item in attempts
        if isinstance(item, Mapping) and "requested_at_utc" in item
    ]
    if not with_timestamp:
        return
    if len(with_timestamp) != len(attempts):
        raise StockObservationError(f"{ticker}: provider attempt timestamp provenance is incomplete")
    previous: datetime | None = None
    lower = max(started, finality_cutoff)
    for item in with_timestamp:
        requested = _aware(item.get("requested_at_utc"), f"{ticker} requested_at_utc")
        if requested < lower or requested > completed:
            raise StockObservationError(f"{ticker}: requested_at_utc is outside acquisition bounds")
        if previous is not None and requested < previous:
            raise StockObservationError(f"{ticker}: requested_at_utc values are not ordered")
        previous = requested


def _load_bars(root: Path, ticker: str) -> tuple[list[dict[str, Any]], bytes]:
    try:
        raw = (root / "normalized" / f"{ticker}.json").read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StockObservationError(f"{ticker}: normalized bars are invalid") from exc
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise StockObservationError(f"{ticker}: normalized bars are empty or malformed")
    return [dict(item) for item in value], raw


def _validate_provider_raw(
    raw: bytes, *, ticker: str, provider_symbol: str,
) -> None:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StockObservationError(f"{ticker}: raw provider response is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise StockObservationError(f"{ticker}: raw provider metadata is missing")
    meta = payload["meta"]
    if str(meta.get("symbol") or "").upper() != provider_symbol:
        raise StockObservationError(f"{ticker}: raw provider symbol mismatch")
    if meta.get("interval") != INTERVAL:
        raise StockObservationError(f"{ticker}: raw provider interval mismatch")


def _validate_series(
    root: Path, entry: Mapping[str, Any], *, expected_sessions: tuple[str, ...],
    target: date, started: datetime, completed: datetime, finality_cutoff: datetime,
) -> StockObservationSeries:
    ticker = str(entry.get("canonical_ticker") or "")
    provider_symbol = str(entry.get("provider_symbol") or "").upper()
    classification = str(entry.get("classification") or "")
    if provider_symbol != ticker:
        raise StockObservationError(f"{ticker}: provider symbol does not match canonical ticker")
    bars, normalized_raw = _load_bars(root, ticker)
    dates = [str(item.get("date") or "") for item in bars]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise StockObservationError(f"{ticker}: sessions must be unique and ordered")
    if classification == "ACTIVE_COMPLETE":
        if tuple(dates) != expected_sessions:
            raise StockObservationError(
                f"{ticker}: ACTIVE_COMPLETE requested-window XNYS coverage mismatch"
            )
    elif classification == "CORPORATE_ACTION_NO_LONGER_TRADING":
        expected_set = set(expected_sessions)
        if any(session not in expected_set for session in dates):
            raise StockObservationError(f"{ticker}: terminal bars are outside the requested XNYS window")
    else:
        raise StockObservationError(f"{ticker}: stock observation classification is not READY")
    try:
        adapted = add_tradable_at(bars)
    except ValidationContractError as exc:
        raise StockObservationError(f"{ticker}: {exc}") from exc
    if entry.get("first_date") != dates[0] \
            or entry.get("last_valid_date") != dates[-1] \
            or entry.get("bar_count") != len(bars):
        raise StockObservationError(f"{ticker}: bar coverage metadata mismatch")
    try:
        raw = (root / "raw" / f"{ticker}.json").read_bytes()
    except OSError as exc:
        raise StockObservationError(f"{ticker}: raw provider response is missing") from exc
    _validate_provider_raw(raw, ticker=ticker, provider_symbol=provider_symbol)
    try:
        capture = normalize_response(ticker, raw, target_session=target)
    except (PriceSnapshotError, TypeError, ValueError) as exc:
        raise StockObservationError(f"{ticker}: raw provider response cannot be normalized") from exc
    if capture.provider_symbol != provider_symbol or capture.classification != classification:
        raise StockObservationError(f"{ticker}: provider provenance does not reproduce classification")
    if canonical_bytes(list(capture.bars)) != normalized_raw:
        raise StockObservationError(f"{ticker}: normalized bars are not reproducible from raw")
    if list(capture.excluded_rows) != entry.get("excluded_rows"):
        raise StockObservationError(f"{ticker}: excluded-row provenance mismatch")
    _validate_attempt_timestamps(
        entry.get("attempts"), started=started, completed=completed,
        finality_cutoff=finality_cutoff, ticker=ticker,
    )
    return StockObservationSeries(
        ticker=ticker, provider_symbol=provider_symbol, classification=classification,
        first_session=dates[0], last_session=dates[-1], bars=adapted,
    )


def load_stock_observation_snapshot(
    root: str | Path, *, expected_tickers: Iterable[str],
    expected_observation_target_session: str,
) -> StockObservationSnapshot:
    """Verify and adapt an immutable stock snapshot for the frozen ticker set."""

    normalized_expected = normalize_expected_tickers(expected_tickers)
    expected_sha = expected_ticker_set_sha256(normalized_expected)
    base = Path(root).resolve()
    try:
        manifest = verify_snapshot(base, require_ready=True, require_raw=True)
    except PriceSnapshotError as exc:
        raise StockObservationError(f"invalid stock observation source snapshot: {exc}") from exc
    if manifest.get("snapshot_scope") is not None:
        verify_stock_observation_manifest_identity(
            manifest, expected_tickers=normalized_expected,
        )
    if manifest.get("provider") != PRICE_PROVIDER \
            or manifest.get("interval") != INTERVAL \
            or manifest.get("adjust") != ADJUSTMENT:
        raise StockObservationError("stock observation provider contract mismatch")
    target_value = str(manifest.get("target_market_session") or "")
    if target_value != expected_observation_target_session:
        raise StockObservationError("stock observation target session mismatch")
    try:
        target = date.fromisoformat(target_value)
    except ValueError as exc:
        raise StockObservationError("stock observation target session is invalid") from exc
    request_start = target - timedelta(days=HISTORY_CALENDAR_DAYS)
    if manifest.get("request_start_date") != request_start.isoformat() \
            or manifest.get("request_end_date_exclusive") != exclusive_end_date(target).isoformat():
        raise StockObservationError("stock observation request window mismatch")
    expected_sessions = _expected_xnys_sessions(request_start, target)
    entries = manifest.get("tickers")
    if not isinstance(entries, list):
        raise StockObservationError("stock observation ticker metadata is malformed")
    actual = tuple(sorted(str(item.get("canonical_ticker") or "") for item in entries))
    if len(entries) != len(normalized_expected) or actual != normalized_expected:
        missing = sorted(set(normalized_expected) - set(actual))
        unexpected = sorted(set(actual) - set(normalized_expected))
        raise StockObservationError(
            "stock observation ticker membership mismatch: "
            f"missing={','.join(missing) or '-'} unexpected={','.join(unexpected) or '-'}"
        )
    started = _aware(manifest.get("acquisition_started_at_utc"), "acquisition_started_at_utc")
    completed = _aware(
        manifest.get("acquisition_completed_at_utc"), "acquisition_completed_at_utc",
    )
    if completed < started:
        raise StockObservationError("stock observation acquisition timestamps are reversed")
    _opened, closed = session_times(target_value)
    finality_cutoff = closed + timedelta(minutes=STOCK_OBSERVATION_FINALITY_GRACE_MINUTES)
    if started < finality_cutoff:
        raise StockObservationError("stock observation target session is not final")
    by_ticker = {str(item["canonical_ticker"]): item for item in entries}
    series = tuple(
        _validate_series(
            base, by_ticker[ticker], expected_sessions=expected_sessions,
            target=target, started=started, completed=completed,
            finality_cutoff=finality_cutoff,
        )
        for ticker in normalized_expected
    )
    return StockObservationSnapshot(
        provider=PRICE_PROVIDER, interval=INTERVAL, adjustment=ADJUSTMENT,
        source_snapshot_id=str(manifest["snapshot_id"]),
        source_snapshot_sha256=str(manifest["snapshot_sha256"]),
        observation_target_session=target_value,
        acquisition_started_at_utc=str(manifest["acquisition_started_at_utc"]),
        acquisition_completed_at_utc=str(manifest["acquisition_completed_at_utc"]),
        expected_tickers=normalized_expected, expected_ticker_set_sha256=expected_sha,
        calendar=CALENDAR_NAME, calendar_version=CALENDAR_VERSION, tickers=series,
    )
