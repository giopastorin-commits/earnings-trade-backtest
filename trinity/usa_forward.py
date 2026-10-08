"""Snapshot prices plus fresh EODHD News and SEC evidence for the forward pilot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping
import zipfile

import requests

from trinity.usa_documents import acquire_sec_documents
from trinity.usa_issuer_registry import get_issuer
from trinity.usa_v2 import EvidencePack, load_company
from trinity.twelvedata_prices import PRICE_PROVIDER, ValidatedPriceSnapshot


EODHD_NEWS = "https://eodhistoricaldata.com/api/news"
MAX_PRICE_AGE_DAYS = 7
DEFAULT_CONTINUITY_PATH = Path("data/local/luna_triage_v1_latest.json")
MIN_SETUP_BARS = 201


class ForwardAcquisitionError(RuntimeError):
    """A required live source was missing, invalid, or stale."""


@dataclass(frozen=True)
class ForwardTickerSources:
    ticker: str
    as_of: str
    pack: EvidencePack
    bars: list[dict[str, Any]]
    price_raw: bytes
    research_archive: bytes
    price_retrieved_at: str
    research_retrieved_at: str
    fresh_price_timestamp: str
    freshest_evidence_timestamp: str
    price_snapshot_id: str = "-"
    price_snapshot_session: str = "-"
    price_snapshot_sha256: str = "-"
    price_provider: str = PRICE_PROVIDER
    news_provider: str = "EODHD"
    primary_evidence_provider: str = "SEC EDGAR"


def utc_timestamp(value: datetime) -> str:
    """Return a Ledger-compatible real UTC timestamp."""
    if value.tzinfo is None:
        raise ValueError("forward timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_forward_bars(
    ticker: str, bars: object, *, retrieved_at: datetime,
    max_age_days: int = MAX_PRICE_AGE_DAYS,
) -> list[dict[str, Any]]:
    if not isinstance(bars, list) or len(bars) < MIN_SETUP_BARS:
        raise ForwardAcquisitionError(
            f"{ticker}: fresh price response has fewer than {MIN_SETUP_BARS} bars"
        )
    required = ("date", "open", "high", "low", "close", "volume")
    normalized = sorted(bars, key=lambda item: str(item.get("date", "")))
    if any(not isinstance(item, dict) or any(item.get(key) is None for key in required)
           for item in normalized):
        raise ForwardAcquisitionError(f"{ticker}: fresh price response has invalid OHLCV")
    latest = date.fromisoformat(str(normalized[-1]["date"]))
    today = retrieved_at.astimezone(timezone.utc).date()
    age = (today - latest).days
    if age < 0:
        raise ForwardAcquisitionError(f"{ticker}: fresh price response contains a future bar")
    if age > max_age_days:
        raise ForwardAcquisitionError(
            f"{ticker}: latest completed price session {latest.isoformat()} is stale ({age} days)"
        )
    if float(normalized[-1]["close"]) <= 0 or float(normalized[-1]["volume"]) < 0:
        raise ForwardAcquisitionError(f"{ticker}: latest fresh OHLCV is invalid")
    return normalized


class EODHDNewsSECForwardProvider:
    """Use snapshot OHLCV and acquire only EODHD News plus SEC evidence live."""

    def __init__(
        self, source_root: str | Path, *, price_snapshot: ValidatedPriceSnapshot,
        api_key: str | None = None,
        session: requests.Session | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sec_acquirer: Callable[..., Path] = acquire_sec_documents,
        continuity_path: str | Path | None = DEFAULT_CONTINUITY_PATH,
        decision_cutoff_utc: datetime | None = None,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.price_snapshot = price_snapshot
        self.api_key = api_key or os.getenv("EODHD_API_KEY")
        if not self.api_key:
            raise ForwardAcquisitionError("EODHD_API_KEY is not configured")
        self.session = session or requests.Session()
        if session is None:
            import truststore
            truststore.inject_into_ssl()
        self.clock = clock
        self.sec_acquirer = sec_acquirer
        self.continuity_path = Path(continuity_path) if continuity_path is not None else None
        self.decision_cutoff_utc = decision_cutoff_utc

    def acquire(self, ticker: str) -> ForwardTickerSources:
        issuer = get_issuer(ticker)
        root = self.source_root / ticker
        prices_dir = root / "prices"
        news_dir = root / "news"
        sec_dir = root / "sec"
        for path in (prices_dir, news_dir / ticker, sec_dir):
            path.mkdir(parents=True, exist_ok=True)

        try:
            price_time = datetime.fromisoformat(
                self.price_snapshot.acquisition_completed_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            price_raw = self.price_snapshot.normalized_bytes(ticker)
            price_value = self.price_snapshot.bars(ticker)
        except (OSError, ValueError, TypeError) as exc:
            raise ForwardAcquisitionError(
                f"{ticker}: validated price snapshot read failed"
            ) from exc
        if self.decision_cutoff_utc is not None and self.price_snapshot.target_session != \
                self.decision_cutoff_utc.astimezone(timezone.utc).date().isoformat():
            raise ForwardAcquisitionError(
                f"{ticker}: snapshot session differs from decision cutoff"
            )
        try:
            bars = validate_forward_bars(ticker, price_value, retrieved_at=self._now())
        except (TypeError, ValueError) as exc:
            raise ForwardAcquisitionError(f"{ticker}: snapshot OHLCV is invalid") from exc
        as_of = str(bars[-1]["date"])
        (prices_dir / f"{ticker}.json").write_bytes(price_raw)

        news_time = self._now()
        news_raw = self._get(
            EODHD_NEWS,
            {"api_token": self.api_key, "s": issuer.provider_symbol,
             "from": (date.fromisoformat(as_of) - timedelta(days=190)).isoformat(),
             "to": as_of, "limit": 1000, "fmt": "json"},
            ticker, "news",
        )
        news = self._normalize_news(
            ticker, news_raw, utc_timestamp(news_time), self.decision_cutoff_utc,
        )
        news_file = news_dir / ticker / f"{as_of[:7]}.jsonl"
        news_file.write_text(
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in news),
            encoding="utf-8",
        )

        sec_time = self._now()
        sec_arguments = {
            "cache_dir": sec_dir, "session": self.session,
            "tickers": (ticker,), "now": sec_time,
        }
        if self.decision_cutoff_utc is not None:
            sec_arguments["decision_cutoff_utc"] = self.decision_cutoff_utc
        self.sec_acquirer(as_of, **sec_arguments)
        captured = self._now()
        pack = load_company(
            ticker, as_of, prices_dir=prices_dir, news_dir=news_dir,
            documents_dir=sec_dir, forward=True,
            price_retrieved_at=utc_timestamp(price_time),
            price_source="Twelve Data validated immutable OHLCV snapshot",
            news_source="EODHD live issuer/news API",
            continuity_evidence_ids=self._continuity_evidence_ids(ticker),
        )
        if not any(str(item.get("source", "")).startswith("SEC EDGAR")
                   for item in pack.company_input.get("evidence", [])):
            raise ForwardAcquisitionError(f"{ticker}: required SEC primary evidence unavailable")

        archive = self._archive(root, news_raw)
        evidence_times = [
            str(item.get("accepted_at") or item.get("published_at") or "")
            for item in pack.company_input.get("evidence", [])
        ]
        return ForwardTickerSources(
            ticker=ticker, as_of=as_of, pack=pack, bars=bars,
            price_raw=price_raw, research_archive=archive,
            price_retrieved_at=utc_timestamp(price_time),
            research_retrieved_at=utc_timestamp(captured),
            fresh_price_timestamp=as_of,
            freshest_evidence_timestamp=max((item for item in evidence_times if item), default="-"),
            price_snapshot_id=self.price_snapshot.snapshot_id,
            price_snapshot_session=self.price_snapshot.target_session,
            price_snapshot_sha256=self.price_snapshot.snapshot_sha256,
        )

    def _continuity_evidence_ids(self, ticker: str) -> tuple[str, ...]:
        path = self.continuity_path
        if path is None or not path.is_file():
            return ()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            results = value.get("results", [])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise ForwardAcquisitionError("invalid Luna evidence-continuity file") from exc
        match = next((item for item in results if isinstance(item, dict) and
                      str(item.get("ticker") or "").upper() == ticker and
                      item.get("decision") == "ESCALATE"), None)
        if match is None:
            return ()
        selected = match.get("selected_evidence_ids", [])
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
            raise ForwardAcquisitionError(f"{ticker}: invalid Luna selected evidence IDs")
        return tuple(selected)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ForwardAcquisitionError("forward acquisition clock is not timezone-aware")
        return value.astimezone(timezone.utc)

    def _get(
        self, url: str, params: Mapping[str, object], ticker: str, kind: str,
    ) -> bytes:
        retryable_statuses = frozenset({429, 500, 502, 503, 504})
        retryable_exceptions = (
            requests.ReadTimeout, requests.ConnectTimeout, requests.ConnectionError,
        )
        for attempt in range(1, 4):
            try:
                response = self.session.get(url, params=dict(params), timeout=45)
                response.raise_for_status()
                return bytes(response.content)
            except requests.RequestException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = isinstance(exc, retryable_exceptions) or status in retryable_statuses
                if retryable and attempt < 3:
                    time.sleep(attempt)
                    continue
                suffix = f" HTTP {status}" if status is not None else ""
                raise ForwardAcquisitionError(
                    f"{ticker}: EODHD {kind} request failed{suffix} ({type(exc).__name__})"
                ) from exc
        raise AssertionError("unreachable EODHD retry state")

    @staticmethod
    def _normalize_news(
        ticker: str, raw: bytes, retrieved_at: str,
        decision_cutoff_utc: datetime | None = None,
    ) -> list[dict[str, Any]]:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ForwardAcquisitionError(f"{ticker}: fresh news response is not JSON") from exc
        if not isinstance(value, list):
            raise ForwardAcquisitionError(f"{ticker}: fresh news response has invalid shape")
        normalized = []
        for item in value:
            if not isinstance(item, dict) or not item.get("date"):
                continue
            if decision_cutoff_utc is not None:
                try:
                    published = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00"))
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                    if published.astimezone(timezone.utc) > decision_cutoff_utc.astimezone(timezone.utc):
                        continue
                except ValueError:
                    continue
            canonical = json.dumps(
                item, ensure_ascii=False, allow_nan=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            normalized.append({
                **item,
                "published_ts": str(item["date"]),
                "ingestion_ts": retrieved_at,
                "canonical_record_sha256": hashlib.sha256(canonical).hexdigest(),
            })
        return normalized

    @staticmethod
    def _archive(root: Path, news_raw: bytes) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("eodhd/news_raw.json", news_raw)
            for path in sorted((root / "sec").rglob("*")):
                if path.is_file():
                    archive.writestr(f"sec/{path.relative_to(root / 'sec').as_posix()}", path.read_bytes())
        return output.getvalue()


# Compatibility import for non-production callers. The implementation no longer
# contains or calls an EODHD price endpoint and requires a validated snapshot.
EODHDSECForwardProvider = EODHDNewsSECForwardProvider
