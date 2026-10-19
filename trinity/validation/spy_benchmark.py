"""Immutable, benchmark-only SPY acquisition for Live Validation V1."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol

import exchange_calendars

from trinity.twelvedata_prices import (
    ADJUSTMENT, INTERVAL, PRICE_PROVIDER, PriceSnapshotError, ProviderCapture, TwelveDataClient,
    canonical_bytes, exclusive_end_date, normalize_response, utc_timestamp,
)

from .outcomes import BenchmarkSeries


SCHEMA_NAME = "trinity.validation.spy-benchmark-snapshot"
SCHEMA_VERSION = "1"
SOURCE_METHOD_VERSION = "TRINITY_SPY_BENCHMARK_ACQUISITION_V1"
SYMBOL = "SPY"
RETURN_KIND = "PRICE_RETURN"
UNIVERSE_ROLE = "BENCHMARK_ONLY"
CALENDAR_NAME = "XNYS"
HISTORY_CALENDAR_DAYS = 450
BENCHMARK_FINALITY_GRACE_MINUTES = 20
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SENSITIVE_KEYS = frozenset({
    "apikey", "api_key", "authorization", "token", "access_token", "client_secret",
    "password", "cookie", "set_cookie", "x_api_key", "headers", "url", "uri", "query",
    "params",
})
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|access[_-]?token|client[_-]?secret|password|cookie)\s*[=:]"
)
_PROVIDER_ATTEMPT_FIELDS = (
    "attempt", "requested_at_utc", "http_status", "elapsed_seconds", "provider_code",
    "provider_message", "exception", "api_credits_used", "api_credits_left",
    "api_credits_request",
)


class BenchmarkSnapshotError(RuntimeError):
    """The benchmark acquisition or immutable snapshot violates V1."""


class SpyProvider(Protocol):
    api_key: str | None

    def fetch(
        self, symbol: str, *, start_date: date, target_session: date,
        remaining_tickers: int | None = None,
    ) -> ProviderCapture: ...


def benchmark_snapshot_id(target_session: date, run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise BenchmarkSnapshotError("unsafe benchmark run ID")
    return f"SPY_TWELVEDATA_{target_session.isoformat()}_RUN_{run_id}"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _snapshot_hash(manifest: Mapping[str, Any]) -> str:
    value = {key: item for key, item in manifest.items()
             if key != "benchmark_snapshot_sha256"}
    return _sha(canonical_bytes(value))


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _assert_no_sensitive_provider_payload(raw: bytes, secret: str | None) -> Any:
    if secret and secret.encode("utf-8") in raw:
        raise BenchmarkSnapshotError("provider payload contained configured secret")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkSnapshotError("provider payload is not JSON") from exc

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
                if normalized_key in _SENSITIVE_KEYS \
                        or normalized_key.endswith(("_token", "_secret", "_password", "_cookie")) \
                        or "authorization" in normalized_key:
                    raise BenchmarkSnapshotError("provider payload contains sensitive field")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and _SENSITIVE_VALUE.search(item):
            raise BenchmarkSnapshotError("provider payload contains sensitive value")

    visit(value)
    return value


def _sanitize_provider_attempts(
    attempts: object, secret: str | None,
) -> list[dict[str, Any]]:
    if not isinstance(attempts, (list, tuple)):
        raise BenchmarkSnapshotError("provider attempts are malformed")
    _assert_no_sensitive_provider_payload(
        canonical_bytes({"attempts": attempts}), secret,
    )
    sanitized: list[dict[str, Any]] = []
    for item in attempts:
        if not isinstance(item, Mapping) or "attempt" not in item:
            raise BenchmarkSnapshotError("provider attempt is malformed")
        sanitized.append({key: item[key] for key in _PROVIDER_ATTEMPT_FIELDS if key in item})
    return sanitized


def _assert_provider_metadata(raw: bytes) -> None:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkSnapshotError("provider payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise BenchmarkSnapshotError("provider payload is not an object")
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise BenchmarkSnapshotError("provider metadata is missing or invalid")
    if meta.get("symbol") != SYMBOL:
        raise BenchmarkSnapshotError("provider metadata symbol mismatch")
    if meta.get("interval") != INTERVAL:
        raise BenchmarkSnapshotError("provider metadata interval mismatch")


def _xnys_session_times(session: date) -> tuple[datetime, datetime]:
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME, start=session - timedelta(days=1), end=session + timedelta(days=1),
    )
    try:
        row = calendar.schedule.loc[session.isoformat()]
    except KeyError as exc:
        raise BenchmarkSnapshotError(f"target is not an XNYS session: {session}") from exc
    return (
        row["open"].to_pydatetime().astimezone(timezone.utc),
        row["close"].to_pydatetime().astimezone(timezone.utc),
    )


def _assert_target_finality(
    target_session: date, acquisition_started_at: datetime,
) -> tuple[datetime, datetime, datetime]:
    if acquisition_started_at.tzinfo is None:
        raise BenchmarkSnapshotError("acquisition start must be timezone-aware")
    opened, closed = _xnys_session_times(target_session)
    eligible_at = closed + timedelta(minutes=BENCHMARK_FINALITY_GRACE_MINUTES)
    if acquisition_started_at.astimezone(timezone.utc) < eligible_at:
        raise BenchmarkSnapshotError(
            "benchmark target session is not final at acquisition start"
        )
    return opened, closed, eligible_at


def _xnys_opens(sessions: list[str], target_session: date) -> dict[str, str]:
    if not sessions:
        raise BenchmarkSnapshotError("benchmark contains no sessions")
    parsed = [date.fromisoformat(item) for item in sessions]
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME, start=min(parsed), end=target_session,
    )
    expected_sessions = [item.date().isoformat() for item in calendar.sessions]
    if sessions != expected_sessions:
        raise BenchmarkSnapshotError("benchmark XNYS session coverage mismatch")
    result: dict[str, str] = {}
    for session in parsed:
        try:
            row = calendar.schedule.loc[session.isoformat()]
        except KeyError as exc:
            raise BenchmarkSnapshotError(f"SPY bar is not an XNYS session: {session}") from exc
        opened = row["open"].to_pydatetime().astimezone(timezone.utc)
        result[session.isoformat()] = utc_timestamp(opened)
    return result


def _normalized_bars(raw: bytes, target_session: date) -> list[dict[str, Any]]:
    _assert_provider_metadata(raw)
    capture = normalize_response(SYMBOL, raw, target_session=target_session)
    if capture.classification != "ACTIVE_COMPLETE":
        raise BenchmarkSnapshotError(
            f"benchmark target session missing: {capture.classification}"
        )
    bars = [dict(item) for item in capture.bars]
    opens = _xnys_opens([str(item["date"]) for item in bars], target_session)
    return [{**item, "tradable_at": opens[str(item["date"])]} for item in bars]


def acquire_spy_benchmark(
    *, target_session: date, run_id: str, output_root: str | Path,
    client: SpyProvider | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Explicit acquisition entrypoint; callers inject a fake provider in tests."""

    root = Path(output_root)
    if root.exists() and any(root.iterdir()):
        raise BenchmarkSnapshotError(f"benchmark output must be new and empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    snapshot_id = benchmark_snapshot_id(target_session, run_id)
    started_at = now()
    target_open, target_close, finality_eligible_at = _assert_target_finality(
        target_session, started_at,
    )
    provider = client or TwelveDataClient()
    history_start = target_session - timedelta(days=HISTORY_CALENDAR_DAYS)
    capture = provider.fetch(
        SYMBOL, start_date=history_start, target_session=target_session, remaining_tickers=1,
    )
    secret = getattr(provider, "api_key", None)
    _assert_no_sensitive_provider_payload(capture.raw, secret if isinstance(secret, str) else None)
    provider_attempts = _sanitize_provider_attempts(
        capture.attempts, secret if isinstance(secret, str) else None,
    )
    try:
        bars = _normalized_bars(capture.raw, target_session)
        ready, blocking_reason = True, None
    except (BenchmarkSnapshotError, PriceSnapshotError) as exc:
        # Preserve sanitized provider evidence and an auditable NOT READY manifest,
        # but never create READY or silently use an earlier session.
        bars, ready, blocking_reason = [], False, str(exc)
    raw_bytes = capture.raw
    normalized_bytes = canonical_bytes(bars)
    _atomic_write(root / "raw" / "SPY.json", raw_bytes)
    _atomic_write(root / "normalized" / "SPY.json", normalized_bytes)
    completed_at = now()
    manifest: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "symbol": SYMBOL,
        "universe_role": UNIVERSE_ROLE,
        "provider": PRICE_PROVIDER,
        "interval": INTERVAL,
        "adjustment": ADJUSTMENT,
        "return_kind": RETURN_KIND,
        "target_market_session": target_session.isoformat(),
        "request_start_date": history_start.isoformat(),
        "request_end_date_exclusive": exclusive_end_date(target_session).isoformat(),
        "acquisition_started_at_utc": utc_timestamp(started_at),
        "acquisition_completed_at_utc": utc_timestamp(completed_at),
        "target_session_open_utc": utc_timestamp(target_open),
        "target_session_close_utc": utc_timestamp(target_close),
        "finality_grace_minutes": BENCHMARK_FINALITY_GRACE_MINUTES,
        "finality_eligible_at_utc": utc_timestamp(finality_eligible_at),
        "raw_sha256": _sha(raw_bytes),
        "normalized_sha256": _sha(normalized_bytes),
        "first_session": bars[0]["date"] if bars else None,
        "last_session": bars[-1]["date"] if bars else None,
        "bar_count": len(bars),
        "coverage_gate": {"ready": ready, "blocking_reason": blocking_reason},
        "benchmark_snapshot_id": snapshot_id,
        "source_run_id": run_id,
        "source_method_version": SOURCE_METHOD_VERSION,
        "session_calendar": CALENDAR_NAME,
        "session_calendar_version": importlib.metadata.version("exchange-calendars"),
        "provider_attempts": provider_attempts,
    }
    manifest["benchmark_snapshot_sha256"] = _snapshot_hash(manifest)
    manifest_bytes = canonical_bytes(manifest)
    _atomic_write(root / "benchmark_manifest.json", manifest_bytes)
    if not ready:
        raise BenchmarkSnapshotError(blocking_reason or "benchmark is NOT READY")
    marker = {
        "schema_name": "trinity.validation.spy-benchmark-ready",
        "schema_version": "1",
        "benchmark_snapshot_id": snapshot_id,
        "benchmark_snapshot_sha256": manifest["benchmark_snapshot_sha256"],
        "manifest_sha256": _sha(manifest_bytes),
    }
    _atomic_write(root / "READY", canonical_bytes(marker))
    return verify_spy_benchmark_snapshot(root, expected_target_session=target_session)


def _load_json(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        raw = path.read_bytes()
        return json.loads(raw), raw
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkSnapshotError(f"invalid or missing {label}") from exc


def verify_spy_benchmark_snapshot(
    root: str | Path, *, expected_target_session: date | str | None = None,
) -> dict[str, Any]:
    base = Path(root).resolve()
    manifest, manifest_bytes = _load_json(base / "benchmark_manifest.json", "benchmark manifest")
    marker, _ = _load_json(base / "READY", "READY marker")
    if not isinstance(manifest, dict) or (
        manifest.get("schema_name"), manifest.get("schema_version")
    ) != (SCHEMA_NAME, SCHEMA_VERSION):
        raise BenchmarkSnapshotError("unsupported benchmark manifest schema")
    expected_contract = {
        "symbol": SYMBOL, "universe_role": UNIVERSE_ROLE, "provider": PRICE_PROVIDER,
        "interval": INTERVAL, "adjustment": ADJUSTMENT, "return_kind": RETURN_KIND,
        "source_method_version": SOURCE_METHOD_VERSION, "session_calendar": CALENDAR_NAME,
    }
    if any(manifest.get(key) != value for key, value in expected_contract.items()):
        raise BenchmarkSnapshotError("benchmark contract mismatch")
    target = str(manifest.get("target_market_session") or "")
    if expected_target_session is not None and target != str(expected_target_session):
        raise BenchmarkSnapshotError("TRINITY and SPY target sessions differ")
    try:
        target_date = date.fromisoformat(target)
    except ValueError as exc:
        raise BenchmarkSnapshotError("invalid benchmark target session") from exc
    if manifest.get("request_start_date") != (
        target_date - timedelta(days=HISTORY_CALENDAR_DAYS)
    ).isoformat() or manifest.get("request_end_date_exclusive") != exclusive_end_date(
        target_date
    ).isoformat():
        raise BenchmarkSnapshotError("benchmark request range mismatch")
    parsed_times: list[datetime] = []
    for field in ("acquisition_started_at_utc", "acquisition_completed_at_utc"):
        try:
            timestamp = datetime.fromisoformat(str(manifest.get(field) or "").replace("Z", "+00:00"))
        except ValueError as exc:
            raise BenchmarkSnapshotError(f"invalid {field}") from exc
        if timestamp.tzinfo is None:
            raise BenchmarkSnapshotError(f"invalid {field}")
        parsed_times.append(timestamp)
    if parsed_times[1] < parsed_times[0]:
        raise BenchmarkSnapshotError("benchmark acquisition timestamps are reversed")
    target_open, target_close = _xnys_session_times(target_date)
    finality_eligible_at = target_close + timedelta(minutes=BENCHMARK_FINALITY_GRACE_MINUTES)
    if manifest.get("target_session_open_utc") != utc_timestamp(target_open) \
            or manifest.get("target_session_close_utc") != utc_timestamp(target_close) \
            or manifest.get("finality_grace_minutes") != BENCHMARK_FINALITY_GRACE_MINUTES \
            or manifest.get("finality_eligible_at_utc") != utc_timestamp(finality_eligible_at) \
            or parsed_times[0].astimezone(timezone.utc) < finality_eligible_at:
        raise BenchmarkSnapshotError("benchmark target session finality mismatch")
    if not isinstance(manifest.get("session_calendar_version"), str) \
            or not manifest.get("session_calendar_version") \
            or not isinstance(manifest.get("provider_attempts"), list):
        raise BenchmarkSnapshotError("benchmark provenance is incomplete")
    try:
        sanitized_attempts = _sanitize_provider_attempts(manifest["provider_attempts"], None)
    except (BenchmarkSnapshotError, TypeError, ValueError) as exc:
        raise BenchmarkSnapshotError("benchmark provider provenance is invalid") from exc
    if canonical_bytes(sanitized_attempts) != canonical_bytes(manifest["provider_attempts"]):
        raise BenchmarkSnapshotError("benchmark provider provenance is not sanitized")
    expected_id = benchmark_snapshot_id(target_date, str(manifest.get("source_run_id") or ""))
    if manifest.get("benchmark_snapshot_id") != expected_id:
        raise BenchmarkSnapshotError("benchmark snapshot ID mismatch")
    if _snapshot_hash(manifest) != manifest.get("benchmark_snapshot_sha256"):
        raise BenchmarkSnapshotError("benchmark snapshot hash mismatch")
    if not isinstance(marker, dict) or marker.get("benchmark_snapshot_id") != expected_id \
            or marker.get("benchmark_snapshot_sha256") != manifest.get("benchmark_snapshot_sha256") \
            or marker.get("manifest_sha256") != _sha(manifest_bytes):
        raise BenchmarkSnapshotError("READY marker or manifest hash mismatch")
    try:
        raw = (base / "raw" / "SPY.json").read_bytes()
        normalized = (base / "normalized" / "SPY.json").read_bytes()
    except OSError as exc:
        raise BenchmarkSnapshotError("benchmark raw or normalized artifact is missing") from exc
    if _sha(raw) != manifest.get("raw_sha256"):
        raise BenchmarkSnapshotError("raw benchmark hash mismatch")
    if _sha(normalized) != manifest.get("normalized_sha256"):
        raise BenchmarkSnapshotError("normalized benchmark hash mismatch")
    _assert_no_sensitive_provider_payload(raw, None)
    try:
        bars = json.loads(normalized)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkSnapshotError("normalized benchmark is not JSON") from exc
    if not isinstance(bars, list) or len(bars) != manifest.get("bar_count") or not bars:
        raise BenchmarkSnapshotError("normalized benchmark bar count mismatch")
    dates = [str(item.get("date") or "") for item in bars if isinstance(item, dict)]
    if len(dates) != len(bars) or dates != sorted(dates) or len(set(dates)) != len(dates):
        raise BenchmarkSnapshotError("benchmark sessions are unordered or duplicated")
    # Re-normalize immutable raw provider bytes and derive authoritative XNYS opens.
    expected_bars = _normalized_bars(raw, target_date)
    if canonical_bytes(bars) != canonical_bytes(expected_bars):
        raise BenchmarkSnapshotError("normalized benchmark content is not reproducible")
    if manifest.get("first_session") != dates[0] or manifest.get("last_session") != dates[-1] \
            or dates[-1] != target:
        raise BenchmarkSnapshotError("benchmark coverage does not reach target session")
    gate = manifest.get("coverage_gate")
    if not isinstance(gate, dict) or gate.get("ready") is not True \
            or gate.get("blocking_reason") is not None:
        raise BenchmarkSnapshotError("benchmark coverage gate is NOT READY")
    expected_files = {
        "READY", "benchmark_manifest.json", "raw/SPY.json", "normalized/SPY.json",
    }
    actual_files = {item.relative_to(base).as_posix() for item in base.rglob("*") if item.is_file()}
    if actual_files != expected_files:
        raise BenchmarkSnapshotError("benchmark snapshot contains unexpected files")
    return manifest


def load_spy_benchmark_snapshot(
    root: str | Path, *, trinity_target_session: date | str,
) -> BenchmarkSeries:
    manifest = verify_spy_benchmark_snapshot(
        root, expected_target_session=trinity_target_session,
    )
    bars = json.loads((Path(root) / "normalized" / "SPY.json").read_bytes())
    return BenchmarkSeries(
        symbol=SYMBOL, return_kind=RETURN_KIND, provider=PRICE_PROVIDER,
        adjustment=ADJUSTMENT, interval=INTERVAL,
        snapshot_id=str(manifest["benchmark_snapshot_id"]),
        snapshot_sha256=str(manifest["benchmark_snapshot_sha256"]),
        coverage_start_session=str(manifest["first_session"]),
        coverage_end_session=str(manifest["last_session"]), bars=tuple(bars),
    )
