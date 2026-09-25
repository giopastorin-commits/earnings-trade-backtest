"""Synthetic regression tests for deterministic USA Setup Engine V1."""

import unittest

from trinity.usa_setup_v1 import build_setup


AS_OF = "2026-09-12"


def _thesis(status="WATCH"):
    return {
        "ticker": "TEST", "as_of": AS_OF, "status": status,
        "evidence_confidence": "HIGH", "thesis_strength": "MEDIUM",
        "event_assessments": [],
    }


def _bars(closes, *, current_volume=1000.0):
    result = []
    start_ordinal = 730000
    for index, close in enumerate(closes):
        day = __import__("datetime").date.fromordinal(start_ordinal + index)
        result.append({
            "date": day.isoformat(), "open": close - 0.2, "high": close + 1.0,
            "low": close - 1.0, "close": close,
            "volume": current_volume if index == len(closes) - 1 else 1000.0,
        })
    shift = __import__("datetime").date.fromisoformat(AS_OF).toordinal() - start_ordinal - len(result) + 1
    for index, item in enumerate(result):
        item["date"] = __import__("datetime").date.fromordinal(
            start_ordinal + index + shift
        ).isoformat()
    return result


def _uptrend(length=240):
    return [100.0 + 0.30 * index for index in range(length)]


def _pullback_prices():
    values = [100.0 + 0.30 * index for index in range(235)]
    values.extend([169.0, 168.0, 167.0, 166.0, 165.5])
    return values


class USASetupV1Tests(unittest.TestCase):
    def test_valid_pullback(self):
        result = build_setup(_thesis(), _bars(_pullback_prices()), AS_OF)
        self.assertEqual(result.setup_type, "PULLBACK")
        self.assertGreaterEqual(result.rr_tp1, 1.5)

    def test_valid_breakout(self):
        result = build_setup(_thesis("INVESTIGATE"), _bars(
            _uptrend(), current_volume=1600.0
        ), AS_OF)
        self.assertEqual(result.setup_type, "BREAKOUT")
        self.assertIn("BUY ABOVE", result.entry_condition)

    def test_downtrend_has_no_long_setup(self):
        closes = [200.0 - 0.30 * index for index in range(240)]
        result = build_setup(_thesis(), _bars(closes), AS_OF)
        self.assertEqual(result.setup_type, "NO_SETUP")
        self.assertIn("DOWNTREND_NO_LONG_SETUP", result.reason_codes)

    def test_insufficient_rr_is_no_setup(self):
        result = build_setup(_thesis(), _bars(_uptrend()), AS_OF)
        self.assertEqual(result.setup_type, "NO_SETUP")
        self.assertIn("RR_TP1_BELOW_1_5", result.reason_codes)

    def test_pass_is_no_setup(self):
        result = build_setup(_thesis("PASS"), _bars(_pullback_prices()), AS_OF)
        self.assertEqual(result.setup_type, "NO_SETUP")
        self.assertEqual(result.reason_codes, ("RESEARCH_STATUS_PASS",))

    def test_insufficient_data_is_no_setup(self):
        result = build_setup(_thesis(), _bars(_uptrend(100)), AS_OF)
        self.assertEqual(result.setup_type, "NO_SETUP")
        self.assertEqual(result.reason_codes, ("INSUFFICIENT_OHLCV",))

    def test_stop_is_below_entry_and_distance_is_consistent(self):
        result = build_setup(_thesis(), _bars(
            _uptrend(), current_volume=1600.0
        ), AS_OF)
        self.assertLess(result.stop_level, result.entry_level)
        expected = 100 * (result.entry_level - result.stop_level) / result.entry_level
        self.assertAlmostEqual(result.stop_distance_pct, expected, places=3)

    def test_targets_and_rewards_are_consistent(self):
        result = build_setup(_thesis(), _bars(
            _uptrend(), current_volume=1600.0
        ), AS_OF)
        self.assertLess(result.entry_level, result.tp1)
        self.assertLess(result.tp1, result.tp2)
        self.assertAlmostEqual(result.rr_tp1, 2.0, places=3)
        self.assertAlmostEqual(result.rr_tp2, 3.0, places=3)

    def test_future_bar_does_not_leak(self):
        bars = _bars(_pullback_prices())
        baseline = build_setup(_thesis(), bars, AS_OF)
        future = {**bars[-1], "date": "2026-09-13", "open": 999.0, "high": 1001.0,
                  "low": 998.0, "close": 1000.0, "volume": 9999999.0}
        with_future = build_setup(_thesis(), [*bars, future], AS_OF)
        self.assertEqual(baseline, with_future)

    def test_same_input_has_same_output(self):
        bars = _bars(_pullback_prices())
        self.assertEqual(
            build_setup(_thesis(), bars, AS_OF),
            build_setup(_thesis(), bars, AS_OF),
        )


if __name__ == "__main__":
    unittest.main()
