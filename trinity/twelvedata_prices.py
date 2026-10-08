"""Validated immutable Twelve Data daily OHLCV snapshots for TRINITY."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

import requests

from trinity.usa_issuer_registry import DEFAULT_REGISTRY, load_registry


TWELVE_DATA_TIME_SERIES = "https://api.twelvedata.com/time_series"
SNAPSHOT_SCHEMA = "trinity.twelvedata-price-snapshot"
SNAPSHOT_VERSION = "1"
PRICE_PROVIDER = "TWELVE_DATA"
INTERVAL = "1day"
ADJUSTMENT = "none"
HISTORY_CALENDAR_DAYS = 450
RATE_CREDITS = 7
RATE_WINDOW_SECONDS = 61.0
MAX_ATTEMPTS = 3
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
ALLOWED_READY_STATES = frozenset({
    "ACTIVE_COMPLETE", "CORPORATE_ACTION_NO_LONGER_TRADING",
})
WBD_STALE_DATE = "2026-10-06"
WBD_LAST_NORMAL_SESSION = "2026-10-05"
WBD_STALE_PRICE = 30.95


class PriceSnapshotError(RuntimeError):
    """A provider response or snapshot violates the production contract."""


class ProviderRequestError(PriceSnapshotError):
    def __init__(
        self, message: str, *, attempts: Sequence[Mapping[str, Any]], raw: bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = tuple(dict(item) for item in attempts)
        self.raw = raw


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def exclusive_end_date(target_session: date) -> date:
    """Twelve Data's date-only end boundary is exclusive."""
    return target_session + timedelta(days=1)


class RollingWindowThrottle:
    """Allow no more than ``limit`` starts in any rolling time window."""

    def __init__(
        self, *, limit: int = RATE_CREDITS, window_seconds: float = RATE_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("throttle limit and window must be positive")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.monotonic = monotonic
        self.sleep = sleep
        self._starts: deque[float] = deque()

    def acquire(self) -> float:
        while True:
            now = self.monotonic()
            while self._starts and now - self._starts[0] >= self.window_seconds:
                self._starts.popleft()
            if len(self._starts) < self.limit:
                self._starts.append(now)
                return now
            self.sleep(max(0.0, self.window_seconds - (now - self._starts[0])))


@dataclass(frozen=True)
class ProviderCapture:
    raw: bytes
    attempts: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class NormalizedCapture:
    provider_symbol: str
    classification: str
    bars: tuple[dict[str, Any], ...]
    excluded_rows: tuple[dict[str, Any], ...]
    error: str | None = None


class TwelveDataClient:
    """Production Twelve Data client with fixed contract, throttle, and retries."""

    def __init__(
        self, *, api_key: str | None = None, session: requests.Session | None = None,
        throttle: RollingWindowThrottle | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 45.0, max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.api_key = api_key or os.getenv("TWELVEDATA_API_KEY")
        if not self.api_key:
            raise PriceSnapshotError("TWELVEDATA_API_KEY is not configured")
        self.session = session or requests.Session()
        if session is None:
            try:
                import truststore
                truststore.inject_into_ssl()
            except ImportError:
                pass
        self.throttle = throttle or RollingWindowThrottle(monotonic=monotonic, sleep=sleep)
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

    def fetch(self, symbol: str, *, start_date: date, target_session: date) -> ProviderCapture:
        params = {
            "symbol": symbol,
            "interval": INTERVAL,
            "adjust": ADJUSTMENT,
            "start_date": start_date.isoformat(),
            "end_date": exclusive_end_date(target_session).isoformat(),
            "outputsize": 5000,
            "apikey": self.api_key,
        }
        attempts: list[dict[str, Any]] = []
        last_raw: bytes | None = None
        retryable_exceptions = (requests.Timeout, requests.ConnectionError)
        for ordinal in range(1, self.max_attempts + 1):
            self.throttle.acquire()
            requested_at = self.now()
            started = self.monotonic()
            status: int | None = None
            provider_code: int | str | None = None
            provider_message: str | None = None
            try:
                response = self.session.get(
                    TWELVE_DATA_TIME_SERIES, params=params, timeout=self.timeout_seconds,
                )
                status = int(response.status_code)
                last_raw = bytes(response.content)
                try:
                    envelope = json.loads(last_raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    envelope = None
                if isinstance(envelope, dict) and envelope.get("status") == "error":
                    provider_code = envelope.get("code")
                    provider_message = str(envelope.get("message") or "provider error").replace(
                        self.api_key, "[REDACTED]",
                    )[:500]
                attempts.append({
                    "attempt": ordinal,
                    "requested_at_utc": utc_timestamp(requested_at),
                    "http_status": status,
                    "elapsed_seconds": round(self.monotonic() - started, 6),
                    "provider_code": provider_code,
                    "provider_message": provider_message,
                })
                retryable_provider = str(provider_code) in {
                    str(item) for item in RETRYABLE_HTTP_STATUSES
                }
                if status in RETRYABLE_HTTP_STATUSES or retryable_provider:
                    if ordinal < self.max_attempts:
                        self.sleep(float(ordinal))
                        continue
                    raise ProviderRequestError(
                        f"{symbol}: transient Twelve Data failure exhausted retries",
                        attempts=attempts, raw=last_raw,
                    )
                if status < 200 or status >= 300:
                    raise ProviderRequestError(
                        f"{symbol}: Twelve Data request failed HTTP {status}",
                        attempts=attempts, raw=last_raw,
                    )
                if provider_code is not None:
                    raise ProviderRequestError(
                        f"{symbol}: Twelve Data error {provider_code}: {provider_message}",
                        attempts=attempts, raw=last_raw,
                    )
                return ProviderCapture(last_raw, tuple(attempts))
            except retryable_exceptions as exc:
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
                    f"{symbol}: Twelve Data request failed ({type(exc).__name__})",
                    attempts=attempts, raw=last_raw,
                ) from exc
            except requests.RequestException as exc:
                attempts.append({
                    "attempt": ordinal,
                    "requested_at_utc": utc_timestamp(requested_at),
                    "http_status": status,
                    "elapsed_seconds": round(self.monotonic() - started, 6),
                    "exception": type(exc).__name__,
                })
                raise ProviderRequestError(
                    f"{symbol}: Twelve Data request failed ({type(exc).__name__})",
                    attempts=attempts, raw=last_raw,
                ) from exc
        raise AssertionError("unreachable Twelve Data retry state")


def _number(value: object, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"missing or invalid {field}")
    result = float(value)
    if not (result == result and abs(result) != float("inf")):
        raise ValueError(f"non-finite {field}")
    return result


def _is_wbd_artifact(ticker: str, row: Mapping[str, Any]) -> bool:
    if ticker != "WBD" or str(row.get("datetime") or row.get("date") or "") != WBD_STALE_DATE:
        return False
    try:
        prices = [_number(row.get(field), field) for field in ("open", "high", "low", "close")]
        volume = _number(row.get("volume"), "volume")
    except (TypeError, ValueError):
        return False
    return volume == 0 and all(abs(item - WBD_STALE_PRICE) < 1e-9 for item in prices)


def normalize_response(
    ticker: str, raw: bytes, *, target_session: date,
) -> NormalizedCapture:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PriceSnapshotError(f"{ticker}: Twelve Data response is not JSON") from exc
    if not isinstance(payload, dict):
        raise PriceSnapshotError(f"{ticker}: Twelve Data response is not an object")
    if payload.get("status") == "error":
        raise PriceSnapshotError(
            f"{ticker}: Twelve Data error {payload.get('code')}: {payload.get('message')}"
        )
    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise PriceSnapshotError(f"{ticker}: Twelve Data response has no values")
    meta = payload.get("meta")
    returned_symbol = str(meta.get("symbol") if isinstance(meta, dict) else ticker).upper()
    if returned_symbol != ticker:
        return NormalizedCapture(
            returned_symbol, "SYMBOL_MAPPING_REQUIRED", (), (),
            f"provider returned symbol {returned_symbol} for canonical {ticker}",
        )
    bars: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in values:
        if not isinstance(source, dict):
            raise PriceSnapshotError(f"{ticker}: non-object OHLCV record")
        if _is_wbd_artifact(ticker, source):
            excluded.append({
                "date": WBD_STALE_DATE, "open": WBD_STALE_PRICE,
                "high": WBD_STALE_PRICE, "low": WBD_STALE_PRICE,
                "close": WBD_STALE_PRICE, "volume": 0.0,
                "reason": "CORPORATE_ACTION_ARTIFACT_STALE_CARRY_FORWARD",
            })
            continue
        day = str(source.get("datetime") or source.get("date") or "")
        try:
            parsed_day = date.fromisoformat(day)
            if parsed_day > target_session:
                raise ValueError("bar exceeds requested target session")
            if day in seen:
                raise ValueError("duplicate date")
            seen.add(day)
            open_value, high, low, close = (
                _number(source.get(field), field) for field in ("open", "high", "low", "close")
            )
            volume = _number(source.get("volume"), "volume")
            if min(open_value, high, low, close) <= 0 or volume < 0:
                raise ValueError("non-positive OHLC or negative volume")
            if high < max(open_value, low, close) or low > min(open_value, high, close):
                raise ValueError("inconsistent OHLC range")
        except (TypeError, ValueError) as exc:
            raise PriceSnapshotError(f"{ticker}: invalid OHLCV record for {day or '<missing>'}: {exc}") from exc
        bars.append({
            "date": day, "open": open_value, "high": high,
            "low": low, "close": close, "volume": volume,
        })
    bars.sort(key=lambda item: item["date"])
    if not bars:
        raise PriceSnapshotError(f"{ticker}: no valid OHLCV records")
    latest = bars[-1]["date"]
    if ticker == "WBD" and latest == WBD_LAST_NORMAL_SESSION:
        classification = "CORPORATE_ACTION_NO_LONGER_TRADING"
    elif latest == target_session.isoformat():
        classification = "ACTIVE_COMPLETE"
    else:
        classification = "ACTIVE_MISSING_LATEST"
    return NormalizedCapture(
        returned_symbol, classification, tuple(bars), tuple(excluded), None,
    )


def snapshot_hash(manifest: Mapping[str, Any]) -> str:
    value = {key: item for key, item in manifest.items() if key != "snapshot_sha256"}
    return canonical_sha256(value)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def acquire_snapshot(
    *, target_session: date, output_root: str | Path, run_id: str,
    client: TwelveDataClient | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    root = Path(output_root)
    if root.exists() and any(root.iterdir()):
        raise PriceSnapshotError(f"snapshot output must be new and empty: {root}")
    raw_root, normalized_root = root / "raw", root / "normalized"
    raw_root.mkdir(parents=True, exist_ok=True)
    normalized_root.mkdir(parents=True, exist_ok=True)
    registry = load_registry(registry_path)
    records = registry.supported
    provider = client or TwelveDataClient()
    acquired_at = now()
    history_start = target_session - timedelta(days=HISTORY_CALENDAR_DAYS)
    entries: list[dict[str, Any]] = []
    for issuer in records:
        ticker = issuer.ticker
        attempts: Sequence[Mapping[str, Any]] = ()
        raw: bytes | None = None
        error: str | None = None
        normalized = NormalizedCapture(ticker, "PROVIDER_FAILURE", (), (), None)
        try:
            capture = provider.fetch(
                ticker, start_date=history_start, target_session=target_session,
            )
            raw, attempts = capture.raw, capture.attempts
            normalized = normalize_response(ticker, raw, target_session=target_session)
        except ProviderRequestError as exc:
            raw, attempts, error = exc.raw, exc.attempts, str(exc)
        except (PriceSnapshotError, TypeError, ValueError) as exc:
            error = str(exc)
            normalized = NormalizedCapture(ticker, "INVALID_DATA", (), (), error)
        configured_secret = getattr(provider, "api_key", None)
        if raw is not None and isinstance(configured_secret, str) \
                and configured_secret.encode("utf-8") in raw:
            raw = None
            error = f"{ticker}: provider response contained configured secret; raw not persisted"
            normalized = NormalizedCapture(ticker, "INVALID_DATA", (), (), error)
        if raw is not None:
            _atomic_write(raw_root / f"{ticker}.json", raw)
        bars_value = list(normalized.bars)
        bars_bytes = canonical_bytes(bars_value)
        _atomic_write(normalized_root / f"{ticker}.json", bars_bytes)
        entries.append({
            "canonical_ticker": ticker,
            "provider_symbol": normalized.provider_symbol,
            "classification": normalized.classification,
            "first_date": bars_value[0]["date"] if bars_value else None,
            "last_valid_date": bars_value[-1]["date"] if bars_value else None,
            "bar_count": len(bars_value),
            "content_sha256": hashlib.sha256(bars_bytes).hexdigest(),
            "raw_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "excluded_rows": list(normalized.excluded_rows),
            "attempts": [dict(item) for item in attempts],
            "retry_count": max(0, len(attempts) - 1),
            "error": error or normalized.error,
        })
    completed_at = now()
    counts = {
        state: sum(item["classification"] == state for item in entries)
        for state in (
            "ACTIVE_COMPLETE", "ACTIVE_MISSING_LATEST",
            "CORPORATE_ACTION_NO_LONGER_TRADING", "SYMBOL_MAPPING_REQUIRED",
            "PROVIDER_FAILURE", "INVALID_DATA",
        )
    }
    ready = len(entries) == len(records) and all(
        item["classification"] in ALLOWED_READY_STATES for item in entries
    )
    manifest: dict[str, Any] = {
        "schema_name": SNAPSHOT_SCHEMA,
        "schema_version": SNAPSHOT_VERSION,
        "snapshot_id": f"TWELVEDATA_{target_session.isoformat()}_RUN_{run_id}",
        "provider": PRICE_PROVIDER,
        "interval": INTERVAL,
        "adjust": ADJUSTMENT,
        "target_market_session": target_session.isoformat(),
        "request_start_date": history_start.isoformat(),
        "request_end_date_exclusive": exclusive_end_date(target_session).isoformat(),
        "acquisition_started_at_utc": utc_timestamp(acquired_at),
        "acquisition_completed_at_utc": utc_timestamp(completed_at),
        "canonical_ticker_count": len(records),
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
        "tickers": entries,
    }
    manifest["snapshot_sha256"] = snapshot_hash(manifest)
    _atomic_write(root / "snapshot_manifest.json", canonical_bytes(manifest))
    return manifest


def verify_snapshot(
    root: str | Path, *, require_ready: bool = True, require_raw: bool = False,
) -> dict[str, Any]:
    base = Path(root)
    try:
        manifest = json.loads((base / "snapshot_manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PriceSnapshotError("invalid price snapshot manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_name") != SNAPSHOT_SCHEMA \
            or manifest.get("schema_version") != SNAPSHOT_VERSION:
        raise PriceSnapshotError("unsupported price snapshot schema")
    if manifest.get("provider") != PRICE_PROVIDER or manifest.get("interval") != INTERVAL \
            or manifest.get("adjust") != ADJUSTMENT:
        raise PriceSnapshotError("price snapshot provider contract mismatch")
    expected_hash = manifest.get("snapshot_sha256")
    if not isinstance(expected_hash, str) or snapshot_hash(manifest) != expected_hash:
        raise PriceSnapshotError("price snapshot manifest hash mismatch")
    entries = manifest.get("tickers")
    if not isinstance(entries, list) or len(entries) != manifest.get("canonical_ticker_count"):
        raise PriceSnapshotError("price snapshot ticker count mismatch")
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("canonical_ticker"), str):
            raise PriceSnapshotError("price snapshot has malformed ticker metadata")
        ticker = item["canonical_ticker"]
        if ticker in seen:
            raise PriceSnapshotError(f"duplicate price snapshot ticker: {ticker}")
        seen.add(ticker)
        path = base / "normalized" / f"{ticker}.json"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise PriceSnapshotError(f"missing normalized snapshot bars: {ticker}") from exc
        if hashlib.sha256(raw).hexdigest() != item.get("content_sha256"):
            raise PriceSnapshotError(f"normalized snapshot hash mismatch: {ticker}")
        try:
            bars = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PriceSnapshotError(f"invalid normalized snapshot bars: {ticker}") from exc
        if not isinstance(bars, list) or len(bars) != item.get("bar_count"):
            raise PriceSnapshotError(f"normalized snapshot bar count mismatch: {ticker}")
        if require_raw:
            raw_path = base / "raw" / f"{ticker}.json"
            if not raw_path.is_file() or hashlib.sha256(raw_path.read_bytes()).hexdigest() != item.get("raw_sha256"):
                raise PriceSnapshotError(f"raw snapshot hash mismatch: {ticker}")
    gate = manifest.get("coverage_gate")
    if not isinstance(gate, dict) or type(gate.get("ready")) is not bool:
        raise PriceSnapshotError("price snapshot lacks coverage gate")
    calculated_ready = all(
        item.get("classification") in ALLOWED_READY_STATES for item in entries
    )
    if gate["ready"] != calculated_ready:
        raise PriceSnapshotError("price snapshot coverage gate is inconsistent")
    if require_ready and not gate["ready"]:
        blockers = ", ".join(str(item) for item in gate.get("blocking_tickers", []))
        raise PriceSnapshotError(f"price snapshot is NOT READY: {blockers or 'unknown blockers'}")
    return manifest


def verify_canonical_universe(
    manifest: Mapping[str, Any], *, registry_path: str | Path = DEFAULT_REGISTRY,
) -> None:
    expected = sorted(item.ticker for item in load_registry(registry_path).supported)
    entries = manifest.get("tickers")
    actual = sorted(
        str(item.get("canonical_ticker") or "")
        for item in entries if isinstance(item, dict)
    ) if isinstance(entries, list) else []
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        detail = (
            f"expected={len(expected)} actual={len(actual)} "
            f"missing={','.join(missing[:10]) or '-'} "
            f"unexpected={','.join(unexpected[:10]) or '-'}"
        )
        raise PriceSnapshotError(f"price snapshot canonical universe mismatch: {detail}")


class ValidatedPriceSnapshot:
    """Read-only access to a hash-verified, coverage-gated snapshot."""

    def __init__(
        self, root: str | Path, *, require_ready: bool = True,
        enforce_canonical_universe: bool = True,
        registry_path: str | Path = DEFAULT_REGISTRY,
    ) -> None:
        self.root = Path(root).resolve()
        self.manifest = verify_snapshot(self.root, require_ready=require_ready)
        if enforce_canonical_universe:
            verify_canonical_universe(self.manifest, registry_path=registry_path)
        self._entries = {
            str(item["canonical_ticker"]): item for item in self.manifest["tickers"]
        }

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    @property
    def target_session(self) -> str:
        return str(self.manifest["target_market_session"])

    @property
    def snapshot_sha256(self) -> str:
        return str(self.manifest["snapshot_sha256"])

    @property
    def acquisition_completed_at(self) -> str:
        return str(self.manifest["acquisition_completed_at_utc"])

    @property
    def normalized_root(self) -> Path:
        return self.root / "normalized"

    def entry(self, ticker: str) -> Mapping[str, Any]:
        normalized = ticker.strip().upper()
        try:
            return self._entries[normalized]
        except KeyError as exc:
            raise PriceSnapshotError(f"ticker absent from price snapshot: {normalized}") from exc

    def normalized_bytes(self, ticker: str) -> bytes:
        item = self.entry(ticker)
        raw = (self.normalized_root / f"{ticker.strip().upper()}.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["content_sha256"]:
            raise PriceSnapshotError(f"normalized snapshot hash mismatch: {ticker}")
        return raw

    def bars(self, ticker: str) -> list[dict[str, Any]]:
        value = json.loads(self.normalized_bytes(ticker))
        if not isinstance(value, list):
            raise PriceSnapshotError(f"normalized snapshot bars are invalid: {ticker}")
        return value

    def provenance(self) -> dict[str, str]:
        return {
            "price_provider": PRICE_PROVIDER,
            "price_snapshot_id": self.snapshot_id,
            "price_snapshot_session": self.target_session,
            "price_snapshot_sha256": self.snapshot_sha256,
        }
