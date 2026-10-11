"""Synthetic regression tests for deterministic USA Setup Engine V1."""

from dataclasses import replace
import unittest
from unittest.mock import patch

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


def _pullback_metrics(*, high_20d, high_60d, low_20d=164.5406, atr14=4.0):
    return {
        "close": 174.0,
        "sma20": 173.8263,
        "sma50": 170.0,
        "sma200": 150.0,
        "distance_sma20_pct": 0.1,
        "distance_sma50_pct": 2.4,
        "distance_sma200_pct": 16.0,
        "return_5d": -1.0,
        "return_20d": 2.0,
        "return_60d": 8.0,
        "high_20d": high_20d,
        "low_20d": low_20d,
        "prior_high_20d": 180.0,
        "high_60d": high_60d,
        "atr14": atr14,
        "rsi14": 50.0,
        "average_volume_20d": 1000.0,
        "relative_volume": 1.0,
    }


def _build_with_metrics(metrics):
    with patch("trinity.usa_setup_v1._technical_metrics", return_value=metrics):
        return build_setup(_thesis(), _bars(_uptrend(201)), AS_OF)


class USASetupV1Tests(unittest.TestCase):
    def test_valid_pullback(self):
        result = build_setup(_thesis(), _bars(_pullback_prices()), AS_OF)
        self.assertEqual(result.setup_type, "PULLBACK")
        self.assertGreaterEqual(result.rr_tp1, 1.5)
        self.assertEqual(
            (result.entry_level, result.stop_level, result.tp1, result.tp2,
             result.rr_tp1, result.rr_tp2, result.reason_codes),
            (164.4536, 161.4321, 171.2, 173.5179, 2.2329, 3.0,
             ("UPTREND_CONFIRMED", "SMA_SUPPORT_NEARBY")),
        )

    def test_valid_breakout(self):
        result = build_setup(_thesis("INVESTIGATE"), _bars(
            _uptrend(), current_volume=1600.0
        ), AS_OF)
        self.assertEqual(result.setup_type, "BREAKOUT")
        self.assertIn("BUY ABOVE", result.entry_condition)
        self.assertEqual(
            (result.entry_level, result.stop_level, result.tp1, result.tp2,
             result.rr_tp1, result.rr_tp2, result.reason_codes),
            (172.6, 168.6, 180.6, 184.6, 2.0, 3.0,
             ("UPTREND_CONFIRMED", "BREAKOUT_VOLUME_CONFIRMED")),
        )

    def test_pullback_tp2_valid_through_high_60d_is_unchanged(self):
        result = _build_with_metrics(_pullback_metrics(
            high_20d=200.0, high_60d=210.0,
        ))
        self.assertEqual(result.setup_type, "PULLBACK")
        self.assertEqual(result.tp2, 210.0)

    def test_pullback_tp2_valid_through_existing_3r_fallback_is_unchanged(self):
        result = _build_with_metrics(_pullback_metrics(
            high_20d=200.0, high_60d=200.0,
        ))
        self.assertEqual(result.setup_type, "PULLBACK")
        self.assertEqual(result.tp2, 205.6834)
        self.assertEqual(result.rr_tp2, 3.0)

    def test_pullback_qcom_geometry_fails_closed_when_3r_does_not_exceed_tp1(self):
        result = _build_with_metrics(_pullback_metrics(
            high_20d=205.85, high_60d=205.85,
        ))
        self.assertEqual(result.setup_type, "NO_SETUP")
        self.assertIn("TP2_NOT_DETERMINABLE", result.reason_codes)
        self.assertIsNone(result.entry_level)
        self.assertIsNone(result.tp2)

    def test_pullback_rounded_tp1_tp2_collision_fails_closed(self):
        metrics = _pullback_metrics(
            high_20d=102.99996, high_60d=102.99996,
            low_20d=99.0, atr14=0.5,
        )
        metrics.update(close=100.0, sma20=99.875, sma50=98.0, sma200=90.0,
                       prior_high_20d=110.0)
        result = _build_with_metrics(metrics)
        self.assertEqual(result.setup_type, "NO_SETUP")
        self.assertIn("TP2_NOT_DETERMINABLE", result.reason_codes)

    def test_setup_record_ordering_invariant_remains_strict(self):
        valid = build_setup(_thesis("INVESTIGATE"), _bars(
            _uptrend(), current_volume=1600.0
        ), AS_OF)
        with self.assertRaisesRegex(
            ValueError, "setup levels must be ordered stop < entry < TP1 < TP2"
        ):
            replace(valid, tp2=valid.tp1)

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
