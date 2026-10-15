"""Deterministic execution-time assessment for an immutable TRINITY Setup."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
from typing import Any, Mapping

from trinity.ledger import canonicalize_json, setup_result_value
from trinity.ledger.contracts_v14 import MINIMUM_RR_TP1
from trinity.usa_forward import MAX_PRICE_AGE_DAYS


EXECUTION_POLICY_VERSION = "ENTRY_FRESHNESS_V1"
EXECUTION_STATUSES = frozenset({"VALID", "WAIT", "EXTENDED", "INVALIDATED", "RR_TOO_LOW"})
PRICE_FRESHNESS_VERIFIED = "VERIFIED_SAME_SESSION"
PRICE_FRESHNESS_UNVERIFIED = "UNVERIFIED"

_PULLBACK = re.compile(
    r"BUY ON PULLBACK (?P<lower>[0-9]+(?:\.[0-9]+)?)-(?P<upper>[0-9]+(?:\.[0-9]+)?)\Z"
)
_BREAKOUT = re.compile(r"BUY ABOVE (?P<entry>[0-9]+(?:\.[0-9]+)?)\Z")


@dataclass(frozen=True)
class ExecutionPolicy:
    """Explicit operational policy; it never changes the source Setup."""

    version: str = EXECUTION_POLICY_VERSION
    minimum_current_rr1: Decimal = MINIMUM_RR_TP1
    maximum_price_age_days: int = MAX_PRICE_AGE_DAYS

    def __post_init__(self) -> None:
        if self.minimum_current_rr1 <= 0 or self.maximum_price_age_days < 0:
            raise ValueError("execution policy thresholds must be non-negative")


@dataclass(frozen=True)
class ExecutionStatus:
    ticker: str
    setup_id: str
    setup_sha256: str
    current_price: str | None
    price_provider: str
    price_as_of: str
    price_retrieved_at: str
    price_freshness: str
    entry_lower: str | None
    entry_upper: str | None
    distance_from_entry_lower: str | None
    distance_from_entry_upper: str | None
    distance_from_entry: str | None
    distance_from_entry_pct: str | None
    current_risk: str | None
    current_reward_tp1: str | None
    current_reward_tp2: str | None
    current_rr1: str | None
    current_rr2: str | None
    execution_status: str
    reason_codes: tuple[str, ...]
    policy_version: str
    minimum_current_rr1: str

    def __post_init__(self) -> None:
        if self.execution_status not in EXECUTION_STATUSES:
            raise ValueError("invalid execution status")
        if not self.ticker or not self.setup_id or len(self.setup_sha256) != 64:
            raise ValueError("invalid execution identity")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_execution_status(
    setup: object,
    *,
    setup_id: str,
    current_price: object,
    price_as_of: str,
    price_provider: str,
    price_retrieved_at: str,
    policy: ExecutionPolicy = ExecutionPolicy(),
) -> ExecutionStatus:
    """Assess current executability without mutating or rebuilding ``setup``.

    Freshness is deliberately tied to the verified snapshot session, not wall
    clock age: the caller supplies the already validated end-of-session close.
    Missing or mismatched provenance fails closed to WAIT with no displayed RR.
    """

    source = asdict(setup) if is_dataclass(setup) else dict(_mapping(setup))
    ticker = str(source.get("ticker") or "")
    setup_as_of = str(source.get("as_of") or "")
    setup_type = str(source.get("setup_type") or "")
    if setup_type not in {"PULLBACK", "BREAKOUT"}:
        raise ValueError("execution freshness requires an operational long Setup")
    setup_value = setup_result_value(source)
    setup_sha256 = hashlib.sha256(canonicalize_json(setup_value)).hexdigest()
    lower, upper = _entry_bounds(source)

    freshness_reasons = _freshness_reasons(
        setup_as_of=setup_as_of,
        price_as_of=price_as_of,
        price_provider=price_provider,
        price_retrieved_at=price_retrieved_at,
        maximum_price_age_days=policy.maximum_price_age_days,
    )
    price = _decimal_or_none(current_price)
    if price is None:
        freshness_reasons.append("CURRENT_PRICE_INVALID")
    if freshness_reasons:
        return _status(
            ticker=ticker, setup_id=setup_id, setup_sha256=setup_sha256,
            price=price, price_provider=price_provider, price_as_of=price_as_of,
            price_retrieved_at=price_retrieved_at, lower=lower, upper=upper,
            policy=policy, execution_status="WAIT",
            reason_codes=tuple(freshness_reasons), verified=False,
        )

    stop = _required_decimal(source, "stop_level")
    tp1 = _required_decimal(source, "tp1")
    tp2 = _required_decimal(source, "tp2")
    assert price is not None
    risk = price - stop
    reward1 = tp1 - price
    reward2 = tp2 - price
    rr1 = reward1 / risk if risk > 0 and reward1 > 0 else None
    rr2 = reward2 / risk if risk > 0 and reward2 > 0 else None

    if price <= stop or risk <= 0:
        status, reasons = "INVALIDATED", ("STOP_OR_INVALIDATION_REACHED",)
    elif price < lower:
        status, reasons = "WAIT", ("PRICE_BELOW_ENTRY_ZONE",)
    elif reward1 <= 0:
        status, reasons = "RR_TOO_LOW", ("TP1_REACHED_OR_EXCEEDED",)
    elif rr1 is None or rr1 < policy.minimum_current_rr1:
        status, reasons = "RR_TOO_LOW", ("CURRENT_RR1_BELOW_MINIMUM",)
    elif price > upper:
        status, reasons = "EXTENDED", ("PRICE_ABOVE_ENTRY_ZONE", "DO_NOT_CHASE")
    else:
        status, reasons = "VALID", ("PRICE_IN_ENTRY_ZONE", "CURRENT_RR1_ACCEPTABLE")

    return _status(
        ticker=ticker, setup_id=setup_id, setup_sha256=setup_sha256,
        price=price, price_provider=price_provider, price_as_of=price_as_of,
        price_retrieved_at=price_retrieved_at, lower=lower, upper=upper,
        policy=policy, execution_status=status, reason_codes=reasons, verified=True,
        risk=risk, reward1=reward1, reward2=reward2, rr1=rr1, rr2=rr2,
    )


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("Setup must be a dataclass or mapping")
    return value


def _entry_bounds(source: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    condition = str(source.get("entry_condition") or "")
    entry = _required_decimal(source, "entry_level")
    if source.get("setup_type") == "PULLBACK":
        match = _PULLBACK.fullmatch(condition)
        if match is None:
            raise ValueError("PULLBACK entry range is not deterministically parseable")
        lower = Decimal(match.group("lower"))
        upper = Decimal(match.group("upper"))
        if lower > upper or upper != entry:
            raise ValueError("PULLBACK entry range differs from frozen entry level")
        return lower, upper
    match = _BREAKOUT.fullmatch(condition)
    if match is None or Decimal(match.group("entry")) != entry:
        raise ValueError("BREAKOUT entry condition differs from frozen entry level")
    return entry, entry


def _freshness_reasons(
    *, setup_as_of: str, price_as_of: str, price_provider: str, price_retrieved_at: str,
    maximum_price_age_days: int,
) -> list[str]:
    reasons: list[str] = []
    if not setup_as_of or price_as_of != setup_as_of:
        reasons.append("PRICE_SESSION_UNVERIFIED")
    if not price_provider or price_provider == "-":
        reasons.append("PRICE_PROVIDER_UNVERIFIED")
    try:
        parsed = datetime.fromisoformat(price_retrieved_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        session = datetime.fromisoformat(price_as_of).date()
        age_days = (parsed.astimezone(timezone.utc).date() - session).days
        if age_days < 0 or age_days > maximum_price_age_days:
            reasons.append("PRICE_STALE")
    except (AttributeError, TypeError, ValueError):
        reasons.append("PRICE_RETRIEVAL_UNVERIFIED")
    return reasons


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return result


def _required_decimal(source: Mapping[str, Any], field: str) -> Decimal:
    value = _decimal_or_none(source.get(field))
    if value is None:
        raise ValueError(f"Setup lacks valid {field}")
    return value


def _canonical(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return format(value.normalize(), "f")


def _status(
    *, ticker: str, setup_id: str, setup_sha256: str, price: Decimal | None,
    price_provider: str, price_as_of: str, price_retrieved_at: str,
    lower: Decimal, upper: Decimal, policy: ExecutionPolicy,
    execution_status: str, reason_codes: tuple[str, ...], verified: bool,
    risk: Decimal | None = None, reward1: Decimal | None = None,
    reward2: Decimal | None = None, rr1: Decimal | None = None,
    rr2: Decimal | None = None,
) -> ExecutionStatus:
    below = price - lower if price is not None else None
    above = price - upper if price is not None else None
    distance = (
        below if price is not None and price < lower
        else above if price is not None and price > upper
        else Decimal(0) if price is not None else None
    )
    distance_pct = distance / upper * Decimal(100) if distance is not None and upper > 0 else None
    return ExecutionStatus(
        ticker=ticker, setup_id=setup_id, setup_sha256=setup_sha256,
        current_price=_canonical(price), price_provider=price_provider,
        price_as_of=price_as_of, price_retrieved_at=price_retrieved_at,
        price_freshness=PRICE_FRESHNESS_VERIFIED if verified else PRICE_FRESHNESS_UNVERIFIED,
        entry_lower=_canonical(lower), entry_upper=_canonical(upper),
        distance_from_entry_lower=_canonical(below),
        distance_from_entry_upper=_canonical(above),
        distance_from_entry=_canonical(distance),
        distance_from_entry_pct=_canonical(distance_pct),
        current_risk=_canonical(risk), current_reward_tp1=_canonical(reward1),
        current_reward_tp2=_canonical(reward2), current_rr1=_canonical(rr1),
        current_rr2=_canonical(rr2), execution_status=execution_status,
        reason_codes=reason_codes, policy_version=policy.version,
        minimum_current_rr1=_canonical(policy.minimum_current_rr1) or "1.5",
    )
