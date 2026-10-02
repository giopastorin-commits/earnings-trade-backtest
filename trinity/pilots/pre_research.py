"""Deterministic, price-only screening before any TRINITY research call."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import time as runtime_clock
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

from trinity.usa_setup_v1 import (
    PRICE_CACHE,
    _prepare_bars,
    _regime,
    _technical_metrics,
)


CANONICAL_DATASET = PRICE_CACHE.parent
CANONICAL_UNIVERSE = CANONICAL_DATASET / "ticker_mapping.csv"
FORWARD_PRICE_ROOT = Path("data/local/trinity_forward_sources/trinity_forward_pilot")
DEFAULT_CACHE = Path("data/local/pre_research_funnel_prices")
DEFAULT_OUTPUT = Path("data/local/pre_research_funnel_latest.json")
EODHD_EOD = "https://eodhd.com/api/eod/{provider_ticker}"
MIN_BARS = 201
NEAR_BOUNDARY_GAP_PCT = 1.0
CONTROL_TICKERS = ("AAPL", "JPM", "JNJ", "WMT", "LIN", "PLD", "XOM", "CAT")
STATES = ("READY_TECHNICALLY", "NEAR", "DISTANT")


class FunnelDataError(RuntimeError):
    """A ticker cannot be screened from trustworthy completed-session bars."""


@dataclass(frozen=True)
class UniverseMember:
    ticker: str
    provider_ticker: str


@dataclass(frozen=True)
class PriceCapture:
    ticker: str
    bars: list[dict[str, Any]]
    source: str
    requested: bool
    downloaded_bytes: int


@dataclass(frozen=True)
class ScreenedTicker:
    ticker: str
    latest_session: str
    close: float
    regime: str
    sma20: float
    sma50: float
    sma200: float
    distance_sma20_pct: float
    distance_sma50_pct: float
    distance_prior_high20_pct: float
    atr14: float
    rsi14: float
    relative_volume: float
    pullback_band: bool
    near_breakout: bool
    volume_confirmed: bool
    screening_state: str
    primary_reason: str
    proximity_gap_pct: float


@dataclass(frozen=True)
class Rejection:
    ticker: str
    reason: str
    detail: str


@dataclass(frozen=True)
class FunnelResult:
    universe_source: str
    latest_completed_session: str
    counts: dict[str, int]
    shortlist: tuple[ScreenedTicker, ...]
    survivors: tuple[ScreenedTicker, ...]
    rejections: tuple[Rejection, ...]
    control_group: dict[str, str]
    eodhd_requests: int
    downloaded_bytes: int
    cache_hits: int
    runtime_seconds: float


def load_canonical_universe(path: str | Path = CANONICAL_UNIVERSE) -> tuple[UniverseMember, ...]:
    source = Path(path)
    with source.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    members = tuple(sorted(
        (UniverseMember(str(row["canonical_ticker"]).strip().upper(),
                        str(row["provider_ticker"]).strip().upper()) for row in rows),
        key=lambda item: item.ticker,
    ))
    if not members or any(not item.ticker or not item.provider_ticker for item in members):
        raise ValueError("canonical universe contains an empty ticker mapping")
    if len({item.ticker for item in members}) != len(members):
        raise ValueError("canonical universe contains duplicate canonical tickers")
    if len({item.provider_ticker for item in members}) != len(members):
        raise ValueError("canonical universe contains duplicate provider tickers")
    return members


def completed_session_ceiling(now: datetime) -> date:
    """Conservative US-session ceiling; provider data determines holiday gaps."""
    if now.tzinfo is None:
        raise ValueError("execution time must be timezone-aware")
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    candidate = eastern.date()
    if eastern.weekday() < 5 and eastern.time() < time(16, 15):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


class IncrementalEODHDPrices:
    """Reuse local captures and request only dates after the newest cached bar."""

    def __init__(
        self, *, cache_dir: str | Path = DEFAULT_CACHE,
        historical_dir: str | Path = PRICE_CACHE,
        forward_root: str | Path = FORWARD_PRICE_ROOT,
        api_key: str | None = None,
        session: requests.Session | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.historical_dir = Path(historical_dir).resolve()
        self.forward_root = Path(forward_root).resolve()
        self.api_key = api_key or os.getenv("EODHD_API_KEY")
        if not self.api_key:
            raise FunnelDataError("EODHD_API_KEY is not configured")
        self.session = session or requests.Session()
        if session is None:
            import truststore
            truststore.inject_into_ssl()
        self.now = now
        self.request_count = 0
        self.downloaded_bytes = 0
        self.cache_hits = 0

    def acquire(self, member: UniverseMember, through: date) -> PriceCapture:
        ticker = member.ticker
        candidates = (
            ("FUNNEL_CACHE", self.cache_dir / f"{ticker}.json"),
            ("FORWARD_PILOT_CACHE", self.forward_root / ticker / "prices" / f"{ticker}.json"),
            ("CANONICAL_HISTORICAL_CACHE", self.historical_dir / f"{ticker}.json"),
        )
        available: list[tuple[str, Path, list[dict[str, Any]]]] = []
        for label, path in candidates:
            if path.is_file():
                available.append((label, path, self._read_bars(path, ticker)))
        if not available:
            raise FunnelDataError(f"{ticker}: no local OHLCV baseline")
        source, _path, base = max(
            available, key=lambda item: max(str(row.get("date") or "") for row in item[2])
        )
        latest = max(date.fromisoformat(str(row["date"])) for row in base)
        if latest >= through:
            self.cache_hits += 1
            merged = self._clip_and_sort(base, through)
            self._write_cache(ticker, merged)
            return PriceCapture(ticker, merged, source, False, 0)

        start = latest + timedelta(days=1)
        raw = self._get(member, start, through)
        try:
            delta = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FunnelDataError(f"{ticker}: EODHD price response is not JSON") from exc
        if not isinstance(delta, list):
            raise FunnelDataError(f"{ticker}: EODHD price response has invalid shape")
        merged_by_date = {str(row.get("date") or ""): row for row in base}
        for row in delta:
            if not isinstance(row, dict) or not row.get("date"):
                raise FunnelDataError(f"{ticker}: EODHD price response contains a malformed row")
            merged_by_date[str(row["date"])] = row
        merged = self._clip_and_sort(list(merged_by_date.values()), through)
        self._write_raw_capture(ticker, start, through, raw)
        self._write_cache(ticker, merged)
        return PriceCapture(ticker, merged, f"{source}+EODHD_INCREMENTAL", True, len(raw))

    def _get(self, member: UniverseMember, start: date, through: date) -> bytes:
        self.request_count += 1
        try:
            response = self.session.get(
                EODHD_EOD.format(provider_ticker=member.provider_ticker),
                params={"api_token": self.api_key, "from": start.isoformat(),
                        "to": through.isoformat(), "fmt": "json"},
                timeout=45,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" HTTP {status}" if status is not None else ""
            raise FunnelDataError(
                f"{member.ticker}: EODHD price request failed{suffix} ({type(exc).__name__})"
            ) from exc
        payload = bytes(response.content)
        self.downloaded_bytes += len(payload)
        return payload

    @staticmethod
    def _read_bars(path: Path, ticker: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FunnelDataError(f"{ticker}: invalid local OHLCV cache {path}") from exc
        if not isinstance(value, list) or not value:
            raise FunnelDataError(f"{ticker}: empty local OHLCV cache {path}")
        return value

    @staticmethod
    def _clip_and_sort(bars: list[dict[str, Any]], through: date) -> list[dict[str, Any]]:
        return sorted(
            (row for row in bars if date.fromisoformat(str(row.get("date") or "")) <= through),
            key=lambda row: str(row["date"]),
        )

    def _write_cache(self, ticker: str, bars: list[dict[str, Any]]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{ticker}.json").write_text(
            json.dumps(bars, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _write_raw_capture(self, ticker: str, start: date, through: date, raw: bytes) -> None:
        directory = self.cache_dir / "raw"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{ticker}_{start.isoformat()}_{through.isoformat()}.json").write_bytes(raw)


def _boundary_gap(close: float, support: float) -> float:
    lower, upper = 0.99 * support, 1.02 * support
    if lower <= close <= upper:
        return 0.0
    boundary = lower if close < lower else upper
    return 100.0 * abs(close - boundary) / support


def classify_proximity(
    metrics: Mapping[str, float], regime: str,
) -> tuple[str, str, float, bool, bool, bool]:
    close = float(metrics["close"])
    supports = (float(metrics["sma20"]), float(metrics["sma50"]))
    pullback_band = any(0.99 * support <= close <= 1.02 * support for support in supports)
    pullback_gap = min(_boundary_gap(close, support) for support in supports)
    prior_high = float(metrics["prior_high_20d"])
    relative_volume = float(metrics["relative_volume"])
    near_breakout = close >= 0.99 * prior_high
    volume_confirmed = relative_volume >= 1.2
    breakout_price_gap = max(0.0, 100.0 * (0.99 * prior_high / close - 1.0))
    breakout_volume_gap = max(0.0, 100.0 * (1.2 - relative_volume) / 1.2)
    breakout_gap = max(breakout_price_gap, breakout_volume_gap)
    gap = min(pullback_gap, breakout_gap)

    if regime == "UPTREND" and (pullback_band or (near_breakout and volume_confirmed)):
        reason = ("BREAKOUT_PRICE_AND_VOLUME_READY" if near_breakout and volume_confirmed
                  else "PULLBACK_SUPPORT_BAND_READY")
        return "READY_TECHNICALLY", reason, 0.0, pullback_band, near_breakout, volume_confirmed
    nearest = "PULLBACK_BOUNDARY" if pullback_gap <= breakout_gap else "BREAKOUT_BOUNDARIES"
    if gap <= NEAR_BOUNDARY_GAP_PCT:
        return "NEAR", f"WITHIN_1PCT_OF_{nearest}", gap, pullback_band, near_breakout, volume_confirmed
    return "DISTANT", f"MORE_THAN_1PCT_FROM_{nearest}", gap, pullback_band, near_breakout, volume_confirmed


def screen_ticker(
    ticker: str, bars: Sequence[Mapping[str, object]], as_of: str,
) -> ScreenedTicker | Rejection:
    try:
        prepared = _prepare_bars(bars, as_of)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return Rejection(ticker, "MALFORMED_OHLCV", str(exc))
    if len(prepared) < MIN_BARS:
        return Rejection(ticker, "INSUFFICIENT_OHLCV", f"{len(prepared)} valid bars; need {MIN_BARS}")
    if str(prepared[-1]["date"]) != as_of:
        return Rejection(ticker, "STALE_OHLCV", f"latest={prepared[-1]['date']}; required={as_of}")
    try:
        metrics = _technical_metrics(prepared)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        return Rejection(ticker, "TECHNICAL_VALUES_UNAVAILABLE", str(exc))
    if (not all(math.isfinite(float(value)) for value in metrics.values())
            or any(float(metrics[key]) <= 0 for key in (
                "close", "sma20", "sma50", "sma200", "atr14", "average_volume_20d",
            ))):
        return Rejection(ticker, "TECHNICAL_VALUES_UNAVAILABLE", "non-finite or non-positive metric")
    regime = _regime(metrics)
    if regime == "DOWNTREND":
        return Rejection(ticker, "DOWNTREND", "frozen Setup V1 regime")
    state, reason, gap, pullback, breakout, volume = classify_proximity(metrics, regime)
    return ScreenedTicker(
        ticker=ticker, latest_session=as_of, close=float(metrics["close"]), regime=regime,
        sma20=float(metrics["sma20"]), sma50=float(metrics["sma50"]),
        sma200=float(metrics["sma200"]),
        distance_sma20_pct=float(metrics["distance_sma20_pct"]),
        distance_sma50_pct=float(metrics["distance_sma50_pct"]),
        distance_prior_high20_pct=100.0 * (
            float(metrics["close"]) / float(metrics["prior_high_20d"]) - 1.0
        ),
        atr14=float(metrics["atr14"]), rsi14=float(metrics["rsi14"]),
        relative_volume=float(metrics["relative_volume"]),
        pullback_band=pullback, near_breakout=breakout, volume_confirmed=volume,
        screening_state=state, primary_reason=reason, proximity_gap_pct=gap,
    )


def order_survivors(items: Sequence[ScreenedTicker]) -> list[ScreenedTicker]:
    """Order only by readiness, boundary distance, regime, then ticker."""
    state_order = {"READY_TECHNICALLY": 0, "NEAR": 1, "DISTANT": 2}
    return sorted(items, key=lambda item: (
        state_order[item.screening_state], item.proximity_gap_pct,
        0 if item.regime == "UPTREND" else 1, item.ticker,
    ))


def run_funnel(
    *, universe_path: str | Path = CANONICAL_UNIVERSE,
    output_path: str | Path | None = DEFAULT_OUTPUT,
    provider: IncrementalEODHDPrices | None = None,
    now: datetime | None = None,
) -> FunnelResult:
    started = runtime_clock.perf_counter()
    execution_time = now or datetime.now(timezone.utc)
    members = load_canonical_universe(universe_path)
    prices = provider or IncrementalEODHDPrices(now=lambda: execution_time)
    ceiling = completed_session_ceiling(execution_time)
    captures: dict[str, PriceCapture] = {}
    acquisition_rejections: list[Rejection] = []
    for member in members:
        try:
            captures[member.ticker] = prices.acquire(member, ceiling)
        except (FunnelDataError, OSError, ValueError) as exc:
            acquisition_rejections.append(
                Rejection(member.ticker, "OHLCV_UNAVAILABLE", str(exc)[:500])
            )
    latest_dates = [
        str(capture.bars[-1]["date"]) for capture in captures.values() if capture.bars
    ]
    if not latest_dates:
        raise FunnelDataError("no OHLCV capture produced a completed session")
    frequency = Counter(latest_dates)
    highest_frequency = max(frequency.values())
    as_of = max(day for day, count in frequency.items() if count == highest_frequency)

    survivors: list[ScreenedTicker] = []
    rejections = list(acquisition_rejections)
    valid_ohlcv = 0
    for member in members:
        capture = captures.get(member.ticker)
        if capture is None:
            continue
        result = screen_ticker(member.ticker, capture.bars, as_of)
        if isinstance(result, Rejection):
            rejections.append(result)
        else:
            valid_ohlcv += 1
            survivors.append(result)
    # Downtrends passed data quality before their stage-1 regime rejection.
    downtrends = sum(item.reason == "DOWNTREND" for item in rejections)
    data_rejected = len(rejections) - downtrends
    valid_ohlcv += downtrends
    ordered = order_survivors(survivors)
    shortlist = tuple(item for item in ordered if item.screening_state != "DISTANT")
    rejection_by_ticker = {item.ticker: item for item in rejections}
    survivor_by_ticker = {item.ticker: item for item in survivors}
    control: dict[str, str] = {}
    for ticker in CONTROL_TICKERS:
        if ticker in rejection_by_ticker:
            control[ticker] = f"REJECTED: {rejection_by_ticker[ticker].reason}"
        elif ticker in survivor_by_ticker:
            control[ticker] = survivor_by_ticker[ticker].screening_state
        else:
            control[ticker] = "NOT_IN_UNIVERSE"
    counts = {
        "universe": len(members),
        "valid_ohlcv": valid_ohlcv,
        "rejected_data_quality": data_rejected,
        "downtrend_rejected": downtrends,
        "neutral_survivors": sum(item.regime == "NEUTRAL" for item in survivors),
        "uptrend_survivors": sum(item.regime == "UPTREND" for item in survivors),
        "ready_technically": sum(item.screening_state == "READY_TECHNICALLY" for item in survivors),
        "near": sum(item.screening_state == "NEAR" for item in survivors),
        "distant": sum(item.screening_state == "DISTANT" for item in survivors),
        "final_shortlist_size": len(shortlist),
    }
    result = FunnelResult(
        universe_source=str(Path(universe_path).resolve()),
        latest_completed_session=as_of, counts=counts, shortlist=shortlist,
        survivors=tuple(ordered), rejections=tuple(sorted(rejections, key=lambda item: item.ticker)),
        control_group=control, eodhd_requests=prices.request_count,
        downloaded_bytes=prices.downloaded_bytes, cache_hits=prices.cache_hits,
        runtime_seconds=runtime_clock.perf_counter() - started,
    )
    if output_path is not None:
        _write_output(Path(output_path), result, captures)
    return result


def _write_output(path: Path, result: FunnelResult, captures: Mapping[str, PriceCapture]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_name": "trinity.pre-research-funnel", "schema_version": "1",
        "universe_source": result.universe_source,
        "latest_completed_session": result.latest_completed_session,
        "policy": {
            "minimum_bars": MIN_BARS,
            "near_boundary_gap_pct": NEAR_BOUNDARY_GAP_PCT,
            "formulas": "trinity.usa_setup_v1 frozen technical functions",
            "purpose": "proximity screening only; not a trading signal",
        },
        "counts": result.counts,
        "cost": {
            "eodhd_requests": result.eodhd_requests,
            "downloaded_bytes": result.downloaded_bytes,
            "cache_hits": result.cache_hits,
            "runtime_seconds": result.runtime_seconds,
            "llm_calls": 0, "llm_tokens": 0, "news_requests": 0,
            "sec_requests": 0, "telegram_requests": 0,
        },
        "control_group": result.control_group,
        "shortlist": [asdict(item) for item in result.shortlist],
        "survivors": [asdict(item) for item in result.survivors],
        "rejections": [asdict(item) for item in result.rejections],
        "inputs": {
            ticker: {
                "source": capture.source,
                "bar_count": len(capture.bars),
                "sha256": hashlib.sha256(json.dumps(
                    capture.bars, ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest(),
            }
            for ticker, capture in sorted(captures.items())
        },
    }
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def render_counts(result: FunnelResult) -> str:
    labels = (
        ("Universe", "universe"), ("Valid OHLCV", "valid_ohlcv"),
        ("Rejected data quality", "rejected_data_quality"),
        ("Downtrend rejected", "downtrend_rejected"),
        ("Neutral survivors", "neutral_survivors"),
        ("Uptrend survivors", "uptrend_survivors"),
        ("READY_TECHNICALLY", "ready_technically"), ("NEAR", "near"),
        ("DISTANT", "distant"), ("Final shortlist size", "final_shortlist_size"),
    )
    return "\n".join(f"{label}: {result.counts[key]}" for label, key in labels)


def render_shortlist(result: FunnelResult, limit: int = 50) -> str:
    headers = (
        "Rank", "Ticker", "Latest session", "Close", "Regime", "SMA20", "SMA50",
        "SMA200", "Dist SMA20 %", "Dist SMA50 %", "Dist high20 %", "ATR14",
        "RSI14", "Rel volume", "Pullback band", "Near breakout", "State", "Reason",
    )
    rows: list[tuple[object, ...]] = [headers]
    for rank, item in enumerate(result.shortlist[:limit], 1):
        rows.append((
            rank, item.ticker, item.latest_session, f"{item.close:.4f}", item.regime,
            f"{item.sma20:.4f}", f"{item.sma50:.4f}", f"{item.sma200:.4f}",
            f"{item.distance_sma20_pct:.4f}", f"{item.distance_sma50_pct:.4f}",
            f"{item.distance_prior_high20_pct:.4f}", f"{item.atr14:.4f}",
            f"{item.rsi14:.4f}", f"{item.relative_volume:.4f}",
            "YES" if item.pullback_band else "NO", "YES" if item.near_breakout else "NO",
            item.screening_state, item.primary_reason,
        ))
    widths = [max(len(str(row[index])) for row in rows) for index in range(len(headers))]
    return "\n".join(
        " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the price-only TRINITY pre-research funnel")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args(argv)
    try:
        result = run_funnel(provider=IncrementalEODHDPrices(cache_dir=args.cache_dir),
                            output_path=args.output)
    except (FunnelDataError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(render_counts(result))
    print()
    print(render_shortlist(result))
    hidden = max(0, len(result.shortlist) - 50)
    print(f"\nAdditional qualifying names not displayed: {hidden}")
    print("\nControl group:")
    for ticker in CONTROL_TICKERS:
        print(f"{ticker}: {result.control_group[ticker]}")
    print(
        f"\nEODHD requests: {result.eodhd_requests} | Downloaded bytes: "
        f"{result.downloaded_bytes} | Cache hits: {result.cache_hits} | "
        f"Runtime seconds: {result.runtime_seconds:.3f} | LLM calls/tokens: 0/0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
