"""Outcome-free H2 V2.1 Phase 1 input construction.

This module deliberately has no dependency on ``trinity.outcomes`` or
``trinity.experimental_dataset``.  It acquires and transforms only SEC release
metadata/text and pre-event price history.  Event price readers stop at the
reference session before decoding any later OHLCV row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time as time_module
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import requests

from trinity.earnings_pairing import pair_earnings_release
from trinity.h2_novelty import H2InputError, calculate_h2_novelty, normalize_text
from trinity.pre_event_covariates import compute_pre_event_covariates
from trinity.twelvedata_prices import (
    TwelveDataClient,
    acquire_snapshot,
    canonical_bytes,
    normalize_response,
)
from trinity.usa_documents import _plain_text, _submission_documents
from trinity.usa_issuer_registry import DEFAULT_REGISTRY, load_registry


SPEC_ID = "H2_V2_1_BUILD_INPUTS_V1"
SEC_USER_AGENT = "Gioele Pastorin giopastorin@gmail.com"
SEC_REQUESTS_PER_SECOND = 8
SEC_TIMEOUT_SECONDS = 45.0
SEC_MAX_ATTEMPTS = 3
SEC_HISTORY_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
DEV_RELEASE_START = datetime(2026, 9, 21, tzinfo=timezone.utc)
DEV_REFERENCE_END = date(2026, 10, 20)
PROSP_REFERENCE_START = date(2026, 10, 21)
PROSP_REFERENCE_END = date(2026, 11, 17)
PRICE_TARGET_SESSION = date(2026, 11, 17)
PRICE_HISTORY_START = date(2025, 8, 24)
PRICE_RUN_ID = "H2V21_PHASE1_20261118_A1"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

PAIRING_EXACT = "EXACT"
PAIRING_FALLBACK = "FALLBACK"
PAIRING_NO_MATCH = "NO_MATCH"
PAIRING_AMBIGUOUS = "AMBIGUOUS"

EXCLUSION_REASONS = frozenset({
    "NO_EX99",
    "TIMING_INTRADAY",
    "TIMING_UNKNOWN",
    "OUTSIDE_SAMPLE_WINDOW",
    "FISCAL_PERIOD_UNKNOWN",
    "PAIRING_FALLBACK",
    "PAIRING_NO_MATCH",
    "PAIRING_AMBIGUOUS",
    "TEXT_UNAVAILABLE",
    "H2_UNAVAILABLE",
    "PRICE_COVERAGE",
    "SIGMA_PRE_60_MISSING",
    "DUPLICATE_SHARE_CLASS",
    "AMBIGUOUS_TICKER_MAPPING",
})

INPUT_COLUMNS = (
    "sample", "release_period", "row_status", "exclusion_reasons",
    "current_release_id", "ticker", "release_at", "acceptanceDateTime",
    "event_timing", "timing_ambiguous", "reference_resolution",
    "reference_session_date", "previous_release_id", "pairing_quality",
    "current_text_sha256", "previous_text_sha256", "h2_similarity",
    "h2_novelty", "current_word_count", "previous_word_count",
    "sigma_pre_60", "dollar_volume_pre_60", "fiscal_year",
    "fiscal_quarter", "fiscal_period_label", "period_type",
    "periodic_source_accession", "periodic_source_form",
    "periodic_source_report_date", "periodic_source_acceptance_at",
    "pairing_status", "pairing_method", "fiscal_continuity", "h2_method",
    "sigma_status", "dollar_volume_status", "price_window_start",
    "price_window_sha256", "selected_exhibit_type", "selected_exhibit_filename",
    "selected_exhibit_ordinal", "selected_exhibit_raw_sha256",
    "price_provider", "price_snapshot_id", "price_snapshot_sha256",
    "price_ensemble_sha256", "universe_sha256", "build_spec_id",
    "usa_documents_sha256", "h2_novelty_component_sha256",
    "earnings_pairing_component_sha256", "pre_event_covariates_component_sha256",
)

EXCLUSION_COLUMNS = (
    "sample", "release_period", "ticker", "current_release_id", "release_at",
    "event_timing", "reference_session_date", "reason", "detail",
)


class H2Phase1Error(RuntimeError):
    """The Phase 1 contract cannot be satisfied."""


class PriceCoverageError(H2Phase1Error):
    """A bounded pre-event price window is unavailable."""


@dataclass(frozen=True)
class TimingResolution:
    timing: str
    local_date: date | None
    reference_session: date | None
    reference_resolution: str
    ambiguous: bool


@dataclass(frozen=True)
class SelectedExhibit:
    document_type: str
    filename: str
    content: bytes
    ordinal: int


@dataclass(frozen=True)
class FiscalPeriod:
    fiscal_year: int | None
    fiscal_quarter: int | None
    period_type: str
    label: str | None
    source_accession: str | None
    source_form: str | None
    source_report_date: str | None
    source_acceptance_at: str | None


@dataclass(frozen=True)
class ShareClassPlan:
    cik: str
    selected_sec_ticker: str | None
    selected_ticker: str | None
    issuer: Mapping[str, object]
    sec_tickers: tuple[str, ...]
    nonselected_supported_tickers: tuple[str, ...]
    resolution_method: str
    resolution_reason: str | None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_bytes(value: object, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def parse_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def utc_text(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("UTC serialization requires an aware datetime")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def xnys_calendar() -> Any:
    return xcals.get_calendar("XNYS")


def _calendar_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def is_session(calendar: Any, day: date) -> bool:
    return bool(calendar.is_session(day.isoformat()))


def previous_session(calendar: Any, day: date) -> date:
    if is_session(calendar, day):
        return _calendar_date(calendar.previous_session(day.isoformat()))
    return _calendar_date(calendar.date_to_session(day.isoformat(), direction="previous"))


def classify_and_resolve(
    acceptance_datetime: object, *, calendar: Any | None = None,
) -> TimingResolution:
    parsed = parse_utc_datetime(acceptance_datetime)
    if parsed is None:
        return TimingResolution("UNKNOWN", None, None, "AMBIGUOUS", True)
    cal = calendar or xnys_calendar()
    local = parsed.astimezone(NY)
    local_day = local.date()
    if not is_session(cal, local_day):
        return TimingResolution(
            "PRE_MARKET", local_day, previous_session(cal, local_day),
            "LAST_SESSION_BEFORE_NON_TRADING_EVENT", False,
        )
    local_time = local.timetz().replace(tzinfo=None)
    if local_time < time(9, 30):
        return TimingResolution(
            "PRE_MARKET", local_day, previous_session(cal, local_day),
            "PREVIOUS_SESSION_CLOSE", False,
        )
    if local_time >= time(16, 0):
        return TimingResolution(
            "AFTER_MARKET", local_day, local_day, "EVENT_SESSION_CLOSE", False,
        )
    return TimingResolution("INTRADAY", local_day, None, "AMBIGUOUS", True)


def descriptive_release_period(local_day: date | None) -> str:
    if local_day is None:
        return "OTHER"
    if date(2026, 9, 21) <= local_day <= date(2026, 10, 20):
        return "DEV_PERIOD"
    if local_day >= date(2026, 10, 21):
        return "PROSP_PERIOD"
    return "OTHER"


def assign_sample(release_at: datetime, resolution: TimingResolution) -> str:
    reference = resolution.reference_session
    if resolution.ambiguous or reference is None:
        return "UNASSIGNED"
    if release_at >= DEV_RELEASE_START and reference <= DEV_REFERENCE_END:
        return "DEV"
    if PROSP_REFERENCE_START <= reference <= PROSP_REFERENCE_END:
        return "PROSP_BATCH1"
    return "OUTSIDE"


def has_item_202(items: object) -> bool:
    return isinstance(items, str) and "2.02" in {
        token.strip() for token in items.split(",")
    }


def select_ex99(submission: bytes) -> SelectedExhibit | None:
    documents = _submission_documents(submission)
    candidates = [
        SelectedExhibit(doc_type.strip().upper(), filename, content, ordinal)
        for ordinal, (doc_type, filename, content) in enumerate(documents, start=1)
        if doc_type.strip().upper().startswith("EX-99")
    ]
    preferred = next(
        (candidate for candidate in candidates if candidate.document_type == "EX-99.1"),
        None,
    )
    return preferred or (candidates[0] if candidates else None)


def normalized_exhibit_text(content: bytes) -> str:
    return normalize_text(_plain_text(content))


def companyfacts_fiscal_tuple(
    companyfacts: Mapping[str, object], *, accession: str, report_date: str,
) -> tuple[int, str, str] | None:
    candidates: set[tuple[int, str, str]] = set()
    facts = companyfacts.get("facts")
    if not isinstance(facts, Mapping):
        return None
    for taxonomy in facts.values():
        if not isinstance(taxonomy, Mapping):
            continue
        for fact in taxonomy.values():
            if not isinstance(fact, Mapping):
                continue
            units = fact.get("units")
            if not isinstance(units, Mapping):
                continue
            for observations in units.values():
                if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
                    continue
                for observation in observations:
                    if not isinstance(observation, Mapping):
                        continue
                    fy = observation.get("fy")
                    fp = observation.get("fp")
                    end = observation.get("end")
                    if observation.get("accn") != accession or observation.get("form") not in {
                        "10-Q", "10-K",
                    }:
                        continue
                    if isinstance(fy, int) and fp in {"Q1", "Q2", "Q3", "FY"} \
                            and end == report_date:
                        candidates.add((fy, str(fp), str(end)))
    return next(iter(candidates)) if len(candidates) == 1 else None


def successor_fiscal_period(
    periodic_filing: Mapping[str, object] | None,
    companyfacts: Mapping[str, object],
) -> FiscalPeriod:
    if not periodic_filing:
        return FiscalPeriod(None, None, "UNKNOWN", None, None, None, None, None)
    accession = str(periodic_filing.get("accessionNumber") or "")
    report_date = str(periodic_filing.get("reportDate") or "")
    matched = companyfacts_fiscal_tuple(
        companyfacts, accession=accession, report_date=report_date,
    )
    source = {
        "source_accession": accession or None,
        "source_form": str(periodic_filing.get("form") or "") or None,
        "source_report_date": report_date or None,
        "source_acceptance_at": str(periodic_filing.get("acceptanceDateTime") or "") or None,
    }
    if matched is None:
        return FiscalPeriod(None, None, "UNKNOWN", None, **source)
    fiscal_year, fp, _end = matched
    if fp == "Q1":
        return FiscalPeriod(fiscal_year, 2, "QUARTERLY", "Q2", **source)
    if fp == "Q2":
        return FiscalPeriod(fiscal_year, 3, "QUARTERLY", "Q3", **source)
    if fp == "Q3":
        return FiscalPeriod(fiscal_year, 4, "QUARTERLY", "FY/Q4", **source)
    return FiscalPeriod(fiscal_year + 1, 1, "QUARTERLY", "Q1", **source)


def latest_periodic_before(
    filings: Sequence[Mapping[str, object]], release_at: datetime,
) -> Mapping[str, object] | None:
    candidates: list[tuple[datetime, Mapping[str, object]]] = []
    for filing in filings:
        if filing.get("form") not in {"10-Q", "10-K"}:
            continue
        accepted = parse_utc_datetime(filing.get("acceptanceDateTime"))
        if accepted is not None and accepted < release_at:
            candidates.append((accepted, filing))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


_DATE_PREFIX = re.compile(br'^\{"date":"(\d{4}-\d{2}-\d{2})"')


def ordered_bar_bytes(bar: Mapping[str, object]) -> bytes:
    ordered = {
        "date": bar.get("date"),
        "open": bar.get("open"),
        "high": bar.get("high"),
        "low": bar.get("low"),
        "close": bar.get("close"),
        "volume": bar.get("volume"),
    }
    return json.dumps(
        ordered, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def iter_bars_through_reference(
    lines: Iterable[bytes], reference_session: date,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    previous: date | None = None
    found = False
    for line in lines:
        match = _DATE_PREFIX.match(line)
        if match is None:
            raise PriceCoverageError("price JSONL row lacks the required leading date")
        row_date = date.fromisoformat(match.group(1).decode("ascii"))
        if previous is not None and row_date <= previous:
            raise PriceCoverageError("price sessions are not strictly ascending")
        if row_date > reference_session:
            break  # the future OHLCV payload is never decoded
        row = json.loads(line)
        if not isinstance(row, dict):
            raise PriceCoverageError("price JSONL row is not an object")
        result.append(row)
        previous = row_date
        if row_date == reference_session:
            found = True
            break  # the next line is never requested from the iterator
    if not found:
        raise PriceCoverageError("reference session is absent from the bounded price source")
    return result


def read_bars_through_reference(path: str | Path, reference_session: date) -> list[dict[str, object]]:
    with Path(path).open("rb") as handle:
        return iter_bars_through_reference(handle, reference_session)


def require_complete_price_window(
    bars: Sequence[Mapping[str, object]], reference_session: date,
    *, calendar: Any | None = None,
) -> None:
    """Require exactly the 61 trailing XNYS sessions needed by the covariates."""

    if len(bars) < 61:
        raise PriceCoverageError("fewer than 61 sessions are available before reference")
    cal = calendar or xnys_calendar()
    expected = [
        _calendar_date(item)
        for item in cal.sessions_window(reference_session.isoformat(), -61)
    ]
    try:
        actual = [date.fromisoformat(str(item["date"])) for item in bars[-61:]]
    except (KeyError, TypeError, ValueError) as exc:
        raise PriceCoverageError("price window has invalid session dates") from exc
    if actual != expected:
        raise PriceCoverageError("price window does not contain 61 consecutive XNYS sessions")


def normalized_json_to_jsonl(source: str | Path, destination: str | Path) -> dict[str, object]:
    value = json.loads(Path(source).read_bytes())
    if not isinstance(value, list):
        raise H2Phase1Error(f"normalized price file is not an array: {source}")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with target.open("wb") as handle:
        for item in value:
            if not isinstance(item, Mapping):
                raise H2Phase1Error(f"normalized price row is invalid: {source}")
            payload = ordered_bar_bytes(item)
            handle.write(payload)
            digest.update(payload)
    return {"bytes": target.stat().st_size, "sha256": digest.hexdigest()}


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return value


def write_deterministic_csv(
    path: str | Path, rows: Sequence[Mapping[str, object]], columns: Sequence[str],
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in columns})


def input_sort_key(row: Mapping[str, object]) -> tuple[str, str, str, str]:
    return (
        str(row.get("reference_session_date") or ""),
        str(row.get("release_at") or ""), str(row.get("ticker") or ""),
        str(row.get("current_release_id") or ""),
    )


def exclusion_sort_key(row: Mapping[str, object]) -> tuple[str, str, str, str]:
    return (
        str(row.get("release_at") or ""), str(row.get("ticker") or ""),
        str(row.get("current_release_id") or ""), str(row.get("reason") or ""),
    )


def write_phase1_csvs(
    output_root: str | Path, dev_rows: Sequence[Mapping[str, object]],
    prosp_rows: Sequence[Mapping[str, object]], exclusions: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    root = Path(output_root)
    if root.exists() and any(root.iterdir()):
        raise H2Phase1Error(f"output directory must be new and empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    outputs = {
        "dev": root / "h2_v2_1_inputs_dev.csv",
        "prosp": root / "h2_v2_1_inputs_prosp_batch1.csv",
        "exclusions": root / "h2_v2_1_exclusions.csv",
    }
    write_deterministic_csv(outputs["dev"], sorted(dev_rows, key=input_sort_key), INPUT_COLUMNS)
    write_deterministic_csv(outputs["prosp"], sorted(prosp_rows, key=input_sort_key), INPUT_COLUMNS)
    write_deterministic_csv(
        outputs["exclusions"], sorted(exclusions, key=exclusion_sort_key), EXCLUSION_COLUMNS,
    )
    return {name: sha256_file(path) for name, path in outputs.items()}


def component_hashes(repo_root: str | Path) -> dict[str, str]:
    root = Path(repo_root)
    files = {
        "usa_documents_sha256": root / "trinity" / "usa_documents.py",
        "h2_novelty_component_sha256": root / "trinity" / "h2_novelty.py",
        "earnings_pairing_component_sha256": root / "trinity" / "earnings_pairing.py",
        "pre_event_covariates_component_sha256": root / "trinity" / "pre_event_covariates.py",
        "universe_sha256": root / "trinity" / "usa_issuer_registry_v2.fixture",
    }
    return {name: sha256_file(path) for name, path in files.items()}


class RequestThrottle:
    def __init__(self, requests_per_second: int = SEC_REQUESTS_PER_SECOND) -> None:
        self.minimum_interval = 1.0 / requests_per_second
        self.last_request = 0.0

    def wait(self) -> None:
        elapsed = time_module.monotonic() - self.last_request
        if elapsed < self.minimum_interval:
            time_module.sleep(self.minimum_interval - elapsed)
        self.last_request = time_module.monotonic()


class SecCacheClient:
    """Rate-limited SEC reader with hash-verified resumable cache."""

    def __init__(self, root: str | Path, session: requests.Session | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt" and session is None:
            try:
                import truststore
                truststore.inject_into_ssl()
            except ImportError:
                pass
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        self.throttle = RequestThrottle()

    def get(self, url: str, relative_path: str | Path) -> bytes:
        target = self.root / relative_path
        metadata_path = target.with_suffix(target.suffix + ".meta.json")
        if target.is_file() and metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            body = target.read_bytes()
            if metadata.get("url") == url and metadata.get("sha256") == sha256_bytes(body):
                return body
            raise H2Phase1Error(f"SEC cache integrity mismatch: {target}")

        last_error: Exception | None = None
        for attempt in range(1, SEC_MAX_ATTEMPTS + 1):
            self.throttle.wait()
            try:
                response = self.session.get(url, timeout=SEC_TIMEOUT_SECONDS)
                if response.status_code == 200:
                    body = bytes(response.content)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                    metadata = {
                        "url": url,
                        "retrieved_at_utc": utc_text(datetime.now(UTC)),
                        "http_status": 200,
                        "bytes": len(body),
                        "sha256": sha256_bytes(body),
                    }
                    metadata_path.write_bytes(canonical_json_bytes(metadata, newline=True))
                    return body
                if response.status_code not in {429, 500, 502, 503, 504}:
                    raise H2Phase1Error(f"SEC request failed HTTP {response.status_code}: {url}")
                last_error = H2Phase1Error(f"SEC transient HTTP {response.status_code}: {url}")
            except requests.RequestException as exc:
                last_error = exc
            if attempt < SEC_MAX_ATTEMPTS:
                time_module.sleep(0.5 * (2 ** (attempt - 1)))
        raise H2Phase1Error(f"SEC request failed after retries: {url}: {last_error}")


def rows_from_columnar(value: Mapping[str, object]) -> list[dict[str, object]]:
    columns = {
        key: item for key, item in value.items()
        if isinstance(item, list)
    }
    if not columns:
        return []
    lengths = {len(item) for item in columns.values()}
    if len(lengths) != 1:
        raise H2Phase1Error("SEC submissions columns have inconsistent lengths")
    return [
        {key: items[index] for key, items in columns.items()}
        for index in range(next(iter(lengths)))
    ]


def submissions_rows(payload: Mapping[str, object]) -> list[dict[str, object]]:
    filings = payload.get("filings")
    if isinstance(filings, Mapping) and isinstance(filings.get("recent"), Mapping):
        return rows_from_columnar(filings["recent"])  # type: ignore[index]
    return rows_from_columnar(payload)


def additional_submission_files(payload: Mapping[str, object]) -> list[dict[str, object]]:
    filings = payload.get("filings")
    if not isinstance(filings, Mapping) or not isinstance(filings.get("files"), list):
        return []
    result = []
    for item in filings["files"]:  # type: ignore[index]
        if not isinstance(item, Mapping):
            continue
        filing_to = str(item.get("filingTo") or "")
        name = str(item.get("name") or "")
        if name and filing_to >= "2025-01-01":
            result.append(dict(item))
    return sorted(result, key=lambda item: str(item.get("name") or ""))


def complete_submission_url(cik: str, accession: str) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{accession}.txt"
    )


def _safe_url_name(url: str) -> str:
    parsed = urlparse(url)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", parsed.path.strip("/"))


def file_inventory(root: str | Path, *, exclude_names: frozenset[str] = frozenset()) -> list[dict[str, object]]:
    base = Path(root)
    result = []
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        if path.name in exclude_names:
            continue
        result.append({
            "relative_path": path.relative_to(base).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return result


def make_tree_read_only(root: str | Path) -> None:
    for path in Path(root).rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD)


def acquire_price_ensemble(root: str | Path) -> dict[str, object]:
    base = Path(root)
    if base.exists() and any(base.iterdir()):
        raise H2Phase1Error(f"price ensemble directory must be new and empty: {base}")
    base.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    client = TwelveDataClient()
    issuer_root = base / "issuers"
    snapshot = acquire_snapshot(
        target_session=PRICE_TARGET_SESSION, output_root=issuer_root,
        run_id=PRICE_RUN_ID, client=client,
    )
    benchmark_root = base / "benchmark"
    benchmark_root.mkdir(parents=True, exist_ok=True)
    capture = client.fetch(
        "SPY", start_date=PRICE_HISTORY_START, target_session=PRICE_TARGET_SESSION,
        remaining_tickers=1,
    )
    (benchmark_root / "SPY.raw.json").write_bytes(capture.raw)
    normalized = normalize_response("SPY", capture.raw, target_session=PRICE_TARGET_SESSION)
    if normalized.classification != "ACTIVE_COMPLETE":
        raise H2Phase1Error(f"SPY price capture is not complete: {normalized.classification}")
    spy_bytes = canonical_bytes(list(normalized.bars))
    (benchmark_root / "SPY.normalized.json").write_bytes(spy_bytes)

    jsonl_root = base / "bounded_jsonl"
    for path in sorted((issuer_root / "normalized").glob("*.json")):
        normalized_json_to_jsonl(path, jsonl_root / f"{path.stem}.jsonl")
    normalized_json_to_jsonl(
        benchmark_root / "SPY.normalized.json", jsonl_root / "SPY.jsonl",
    )

    inventory = file_inventory(base, exclude_names=frozenset({"price_ensemble_manifest.json"}))
    ensemble_sha256 = sha256_bytes(canonical_json_bytes(inventory))
    completed = datetime.now(UTC)
    manifest = {
        "schema_name": "trinity.h2-v2-1-price-ensemble",
        "schema_version": "1",
        "spec_id": SPEC_ID,
        "provider": "TWELVE_DATA",
        "snapshot_id": snapshot["snapshot_id"],
        "issuer_snapshot_sha256": snapshot["snapshot_sha256"],
        "target_session": PRICE_TARGET_SESSION.isoformat(),
        "acquisition_started_at_utc": utc_text(started),
        "acquisition_completed_at_utc": utc_text(completed),
        "endpoint": "https://api.twelvedata.com/time_series",
        "parameters": {
            "interval": "1day", "start_date": PRICE_HISTORY_START.isoformat(),
            "end_date": "2026-11-18", "outputsize": 5000, "adjust": "none",
        },
        "adjustment_policy": {
            "provider_adjust": "none", "split_adjusted": False,
            "dividend_adjusted": False,
        },
        "spy_normalized_sha256": sha256_bytes(spy_bytes),
        "files": inventory,
        "ensemble_sha256": ensemble_sha256,
    }
    manifest_path = base / "price_ensemble_manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest, newline=True))
    manifest["manifest_file_sha256"] = sha256_file(manifest_path)
    make_tree_read_only(base)
    return manifest


def verify_price_ensemble(root: str | Path) -> dict[str, object]:
    base = Path(root)
    manifest_path = base / "price_ensemble_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_name") != "trinity.h2-v2-1-price-ensemble":
        raise H2Phase1Error("invalid price ensemble manifest")
    expected = manifest.get("files")
    actual = file_inventory(base, exclude_names=frozenset({"price_ensemble_manifest.json"}))
    if actual != expected or sha256_bytes(canonical_json_bytes(actual)) != manifest.get("ensemble_sha256"):
        raise H2Phase1Error("price ensemble integrity mismatch")
    return manifest


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise H2Phase1Error(f"invalid JSON: {label}") from exc
    if not isinstance(value, dict):
        raise H2Phase1Error(f"JSON is not an object: {label}")
    return value


def _deduplicate_filings(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    by_accession: dict[str, dict[str, object]] = {}
    for row in rows:
        accession = str(row.get("accessionNumber") or "")
        if not accession:
            continue
        normalized = dict(row)
        previous = by_accession.get(accession)
        if previous is not None and previous != normalized:
            raise H2Phase1Error(f"conflicting SEC submission metadata: {accession}")
        by_accession[accession] = normalized
    return sorted(
        by_accession.values(),
        key=lambda row: (str(row.get("acceptanceDateTime") or ""), str(row["accessionNumber"])),
    )


def cache_complete_submission_or_defer(
    client: SecCacheClient,
    url: str,
    relative_path: Path,
    deferred: list[tuple[str, Path]],
) -> None:
    """Cache one complete submission, deferring only an exhausted transient read."""

    try:
        client.get(url, relative_path)
    except H2Phase1Error:
        deferred.append((url, relative_path))


def retry_deferred_complete_submissions(
    client: SecCacheClient, deferred: Sequence[tuple[str, Path]],
) -> None:
    """Require every deferred resource before the corpus can be sealed."""

    for url, relative_path in deferred:
        client.get(url, relative_path)


def acquire_sec_corpus(
    root: str | Path, *, cutoff: datetime | None = None,
) -> dict[str, object]:
    """Acquire SEC metadata and complete submissions without calculating H2."""

    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    client = SecCacheClient(base / "cache")
    run_started = cutoff or datetime.now(UTC)
    if run_started.utcoffset() is None:
        raise H2Phase1Error("SEC cutoff must be timezone-aware")
    run_started = run_started.astimezone(UTC)
    registry = load_registry(DEFAULT_REGISTRY)
    issuers_index: list[dict[str, object]] = []
    deferred_complete_submissions: list[tuple[str, Path]] = []

    for issuer in sorted(registry.supported, key=lambda item: item.ticker):
        ticker = issuer.ticker
        cik = issuer.sec_cik
        submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        submissions_rel = Path("submissions") / ticker / f"CIK{cik}.json"
        submissions = _json_object(
            client.get(submissions_url, submissions_rel), f"{ticker} submissions",
        )
        filing_rows = submissions_rows(submissions)
        for extra in additional_submission_files(submissions):
            name = str(extra["name"])
            extra_url = f"https://data.sec.gov/submissions/{name}"
            extra_rel = Path("submissions") / ticker / name
            filing_rows.extend(submissions_rows(_json_object(
                client.get(extra_url, extra_rel), f"{ticker} {name}",
            )))
        filings = _deduplicate_filings(filing_rows)

        companyfacts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        companyfacts_rel = Path("companyfacts") / ticker / f"CIK{cik}.json"
        client.get(companyfacts_url, companyfacts_rel)

        relevant: list[dict[str, object]] = []
        for filing in filings:
            accepted = parse_utc_datetime(filing.get("acceptanceDateTime"))
            if accepted is None or accepted > run_started:
                continue
            form = filing.get("form")
            is_periodic = form in {"10-Q", "10-K"}
            is_release = form == "8-K" and has_item_202(filing.get("items")) \
                and accepted >= SEC_HISTORY_START
            if not (is_periodic or is_release):
                continue
            row = dict(filing)
            if is_release:
                accession = str(filing["accessionNumber"])
                submission_url = complete_submission_url(cik, accession)
                submission_rel = Path("complete_submissions") / ticker / f"{accession}.txt"
                # A single transient Archives failure must not prevent the
                # remaining issuers from entering the resumable cache.  The
                # exact same resource is retried before any index or manifest
                # is written, so an incomplete corpus cannot be sealed.
                cache_complete_submission_or_defer(
                    client, submission_url, submission_rel,
                    deferred_complete_submissions,
                )
                row["complete_submission_relative_path"] = submission_rel.as_posix()
                row["complete_submission_url"] = submission_url
            relevant.append(row)
        issuers_index.append({
            "ticker": ticker,
            "cik": cik,
            "company_name": issuer.company_name,
            "companyfacts_relative_path": companyfacts_rel.as_posix(),
            "filings": relevant,
        })

    retry_deferred_complete_submissions(client, deferred_complete_submissions)

    index = {
        "schema_name": "trinity.h2-v2-1-sec-corpus-index",
        "schema_version": "1",
        "spec_id": SPEC_ID,
        "run_started_at_utc": utc_text(run_started),
        "user_agent": SEC_USER_AGENT,
        "maximum_requests_per_second": SEC_REQUESTS_PER_SECOND,
        "universe_sha256": sha256_file(DEFAULT_REGISTRY),
        "issuer_count": len(issuers_index),
        "issuers": issuers_index,
    }
    index_path = base / "sec_corpus_index.json"
    index_path.write_bytes(canonical_json_bytes(index, newline=True))
    inventory = file_inventory(base, exclude_names=frozenset({"sec_acquisition_manifest.json"}))
    manifest = {
        "schema_name": "trinity.h2-v2-1-sec-acquisition",
        "schema_version": "1",
        "spec_id": SPEC_ID,
        "run_started_at_utc": utc_text(run_started),
        "index_sha256": sha256_file(index_path),
        "files": inventory,
        "ensemble_sha256": sha256_bytes(canonical_json_bytes(inventory)),
    }
    manifest_path = base / "sec_acquisition_manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest, newline=True))
    manifest["manifest_file_sha256"] = sha256_file(manifest_path)
    return manifest


def verify_sec_corpus(root: str | Path) -> tuple[dict[str, object], dict[str, object]]:
    base = Path(root)
    manifest = _json_object(
        (base / "sec_acquisition_manifest.json").read_bytes(), "SEC acquisition manifest",
    )
    expected = manifest.get("files")
    actual = file_inventory(base, exclude_names=frozenset({"sec_acquisition_manifest.json"}))
    if actual != expected or sha256_bytes(canonical_json_bytes(actual)) != manifest.get("ensemble_sha256"):
        raise H2Phase1Error("SEC corpus integrity mismatch")
    index_path = base / "sec_corpus_index.json"
    if sha256_file(index_path) != manifest.get("index_sha256"):
        raise H2Phase1Error("SEC corpus index hash mismatch")
    index = _json_object(index_path.read_bytes(), "SEC corpus index")
    return manifest, index


def resolve_share_class_plans(
    sec_root: str | Path,
    issuers: Sequence[Mapping[str, object]],
    supported_by_ticker: Mapping[str, str],
    sec_ticker_to_canonical: Mapping[str, str] | None = None,
) -> tuple[list[ShareClassPlan], dict[str, object]]:
    """Resolve one SEC-selected security per CIK without consulting prices."""

    sec_base = Path(sec_root)
    supported = {
        str(ticker).strip().upper(): str(cik)
        for ticker, cik in supported_by_ticker.items()
    }
    if sec_ticker_to_canonical is None:
        sec_ticker_to_canonical = {ticker: ticker for ticker in supported}
    sec_to_canonical = {
        str(sec_ticker).strip().upper(): str(ticker).strip().upper()
        for sec_ticker, ticker in sec_ticker_to_canonical.items()
    }
    if not sec_to_canonical or any(ticker not in supported for ticker in sec_to_canonical.values()):
        raise H2Phase1Error("SEC ticker mapping references an unsupported canonical ticker")
    by_cik: dict[str, list[Mapping[str, object]]] = {}
    indexed_pairs: set[tuple[str, str]] = set()
    for issuer in issuers:
        ticker = str(issuer.get("ticker") or "").strip().upper()
        cik = str(issuer.get("cik") or "")
        if not ticker or not cik:
            raise H2Phase1Error("SEC issuer index lacks ticker or CIK")
        pair = (ticker, cik)
        if pair in indexed_pairs:
            raise H2Phase1Error(f"duplicate SEC issuer index identity: {ticker}/{cik}")
        indexed_pairs.add(pair)
        if supported.get(ticker) != cik:
            raise H2Phase1Error(f"SEC index/registry identity mismatch: {ticker}/{cik}")
        by_cik.setdefault(cik, []).append(issuer)

    expected_pairs = {(ticker, cik) for ticker, cik in supported.items()}
    if indexed_pairs != expected_pairs:
        raise H2Phase1Error("SEC issuer index does not exactly match REGISTRY.supported")

    plans: list[ShareClassPlan] = []
    audit_cases: list[dict[str, object]] = []
    for cik in sorted(by_cik):
        group = sorted(by_cik[cik], key=lambda item: str(item.get("ticker") or ""))
        arrays: list[tuple[str, ...]] = []
        accession_sets: list[frozenset[str]] = []
        for issuer in group:
            ticker = str(issuer["ticker"]).upper()
            submissions_path = sec_base / "cache" / "submissions" / ticker / f"CIK{cik}.json"
            submissions = _json_object(
                submissions_path.read_bytes(), f"{ticker} cached SEC submissions",
            )
            raw_tickers = submissions.get("tickers")
            if not isinstance(raw_tickers, list) \
                    or not all(isinstance(value, str) and value.strip() for value in raw_tickers):
                raise H2Phase1Error(f"SEC submissions tickers array is invalid: CIK {cik}")
            arrays.append(tuple(raw_tickers))
            filings = issuer.get("filings")
            if not isinstance(filings, list):
                raise H2Phase1Error(f"SEC issuer filings are invalid: {ticker}/{cik}")
            accession_sets.append(frozenset(
                str(filing.get("accessionNumber"))
                for filing in filings if isinstance(filing, Mapping)
                and filing.get("accessionNumber")
            ))
        if any(value != arrays[0] for value in arrays[1:]):
            raise H2Phase1Error(f"SEC ticker arrays disagree across cached classes: CIK {cik}")
        if any(value != accession_sets[0] for value in accession_sets[1:]):
            raise H2Phase1Error(f"SEC filing sets disagree across cached classes: CIK {cik}")

        sec_tickers = arrays[0]
        selected_sec_ticker: str | None = None
        selected_ticker: str | None = None
        resolution_method = "SEC_ORDER"
        resolution_reason: str | None = None
        for raw_ticker in sec_tickers:
            sec_candidate = raw_ticker.strip().upper()
            candidate = sec_to_canonical.get(sec_candidate)
            if candidate is not None:
                if supported[candidate] != cik:
                    continue
                selected_sec_ticker = sec_candidate
                selected_ticker = candidate
                break
        if selected_ticker is None:
            resolution_reason = (
                "SEC_TICKERS_EMPTY" if not sec_tickers
                else "SEC_TICKERS_NO_REGISTRY_MATCH"
            )
            if len(group) == 1:
                selected_ticker = str(group[0]["ticker"]).upper()
                resolution_method = "REGISTRY_UNIQUE_CIK_FALLBACK"
            else:
                resolution_method = "AMBIGUOUS_TICKER_MAPPING"

        if selected_ticker is None:
            selected = group[0]  # metadata source only; no security is selected
            nonselected = tuple(str(issuer["ticker"]).upper() for issuer in group)
        else:
            selected = next(
                (issuer for issuer in group
                 if str(issuer.get("ticker") or "").upper() == selected_ticker),
                None,
            )
            if selected is None:
                raise H2Phase1Error(
                    f"selected ticker absent from SEC issuer index: {selected_ticker}",
                )
            nonselected = tuple(
                str(issuer["ticker"]).upper() for issuer in group
                if str(issuer["ticker"]).upper() != selected_ticker
            )
        plans.append(ShareClassPlan(
            cik=cik, selected_sec_ticker=selected_sec_ticker,
            selected_ticker=selected_ticker, issuer=selected,
            sec_tickers=sec_tickers,
            nonselected_supported_tickers=nonselected,
            resolution_method=resolution_method,
            resolution_reason=resolution_reason,
        ))

        if nonselected or resolution_method != "SEC_ORDER":
            source_ticker = (
                selected_ticker if selected_ticker is not None
                else str(group[0]["ticker"]).upper()
            )
            relative_source = Path("cache") / "submissions" / source_ticker / f"CIK{cik}.json"
            source_path = sec_base / relative_source
            metadata_path = source_path.with_suffix(source_path.suffix + ".meta.json")
            metadata = _json_object(
                metadata_path.read_bytes(), f"{selected_ticker} submissions cache metadata",
            )
            source_sha = sha256_file(source_path)
            if metadata.get("sha256") != source_sha:
                raise H2Phase1Error(f"SEC submissions cache hash mismatch: {source_path}")
            audit_cases.append({
                "cik": cik,
                "sec_tickers": list(sec_tickers),
                "registry_supported_tickers": [str(issuer["ticker"]).upper() for issuer in group],
                "resolution_method": resolution_method,
                "reason": resolution_reason,
                "selected_sec_ticker": selected_sec_ticker,
                "selected_ticker": selected_ticker,
                "registry_ticker_used": selected_ticker,
                "nonselected_supported_tickers": list(nonselected),
                "submissions_source_relative_path": relative_source.as_posix(),
                "submissions_sha256": source_sha,
                "acquired_at_utc": metadata.get("retrieved_at_utc"),
            })

    audit = {
        "schema_name": "trinity.h2-v2-1-share-class-resolution",
        "schema_version": "1",
        "spec_id": SPEC_ID,
        "selection_rule": "SEC_ORDER_THEN_UNIQUE_CIK_REGISTRY_FALLBACK",
        "cases": audit_cases,
    }
    return plans, audit


def _release_identity_from_filing(
    *, ticker: str, filing: Mapping[str, object],
) -> dict[str, object]:
    """Build timing/sample identity without reading text, prices, or facts."""

    accession = str(filing.get("accessionNumber") or "")
    accepted = parse_utc_datetime(filing.get("acceptanceDateTime"))
    if not accession or accepted is None:
        raise H2Phase1Error(f"release lacks accession or aware acceptance time: {ticker}")
    resolution = classify_and_resolve(filing.get("acceptanceDateTime"))
    return {
        "ticker": ticker,
        "release_id": accession,
        "release_at": utc_text(accepted),
        "release_at_dt": accepted,
        "acceptanceDateTime": filing.get("acceptanceDateTime"),
        "event_timing": resolution.timing,
        "timing_ambiguous": resolution.ambiguous,
        "reference_resolution": resolution.reference_resolution,
        "reference_session_date": resolution.reference_session,
        "release_local_date": resolution.local_date,
        "release_period": descriptive_release_period(resolution.local_date),
        "sample": assign_sample(accepted, resolution),
    }


def _release_from_filing(
    *, ticker: str, filing: Mapping[str, object], all_filings: Sequence[Mapping[str, object]],
    companyfacts: Mapping[str, object], sec_root: Path, text_root: Path,
) -> dict[str, object]:
    identity = _release_identity_from_filing(ticker=ticker, filing=filing)
    accession = str(identity["release_id"])
    accepted = identity["release_at_dt"]
    assert isinstance(accepted, datetime)
    relative = filing.get("complete_submission_relative_path")
    selected: SelectedExhibit | None = None
    normalized: str | None = None
    text_sha: str | None = None
    text_error: str | None = None
    if isinstance(relative, str) and relative:
        submission = (sec_root / "cache" / Path(relative)).read_bytes()
        selected = select_ex99(submission)
        if selected is not None:
            try:
                normalized = normalized_exhibit_text(selected.content)
                text_bytes = normalized.encode("utf-8")
                text_sha = sha256_bytes(text_bytes)
                text_path = text_root / ticker / f"{accession}.txt"
                text_path.parent.mkdir(parents=True, exist_ok=True)
                if text_path.exists() and text_path.read_bytes() != text_bytes:
                    raise H2Phase1Error(f"normalized text collision: {text_path}")
                text_path.write_bytes(text_bytes)
            except (H2InputError, UnicodeError, ValueError) as exc:
                text_error = str(exc)
    periodic = latest_periodic_before(all_filings, accepted)
    fiscal = successor_fiscal_period(periodic, companyfacts)
    return {
        **identity,
        "fiscal_year": fiscal.fiscal_year,
        "fiscal_quarter": fiscal.fiscal_quarter,
        "period_type": fiscal.period_type,
        "fiscal_period_label": fiscal.label,
        "periodic_source_accession": fiscal.source_accession,
        "periodic_source_form": fiscal.source_form,
        "periodic_source_report_date": fiscal.source_report_date,
        "periodic_source_acceptance_at": fiscal.source_acceptance_at,
        "document_type": "EARNINGS_RELEASE",
        "selected_exhibit": selected,
        "normalized_text": normalized,
        "text_sha256": text_sha,
        "text_error": text_error,
    }


def pairing_quality(result: Mapping[str, object]) -> str:
    status = result.get("pairing_status")
    method = result.get("pairing_method")
    if status == "MATCHED" and method == "EXACT_PREVIOUS_FISCAL_PERIOD":
        return PAIRING_EXACT
    if status == "MATCHED" and method == "CHRONOLOGICAL_COMPATIBLE_FALLBACK":
        return PAIRING_FALLBACK
    if status == "AMBIGUOUS":
        return PAIRING_AMBIGUOUS
    return PAIRING_NO_MATCH


def _pairing_record(release: Mapping[str, object]) -> dict[str, object]:
    return {
        "ticker": release["ticker"],
        "release_id": release["release_id"],
        "release_at": release["release_at"],
        "fiscal_year": release.get("fiscal_year"),
        "fiscal_quarter": release.get("fiscal_quarter"),
        "period_type": release.get("period_type"),
        "document_type": "EARNINGS_RELEASE",
    }


def _exclusion_row(
    release: Mapping[str, object], reason: str, detail: str = "",
    *, ticker_override: str | None = None,
) -> dict[str, object]:
    if reason not in EXCLUSION_REASONS:
        raise H2Phase1Error(f"unsupported exclusion reason: {reason}")
    reference = release.get("reference_session_date")
    return {
        "sample": release.get("sample"),
        "release_period": release.get("release_period"),
        "ticker": release.get("ticker") if ticker_override is None else ticker_override,
        "current_release_id": release.get("release_id"),
        "release_at": release.get("release_at"),
        "event_timing": release.get("event_timing"),
        "reference_session_date": reference.isoformat() if isinstance(reference, date) else None,
        "reason": reason,
        "detail": detail,
    }


def resolution_event_scope(release: Mapping[str, object]) -> str:
    accepted = release.get("release_at_dt")
    if not isinstance(accepted, datetime):
        raise H2Phase1Error("resolution audit release lacks acceptance time")
    if accepted < DEV_RELEASE_START:
        return "HISTORICAL_PAIRING"
    sample = str(release.get("sample") or "")
    if sample in {"DEV", "PROSP_BATCH1", "UNASSIGNED"}:
        return sample
    return "OUTSIDE_WINDOW"


def _price_manifest_commit_view(manifest: Mapping[str, object]) -> dict[str, object]:
    return {
        key: manifest[key]
        for key in (
            "schema_name", "schema_version", "spec_id", "provider", "snapshot_id",
            "issuer_snapshot_sha256", "target_session", "acquisition_started_at_utc",
            "acquisition_completed_at_utc", "endpoint", "parameters", "adjustment_policy",
            "spy_normalized_sha256", "files", "ensemble_sha256",
        )
    }


def build_phase1_inputs(
    *, repo_root: str | Path, sec_root: str | Path, price_root: str | Path,
    text_root: str | Path, output_root: str | Path,
    supported_by_ticker: Mapping[str, str] | None = None,
    sec_ticker_to_canonical: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Build outcome-free input snapshots from verified immutable sources."""

    repo = Path(repo_root)
    sec_base = Path(sec_root)
    text_base = Path(text_root)
    output = Path(output_root)
    if text_base.exists() and any(text_base.iterdir()):
        raise H2Phase1Error(f"normalized text output must be new and empty: {text_base}")
    text_base.mkdir(parents=True, exist_ok=True)
    sec_manifest, sec_index = verify_sec_corpus(sec_base)
    price_manifest = verify_price_ensemble(price_root)
    hashes = component_hashes(repo)
    universe_sha = hashes["universe_sha256"]
    price_jsonl = Path(price_root) / "bounded_jsonl"

    dev_rows: list[dict[str, object]] = []
    prosp_rows: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []

    issuers = sec_index.get("issuers")
    if not isinstance(issuers, list):
        raise H2Phase1Error("SEC corpus index lacks issuers")
    if not all(isinstance(issuer, Mapping) for issuer in issuers):
        raise H2Phase1Error("SEC issuer index entry is invalid")
    if supported_by_ticker is None:
        registry = load_registry(repo / "trinity" / "usa_issuer_registry_v2.fixture")
        supported_by_ticker = {
            record.ticker: record.sec_cik for record in registry.supported
        }
        sec_pairs = [(record.sec_ticker, record.ticker) for record in registry.supported]
        sec_ticker_to_canonical = dict(sec_pairs)
        if len(sec_ticker_to_canonical) != len(sec_pairs):
            raise H2Phase1Error("REGISTRY.supported has a non-unique SEC ticker mapping")
    plans, share_class_audit = resolve_share_class_plans(
        sec_base, issuers, supported_by_ticker, sec_ticker_to_canonical,
    )
    audit_cases = share_class_audit.get("cases")
    if not isinstance(audit_cases, list):
        raise H2Phase1Error("share-class resolution audit is malformed")
    audit_by_cik: dict[str, dict[str, object]] = {}
    for case in audit_cases:
        if not isinstance(case, dict) or not isinstance(case.get("cik"), str):
            raise H2Phase1Error("share-class resolution case is malformed")
        case["event_counts"] = {
            "HISTORICAL_PAIRING": 0, "DEV": 0, "PROSP_BATCH1": 0,
            "UNASSIGNED": 0, "OUTSIDE_WINDOW": 0,
        }
        audit_by_cik[str(case["cik"])] = case
    seen_accessions: dict[str, str] = {}
    duplicate_share_class_row_count = 0
    for plan in plans:
        issuer = plan.issuer
        ticker = plan.selected_ticker
        filings_value = issuer.get("filings")
        if not isinstance(filings_value, list):
            raise H2Phase1Error("SEC issuer index is malformed")
        filings = [dict(item) for item in filings_value if isinstance(item, Mapping)]
        release_filings = [
            filing for filing in filings
            if filing.get("form") == "8-K" and has_item_202(filing.get("items"))
            and (parse_utc_datetime(filing.get("acceptanceDateTime")) or datetime.min.replace(tzinfo=UTC))
            >= SEC_HISTORY_START
        ]
        if ticker is None:
            releases = [
                _release_identity_from_filing(ticker="", filing=filing)
                for filing in release_filings
            ]
        else:
            facts_rel = issuer.get("companyfacts_relative_path")
            if not isinstance(facts_rel, str):
                raise H2Phase1Error(f"companyfacts path missing: {ticker}")
            companyfacts = _json_object(
                (sec_base / "cache" / Path(facts_rel)).read_bytes(), f"{ticker} companyfacts",
            )
            releases = [
                _release_from_filing(
                    ticker=ticker, filing=filing, all_filings=filings,
                    companyfacts=companyfacts, sec_root=sec_base, text_root=text_base,
                )
                for filing in release_filings
            ]
        releases.sort(key=lambda item: (item["release_at"], item["release_id"]))
        audit_case = audit_by_cik.get(plan.cik)
        for release in releases:
            accession = str(release["release_id"])
            previous_cik = seen_accessions.get(accession)
            if previous_cik is not None:
                raise H2Phase1Error(
                    f"accession appears under multiple issuer plans: {accession} "
                    f"({previous_cik}, {plan.cik})",
                )
            seen_accessions[accession] = plan.cik
            if audit_case is not None:
                counts = audit_case.get("event_counts")
                assert isinstance(counts, dict)
                scope = resolution_event_scope(release)
                counts[scope] = int(counts[scope]) + 1

        if ticker is None:
            registry_tickers = ",".join(plan.nonselected_supported_tickers)
            for release in releases:
                exclusions.append(_exclusion_row(
                    release, "AMBIGUOUS_TICKER_MAPPING",
                    f"cik={plan.cik};registry_tickers={registry_tickers}",
                    ticker_override="",
                ))
            continue

        by_id = {str(item["release_id"]): item for item in releases}
        for release in releases:
            for alias in plan.nonselected_supported_tickers:
                exclusions.append(_exclusion_row(
                    release, "DUPLICATE_SHARE_CLASS",
                    f"cik={plan.cik};selected_ticker={ticker}",
                    ticker_override=alias,
                ))
                duplicate_share_class_row_count += 1

        for release in releases:
            accepted = release["release_at_dt"]
            if not isinstance(accepted, datetime) or accepted < DEV_RELEASE_START:
                continue  # historical pairing input only
            selected = release.get("selected_exhibit")
            timing = release.get("event_timing")
            sample = str(release.get("sample"))
            initial_reasons: list[tuple[str, str]] = []
            if timing == "INTRADAY":
                initial_reasons.append(("TIMING_INTRADAY", "daily bars cannot resolve intraday timing"))
            elif timing == "UNKNOWN":
                initial_reasons.append(("TIMING_UNKNOWN", "acceptance timestamp is unavailable or invalid"))
            if selected is None:
                initial_reasons.append(("NO_EX99", "no qualifying EX-99 document in the complete submission"))
            if sample == "OUTSIDE":
                initial_reasons.append(("OUTSIDE_SAMPLE_WINDOW", "reference session is outside DEV/PROSP_BATCH1"))
            if sample == "UNASSIGNED" or selected is None or sample == "OUTSIDE":
                exclusions.extend(_exclusion_row(release, reason, detail) for reason, detail in initial_reasons)
                continue

            pairing_history = [
                item for item in releases
                if item["release_at"] < release["release_at"]
                and item.get("selected_exhibit") is not None
            ]
            pairing = pair_earnings_release(
                _pairing_record(release),
                [_pairing_record(item) for item in pairing_history],
            )
            quality = pairing_quality(pairing)
            previous_id = pairing.get("previous_release_id")
            previous = by_id.get(str(previous_id)) if previous_id else None
            novelty: Mapping[str, object] = {}
            h2_error = ""
            if previous is not None and release.get("normalized_text") \
                    and previous.get("normalized_text"):
                try:
                    novelty = calculate_h2_novelty(
                        str(release["normalized_text"]), str(previous["normalized_text"]),
                    )
                except H2InputError as exc:
                    h2_error = str(exc)

            covariates: Mapping[str, object] = {
                "sigma_pre_60": None, "dollar_volume_pre_60": None,
                "sigma_status": "PRICE_COVERAGE", "dollar_volume_status": "PRICE_COVERAGE",
                "price_window_start": None, "price_window_sha256": None,
            }
            price_error = ""
            reference = release.get("reference_session_date")
            if isinstance(reference, date):
                try:
                    bars = read_bars_through_reference(price_jsonl / f"{ticker}.jsonl", reference)
                    require_complete_price_window(bars, reference)
                    covariates = compute_pre_event_covariates(
                        reference, bars, novelty.get("current_word_count"),
                        novelty.get("previous_word_count"), timing,
                    )
                except (OSError, PriceCoverageError, ValueError) as exc:
                    price_error = str(exc)

            reasons: list[tuple[str, str]] = []
            if release.get("period_type") == "UNKNOWN":
                reasons.append(("FISCAL_PERIOD_UNKNOWN", "point-in-time fiscal period is unavailable"))
            if quality == PAIRING_FALLBACK:
                reasons.append(("PAIRING_FALLBACK", "chronological compatible fallback was used"))
            elif quality == PAIRING_NO_MATCH:
                reasons.append(("PAIRING_NO_MATCH", "no comparable prior release"))
            elif quality == PAIRING_AMBIGUOUS:
                reasons.append(("PAIRING_AMBIGUOUS", "pairing has tied best candidates"))
            if not release.get("normalized_text"):
                reasons.append(("TEXT_UNAVAILABLE", str(release.get("text_error") or "text unavailable")))
            if not novelty:
                reasons.append(("H2_UNAVAILABLE", h2_error or "matched normalized text pair unavailable"))
            if price_error or covariates.get("sigma_status") != "COMPLETE":
                reasons.append(("PRICE_COVERAGE", price_error or str(covariates.get("sigma_status"))))
            if covariates.get("sigma_pre_60") is None:
                reasons.append(("SIGMA_PRE_60_MISSING", str(covariates.get("sigma_status"))))

            exhibit = selected
            assert isinstance(exhibit, SelectedExhibit)
            row: dict[str, object] = {
                "sample": sample,
                "release_period": release.get("release_period"),
                "row_status": "INCLUDED" if not reasons else "EXCLUDED",
                "exclusion_reasons": ";".join(reason for reason, _detail in reasons),
                "current_release_id": release["release_id"],
                "ticker": ticker,
                "release_at": release["release_at"],
                "acceptanceDateTime": release["acceptanceDateTime"],
                "event_timing": timing,
                "timing_ambiguous": release["timing_ambiguous"],
                "reference_resolution": release["reference_resolution"],
                "reference_session_date": reference.isoformat() if isinstance(reference, date) else None,
                "previous_release_id": previous_id,
                "pairing_quality": quality,
                "current_text_sha256": release.get("text_sha256"),
                "previous_text_sha256": previous.get("text_sha256") if previous else None,
                "h2_similarity": novelty.get("similarity"),
                "h2_novelty": novelty.get("h2_novelty"),
                "current_word_count": novelty.get("current_word_count"),
                "previous_word_count": novelty.get("previous_word_count"),
                "sigma_pre_60": covariates.get("sigma_pre_60"),
                "dollar_volume_pre_60": covariates.get("dollar_volume_pre_60"),
                "fiscal_year": release.get("fiscal_year"),
                "fiscal_quarter": release.get("fiscal_quarter"),
                "fiscal_period_label": release.get("fiscal_period_label"),
                "period_type": release.get("period_type"),
                "periodic_source_accession": release.get("periodic_source_accession"),
                "periodic_source_form": release.get("periodic_source_form"),
                "periodic_source_report_date": release.get("periodic_source_report_date"),
                "periodic_source_acceptance_at": release.get("periodic_source_acceptance_at"),
                "pairing_status": pairing.get("pairing_status"),
                "pairing_method": pairing.get("pairing_method"),
                "fiscal_continuity": pairing.get("fiscal_continuity"),
                "h2_method": novelty.get("method"),
                "sigma_status": covariates.get("sigma_status"),
                "dollar_volume_status": covariates.get("dollar_volume_status"),
                "price_window_start": covariates.get("price_window_start"),
                "price_window_sha256": covariates.get("price_window_sha256"),
                "selected_exhibit_type": exhibit.document_type,
                "selected_exhibit_filename": exhibit.filename,
                "selected_exhibit_ordinal": exhibit.ordinal,
                "selected_exhibit_raw_sha256": sha256_bytes(exhibit.content),
                "price_provider": price_manifest.get("provider"),
                "price_snapshot_id": price_manifest.get("snapshot_id"),
                "price_snapshot_sha256": price_manifest.get("issuer_snapshot_sha256"),
                "price_ensemble_sha256": price_manifest.get("ensemble_sha256"),
                "universe_sha256": universe_sha,
                "build_spec_id": SPEC_ID,
                **hashes,
            }
            if sample == "DEV":
                dev_rows.append(row)
            elif sample == "PROSP_BATCH1":
                prosp_rows.append(row)
            else:
                raise H2Phase1Error(f"unexpected sample after filtering: {sample}")
            exclusions.extend(_exclusion_row(release, reason, detail) for reason, detail in reasons)

    csv_hashes = write_phase1_csvs(output, dev_rows, prosp_rows, exclusions)
    text_inventory = file_inventory(text_base)
    text_manifest = {
        "schema_name": "trinity.h2-v2-1-normalized-sec-texts",
        "schema_version": "1", "spec_id": SPEC_ID, "files": text_inventory,
        "ensemble_sha256": sha256_bytes(canonical_json_bytes(text_inventory)),
    }
    (output / "h2_v2_1_sec_text_manifest.json").write_bytes(
        canonical_json_bytes(text_manifest, newline=True),
    )
    (output / "h2_v2_1_price_source_manifest.json").write_bytes(
        canonical_json_bytes(_price_manifest_commit_view(price_manifest), newline=True),
    )
    share_class_path = output / "h2_v2_1_share_class_resolution.json"
    share_class_path.write_bytes(canonical_json_bytes(share_class_audit, newline=True))
    build_manifest = {
        "schema_name": "trinity.h2-v2-1-phase1-build",
        "schema_version": "1", "spec_id": SPEC_ID,
        "built_at_utc": utc_text(datetime.now(UTC)),
        "sec_ensemble_sha256": sec_manifest.get("ensemble_sha256"),
        "price_ensemble_sha256": price_manifest.get("ensemble_sha256"),
        "universe_sha256": universe_sha,
        "component_hashes": hashes,
        "csv_sha256": csv_hashes,
        "text_ensemble_sha256": text_manifest["ensemble_sha256"],
        "share_class_resolution_sha256": sha256_file(share_class_path),
        "duplicate_share_class_row_count": duplicate_share_class_row_count,
    }
    build_manifest_path = output / "h2_v2_1_build_manifest.json"
    build_manifest_path.write_bytes(canonical_json_bytes(build_manifest, newline=True))
    build_manifest["build_manifest_sha256"] = sha256_file(build_manifest_path)
    build_manifest["price_source_manifest_sha256"] = sha256_file(
        output / "h2_v2_1_price_source_manifest.json",
    )
    build_manifest["sec_text_manifest_sha256"] = sha256_file(
        output / "h2_v2_1_sec_text_manifest.json",
    )
    build_manifest["dev_candidate_count"] = len(dev_rows)
    build_manifest["prosp_candidate_count"] = len(prosp_rows)
    build_manifest["exclusion_row_count"] = len(exclusions)
    return build_manifest

