"""Portable runtime paths with the historical workstation layout as fallback."""

from __future__ import annotations

import os
from pathlib import Path


LEGACY_BASELINE_ROOT = Path("C:/Users/giopa/trinity-scanner-v1/historical/raw")
PRICE_DATASET = "eodhd_prices_518_daily_20220101_20260913_v1"
NEWS_DATASET = "eodhd_news/eodhd_news_historical_20250101_20260912_v2"


def data_root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("TRINITY_DATA_ROOT", "data")).expanduser()


def baseline_root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("TRINITY_BASELINE_ROOT")
    return Path(configured).expanduser() if configured else LEGACY_BASELINE_ROOT


def run_root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("TRINITY_RUN_ROOT")
    return Path(configured).expanduser() if configured else data_root(env) / "local" / "shadow_runs"


def price_cache(environ: dict[str, str] | None = None) -> Path:
    return baseline_root(environ) / PRICE_DATASET / "provider_raw"


def news_cache(environ: dict[str, str] | None = None) -> Path:
    return baseline_root(environ) / NEWS_DATASET / "records"
