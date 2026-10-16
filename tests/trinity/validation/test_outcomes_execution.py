from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from trinity.validation.contracts import SetupObservation
from trinity.validation.execution import evaluate_execution_lifecycle
from trinity.validation.outcomes import (
    BenchmarkSeries, calculate_selection_outcome, net_return_after_round_trip_cost,
)


SHA = "a" * 64


def _bars(count=70, start=date(2026, 10, 12), base=100.0):
    rows = []
    current = start
    while len(rows) < count:
        if current.weekday() < 5:
            index = len(rows)
            rows.append({
                "date": current.isoformat(),
                "tradable_at": datetime(current.year, current.month, current.day, 13, 30,
                                         tzinfo=timezone.utc).isoformat(),
                "open": base + index, "high": base + index + 1,
                "low": base + index - 1, "close": base + index + 0.5,
            })
        current += timedelta(days=1)
    return rows


def _outcome(horizon, stock=None, benchmark=None):
    benchmark_series = None if benchmark is None else BenchmarkSeries(
        "SPY", "TOTAL_RETURN", "spy-snapshot", "b" * 64, tuple(benchmark)
    )
    return calculate_selection_outcome(
        run_id="run", ticker="ABC", cohort="SCANNER",
        decision_timestamp_utc="2026-10-12T12:00:00Z", horizon_sessions=horizon,
        stock_bars=stock if stock is not None else _bars(),
        benchmark=benchmark_series, source_snapshot_id="snap", source_snapshot_sha256=SHA,
    )


def test_first_tradable_price_is_strictly_after_decision_and_no_lookahead():
    rows = _bars()
    result = _outcome(5, rows, _bars(base=400))
    assert result.first_tradable_session == "2026-10-12"
    assert result.first_tradable_price == 100.0
    assert result.future_session == rows[4]["date"]
    assert result.maturity_status == "COMPLETE"


def test_horizons_5_20_60_mature_only_with_required_sessions():
    assert _outcome(5, _bars(5), _bars(5, base=400)).maturity_status == "COMPLETE"
    assert _outcome(20, _bars(20), _bars(20, base=400)).maturity_status == "COMPLETE"
    assert _outcome(60, _bars(60), _bars(60, base=400)).maturity_status == "COMPLETE"
    assert _outcome(20, _bars(19), _bars(19, base=400)).maturity_status == "PENDING"


def test_missing_benchmark_is_explicit_data_quality_failure_when_stock_mature():
    result = _outcome(5, _bars(5), None)
    assert result.maturity_status == "DATA_QUALITY_FAILURE"
    assert result.benchmark_return_pct is None and result.active_return_pct is None
    assert result.quality_flags == ("BENCHMARK_SERIES_MISSING",)


def test_benchmark_contract_requires_spy_total_return_and_tracks_lineage():
    with pytest.raises(Exception, match="SPY total return"):
        BenchmarkSeries("SPY", "PRICE_RETURN", "snap", "b" * 64, tuple(_bars()))
    result = _outcome(5, _bars(5), _bars(5, base=400))
    assert result.benchmark_snapshot_id == "spy-snapshot"
    assert result.benchmark_snapshot_sha256 == "b" * 64


def test_frozen_round_trip_cost_assumptions():
    assert net_return_after_round_trip_cost(5.0, 10) == pytest.approx(4.8)
    assert net_return_after_round_trip_cost(5.0, 0) == 5.0
    assert net_return_after_round_trip_cost(5.0, 25) == pytest.approx(4.5)


def _setup(**changes):
    values = dict(
        run_id="run", ticker="ABC", setup_id=None, setup_sha256=SHA,
        setup_type="PULLBACK", setup_timestamp="2026-10-09",
        entry_condition="BUY ON PULLBACK 99-100", entry_level=100.0,
        entry_lower=99.0, entry_upper=100.0, stop=95.0, tp1=110.0, tp2=115.0,
        original_rr1=2.0, original_rr2=3.0, expiry=None,
    )
    values.update(changes)
    return SetupObservation(**values)


def test_never_triggered_is_not_a_loss_and_expiry_is_explicit():
    bars = [{"date": "2026-10-12", "open": 105, "high": 106, "low": 104, "close": 105}]
    assert evaluate_execution_lifecycle(_setup(), bars, through_session="2026-10-12").status == "NOT_TRIGGERED"
    expired = evaluate_execution_lifecycle(
        _setup(expiry="2026-10-12"), bars, through_session="2026-10-12"
    )
    assert expired.status == "EXPIRED" and expired.result_r is None


def test_invalidated_before_entry_is_distinct():
    bars = [{"date": "2026-10-12", "open": 96, "high": 98, "low": 94, "close": 95}]
    result = evaluate_execution_lifecycle(_setup(), bars, through_session="2026-10-12")
    assert result.status == "INVALIDATED_BEFORE_ENTRY"


def test_same_bar_stop_and_target_is_ambiguous_and_stop_first():
    bars = [{"date": "2026-10-12", "open": 100, "high": 111, "low": 94, "close": 105}]
    result = evaluate_execution_lifecycle(_setup(), bars, through_session="2026-10-12")
    assert result.status == "STOPPED"
    assert result.ambiguous_ohlc is True
    assert result.result_r == -1.0
    assert result.mfe_r == 2.2 and result.mae_r == -1.2
