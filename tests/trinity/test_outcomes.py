"""Unit tests for the independent TRINITY outcome engine."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest

from trinity.outcomes import InsufficientDataError, calculate_event_outcomes


def _weekdays(start: date, count: int) -> list[date]:
    sessions: list[date] = []
    current = start
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions


def _bars(start: date = date(2024, 1, 2), count: int = 70) -> list[dict[str, object]]:
    return [
        {
            "date": session.isoformat(),
            "open": 99.5 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
            "volume": 1_000.0 + index * 10,
        }
        for index, session in enumerate(_weekdays(start, count))
    ]


class EventOutcomeTests(unittest.TestCase):
    def test_after_market_uses_event_day_close_and_next_session_as_plus_one(self) -> None:
        bars = _bars()
        result = calculate_event_outcomes(
            "ACME", bars[5]["date"], "AFTER_MARKET", bars, horizons=(1, 5)
        )

        self.assertEqual(result[0]["reference_session_date"], bars[5]["date"])
        self.assertEqual(result[0]["reference_price"], bars[5]["close"])
        self.assertEqual(result[0]["future_session_date"], bars[6]["date"])
        self.assertEqual(result[0]["future_close"], bars[6]["close"])

    def test_pre_market_uses_previous_close_and_event_session_as_plus_one(self) -> None:
        bars = _bars()
        result = calculate_event_outcomes(
            "ACME", bars[5]["date"], "PRE_MARKET", bars, horizons=(1,)
        )[0]

        self.assertEqual(result["reference_session_date"], bars[4]["date"])
        self.assertEqual(result["reference_price"], bars[4]["close"])
        self.assertEqual(result["future_session_date"], bars[5]["date"])

    def test_unknown_is_explicitly_ambiguous_without_assumed_prices(self) -> None:
        result = calculate_event_outcomes(
            "ACME", "2024-01-08", "UNKNOWN", _bars(), horizons=(1, 5)
        )

        self.assertEqual(len(result), 2)
        self.assertTrue(all(row["timing_ambiguous"] is True for row in result))
        self.assertTrue(all(row["reference_price"] is None for row in result))
        self.assertTrue(all(row["future_close"] is None for row in result))

    def test_insufficient_data_for_plus_60_is_pending(self) -> None:
        bars = _bars(count=61)

        result = calculate_event_outcomes("ACME", bars[5]["date"], "AFTER_MARKET", bars)

        self.assertEqual(result[-1]["horizon_sessions"], 60)
        self.assertEqual(result[-1]["outcome_status"], "PENDING")
        self.assertIsNone(result[-1]["future_close"])

    def test_spy_benchmark_and_excess_return(self) -> None:
        bars = _bars()
        benchmark = [
            {"date": row["date"], "close": 400.0 + index * 2}
            for index, row in enumerate(bars)
        ]
        result = calculate_event_outcomes(
            "ACME",
            bars[5]["date"],
            "AFTER_MARKET",
            bars,
            benchmark_ohlcv=benchmark,
            horizons=(1,),
        )[0]

        expected_benchmark = ((412.0 / 410.0) - 1.0) * 100.0
        benchmark_return = result["benchmark_return_pct"]
        raw_return = result["raw_return_pct"]
        excess_return = result["excess_return_pct"]
        self.assertIsInstance(benchmark_return, float)
        self.assertIsInstance(raw_return, float)
        self.assertIsInstance(excess_return, float)
        self.assertAlmostEqual(benchmark_return, expected_benchmark)  # type: ignore[arg-type]
        self.assertAlmostEqual(
            excess_return, raw_return - expected_benchmark  # type: ignore[operator, arg-type]
        )

    def test_weekend_event_uses_only_present_sessions(self) -> None:
        bars = _bars(start=date(2024, 1, 1))
        friday_index = next(
            index for index, row in enumerate(bars) if row["date"] == "2024-01-05"
        )
        result = calculate_event_outcomes(
            "ACME", "2024-01-06", "AFTER_MARKET", bars, horizons=(1,)
        )[0]

        self.assertEqual(result["reference_session_date"], "2024-01-05")
        self.assertEqual(result["future_session_date"], "2024-01-08")
        self.assertEqual(result["reference_price"], bars[friday_index]["close"])

    def test_after_market_reference_has_no_look_ahead(self) -> None:
        bars = _bars()
        event_index = 5
        bars[event_index + 1]["close"] = 500.0
        bars[event_index + 1]["high"] = 501.0
        result = calculate_event_outcomes(
            "ACME", bars[event_index]["date"], "AFTER_MARKET", bars, horizons=(1,)
        )[0]

        self.assertEqual(result["reference_price"], bars[event_index]["close"])
        self.assertNotEqual(result["reference_price"], bars[event_index + 1]["close"])

    def test_partial_horizons_complete_and_pending(self) -> None:
        bars = _bars(count=11)
        result = calculate_event_outcomes(
            "ACME", bars[5]["date"], "AFTER_MARKET", bars
        )

        statuses = {
            row["horizon_sessions"]: row["outcome_status"] for row in result
        }
        self.assertEqual(
            statuses,
            {1: "COMPLETE", 5: "COMPLETE", 20: "PENDING", 60: "PENDING"},
        )

    def test_after_market_non_trading_date_records_resolution(self) -> None:
        result = calculate_event_outcomes(
            "ACME", "2024-01-06", "AFTER_MARKET", _bars(date(2024, 1, 1)), horizons=(1,)
        )[0]

        self.assertEqual(result["event_date_received"], "2024-01-06")
        self.assertEqual(result["reference_session_date"], "2024-01-05")
        self.assertEqual(
            result["reference_resolution"],
            "LAST_SESSION_BEFORE_NON_TRADING_EVENT",
        )

    def test_pre_market_non_trading_date_records_resolution(self) -> None:
        result = calculate_event_outcomes(
            "ACME", "2024-01-06", "PRE_MARKET", _bars(date(2024, 1, 1)), horizons=(1,)
        )[0]

        self.assertEqual(result["event_date_received"], "2024-01-06")
        self.assertEqual(result["reference_session_date"], "2024-01-05")
        self.assertEqual(result["future_session_date"], "2024-01-08")
        self.assertEqual(
            result["reference_resolution"],
            "PREVIOUS_SESSION_BEFORE_NEXT_TRADING_SESSION",
        )

    def test_timezone_aware_datetime_is_preserved_without_conversion(self) -> None:
        event_at = datetime(
            2024,
            1,
            9,
            0,
            30,
            tzinfo=timezone(timedelta(hours=9)),
        )
        result = calculate_event_outcomes(
            "ACME", event_at, "PRE_MARKET", _bars(), horizons=(1,)
        )[0]

        self.assertEqual(result["event_at"], "2024-01-09T00:30:00+09:00")
        self.assertEqual(result["event_date_received"], "2024-01-09")

    def test_missing_reference_still_raises_insufficient_data(self) -> None:
        bars = _bars(start=date(2024, 1, 8))

        with self.assertRaisesRegex(InsufficientDataError, "no session exists"):
            calculate_event_outcomes(
                "ACME", "2024-01-06", "AFTER_MARKET", bars, horizons=(1,)
            )

    def test_available_outcome_has_complete_status(self) -> None:
        bars = _bars()
        result = calculate_event_outcomes(
            "ACME", bars[5]["date"], "AFTER_MARKET", bars, horizons=(1,)
        )[0]

        self.assertEqual(result["outcome_status"], "COMPLETE")
        self.assertEqual(result["reference_resolution"], "EVENT_SESSION_CLOSE")

    def test_pending_outcome_has_all_future_metrics_null(self) -> None:
        bars = _bars(count=7)
        result = calculate_event_outcomes(
            "ACME", bars[5]["date"], "AFTER_MARKET", bars, horizons=(5,)
        )[0]

        self.assertEqual(result["outcome_status"], "PENDING")
        nullable_fields = (
            "future_session_date",
            "future_close",
            "raw_return_pct",
            "benchmark_return_pct",
            "excess_return_pct",
            "absolute_return_pct",
            "max_favorable_excursion_pct",
            "max_adverse_excursion_pct",
            "volume_change_pct",
            "realized_volatility",
        )
        self.assertTrue(all(result[field] is None for field in nullable_fields))


if __name__ == "__main__":
    unittest.main()
