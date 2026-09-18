"""Unit tests for the independent TRINITY outcome engine."""

from __future__ import annotations

from datetime import date, timedelta
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

    def test_insufficient_data_for_plus_60_has_clear_error(self) -> None:
        bars = _bars(count=61)

        with self.assertRaisesRegex(InsufficientDataError, r"\+60.*only 55"):
            calculate_event_outcomes("ACME", bars[5]["date"], "AFTER_MARKET", bars)

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


if __name__ == "__main__":
    unittest.main()
