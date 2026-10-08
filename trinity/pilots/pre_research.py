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
from pathlib import Path
import time as runtime_clock
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from trinity.twelvedata_prices import PRICE_PROVIDER, ValidatedPriceSnapshot
from trinity.usa_issuer_registry import DEFAULT_REGISTRY, load_registry
from trinity.usa_setup_v1 import (
    _prepare_bars,
    _regime,
    _technical_metrics,
)


CANONICAL_UNIVERSE = DEFAULT_REGISTRY
DEFAULT_CACHE = Path("data/local/price_snapshot/normalized")
DEFAULT_OUTPUT = Path("data/local/pre_research_funnel_latest.json")
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
    classification: str = "ACTIVE_COMPLETE"


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
    price_provider: str
    price_snapshot_id: str
    price_snapshot_session: str
    price_snapshot_sha256: str
    price_requests: int
    downloaded_bytes: int
    cache_hits: int
    runtime_seconds: float


def load_canonical_universe(path: str | Path = CANONICAL_UNIVERSE) -> tuple[UniverseMember, ...]:
    source = Path(path)
    if source.suffix == ".fixture" or source.suffix == ".json":
        records = load_registry(source).supported
        members = tuple(sorted(
            (UniverseMember(item.ticker, item.provider_symbol) for item in records),
            key=lambda item: item.ticker,
        ))
    else:
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


class SnapshotPrices:
    """Read production prices only from one validated immutable snapshot."""

    def __init__(self, snapshot: ValidatedPriceSnapshot) -> None:
        self.snapshot = snapshot
        self.request_count = 0
        self.downloaded_bytes = 0
        self.cache_hits = 0
        self.price_provider = PRICE_PROVIDER
        self.price_snapshot_id = snapshot.snapshot_id
        self.price_snapshot_session = snapshot.target_session
        self.price_snapshot_sha256 = snapshot.snapshot_sha256

    def acquire(self, member: UniverseMember, through: date) -> PriceCapture:
        if through.isoformat() != self.snapshot.target_session:
            raise FunnelDataError(
                f"{member.ticker}: analytical ceiling {through} differs from snapshot session "
                f"{self.snapshot.target_session}"
            )
        entry = self.snapshot.entry(member.ticker)
        bars = self.snapshot.bars(member.ticker)
        self.cache_hits += 1
        return PriceCapture(
            member.ticker, bars, self.snapshot.snapshot_id, False, 0,
            str(entry["classification"]),
        )


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
    provider: object | None = None,
    now: datetime | None = None,
) -> FunnelResult:
    started = runtime_clock.perf_counter()
    execution_time = now or datetime.now(timezone.utc)
    members = load_canonical_universe(universe_path)
    if provider is None:
        raise FunnelDataError("a validated price snapshot provider is required")
    prices = provider
    snapshot_session = getattr(prices, "price_snapshot_session", None)
    ceiling = (
        date.fromisoformat(str(snapshot_session)) if snapshot_session
        else completed_session_ceiling(execution_time)
    )
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
        str(capture.bars[-1]["date"]) for capture in captures.values()
        if capture.bars and capture.classification == "ACTIVE_COMPLETE"
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
        if capture.classification == "CORPORATE_ACTION_NO_LONGER_TRADING":
            rejections.append(Rejection(
                member.ticker, "CORPORATE_ACTION_NO_LONGER_TRADING",
                f"last normal session={capture.bars[-1]['date'] if capture.bars else '-'}",
            ))
            continue
        result = screen_ticker(member.ticker, capture.bars, as_of)
        if isinstance(result, Rejection):
            rejections.append(result)
        else:
            valid_ohlcv += 1
            survivors.append(result)
    # Downtrends passed data quality before their stage-1 regime rejection.
    downtrends = sum(item.reason == "DOWNTREND" for item in rejections)
    explicit_exceptions = sum(
        item.reason == "CORPORATE_ACTION_NO_LONGER_TRADING" for item in rejections
    )
    data_rejected = len(rejections) - downtrends - explicit_exceptions
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
    if explicit_exceptions:
        counts["explicit_price_exceptions"] = explicit_exceptions
    result = FunnelResult(
        universe_source=str(Path(universe_path).resolve()),
        latest_completed_session=as_of, counts=counts, shortlist=shortlist,
        survivors=tuple(ordered), rejections=tuple(sorted(rejections, key=lambda item: item.ticker)),
        control_group=control,
        price_provider=str(getattr(prices, "price_provider", "TEST_PROVIDER")),
        price_snapshot_id=str(getattr(prices, "price_snapshot_id", "TEST_SNAPSHOT")),
        price_snapshot_session=str(getattr(prices, "price_snapshot_session", as_of)),
        price_snapshot_sha256=str(getattr(prices, "price_snapshot_sha256", "TEST_HASH")),
        price_requests=int(getattr(prices, "request_count", 0)),
        downloaded_bytes=int(getattr(prices, "downloaded_bytes", 0)),
        cache_hits=int(getattr(prices, "cache_hits", 0)),
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
        "price_snapshot": {
            "provider": result.price_provider,
            "snapshot_id": result.price_snapshot_id,
            "session": result.price_snapshot_session,
            "sha256": result.price_snapshot_sha256,
        },
        "policy": {
            "minimum_bars": MIN_BARS,
            "near_boundary_gap_pct": NEAR_BOUNDARY_GAP_PCT,
            "formulas": "trinity.usa_setup_v1 frozen technical functions",
            "purpose": "proximity screening only; not a trading signal",
        },
        "counts": result.counts,
        "cost": {
            "price_requests": result.price_requests,
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
                "classification": capture.classification,
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
    parser.add_argument("--snapshot-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_funnel(
            provider=SnapshotPrices(ValidatedPriceSnapshot(args.snapshot_root)),
            output_path=args.output,
        )
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
        f"\nPrice provider: {result.price_provider} | Snapshot: {result.price_snapshot_id} | "
        f"Price requests: {result.price_requests} | Downloaded bytes: "
        f"{result.downloaded_bytes} | Cache hits: {result.cache_hits} | "
        f"Runtime seconds: {result.runtime_seconds:.3f} | LLM calls/tokens: 0/0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
