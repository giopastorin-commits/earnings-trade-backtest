"""Independent building blocks for TRINITY/H2 research."""

from .outcomes import (
    EventTiming,
    InsufficientDataError,
    OutcomeInputError,
    calculate_event_outcomes,
)

__all__ = [
    "EventTiming",
    "InsufficientDataError",
    "OutcomeInputError",
    "calculate_event_outcomes",
]
