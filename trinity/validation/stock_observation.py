"""Read-only adapter for verified canonical stock observation snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from trinity.twelvedata_prices import (
    ADJUSTMENT, INTERVAL, PRICE_PROVIDER, PriceSnapshotError, ValidatedPriceSnapshot,
)

from .contracts import ValidationContractError
from .market_calendar import CALENDAR_NAME, CALENDAR_VERSION, add_tradable_at


class StockObservationError(ValidationContractError):
    pass


@dataclass(frozen=True)
class StockObservationSeries:
    ticker: str
    provider: str
    adjustment: str
    interval: str
    snapshot_id: str
    snapshot_sha256: str
    observation_snapshot_target_session: str
    acquisition_completed_at_utc: str
    calendar: str
    calendar_version: str
    bars: tuple[Mapping[str, object], ...]


def load_stock_observation_snapshot(
    root: str | Path, *, ticker: str,
    expected_observation_target_session: str,
) -> StockObservationSeries:
    """Verify the immutable snapshot before adapting a detached ticker series."""

    try:
        snapshot = ValidatedPriceSnapshot(
            root, require_ready=True, enforce_canonical_universe=False,
        )
    except PriceSnapshotError as exc:
        raise StockObservationError(f"invalid canonical stock observation snapshot: {exc}") from exc
    if snapshot.target_session != expected_observation_target_session:
        raise StockObservationError("stock observation target session mismatch")
    manifest = snapshot.manifest
    if (
        manifest.get("provider") != PRICE_PROVIDER
        or manifest.get("interval") != INTERVAL
        or manifest.get("adjust") != ADJUSTMENT
    ):
        raise StockObservationError("stock observation provider contract mismatch")
    try:
        bars = snapshot.bars(ticker)
    except (PriceSnapshotError, OSError) as exc:
        raise StockObservationError("stock ticker missing from observation snapshot") from exc
    try:
        adapted = add_tradable_at(bars)
    except ValidationContractError as exc:
        raise StockObservationError(str(exc)) from exc
    return StockObservationSeries(
        ticker=ticker, provider=PRICE_PROVIDER, adjustment=ADJUSTMENT,
        interval=INTERVAL, snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.snapshot_sha256,
        observation_snapshot_target_session=snapshot.target_session,
        acquisition_completed_at_utc=snapshot.acquisition_completed_at,
        calendar=CALENDAR_NAME, calendar_version=CALENDAR_VERSION, bars=adapted,
    )
