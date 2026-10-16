"""Frozen cohort membership and deterministic equal-weight basket returns."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Iterable, Mapping

from .contracts import SelectionObservation, ValidationContractError


@dataclass(frozen=True)
class Cohorts:
    scanner: tuple[str, ...]
    luna: tuple[str, ...]
    sol: tuple[str, ...]


def freeze_memberships(rows: Iterable[SelectionObservation]) -> Cohorts:
    values = tuple(rows)
    scanner = tuple(sorted(item.ticker for item in values if item.scanner_selected))
    luna = tuple(sorted(item.ticker for item in values if item.luna_decision == "ESCALATE"))
    sol = tuple(sorted(item.ticker for item in values if item.sol_status == "PROMOTED"))
    if len(scanner) != len(set(scanner)) or not set(luna).issubset(scanner) or not set(sol).issubset(luna):
        raise ValidationContractError("cohort membership violates funnel ordering")
    return Cohorts(scanner, luna, sol)


def equal_weight_return(members: Iterable[str], returns: Mapping[str, float], *,
                        benchmark_return: float | None = None, empty_sol: bool = False) -> float:
    tickers = tuple(sorted(set(members)))
    if not tickers:
        if empty_sol and benchmark_return is not None:
            return benchmark_return
        raise ValidationContractError("empty basket lacks frozen benchmark sleeve")
    missing = [ticker for ticker in tickers if ticker not in returns]
    if missing:
        raise ValidationContractError("basket return is missing member outcomes")
    return fmean(returns[ticker] for ticker in tickers)
