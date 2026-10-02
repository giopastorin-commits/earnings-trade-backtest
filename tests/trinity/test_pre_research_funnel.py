from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from trinity.pilots.pre_research import (
    IncrementalEODHDPrices,
    PriceCapture,
    Rejection,
    ScreenedTicker,
    UniverseMember,
    classify_proximity,
    order_survivors,
    run_funnel,
    screen_ticker,
)


AS_OF = "2026-10-01"


def _bars(closes, *, end=AS_OF, latest_volume=1000.0):
    last = date.fromisoformat(end)
    first = last - timedelta(days=len(closes) - 1)
    return [
        {
            "date": (first + timedelta(days=index)).isoformat(),
            "open": close - 0.2, "high": close + 1.0,
            "low": close - 1.0, "close": close,
            "volume": latest_volume if index == len(closes) - 1 else 1000.0,
        }
        for index, close in enumerate(closes)
    ]


def _uptrend(count=240):
    return [100.0 + 0.3 * index for index in range(count)]


def _downtrend(count=240):
    return [200.0 - 0.3 * index for index in range(count)]


def _metrics(**overrides):
    value = {
        "close": 100.0, "sma20": 100.0, "sma50": 90.0,
        "sma200": 80.0, "prior_high_20d": 110.0, "relative_volume": 1.0,
    }
    value.update(overrides)
    return value


def test_minimum_201_bars():
    rejected = screen_ticker("TEST", _bars(_uptrend(200)), AS_OF)
    assert isinstance(rejected, Rejection)
    assert rejected.reason == "INSUFFICIENT_OHLCV"
    accepted = screen_ticker("TEST", _bars(_uptrend(201)), AS_OF)
    assert isinstance(accepted, ScreenedTicker)


def test_stale_data_rejection():
    result = screen_ticker("TEST", _bars(_uptrend(), end="2026-09-30"), AS_OF)
    assert isinstance(result, Rejection)
    assert result.reason == "STALE_OHLCV"


def test_malformed_ohlcv_rejection():
    bars = _bars(_uptrend())
    bars[-1]["high"] = bars[-1]["close"] - 1
    result = screen_ticker("TEST", bars, AS_OF)
    assert isinstance(result, Rejection)
    assert result.reason == "MALFORMED_OHLCV"


def test_downtrend_rejected_and_neutral_uptrend_survive():
    down = screen_ticker("DOWN", _bars(_downtrend()), AS_OF)
    neutral = screen_ticker("FLAT", _bars([100.0] * 240), AS_OF)
    up = screen_ticker("UP", _bars(_uptrend()), AS_OF)
    assert isinstance(down, Rejection) and down.reason == "DOWNTREND"
    assert isinstance(neutral, ScreenedTicker) and neutral.regime == "NEUTRAL"
    assert isinstance(up, ScreenedTicker) and up.regime == "UPTREND"


@pytest.mark.parametrize("close", [99.0, 102.0])
def test_exact_setup_pullback_band_boundary(close):
    state, _reason, gap, inside, _breakout, _volume = classify_proximity(
        _metrics(close=close, sma20=100.0), "UPTREND"
    )
    assert inside is True
    assert gap == 0.0
    assert state == "READY_TECHNICALLY"


def test_exact_setup_near_breakout_boundary():
    state, reason, gap, _inside, breakout, volume = classify_proximity(
        _metrics(close=99.0, sma20=80.0, sma50=80.0,
                 prior_high_20d=100.0, relative_volume=1.2),
        "UPTREND",
    )
    assert breakout is True and volume is True
    assert state == "READY_TECHNICALLY"
    assert reason == "BREAKOUT_PRICE_AND_VOLUME_READY"
    assert gap == 0.0


def test_deterministic_readiness_order():
    base = ScreenedTicker(
        ticker="ZZZ", latest_session=AS_OF, close=100, regime="NEUTRAL",
        sma20=100, sma50=100, sma200=100, distance_sma20_pct=0,
        distance_sma50_pct=0, distance_prior_high20_pct=0, atr14=1,
        rsi14=50, relative_volume=1, pullback_band=False,
        near_breakout=False, volume_confirmed=False, screening_state="DISTANT",
        primary_reason="test", proximity_gap_pct=3,
    )
    items = [
        replace(base, ticker="DDD"),
        replace(base, ticker="NNEU", screening_state="NEAR", proximity_gap_pct=0.5),
        replace(base, ticker="NUP", screening_state="NEAR", proximity_gap_pct=0.5,
                regime="UPTREND"),
        replace(base, ticker="BBB", screening_state="READY_TECHNICALLY",
                proximity_gap_pct=0),
        replace(base, ticker="AAA", screening_state="READY_TECHNICALLY",
                proximity_gap_pct=0),
    ]
    assert [item.ticker for item in order_survivors(items)] == [
        "AAA", "BBB", "NUP", "NNEU", "DDD",
    ]


def test_future_data_is_not_used():
    bars = _bars(_uptrend())
    baseline = screen_ticker("TEST", bars, AS_OF)
    future = {**bars[-1], "date": "2026-10-02", "open": 999.0,
              "high": 1001.0, "low": 998.0, "close": 1000.0,
              "volume": 9_999_999.0}
    assert screen_ticker("TEST", [*bars, future], AS_OF) == baseline


def test_price_provider_only_calls_eodhd_eod_and_reuses_cache(tmp_path):
    historical = tmp_path / "historical"
    historical.mkdir()
    baseline = _bars(_uptrend(), end="2026-09-30")
    (historical / "TEST.json").write_text(json.dumps(baseline), encoding="utf-8")
    delta = [{**baseline[-1], "date": AS_OF, "close": baseline[-1]["close"] + 0.3,
              "open": baseline[-1]["open"] + 0.3,
              "high": baseline[-1]["high"] + 0.3,
              "low": baseline[-1]["low"] + 0.3}]
    calls = []

    class Response:
        content = json.dumps(delta).encode()
        def raise_for_status(self):
            return None

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    provider = IncrementalEODHDPrices(
        cache_dir=tmp_path / "cache", historical_dir=historical,
        forward_root=tmp_path / "forward", api_key="test-only", session=Session(),
    )
    member = UniverseMember("TEST", "TEST.US")
    first = provider.acquire(member, date.fromisoformat(AS_OF))
    second = provider.acquire(member, date.fromisoformat(AS_OF))
    assert len(calls) == 1
    assert calls[0][0] == "https://eodhd.com/api/eod/TEST.US"
    assert first.requested is True and second.requested is False
    assert provider.request_count == 1 and provider.cache_hits == 1


def test_funnel_has_zero_llm_telegram_news_or_sec_calls(tmp_path, monkeypatch):
    universe = tmp_path / "universe.csv"
    universe.write_text(
        "canonical_ticker,provider_ticker,mapping_reason\nTEST,TEST.US,test\n",
        encoding="utf-8",
    )

    class Provider:
        request_count = 1
        downloaded_bytes = 123
        cache_hits = 0
        def acquire(self, member, through):
            assert member.ticker == "TEST"
            return PriceCapture("TEST", _bars(_uptrend(), end=through.isoformat()),
                                "MOCK_EODHD", True, 123)

    forbidden = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: forbidden.append("llm"))
    monkeypatch.setattr("requests.post", lambda *a, **k: forbidden.append("telegram"))
    result = run_funnel(
        universe_path=universe, output_path=None, provider=Provider(),
        now=datetime(2026, 10, 2, 12, tzinfo=timezone.utc),
    )
    assert result.counts["universe"] == 1
    assert forbidden == []
    assert result.eodhd_requests == 1
