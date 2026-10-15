from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal

import pytest

from trinity.execution_freshness import (
    ExecutionPolicy,
    evaluate_execution_status,
)
from trinity.notifications.telegram import TelegramNotification, render_telegram_message
from trinity.usa_setup_v1 import SetupRecord


SETUP_ID = "11111111-1111-4111-8111-111111111111"
RETRIEVED_AT = "2026-10-10T01:02:03.000000Z"


def _setup(**changes) -> SetupRecord:
    values = dict(
        ticker="SWKS", as_of="2026-10-09", research_status="INVESTIGATE",
        evidence_confidence="HIGH", thesis_strength="MEDIUM", technical_regime="UPTREND",
        setup_type="PULLBACK", entry_condition="BUY ON PULLBACK 76.1342-77.2496",
        entry_level=77.2496, stop_level=70.5574, stop_distance_pct=8.6637,
        tp1=92.8, tp2=97.326, risk_per_share=6.6922, reward_tp1=15.5504,
        reward_tp2=20.0764, rr_tp1=2.3237, rr_tp2=3.0, close=79.21,
        atr14=4.4615, rsi14=60.0, sma20=76.1342, sma50=72.0, sma200=65.0,
        distance_sma20_pct=4.04, distance_sma50_pct=10.01, distance_sma200_pct=21.86,
        return_5d=2.0, return_20d=5.0, return_60d=10.0, high_20d=92.8,
        low_20d=70.5574, average_volume_20d=1_000_000.0, relative_volume=1.1,
        reason_codes=("UPTREND_CONFIRMED", "SMA_SUPPORT_NEARBY"),
        invalidation="Setup invalid below 70.5574 on a closing basis.",
    )
    values.update(changes)
    return SetupRecord(**values)


def _evaluate(price, setup=None, **changes):
    arguments = dict(
        setup_id=SETUP_ID, current_price=price, price_as_of="2026-10-09",
        price_provider="TWELVE_DATA", price_retrieved_at=RETRIEVED_AT,
    )
    arguments.update(changes)
    return evaluate_execution_status(setup or _setup(), **arguments)


def test_price_inside_entry_zone_with_adequate_rr_is_valid():
    result = _evaluate("76.5")
    assert result.execution_status == "VALID"
    assert Decimal(result.current_rr1) == (Decimal("92.8") - Decimal("76.5")) / (
        Decimal("76.5") - Decimal("70.5574")
    )
    assert result.price_freshness == "VERIFIED_SAME_SESSION"


def test_price_below_entry_zone_is_wait():
    result = _evaluate("75")
    assert result.execution_status == "WAIT"
    assert result.reason_codes == ("PRICE_BELOW_ENTRY_ZONE",)
    assert result.distance_from_entry == "-1.1342"


def test_price_above_entry_zone_with_adequate_rr_is_extended():
    result = _evaluate("78")
    assert result.execution_status == "EXTENDED"
    assert result.reason_codes == ("PRICE_ABOVE_ENTRY_ZONE", "DO_NOT_CHASE")


def test_current_rr_below_configured_threshold_takes_precedence_over_extended():
    result = _evaluate("80")
    assert result.execution_status == "RR_TOO_LOW"
    assert Decimal(result.current_rr1) < Decimal("1.5")
    assert result.minimum_current_rr1 == "1.5"


@pytest.mark.parametrize("price", ["70.5574", "70", "-1"])
def test_price_at_or_below_stop_is_fail_safe(price):
    result = _evaluate(price)
    assert result.execution_status == "INVALIDATED"
    assert result.current_rr1 is None


def test_tp1_reached_never_displays_positive_rr1():
    result = _evaluate("93")
    assert result.execution_status == "RR_TOO_LOW"
    assert result.current_reward_tp1 == "-0.2"
    assert result.current_rr1 is None
    assert result.reason_codes == ("TP1_REACHED_OR_EXCEEDED",)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"price_as_of": "2026-10-08"}, "PRICE_SESSION_UNVERIFIED"),
        ({"price_provider": "-"}, "PRICE_PROVIDER_UNVERIFIED"),
        ({"price_retrieved_at": ""}, "PRICE_RETRIEVAL_UNVERIFIED"),
        ({"price_retrieved_at": "2026-10-20T01:00:00.000000Z"}, "PRICE_STALE"),
        ({"current_price": None}, "CURRENT_PRICE_INVALID"),
        ({"current_price": "not-a-price"}, "CURRENT_PRICE_INVALID"),
    ],
)
def test_unverified_or_missing_price_is_never_valid(changes, reason):
    price = changes.pop("current_price", "76.5")
    result = _evaluate(price, **changes)
    assert result.execution_status == "WAIT"
    assert result.price_freshness == "UNVERIFIED"
    assert reason in result.reason_codes
    assert result.current_rr1 is None


def test_original_setup_and_original_rr_are_immutable():
    setup = _setup()
    before = asdict(setup)
    result = _evaluate("80", setup=setup)
    assert asdict(setup) == before
    assert setup.rr_tp1 == 2.3237
    assert result.current_rr1 != str(setup.rr_tp1)


def test_pullback_range_must_match_frozen_entry_level():
    setup = replace(_setup(), entry_condition="BUY ON PULLBACK 76.1342-77.0000")
    with pytest.raises(ValueError, match="differs from frozen entry"):
        _evaluate("76.5", setup=setup)


def test_single_entry_breakout_is_supported_without_tolerance_policy():
    setup = replace(
        _setup(), ticker="MRNA", setup_type="BREAKOUT",
        entry_condition="BUY ABOVE 213.8284", entry_level=213.8284,
        stop_level=181.6603, tp1=278.1647, tp2=310.3328,
        risk_per_share=32.1681, reward_tp1=64.3363, reward_tp2=96.5044,
        rr_tp1=2.0, rr_tp2=3.0,
        invalidation="Setup invalid below 181.6603 on a closing basis.",
    )
    result = _evaluate("225", setup=setup)
    assert result.execution_status == "RR_TOO_LOW"
    assert result.entry_lower == result.entry_upper == "213.8284"
    assert Decimal(result.current_rr1) == (Decimal("278.1647") - Decimal("225")) / (
        Decimal("225") - Decimal("181.6603")
    )


def test_policy_is_explicit_and_configurable():
    result = evaluate_execution_status(
        _setup(), setup_id=SETUP_ID, current_price="80", price_as_of="2026-10-09",
        price_provider="TWELVE_DATA", price_retrieved_at=RETRIEVED_AT,
        policy=ExecutionPolicy(minimum_current_rr1=Decimal("1.25")),
    )
    assert result.execution_status == "EXTENDED"
    assert result.minimum_current_rr1 == "1.25"


def test_report_separates_original_setup_from_current_execution():
    execution = _evaluate("80").to_dict()
    notification = TelegramNotification(
        setup_id=SETUP_ID, run_id="run", research_id="research", ticker="SWKS",
        setup_type="PULLBACK", technical_regime="UPTREND",
        research_status="INVESTIGATE", evidence_confidence="HIGH", thesis_strength="MEDIUM",
        entry="77.2496", stop="70.5574", tp1="92.8", tp2="97.326",
        rr1="2.3237", rr2="3", reason_codes=("UPTREND_CONFIRMED",),
        resolved_pit_class="PIT_SAFE_DECISION", resolved_record_class="PROSPECTIVE",
        research={"thesis_invalidation": "Close below stop."}, execution_status=execution,
    )
    text = render_telegram_message(notification)
    assert "PIANO OPERATIVO\nSETUP ORIGINALE\nEntry: 77.2496" in text
    assert "RR1: 2.3237" in text
    assert "ESECUZIONE CORRENTE" in text
    assert "Price: 80" in text
    assert "Current RR1: " + execution["current_rr1"] in text
    assert "Execution status: RR_TOO_LOW" in text
    assert "Action: WAIT / DO NOT CHASE" in text


def test_execution_evaluation_never_calls_build_setup(monkeypatch):
    monkeypatch.setattr(
        "trinity.usa_setup_v1.build_setup",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("build_setup called")),
    )
    assert _evaluate("76.5").execution_status == "VALID"
