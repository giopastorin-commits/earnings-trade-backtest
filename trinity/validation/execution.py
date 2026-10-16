"""Diagnostic execution lifecycle for immutable long Setups."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import math
from typing import Mapping, Sequence

from .contracts import SetupObservation, ValidationContractError


@dataclass(frozen=True)
class ExecutionOutcome:
    ticker: str
    status: str
    fill_session: str | None
    fill_price: float | None
    exit_session: str | None
    exit_price: float | None
    result_r: float | None
    mfe_r: float | None
    mae_r: float | None
    ambiguous_ohlc: bool
    policy: str = "STOP_FIRST"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _ohlc(rows: Sequence[Mapping[str, object]]) -> list[dict[str, float | str]]:
    result: list[dict[str, float | str]] = []
    previous: date | None = None
    for row in rows:
        try:
            session = date.fromisoformat(str(row["date"]))
            values = {key: float(row[key]) for key in ("open", "high", "low", "close")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationContractError("invalid execution OHLC bar") from exc
        if previous is not None and session <= previous:
            raise ValidationContractError("execution bars must be strictly ordered")
        if any(not math.isfinite(x) or x <= 0 for x in values.values()) \
                or values["high"] < max(values["open"], values["close"]) \
                or values["low"] > min(values["open"], values["close"]):
            raise ValidationContractError("inconsistent execution OHLC bar")
        previous = session
        result.append({"date": session.isoformat(), **values})
    return result


def evaluate_execution_lifecycle(
    setup: SetupObservation, bars: Sequence[Mapping[str, object]], *, through_session: str,
) -> ExecutionOutcome:
    """Evaluate activation and exits without changing the frozen Setup."""

    if setup.setup_type == "NO_SETUP":
        return ExecutionOutcome(setup.ticker, "NOT_TRIGGERED", None, None, None, None, None, None, None, False)
    if any(value is None for value in (setup.entry_level, setup.entry_lower, setup.entry_upper,
                                       setup.stop, setup.tp1, setup.tp2)):
        raise ValidationContractError("execution requires complete frozen levels")
    normalized = _ohlc(bars)
    eligible = [row for row in normalized if str(row["date"]) > setup.setup_timestamp]
    expiry = setup.expiry
    fill_index: int | None = None
    for index, row in enumerate(eligible):
        if expiry is not None and str(row["date"]) > expiry:
            break
        if float(row["low"]) <= float(setup.entry_upper) and float(row["high"]) >= float(setup.entry_lower):
            fill_index = index
            break
        if float(row["low"]) <= float(setup.stop):
            return ExecutionOutcome(setup.ticker, "INVALIDATED_BEFORE_ENTRY", None, None,
                                    str(row["date"]), float(setup.stop), None, None, None, False)
    if fill_index is None:
        expired = expiry is not None and through_session >= expiry
        return ExecutionOutcome(setup.ticker, "EXPIRED" if expired else "NOT_TRIGGERED",
                                None, None, None, None, None, None, None, False)
    fill = float(setup.entry_level)
    risk = fill - float(setup.stop)
    if risk <= 0:
        raise ValidationContractError("Setup has non-positive initial risk")
    path = eligible[fill_index:]
    max_high = fill
    min_low = fill
    for row in path:
        high, low = float(row["high"]), float(row["low"])
        max_high, min_low = max(max_high, high), min(min_low, low)
        stop_hit = low <= float(setup.stop)
        tp1_hit = high >= float(setup.tp1)
        tp2_hit = high >= float(setup.tp2)
        ambiguous = stop_hit and (tp1_hit or tp2_hit)
        if stop_hit:  # frozen V1 primary policy: stop first
            return ExecutionOutcome(
                setup.ticker, "STOPPED", str(path[0]["date"]), fill, str(row["date"]),
                float(setup.stop), -1.0, (max_high - fill) / risk, (min_low - fill) / risk,
                ambiguous,
            )
        if tp2_hit:
            return ExecutionOutcome(
                setup.ticker, "TP2", str(path[0]["date"]), fill, str(row["date"]),
                float(setup.tp2), (float(setup.tp2) - fill) / risk,
                (max_high - fill) / risk, (min_low - fill) / risk, False,
            )
        if tp1_hit:
            return ExecutionOutcome(
                setup.ticker, "TP1", str(path[0]["date"]), fill, str(row["date"]),
                float(setup.tp1), (float(setup.tp1) - fill) / risk,
                (max_high - fill) / risk, (min_low - fill) / risk, False,
            )
    return ExecutionOutcome(
        setup.ticker, "ENTRY_TRIGGERED", str(path[0]["date"]), fill, None, None, None,
        (max_high - fill) / risk, (min_low - fill) / risk, False,
    )
